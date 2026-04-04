"""Tests for the Pipeline orchestrator.

Covers: normal execution, partial failure, full-failure skip, retry,
event callbacks, and dry-run.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.core.pipeline import Pipeline, PipelineEvent
from src.core.task_context import TaskContext
from src.core.task_graph import AgentResult, AgentStatus, TaskGraph, TaskNode, TaskState


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

class _StubConfig:
    """Minimal stand-in for Config with a working_dir."""

    def __init__(self, working_dir: str, config: dict | None = None):
        self.working_dir = working_dir
        self.config = config or {
            "target_name": "TestCo",
            "stock_code": "000001",
            "target_type": "financial_company",
            "language": "zh",
            "custom_collect_tasks": [],
            "custom_analysis_tasks": [],
        }
        self.llm_dict = {}


class _FakeAgent:
    """Agent whose ``async_run`` simply records a call and optionally raises."""

    AGENT_NAME = "fake"
    calls: list = []

    def __init__(self, *, config=None, task_context=None, agent_id="", **kwargs):
        self.id = agent_id
        self.checkpoint_mgr = None

    async def async_run(self, **kwargs):
        _FakeAgent.calls.append(kwargs)

    @classmethod
    async def from_checkpoint(cls, **kwargs):
        return None  # never "restore" in tests


class _FailingAgent(_FakeAgent):
    """Always raises on first call, succeeds on second (for retry tests)."""

    _attempt: dict = {}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        _FailingAgent._attempt.setdefault(self.id, 0)

    async def async_run(self, **kwargs):
        _FailingAgent._attempt[self.id] += 1
        if _FailingAgent._attempt[self.id] <= 1:
            raise RuntimeError(f"{self.id} failed")
        _FakeAgent.calls.append(kwargs)


class _AlwaysFailAgent(_FakeAgent):
    """Always raises."""

    async def async_run(self, **kwargs):
        raise RuntimeError(f"{self.id} always fails")


def _build_simple_graph(agent_cls=_FakeAgent) -> TaskGraph:
    """A→B (hard dep) — minimal two-node DAG."""
    g = TaskGraph()
    g.add_task(TaskNode(task_id="A", agent_class=agent_cls, run_kwargs={"x": 1}))
    g.add_task(TaskNode(
        task_id="B", agent_class=agent_cls,
        run_kwargs={"x": 2}, depends_on=["A"],
    ))
    return g


def _build_soft_graph(
    collector_cls=_FakeAgent,
    analyzer_cls=_FakeAgent,
    report_cls=_FakeAgent,
) -> TaskGraph:
    """c0,c1 (parallel) → a0 (soft dep, min=1) → report (soft dep, min=1)."""
    g = TaskGraph()
    g.add_task(TaskNode(task_id="c0", agent_class=collector_cls))
    g.add_task(TaskNode(task_id="c1", agent_class=collector_cls))
    g.add_task(TaskNode(
        task_id="a0", agent_class=analyzer_cls,
        soft_depends_on=["c0", "c1"], min_soft_deps=1,
    ))
    g.add_task(TaskNode(
        task_id="report", agent_class=report_cls,
        soft_depends_on=["a0"], min_soft_deps=1,
    ))
    return g


# ------------------------------------------------------------------
# 1. Normal execution
# ------------------------------------------------------------------

class TestNormalExecution:
    @pytest.fixture(autouse=True)
    def _reset(self):
        _FakeAgent.calls = []

    @pytest.fixture
    def cfg(self, tmp_path):
        return _StubConfig(str(tmp_path))

    async def test_all_tasks_done(self, cfg):
        graph = _build_simple_graph()
        pipeline = Pipeline(config=cfg, max_concurrent=2)
        await pipeline._execute_graph(graph, TaskContext.from_config(cfg))

        assert graph._nodes["A"].state == TaskState.DONE
        assert graph._nodes["B"].state == TaskState.DONE
        assert graph.is_complete()

    async def test_soft_dep_graph_completes(self, cfg):
        graph = _build_soft_graph()
        pipeline = Pipeline(config=cfg, max_concurrent=4)
        await pipeline._execute_graph(graph, TaskContext.from_config(cfg))

        assert all(n.state == TaskState.DONE for n in graph._nodes.values())


# ------------------------------------------------------------------
# 2. Partial failure
# ------------------------------------------------------------------

class TestPartialFailure:
    @pytest.fixture(autouse=True)
    def _reset(self):
        _FakeAgent.calls = []

    @pytest.fixture
    def cfg(self, tmp_path):
        return _StubConfig(str(tmp_path))

    async def test_soft_dep_failure_does_not_block(self, cfg):
        """One collector fails, but analyzer still runs (min_soft_deps=1)."""
        graph = _build_soft_graph(collector_cls=_FakeAgent)
        # Make c1 fail
        graph._nodes["c1"].agent_class = _AlwaysFailAgent

        pipeline = Pipeline(config=cfg, max_concurrent=4)
        await pipeline._execute_graph(graph, TaskContext.from_config(cfg))

        assert graph._nodes["c0"].state == TaskState.DONE
        assert graph._nodes["c1"].state == TaskState.FAILED
        assert graph._nodes["a0"].state == TaskState.DONE
        assert graph._nodes["report"].state == TaskState.DONE

    async def test_hard_dep_failure_cascades(self, cfg):
        graph = _build_simple_graph(_AlwaysFailAgent)
        pipeline = Pipeline(config=cfg, max_concurrent=2)
        await pipeline._execute_graph(graph, TaskContext.from_config(cfg))

        assert graph._nodes["A"].state == TaskState.FAILED
        assert graph._nodes["B"].state == TaskState.SKIPPED


# ------------------------------------------------------------------
# 3. Full failure → all SKIP
# ------------------------------------------------------------------

class TestFullFailure:
    @pytest.fixture(autouse=True)
    def _reset(self):
        _FakeAgent.calls = []

    @pytest.fixture
    def cfg(self, tmp_path):
        return _StubConfig(str(tmp_path))

    async def test_all_collectors_fail_skips_downstream(self, cfg):
        graph = _build_soft_graph(
            collector_cls=_AlwaysFailAgent,
            analyzer_cls=_FakeAgent,
            report_cls=_FakeAgent,
        )
        pipeline = Pipeline(config=cfg, max_concurrent=4)
        await pipeline._execute_graph(graph, TaskContext.from_config(cfg))

        assert graph._nodes["c0"].state == TaskState.FAILED
        assert graph._nodes["c1"].state == TaskState.FAILED
        # min_soft_deps=1 not met → analyzer skipped
        assert graph._nodes["a0"].state == TaskState.SKIPPED
        assert graph._nodes["report"].state == TaskState.SKIPPED


# ------------------------------------------------------------------
# 4. Retry
# ------------------------------------------------------------------

class TestRetry:
    @pytest.fixture(autouse=True)
    def _reset(self):
        _FakeAgent.calls = []
        _FailingAgent._attempt = {}

    @pytest.fixture
    def cfg(self, tmp_path):
        return _StubConfig(str(tmp_path))

    async def test_retry_succeeds_on_second_attempt(self, cfg):
        graph = TaskGraph()
        graph.add_task(TaskNode(task_id="X", agent_class=_FailingAgent))

        pipeline = Pipeline(config=cfg, max_concurrent=1, max_retries=1)
        await pipeline._execute_graph(graph, TaskContext.from_config(cfg))

        assert graph._nodes["X"].state == TaskState.DONE

    async def test_no_retry_fails_immediately(self, cfg):
        graph = TaskGraph()
        graph.add_task(TaskNode(task_id="X", agent_class=_FailingAgent))

        pipeline = Pipeline(config=cfg, max_concurrent=1, max_retries=0)
        await pipeline._execute_graph(graph, TaskContext.from_config(cfg))

        assert graph._nodes["X"].state == TaskState.FAILED


# ------------------------------------------------------------------
# 5. Event callback
# ------------------------------------------------------------------

class TestEventCallbacks:
    @pytest.fixture(autouse=True)
    def _reset(self):
        _FakeAgent.calls = []

    @pytest.fixture
    def cfg(self, tmp_path):
        return _StubConfig(str(tmp_path))

    async def test_events_emitted(self, cfg):
        events: list[PipelineEvent] = []

        async def recorder(event: PipelineEvent):
            events.append(event)

        graph = _build_simple_graph()
        pipeline = Pipeline(config=cfg, max_concurrent=2, on_event=recorder)
        await pipeline._execute_graph(graph, TaskContext.from_config(cfg))

        types = [e.type for e in events]
        assert types.count("task_started") == 2
        assert types.count("task_completed") == 2

    async def test_failing_callback_does_not_crash_pipeline(self, cfg):
        async def bad_callback(event: PipelineEvent):
            raise ValueError("callback error")

        graph = _build_simple_graph()
        pipeline = Pipeline(config=cfg, max_concurrent=2, on_event=bad_callback)
        # Should not raise
        await pipeline._execute_graph(graph, TaskContext.from_config(cfg))
        assert graph.is_complete()


# ------------------------------------------------------------------
# 6. Dry run
# ------------------------------------------------------------------

class TestDryRun:
    @pytest.fixture
    def cfg(self, tmp_path):
        return _StubConfig(str(tmp_path))

    async def test_dry_run_does_not_execute(self, cfg, capsys):
        _FakeAgent.calls = []
        pipeline = Pipeline(config=cfg, dry_run=True)

        class _StubPlugin:
            name = "stub"
            def build_task_graph(self, config, ctx, collect, analyze, **kwargs):
                return _build_simple_graph()

        # Patch generate_tasks so no LLM is needed
        with patch("src.core.pipeline.generate_tasks", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = (["task_a"], ["task_b"])
            ctx = TaskContext.from_config(cfg)
            graph = await pipeline.run(ctx, plugin=_StubPlugin())

        captured = capsys.readouterr()
        assert "Dry Run" in captured.out
        assert _FakeAgent.calls == []  # nothing actually executed


# ------------------------------------------------------------------
# 7. Plugin.build_task_graph (replaces build_default_task_graph)
# ------------------------------------------------------------------

try:
    from src.agents import DataCollector  # noqa: F401 — just a probe
    from src.plugins.base_plugin import ReportPlugin
    _has_agents = True
except (ImportError, ModuleNotFoundError):
    ReportPlugin = None  # type: ignore[assignment,misc]
    _has_agents = False


@pytest.mark.skipif(not _has_agents, reason="Optional agent deps not installed")
class TestPluginBuildTaskGraph:
    def test_graph_shape(self):
        from src.plugins.financial_company.plugin import FinancialCompanyPlugin
        plugin = FinancialCompanyPlugin()
        cfg = _StubConfig("/tmp/test")
        ctx = TaskContext(
            config=cfg,
            target_name="TestCo",
            stock_code="000001",
            target_type="financial_company",
            language="zh",
        )
        graph = plugin.build_task_graph(cfg, ctx, ["c1", "c2"], ["a1"])

        assert "collect_0" in graph
        assert "collect_1" in graph
        assert "analyze_0" in graph
        assert "report" in graph
        assert len(graph) == 4

    def test_empty_tasks_still_creates_report(self):
        from src.plugins.general.plugin import GeneralPlugin
        plugin = GeneralPlugin()
        cfg = _StubConfig("/tmp/test")
        ctx = TaskContext(
            config=cfg,
            target_name="TestCo",
            stock_code="",
            target_type="general",
            language="en",
        )
        graph = plugin.build_task_graph(cfg, ctx, [], [])
        assert "report" in graph
        assert len(graph) == 1

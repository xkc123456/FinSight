"""Tests for TaskGraph: linear DAG, cascade skip, soft deps, min_soft_deps, serialisation."""
import pytest

from src.core.task_graph import (
    AgentResult,
    AgentStatus,
    TaskGraph,
    TaskNode,
    TaskState,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

class _DummyAgent:
    pass


def _node(tid, depends=None, soft_depends=None, min_soft=0):
    return TaskNode(
        task_id=tid,
        agent_class=_DummyAgent,
        depends_on=depends or [],
        soft_depends_on=soft_depends or [],
        min_soft_deps=min_soft,
    )


# ------------------------------------------------------------------
# 1. Linear chain
# ------------------------------------------------------------------

class TestLinearChain:
    def test_linear_execution_order(self):
        g = TaskGraph()
        g.add_task(_node("A"))
        g.add_task(_node("B", depends=["A"]))
        g.add_task(_node("C", depends=["B"]))

        # Only A is ready
        ready = g.get_ready_tasks()
        assert [n.task_id for n in ready] == ["A"]

        g.mark_done("A", AgentResult("A", AgentStatus.SUCCESS))
        ready = g.get_ready_tasks()
        assert [n.task_id for n in ready] == ["B"]

        g.mark_done("B", AgentResult("B", AgentStatus.SUCCESS))
        ready = g.get_ready_tasks()
        assert [n.task_id for n in ready] == ["C"]

        g.mark_done("C", AgentResult("C", AgentStatus.SUCCESS))
        assert g.is_complete()


# ------------------------------------------------------------------
# 2. Cascade skip on hard-dependency failure
# ------------------------------------------------------------------

class TestCascadeSkip:
    def test_failure_cascades_downstream(self):
        g = TaskGraph()
        g.add_task(_node("A"))
        g.add_task(_node("B", depends=["A"]))
        g.add_task(_node("C", depends=["B"]))

        g.mark_failed("A", "boom")
        assert g._nodes["B"].state == TaskState.SKIPPED
        assert g._nodes["C"].state == TaskState.SKIPPED
        assert g.is_complete()

    def test_failure_does_not_skip_independent(self):
        g = TaskGraph()
        g.add_task(_node("A"))
        g.add_task(_node("B"))
        g.add_task(_node("C", depends=["A"]))

        g.mark_failed("A", "boom")
        assert g._nodes["B"].state == TaskState.PENDING  # unrelated
        assert g._nodes["C"].state == TaskState.SKIPPED


# ------------------------------------------------------------------
# 3. Parallel tasks
# ------------------------------------------------------------------

class TestParallel:
    def test_independent_tasks_all_ready(self):
        g = TaskGraph()
        g.add_task(_node("C1"))
        g.add_task(_node("C2"))
        g.add_task(_node("C3"))

        ready = g.get_ready_tasks()
        assert len(ready) == 3


# ------------------------------------------------------------------
# 4. Soft dependencies — no cascade
# ------------------------------------------------------------------

class TestSoftDeps:
    def test_soft_dep_failure_does_not_cascade(self):
        g = TaskGraph()
        g.add_task(_node("C1"))
        g.add_task(_node("C2"))
        g.add_task(_node("A1", soft_depends=["C1", "C2"], min_soft=0))

        g.mark_done("C1", AgentResult("C1", AgentStatus.SUCCESS))
        g.mark_failed("C2", "timeout")

        ready = g.get_ready_tasks()
        assert [n.task_id for n in ready] == ["A1"]

    def test_get_failed_soft_deps(self):
        g = TaskGraph()
        g.add_task(_node("C1"))
        g.add_task(_node("C2"))
        g.add_task(_node("A1", soft_depends=["C1", "C2"]))

        g.mark_done("C1", AgentResult("C1", AgentStatus.SUCCESS))
        g.mark_failed("C2", "err")

        assert g.get_failed_soft_deps("A1") == ["C2"]


# ------------------------------------------------------------------
# 5. min_soft_deps threshold
# ------------------------------------------------------------------

class TestMinSoftDeps:
    def test_skip_when_below_threshold(self):
        g = TaskGraph()
        g.add_task(_node("C1"))
        g.add_task(_node("C2"))
        g.add_task(_node("C3"))
        g.add_task(_node("A1", soft_depends=["C1", "C2", "C3"], min_soft=2))

        g.mark_failed("C1", "err")
        g.mark_failed("C2", "err")
        g.mark_done("C3", AgentResult("C3", AgentStatus.SUCCESS))

        ready = g.get_ready_tasks()
        # Only 1 soft dep done, need 2 → SKIP
        assert g._nodes["A1"].state == TaskState.SKIPPED
        assert ready == []

    def test_proceed_when_meeting_threshold(self):
        g = TaskGraph()
        g.add_task(_node("C1"))
        g.add_task(_node("C2"))
        g.add_task(_node("C3"))
        g.add_task(_node("A1", soft_depends=["C1", "C2", "C3"], min_soft=2))

        g.mark_done("C1", AgentResult("C1", AgentStatus.SUCCESS))
        g.mark_done("C2", AgentResult("C2", AgentStatus.SUCCESS))
        g.mark_failed("C3", "err")

        ready = g.get_ready_tasks()
        assert [n.task_id for n in ready] == ["A1"]


# ------------------------------------------------------------------
# 6. Typical DAG: collectors → analyzers → report
# ------------------------------------------------------------------

class TestTypicalDAG:
    def test_full_pipeline_dag(self):
        g = TaskGraph()
        g.add_task(_node("col_0"))
        g.add_task(_node("col_1"))
        g.add_task(_node("ana_0", soft_depends=["col_0", "col_1"], min_soft=1))
        g.add_task(_node("report", soft_depends=["ana_0"], min_soft=1))

        # Both collectors ready
        ready = g.get_ready_tasks()
        assert len(ready) == 2

        g.mark_done("col_0", AgentResult("col_0", AgentStatus.SUCCESS))
        g.mark_failed("col_1", "net error")

        ready = g.get_ready_tasks()
        assert [n.task_id for n in ready] == ["ana_0"]

        g.mark_done("ana_0", AgentResult("ana_0", AgentStatus.SUCCESS))
        ready = g.get_ready_tasks()
        assert [n.task_id for n in ready] == ["report"]

        g.mark_done("report", AgentResult("report", AgentStatus.SUCCESS))
        assert g.is_complete()


# ------------------------------------------------------------------
# 7. Serialisation round-trip
# ------------------------------------------------------------------

class TestSerialisation:
    def test_to_dict_and_restore(self):
        g = TaskGraph()
        g.add_task(_node("A"))
        g.add_task(_node("B", depends=["A"]))

        g.mark_done("A", AgentResult("A", AgentStatus.SUCCESS))

        data = g.to_dict()
        assert data["A"]["state"] == "done"
        assert data["B"]["state"] == "pending"

        # Create a new graph with same structure and restore
        g2 = TaskGraph()
        g2.add_task(_node("A"))
        g2.add_task(_node("B", depends=["A"]))
        g2.restore_from_dict(data)

        assert g2._nodes["A"].state == TaskState.DONE
        assert g2._nodes["B"].state == TaskState.PENDING

    def test_summary(self):
        g = TaskGraph()
        g.add_task(_node("X"))
        g.mark_failed("X", "err")
        assert g.summary() == {"X": "failed"}

    def test_restore_failed_with_error(self):
        g = TaskGraph()
        g.add_task(_node("A"))
        g.mark_failed("A", "some error")

        data = g.to_dict()

        g2 = TaskGraph()
        g2.add_task(_node("A"))
        g2.restore_from_dict(data)

        assert g2._nodes["A"].state == TaskState.FAILED
        assert g2._nodes["A"].result.error == "some error"


# ------------------------------------------------------------------
# 8. Container protocol
# ------------------------------------------------------------------

class TestContainerProtocol:
    def test_len_and_contains(self):
        g = TaskGraph()
        g.add_task(_node("A"))
        g.add_task(_node("B"))
        assert len(g) == 2
        assert "A" in g
        assert "Z" not in g

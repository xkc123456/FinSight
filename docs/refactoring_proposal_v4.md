# FinSight v2.0 Refactoring Plan (v4)

## Context

FinSight is a multi-agent research report generation system. The current architecture has validated pain points:

1. **Rigid priority-tier orchestration** — all analyzers wait for ALL collectors (`run_report.py:196-257`)
2. **Monolithic Memory class** — ~515 lines mixing 6 responsibilities: data store, log store, agent factory, task planner, data selector, embedding cache (`src/memory/variable_memory.py`)
3. **Duplicated orchestration** — `run_report.py` and `demo/backend/app.py` share ~200 lines of near-identical priority-group logic
4. **Ad-hoc phase management** — string-based `self.current_phase` in DataAnalyzer (4 phases), 3 separate state vars in ReportGenerator (`_phase`, `_section_index_done`, `_post_stage`)
5. **Massive prompt duplication** — `select_data` has 2 exact copies + 1 near-copy, `data_api` has 3 exact copies in report_generator alone, `financial_company_prompts.yaml` and `financial_industry_prompts.yaml` share 8/11 identical keys
6. **Inconsistent checkpoints** — DataAnalyzer's dual `latest.pkl` + `charts.pkl`, ReportGenerator's never-read `report_obj_stageN` deepcopies, non-functional `Semaphore(1)` (created fresh each iteration, never shared)
7. **Bug: `report_draft` == `report_draft_wo_chart`** in `data_analyzer/prompts/financial_prompts.yaml` (lines 207-256 are byte-identical, confirmed `# TODO: fix this` comment)

Prior proposals (v2, v3) correctly identified these problems. v2 over-engineered (~15 abstractions, YAML DSL). v3 stripped to essentials but still introduced unnecessary abstractions (standalone EventEmitter, standalone PhaseRunner class, separate TaskPlanner + DataSelector files) and left gaps (no DAG failure handling, no debuggability story, unnecessary Memory adapter migration phase).

**This plan (v4)** further simplifies v3: **6 core files instead of 9, default DAG in plugin base, explicit failure handling, debuggability-first design, 6-week migration with no throwaway adapter layer.**

---

## Design Principles

1. **No abstraction without two callers** — if only one place uses it, inline it
2. **Data flows through TaskContext, status flows through AgentResult** — clean separation
3. **Debuggability is a feature** — every DAG transition logged, pipeline state JSON-inspectable, single-agent test mode
4. **Plugins declare differences, not boilerplate** — base class provides the common DAG; plugins only override what's unique

---

## What Changes

### 1. Core Abstractions (`src/core/` — 6 files)

Reduced from v3's 9 files. PhaseRunner inlined into BaseAgent, EventEmitter replaced by callback, TaskPlanner + DataSelector merged into pure functions.

#### `agent_result.py` — Lightweight status envelope

```python
class AgentStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"

@dataclass
class AgentResult:
    agent_id: str
    status: AgentStatus
    error: Optional[str] = None
```

No `artifacts` dict — data written to TaskContext by agents directly; AgentResult only tracks success/failure. Pipeline doesn't need to know what type of data an agent produced.

#### `task_context.py` — Generic artifact store

```python
class TaskContext:
    """Shared data bus for all agents in a pipeline run."""
    config: Config
    target_name: str
    stock_code: str
    target_type: str
    language: str

    _artifacts: dict[str, list[Any]]   # key -> list of values
    _lock: threading.Lock

    def put(self, key: str, value: Any):
        with self._lock:
            self._artifacts.setdefault(key, []).append(value)

    def get(self, key: str) -> list[Any]:
        return list(self._artifacts.get(key, []))

    # Convenience properties for type hints — no hardcoded agent coupling
    @property
    def collected_data(self) -> list[ToolResult]:
        return self.get("collected_data")

    @property
    def analysis_results(self) -> list[AnalysisResult]:
        return self.get("analysis_results")

    @property
    def report(self) -> Optional[Report]:
        items = self.get("report")
        return items[0] if items else None

    def to_dict(self) -> dict:
        """JSON-serializable snapshot for checkpoint and debugging."""
        ...

    @classmethod
    def from_dict(cls, data: dict, config: Config) -> 'TaskContext':
        ...
```

**Why generic?** If we later add a FactChecker or DataValidator agent, we just `ctx.put("validation_results", ...)` — no TaskContext modifications needed. The convenience properties give IDE autocomplete without coupling.

#### `task_graph.py` — DAG with failure propagation

```python
class TaskState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

@dataclass
class TaskNode:
    task_id: str
    agent_class: type
    agent_kwargs: dict
    run_kwargs: dict
    depends_on: list[str]           # hard deps: any failure → SKIP this task
    soft_depends_on: list[str] = field(default_factory=list)  # soft deps: failure won't block, only marks data as missing
    state: TaskState = TaskState.PENDING
    result: Optional[AgentResult] = None

class TaskGraph:
    def __init__(self):
        self._nodes: dict[str, TaskNode] = {}

    def add_task(self, node: TaskNode) -> 'TaskGraph':
        self._nodes[node.task_id] = node
        return self  # chainable

    def get_ready_tasks(self) -> list[TaskNode]:
        """Return tasks whose hard deps are all DONE and soft deps are all terminal."""
        ready = []
        for n in self._nodes.values():
            if n.state != TaskState.PENDING:
                continue
            # Hard deps: must all be DONE
            if not all(self._nodes[d].state == TaskState.DONE for d in n.depends_on):
                continue
            # Soft deps: must all be terminal (DONE / FAILED / SKIPPED), but allow failures
            terminal = {TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED}
            if not all(self._nodes[d].state in terminal for d in n.soft_depends_on):
                continue
            ready.append(n)
        return ready

    def get_failed_soft_deps(self, task_id: str) -> list[str]:
        """Return list of failed soft dependencies, so agents can be aware of missing data."""
        node = self._nodes[task_id]
        return [
            d for d in node.soft_depends_on
            if self._nodes[d].state in (TaskState.FAILED, TaskState.SKIPPED)
        ]

    def mark_done(self, task_id: str, result: AgentResult):
        self._nodes[task_id].state = TaskState.DONE
        self._nodes[task_id].result = result

    def mark_failed(self, task_id: str, error: str):
        """Mark task as FAILED and recursively SKIP all downstream dependents."""
        self._nodes[task_id].state = TaskState.FAILED
        self._nodes[task_id].result = AgentResult(task_id, AgentStatus.FAILED, error)
        self._cascade_skip(task_id)

    def _cascade_skip(self, failed_id: str):
        """Only propagate SKIP along hard dependency edges. Soft dep failures don't cascade."""
        for node in self._nodes.values():
            if failed_id in node.depends_on and node.state == TaskState.PENDING:
                node.state = TaskState.SKIPPED
                self._cascade_skip(node.task_id)
            # Note: failed_id in node.soft_depends_on does NOT propagate SKIP

    def is_complete(self) -> bool:
        return all(n.state in (TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED)
                   for n in self._nodes.values())

    def summary(self) -> dict:
        """Human-readable DAG status for logging and debugging."""
        return {tid: n.state.value for tid, n in self._nodes.items()}
```

**Key differences from v3:**
- `mark_failed` cascades SKIP along **hard dependency** edges to all transitive dependents. No downstream task will sit in PENDING forever after an upstream failure.
- New **soft dependencies** (`soft_depends_on`): when a soft dependency fails, the cascade does NOT propagate SKIP. Downstream tasks still execute but can query `get_failed_soft_deps()` to know which upstream data is missing.

**Why soft dependencies?** In report generation, data collection tasks (collectors) have a high failure rate (API timeouts, data source unavailability, etc.). If 1 of 5 collectors fails and causes all analyzers to be skipped, no report is produced at all. By making analyzers' dependency on collectors a soft dependency (see `build_task_graph` default implementation), we allow analysis based on partial data — partial data is always better than no data. Only truly indispensable prerequisites (e.g., analyzer → report) use hard dependencies.

**Typical DAG dependency configuration:**
- `collector_*` → no dependencies
- `analyzer_*` → `depends_on=[]`, `soft_depends_on=[all collector_ids]` (tolerates partial collection failures)
- `report` → `depends_on=[all analyzer_ids]` or `soft_depends_on=[all analyzer_ids]`

#### `pipeline.py` — Single orchestrator for CLI and web

```python
class Pipeline:
    def __init__(self, config: Config, max_concurrent: int = 3,
                 on_event: Callable[[dict], Awaitable[None]] | None = None,
                 max_retries: int = 0):
        self.config = config
        self.max_concurrent = max_concurrent
        self.on_event = on_event       # simple callback, no EventEmitter
        self.max_retries = max_retries
        self.checkpoint_mgr = CheckpointManager(config.working_dir)

    async def run(self, plugin: 'ReportPlugin', task_context: TaskContext,
                  resume: bool = True):
        # 1. Generate LLM tasks + merge with config tasks
        all_collect, all_analyze = await generate_tasks(
            task_context, self.config, plugin
        )
        # 2. Build DAG
        graph = plugin.build_task_graph(self.config, task_context,
                                        all_collect, all_analyze)
        # 3. Resume: restore graph state from checkpoint
        if resume:
            self.checkpoint_mgr.restore_pipeline(graph, task_context)
        # 4. Execute DAG
        await self._execute_graph(graph, task_context)
        # 5. Save final state
        self.checkpoint_mgr.save_pipeline(graph, task_context)

    async def _execute_graph(self, graph: TaskGraph, ctx: TaskContext):
        sem = asyncio.Semaphore(self.max_concurrent)
        running: dict[str, asyncio.Task] = {}

        while not graph.is_complete():
            # Launch ready tasks up to concurrency limit
            for node in graph.get_ready_tasks():
                if node.task_id in running:
                    continue
                node.state = TaskState.RUNNING
                await self._emit("task_started", node.task_id)
                running[node.task_id] = asyncio.create_task(
                    self._run_node(node, ctx, sem, graph)
                )

            if not running:
                break  # deadlock guard

            # Wait for first completion
            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                # Find which node finished
                tid = next(k for k, v in running.items() if v is task)
                del running[tid]
                try:
                    result = task.result()
                    graph.mark_done(tid, result)
                    await self._emit("task_completed", tid)
                except Exception as e:
                    graph.mark_failed(tid, str(e))
                    await self._emit("task_failed", tid, error=str(e))

            # Log DAG snapshot after every transition
            self._log_graph_state(graph)
            self.checkpoint_mgr.save_pipeline(graph, ctx)

    async def _run_node(self, node: TaskNode, ctx: TaskContext,
                        sem: asyncio.Semaphore,
                        graph: TaskGraph) -> AgentResult:
        async with sem:
            # --- Agent factory: restore from checkpoint first, create new otherwise ---
            saved_state = self.checkpoint_mgr.load_agent(node.task_id, phase=None)
            if saved_state:
                agent = node.agent_class.from_checkpoint(
                    saved_state, config=self.config, task_context=ctx,
                    **node.agent_kwargs
                )
                logger.info(f"Restored agent {node.task_id} from checkpoint")
            else:
                agent = node.agent_class(
                    config=self.config, task_context=ctx, **node.agent_kwargs
                )

            # --- Inject soft dependency failure info so agent knows what data is missing ---
            failed_deps = graph.get_failed_soft_deps(node.task_id)
            if failed_deps:
                node.run_kwargs['missing_dependencies'] = failed_deps
                logger.warning(
                    f"{node.task_id}: soft dependencies failed: {failed_deps}, "
                    f"proceeding with partial data"
                )

            # --- Retry wrapper ---
            last_err = None
            for attempt in range(1 + self.max_retries):
                try:
                    await agent.async_run(**node.run_kwargs)
                    return AgentResult(node.task_id, AgentStatus.SUCCESS)
                except Exception as e:
                    last_err = e
                    if attempt < self.max_retries:
                        logger.warning(f"Retry {attempt+1} for {node.task_id}: {e}")
            raise last_err

    async def _emit(self, event_type: str, task_id: str, **kwargs):
        if self.on_event:
            try:
                await self.on_event({"type": event_type, "task_id": task_id, **kwargs})
            except Exception as e:
                # Callback exceptions (e.g., WebSocket disconnect) must not crash the pipeline
                logger.error(f"Event callback failed for {event_type}/{task_id}: {e}")

    def _log_graph_state(self, graph: TaskGraph):
        logger.info(f"DAG state: {graph.summary()}")
```

**Why callback instead of EventEmitter?** Only one consumer exists (WebSocket broadcast in `app.py`). A callback is simpler, has no subscribe/unsubscribe lifecycle, and is trivially testable. Note that `_emit` internally catches callback exceptions, ensuring external failures (e.g., WebSocket disconnect) don't crash the entire pipeline.

**Agent factory migration note:** The current `Memory.get_or_create_agent()` and `Memory.from_checkpoint()` maintain agent identity consistency (same agent_id is never created twice). After refactoring, this responsibility moves to `Pipeline._run_node()`: each `TaskNode.task_id` uniquely identifies an agent instance. `_run_node` first attempts to restore the agent from `CheckpointManager` (calling `agent_class.from_checkpoint()`), and only creates a new instance when no checkpoint exists. This requires all Agent subclasses to implement a `from_checkpoint(cls, saved_state, **kwargs)` classmethod. The existing `BaseAgent` already has similar restoration logic (`_restore_tools_from_checkpoint`, etc.) — during migration, standardize it into the `from_checkpoint` interface.

#### `checkpoint.py` — Unified checkpoint authority

```python
CHECKPOINT_VERSION = 2          # increment on every incompatible change

class CheckpointManager:
    def __init__(self, working_dir: str):
        self.checkpoint_dir = os.path.join(working_dir, 'checkpoints')

    def save_pipeline(self, graph: TaskGraph, ctx: TaskContext):
        """Save pipeline-level state as JSON (inspectable)."""
        data = {
            "version": CHECKPOINT_VERSION,
            "saved_at": datetime.utcnow().isoformat(),
            "graph": graph.to_dict(),
            "task_context": ctx.to_dict(),
        }
        path = os.path.join(self.checkpoint_dir, 'pipeline.json')
        # Atomic write: write to temp file then rename, preventing corruption on crash
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def restore_pipeline(self, graph: TaskGraph, ctx: TaskContext) -> bool:
        """Restore pipeline state from checkpoint. Returns False on version mismatch."""
        path = os.path.join(self.checkpoint_dir, 'pipeline.json')
        if not os.path.exists(path):
            return False
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        saved_version = data.get("version", 1)
        if saved_version != CHECKPOINT_VERSION:
            logger.warning(
                f"Checkpoint version mismatch: saved={saved_version}, "
                f"current={CHECKPOINT_VERSION}. Starting fresh run."
            )
            return False
        graph.restore_from_dict(data["graph"])
        ctx.restore_from_dict(data["task_context"])
        logger.info(f"Restored pipeline from checkpoint (version={saved_version})")
        return True

    def save_agent(self, agent_id: str, phase: str, data: dict):
        """Save agent-level state (dill for code executor state)."""
        ...

    def load_agent(self, agent_id: str, phase: str) -> Optional[dict]:
        ...

    def try_load_legacy_checkpoint(self, legacy_dir: str) -> Optional[dict]:
        """Attempt to load v1-era dill-format checkpoints for one-time migration.

        Keep this method during the migration transition period (2 weeks after
        Phase 1 completion). If a legacy checkpoint is detected, extract
        collected_data and analysis_results, convert to new JSON format
        TaskContext, save as JSON, then archive the old file.
        Remove this method after the transition period.
        """
        ...
```

**Checkpoint format strategy:**
- Pipeline state (graph + TaskContext): **JSON** — human-readable, inspectable via any text editor, version-stable
- Agent internal state (conversation history, code executor): **dill** — necessary for lambda/closure serialization
- **Version compatibility**: `CHECKPOINT_VERSION` constant increments on incompatible changes. `restore_pipeline` gracefully degrades to a fresh run on version mismatch, with a warning log, preventing silent deserialization errors from old formats.
- **Legacy migration**: `try_load_legacy_checkpoint()` provides a one-time migration path, extracting valid data (collected_data, analysis_results) from old dill checkpoints and converting them to the new JSON format. Remove after the 2-week transition period.

This layering means when something fails, you can `cat checkpoints/pipeline.json` to see exactly which tasks completed and what data was collected, without needing Python.

#### `llm_helpers.py` — Pure functions extracted from Memory

```python
async def generate_collect_tasks(ctx, config, prompt_loader, query, existing_tasks, max_num=5) -> list[str]:
    """LLM-based collect task generation. Formerly Memory.generate_collect_tasks()."""
    ...

async def generate_analyze_tasks(ctx, config, prompt_loader, query, existing_tasks, max_num=5) -> list[str]:
    """LLM-based analysis task generation. Formerly Memory.generate_analyze_tasks()."""
    ...

async def select_data_by_llm(ctx, config, prompt_loader, query, max_k=-1) -> tuple[list, str]:
    """LLM-based data selection. Formerly Memory.select_data_by_llm()."""
    ...

async def select_analysis_by_llm(ctx, config, prompt_loader, query, max_k=-1) -> tuple[list, str]:
    """LLM-based analysis selection. Formerly Memory.select_analysis_result_by_llm()."""
    ...

async def retrieve_relevant_data(ctx, config, query, top_k=10) -> list:
    """Embedding-based retrieval. Formerly Memory.retrieve_relevant_data()."""
    ...
```

Pure functions, not a class. Inputs and outputs explicit. Easy to test in isolation with a mock TaskContext.

### 2. Phase Management — Inline into BaseAgent

**No standalone PhaseRunner class.** The logic is ~10 lines and only used by BaseAgent subclasses. Adding it as `BaseAgent._run_phases()`:

```python
# In BaseAgent
async def _run_phases(
    self,
    phases: list[tuple[str, Callable]],   # [(name, async_fn), ...]
    start_from: str | None = None,
    **state
) -> dict:
    """Run named phases sequentially with per-phase checkpointing."""
    started = start_from is None
    for name, execute_fn in phases:
        if not started:
            if name == start_from:
                started = True
            else:
                continue
        self.logger.info(f"[{self.AGENT_NAME}] Phase: {name}")
        result = await execute_fn(**state)
        if result:
            state.update(result)
        self.checkpoint_mgr.save_agent(self.id, name, state)
    return state
```

DataAnalyzer usage:
```python
async def async_run(self, ...):
    return await self._run_phases([
        ("analyze", self._phase_analyze),
        ("parse",   self._phase_parse),
        ("charts",  self._phase_charts),
        ("finalize", self._phase_finalize),
    ], start_from=self._resume_phase, ...)
```

ReportGenerator usage:
```python
async def async_run(self, ...):
    return await self._run_phases([
        ("outline",       self._phase_outline),
        ("sections",      self._phase_sections),
        ("post_images",   self._phase_post_images),
        ("post_abstract", self._phase_post_abstract),
        ("post_cover",    self._phase_post_cover),
        ("post_refs",     self._phase_post_refs),
        ("render",        self._phase_render),
    ], start_from=self._resume_phase, ...)
```

**Benefits:**
- No new file, no new class, no new dataclass
- Eliminates `self.current_phase = 'phase2'` chains in DataAnalyzer
- Eliminates `_phase` + `_section_index_done` + `_post_stage` trio in ReportGenerator
- Checkpoint per phase is automatic
- Resume by name (`start_from="charts"`) instead of string comparison chains

### 3. Plugin System (`src/plugins/`)

```
src/plugins/
    base_plugin.py              # ReportPlugin ABC with default DAG
    financial_company/
        plugin.py               # Minimal override
        prompts/                # Type-specific prompt overrides only
        templates/
    financial_industry/
    financial_macro/
    general/
    governance/
```

#### `base_plugin.py` — Default DAG provided

```python
class ReportPlugin(ABC):
    name: str

    def get_prompt_dir(self) -> Path: ...
    def get_template_path(self, name: str) -> Path: ...

    def get_post_process_flags(self) -> dict:
        """Override to customize post-processing behavior."""
        return {
            'add_introduction': True,
            'add_cover_page': False,
            'add_references': True,
            'enable_chart': True,
        }

    def build_task_graph(self, config: Config, ctx: TaskContext,
                         collect_tasks: list[str],
                         analyze_tasks: list[str]) -> TaskGraph:
        """Default DAG: collectors parallel -> analyzers parallel -> report.

        Most plugins don't need to override this. Only override if your
        report type has a genuinely different DAG topology (e.g., two
        rounds of analysis, or analyzers that only depend on specific
        collectors).
        """
        graph = TaskGraph()

        collector_ids = []
        for i, task in enumerate(collect_tasks):
            tid = f"collect_{i}"
            graph.add_task(TaskNode(
                task_id=tid,
                agent_class=DataCollector,
                agent_kwargs={...},
                run_kwargs={'input_data': {'task': ..., ...}},
                depends_on=[],
            ))
            collector_ids.append(tid)

        analyzer_ids = []
        for i, task in enumerate(analyze_tasks):
            tid = f"analyze_{i}"
            graph.add_task(TaskNode(
                task_id=tid,
                agent_class=DataAnalyzer,
                agent_kwargs={...},
                run_kwargs={'input_data': {'task': ..., 'analysis_task': task}},
                depends_on=[],                        # no hard deps
                soft_depends_on=collector_ids,         # tolerate partial collection failures
            ))
            analyzer_ids.append(tid)

        graph.add_task(TaskNode(
            task_id="report",
            agent_class=ReportGenerator,
            agent_kwargs={...},
            run_kwargs={'input_data': {...}},
            depends_on=analyzer_ids,                   # all analyzers must be terminal
        ))
        return graph
```

A typical plugin is now minimal — just declare what's different:

```python
class GovernancePlugin(ReportPlugin):
    name = "governance"

    def get_post_process_flags(self):
        return {
            'add_introduction': True,
            'add_cover_page': False,
            'add_references': True,
            'enable_chart': True,
        }
```

Only plugins with genuinely different DAG topology (e.g., a macro report that needs two analysis rounds) override `build_task_graph()`.

### 4. Prompt Deduplication (`src/prompts/_base/`)

Based on actual file-level inspection, here is the real duplication map:

| Prompt Key | Duplication | Action |
|---|---|---|
| `data_api` (report_generator) | 3 exact copies across company/industry/general | Extract to `_base/` |
| `data_api_outline` | 3 exact copies | Extract to `_base/` |
| `table_beautify` | 4 exact copies across all financial types | Extract to `_base/` |
| `select_data` | 2 exact + 1 near-copy (memory) | Extract to `_base/`, parameterize `{analyst_role}` |
| `select_analysis` | 2 exact + 1 near-copy (memory) | Extract to `_base/`, parameterize `{analyst_role}` |
| `outline_critique` | 2 duplicate pairs | Extract to `_base/` |
| `outline_refinement` | 2 duplicate pairs | Extract to `_base/` |
| `section_writing` | company == industry | Deduplicate |
| `section_writing_wo_chart` | company == industry | Deduplicate |
| `final_polish` | company == industry | Deduplicate |
| `vlm_critique` | 2 near-copies (1 word + 1 variable) | Extract, parameterize `{domain}` |
| `report_draft_wo_chart` | **BUG**: identical to `report_draft` in financial | Fix immediately (general version shows correct pattern) |

**Strategy**: `_base/` holds shared prompts. Each plugin's `prompts/` directory holds only keys that genuinely differ. `PromptLoader` resolution order: plugin-specific -> `_base/` -> error.

**Not worth deduplicating** (legitimately different):
- `generate_task` — 3 versions with entirely different guidelines and examples
- `abstract` — 5 distinct versions per report type
- `outline_draft` — substantively different outlining approaches per type
- `data_analysis` / `data_analysis_wo_chart` — different analytical norms per domain

### 5. Agent Refactoring

**BaseAgent** (`src/agents/base_agent.py`):
- Constructor accepts `task_context: TaskContext` instead of `memory`
- Adds `_run_phases()` method (see Section 2)
- `_agent_tool_function` logs to a lightweight log list, not Memory
- Checkpoint delegates to `CheckpointManager`
- Core agentic loop (`async_run`, `_parse_llm_response`, `_execute_action`) **UNCHANGED**

**DataAnalyzer** (`src/agents/data_analyzer/data_analyzer.py`):
- 4 phases via `_run_phases`: `analyze` -> `parse` -> `charts` -> `finalize`
- Remove dual checkpoint (`charts.pkl` merged into phase-based checkpointing)
- Remove non-functional `Semaphore(1)` at line 257 (created fresh each loop, never shared)
- **Fix `report_draft_wo_chart` bug** (financial_prompts.yaml lines 232-256) — use general_prompts.yaml's version as reference

**ReportGenerator** (`src/agents/report_generator/report_generator.py`):
- 7 phases via `_run_phases`: `outline` -> `sections` -> `post_images` -> `post_abstract` -> `post_cover` -> `post_refs` -> `render`
- Eliminates `_phase` + `_section_index_done` + `_post_stage` — single `_resume_phase` string
- Remove never-read `report_obj_stageN` deepcopies (lines 546, 570, 584, 598)
- Uses `llm_helpers.select_data_by_llm()` instead of `self.memory.select_data_by_llm()`

**DataCollector** (`src/agents/data_collector/data_collector.py`):
- Swap `memory` -> `task_context`, use `task_context.put("collected_data", result)`

### 6. Orchestration Consolidation

**`run_report.py`** — from 265 lines to ~20:
```python
async def main():
    config = Config(config_file_path='my_config.yaml')
    ctx = TaskContext.from_config(config)
    plugin = load_plugin(config.config['target_type'])
    pipeline = Pipeline(config, max_concurrent=3)
    await pipeline.run(plugin, ctx, resume=True)

if __name__ == '__main__':
    asyncio.run(main())
```

**`demo/backend/app.py`** — drop ~220 lines of duplicated orchestration:
```python
async def run_report_generation(resume: bool = False):
    config = Config(config_dict=config_dict)
    ctx = TaskContext.from_config(config)
    plugin = load_plugin(config.config['target_type'])
    pipeline = Pipeline(
        config, max_concurrent=3,
        on_event=lambda event: manager.broadcast(event)  # WebSocket hook
    )
    await pipeline.run(plugin, ctx, resume=resume)
```

**`demo/backend/template/`** — deleted (duplicates of `src/template/` files, now in plugins).

### 7. Debuggability Features

This is the major gap in v3 that v4 explicitly addresses.

#### 7a. DAG State Logging

Every DAG transition is logged:
```
INFO DAG state: {collect_0: done, collect_1: running, collect_2: pending, analyze_0: pending, report: pending}
```

When a task fails:
```
ERROR task_failed: collect_1 - ConnectionError: API timeout
INFO  DAG state: {collect_0: done, collect_1: failed, collect_2: done, analyze_0: skipped, report: skipped}
```

You see immediately what failed and what got skipped — no need to grep through agent logs.

#### 7b. JSON-Inspectable Pipeline Checkpoint

Pipeline state is saved as JSON (not dill):
```json
{
  "graph": {
    "collect_0": {"state": "done", "agent_id": "agent_data_collector_a1b2c3"},
    "collect_1": {"state": "failed", "error": "API timeout"},
    "analyze_0": {"state": "skipped"}
  },
  "task_context": {
    "collected_data": ["StockBasicInfo: 贵州茅台", "BalanceSheet: 贵州茅台"],
    "analysis_results": []
  }
}
```

You can inspect this with any text editor or `jq`. No Python needed to understand pipeline state.

#### 7c. Single-Agent Test Mode

For debugging a specific agent in isolation:

```python
# test_single_analyzer.py
ctx = TaskContext.from_config(config)
# Load pre-collected data from a previous run
ctx.load_artifacts_from("outputs/moutai/checkpoints/pipeline.json")

analyzer = DataAnalyzer(config=config, task_context=ctx)
result = await analyzer.async_run(input_data={
    'task': 'Research target: 贵州茅台',
    'analysis_task': '分析营收结构'
})
```

No need to run the full pipeline just to test one agent. Load previous TaskContext, run the agent, inspect output.

#### 7d. Dry-Run Mode

```python
pipeline = Pipeline(config, dry_run=True)
await pipeline.run(plugin, ctx)
# Output: prints the DAG topology and task list without executing anything
```

Useful for validating plugin DAG construction and task generation before committing to a multi-hour pipeline run.

### 8. What Stays Unchanged

- **Tool system** — `src/tools/base.py`, `src/tools/__init__.py`, all tool implementations
- **LLM wrappers** — `src/utils/llm.py` (AsyncLLM)
- **BaseAgent core loop** — `async_run` conversation iteration, `_parse_llm_response`, `_execute_action` handlers
- **AsyncCodeExecutor** — `src/utils/code_executor_async.py`
- **Report/Section model** — `src/agents/report_generator/report_class.py`
- **IndexBuilder** — `src/utils/index_builder.py`
- **Rate limiter, Logger, Async bridge** — all utilities stay

---

## Migration Plan (6 Weeks, 2 Phases)

Compressed from v3's 12 weeks / 4 phases. The Memory adapter layer is skipped entirely — agents switch directly to TaskContext.

### Phase 1: Core + Agent Refactoring (Weeks 1-3)

**Goal:** New core package, agents refactored, Pipeline replaces both `run_report.py` and `app.py` orchestration.

**Week 1 — Foundation:**

Files created:
- `src/core/__init__.py`
- `src/core/agent_result.py` — AgentResult, AgentStatus
- `src/core/task_context.py` — TaskContext
- `src/core/task_graph.py` — TaskGraph, TaskNode, TaskState
- `src/core/checkpoint.py` — CheckpointManager
- `src/core/llm_helpers.py` — extracted from Memory

Files modified:
- `src/agents/base_agent.py` — add `_run_phases()`, accept `task_context`

Verification:
- Unit tests for TaskGraph (DAG resolution, failure cascading, SKIP propagation)
- Unit tests for TaskContext (thread safety, serialization round-trip)
- Unit tests for `llm_helpers` with mocked LLM

**Week 2 — Agent refactoring:**

Files modified:
- `src/agents/data_collector/data_collector.py` — `memory` -> `task_context`
- `src/agents/data_analyzer/data_analyzer.py` — `_run_phases`, remove dual checkpoint, remove `Semaphore(1)`, fix `report_draft_wo_chart` bug
- `src/agents/report_generator/report_generator.py` — `_run_phases`, remove `_phase`/`_section_index_done`/`_post_stage`, remove deepcopy waste
- `src/agents/data_analyzer/prompts/financial_prompts.yaml` — fix `report_draft_wo_chart` (copy pattern from `general_prompts.yaml`)

Verification:
- End-to-end `financial_company` report generation with new agent code
- Resume test: interrupt mid-analysis, restart, verify it continues correctly

**Week 3 — Pipeline + orchestration:**

Files created:
- `src/core/pipeline.py` — Pipeline

Files modified:
- `run_report.py` — reduce to ~20 lines
- `demo/backend/app.py` — drop duplicated orchestration, use Pipeline

Files deleted:
- `demo/backend/template/` — duplicates of `src/template/`

Verification:
- CLI: `python run_report.py` produces identical output to pre-refactoring
- Web: demo backend WebSocket logs work correctly via `on_event` callback
- Resume across CLI restart

### Phase 2: Plugins + Prompts + Polish (Weeks 4-6)

**Goal:** Report types become plugins, prompts deduplicated, config validation, tests.

**Week 4 — Plugin system:**

Files created:
- `src/plugins/base_plugin.py`
- `src/plugins/financial_company/plugin.py`
- `src/plugins/financial_industry/plugin.py`
- `src/plugins/financial_macro/plugin.py`
- `src/plugins/general/plugin.py`
- `src/plugins/governance/plugin.py`

Files moved:
- `src/template/*` -> `src/plugins/*/templates/`

Verification:
- Generate reports for all 5 types via plugins
- Create a minimal test plugin to verify extensibility

**Week 5 — Prompt deduplication:**

Files created:
- `src/prompts/_base/data_api.yaml`
- `src/prompts/_base/data_api_outline.yaml`
- `src/prompts/_base/select_data.yaml`
- `src/prompts/_base/select_analysis.yaml`
- `src/prompts/_base/table_beautify.yaml`
- `src/prompts/_base/vlm_critique.yaml`
- `src/prompts/_base/outline_critique.yaml`
- `src/prompts/_base/outline_refinement.yaml`

Files modified:
- `src/utils/prompt_loader.py` — base + overlay resolution
- Agent prompt YAML files — remove duplicated keys, keep only type-specific overrides

Files moved:
- `src/agents/*/prompts/*.yaml` -> `src/plugins/*/prompts/` (type-specific keys only)
- `src/memory/prompts/*.yaml` -> consolidated into `_base/` + minimal overrides

Verification:
- Byte-compare prompt output pre/post dedup for all 5 report types
- No prompt key resolves to empty/missing

**Week 6 — Polish:**

- Config validation (Pydantic) in `src/config/config.py`
- Dry-run mode in Pipeline
- Remove deprecated `src/memory/` module (or mark deprecated with warnings)
- Comprehensive integration tests across all 5 report types
- Remove dead code: old checkpoint logic, unused Memory methods

---

## Final Directory Structure

```
src/
    core/                           # NEW: 6 files
        __init__.py
        agent_result.py             # AgentResult, AgentStatus
        task_context.py             # TaskContext (generic artifact store)
        task_graph.py               # TaskGraph, TaskNode (with failure cascade)
        pipeline.py                 # Pipeline (orchestrator + DAG executor)
        checkpoint.py               # CheckpointManager (JSON pipeline + dill agent)
        llm_helpers.py              # Pure functions: task generation, data selection
    agents/                         # MODIFIED: use core abstractions
        base_agent.py               # + _run_phases(), task_context, CheckpointManager
        data_collector/
        data_analyzer/
        report_generator/
        search_agent/
    plugins/                        # NEW: report type plugins
        base_plugin.py              # ReportPlugin ABC with default DAG
        financial_company/
        financial_industry/
        financial_macro/
        general/
        governance/
    prompts/                        # NEW: shared base prompts
        _base/
            data_api.yaml
            data_api_outline.yaml
            select_data.yaml
            select_analysis.yaml
            table_beautify.yaml
            vlm_critique.yaml
            outline_critique.yaml
            outline_refinement.yaml
    tools/                          # UNCHANGED
    config/                         # MODIFIED: add Pydantic in Week 6
    utils/                          # UNCHANGED
    memory/                         # DEPRECATED after Phase 1, removed in Week 6
```

**File count comparison:**
| | v3 core/ | v4 core/ |
|--|----------|----------|
| agent_result.py | yes | yes |
| task_context.py | yes | yes (generic) |
| task_graph.py | yes | yes (+ failure cascade) |
| pipeline.py | yes | yes (+ retry, callback, dry-run) |
| checkpoint.py | yes | yes (+ JSON layer) |
| phase_runner.py | yes | **no** (inlined into BaseAgent) |
| task_planner.py | yes | **no** (merged into llm_helpers) |
| data_selector.py | yes | **no** (merged into llm_helpers) |
| events.py | yes | **no** (callback parameter) |
| llm_helpers.py | no | yes (merged planner + selector) |
| **Total** | **9** | **6** |

---

## Scope Explicitly Cut

- **YAML graph DSL** — Python graph definitions only. Debuggable, type-checkable.
- **SwarmDispatcher** — Pipeline dispatches directly. No extra layer.
- **Skill Injection System** — `custom_instructions` config field if needed.
- **Auto-Adapt from Examples** — Too fragile. Users copy an existing plugin folder.
- **Full Human-in-the-Loop** — Simple checkpoint pausing only. Interactive gates deferred.
- **`src/` -> `finsight/` rename** — Breaks all imports for cosmetic benefit.
- **LLM interaction model change** — XML tag parsing stays. Function calling is a separate future effort.
- **Parallel section generation** — Marked as TODO in current code, deferred to post-refactoring. The current sequential approach with per-section checkpointing is correct and resumable; parallelizing introduces shared-state complexity that should be tackled independently.

---

## Key Differences from v3

| Aspect | v3 | v4 |
|--------|-----|-----|
| core/ files | 9 | 6 |
| EventEmitter | Separate class + file | `on_event` callback parameter |
| PhaseRunner | Separate class + file | `BaseAgent._run_phases()` method |
| TaskPlanner + DataSelector | 2 separate files with classes | 1 file with pure functions |
| TaskContext | Hardcoded fields per agent type | Generic artifact store + convenience properties |
| AgentResult | `artifacts: dict[str, Any]` | Status only; data flows through TaskContext |
| Plugin DAG | Every plugin must implement `build_task_graph()` | Base class provides default; override only when needed |
| DAG failure | Not discussed | `mark_failed` cascades SKIP to downstream |
| Retry | Not discussed | Configurable `max_retries` in Pipeline |
| Pipeline checkpoint | Format not discussed | JSON for pipeline (inspectable), dill for agent internals |
| Debuggability | Not discussed | DAG state logging, JSON checkpoint, single-agent test, dry-run |
| Migration | 12 weeks, 4 phases, Memory adapter | 6 weeks, 2 phases, direct refactoring |
| `report_draft_wo_chart` bug | Fix deferred to Phase 3 | Fix in Week 2 |
| LLM task generation + Plugin | Not clearly specified | Explicit: Pipeline calls `generate_tasks()` before `build_task_graph()` |

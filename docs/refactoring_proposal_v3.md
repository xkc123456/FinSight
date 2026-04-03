# FinSight v2.0 Optimized Refactoring Plan

## Context

FinSight is a multi-agent research report generation system. The current architecture has validated pain points: rigid priority-tier orchestration (all analyzers wait for ALL collectors), a monolithic Memory class (~515 lines mixing 6 responsibilities), duplicated orchestration (~200 lines copied between `run_report.py` and `demo/backend/app.py`), ad-hoc phase management (string-based `current_phase` in DataAnalyzer, 3 separate state vars in ReportGenerator), massive prompt duplication (select_data has 4 identical copies), and an inconsistent checkpoint system (dual files, never-read deepcopies).

A prior proposal (docs/refactoring_proposal_v2.md) correctly identified these problems but over-engineered the solution (~15 new abstractions, YAML DSL, Skill Injection System, Auto-Adapter). This plan strips to the essentials: **DAG scheduler, Memory decomposition, plugin-based report types, prompt deduplication, unified checkpoints, and orchestration consolidation** — delivering the same architectural benefits with half the abstractions.

## What Changes

### 1. Core Abstractions (`src/core/` — NEW package, 9 files)

**`agent_result.py`** — Standardized output replacing ad-hoc dicts:
```python
@dataclass
class AgentResult:
    agent_id: str
    agent_name: str
    status: AgentStatus  # SUCCESS | FAILED | PARTIAL
    artifacts: dict[str, Any]  # typed by convention per agent
    error: Optional[str] = None
    working_dir: Optional[str] = None
```

**`task_context.py`** — Typed data bus replacing Memory's data role:
- `collected_data: list[ToolResult]` — written by DataCollectors
- `analysis_results: list[AnalysisResult]` — written by DataAnalyzers  
- `report: Optional[Report]` — written by ReportGenerator
- Thread-safe `add_collected_data()` / `add_analysis_result()` methods
- Serializable for checkpoint persistence
- NO agent lifecycle, NO logging, NO task generation (those go elsewhere)

**`task_graph.py`** — Python-defined DAG (no YAML DSL):
```python
@dataclass
class TaskNode:
    task_id: str
    agent_class: type
    agent_kwargs: dict
    run_kwargs: dict
    depends_on: list[str]  # task_ids
    state: TaskState  # PENDING | RUNNING | DONE | FAILED | SKIPPED

class TaskGraph:
    def add_task(self, node: TaskNode) -> TaskGraph  # chainable
    def get_ready_tasks(self) -> list[TaskNode]       # deps all DONE
    def is_complete(self) -> bool
    def mark_done(self, task_id, result) / mark_failed(self, task_id, error)
```

**`pipeline.py`** — Replaces BOTH `run_report.py` orchestration AND `app.py` orchestration:
```python
class Pipeline:
    def __init__(self, config, max_concurrent=3)
    async def run(self, resume=True)    # load plugin -> build graph -> execute DAG -> save
    async def _execute_graph(self, graph, task_context)  # asyncio.wait FIRST_COMPLETED loop
```
After this, `run_report.py` becomes ~15 lines. `app.py` drops ~220 lines of duplicated logic.

**`phase_runner.py`** — Replaces ad-hoc string-based phase state machines:
```python
@dataclass
class Phase:
    name: str
    execute: Callable[..., Awaitable[Any]]

class PhaseRunner:
    def __init__(self, phases, checkpoint_mgr, agent_id)
    async def run(self, start_from=None, **shared_state) -> dict
```
DataAnalyzer's 4 phases and ReportGenerator's 7 phases (outline + sections + 5 post-process stages) become named Phase objects. Eliminates `self.current_phase = 'phase2'` chains and the 3 separate state variables (`_phase`, `_section_index_done`, `_post_stage`).

**`checkpoint.py`** — Unified checkpoint authority:
- `save_pipeline(graph, task_context)` / `restore_pipeline(graph, task_context)`
- `save_agent(agent_id, phase, data)` / `load_agent(agent_id, phase)`
- Eliminates: DataAnalyzer's dual `latest.pkl` + `charts.pkl`, ReportGenerator's never-read `report_obj_stageN` deepcopies, inconsistent naming (`outline_latest.pkl`, `section_0.pkl`, etc.)

**`task_planner.py`** — Extracted from Memory's `generate_collect_tasks()` / `generate_analyze_tasks()`:
- Same LLM-based task generation logic, just in the right place
- Called by Pipeline or plugin, not by a data store

**`data_selector.py`** — Extracted from Memory's `select_data_by_llm()`, `select_analysis_result_by_llm()`, `retrieve_relevant_data()`:
- Takes TaskContext + LLM reference as inputs
- Used by ReportGenerator for section writing and post-processing

**`events.py`** — Simple EventEmitter for progress reporting:
- Fires typed events: `task_started`, `task_completed`, `phase_started`, etc.
- `app.py` WebSocket handler consumes these (replaces current ad-hoc broadcasting)

### 2. Plugin System (`src/plugins/` — NEW package)

Each report type = a Python package with a plugin class + prompts + templates:

```
src/plugins/
    base_plugin.py              # ReportPlugin ABC
    financial_company/
        plugin.py               # FinancialCompanyPlugin
        prompts/                # Type-specific prompt overrides
        templates/              # Outline templates + docx reference
    financial_industry/
    financial_macro/
    general/
    governance/
```

**`base_plugin.py`**:
```python
class ReportPlugin(ABC):
    name: str
    @abstractmethod
    def build_task_graph(self, config, ctx) -> TaskGraph  # construct DAG
    def get_prompt_loader(self, agent_name) -> PromptLoader
    def get_template_path(self, name) -> Path
    def get_post_process_flags(self) -> dict  # add_cover_page, enable_chart, etc.
```

Each concrete plugin's `build_task_graph()` constructs the DAG in Python — e.g., FinancialCompanyPlugin creates collector nodes, analyzer nodes depending on collectors, and a report node depending on all analyzers. Fine-grained dependencies (analyzer A depends on collectors 1 and 3 only) are expressed naturally.

**Adding a new report type**: create a new plugin folder, implement `ReportPlugin`, add prompts/templates. Set `target_type` in config. No other code changes needed.

### 3. Prompt Deduplication (`src/prompts/_base/` — NEW)

**Current state**: `select_data` has 4 copies, `data_api` has 4 copies, `vlm_critique` has 2 copies, `generate_task` has 4 copies (same template, different examples), `report_draft` == `report_draft_wo_chart` in financial analyzer (acknowledged by `# TODO: fix this` comment).

**Strategy**: Shared base + type-specific overlays.

```
src/prompts/
    _base/                      # Shared prompts (single copy)
        select_data.yaml        # was 4 copies
        select_analysis.yaml    # was 4 copies
        data_api.yaml           # was 4 copies
        vlm_critique.yaml       # was 2 copies
```

`PromptLoader` enhanced: loads `_base.yaml` for agent, overlays type-specific YAML, then falls back to `src/prompts/_base/` for shared keys. This reduces total prompt YAML files from ~14 to ~8 unique files.

### 4. Agent Refactoring (modify existing files)

**BaseAgent** (`src/agents/base_agent.py`):
- Constructor accepts `task_context: TaskContext` instead of `memory`
- `_agent_tool_function` logs to a lightweight log collector, not Memory
- Checkpoint save/load delegates to `CheckpointManager`
- Core agentic loop (`async_run` conversation iteration, XML parse, action dispatch) UNCHANGED

**DataAnalyzer** (`src/agents/data_analyzer/data_analyzer.py`):
- 4 phases expressed via PhaseRunner: `analyze` -> `parse` -> `charts` -> `finalize`
- Remove dual checkpoint (`charts.pkl` becomes the `charts` phase checkpoint)
- Remove the non-functional `Semaphore(1)` (created fresh each loop iteration, never shared)
- Fix `report_draft` == `report_draft_wo_chart` duplication

**ReportGenerator** (`src/agents/report_generator/report_generator.py`):
- 7 phases via PhaseRunner: `outline` -> `sections` -> `post_images` -> `post_abstract` -> `post_cover` -> `post_references` -> `render`
- Remove `_phase`, `_section_index_done`, `_post_stage` — single phase tracking via PhaseRunner
- Remove never-read `report_obj_stageN` deepcopies
- Uses `DataSelector` (extracted from Memory) for section writing prompts

**DataCollector** (`src/agents/data_collector/data_collector.py`):
- Swap `memory` -> `task_context`, use `task_context.add_collected_data()`

### 5. Orchestration Consolidation

- `run_report.py`: reduced from 265 lines to ~15 (instantiate Config + Pipeline, call `pipeline.run()`)
- `demo/backend/app.py`: drop ~220 lines of duplicated orchestration, use Pipeline with WebSocket event callback
- `demo/backend/template/`: deleted (duplicates of `src/template/` files, now in plugins)

### 6. What Stays Unchanged

- **Tool system** — `src/tools/base.py`, `src/tools/__init__.py`, all tool implementations
- **LLM wrappers** — `src/utils/llm.py` (AsyncLLM)
- **BaseAgent core loop** — the `async_run` conversation iteration, `_parse_llm_response`, `_execute_action` handlers
- **AsyncCodeExecutor** — `src/utils/code_executor_async.py`
- **Report/Section model** — `src/agents/report_generator/report_class.py`
- **IndexBuilder** — `src/utils/index_builder.py`
- **Rate limiter, Logger, Async bridge** — all utilities stay

## Migration Plan (12 Weeks, 4 Phases)

### Phase 1: Foundation (Weeks 1-3)

Create `src/core/` with all new abstractions. Memory becomes an adapter that delegates to TaskContext internally. All agents continue to work through Memory's interface.

**Key files created**: `src/core/__init__.py`, `agent_result.py`, `task_context.py`, `task_graph.py`, `pipeline.py`, `phase_runner.py`, `checkpoint.py`, `task_planner.py`, `data_selector.py`, `events.py`

**Key files modified**: `src/memory/variable_memory.py` (add delegation to TaskContext)

**Verification**: Run full pipeline via existing `run_report.py` — identical behavior. Unit tests for TaskGraph DAG resolution, TaskContext thread safety, PhaseRunner.

### Phase 2: Agent Refactoring + Pipeline (Weeks 4-7)

Agents switch from Memory to TaskContext directly. Pipeline replaces procedural orchestration.

**Key files modified**: 
- `src/agents/base_agent.py` — constructor, checkpoint, tool function
- `src/agents/data_analyzer/data_analyzer.py` — PhaseRunner, fix Semaphore bug, unified checkpoint
- `src/agents/report_generator/report_generator.py` — PhaseRunner, remove deepcopy waste
- `src/agents/data_collector/data_collector.py` — task_context swap
- `run_report.py` — reduced to ~15 lines
- `demo/backend/app.py` — drop duplicated orchestration, use Pipeline

**Verification**: End-to-end report generation for `financial_company`. Resume test (interrupt mid-analysis, restart). Demo backend integration test.

### Phase 3: Plugins + Prompt Dedup (Weeks 8-10)

Report types become plugin folders. Prompts deduplicated.

**Key files created**: `src/plugins/` entire package, `src/prompts/_base/` shared prompts

**Key files modified**: `src/utils/prompt_loader.py` — base + overlay loading

**Key files moved**: `src/template/*` -> `src/plugins/*/templates/`, `src/agents/*/prompts/*.yaml` -> `src/plugins/*/prompts/` + `src/prompts/_base/`

**Key files deleted**: `demo/backend/template/` (duplicates)

**Verification**: Generate reports for all 5 types. Create a test plugin to verify extensibility. Byte-compare prompt content pre/post dedup.

### Phase 4: Polish (Weeks 11-12)

Config validation (Pydantic), progress events, cleanup dead code from Memory, documentation, comprehensive tests.

**Key files modified**: `src/config/config.py` (add Pydantic), `src/memory/variable_memory.py` (deprecate or remove)

**Verification**: Full regression across all 5 report types. Checkpoint migration from v1 format.

## Final Directory Structure

```
src/
    core/                           # NEW: 9 files
        __init__.py
        agent_result.py             # AgentResult, AgentStatus
        task_context.py             # TaskContext (data bus)
        task_graph.py               # TaskGraph, TaskNode, TaskState
        pipeline.py                 # Pipeline (orchestrator)
        phase_runner.py             # PhaseRunner, Phase
        checkpoint.py               # CheckpointManager
        task_planner.py             # LLM task generation (from Memory)
        data_selector.py            # LLM data selection (from Memory)
        events.py                   # EventEmitter
    agents/                         # MODIFIED: use core abstractions
        base_agent.py               # task_context, CheckpointManager
        data_collector/
        data_analyzer/
        report_generator/
        search_agent/
    plugins/                        # NEW: report type plugins
        base_plugin.py
        financial_company/
        financial_industry/
        financial_macro/
        general/
        governance/
    prompts/                        # NEW: shared base prompts
        _base/
            select_data.yaml
            select_analysis.yaml
            data_api.yaml
            vlm_critique.yaml
    tools/                          # UNCHANGED
    config/                         # MODIFIED: add Pydantic in Phase 4
    utils/                          # UNCHANGED
    memory/                         # DEPRECATED after Phase 2
```

## Scope Explicitly Cut

- **YAML graph DSL** — Python graph definitions only. Simpler, debuggable, sufficient.
- **SwarmDispatcher** — Scheduler dispatches directly. No extra layer.
- **Skill Injection System** — Use `custom_instructions` config field if needed. 10 lines, not a module.
- **Auto-Adapt from Examples** — Too fragile (4 chained LLM calls). Users copy an existing plugin folder.
- **Full Human-in-the-Loop** — Simple checkpoint pausing only. Full interactive gates deferred.
- **`src/` -> `finsight/` rename** — Breaks all imports for cosmetic benefit. Not worth it.
- **LLM interaction model change** — XML tag parsing stays for now. Function calling is a separate future effort.

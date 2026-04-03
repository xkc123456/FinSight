# FinSight v2.0 Refactoring Plan (Optimized)

> Date: 2026-04-03 | Status: Ready for Review

---

## Table of Contents

1. [Background & Motivation](#1-background--motivation)
2. [Current Architecture Pain Points](#2-current-architecture-pain-points)
3. [Design Principles](#3-design-principles)
4. [Architecture Overview](#4-architecture-overview)
5. [Module 1: Core Abstractions](#5-module-1-core-abstractions)
6. [Module 2: Task Graph & Pipeline](#6-module-2-task-graph--pipeline)
7. [Module 3: Plugin-Based Report Types](#7-module-3-plugin-based-report-types)
8. [Module 4: Prompt Deduplication](#8-module-4-prompt-deduplication)
9. [Module 5: Agent Refactoring](#9-module-5-agent-refactoring)
10. [Module 6: Codebase Cleanup](#10-module-6-codebase-cleanup)
11. [Directory Structure](#11-directory-structure)
12. [Migration Plan (12 Weeks)](#12-migration-plan-12-weeks)
13. [Scope Explicitly Cut (and Why)](#13-scope-explicitly-cut-and-why)
14. [Risk Assessment](#14-risk-assessment)

---

## 1. Background & Motivation

FinSight is a custom-built multi-agent research report generation system. The current architecture has been validated in production but has accumulated significant technical debt that limits extensibility, parallelism, and code maintainability.

This plan strips the refactoring to the essentials: **DAG scheduler, Memory decomposition, plugin-based report types, prompt deduplication, unified checkpoints, and orchestration consolidation** — delivering maximum architectural benefit with minimum abstraction overhead.

---

## 2. Current Architecture Pain Points

### 2.1 Architecture Diagram (Current)

```
run_report.py / app.py  (procedural orchestration, ~200 lines DUPLICATED)
        |
        v
  +-----------+     +--------------+     +-----------------+
  | Priority 1 |---->| Priority 2   |---->| Priority 3      |
  | DataCollect|     | DataAnalyze  |     | ReportGenerate  |
  | (parallel) |     | (parallel)   |     | (sequential)    |
  +-----------+     +--------------+     +-----------------+
        |                  |                      |
        v                  v                      v
    Memory (monolithic ~515 lines, 6+ mixed responsibilities)
```

### 2.2 Validated Pain Points

| ID | Pain Point | Evidence |
|----|-----------|----------|
| P1 | **Rigid report types**: 5 hardcoded types, adding one requires code changes in multiple files | `target_type` conditional logic scattered across Memory, ReportGenerator, prompt loader |
| P2 | **Linear priority tiers**: Analyzer A waits for ALL collectors, even those it doesn't need | `run_report.py:196-257` groups by priority, `asyncio.gather` within tier |
| P3 | **Ad-hoc phase management**: DataAnalyzer uses string `self.current_phase = 'phase1'...'phase4'`, ReportGenerator uses 3 separate vars (`_phase`, `_section_index_done`, `_post_stage`) | `data_analyzer.py:62,478-529`, `report_generator.py:76-80` |
| P4 | **Monolithic Memory (~515 lines)**: Mixes agent lifecycle, data store, embeddings, logs, LLM task generation, LLM data selection | `variable_memory.py` — single class with 6+ responsibilities |
| P5 | **Duplicated orchestration**: `run_report.py` and `app.py` contain ~200 lines of near-identical pipeline code | `run_report.py:23-261` vs `app.py:591-842` |
| P6 | **Massive prompt duplication**: `select_data` has 4 identical copies, `data_api` has 4 copies, `report_draft` == `report_draft_wo_chart` (has `# TODO: fix this`) | Verified by reading all 14 prompt YAML files |
| P7 | **Inconsistent checkpoints**: DataAnalyzer dual files (`latest.pkl` + `charts.pkl`), ReportGenerator saves deepcopy per post-stage that is never read back | `data_analyzer.py:245-251`, `report_generator.py:546,571,583,597` |
| P8 | **Broken concurrency**: `threading.Semaphore(1)` in DataAnalyzer created fresh each loop iteration — provides zero synchronization | `data_analyzer.py:257` |
| P9 | **Duplicated templates**: `demo/backend/template/` copies files from `src/template/` | Identical `company_outline.md`, near-identical `company_outline_zh.md` |

---

## 3. Design Principles

| Principle | Guideline |
|-----------|-----------|
| **Simplicity** | Every new class must justify its existence. No abstractions for hypothetical future needs. |
| **Convention over configuration** | Plugins are Python classes, not YAML DSLs. Graphs defined in code, not config files. |
| **Preserve what works** | BaseAgent core loop, tool system, AsyncCodeExecutor, IndexBuilder — all stay. |
| **Incremental migration** | Each phase produces a working system. Memory becomes an adapter first, then gets replaced. |
| **12-week timeline** | Aggressive scope cuts to fit. Anything speculative goes to "Deferred" list. |

---

## 4. Architecture Overview

### 4.1 V2 Layered Architecture

```
                     +---------------------------------+
                     |        User Interface            |
                     |   CLI (run_report.py, ~15 lines) |
                     |   Web (app.py, uses Pipeline)    |
                     +-----------+---------------------+
                                 |
                     +-----------v---------------------+
                     |         Pipeline                 |
                     |  Plugin + TaskGraph + Scheduler   |
                     +-----------+---------------------+
                                 |
          +----------------------+----------------------+
          |                      |                      |
+---------v--------+  +----------v---------+  +---------v---------+
|   Agents         |  |   Report Plugin    |  |   Core Services    |
|  (BaseAgent v2)  |  | (build_task_graph) |  | CheckpointManager  |
|  PhaseRunner     |  | (prompts/templates)|  | TaskContext         |
+--------+---------+  +----------+---------+  | DataSelector       |
         |                       |            | TaskPlanner        |
+--------v-----------------------v------------+---------+----------+
|                     Existing Infrastructure                       |
|  Tools | AsyncLLM | AsyncCodeExecutor | IndexBuilder | Logger     |
+------------------------------------------------------------------+
```

### 4.2 Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Graph definition | Python classes | Simpler than YAML DSL. Only ~5 graph topologies needed. Debuggable. No parser/validator code. |
| Scheduling | Loop inside Pipeline | Not a separate Scheduler class. Not a SwarmDispatcher. 40 lines of asyncio code. |
| Plugin interface | ABC with `build_task_graph()` | Convention: plugin folder + Python class. User copies existing plugin to create new one. |
| Phase management | PhaseRunner | Lightweight: ~30 lines. Extracted from ad-hoc state machines. Named phases + auto-checkpoint. |
| Memory decomposition | TaskContext + TaskPlanner + DataSelector | 3 focused classes replacing 1 monolith. Memory becomes adapter first, then deprecated. |
| Prompt dedup | Shared base + overlay | `_base.yaml` per agent, type-specific overlay, shared utility prompts. Eliminates 4x copies. |

---

## 5. Module 1: Core Abstractions

### 5.1 AgentResult (`src/core/agent_result.py`)

Standardized output. Currently every agent returns a different dict shape.

```python
from dataclasses import dataclass, field
from typing import Any, Optional
from enum import Enum

class AgentStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"  # hit max_iterations with partial work

@dataclass
class AgentResult:
    agent_id: str
    agent_name: str
    status: AgentStatus
    artifacts: dict[str, Any] = field(default_factory=dict)
    # Convention:
    #   DataCollector:   {"collected_data": list[ToolResult]}
    #   DataAnalyzer:    {"analysis_result": AnalysisResult}
    #   ReportGenerator: {"report": Report}
    error: Optional[str] = None
    working_dir: Optional[str] = None
```

### 5.2 TaskContext (`src/core/task_context.py`)

Typed data bus. Replaces Memory's data-store responsibility ONLY. No agent lifecycle, no logging, no task generation.

```python
@dataclass
class TaskContext:
    config: Config
    target_name: str
    stock_code: str
    target_type: str      # "financial_company", "general", etc.
    language: str

    # Written by DataCollectors
    collected_data: list = field(default_factory=list)       # list[ToolResult]
    # Written by DataAnalyzers
    analysis_results: list = field(default_factory=list)     # list[AnalysisResult]
    # Written by ReportGenerator
    report: Optional[Any] = None

    # Embeddings cache
    data_embeddings: dict = field(default_factory=dict)
    # Thread-safe lock for concurrent writes
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_collected_data(self, data):
        with self._lock:
            self.collected_data.append(data)

    def add_analysis_result(self, result):
        with self._lock:
            self.analysis_results.append(result)

    def get_collected_data(self, exclude_types=None) -> list:
        if not exclude_types:
            return list(self.collected_data)
        return [d for d in self.collected_data
                if not any(isinstance(d, t) for t in exclude_types)]
```

Agents receive `task_context` in constructor (replacing `memory`). A DataCollector calls `task_context.add_collected_data(...)`. A DataAnalyzer reads `task_context.get_collected_data()`.

### 5.3 PhaseRunner (`src/core/phase_runner.py`)

Lightweight phase executor replacing ad-hoc state machines. ~30 lines of core logic.

```python
@dataclass
class Phase:
    name: str
    execute: Callable[..., Awaitable[Any]]

class PhaseRunner:
    def __init__(self, phases: list[Phase], checkpoint_mgr, agent_id: str):
        self.phases = phases
        self.checkpoint_mgr = checkpoint_mgr
        self.agent_id = agent_id

    async def run(self, start_from: str = None, **shared_state) -> dict:
        started = (start_from is None)
        for phase in self.phases:
            if not started:
                if phase.name == start_from:
                    started = True
                else:
                    continue
            result = await phase.execute(**shared_state)
            shared_state.update(result or {})
            self.checkpoint_mgr.save_agent(self.agent_id, phase.name, shared_state)
        return shared_state
```

**DataAnalyzer phases** (replaces `self.current_phase = 'phase1'...'phase4'`):
```python
def _build_phases(self) -> list[Phase]:
    return [
        Phase("analyze", self._phase_analyze),       # agentic loop via super().async_run()
        Phase("parse", self._phase_parse_report),     # extract title + content
        Phase("charts", self._phase_draw_charts),     # VLM critique loop
        Phase("finalize", self._phase_build_result),  # assemble AnalysisResult
    ]
```

**ReportGenerator phases** (replaces `_phase` + `_section_index_done` + `_post_stage`):
```python
def _build_phases(self) -> list[Phase]:
    return [
        Phase("outline", self._phase_generate_outline),
        Phase("sections", self._phase_write_sections),    # internally tracks section index
        Phase("post_images", self._phase_replace_images),
        Phase("post_abstract", self._phase_add_abstract),
        Phase("post_cover", self._phase_add_cover),
        Phase("post_references", self._phase_add_references),
        Phase("render", self._phase_render_docx),
    ]
```

### 5.4 CheckpointManager (`src/core/checkpoint.py`)

Single checkpoint authority replacing the inconsistent current system.

```python
class CheckpointManager:
    def __init__(self, working_dir: str):
        self.base_dir = Path(working_dir) / "checkpoints"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_pipeline(self, graph, task_context):
        """Persist entire pipeline state (graph + context)."""
        ...

    def restore_pipeline(self, graph, task_context) -> bool:
        """Restore pipeline state. Returns True if restored."""
        ...

    def save_agent(self, agent_id: str, phase: str, data: dict):
        """Per-agent, per-phase checkpoint."""
        ...

    def load_agent(self, agent_id: str, phase: str) -> dict | None:
        ...
```

**What this eliminates:**
- DataAnalyzer's dual `latest.pkl` + `charts.pkl` files
- ReportGenerator's never-read `report_obj_stageN` deepcopies
- Inconsistent naming (`outline_latest.pkl`, `section_0.pkl`, etc.)
- Key accumulation in a single dict (each phase now has its own checkpoint file)

### 5.5 TaskPlanner (`src/core/task_planner.py`)

Extracted from Memory's `generate_collect_tasks()` / `generate_analyze_tasks()`. Same LLM logic, right location.

```python
class TaskPlanner:
    def __init__(self, config, prompt_loader):
        self.config = config
        self.prompt_loader = prompt_loader

    async def generate_collect_tasks(self, query, llm_name, max_num=5, existing=None) -> list[str]:
        ...
    async def generate_analysis_tasks(self, query, llm_name, max_num=5, existing=None) -> list[str]:
        ...
```

### 5.6 DataSelector (`src/core/data_selector.py`)

Extracted from Memory's `select_data_by_llm()`, `select_analysis_result_by_llm()`, `retrieve_relevant_data()`.

```python
class DataSelector:
    def __init__(self, task_context, config):
        self.ctx = task_context
        self.config = config

    async def select_data_by_llm(self, query, max_k=-1, model_name=...) -> tuple[list, str]:
        ...
    async def select_analysis_by_llm(self, query, max_k=-1, model_name=...) -> tuple[list, str]:
        ...
    async def retrieve_relevant_data(self, query, top_k=10, embedding_model=...) -> list:
        ...
```

---

## 6. Module 2: Task Graph & Pipeline

### 6.1 TaskGraph (`src/core/task_graph.py`)

Python-defined DAG. No YAML DSL.

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
    agent_kwargs: dict = field(default_factory=dict)
    run_kwargs: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    state: TaskState = TaskState.PENDING
    result: Any = None

class TaskGraph:
    nodes: dict[str, TaskNode]

    def add_task(self, node: TaskNode) -> 'TaskGraph':   # chainable
        ...

    def get_ready_tasks(self) -> list[TaskNode]:
        """Return PENDING tasks whose dependencies are all DONE."""
        ...

    def is_complete(self) -> bool:
        """All nodes in terminal state (DONE/FAILED/SKIPPED)."""
        ...

    def mark_done(self, task_id: str, result: AgentResult): ...
    def mark_failed(self, task_id: str, error: str): ...
```

### 6.2 Pipeline (`src/core/pipeline.py`)

Replaces BOTH `run_report.py` orchestration AND `app.py` orchestration.

```python
class Pipeline:
    def __init__(self, config: Config, max_concurrent: int = 3):
        self.config = config
        self.max_concurrent = max_concurrent
        self.task_context = TaskContext(...)
        self.checkpoint_mgr = CheckpointManager(config.working_dir)
        self.on_event = None  # optional callback for progress reporting

    async def run(self, resume: bool = True):
        # 1. Load plugin
        plugin = load_report_plugin(self.task_context.target_type)
        # 2. Build graph
        graph = plugin.build_task_graph(self.config, self.task_context)
        # 3. Resume
        if resume:
            self.checkpoint_mgr.restore_pipeline(graph, self.task_context)
        # 4. Execute DAG
        await self._execute_graph(graph, self.task_context)
        # 5. Save
        self.checkpoint_mgr.save_pipeline(graph, self.task_context)
        return self.task_context.report

    async def _execute_graph(self, graph, task_context):
        """DAG execution with bounded concurrency."""
        sem = asyncio.Semaphore(self.max_concurrent) if self.max_concurrent else None
        running: dict[str, asyncio.Task] = {}

        while not graph.is_complete():
            # Launch ready tasks
            for node in graph.get_ready_tasks():
                node.state = TaskState.RUNNING
                coro = self._run_node(node, task_context, sem)
                running[node.task_id] = asyncio.create_task(coro)

            if not running:
                break  # deadlock or done

            # Wait for at least one to finish
            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                tid = next(k for k, v in running.items() if v is task)
                del running[tid]
                try:
                    result = task.result()
                    graph.mark_done(tid, result)
                    self._emit("task_completed", tid)
                except Exception as e:
                    graph.mark_failed(tid, str(e))
                    self._emit("task_failed", tid, error=str(e))
```

### 6.3 How Orchestration Simplifies

**`run_report.py` (before: 265 lines, after: ~15 lines):**
```python
async def main():
    config = Config(config_file_path='my_config.yaml')
    pipeline = Pipeline(config, max_concurrent=3)
    await pipeline.run(resume=True)

if __name__ == '__main__':
    asyncio.run(main())
```

**`demo/backend/app.py` (drops ~220 lines of duplicated logic):**
```python
@app.post("/api/execution/start")
async def start_execution(...):
    pipeline = Pipeline(config, max_concurrent=3)
    pipeline.on_event = lambda event: manager.broadcast(event)  # WebSocket hook
    task = asyncio.create_task(pipeline.run(resume=request.resume))
```

### 6.4 Fine-Grained Dependencies

The key benefit: with a DAG, you can express "Analyzer A depends only on Collectors 1 and 3":

```python
# In a plugin's build_task_graph():
graph.add_task(TaskNode("collect_financial", DataCollector, ...))
graph.add_task(TaskNode("collect_market", DataCollector, ...))
graph.add_task(TaskNode("collect_industry", DataCollector, ...))

# This analyzer needs financial + market data, NOT industry data
graph.add_task(TaskNode(
    "analyze_valuation", DataAnalyzer,
    depends_on=["collect_financial", "collect_market"],  # not "collect_industry"
    ...
))

# This analyzer needs only industry data
graph.add_task(TaskNode(
    "analyze_industry", DataAnalyzer,
    depends_on=["collect_industry"],
    ...
))
```

`analyze_industry` can start as soon as `collect_industry` finishes, without waiting for `collect_financial` or `collect_market`.

---

## 7. Module 3: Plugin-Based Report Types

### 7.1 Plugin Structure

Each report type = a Python package:

```
src/plugins/
    __init__.py                    # Plugin discovery (import-scan, like tools/__init__.py)
    base_plugin.py                 # ReportPlugin ABC
    financial_company/
        __init__.py
        plugin.py                  # FinancialCompanyPlugin class
        prompts/
            data_analyzer.yaml     # Type-specific prompt overrides
            report_generator.yaml
        templates/
            company_outline_zh.md  # Moved from src/template/
            company_outline.md
            report_template.docx
    financial_industry/
        __init__.py
        plugin.py
        prompts/
        templates/
    financial_macro/ ...
    general/ ...
    governance/ ...
```

### 7.2 Plugin Base Class

```python
# src/plugins/base_plugin.py
class ReportPlugin(ABC):
    name: str

    @abstractmethod
    def build_task_graph(self, config: Config, ctx: TaskContext) -> TaskGraph:
        """Construct the DAG of tasks for this report type."""
        ...

    def get_prompt_dir(self) -> Path:
        """Return path to this plugin's prompts directory."""
        return Path(__file__).parent / self.name / "prompts"

    def get_template_path(self, name: str) -> Path:
        return Path(__file__).parent / self.name / "templates" / name

    def get_post_process_flags(self) -> dict:
        """Control which post-processing steps run."""
        return {
            'add_introduction': True,
            'add_cover_page': False,
            'add_references': True,
            'enable_chart': True,
        }
```

### 7.3 Concrete Example: FinancialCompanyPlugin

```python
# src/plugins/financial_company/plugin.py
class FinancialCompanyPlugin(ReportPlugin):
    name = "financial_company"

    def build_task_graph(self, config, ctx) -> TaskGraph:
        graph = TaskGraph()
        llm_name = os.getenv("DS_MODEL_NAME")
        collect_tasks = config.config.get('custom_collect_tasks', [])
        analysis_tasks = config.config.get('custom_analysis_tasks', [])

        # Collectors — all parallel, no dependencies
        for i, task in enumerate(collect_tasks):
            graph.add_task(TaskNode(
                task_id=f"collect_{i}",
                agent_class=DataCollector,
                agent_kwargs={'use_llm_name': llm_name},
                run_kwargs={'input_data': {'task': f'Research: {ctx.target_name} ({ctx.stock_code}), {task}'}, 'max_iterations': 20},
            ))

        # Analyzers — depend on all collectors
        collector_ids = [f"collect_{i}" for i in range(len(collect_tasks))]
        for i, task in enumerate(analysis_tasks):
            graph.add_task(TaskNode(
                task_id=f"analyze_{i}",
                agent_class=DataAnalyzer,
                depends_on=collector_ids,
                agent_kwargs={'use_llm_name': llm_name, 'use_vlm_name': os.getenv("VLM_MODEL_NAME"), 'use_embedding_name': os.getenv("EMBEDDING_MODEL_NAME")},
                run_kwargs={'input_data': {'task': f'Research: {ctx.target_name} ({ctx.stock_code})', 'analysis_task': task}, 'max_iterations': 20, 'enable_chart': True},
            ))

        # Report — depends on all analyzers
        analyzer_ids = [f"analyze_{i}" for i in range(len(analysis_tasks))]
        graph.add_task(TaskNode(
            task_id="report",
            agent_class=ReportGenerator,
            depends_on=analyzer_ids,
            agent_kwargs={'use_llm_name': llm_name, 'use_embedding_name': os.getenv("EMBEDDING_MODEL_NAME")},
            run_kwargs={'input_data': {'task': f'Research: {ctx.target_name} ({ctx.stock_code})'}, 'max_iterations': 20},
        ))
        return graph

    def get_post_process_flags(self):
        return {'add_introduction': True, 'add_cover_page': True, 'add_references': True, 'enable_chart': True}
```

### 7.4 Existing Types Migration

| Current `target_type` | Plugin | Key Differences |
|----------------------|--------|-----------------|
| `financial_company` | `src/plugins/financial_company/` | Cover page, financial tables, chart-enabled |
| `financial_industry` | `src/plugins/financial_industry/` | No cover page, industry-specific prompts |
| `financial_macro` | `src/plugins/financial_macro/` | Macro-specific tools, US macro support |
| `general` | `src/plugins/general/` | No cover page, no introduction, simpler prompts |
| `governance` | `src/plugins/governance/` | Governance-specific prompts |

### 7.5 Adding a New Report Type

1. Create `src/plugins/my_type/` with `__init__.py` and `plugin.py`
2. Implement `ReportPlugin.build_task_graph()` — define the DAG
3. Add `prompts/` with type-specific overrides (shared base prompts inherited automatically)
4. Add `templates/` with outline template and docx reference if needed
5. Set `target_type: my_type` in config YAML
6. Done — no other code changes required

---

## 8. Module 4: Prompt Deduplication

### 8.1 Current Duplication (Confirmed by Reading All 14 YAML Files)

| Prompt Key | Copies | Files |
|-----------|--------|-------|
| `select_data` | **4x identical** | memory/financial, memory/general, report_gen/financial, report_gen/general |
| `select_analysis` | **4x identical** | same as above |
| `data_api` | **4x identical** | analyzer/financial, analyzer/general, report_gen/financial, report_gen/general |
| `data_api_outline` | 2x identical | report_gen/financial, report_gen/general |
| `vlm_critique` | 2x near-identical | analyzer/financial, analyzer/general (1 word diff) |
| `img_search` | 2x near-identical | analyzer/financial, analyzer/general |
| `title_generation` | 2x near-identical | report_gen/financial, report_gen/general |
| `report_draft` == `report_draft_wo_chart` | **bug** | analyzer/financial (has `# TODO: fix this`) |
| `generate_task` | 4x same template | Different examples per type |

### 8.2 Strategy: Shared Base + Type Overlays

```
src/prompts/
    _base/                          # Single-copy shared prompts
        select_data.yaml            # Was 4 copies → 1
        select_analysis.yaml        # Was 4 copies → 1
        data_api.yaml               # Was 4 copies → 1
        vlm_critique.yaml           # Was 2 copies → 1
```

Each agent's prompts become `_base.yaml` (shared structure) + `{type}.yaml` (domain-specific overrides):

```
src/plugins/financial_company/prompts/
    data_analyzer.yaml              # Financial-specific: persona, chart font, examples
    report_generator.yaml           # Financial-specific: section_writing, abstract, cover
    memory.yaml                     # Financial-specific: generate_task examples
```

### 8.3 Enhanced PromptLoader

```python
class PromptLoader:
    def _load_prompts(self):
        # 1. Load _base.yaml for this agent (shared structure)
        base = self._read_yaml(self.prompts_dir / "_base.yaml")
        # 2. Overlay type-specific file
        override = self.prompts_dir / f"{self.report_type}.yaml"
        if override.exists():
            base.update(self._read_yaml(override))
        # 3. Fall back to shared _base/ for utility prompts
        for shared in self.shared_dir.glob("*.yaml"):
            key = shared.stem
            if key not in base:
                base[key] = self._read_yaml(shared)[key]
        self.prompts = base
```

### 8.4 Results

- **14 YAML files → ~8 unique files** (plus 4 shared base files)
- `select_data` / `select_analysis` / `data_api` / `vlm_critique`: single source of truth
- `report_draft_wo_chart` bug fixed (properly separate from `report_draft`)
- `generate_task` template shared, only examples differ per type

---

## 9. Module 5: Agent Refactoring

### 9.1 BaseAgent Changes (`src/agents/base_agent.py`)

**Constructor**: Accept `task_context: TaskContext` instead of `memory`.

```python
def __init__(self, config, tools, task_context, use_llm_name=..., enable_code=True, agent_id=None):
    # ...
    self.task_context = task_context  # replaces self.memory
```

**`_agent_tool_function`**: Logs to lightweight collector, not Memory.

**Checkpoint**: Delegates to `CheckpointManager` instead of `self.save()`.

**Core loop UNCHANGED**: `async_run()` conversation iteration, `_parse_llm_response()`, `_execute_action()`, `_handle_code_action()`, `_handle_final_action()` — all stay as-is.

### 9.2 DataAnalyzer Refactoring

**Before** (ad-hoc state machine):
```python
# Current: string-based phase tracking
self.current_phase = 'phase1'
# ... 60 lines of if/elif chains ...
if self.current_phase == 'phase1':
    run_result = await super().async_run(...)
    self.current_phase = 'phase2'
elif self.current_phase == 'phase2':
    self._parse_generated_report(...)
    self.current_phase = 'phase3'
# ...
```

**After** (PhaseRunner):
```python
def _build_phases(self):
    return [
        Phase("analyze", self._phase_analyze),
        Phase("parse", self._phase_parse_report),
        Phase("charts", self._phase_draw_charts),
        Phase("finalize", self._phase_build_result),
    ]

async def async_run(self, ...):
    runner = PhaseRunner(self._build_phases(), self.checkpoint_mgr, self.id)
    return await runner.run(start_from=self._resume_phase, ...)
```

**Additional fixes:**
- Remove non-functional `Semaphore(1)` (created fresh each iteration at line 257)
- Remove dual checkpoint file (`charts.pkl` becomes the `charts` phase checkpoint)
- Fix `report_draft` == `report_draft_wo_chart` bug

### 9.3 ReportGenerator Refactoring

**Before** (3 separate state variables):
```python
self._phase = 'outline'          # 'outline' | 'sections' | 'post_process'
self._section_index_done = 0     # 0..N
self._post_stage = 0             # 0..4
```

**After** (7 named phases via PhaseRunner):
```python
Phase("outline",        self._phase_generate_outline),
Phase("sections",       self._phase_write_sections),     # tracks section index internally
Phase("post_images",    self._phase_replace_images),
Phase("post_abstract",  self._phase_add_abstract),
Phase("post_cover",     self._phase_add_cover),
Phase("post_references",self._phase_add_references),
Phase("render",         self._phase_render_docx),
```

**Additional fixes:**
- Remove never-read `report_obj_stageN` deepcopies (lines 546, 571, 583, 597)
- Use `DataSelector` for section writing data access (extracted from Memory)

### 9.4 DataCollector Refactoring

Minimal change: swap `memory` → `task_context`, use `task_context.add_collected_data()`.

---

## 10. Module 6: Codebase Cleanup

### 10.1 Memory Decomposition

**Memory (~515 lines) splits into:**

| Responsibility | New Location | Lines |
|---------------|-------------|-------|
| Data store (collected_data, analysis_results) | `TaskContext` | ~60 |
| Task generation (LLM-based) | `TaskPlanner` | ~80 |
| Data selection (LLM-based) | `DataSelector` | ~100 |
| Agent lifecycle (get_or_create_agent, is_finished) | `Pipeline` | ~80 |
| Logging (add_log, get_log) | Lightweight log collector in Pipeline | ~30 |
| Embedding cache | `TaskContext.data_embeddings` | ~10 |

Memory becomes a thin adapter in Phase 1, then deprecated in Phase 4.

### 10.2 Eliminate Duplicated Orchestration

`run_report.py` (265 lines) and `app.py` (~250 lines of pipeline code) both become thin wrappers around `Pipeline.run()`.

### 10.3 Delete Duplicated Templates

`demo/backend/template/` contains copies of `src/template/` files. After plugin migration, templates live in `src/plugins/*/templates/`. Both `src/template/` and `demo/backend/template/` are deleted.

### 10.4 Configuration Validation (Phase 4)

Add Pydantic models for config validation. Currently `Config` is a raw dict wrapper with no validation — config errors produce cryptic runtime failures.

### 10.5 Progress Events (Phase 4)

Simple `EventEmitter` on Pipeline firing typed events (`task_started`, `task_completed`, `phase_started`). The demo backend's WebSocket handler consumes these, replacing current ad-hoc broadcasting.

---

## 11. Directory Structure

```
src/
    core/                               # NEW (9 files)
        __init__.py
        agent_result.py                 # AgentResult, AgentStatus
        task_context.py                 # TaskContext (data bus)
        task_graph.py                   # TaskGraph, TaskNode, TaskState
        pipeline.py                     # Pipeline (orchestrator + DAG scheduler)
        phase_runner.py                 # PhaseRunner, Phase
        checkpoint.py                   # CheckpointManager
        task_planner.py                 # LLM task generation (from Memory)
        data_selector.py                # LLM data selection (from Memory)
        events.py                       # EventEmitter for progress

    agents/                             # MODIFIED (use core abstractions)
        __init__.py
        base_agent.py                   # task_context, CheckpointManager
        data_collector/
            data_collector.py
        data_analyzer/
            data_analyzer.py            # PhaseRunner, fix bugs
        report_generator/
            report_generator.py         # PhaseRunner, remove deepcopy waste
            report_class.py             # UNCHANGED
        search_agent/
            search_agent.py             # UNCHANGED

    plugins/                            # NEW (report type plugins)
        __init__.py                     # Plugin discovery
        base_plugin.py                  # ReportPlugin ABC
        financial_company/
            __init__.py
            plugin.py
            prompts/
            templates/
        financial_industry/ ...
        financial_macro/ ...
        general/ ...
        governance/ ...

    prompts/                            # NEW (shared base)
        _base/
            select_data.yaml
            select_analysis.yaml
            data_api.yaml
            vlm_critique.yaml

    tools/                              # UNCHANGED
    config/                             # MODIFIED (add Pydantic in Phase 4)
    utils/                              # UNCHANGED
    memory/                             # DEPRECATED after Phase 2
```

### File Migration Map

| Current | After | Change |
|---------|-------|--------|
| `run_report.py` | `run_report.py` | 265 lines → ~15 lines |
| `demo/backend/app.py` | Same | Drop ~220 lines duplicated orchestration |
| `src/memory/variable_memory.py` | Adapter → deprecated | Decomposed into TaskContext + TaskPlanner + DataSelector |
| `src/agents/base_agent.py` | Same | Modified constructor + checkpoint |
| `src/agents/data_analyzer/data_analyzer.py` | Same | PhaseRunner, fix bugs |
| `src/agents/report_generator/report_generator.py` | Same | PhaseRunner, remove waste |
| `src/template/*` | `src/plugins/*/templates/` | Moved |
| `demo/backend/template/*` | Deleted | Duplicates removed |
| `src/agents/*/prompts/*.yaml` | `src/plugins/*/prompts/` + `src/prompts/_base/` | Deduplicated |
| `src/memory/prompts/*.yaml` | `src/plugins/*/prompts/` + `src/prompts/_base/` | Deduplicated |

---

## 12. Migration Plan (12 Weeks)

### Phase 1: Foundation (Weeks 1-3)

**Goal**: Create core abstractions. Memory becomes adapter. Existing pipeline still works.

| Task | Files | Notes |
|------|-------|-------|
| Create `src/core/` package | `agent_result.py`, `task_context.py`, `task_graph.py`, `phase_runner.py`, `checkpoint.py`, `pipeline.py`, `task_planner.py`, `data_selector.py`, `events.py` | New files |
| Memory adapter | `variable_memory.py` | Delegate `add_data()` to `task_context.add_collected_data()` internally |
| Extract TaskPlanner | `task_planner.py` | Memory's `generate_*` methods become thin wrappers |
| Extract DataSelector | `data_selector.py` | Memory's `select_*` and `retrieve_*` methods become thin wrappers |
| Checkpoint migration util | `checkpoint.py` | Read old `.pkl` files, write to new layout |
| Unit tests | `tests/test_core/` | TaskGraph DAG, TaskContext thread safety, PhaseRunner |

**Verification**: Run `python run_report.py` — identical behavior. Old checkpoints resume correctly.

### Phase 2: Agent Refactoring + Pipeline (Weeks 4-7)

**Goal**: Agents use new abstractions directly. Pipeline replaces procedural orchestration.

| Task | Files | Notes |
|------|-------|-------|
| Refactor BaseAgent | `base_agent.py` | `task_context` instead of `memory`, use `CheckpointManager` |
| Refactor DataAnalyzer | `data_analyzer.py` | PhaseRunner, fix Semaphore bug, unified checkpoint |
| Refactor ReportGenerator | `report_generator.py` | PhaseRunner, remove deepcopy waste |
| Refactor DataCollector | `data_collector.py` | `task_context` swap |
| Implement Pipeline | `pipeline.py` | DAG execution loop |
| Simplify run_report.py | `run_report.py` | 265 → ~15 lines |
| Simplify app.py | `app.py` | Drop ~220 lines, use Pipeline |

**Verification**: E2E report for `financial_company`. Resume test (interrupt mid-analysis). Demo backend integration test.

### Phase 3: Plugins + Prompts (Weeks 8-10)

**Goal**: Report types become plugins. Prompts deduplicated.

| Task | Files | Notes |
|------|-------|-------|
| Create plugin framework | `src/plugins/base_plugin.py`, `__init__.py` | ABC + discovery |
| Migrate 5 report types | `src/plugins/*/plugin.py` | One plugin per type |
| Move templates | `src/template/` → `src/plugins/*/templates/` | Delete originals |
| Delete demo templates | `demo/backend/template/` | Duplicates |
| Create shared prompts | `src/prompts/_base/` | 4 files |
| Deduplicate per-agent prompts | `src/plugins/*/prompts/` | Base + overlay |
| Enhance PromptLoader | `prompt_loader.py` | Add overlay mechanism |
| Fix `report_draft_wo_chart` bug | analyzer financial prompts | Separate properly |

**Verification**: Generate reports for all 5 types. Create test plugin to verify extensibility.

### Phase 4: Polish (Weeks 11-12)

**Goal**: Clean up, validate, document.

| Task | Files | Notes |
|------|-------|-------|
| Deprecate Memory | `variable_memory.py` | Remove or minimize |
| Config validation | `config.py` | Add Pydantic models |
| Progress events | `events.py` + `app.py` | Replace ad-hoc broadcasting |
| Integration tests | `tests/` | All 5 plugin types |
| Checkpoint migration test | `tests/` | v1 → v2 format |

---

## 13. Scope Explicitly Cut (and Why)

| Feature | Reason for Cutting |
|---------|-------------------|
| **YAML graph DSL** | 5 graph topologies don't justify a parser + validator + wildcard expansion. Python is simpler and debuggable. |
| **SwarmDispatcher** | Adds a layer between Scheduler and agents that does nothing Scheduler can't do in 6 lines. 4 agent types ≠ a "swarm." |
| **Skill Injection System** | Skills = "append text to prompt." A `custom_instructions` config field achieves the same in 10 lines. |
| **Auto-Adapt from Examples** | 4 chained LLM calls to parse an example report, infer data needs, generate prompts, build a graph. Too fragile. Users copy an existing plugin folder. |
| **Full Human-in-the-Loop** | 4 interaction modes + FeedbackLoop + re-dispatch. v2.0 needs simple checkpoint pausing only. Full gates need significant UI work → v2.1. |
| **`src/` → `finsight/` rename** | Breaks every import in the project for cosmetic benefit. |
| **LLM interaction model change** | XML tag parsing works. Function calling would be better but is orthogonal to this refactoring. Separate effort. |
| **Report density tiers** | Good feature but low priority vs structural fixes. Add after core refactoring stabilizes. |

All cut features can be added incrementally after v2.0 ships — the architecture supports them, but building them now would delay the core improvements.

---

## 14. Risk Assessment

### High Risk

| Risk | Mitigation |
|------|-----------|
| Breaking checkpoint compatibility | Phase 1 includes migration utility. Memory adapter provides backward compatibility. |
| Pipeline scheduler correctness (deadlocks, lost results) | Extensive unit tests for TaskGraph. `asyncio.wait(FIRST_COMPLETED)` is well-understood. |
| Agent refactoring breaks report quality | Side-by-side comparison: generate same report with old and new code. Prompt content unchanged. |

### Medium Risk

| Risk | Mitigation |
|------|-----------|
| Prompt deduplication changes behavior | Byte-compare effective prompt content before and after. Only structural changes, not content. |
| Plugin migration misses edge cases per report type | Generate reports for all 5 types in Phase 3. Compare with pre-refactoring output. |
| PhaseRunner doesn't handle DataAnalyzer's chart-per-chart granularity | `_phase_draw_charts` handles its own internal loop and per-chart checkpointing. PhaseRunner only manages phase-level transitions. |

### Low Risk

| Risk | Mitigation |
|------|-----------|
| Tool system needs changes | Tool system is explicitly unchanged. |
| LLM wrappers need changes | AsyncLLM is explicitly unchanged. |
| Demo frontend breaks | Only backend changes. Frontend API contract unchanged. |

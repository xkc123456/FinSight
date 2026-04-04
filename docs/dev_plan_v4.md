# FinSight v2.0 重构开发文档

> 本文档是 FinSight 研报生成系统重构的**唯一参考**，包含：现状问题、目标架构、详细代码规格、逐周任务清单。
> 
> **目录**：[1.现状问题](#1-现状问题) · [2.设计原则](#2-设计原则) · [3.目标架构](#3-目标架构) · [4.核心模块规格](#4-核心模块规格) · [5.不变的部分](#5-不变的部分) · [6.排除范围](#6-明确排除的范围) · [7.逐周任务](#7-逐周任务清单) · [8.可调试性](#8-可调试性) · [9.交付物](#9-交付物清单) · [10.风险](#10-风险与缓解)

---

## 1. 现状问题

FinSight 是一个多 Agent 研究报告生成系统。当前架构存在 7 个已验证的问题：

| # | 问题 | 位置 | 影响 |
|---|------|------|------|
| 1 | **僵化的优先级编排** — 所有 analyzer 必须等全部 collector 完成 | `run_report.py` L196-257 | 一个 collector 慢，整条线卡住 |
| 2 | **臃肿的 Memory 类** — 515 行混杂 6 项职责：数据存储、日志、Agent 工厂、任务规划、数据选择、Embedding 缓存 | `src/memory/variable_memory.py` | 不可测试、不可复用 |
| 3 | **编排逻辑重复** — `run_report.py` 和 `demo/backend/app.py` 约 220 行几乎相同 | 两个文件 | 改一处忘另一处 |
| 4 | **临时阶段管理** — DataAnalyzer 用 `self.current_phase` 字符串(4 阶段)，ReportGenerator 用 `_phase` + `_section_index_done` + `_post_stage` 三元组 | 两个 Agent | 恢复逻辑脆弱 |
| 5 | **Prompt 大量重复** — `data_api` 3 份完全相同、`table_beautify` 5 份完全相同、`financial_company` 和 `financial_industry` 的 11/11 key 100% 相同 | `src/agents/*/prompts/` | 改一份忘 N 份 |
| 6 | **不一致的检查点** — DataAnalyzer 双重检查点 `latest.pkl`+`charts.pkl`，ReportGenerator 4 处 `deepcopy` 从未被读取，`Semaphore(1)` 每次循环重建从未共享 | DataAnalyzer L245/278/294，ReportGenerator L546/570/581/595 | 多余 IO + 内存浪费 |
| 7 | **Bug** — `report_draft_wo_chart` 与 `report_draft` 在 `financial_prompts.yaml` 中字节完全相同（有 `# TODO: fix this` 注释） | `data_analyzer/prompts/financial_prompts.yaml` L207-256 | 关闭图表时仍生成图表指令 |

**额外问题**：DataCollector 硬编码加载全部 financial/macro/industry 工具(L40-55)，无法按报告类型定制；Prompt 加载硬编码 `report_type='general'`(L32)。

---

## 2. 设计原则

1. **没有两个调用方就不做抽象** — 只有一处使用就内联
2. **数据通过 TaskContext 流转，状态通过 AgentResult 流转** — 清晰分离
3. **可调试性是一等特性** — 每次 DAG 状态转换记日志，Pipeline 状态 JSON 可审查
4. **Plugin 只声明差异** — 基类提供通用 DAG；Plugin 只覆盖独特部分

---

## 3. 目标架构

### 3.1 目录结构

```
src/
├── core/                               # 【新增】编排核心，5 个文件
│   ├── __init__.py
│   ├── task_context.py                 #   共享数据总线（put/get）
│   ├── task_graph.py                   #   DAG 引擎 + AgentResult + 失败级联 + min_soft_deps
│   ├── pipeline.py                     #   统一编排器 + PipelineEvent + dry-run
│   ├── checkpoint.py                   #   Pipeline→JSON / Agent→dill
│   └── llm_helpers.py                  #   纯函数：任务规划 & 数据选择
│
├── agents/                             # 【修改】memory → task_context
│   ├── base_agent.py                   #   + _run_phases() + checkpoint_mgr
│   ├── data_collector/                 #   + tool_categories 过滤
│   ├── data_analyzer/                  #   4 phase: analyze→parse→charts→finalize
│   ├── report_generator/               #   7 phase: outline→…→render
│   ├── search_agent/
│   └── chart_generator/
│
├── plugins/                            # 【新增】报告类型 Plugin
│   ├── __init__.py                     #   load_plugin() + 注册表
│   ├── base_plugin.py                  #   ReportPlugin ABC + PostProcessFlags + 默认 DAG
│   ├── financial_company/              #   plugin.py + prompts/ + templates/
│   ├── financial_industry/
│   ├── financial_macro/
│   ├── general/
│   └── governance/
│
├── prompts/                            # 【新增】共享基础 Prompt
│   └── _base/                          #   8 个 YAML（去重后）
│
├── tools/                              # 【不变】
├── config/                             # 【修改】+ Pydantic 校验
├── utils/                              # 【不变】
├── memory/                             #   第 6 周移除
├── report_packs/                       #   删除（仅 __pycache__）
└── scenario/                           #   删除（仅 __pycache__）
```

### 3.2 数据流

```
用户 YAML 配置
    │
    ▼
 Pipeline.run()
    ├── generate_tasks()          ← LLM 规划 + 自定义任务
    ├── Plugin.build_task_graph() ← 构建 DAG（软/硬依赖 + min_soft_deps）
    └── _execute_graph()          ← 并发调度，max_concurrent 控制
            │
            ├── DataCollector × N     → ctx.put("collected_data", ToolResult)
            │       │ (soft dep, min=1)
            ├── DataAnalyzer  × M     → ctx.put("analysis_results", AnalysisResult)
            │       │ (soft dep, min=1)
            └── ReportGenerator × 1   → ctx.put("report", Report) → Markdown/DOCX
```

---

## 4. 核心模块规格

### 4.1 `src/core/task_context.py` — 共享数据总线

```python
from __future__ import annotations
import threading, json
from typing import Any
from src.config.config import Config

class TaskContext:
    def __init__(self, config: Config, target_name: str, stock_code: str,
                 target_type: str, language: str):
        self.config = config
        self.target_name = target_name
        self.stock_code = stock_code
        self.target_type = target_type
        self.language = language
        self._artifacts: dict[str, list[Any]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: Config) -> TaskContext:
        c = config.config
        return cls(config=config, target_name=c['target_name'],
                   stock_code=c.get('stock_code', ''),
                   target_type=c['target_type'], language=c.get('language', 'zh'))

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._artifacts.setdefault(key, []).append(value)

    def get(self, key: str) -> list[Any]:
        with self._lock:
            return list(self._artifacts.get(key, []))

    def to_dict(self) -> dict:
        return {"target_name": self.target_name, "stock_code": self.stock_code,
                "target_type": self.target_type, "language": self.language,
                "artifacts": {k: [str(v) for v in vs] for k, vs in self._artifacts.items()}}

    def restore_from_dict(self, data: dict) -> None:
        self.target_name = data["target_name"]
        self.stock_code = data["stock_code"]

    def load_artifacts_from(self, json_path: str) -> None:
        """单 Agent 调试用。"""
        with open(json_path, 'r', encoding='utf-8') as f:
            self._artifacts = json.load(f).get("task_context", {}).get("artifacts", {})
```

**不设便捷属性**（如 `ctx.collected_data`），避免对 `ToolResult`/`Report` 的导入依赖，防止 God Object。

---

### 4.2 `src/core/task_graph.py` — DAG 引擎

```python
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

class AgentStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"

@dataclass
class AgentResult:
    agent_id: str
    status: AgentStatus
    error: Optional[str] = None

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
    depends_on: list[str]                                       # 硬依赖
    soft_depends_on: list[str] = field(default_factory=list)    # 软依赖
    min_soft_deps: int = 0                                      # 软依赖最低成功数
    state: TaskState = TaskState.PENDING
    result: Optional[AgentResult] = None

class TaskGraph:
    def __init__(self):
        self._nodes: dict[str, TaskNode] = {}

    def add_task(self, node: TaskNode) -> 'TaskGraph':
        self._nodes[node.task_id] = node
        return self

    def get_ready_tasks(self) -> list[TaskNode]:
        ready = []
        for n in self._nodes.values():
            if n.state != TaskState.PENDING:
                continue
            if not all(self._nodes[d].state == TaskState.DONE for d in n.depends_on):
                continue
            terminal = {TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED}
            if not all(self._nodes[d].state in terminal for d in n.soft_depends_on):
                continue
            done_soft = sum(1 for d in n.soft_depends_on
                           if self._nodes[d].state == TaskState.DONE)
            if done_soft < n.min_soft_deps:
                n.state = TaskState.SKIPPED
                self._cascade_skip(n.task_id)
                continue
            ready.append(n)
        return ready

    def get_failed_soft_deps(self, task_id: str) -> list[str]:
        node = self._nodes[task_id]
        return [d for d in node.soft_depends_on
                if self._nodes[d].state in (TaskState.FAILED, TaskState.SKIPPED)]

    def mark_done(self, task_id: str, result: AgentResult):
        self._nodes[task_id].state = TaskState.DONE
        self._nodes[task_id].result = result

    def mark_failed(self, task_id: str, error: str):
        self._nodes[task_id].state = TaskState.FAILED
        self._nodes[task_id].result = AgentResult(task_id, AgentStatus.FAILED, error)
        self._cascade_skip(task_id)

    def _cascade_skip(self, failed_id: str):
        for node in self._nodes.values():
            if failed_id in node.depends_on and node.state == TaskState.PENDING:
                node.state = TaskState.SKIPPED
                self._cascade_skip(node.task_id)

    def is_complete(self) -> bool:
        return all(n.state in (TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED)
                   for n in self._nodes.values())

    def summary(self) -> dict:
        return {tid: n.state.value for tid, n in self._nodes.items()}

    def to_dict(self) -> dict:
        return {tid: {"state": n.state.value, "depends_on": n.depends_on,
                       "soft_depends_on": n.soft_depends_on,
                       "error": n.result.error if n.result else None}
                for tid, n in self._nodes.items()}

    def restore_from_dict(self, data: dict) -> None:
        for tid, info in data.items():
            if tid in self._nodes:
                self._nodes[tid].state = TaskState(info["state"])
```

**关键设计**：
- **硬依赖**失败 → 下游 SKIP，沿硬依赖边级联
- **软依赖**失败 → 不级联，下游仍执行，但可通过 `get_failed_soft_deps()` 感知缺失
- **`min_soft_deps`** → 在 DAG 层统一"数据充足性"判断，Agent 无需各自校验

**典型配置**：collector 全部并行 → analyzer 软依赖全部 collector(min=1) → report 软依赖全部 analyzer(min=1)

---

### 4.3 `src/core/pipeline.py` — 统一编排器

```python
import asyncio, json, logging
from dataclasses import dataclass
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

@dataclass
class PipelineEvent:
    type: str       # "task_started" | "task_completed" | "task_failed"
    task_id: str
    error: Optional[str] = None

class Pipeline:
    def __init__(self, config, max_concurrent=3,
                 on_event: Callable[[PipelineEvent], Awaitable[None]] | None = None,
                 max_retries=0, dry_run=False):
        self.config = config
        self.max_concurrent = max_concurrent
        self.on_event = on_event
        self.max_retries = max_retries
        self.dry_run = dry_run
        self.checkpoint_mgr = CheckpointManager(config.working_dir)

    async def run(self, plugin, task_context, resume=True):
        all_collect, all_analyze = await generate_tasks(task_context, self.config, plugin)
        graph = plugin.build_task_graph(self.config, task_context, all_collect, all_analyze)
        if self.dry_run:
            print(f"=== Dry Run ===\nCollect: {all_collect}\nAnalyze: {all_analyze}")
            print(f"DAG: {json.dumps(graph.summary(), indent=2)}")
            return
        if resume:
            self.checkpoint_mgr.restore_pipeline(graph, task_context)
        await self._execute_graph(graph, task_context)
        self.checkpoint_mgr.save_pipeline(graph, task_context)

    async def _execute_graph(self, graph, ctx):
        sem = asyncio.Semaphore(self.max_concurrent)
        running: dict[str, asyncio.Task] = {}
        while not graph.is_complete():
            for node in graph.get_ready_tasks():
                if node.task_id in running:
                    continue
                node.state = TaskState.RUNNING
                await self._emit("task_started", node.task_id)
                running[node.task_id] = asyncio.create_task(
                    self._run_node(node, ctx, sem, graph))
            if not running:
                break
            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                tid = next(k for k, v in running.items() if v is task)
                del running[tid]
                try:
                    graph.mark_done(tid, task.result())
                    await self._emit("task_completed", tid)
                except Exception as e:
                    graph.mark_failed(tid, str(e))
                    await self._emit("task_failed", tid, error=str(e))
            logger.info(f"DAG state: {graph.summary()}")
            self.checkpoint_mgr.save_pipeline(graph, ctx)

    async def _run_node(self, node, ctx, sem, graph):
        async with sem:
            agent = await self._create_or_restore_agent(node, ctx)
            failed_deps = graph.get_failed_soft_deps(node.task_id)
            if failed_deps:
                node.run_kwargs['missing_dependencies'] = failed_deps
                logger.warning(f"{node.task_id}: soft deps failed: {failed_deps}")
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

    async def _create_or_restore_agent(self, node, ctx):
        """注意：from_checkpoint 是 async classmethod，必须 await。"""
        saved = self.checkpoint_mgr.load_agent(node.task_id, phase=None)
        if saved:
            return await node.agent_class.from_checkpoint(
                config=self.config, task_context=ctx,
                agent_id=node.task_id, **node.agent_kwargs)
        agent = node.agent_class(config=self.config, task_context=ctx, **node.agent_kwargs)
        agent.checkpoint_mgr = self.checkpoint_mgr
        return agent

    async def _emit(self, event_type, task_id, **kwargs):
        if self.on_event:
            try:
                await self.on_event(PipelineEvent(type=event_type, task_id=task_id, **kwargs))
            except Exception as e:
                logger.error(f"Event callback failed: {e}")
```

**回调 vs EventEmitter**：只有一个消费者(WebSocket)，回调更简单。`_emit` 内 try-catch 确保外部故障不崩 Pipeline。

`generate_tasks` 函数：
```python
async def generate_tasks(ctx, config, plugin):
    prompt_loader = PromptLoader.create_loader_for_memory(ctx.target_type)
    query = f"Research target: {ctx.target_name}"
    custom_collect = config.config.get('custom_collect_tasks', [])
    custom_analyze = config.config.get('custom_analysis_tasks', [])
    llm_collect = await generate_collect_tasks(ctx, config, prompt_loader, query, custom_collect)
    llm_analyze = await generate_analyze_tasks(ctx, config, prompt_loader, query, custom_analyze)
    return custom_collect + llm_collect, custom_analyze + llm_analyze
```

---

### 4.4 `src/core/checkpoint.py` — 检查点管理

```python
CHECKPOINT_VERSION = 2

class CheckpointManager:
    def __init__(self, working_dir):
        self.checkpoint_dir = os.path.join(working_dir, 'checkpoints')

    def save_pipeline(self, graph, ctx):
        data = {"version": CHECKPOINT_VERSION, "saved_at": datetime.utcnow().isoformat(),
                "graph": graph.to_dict(), "task_context": ctx.to_dict()}
        path = os.path.join(self.checkpoint_dir, 'pipeline.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子写

    def restore_pipeline(self, graph, ctx) -> bool:
        path = os.path.join(self.checkpoint_dir, 'pipeline.json')
        if not os.path.exists(path): return False
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get("version", 1) != CHECKPOINT_VERSION:
            logger.warning("Checkpoint version mismatch. Starting fresh.")
            return False
        graph.restore_from_dict(data["graph"])
        ctx.restore_from_dict(data["task_context"])
        return True

    def save_agent(self, agent_id, phase, data): ...   # dill
    def load_agent(self, agent_id, phase): ...          # dill
```

**格式**：Pipeline → JSON（`cat` 即审查）；Agent 内部 → dill（lambda 序列化需要）。旧 checkpoint 不迁移。

---

### 4.5 `src/core/llm_helpers.py` — 纯函数

从 `variable_memory.py` 提取 4 个方法，改为模块级 async 函数：

```python
# ---- Task Planning（Pipeline 调用） ----
async def generate_collect_tasks(ctx, config, prompt_loader, query, existing, max_num=5): ...
async def generate_analyze_tasks(ctx, config, prompt_loader, query, existing, max_num=5): ...

# ---- Data Selection（Agent 调用） ----
async def select_data_by_llm(ctx, config, prompt_loader, query, max_k=-1): ...
async def select_analysis_by_llm(ctx, config, prompt_loader, query, max_k=-1): ...
```

迁移：`self.data` → `ctx.get("collected_data")`，`self.config` → `config` 参数。

`retrieve_relevant_data()`(Embedding) 迁至 `src/utils/index_builder.py`。

---

### 4.6 `BaseAgent._run_phases()` — 阶段管理

内联到 `base_agent.py`（约 10 行）：

```python
async def _run_phases(self, phases: list[tuple[str, Callable]], start_from: str | None = None):
    started = start_from is None
    for name, fn in phases:
        if not started:
            if name == start_from: started = True
            else: continue
        self.logger.info(f"[{self.AGENT_NAME}] Phase: {name}")
        await fn()
        if hasattr(self, 'checkpoint_mgr') and self.checkpoint_mgr:
            self.checkpoint_mgr.save_agent(self.id, name, self._get_checkpoint_state())

def _get_checkpoint_state(self) -> dict:
    return {}  # 子类覆盖
```

消除 DataAnalyzer 的 `current_phase` 和 ReportGenerator 的三元组状态。

---

### 4.7 Plugin 系统

```python
# src/plugins/base_plugin.py
@dataclass
class PostProcessFlags:
    add_introduction: bool = True
    add_cover_page: bool = False
    add_references: bool = True
    enable_chart: bool = True

class ReportPlugin(ABC):
    name: str
    def get_prompt_dir(self) -> Path: ...
    def get_template_path(self, name: str) -> Path: ...
    def get_tool_categories(self) -> list[str]:
        return ['financial', 'macro', 'industry', 'web']
    def get_post_process_flags(self) -> PostProcessFlags:
        return PostProcessFlags()
    def build_task_graph(self, config, ctx, collect_tasks, analyze_tasks) -> TaskGraph:
        """默认 DAG，大多数 Plugin 不需要覆盖。"""
        ...
```

**各 Plugin 配置**：

| Plugin | Tool 分类 | 说明 |
|--------|-----------|------|
| `financial_company` | `financial, macro, industry, web` | 全部 |
| `financial_industry` | `financial, macro, industry, web` | 全部 |
| `financial_macro` | `macro, web` | 不需个股 API |
| `general` | `web` | 仅搜索 |
| `governance` | `web` | 仅搜索 |

**新增报告类型**只需：新建 `plugins/xxx/plugin.py`(~20行) + `prompts/` + `templates/` + 配置 `target_type: "xxx"`。

---

### 4.8 Prompt 去重

`src/prompts/_base/` 存共享 Prompt。Plugin `prompts/` 只存类型特定 key。查找顺序：Plugin → `_base/` → 报错。按 `(plugin_name, key)` 缓存。

**提取清单**：

| 基础文件 | 原始重复数 |
|----------|-----------|
| `data_api.yaml` | 3 份完全相同 |
| `data_api_outline.yaml` | 3 份完全相同 |
| `table_beautify.yaml` | 5 份完全相同 |
| `select_data.yaml` | 2+1 近似（参数化 `{analyst_role}`） |
| `select_analysis.yaml` | 同上 |
| `outline_critique.yaml` | 2 对重复 |
| `outline_refinement.yaml` | 2 对重复 |
| `vlm_critique.yaml` | 2 份近似（参数化 `{domain}`） |

**不去重的**（确实有差异）：`generate_task`、`abstract`、`outline_draft`、`data_analysis`。

---

## 5. 不变的部分

| 模块 | 说明 |
|------|------|
| `src/tools/` | base.py、\_\_init\_\_.py、所有工具实现 |
| `src/utils/llm.py` | AsyncLLM 封装 |
| BaseAgent 核心循环 | `async_run`、`_parse_llm_response`、`_execute_action` |
| `src/utils/code_executor_async.py` | AsyncCodeExecutor |
| `report_class.py` | Report / Section 模型 |
| `src/utils/index_builder.py` | 仅新增 `retrieve_relevant_data` 函数 |
| 速率限制器、Logger、异步桥接 | 全部不变 |

---

## 6. 明确排除的范围

- **YAML 图 DSL** — 仅用 Python 定义 DAG
- **`src/` → `finsight/` 重命名** — 不值得为美观破坏 import
- **Function Calling** — XML 解析不变，FC 独立后续
- **并行章节生成** — 当前顺序+逐章检查点正确，并行化独立处理
- **人机交互门控** — 仅支持检查点暂停

---

## 7. 逐周任务清单

### 全局约定

| 项目 | 约定 |
|------|------|
| 分支 | 从 `2.0` 拉 `refactor/core`，每周 PR 合回 |
| Python | ≥ 3.10 |
| 测试 | `pytest -x --tb=short`；新增覆盖率 ≥ 80% |
| 提交 | `feat:` / `fix:` / `refactor:` / `chore:` 前缀 |
| CI | `pytest` + `ruff check` 全通过 |

### W1 — 核心模块

| 任务 | 操作 |
|------|------|
| W1-1 | 新建 `src/core/task_context.py`（§ 4.1） |
| W1-2 | 新建 `src/core/task_graph.py`（§ 4.2） |
| W1-3 | 新建 `src/core/checkpoint.py`（§ 4.4） |
| W1-4 | 新建 `src/core/llm_helpers.py`（§ 4.5，从 `variable_memory.py` 提取） |
| W1-5 | 修改 `base_agent.py`：新增 `task_context` 参数 + `_run_phases()`(§ 4.6) + 扩展 `from_checkpoint` 签名为 `async def from_checkpoint(cls, config, memory=None, task_context=None, ...)` |

**测试**：`test_task_context.py`（线程安全/序列化）、`test_task_graph.py`（7 case：线性/级联 SKIP/软依赖/min_soft_deps）、`test_checkpoint.py`（往返/版本/原子写）、`test_llm_helpers.py`（mock LLM）

### W2 — Agent 重构

| 任务 | 操作 |
|------|------|
| W2-1 | `data_collector.py`：`memory→task_context`；`_set_default_tools` 按 `tool_categories` 加载；修复 `report_type='general'` 硬编码 |
| W2-2 | `data_analyzer.py`：`_run_phases` 4 阶段；删除 `Semaphore(1)`(L257)；删除 `charts.pkl`(L245/278/294)；修复 `report_draft_wo_chart` bug（以 `general_prompts.yaml` 为参考）|
| W2-3 | `report_generator.py`：`_run_phases` 7 阶段；删除 `_phase`/`_section_index_done`/`_post_stage`；删除 4 处 `deepcopy`(L546/570/581/595)；`memory.get_*` → `task_context.get(...)` |
| W2-4 | `search_agent.py` / `chart_generator/` 改 `memory→task_context`；删除 `orchestrator/` |

**验证**：W1 单元测试 + `financial_company` 端到端 + 中途中断恢复

### W3 — Pipeline + 编排整合

| 任务 | 操作 |
|------|------|
| W3-1 | 新建 `src/core/pipeline.py`（§ 4.3 + `generate_tasks` 函数 + dry-run） |
| W3-2 | 重写 `run_report.py`：265 行 → ~25 行 + argparse(`--config`/`--dry-run`/`--no-resume`/`--max-concurrent`) |
| W3-3 | 重写 `demo/backend/app.py`：删除 ~220 行编排逻辑，改为 `Pipeline(on_event=broadcast)` |
| W3-4 | 删除 `demo/backend/template/`：`company_outline*.md` 删除(重复)；`report_template.docx` 移入 Plugin |

**验证**：`run_report.py` 生成完整报告 / `--dry-run` / WebSocket 事件 / 中断恢复

### W4 — Plugin 系统

| 任务 | 操作 |
|------|------|
| W4-1 | 新建 `plugins/__init__.py`（注册表 + `load_plugin`）+ `base_plugin.py`（§ 4.7） |
| W4-2 | 新建 5 个 Plugin（各 ~20 行 `plugin.py` + `prompts/` + `templates/`） |
| W4-3 | 删除 `report_packs/`、`scenario/`、`src/template/` |

**验证**：5 种报告类型通过 Plugin 生成

### W5 — Prompt 去重

| 任务 | 操作 |
|------|------|
| W5-1 | 新建 `src/prompts/_base/`（8 YAML，见 § 4.8 清单） |
| W5-2 | 改造 `prompt_loader.py`：继承查找(Plugin → `_base/`) + `(plugin_name, key)` 缓存 |
| W5-3 | 移动 Agent prompt YAML → `plugins/*/prompts/`，仅保留类型特定 key |

**验证**：逐 key 字节比对旧/新 PromptLoader 输出

### W6 — 收尾

| 任务 | 操作 |
|------|------|
| W6-1 | `config.py` 添加 Pydantic `ConfigSchema` 校验 |
| W6-2 | `variable_memory.py` 添加 `DeprecationWarning` |
| W6-3 | 新增 `test_pipeline.py`(5 case: 正常/部分失败/全失败 SKIP/重试/事件回调)、`test_plugin.py`；现有测试改 `memory→task_context`（含 `tests/agents/` 下 4 个） |
| W6-4 | 全局搜索 `from src.memory` 确认零残留（已知 10 处）；更新 `README.md`、`ADVANCED_USAGE.md` |

**验证**：`pytest` 全通过 / 5 种类型端到端 / `pipeline.json` 审查 / dry-run

---

## 8. 可调试性

**DAG 状态日志** — 每次状态转换自动输出：
```
INFO DAG state: {collect_0: done, collect_1: failed, collect_2: done, analyze_0: pending, report: pending}
```

**JSON 检查点** — `cat outputs/*/checkpoints/pipeline.json |  python -m json.tool` 即可审查。

**单 Agent 测试**：
```python
ctx = TaskContext.from_config(config)
ctx.load_artifacts_from("outputs/moutai/checkpoints/pipeline.json")
analyzer = DataAnalyzer(config=config, task_context=ctx)
await analyzer.async_run(input_data={'analysis_task': '分析营收结构'})
```

**Dry-Run**：`python run_report.py --dry-run` 打印 DAG 拓扑不执行。

---

## 9. 交付物清单

| 周 | 新建 | 修改 | 删除 |
|----|------|------|------|
| W1 | `src/core/` 5 文件 + 4 测试 | `base_agent.py` | — |
| W2 | — | 4 Agent + prompt yaml | `orchestrator/` |
| W3 | `pipeline.py` | `run_report.py`, `app.py` | `demo/backend/template/` |
| W4 | `plugins/` 12+ 文件 | — | `report_packs/`, `scenario/`, `template/` |
| W5 | `prompts/_base/` 8 yaml | `prompt_loader.py` + 各 prompt yaml | — |
| W6 | 2 测试文件 | `config.py`, tests, docs | `memory/`(或标废弃) |

---

## 10. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| `memory→task_context` 遗漏 | AttributeError | W2 全局搜索 `self.memory.` 零残留（已知 10 处引用） |
| `from_checkpoint` 是 async | 忘 `await` 导致协程未执行 | `_create_or_restore_agent` 标为 `async def` |
| matplotlib 并发 | 图表损坏 | W2 移除 Semaphore 后并行画图测试 |
| Prompt 去重误删 | 生成异常 | W5 逐 key 字节比对 |
| 检查点格式变更 | 旧 checkpoint 失效 | 直接作废，新版本从头开始 |
| LLM 输出不确定 | 端到端测试不稳定 | 以 "DAG 全终结" 为通过标准 |

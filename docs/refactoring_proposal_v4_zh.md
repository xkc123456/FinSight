# FinSight v2.0 重构方案 (v4)

## 背景

FinSight 是一个多 Agent 研究报告生成系统。当前架构存在以下已验证的痛点：

1. **僵化的优先级层级编排** — 所有分析器必须等待全部采集器完成 (`run_report.py:196-257`)
2. **臃肿的 Memory 类** — 约 515 行代码混杂了 6 项职责：数据存储、日志存储、Agent 工厂、任务规划、数据选择、Embedding 缓存 (`src/memory/variable_memory.py`)
3. **编排逻辑重复** — `run_report.py` 和 `demo/backend/app.py` 共享约 200 行几乎相同的优先级分组逻辑
4. **临时性的阶段管理** — DataAnalyzer 中基于字符串的 `self.current_phase`（4 个阶段），ReportGenerator 中 3 个独立的状态变量（`_phase`、`_section_index_done`、`_post_stage`）
5. **大量 Prompt 重复** — `select_data` 有 2 份完全重复 + 1 份近似重复，`data_api` 在 report_generator 中有 3 份完全重复，`financial_company_prompts.yaml` 和 `financial_industry_prompts.yaml` 的 11 个 key 中有 8 个完全相同
6. **不一致的检查点机制** — DataAnalyzer 的双重检查点 `latest.pkl` + `charts.pkl`，ReportGenerator 中从未被读取的 `report_obj_stageN` 深拷贝，无效的 `Semaphore(1)`（每次迭代重新创建，从未共享）
7. **Bug: `report_draft` == `report_draft_wo_chart`** — 在 `data_analyzer/prompts/financial_prompts.yaml` 中（第 207-256 行字节完全相同，有 `# TODO: fix this` 注释确认）

之前的方案（v2、v3）正确识别了这些问题。v2 过度设计（约 15 个抽象、YAML DSL）。v3 精简至要素，但仍引入了不必要的抽象（独立的 EventEmitter、独立的 PhaseRunner 类、独立的 TaskPlanner + DataSelector 文件），并留有空白（无 DAG 失败处理、无可调试性方案、不必要的 Memory 适配层迁移阶段）。

**本方案（v4）** 在 v3 基础上进一步简化：**6 个核心文件（而非 9 个），Plugin 基类中提供默认 DAG，显式的失败处理，以调试为先的设计，6 周迁移计划且无一次性适配层。**

---

## 设计原则

1. **没有两个调用方就不做抽象** — 如果只有一处使用，就内联
2. **数据通过 TaskContext 流转，状态通过 AgentResult 流转** — 清晰分离
3. **可调试性是一等特性** — 每次 DAG 状态转换都记录日志，Pipeline 状态可 JSON 审查，支持单 Agent 测试模式
4. **Plugin 只声明差异，不声明样板代码** — 基类提供通用 DAG；Plugin 只覆盖独特部分

---

## 变更内容

### 1. 核心抽象 (`src/core/` — 6 个文件)

从 v3 的 9 个文件精简而来。PhaseRunner 内联到 BaseAgent，EventEmitter 替换为回调函数，TaskPlanner + DataSelector 合并为纯函数。

#### `agent_result.py` — 轻量级状态信封

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

没有 `artifacts` 字典 — 数据由 Agent 直接写入 TaskContext；AgentResult 仅追踪成功/失败。Pipeline 不需要知道 Agent 产出了什么类型的数据。

#### `task_context.py` — 通用产物存储

```python
class TaskContext:
    """Pipeline 运行中所有 Agent 的共享数据总线。"""
    config: Config
    target_name: str
    stock_code: str
    target_type: str
    language: str

    _artifacts: dict[str, list[Any]]   # key -> 值列表
    _lock: threading.Lock

    def put(self, key: str, value: Any):
        with self._lock:
            self._artifacts.setdefault(key, []).append(value)

    def get(self, key: str) -> list[Any]:
        return list(self._artifacts.get(key, []))

    # 便捷属性用于类型提示 — 无硬编码 Agent 耦合
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
        """可 JSON 序列化的快照，用于检查点和调试。"""
        ...

    @classmethod
    def from_dict(cls, data: dict, config: Config) -> 'TaskContext':
        ...
```

**为何使用通用设计？** 如果后续添加 FactChecker 或 DataValidator Agent，只需 `ctx.put("validation_results", ...)` — 无需修改 TaskContext。便捷属性提供 IDE 自动补全，同时不产生耦合。

#### `task_graph.py` — 带失败传播的 DAG

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
    depends_on: list[str]
    state: TaskState = TaskState.PENDING
    result: Optional[AgentResult] = None

class TaskGraph:
    def __init__(self):
        self._nodes: dict[str, TaskNode] = {}

    def add_task(self, node: TaskNode) -> 'TaskGraph':
        self._nodes[node.task_id] = node
        return self  # 链式调用

    def get_ready_tasks(self) -> list[TaskNode]:
        """返回所有依赖均已完成的任务。"""
        return [
            n for n in self._nodes.values()
            if n.state == TaskState.PENDING
            and all(self._nodes[d].state == TaskState.DONE for d in n.depends_on)
        ]

    def mark_done(self, task_id: str, result: AgentResult):
        self._nodes[task_id].state = TaskState.DONE
        self._nodes[task_id].result = result

    def mark_failed(self, task_id: str, error: str):
        """将任务标记为 FAILED，并递归地将所有下游依赖标记为 SKIPPED。"""
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
        """人类可读的 DAG 状态，用于日志和调试。"""
        return {tid: n.state.value for tid, n in self._nodes.items()}
```

**与 v3 的关键区别：** `mark_failed` 会级联将 SKIP 传播至所有传递性下游依赖。上游任务失败后，下游任务不会永远停留在 PENDING 状态。

#### `pipeline.py` — CLI 和 Web 共用的统一编排器

```python
class Pipeline:
    def __init__(self, config: Config, max_concurrent: int = 3,
                 on_event: Callable[[dict], Awaitable[None]] | None = None,
                 max_retries: int = 0):
        self.config = config
        self.max_concurrent = max_concurrent
        self.on_event = on_event       # 简单回调，无 EventEmitter
        self.max_retries = max_retries
        self.checkpoint_mgr = CheckpointManager(config.working_dir)

    async def run(self, plugin: 'ReportPlugin', task_context: TaskContext,
                  resume: bool = True):
        # 1. 生成 LLM 任务 + 合并配置中的任务
        all_collect, all_analyze = await generate_tasks(
            task_context, self.config, plugin
        )
        # 2. 构建 DAG
        graph = plugin.build_task_graph(self.config, task_context,
                                        all_collect, all_analyze)
        # 3. 恢复：从检查点还原 DAG 状态
        if resume:
            self.checkpoint_mgr.restore_pipeline(graph, task_context)
        # 4. 执行 DAG
        await self._execute_graph(graph, task_context)
        # 5. 保存最终状态
        self.checkpoint_mgr.save_pipeline(graph, task_context)

    async def _execute_graph(self, graph: TaskGraph, ctx: TaskContext):
        sem = asyncio.Semaphore(self.max_concurrent)
        running: dict[str, asyncio.Task] = {}

        while not graph.is_complete():
            # 在并发限制内启动就绪任务
            for node in graph.get_ready_tasks():
                if node.task_id in running:
                    continue
                node.state = TaskState.RUNNING
                await self._emit("task_started", node.task_id)
                running[node.task_id] = asyncio.create_task(
                    self._run_node(node, ctx, sem)
                )

            if not running:
                break  # 死锁防护

            # 等待首个任务完成
            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                # 找到完成的节点
                tid = next(k for k, v in running.items() if v is task)
                del running[tid]
                try:
                    result = task.result()
                    graph.mark_done(tid, result)
                    await self._emit("task_completed", tid)
                except Exception as e:
                    graph.mark_failed(tid, str(e))
                    await self._emit("task_failed", tid, error=str(e))

            # 每次状态转换后记录 DAG 快照
            self._log_graph_state(graph)
            self.checkpoint_mgr.save_pipeline(graph, ctx)

    async def _run_node(self, node: TaskNode, ctx: TaskContext,
                        sem: asyncio.Semaphore) -> AgentResult:
        async with sem:
            agent = node.agent_class(
                config=self.config, task_context=ctx, **node.agent_kwargs
            )
            # 重试封装
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
            await self.on_event({"type": event_type, "task_id": task_id, **kwargs})

    def _log_graph_state(self, graph: TaskGraph):
        logger.info(f"DAG state: {graph.summary()}")
```

**为何用回调而非 EventEmitter？** 只有一个消费者（`app.py` 中的 WebSocket 广播）。回调更简单，无订阅/取消订阅的生命周期管理，且易于测试。

#### `checkpoint.py` — 统一的检查点管理器

```python
class CheckpointManager:
    def __init__(self, working_dir: str):
        self.checkpoint_dir = os.path.join(working_dir, 'checkpoints')

    def save_pipeline(self, graph: TaskGraph, ctx: TaskContext):
        """保存 Pipeline 级别状态为 JSON（可审查）。"""
        ...

    def restore_pipeline(self, graph: TaskGraph, ctx: TaskContext) -> bool:
        ...

    def save_agent(self, agent_id: str, phase: str, data: dict):
        """保存 Agent 级别状态（代码执行器状态使用 dill）。"""
        ...

    def load_agent(self, agent_id: str, phase: str) -> Optional[dict]:
        ...
```

**检查点格式策略：**
- Pipeline 状态（DAG + TaskContext）：**JSON** — 人类可读，可通过任何文本编辑器审查，版本稳定
- Agent 内部状态（对话历史、代码执行器）：**dill** — lambda/闭包序列化所必需

这种分层意味着当出现故障时，你可以直接 `cat checkpoints/pipeline.json` 查看哪些任务已完成、采集了哪些数据，无需启动 Python。

#### `llm_helpers.py` — 从 Memory 中提取的纯函数

```python
async def generate_collect_tasks(ctx, config, prompt_loader, query, existing_tasks, max_num=5) -> list[str]:
    """基于 LLM 的采集任务生成。原 Memory.generate_collect_tasks()。"""
    ...

async def generate_analyze_tasks(ctx, config, prompt_loader, query, existing_tasks, max_num=5) -> list[str]:
    """基于 LLM 的分析任务生成。原 Memory.generate_analyze_tasks()。"""
    ...

async def select_data_by_llm(ctx, config, prompt_loader, query, max_k=-1) -> tuple[list, str]:
    """基于 LLM 的数据选择。原 Memory.select_data_by_llm()。"""
    ...

async def select_analysis_by_llm(ctx, config, prompt_loader, query, max_k=-1) -> tuple[list, str]:
    """基于 LLM 的分析结果选择。原 Memory.select_analysis_result_by_llm()。"""
    ...

async def retrieve_relevant_data(ctx, config, query, top_k=10) -> list:
    """基于 Embedding 的检索。原 Memory.retrieve_relevant_data()。"""
    ...
```

纯函数，不是类。输入输出清晰明确。用 mock TaskContext 即可轻松独立测试。

### 2. 阶段管理 — 内联到 BaseAgent

**不设独立的 PhaseRunner 类。** 该逻辑仅约 10 行且仅被 BaseAgent 子类使用。以 `BaseAgent._run_phases()` 方式添加：

```python
# 在 BaseAgent 中
async def _run_phases(
    self,
    phases: list[tuple[str, Callable]],   # [(name, async_fn), ...]
    start_from: str | None = None,
    **state
) -> dict:
    """按顺序运行命名阶段，每个阶段自动保存检查点。"""
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

DataAnalyzer 用法：
```python
async def async_run(self, ...):
    return await self._run_phases([
        ("analyze", self._phase_analyze),
        ("parse",   self._phase_parse),
        ("charts",  self._phase_charts),
        ("finalize", self._phase_finalize),
    ], start_from=self._resume_phase, ...)
```

ReportGenerator 用法：
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

**优势：**
- 无新文件、无新类、无新数据类
- 消除 DataAnalyzer 中 `self.current_phase = 'phase2'` 的链式赋值
- 消除 ReportGenerator 中 `_phase` + `_section_index_done` + `_post_stage` 三元组
- 每阶段自动保存检查点
- 通过名称恢复（`start_from="charts"`），取代字符串比较链

### 3. Plugin 系统 (`src/plugins/`)

```
src/plugins/
    base_plugin.py              # ReportPlugin 抽象基类，提供默认 DAG
    financial_company/
        plugin.py               # 最小化覆盖
        prompts/                # 仅包含类型特定的 Prompt 覆盖
        templates/
    financial_industry/
    financial_macro/
    general/
    governance/
```

#### `base_plugin.py` — 提供默认 DAG

```python
class ReportPlugin(ABC):
    name: str

    def get_prompt_dir(self) -> Path: ...
    def get_template_path(self, name: str) -> Path: ...

    def get_post_process_flags(self) -> dict:
        """覆盖以自定义后处理行为。"""
        return {
            'add_introduction': True,
            'add_cover_page': False,
            'add_references': True,
            'enable_chart': True,
        }

    def build_task_graph(self, config: Config, ctx: TaskContext,
                         collect_tasks: list[str],
                         analyze_tasks: list[str]) -> TaskGraph:
        """默认 DAG：采集器并行 -> 分析器并行 -> 报告生成。

        大多数 Plugin 不需要覆盖此方法。仅当你的报告类型有真正不同的
        DAG 拓扑时才需覆盖（例如两轮分析，或分析器仅依赖特定采集器）。
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
                depends_on=collector_ids,  # 所有采集器必须完成
            ))
            analyzer_ids.append(tid)

        graph.add_task(TaskNode(
            task_id="report",
            agent_class=ReportGenerator,
            agent_kwargs={...},
            run_kwargs={'input_data': {...}},
            depends_on=analyzer_ids,
        ))
        return graph
```

典型的 Plugin 现在非常精简 — 只声明差异部分：

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

仅当 DAG 拓扑确实不同的 Plugin（例如需要两轮分析的宏观报告）才需覆盖 `build_task_graph()`。

### 4. Prompt 去重 (`src/prompts/_base/`)

基于实际文件级检查，以下是真实的重复映射：

| Prompt Key | 重复情况 | 处理方式 |
|---|---|---|
| `data_api`（report_generator） | 3 份完全相同，分布在 company/industry/general | 提取到 `_base/` |
| `data_api_outline` | 3 份完全相同 | 提取到 `_base/` |
| `table_beautify` | 4 份完全相同，分布在所有 financial 类型 | 提取到 `_base/` |
| `select_data` | 2 份完全相同 + 1 份近似（memory） | 提取到 `_base/`，参数化 `{analyst_role}` |
| `select_analysis` | 2 份完全相同 + 1 份近似（memory） | 提取到 `_base/`，参数化 `{analyst_role}` |
| `outline_critique` | 2 对重复 | 提取到 `_base/` |
| `outline_refinement` | 2 对重复 | 提取到 `_base/` |
| `section_writing` | company == industry | 去重 |
| `section_writing_wo_chart` | company == industry | 去重 |
| `final_polish` | company == industry | 去重 |
| `vlm_critique` | 2 份近似（1 个词 + 1 个变量不同） | 提取，参数化 `{domain}` |
| `report_draft_wo_chart` | **BUG**：在 financial 中与 `report_draft` 完全相同 | 立即修复（general 版本为正确模式） |

**策略**：`_base/` 存放共享 Prompt。每个 Plugin 的 `prompts/` 目录仅保留真正不同的 key。`PromptLoader` 解析顺序：Plugin 特定 -> `_base/` -> 报错。

**不值得去重的内容**（确实存在差异）：
- `generate_task` — 3 个版本有完全不同的指南和示例
- `abstract` — 5 个版本对应不同报告类型
- `outline_draft` — 每种类型有实质性不同的大纲生成方式
- `data_analysis` / `data_analysis_wo_chart` — 不同领域有不同的分析规范

### 5. Agent 重构

**BaseAgent** (`src/agents/base_agent.py`)：
- 构造函数接受 `task_context: TaskContext` 而非 `memory`
- 新增 `_run_phases()` 方法（见第 2 节）
- `_agent_tool_function` 记录日志到轻量级列表，而非 Memory
- 检查点委托给 `CheckpointManager`
- 核心 Agent 循环（`async_run`、`_parse_llm_response`、`_execute_action`）**不变**

**DataAnalyzer** (`src/agents/data_analyzer/data_analyzer.py`)：
- 通过 `_run_phases` 实现 4 个阶段：`analyze` -> `parse` -> `charts` -> `finalize`
- 移除双重检查点（`charts.pkl` 合并到基于阶段的检查点机制）
- 移除无效的 `Semaphore(1)`（第 257 行，每次循环重新创建，从未共享）
- **修复 `report_draft_wo_chart` Bug**（financial_prompts.yaml 第 232-256 行）— 以 general_prompts.yaml 的版本为参考

**ReportGenerator** (`src/agents/report_generator/report_generator.py`)：
- 通过 `_run_phases` 实现 7 个阶段：`outline` -> `sections` -> `post_images` -> `post_abstract` -> `post_cover` -> `post_refs` -> `render`
- 消除 `_phase` + `_section_index_done` + `_post_stage` — 改为单一 `_resume_phase` 字符串
- 移除从未被读取的 `report_obj_stageN` 深拷贝（第 546、570、584、598 行）
- 使用 `llm_helpers.select_data_by_llm()` 替代 `self.memory.select_data_by_llm()`

**DataCollector** (`src/agents/data_collector/data_collector.py`)：
- 将 `memory` 替换为 `task_context`，使用 `task_context.put("collected_data", result)`

### 6. 编排逻辑整合

**`run_report.py`** — 从 265 行缩减至约 20 行：
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

**`demo/backend/app.py`** — 移除约 220 行重复编排逻辑：
```python
async def run_report_generation(resume: bool = False):
    config = Config(config_dict=config_dict)
    ctx = TaskContext.from_config(config)
    plugin = load_plugin(config.config['target_type'])
    pipeline = Pipeline(
        config, max_concurrent=3,
        on_event=lambda event: manager.broadcast(event)  # WebSocket 钩子
    )
    await pipeline.run(plugin, ctx, resume=resume)
```

**`demo/backend/template/`** — 删除（`src/template/` 文件的重复副本，现已移入 Plugin）。

### 7. 可调试性特性

这是 v3 中的重大缺失，v4 显式解决。

#### 7a. DAG 状态日志

每次 DAG 状态转换都会记录日志：
```
INFO DAG state: {collect_0: done, collect_1: running, collect_2: pending, analyze_0: pending, report: pending}
```

当任务失败时：
```
ERROR task_failed: collect_1 - ConnectionError: API timeout
INFO  DAG state: {collect_0: done, collect_1: failed, collect_2: done, analyze_0: skipped, report: skipped}
```

可以立即看到什么失败了、什么被跳过了 — 无需在 Agent 日志中 grep 搜索。

#### 7b. JSON 可审查的 Pipeline 检查点

Pipeline 状态保存为 JSON（非 dill）：
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

可以用任何文本编辑器或 `jq` 审查。无需 Python 即可理解 Pipeline 状态。

#### 7c. 单 Agent 测试模式

用于隔离调试特定 Agent：

```python
# test_single_analyzer.py
ctx = TaskContext.from_config(config)
# 从上次运行中加载已采集的数据
ctx.load_artifacts_from("outputs/moutai/checkpoints/pipeline.json")

analyzer = DataAnalyzer(config=config, task_context=ctx)
result = await analyzer.async_run(input_data={
    'task': 'Research target: 贵州茅台',
    'analysis_task': '分析营收结构'
})
```

无需运行完整 Pipeline 即可测试单个 Agent。加载之前的 TaskContext，运行 Agent，检查输出。

#### 7d. Dry-Run 模式

```python
pipeline = Pipeline(config, dry_run=True)
await pipeline.run(plugin, ctx)
# 输出：打印 DAG 拓扑和任务列表，不实际执行任何内容
```

适用于在提交多小时 Pipeline 运行之前，验证 Plugin 的 DAG 构建和任务生成。

### 8. 保持不变的部分

- **工具系统** — `src/tools/base.py`、`src/tools/__init__.py`、所有工具实现
- **LLM 封装** — `src/utils/llm.py`（AsyncLLM）
- **BaseAgent 核心循环** — `async_run` 对话迭代、`_parse_llm_response`、`_execute_action` 处理器
- **AsyncCodeExecutor** — `src/utils/code_executor_async.py`
- **Report/Section 模型** — `src/agents/report_generator/report_class.py`
- **IndexBuilder** — `src/utils/index_builder.py`
- **速率限制器、Logger、异步桥接** — 所有工具类保持不变

---

## 迁移计划（6 周，2 个阶段）

从 v3 的 12 周/4 阶段压缩而来。完全跳过 Memory 适配层 — Agent 直接切换到 TaskContext。

### 第一阶段：核心 + Agent 重构（第 1-3 周）

**目标：** 新建核心包，Agent 完成重构，Pipeline 替代 `run_report.py` 和 `app.py` 中的编排逻辑。

**第 1 周 — 基础搭建：**

新建文件：
- `src/core/__init__.py`
- `src/core/agent_result.py` — AgentResult、AgentStatus
- `src/core/task_context.py` — TaskContext
- `src/core/task_graph.py` — TaskGraph、TaskNode、TaskState
- `src/core/checkpoint.py` — CheckpointManager
- `src/core/llm_helpers.py` — 从 Memory 中提取

修改文件：
- `src/agents/base_agent.py` — 新增 `_run_phases()`，接受 `task_context`

验证：
- TaskGraph 的单元测试（DAG 解析、失败级联、SKIP 传播）
- TaskContext 的单元测试（线程安全、序列化往返）
- `llm_helpers` 的单元测试（使用 mock LLM）

**第 2 周 — Agent 重构：**

修改文件：
- `src/agents/data_collector/data_collector.py` — `memory` -> `task_context`
- `src/agents/data_analyzer/data_analyzer.py` — `_run_phases`、移除双重检查点、移除 `Semaphore(1)`、修复 `report_draft_wo_chart` Bug
- `src/agents/report_generator/report_generator.py` — `_run_phases`、移除 `_phase`/`_section_index_done`/`_post_stage`、移除无用的深拷贝
- `src/agents/data_analyzer/prompts/financial_prompts.yaml` — 修复 `report_draft_wo_chart`（参考 `general_prompts.yaml` 的模式）

验证：
- 使用新 Agent 代码进行 `financial_company` 端到端报告生成
- 恢复测试：在分析过程中中断，重启后验证是否正确继续

**第 3 周 — Pipeline + 编排整合：**

新建文件：
- `src/core/pipeline.py` — Pipeline

修改文件：
- `run_report.py` — 缩减至约 20 行
- `demo/backend/app.py` — 移除重复编排逻辑，使用 Pipeline

删除文件：
- `demo/backend/template/` — `src/template/` 的重复副本

验证：
- CLI：`python run_report.py` 产生与重构前相同的输出
- Web：Demo 后端的 WebSocket 日志通过 `on_event` 回调正常工作
- 跨 CLI 重启的恢复功能

### 第二阶段：Plugin + Prompt + 收尾（第 4-6 周）

**目标：** 报告类型变为 Plugin，Prompt 完成去重，配置校验，测试。

**第 4 周 — Plugin 系统：**

新建文件：
- `src/plugins/base_plugin.py`
- `src/plugins/financial_company/plugin.py`
- `src/plugins/financial_industry/plugin.py`
- `src/plugins/financial_macro/plugin.py`
- `src/plugins/general/plugin.py`
- `src/plugins/governance/plugin.py`

移动文件：
- `src/template/*` -> `src/plugins/*/templates/`

验证：
- 通过 Plugin 为所有 5 种类型生成报告
- 创建一个最小化的测试 Plugin 以验证可扩展性

**第 5 周 — Prompt 去重：**

新建文件：
- `src/prompts/_base/data_api.yaml`
- `src/prompts/_base/data_api_outline.yaml`
- `src/prompts/_base/select_data.yaml`
- `src/prompts/_base/select_analysis.yaml`
- `src/prompts/_base/table_beautify.yaml`
- `src/prompts/_base/vlm_critique.yaml`
- `src/prompts/_base/outline_critique.yaml`
- `src/prompts/_base/outline_refinement.yaml`

修改文件：
- `src/utils/prompt_loader.py` — 基础 + 覆盖层解析
- Agent Prompt YAML 文件 — 移除重复 key，仅保留类型特定的覆盖

移动文件：
- `src/agents/*/prompts/*.yaml` -> `src/plugins/*/prompts/`（仅类型特定的 key）
- `src/memory/prompts/*.yaml` -> 整合到 `_base/` + 最小化覆盖

验证：
- 对所有 5 种报告类型进行去重前后的 Prompt 输出字节比对
- 确保没有 Prompt key 解析为空或缺失

**第 6 周 — 收尾：**

- 在 `src/config/config.py` 中添加配置校验（Pydantic）
- Pipeline 中实现 Dry-run 模式
- 移除已废弃的 `src/memory/` 模块（或标记为废弃并添加警告）
- 所有 5 种报告类型的全面集成测试
- 移除死代码：旧的检查点逻辑、未使用的 Memory 方法

---

## 最终目录结构

```
src/
    core/                           # 新增：6 个文件
        __init__.py
        agent_result.py             # AgentResult、AgentStatus
        task_context.py             # TaskContext（通用产物存储）
        task_graph.py               # TaskGraph、TaskNode（带失败级联）
        pipeline.py                 # Pipeline（编排器 + DAG 执行器）
        checkpoint.py               # CheckpointManager（JSON Pipeline + dill Agent）
        llm_helpers.py              # 纯函数：任务生成、数据选择
    agents/                         # 修改：使用核心抽象
        base_agent.py               # + _run_phases()、task_context、CheckpointManager
        data_collector/
        data_analyzer/
        report_generator/
        search_agent/
    plugins/                        # 新增：报告类型 Plugin
        base_plugin.py              # ReportPlugin 抽象基类，提供默认 DAG
        financial_company/
        financial_industry/
        financial_macro/
        general/
        governance/
    prompts/                        # 新增：共享基础 Prompt
        _base/
            data_api.yaml
            data_api_outline.yaml
            select_data.yaml
            select_analysis.yaml
            table_beautify.yaml
            vlm_critique.yaml
            outline_critique.yaml
            outline_refinement.yaml
    tools/                          # 不变
    config/                         # 修改：第 6 周添加 Pydantic
    utils/                          # 不变
    memory/                         # 第一阶段后废弃，第 6 周移除
```

**文件数量对比：**
| | v3 core/ | v4 core/ |
|--|----------|----------|
| agent_result.py | 有 | 有 |
| task_context.py | 有 | 有（通用化） |
| task_graph.py | 有 | 有（+ 失败级联） |
| pipeline.py | 有 | 有（+ 重试、回调、dry-run） |
| checkpoint.py | 有 | 有（+ JSON 层） |
| phase_runner.py | 有 | **无**（内联到 BaseAgent） |
| task_planner.py | 有 | **无**（合并到 llm_helpers） |
| data_selector.py | 有 | **无**（合并到 llm_helpers） |
| events.py | 有 | **无**（回调参数） |
| llm_helpers.py | 无 | 有（合并了 planner + selector） |
| **合计** | **9** | **6** |

---

## 明确排除的范围

- **YAML 图 DSL** — 仅使用 Python 图定义。可调试，可类型检查。
- **SwarmDispatcher** — Pipeline 直接调度，不增加额外层。
- **技能注入系统** — 如有需要使用 `custom_instructions` 配置字段。
- **基于示例的自动适配** — 太脆弱。用户直接复制现有 Plugin 文件夹。
- **完整的人机协作** — 仅支持简单的检查点暂停。交互式门控推迟处理。
- **`src/` -> `finsight/` 重命名** — 仅为美观就破坏所有 import 不值得。
- **LLM 交互模式变更** — XML 标签解析保持不变。Function Calling 作为独立的后续工作。
- **并行章节生成** — 当前代码中标记为 TODO，推迟到重构后处理。当前的顺序生成方式带有逐章节检查点，正确且可恢复；并行化会引入共享状态复杂性，应独立处理。

---

## 与 v3 的关键差异

| 方面 | v3 | v4 |
|--------|-----|-----|
| core/ 文件数 | 9 | 6 |
| EventEmitter | 独立类 + 独立文件 | `on_event` 回调参数 |
| PhaseRunner | 独立类 + 独立文件 | `BaseAgent._run_phases()` 方法 |
| TaskPlanner + DataSelector | 2 个独立文件，使用类 | 1 个文件，使用纯函数 |
| TaskContext | 按 Agent 类型硬编码字段 | 通用产物存储 + 便捷属性 |
| AgentResult | `artifacts: dict[str, Any]` | 仅状态；数据通过 TaskContext 流转 |
| Plugin DAG | 每个 Plugin 必须实现 `build_task_graph()` | 基类提供默认实现；仅需要时覆盖 |
| DAG 失败处理 | 未讨论 | `mark_failed` 级联 SKIP 到下游 |
| 重试 | 未讨论 | Pipeline 中可配置 `max_retries` |
| Pipeline 检查点 | 格式未讨论 | Pipeline 使用 JSON（可审查），Agent 内部使用 dill |
| 可调试性 | 未讨论 | DAG 状态日志、JSON 检查点、单 Agent 测试、dry-run |
| 迁移周期 | 12 周，4 阶段，Memory 适配层 | 6 周，2 阶段，直接重构 |
| `report_draft_wo_chart` Bug | 推迟到第 3 阶段修复 | 第 2 周修复 |
| LLM 任务生成 + Plugin | 未明确说明 | 显式：Pipeline 在 `build_task_graph()` 之前调用 `generate_tasks()` |

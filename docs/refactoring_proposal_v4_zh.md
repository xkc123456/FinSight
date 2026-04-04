# FinSight v2.0 重构方案 (v4)

## 背景

FinSight 是一个多 Agent 研究报告生成系统。当前架构存在以下已验证的痛点：

1. **僵化的优先级层级编排** — 所有分析器必须等待全部采集器完成 (`run_report.py:196-257`)
2. **臃肿的 Memory 类** — 约 515 行代码混杂了 6 项职责：数据存储、日志存储、Agent 工厂、任务规划、数据选择、Embedding 缓存 (`src/memory/variable_memory.py`)
3. **编排逻辑重复** — `run_report.py` 和 `demo/backend/app.py` 共享约 200 行几乎相同的优先级分组逻辑
4. **临时性的阶段管理** — DataAnalyzer 中基于字符串的 `self.current_phase`（4 个阶段），ReportGenerator 中 3 个独立的状态变量（`_phase`、`_section_index_done`、`_post_stage`）
5. **大量 Prompt 重复** — `select_data` 有 2 份完全重复 + 1 份近似重复，`data_api` 在 report_generator 中有 3 份完全重复，`financial_company_prompts.yaml` 和 `financial_industry_prompts.yaml` 的 11 个 key 完全相同（100% 重叠）
6. **不一致的检查点机制** — DataAnalyzer 的双重检查点 `latest.pkl` + `charts.pkl`，ReportGenerator 中从未被读取的 `report_obj_stageN` 深拷贝，无效的 `Semaphore(1)`（每次迭代重新创建，从未共享）
7. **Bug: `report_draft` == `report_draft_wo_chart`** — 在 `data_analyzer/prompts/financial_prompts.yaml` 中（第 207-256 行字节完全相同，有 `# TODO: fix this` 注释确认）

之前的方案（v2、v3）正确识别了这些问题。v2 过度设计（约 15 个抽象、YAML DSL）。v3 精简至要素，但仍引入了不必要的抽象（独立的 EventEmitter、独立的 PhaseRunner 类、独立的 TaskPlanner + DataSelector 文件），并留有空白（无 DAG 失败处理、无可调试性方案、不必要的 Memory 适配层迁移阶段）。

**本方案（v4）** 在 v3 基础上进一步简化：**5 个核心文件（而非 9 个），Plugin 基类中提供默认 DAG，显式的失败处理，以调试为先的设计，6 周迁移计划且无一次性适配层。**

---

## 设计原则

1. **没有两个调用方就不做抽象** — 如果只有一处使用，就内联
2. **数据通过 TaskContext 流转，状态通过 AgentResult 流转** — 清晰分离
3. **可调试性是一等特性** — 每次 DAG 状态转换都记录日志，Pipeline 状态可 JSON 审查，支持单 Agent 测试模式
4. **Plugin 只声明差异，不声明样板代码** — 基类提供通用 DAG；Plugin 只覆盖独特部分

---

## 变更内容

### 1. 核心抽象 (`src/core/` — 5 个文件)
从 v3 的 9 个文件精简而来。PhaseRunner 内联到 BaseAgent，EventEmitter 替换为回调函数，TaskPlanner + DataSelector 合并为纯函数，AgentResult 内联到 task_graph.py（仅约 10 行代码、仅被 TaskGraph 使用，不值得独立文件）。

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

    def to_dict(self) -> dict:
        """可 JSON 序列化的快照，用于检查点和调试。"""
        ...

    @classmethod
    def from_dict(cls, data: dict, config: Config) -> 'TaskContext':
        ...
```

**为何使用通用设计？** 如果后续添加 FactChecker 或 DataValidator Agent，只需 `ctx.put("validation_results", ...)` — 无需修改 TaskContext。不设便捷属性（如 `ctx.collected_data`），避免引入对具体类型（`ToolResult`、`Report`）的导入依赖，防止 TaskContext 退化为 God Object。调用方直接 `ctx.get("collected_data")` 即可，需要类型安全时在调用处做类型断言。

#### `task_graph.py` — 带失败传播的 DAG

```python
# ---- AgentResult：轻量级状态信封（不值得独立文件，仅被 TaskGraph 使用） ----

class AgentStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"

@dataclass
class AgentResult:
    """没有 artifacts 字典 — 数据由 Agent 直接写入 TaskContext；
    AgentResult 仅追踪成功/失败。Pipeline 不需要知道 Agent 产出了什么类型的数据。"""
    agent_id: str
    status: AgentStatus
    error: Optional[str] = None

# ---- TaskGraph ----

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
    depends_on: list[str]           # 硬依赖：任一失败则本任务 SKIP
    soft_depends_on: list[str] = field(default_factory=list)  # 软依赖：失败不阻塞，仅标记数据缺失
    min_soft_deps: int = 0          # 软依赖最低成功数：不满足则自动 SKIP
    state: TaskState = TaskState.PENDING
    result: Optional[AgentResult] = None

class TaskGraph:
    def __init__(self):
        self._nodes: dict[str, TaskNode] = {}

    def add_task(self, node: TaskNode) -> 'TaskGraph':  
        self._nodes[node.task_id] = node
        return self  # 链式调用

    def get_ready_tasks(self) -> list[TaskNode]:
        """返回所有硬依赖已完成、且软依赖已终结（DONE/FAILED/SKIPPED）的任务。
        若已完成的软依赖数量 < min_soft_deps，则自动 SKIP。"""
        ready = []
        for n in self._nodes.values():
            if n.state != TaskState.PENDING:
                continue
            # 硬依赖：必须全部 DONE
            if not all(self._nodes[d].state == TaskState.DONE for d in n.depends_on):
                continue
            # 软依赖：必须全部终结（DONE / FAILED / SKIPPED），但允许失败
            terminal = {TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED}
            if not all(self._nodes[d].state in terminal for d in n.soft_depends_on):
                continue
            # min_soft_deps 校验：已成功的软依赖不足时自动 SKIP
            done_soft = sum(1 for d in n.soft_depends_on if self._nodes[d].state == TaskState.DONE)
            if done_soft < n.min_soft_deps:
                n.state = TaskState.SKIPPED
                self._cascade_skip(n.task_id)
                continue
            ready.append(n)
        return ready

    def get_failed_soft_deps(self, task_id: str) -> list[str]:
        """返回指定任务中失败的软依赖列表，供 Agent 在运行时感知数据缺失。"""
        node = self._nodes[task_id]
        return [
            d for d in node.soft_depends_on
            if self._nodes[d].state in (TaskState.FAILED, TaskState.SKIPPED)
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
        """仅沿硬依赖边传播 SKIP。软依赖失败不会导致下游跳过。"""
        for node in self._nodes.values():
            if failed_id in node.depends_on and node.state == TaskState.PENDING:
                node.state = TaskState.SKIPPED
                self._cascade_skip(node.task_id)
            # 注意：failed_id in node.soft_depends_on 时不传播 SKIP

    def is_complete(self) -> bool:
        return all(n.state in (TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED)
                   for n in self._nodes.values())

    def summary(self) -> dict:
        """人类可读的 DAG 状态，用于日志和调试。"""
        return {tid: n.state.value for tid, n in self._nodes.items()}
```

**与 v3 的关键区别：**
- `mark_failed` 会沿**硬依赖**边级联 SKIP 至所有传递性下游依赖，上游任务失败后下游任务不会永远停留在 PENDING。
- 新增**软依赖**（`soft_depends_on`）：软依赖失败时不传播 SKIP，下游任务仍可执行，但可通过 `get_failed_soft_deps()` 感知缺失的上游数据。

**为何需要软依赖？** 在研报生成场景中，数据采集任务（collector）的失败率较高（API 超时、数据源不可用等）。如果 5 个 collector 中有 1 个失败就导致所有 analyzer 被跳过，最终无法产出任何报告。通过将 analyzer 对 collector 的依赖设为软依赖（见 `build_task_graph` 默认实现），我们允许基于部分数据生成分析——部分数据总好过没有数据。只有真正不可缺少的前置任务（如 analyzer → report）才使用硬依赖。

**典型 DAG 依赖配置：**
- `collector_*` → 无依赖
- `analyzer_*` → `depends_on=[]`, `soft_depends_on=[所有 collector_id]`, `min_soft_deps=1`（至少 1 个采集成功才执行）
- `report` → `depends_on=[]`, `soft_depends_on=[所有 analyzer_id]`, `min_soft_deps=1`（至少 1 个分析成功才执行；否则 DAG 层自动 SKIP，无需 Agent 内部判断）

**为何 report 使用软依赖 + min_soft_deps 而非硬依赖？** 硬依赖意味着任何一个 analyzer 失败就导致 report 被 SKIP、整条 Pipeline 无产出。`min_soft_deps=1` 则允许在部分分析失败时仍能出报告，同时保证所有分析都失败时不会产出空报告。"数据充足性"的判断统一在 DAG 层完成，Agent 无需各自实现前置校验。

#### `pipeline.py` — CLI 和 Web 共用的统一编排器

```python
@dataclass
class PipelineEvent:
    type: str       # "task_started" | "task_completed" | "task_failed"
    task_id: str
    error: Optional[str] = None

class Pipeline:
    def __init__(self, config: Config, max_concurrent: int = 3,
                 on_event: Callable[[PipelineEvent], Awaitable[None]] | None = None,
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
                    self._run_node(node, ctx, sem, graph)
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
                        sem: asyncio.Semaphore,
                        graph: TaskGraph) -> AgentResult:
        async with sem:
            agent = await self._create_or_restore_agent(node, ctx)

            # --- 注入软依赖缺失信息，供 Agent 感知数据缺失 ---
            failed_deps = graph.get_failed_soft_deps(node.task_id)
            if failed_deps:
                node.run_kwargs['missing_dependencies'] = failed_deps
                logger.warning(
                    f"{node.task_id}: soft dependencies failed: {failed_deps}, "
                    f"proceeding with partial data"
                )

            # --- 重试封装 ---
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

    async def _create_or_restore_agent(self, node: TaskNode, ctx: TaskContext):
        """Agent 工厂：优先从检查点恢复，否则新建。注意 from_checkpoint 是 async 方法。"""
        saved_state = self.checkpoint_mgr.load_agent(node.task_id, phase=None)
        if saved_state:
            logger.info(f"Restored agent {node.task_id} from checkpoint")
            return await node.agent_class.from_checkpoint(
                config=self.config, task_context=ctx,
                agent_id=node.task_id,
                **node.agent_kwargs
            )
        agent = node.agent_class(
            config=self.config, task_context=ctx, **node.agent_kwargs
        )
        agent.checkpoint_mgr = self.checkpoint_mgr
        return agent

    async def _emit(self, event_type: str, task_id: str, **kwargs):
        if self.on_event:
            try:
                await self.on_event(PipelineEvent(type=event_type, task_id=task_id, **kwargs))
            except Exception as e:
                # 回调异常（如 WebSocket 断开）不应导致 Pipeline 崩溃
                logger.error(f"Event callback failed for {event_type}/{task_id}: {e}")

    def _log_graph_state(self, graph: TaskGraph):
        logger.info(f"DAG state: {graph.summary()}")
```

**为何用回调而非 EventEmitter？** 只有一个消费者（`app.py` 中的 WebSocket 广播）。回调更简单，无订阅/取消订阅的生命周期管理，且易于测试。注意 `_emit` 内部 try-catch 了回调异常，确保 WebSocket 断开等外部故障不会导致整个 Pipeline 崩溃。

**Agent 工厂迁移说明：** 当前 `Memory.get_or_create_agent()` 和 `Memory.from_checkpoint()` 维护了 Agent 身份一致性（同一 agent_id 不会重复创建）。重构后，该职责由 `Pipeline._run_node()` 承担：每个 `TaskNode.task_id` 唯一标识一个 Agent 实例，`_run_node` 首先尝试从 `CheckpointManager` 恢复该 Agent（调用 `agent_class.from_checkpoint()`），仅在无检查点时才新建。这要求所有 Agent 子类实现 `from_checkpoint(cls, saved_state, **kwargs)` 类方法。现有 `BaseAgent` 已有类似的恢复逻辑（`_restore_tools_from_checkpoint` 等），迁移时将其标准化为 `from_checkpoint` 接口即可。

#### `checkpoint.py` — 统一的检查点管理器

```python
CHECKPOINT_VERSION = 2          # 每次不兼容变更时递增

class CheckpointManager:
    def __init__(self, working_dir: str):
        self.checkpoint_dir = os.path.join(working_dir, 'checkpoints')

    def save_pipeline(self, graph: TaskGraph, ctx: TaskContext):
        """保存 Pipeline 级别状态为 JSON（可审查）。"""
        data = {
            "version": CHECKPOINT_VERSION,
            "saved_at": datetime.utcnow().isoformat(),
            "graph": graph.to_dict(),
            "task_context": ctx.to_dict(),
        }
        path = os.path.join(self.checkpoint_dir, 'pipeline.json')
        # 原子写入：先写临时文件再 rename，防止进程中断导致文件损坏
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def restore_pipeline(self, graph: TaskGraph, ctx: TaskContext) -> bool:
        """从检查点恢复 Pipeline 状态。版本不兼容时返回 False 并从头开始。"""
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
        """保存 Agent 级别状态（代码执行器状态使用 dill）。"""
        ...

    def load_agent(self, agent_id: str, phase: str) -> Optional[dict]:
        ...
```

**检查点格式策略：**
- Pipeline 状态（DAG + TaskContext）：**JSON** — 人类可读，可通过任何文本编辑器审查，版本稳定
- Agent 内部状态（对话历史、代码执行器）：**dill** — lambda/闭包序列化所必需
- **版本兼容**：`CHECKPOINT_VERSION` 常量随不兼容变更递增。`restore_pipeline` 在版本不匹配时安全降级为全新运行，并输出警告日志，避免反序列化旧格式时产生隐蔽错误。
- **旧格式不迁移**：重构后旧版 dill 检查点直接作废，用户重新运行即可。旧检查点与新 Agent 结构强耦合，2 周过渡期内大概率已因 LLM 输出差异而失效，实现迁移方法的收益不值得复杂度。

这种分层意味着当出现故障时，你可以直接 `cat checkpoints/pipeline.json` 查看哪些任务已完成、采集了哪些数据，无需启动 Python。

#### `llm_helpers.py` — 从 Memory 中提取的纯函数

文件内按职责分区：任务规划（Pipeline 调用）和数据选择（Agent 调用）。

```python
# ---- Task Planning（由 Pipeline 在 build_task_graph 前调用） ----

async def generate_collect_tasks(ctx, config, prompt_loader, query, existing_tasks, max_num=5) -> list[str]:
    """基于 LLM 的采集任务生成。原 Memory.generate_collect_tasks()。"""
    ...

async def generate_analyze_tasks(ctx, config, prompt_loader, query, existing_tasks, max_num=5) -> list[str]:
    """基于 LLM 的分析任务生成。原 Memory.generate_analyze_tasks()。"""
    ...

# ---- Data Selection（由 Agent 在运行时调用） ----

async def select_data_by_llm(ctx, config, prompt_loader, query, max_k=-1) -> tuple[list, str]:
    """基于 LLM 的数据选择。原 Memory.select_data_by_llm()。"""
    ...

async def select_analysis_by_llm(ctx, config, prompt_loader, query, max_k=-1) -> tuple[list, str]:
    """基于 LLM 的分析结果选择。原 Memory.select_analysis_result_by_llm()。"""
    ...
```

纯函数，不是类。输入输出清晰明确。用 mock TaskContext 即可轻松独立测试。

**注意：** 原 `Memory.retrieve_relevant_data()`（基于 Embedding 的向量检索）不放在此文件——它的职责是向量相似度匹配而非 LLM 调用，与上述 4 个函数不在同一抽象层。该函数保留在 `utils/index_builder.py`（现有 212 行，已承担 embedding 相关职责），接口改为接受 `TaskContext` 参数：

```python
# utils/index_builder.py 中新增
async def retrieve_relevant_data(ctx: TaskContext, config: Config, query: str, top_k: int = 10) -> list:
    """基于 Embedding 的检索。原 Memory.retrieve_relevant_data()。
    迁移至此处，因为 IndexBuilder 已管理 embedding 索引的构建和查询。"""
    ...
```

### 2. 阶段管理 — 内联到 BaseAgent

**不设独立的 PhaseRunner 类。** 该逻辑仅约 10 行且仅被 BaseAgent 子类使用。以 `BaseAgent._run_phases()` 方式添加：

```python
# 在 BaseAgent 中
async def _run_phases(
    self,
    phases: list[tuple[str, Callable]],   # [(name, async_fn), ...]
    start_from: str | None = None,
):
    """按顺序运行命名阶段，每个阶段自动保存检查点。
    
    Phase 函数通过 self 上的实例变量读写状态（与现有代码模式一致），
    不通过参数传递中间结果——避免隐式 dict merge 的类型安全隐患。
    """
    started = start_from is None
    for name, execute_fn in phases:
        if not started:
            if name == start_from:
                started = True
            else:
                continue
        self.logger.info(f"[{self.AGENT_NAME}] Phase: {name}")
        await execute_fn()
        self.checkpoint_mgr.save_agent(self.id, name, self._get_checkpoint_state())
```

Phase 函数签名统一为 `async def _phase_xxx(self) -> None`，通过 `self.xxx` 读写状态。`_get_checkpoint_state()` 由子类实现，返回当前需要持久化的状态 dict。

**为何不用 `**state` 传参？**
- 现有 DataAnalyzer 和 ReportGenerator 的阶段函数都通过 `self` 上的实例变量传递中间结果（如 `self.report`、`self.chart_results`），改为 `**state` 需要大量重写
- `state.update(result)` 没有类型约束——如果 phase 函数返回非 dict 的 truthy 值会运行时报错，上一个 phase 的 key 也可能被下一个 phase 意外覆盖
- 通过 `self` 访问是 Python 中最自然的模式，IDE 可以提供完整的类型提示和自动补全

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
        """返回本报告类型需要的工具分类列表。
        DataCollector 仅加载这些分类下注册的工具，避免无关工具浪费 token。
        默认返回所有分类；子类覆盖以精确控制。"""
        return ['financial', 'macro', 'industry', 'web']

    def get_post_process_flags(self) -> 'PostProcessFlags':
        """覆盖以自定义后处理行为。"""
        return PostProcessFlags()

    def build_task_graph(self, config: Config, ctx: TaskContext,
                         collect_tasks: list[str],
                         analyze_tasks: list[str]) -> TaskGraph:
        """默认 DAG：采集器并行 -> 分析器并行 -> 报告生成。

        大多数 Plugin 不需要覆盖此方法。仅当你的报告类型有真正不同的
        DAG 拓扑时才需覆盖（例如两轮分析，或分析器仅依赖特定采集器）。
        """
        graph = TaskGraph()
        tool_categories = self.get_tool_categories()

        collector_ids = []
        for i, task in enumerate(collect_tasks):
            tid = f"collect_{i}"
            graph.add_task(TaskNode(
                task_id=tid,
                agent_class=DataCollector,
                agent_kwargs={'tool_categories': tool_categories, ...},
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
                depends_on=[],                        # 无硬依赖
                soft_depends_on=collector_ids,         # 容忍部分采集失败
                min_soft_deps=1,                       # 至少 1 个采集成功
            ))
            analyzer_ids.append(tid)

        graph.add_task(TaskNode(
            task_id="report",
            agent_class=ReportGenerator,
            agent_kwargs={...},
            run_kwargs={'input_data': {...}},
            depends_on=[],                             # 无硬依赖
            soft_depends_on=analyzer_ids,              # 容忍部分分析失败
            min_soft_deps=1,                           # 至少 1 个分析成功
        ))
        return graph
```

典型的 Plugin 现在非常精简 — 只声明差异部分：

```python
class GovernancePlugin(ReportPlugin):
    name = "governance"
    # 默认 PostProcessFlags 即可，无需覆盖

    def get_tool_categories(self) -> list[str]:
        # 治理报告只需要 web 搜索，不需要财务/宏观/行业 API
        return ['web']
```

仅当 DAG 拓扑确实不同的 Plugin（例如需要两轮分析的宏观报告）才需覆盖 `build_task_graph()`。

### 4. Prompt 去重 (`src/prompts/_base/`)

基于实际文件级检查，以下是真实的重复映射：

| Prompt Key | 重复情况 | 处理方式 |
|---|---|---|
| `data_api`（report_generator） | 3 份完全相同，分布在 company/industry/general | 提取到 `_base/` |
| `data_api_outline` | 3 份完全相同 | 提取到 `_base/` |
| `table_beautify` | 5 份完全相同，分布在所有 5 种报告类型 | 提取到 `_base/` |
| `select_data` | 2 份完全相同 + 1 份近似（memory） | 提取到 `_base/`，参数化 `{analyst_role}` |
| `select_analysis` | 2 份完全相同 + 1 份近似（memory） | 提取到 `_base/`，参数化 `{analyst_role}` |
| `outline_critique` | 2 对重复 | 提取到 `_base/` |
| `outline_refinement` | 2 对重复 | 提取到 `_base/` |
| `section_writing` | company == industry | 去重 |
| `section_writing_wo_chart` | company == industry | 去重 |
| `final_polish` | company == industry | 去重 |
| `vlm_critique` | 2 份近似（1 个词 + 1 个变量不同） | 提取，参数化 `{domain}` |
| `report_draft_wo_chart` | **BUG**：在 financial 中与 `report_draft` 完全相同 | 立即修复（general 版本为正确模式） |

**策略**：`_base/` 存放共享 Prompt。每个 Plugin 的 `prompts/` 目录仅保留真正不同的 key。`PromptLoader` 解析顺序：Plugin 特定 -> `_base/` -> 报错。首次加载的 Prompt 按 `(plugin_name, key)` 缓存，避免每次 LLM 调用重复读取和合并 YAML 文件。

**注意**：`financial_company` 和 `financial_industry` 的 report_generator prompts 实际 11/11 key 完全相同。短期按两个 Plugin 处理（保留扩展空间）；如果长期确认无差异化需求，可合并为单个 `financial` Plugin。

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
- 移除无效的 `Semaphore(1)`（第 257 行，每次循环重新创建，从未共享）— 如果多个 DataAnalyzer 并行绘图需要 matplotlib 并发控制，改为 Pipeline 级 `asyncio.Semaphore` 注入，而非 Agent 内部自建
- **修复 `report_draft_wo_chart` Bug**（financial_prompts.yaml 第 232-256 行）— 以 general_prompts.yaml 的版本为参考

**ReportGenerator** (`src/agents/report_generator/report_generator.py`)：
- 通过 `_run_phases` 实现 7 个阶段：`outline` -> `sections` -> `post_images` -> `post_abstract` -> `post_cover` -> `post_refs` -> `render`
- 消除 `_phase` + `_section_index_done` + `_post_stage` — 改为单一 `_resume_phase` 字符串
- 移除从未被读取的 `report_obj_stageN` 深拷贝（第 546、570、584、598 行）
- 使用 `llm_helpers.select_data_by_llm()` 替代 `self.memory.select_data_by_llm()`
- 数据充足性校验已由 DAG 层 `min_soft_deps` 统一处理，Agent 内部无需额外前置校验

**DataCollector** (`src/agents/data_collector/data_collector.py`)：
- 将 `memory` 替换为 `task_context`，使用 `task_context.put("collected_data", result)`
- 构造函数新增 `tool_categories: list[str]` 参数，`_set_default_tools()` 仅加载指定分类的工具（当前硬编码加载全部 financial/macro/industry 工具，改为 `get_avail_tools(cat) for cat in tool_categories`）
- 修复 prompt 加载 bug：当前硬编码 `report_type='general'`，改为使用 `ctx.target_type`

**SearchAgent** (`src/agents/search_agent/`)：
- 将 `memory` 替换为 `task_context`，逻辑不变（被 DataCollector 内部调用，不作为独立 DAG 节点）

**ChartGenerator** (`src/agents/chart_generator/`)：
- 将 `memory` 替换为 `task_context`，逻辑不变（被 DataAnalyzer 内部调用，不作为独立 DAG 节点）

**Orchestrator** (`src/agents/orchestrator/`)：
- **删除** — 其编排职责已由 Pipeline + TaskGraph 完全取代

**清理空目录：**
- `src/report_packs/` — 早期 Plugin 雏形，4 个空子目录，在第 4 周 Plugin 系统建立后删除
- `src/scenario/` — 空目录，直接删除

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
- `src/core/task_context.py` — TaskContext
- `src/core/task_graph.py` — TaskGraph、TaskNode、TaskState + AgentResult（内联）
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
- `src/agents/data_analyzer/data_analyzer.py` — `_run_phases`、移除双重检查点、移除 `Semaphore(1)`（评估是否需 Pipeline 级 `asyncio.Semaphore` 替代）、修复 `report_draft_wo_chart` Bug
- `src/agents/report_generator/report_generator.py` — `_run_phases`、移除 `_phase`/`_section_index_done`/`_post_stage`、移除无用的深拷贝
- `src/agents/search_agent/` — `memory` -> `task_context`
- `src/agents/chart_generator/` — `memory` -> `task_context`
- `src/agents/data_analyzer/prompts/financial_prompts.yaml` — 修复 `report_draft_wo_chart`（参考 `general_prompts.yaml` 的模式）

删除文件：
- `src/agents/orchestrator/` — 编排职责已由 Pipeline 取代

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

删除文件：
- `src/report_packs/` — 早期 Plugin 雏形，空目录
- `src/scenario/` — 空目录

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
- `src/utils/prompt_loader.py` — 基础 + 覆盖层解析，按 `(plugin_name, key)` 缓存已加载的 Prompt
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
- 移除死代码：旧的检查点逻辑、未使用的 Memory 方法
- 删除空目录 `src/report_packs/` 和 `src/scenario/`
- 更新现有 `tests/` 目录中的测试以适配新接口（`memory` -> `task_context` 等）

**回归测试策略：**
- **单元测试**：TaskGraph（DAG 解析、失败级联、min_soft_deps SKIP）、TaskContext（线程安全、序列化往返）、Pipeline（mock Agent 的编排逻辑）
- **集成测试**：所有 5 种报告类型端到端生成，以 Pipeline JSON 检查点中的 DAG 状态为验证依据（所有非 SKIP 任务均为 DONE）
- **Prompt 回归**：去重前后对所有 Prompt key 进行输出字节比对，确保无遗漏
- **注意**：LLM 输出具有不确定性，不适合 golden output 对比。以"Pipeline 完整运行且 DAG 全部终结"作为通过标准，而非输出内容完全一致

---

## 最终目录结构

```
src/
    core/                           # 新增：5 个文件
        __init__.py
        task_context.py             # TaskContext（通用产物存储）
        task_graph.py               # TaskGraph、TaskNode + AgentResult（内联）
        pipeline.py                 # Pipeline（编排器 + DAG 执行器 + PipelineEvent）
        checkpoint.py               # CheckpointManager（JSON Pipeline + dill Agent）
        llm_helpers.py              # 纯函数：任务生成、数据选择
    agents/                         # 修改：使用核心抽象
        base_agent.py               # + _run_phases()、task_context、CheckpointManager
        data_collector/
        data_analyzer/
        report_generator/
        search_agent/
    plugins/                        # 新增：报告类型 Plugin
        base_plugin.py              # ReportPlugin 抽象基类 + PostProcessFlags
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
    report_packs/                   # 删除（空目录）
    scenario/                       # 删除（空目录）
```

**文件数量对比：**
| | v3 core/ | v4 core/ |
|--|----------|----------|
| agent_result.py | 有 | **无**（内联到 task_graph.py） |
| task_context.py | 有 | 有（通用化） |
| task_graph.py | 有 | 有（+ AgentResult 内联、失败级联、min_soft_deps） |
| pipeline.py | 有 | 有（+ PipelineEvent、重试、回调、dry-run） |
| checkpoint.py | 有 | 有（+ JSON 层） |
| phase_runner.py | 有 | **无**（内联到 BaseAgent） |
| task_planner.py | 有 | **无**（合并到 llm_helpers） |
| data_selector.py | 有 | **无**（合并到 llm_helpers） |
| events.py | 有 | **无**（回调参数） |
| llm_helpers.py | 无 | 有（合并了 planner + selector） |
| **合计** | **9** | **5** |

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
| core/ 文件数 | 9 | 5 |
| EventEmitter | 独立类 + 独立文件 | `on_event` 回调参数 |
| PhaseRunner | 独立类 + 独立文件 | `BaseAgent._run_phases()` 方法 |
| TaskPlanner + DataSelector | 2 个独立文件，使用类 | 1 个文件，使用纯函数 |
| TaskContext | 按 Agent 类型硬编码字段 | 通用产物存储（纯 put/get） |
| AgentResult | `artifacts: dict[str, Any]` | 仅状态；数据通过 TaskContext 流转 |
| Plugin DAG | 每个 Plugin 必须实现 `build_task_graph()` | 基类提供默认实现；仅需要时覆盖 |
| DAG 失败处理 | 未讨论 | `mark_failed` 级联 SKIP 到下游 |
| 重试 | 未讨论 | Pipeline 中可配置 `max_retries` |
| Pipeline 检查点 | 格式未讨论 | Pipeline 使用 JSON（可审查），Agent 内部使用 dill |
| 可调试性 | 未讨论 | DAG 状态日志、JSON 检查点、单 Agent 测试、dry-run |
| 迁移周期 | 12 周，4 阶段，Memory 适配层 | 6 周，2 阶段，直接重构 |
| `report_draft_wo_chart` Bug | 推迟到第 3 阶段修复 | 第 2 周修复 |
| LLM 任务生成 + Plugin | 未明确说明 | 显式：Pipeline 在 `build_task_graph()` 之前调用 `generate_tasks()` |

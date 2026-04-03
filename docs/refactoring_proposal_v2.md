# FinSight v2.0 重构方案 Review

## 总体评价

这份方案在「识别问题」和「砍掉过度设计」两个方面做得很好。相比之前的 v2 proposal（~15 个新抽象、YAML DSL、Skill Injection、Auto-Adapter），这版确实精简了很多。DAG 调度、Memory 拆分、Plugin 化、Prompt 去重这四个方向都是正确的。

但方案在追求"简洁"的过程中，**仍然引入了一些不必要的抽象层**，并且在几个关键设计决策上可以做得更简洁。以下逐项分析。

---

## 一、`src/core/` 9 个文件 — 仍然偏多，建议精简到 5-6 个

### 1.1 `events.py`（EventEmitter）— 建议删除

方案中 EventEmitter 的唯一消费者是 `app.py` 的 WebSocket handler。但 Pipeline 已经有 `on_event` callback：

```python
pipeline.on_event = lambda event: manager.broadcast(event)
```

一个 callback 函数已经完全够用。EventEmitter 模式（subscribe/emit/unsubscribe）适合多个消费者、动态订阅的场景，但这里只有一个消费者（WebSocket），且生命周期和 Pipeline 完全一致。

**建议**：Pipeline 直接接受 `on_event: Callable` 参数，内部在关键节点调用。不需要单独的 EventEmitter 类。

### 1.2 `phase_runner.py`（PhaseRunner）— 建议内联到 BaseAgent

PhaseRunner 的核心逻辑只有 ~10 行：

```python
for phase in phases:
    if not started and phase.name != start_from:
        continue
    result = await phase.execute(**state)
    state.update(result or {})
    checkpoint_mgr.save_agent(agent_id, phase.name, state)
```

这段逻辑完全可以作为 BaseAgent 的一个 `_run_phases()` 方法，而不需要独立成类。独立成类意味着：
- 需要 Phase dataclass 的定义
- 需要把 checkpoint_mgr 和 agent_id 传入构造函数
- 需要在 agent 中实例化 PhaseRunner

直接作为 BaseAgent 方法的话：

```python
# BaseAgent 中
async def _run_phases(self, phases: list[tuple[str, Callable]], start_from: str = None, **state):
    started = start_from is None
    for name, execute_fn in phases:
        if not started:
            if name == start_from: started = True
            else: continue
        result = await execute_fn(**state)
        state.update(result or {})
        self.checkpoint_mgr.save_agent(self.id, name, state)
    return state
```

DataAnalyzer 和 ReportGenerator 调用 `self._run_phases([("analyze", self._phase_analyze), ...])` 即可。**没有新类型，没有新文件，同样的功能**。

### 1.3 `task_planner.py` 和 `data_selector.py` — 建议合并

两者都是「用 LLM 处理 TaskContext 中的数据」的辅助逻辑，且都依赖 config + prompt_loader + task_context。合并为一个 `llm_helpers.py`（或 `context_ops.py`）更合理：

```python
# src/core/llm_helpers.py
async def generate_collect_tasks(ctx, config, prompt_loader, ...) -> list[str]: ...
async def generate_analysis_tasks(ctx, config, prompt_loader, ...) -> list[str]: ...
async def select_data_by_llm(ctx, config, prompt_loader, query, ...) -> tuple[list, str]: ...
async def select_analysis_by_llm(ctx, config, prompt_loader, query, ...) -> tuple[list, str]: ...
async def retrieve_relevant_data(ctx, config, query, ...) -> list: ...
```

纯函数，不需要类。输入明确，输出明确，易于测试。

### 1.4 精简后的 `src/core/` 结构

| 文件 | 职责 |
|------|------|
| `agent_result.py` | AgentResult, AgentStatus |
| `task_context.py` | TaskContext (数据总线) |
| `task_graph.py` | TaskGraph, TaskNode, TaskState |
| `pipeline.py` | Pipeline (编排 + DAG 调度) |
| `checkpoint.py` | CheckpointManager |
| `llm_helpers.py` | 从 Memory 提取的 LLM 辅助函数 |

**9 个文件 → 6 个文件**。PhaseRunner 内联到 BaseAgent，EventEmitter 变成 callback 参数，TaskPlanner + DataSelector 合并为纯函数模块。

---

## 二、TaskContext 的 API 设计过于僵硬

当前设计：

```python
class TaskContext:
    collected_data: list       # 写死了 DataCollector 的输出类型
    analysis_results: list     # 写死了 DataAnalyzer 的输出类型
    report: Optional[Any]      # 写死了 ReportGenerator 的输出类型
    
    def add_collected_data(self, data): ...
    def add_analysis_result(self, result): ...
```

**问题**：这把 pipeline 的三个阶段硬编码进了 TaskContext。如果未来增加新的 agent 类型（比如 FactChecker、DataValidator），就需要修改 TaskContext 加新字段和新方法。

**建议**：使用通用的 artifact store + 类型约束：

```python
class TaskContext:
    config: Config
    target_name: str
    stock_code: str
    target_type: str
    language: str
    
    _artifacts: dict[str, list[Any]]  # key -> list of results
    _lock: threading.Lock
    
    def put(self, key: str, value: Any):
        """添加一个 artifact 到指定 key 下"""
        with self._lock:
            self._artifacts.setdefault(key, []).append(value)
    
    def get(self, key: str) -> list[Any]:
        """获取指定 key 下的所有 artifacts"""
        return list(self._artifacts.get(key, []))
    
    # 便捷属性，保持类型提示
    @property
    def collected_data(self) -> list[ToolResult]:
        return self.get("collected_data")
    
    @property
    def analysis_results(self) -> list[AnalysisResult]:
        return self.get("analysis_results")
```

这样既保留了类型提示的便利性，又不会因为新增 agent 类型而需要修改 TaskContext。

---

## 三、Plugin 的 `build_task_graph()` 门槛过高

当前设计要求每个 Plugin 都必须实现 `build_task_graph()`，手动构造完整的 DAG。但看了5种报告类型的实际代码后发现：**它们的 DAG 结构几乎一模一样** —— collectors 并行 → analyzers 依赖 collectors → report 依赖所有 analyzers。区别只在于：

- 使用哪些 tools
- 使用哪些 prompts
- 后处理选项（是否加封面、是否加引用等）

让每个 Plugin 都写一遍 DAG 构建逻辑，违背了 DRY 原则。

**建议**：基类提供默认的 DAG 构建，Plugin 只需要声明式地配置差异：

```python
class ReportPlugin(ABC):
    name: str
    
    def get_post_process_flags(self) -> dict:
        return {'add_introduction': True, 'add_cover_page': False, ...}
    
    def get_prompt_dir(self) -> Path: ...
    def get_template_path(self, name: str) -> Path: ...
    
    # 默认实现：标准的 collect → analyze → report DAG
    def build_task_graph(self, config, ctx) -> TaskGraph:
        graph = TaskGraph()
        collect_tasks = config.config.get('custom_collect_tasks', [])
        analysis_tasks = config.config.get('custom_analysis_tasks', [])
        
        # Collectors
        for i, task in enumerate(collect_tasks):
            graph.add_task(TaskNode(f"collect_{i}", DataCollector, ...))
        
        # Analyzers depend on all collectors
        collector_ids = [f"collect_{i}" for i in range(len(collect_tasks))]
        for i, task in enumerate(analysis_tasks):
            graph.add_task(TaskNode(f"analyze_{i}", DataAnalyzer, depends_on=collector_ids, ...))
        
        # Report depends on all analyzers
        analyzer_ids = [f"analyze_{i}" for i in range(len(analysis_tasks))]
        graph.add_task(TaskNode("report", ReportGenerator, depends_on=analyzer_ids, ...))
        
        return graph
```

绝大多数 Plugin 不需要 override `build_task_graph()`。只有真正有不同 DAG 拓扑的（比如某种报告类型需要两轮 analysis）才需要 override。这样新建一个 Plugin 只需要：

```python
class GovernancePlugin(ReportPlugin):
    name = "governance"
    
    def get_post_process_flags(self):
        return {'add_introduction': True, 'add_cover_page': False, 'add_references': True}
```

**极大降低了扩展成本**。

---

## 四、`AgentResult` 的设计可以更精确

当前设计：

```python
@dataclass
class AgentResult:
    agent_id: str
    agent_name: str
    status: AgentStatus
    artifacts: dict[str, Any]  # 无类型
    error: Optional[str] = None
```

`artifacts: dict[str, Any]` 本质上还是无类型的 —— 只是从「ad-hoc dict」变成了「规范化的 ad-hoc dict」。使用者仍然需要知道 key 名称和 value 类型。

**建议**：既然只有 3 种 agent，直接让每种 agent 返回具体类型即可：

- DataCollector → `list[ToolResult]`（已有类型）
- DataAnalyzer → `AnalysisResult`（已有类型）
- ReportGenerator → `Report`（已有类型）

Pipeline 的 DAG 调度器不需要关心具体返回类型 —— 它只需要知道 task 是否成功。数据流通过 TaskContext 传递，而不是通过 AgentResult.artifacts。

如果仍然需要 AgentResult 做统一的状态追踪，可以简化为：

```python
@dataclass
class AgentResult:
    agent_id: str
    status: AgentStatus  # SUCCESS | FAILED | PARTIAL
    error: Optional[str] = None
```

去掉 `artifacts` 和 `agent_name`（可以从 agent_id 或 TaskNode 获取）。数据写入 TaskContext，状态记录在 AgentResult。**职责分离更清晰**。

---

## 五、DAG 执行器缺少失败处理策略

方案中 `_execute_graph` 的逻辑是：

```python
except Exception as e:
    graph.mark_failed(tid, str(e))
    self._emit("task_failed", tid, error=str(e))
```

但没有讨论：
1. **一个 task 失败后，依赖它的下游 task 怎么办？** 应该自动标记为 SKIPPED。
2. **是否支持重试？** 对于 LLM 调用这种不稳定操作，单次失败可能是偶发的。
3. **是否支持 `fail_fast`？** 某些场景下（比如唯一的 ReportGenerator 失败），应该立即停止整个 pipeline。

**建议**：在 TaskGraph 中加入：

```python
def mark_failed(self, task_id: str, error: str):
    self.nodes[task_id].state = TaskState.FAILED
    # 递归标记所有下游为 SKIPPED
    for node in self.nodes.values():
        if task_id in node.depends_on and node.state == TaskState.PENDING:
            node.state = TaskState.SKIPPED
```

以及在 Pipeline 中支持可配置的 `max_retries`（默认0，即不重试）。

---

## 六、Migration 策略的问题

### 6.1 12 周过长

这个项目的核心代码量不大（Memory ~515 行，run_report ~265 行，DataAnalyzer ~600 行，ReportGenerator ~930 行）。4 个 phase 分 12 周过于保守。

**建议**：压缩到 6 周，分 2 个 phase：

| Phase | 周数 | 内容 |
|-------|------|------|
| Phase 1: Core + Agent 重构 | 1-3 周 | 创建 core 包，直接重构 agents（跳过 Memory adapter），实现 Pipeline，简化 run_report.py |
| Phase 2: Plugin + Prompt + Polish | 4-6 周 | Plugin 化，Prompt 去重，Config 验证，测试 |

### 6.2 Memory Adapter 是不必要的过渡

方案 Phase 1 先让 Memory 代理到 TaskContext，Phase 2 再让 agents 直接用 TaskContext。这意味着：
- Phase 1 要写 adapter 代码
- Phase 2 要删掉 adapter 代码
- 两次改动都需要测试

**建议**：直接在 Phase 1 重构 agents。Memory 的接口并不复杂（主要是 `add_data`, `get_collect_data`, `get_analysis_result`, `select_data_by_llm`），直接替换成 TaskContext + llm_helpers 的调用即可。省去一层临时抽象。

---

## 七、其他细节建议

### 7.1 Checkpoint 序列化格式

方案没有讨论序列化格式。当前用 `dill`，这有安全风险（反序列化可执行任意代码）且版本兼容性差。

**建议**：明确保留 dill（如果 agent 状态包含 lambda/闭包等不可 JSON 化的对象），但在 TaskContext / Pipeline 级别用 JSON 或 msgpack，只在 agent 内部状态用 dill。

### 7.2 Prompt 去重可以更激进

方案说 14 个 YAML → 8 个。实际上如果把 `select_data`、`select_analysis`、`data_api`、`vlm_critique` 这些完全相同的 prompt 提到 `_base/` 后，每个 agent 的 type-specific YAML 应该只剩 3-5 个真正不同的 key。

**建议**：统计每个 prompt key 在不同 report type 间的差异率，只有差异率 > 0 的才放到 type-specific YAML 中。

### 7.3 `report_draft` == `report_draft_wo_chart` bug

方案提到了这个 bug，但把修复放到了 Phase 3（Prompt 去重阶段）。这是一个实际影响输出质量的 bug。

**建议**：在 Phase 1 就修复，不需要等到 Phase 3。

### 7.4 DAG 构建中的 LLM 任务生成

方案没有明确说明 LLM 生成的额外 tasks（`generate_collect_tasks`、`generate_analyze_tasks`）在新架构中如何与 Plugin 的 `build_task_graph()` 协作。当前在 `run_report.py` 中，LLM 生成的 tasks 和 config 中的 custom tasks 是合并后一起执行的。

**建议**：在 Plugin 的 `build_task_graph()` 之前，先调用 TaskPlanner 生成额外 tasks，合并到 config 的 task list 中，再传入 `build_task_graph()`。或者让 Pipeline 在构建 graph 前统一处理这一步。需要在方案中明确这个流程。

---

## 八、总结：建议修改清单

| 优先级 | 修改项 | 影响 |
|--------|--------|------|
| **高** | 删除 EventEmitter，用 callback 代替 | 少 1 个文件，概念更简单 |
| **高** | PhaseRunner 内联到 BaseAgent 作为 `_run_phases()` 方法 | 少 1 个文件 + 1 个类型，同样功能 |
| **高** | Plugin 基类提供默认 `build_task_graph()`，子类仅声明配置差异 | 大幅降低扩展成本 |
| **高** | DAG 失败时自动 SKIP 下游 tasks | 必要的健壮性 |
| **中** | TaskPlanner + DataSelector 合并为纯函数模块 | 少 1 个文件，API 更清晰 |
| **中** | TaskContext 用通用 artifact store + 便捷属性 | 更好的扩展性 |
| **中** | AgentResult 去掉 artifacts，数据走 TaskContext | 职责分离更清晰 |
| **中** | 跳过 Memory adapter，直接重构 | 省去无意义的过渡层 |
| **中** | 时间线压缩到 6 周 | 更现实的节奏 |
| **低** | Phase 1 就修复 report_draft bug | 尽早修复质量问题 |
| **低** | 明确 LLM task 生成与 Plugin DAG 构建的协作流程 | 补全方案空白 |
| **低** | Checkpoint 格式分层（JSON for pipeline, dill for agent） | 安全性和兼容性 |

# FinSight 升级开发计划（v1）

## 1. 背景与目标

基于当前 FinSight 的能力（collector/analyzer/report generator + checkpoint），本轮升级目标是把系统从“线性多 Agent 工作流”升级为“可观测、可调度、可扩展、可协同”的任务图驱动系统。

核心目标：
- 建立全链路统计与成本可观测能力（先量化，再优化）。
- 完成 Agent Swarm 化改造：统一 Agent 架构 + 图调度运行时 + 稳定性机制。
- 完成报告插件化与需求理解能力：支持新报告类型快速接入与用户自然语言定制。
- 升级 Demo 交互，增加人机协同入口。

---

## 2. 总体原则

- **先观测后优化**：Stage 0 不改业务逻辑，先建立基线指标。
- **兼容优先、渐进重构**：先跑通“兼容图模式”，再逐步替换旧路径。
- **运行时统一**：所有 Agent 统一到同一执行协议（调度、超时、重试、日志）。
- **插件化优先于硬编码**：报告风格/主题/模板能力以 Pack 承载。
- **可回放、可审计**：关键决策、人工干预、运行事件可追溯。

---

## 3. 分阶段计划（12 周）
## Stage 0（第 0-1 周）：Agent架构优化

### 目标
- 统一 Agent 实现协议：全部基于同一执行框架，统一通过 code executor 调用工具。  
- 将线性流程升级为任务图调度：先兼容现有 collect/analyze/report，再引入 Swarm 式动态扩图。  
- 补齐稳定性能力：并发控制、重试策略、超时治理、失败恢复。  

### 技术路线（参考 Kimi Agent Swarm 思路）
1. **Orchestrator + Worker 模式**：由调度器拆分任务、并行执行、聚合结果。  
2. **任务图驱动执行**：以 DAG 表达依赖，支持运行期 fan-out / fan-in。  
3. **关键路径优先**：按节点 criticality 动态分配并发、模型档位与重试预算。  
4. **兼容迁移**：先上线兼容图模式，再逐步替换旧线性路径。  

### 任务清单（Checklist）
- [ ] **S0-1 统一 Agent 协议（优先级 P0）**
  - [ ] 定义统一输入输出协议：`AgentInput / AgentResult / ToolCallRecord`。  
  - [ ] 统一 BaseAgent 执行动作，仅保留 `<execute>` 与 `<final_result>`。  
  - [ ] 增加 legacy 兼容层，将旧 action（如 search/click）映射到统一协议。  

- [ ] **S0-2 全量 Agent 迁移到 code executor（优先级 P0）**
  - [ ] 重构 `DeepSearchAgent`：统一走 code executor + `call_tool`。  
  - [ ] 清理 agent 内部混用调用方式，移除 `asyncio.run` 等不安全异步调用。  
  - [ ] 统一工具调用日志结构（入参/出参/耗时/异常/重试次数）。  

- [ ] **S0-3 线性流程升级为任务图运行时（优先级 P0）**
  - [ ] 新增任务图核心模型：`TaskNode / TaskGraph / NodeState / RetryPolicy`。  
  - [ ] 新增 planner：把现有 collect/analyze/report 编译为兼容 DAG。  
  - [ ] 新增 scheduler：按依赖执行 DAG，支持并行节点与失败隔离。  
  - [ ] `run_report.py` 增加双模式开关：`PIPELINE_MODE=linear|graph`。  

- [ ] **S0-4 引入 Swarm 动态调度能力（优先级 P1）**
  - [ ] 增加 orchestrator 节点，支持运行期动态拆分子任务。  
  - [ ] 支持运行期扩图（新增节点/依赖）与聚合节点（fan-in）。  
  - [ ] 增加 criticality score，用于调度优先级、超时与重试预算。  

- [ ] **S0-5 稳定性增强（优先级 P0）**
  - [ ] 超时治理：节点级 + 工具级 + LLM 调用级超时，区分 soft/hard timeout。  
  - [ ] 重试治理：异常分类（可重试/不可重试）+ 指数退避 + jitter。  
  - [ ] 并发治理：全局并发池 + 服务级限流 + 背压队列。  
  - [ ] 幂等保障：节点 `idempotency_key` 与断点恢复一致性检查。  

### 交付物
- 统一 Agent 协议文档与迁移清单。  
- 最小可用任务图运行时（planner + scheduler + runtime）。  
- Swarm 动态扩图能力（含示例任务）。  
- 稳定性策略配置（超时/重试/并发）与默认策略模板。  

### 验收标准
- 所有 Agent 统一到 code executor 路径（无 function-call / executor 混用）。  
- 图模式可稳定跑通原三阶段流程，并支持至少一种动态扩图场景。  
- 常见失败场景（超时、429、临时网络故障）可自动恢复，且可追溯。  
- 与线性流程对比：质量不下降，端到端耗时有可观优化（目标 20%+）。  


## Stage 1（第 1-2 周）：全链路可观测与统计体系

### 目标
建立报告生成全过程指标链路，支持 API 调用、token、任务数量、重试、超时、成功率等统计。

### 交付物
- 统一事件模型（run/node/llm/tool）。
- 运行级指标汇总文件：`metrics.json`。
- 事件流水文件：`events.jsonl`。
- Demo 端可查看基础运行指标（run 级别 + 节点级别）。

### 任务拆解
1. 在 LLM 调用层埋点：请求耗时、token in/out、模型名、异常类型。  
2. 在工具调用层埋点：tool 名称、调用次数、成功率、耗时。  
3. 在调度层埋点：任务数、并发数、排队时长、执行时长、重试次数。  
4. 增加一次运行的 run_id 与 trace_id，串联全链路。  
5. 输出指标聚合器（按 run 结束时写入聚合结果）。

### 验收标准
- 每次运行均可生成完整指标文件。  
- 能清晰回答：本次报告总 token、总 API 次数、失败节点、耗时瓶颈。  
- 指标采集不显著影响性能（额外开销 < 5%）。


---

## Stage 2（第 2-4 周）：报告插件化 + 用户需求理解

### 目标
实现报告能力插件化，支持通过示例报告/模板快速接入新类型；支持自然语言控制内容方向、风格、粒度，并影响运行图构建。

### 交付物
- Report Pack 机制（manifest/tasks/outline/prompts/render）。
- Prompt 组合与回退机制（runtime > pack > default）。
- RequirementSpec（自然语言需求结构化）。
- Skill/分析路径注入机制（用户预设思路直接影响任务图）。

### 任务拆解
1. 设计并实现 Report Pack：
   - `manifest.yaml`：pack 元信息与能力开关。  
   - `tasks.yaml`：默认 collect/analyze 任务模板。  
   - `outline.md`：结构模板。  
   - `prompts/*.yaml`：各 agent prompt 模块。  
   - `render.yaml`：输出格式与排版策略。
2. Prompt 系统重构：
   - 支持 pack 私有 prompt 覆盖。  
   - 缺失键自动 fallback 到默认 prompt。
3. 需求理解引擎：
   - 从用户自然语言提取：主题、风格、深度、输出粒度、预算约束。  
   - 产出 RequirementSpec 并喂给 planner 构图。
4. 示例学习与泛化：
   - 支持通过 example report 提取 style 约束与章节偏好。  
   - 形成 pack 内的风格偏置模板（非在线训练，规则 + prompt 归纳）。

### 验收标准
- 新增一种报告类型时，主要新增 pack 文件，不改核心调度代码。  
- 用户用自然语言可调整：内容方向、文风、粒度。  
- 用户提供分析路径后，任务图结构能显式体现并执行。

---

## Stage 3（第 5-8 周）：UI 人机协同入口

### 目标
在 Demo 中加入“可干预、可确认、可回放”的协同入口，支持人机共同完成高质量报告。

### 交付物
- 计划预览与确认（运行前）。
- 运行中节点干预（暂停/跳过/重跑/补充指令）。
- 人工反馈注入（对某节点追加约束）。
- 运行后审计视图（人工干预历史 + 影响范围）。

### 任务拆解
1. 后端 API：
   - 任务图预览/确认接口。  
   - 节点控制接口（pause/resume/retry/skip）。  
   - 干预日志接口（who/when/what/impact）。
2. 前端页面增强：
   - 执行页增加任务图视图与节点状态面板。  
   - 增加干预弹窗与运行日志联动。  
   - 增加“人工意见”输入区，写入节点上下文。

### 验收标准
- 用户可在不停止全局运行的前提下干预节点。  
- 干预行为可追溯，并可在最终报告附录体现。  
- Demo 可完整演示“自动生成 + 人工修正 + 继续执行”。

---

## 4. 架构与模块落地建议

### 新增模块
- `src/pipeline/models.py`：任务图与策略模型。
- `src/pipeline/planner.py`：从需求/配置/pack 编译任务图。
- `src/pipeline/scheduler.py`：DAG 调度器。
- `src/pipeline/runtime.py`：统一执行运行时。
- `src/pipeline/metrics.py`：指标采集与聚合。
- `src/report_packs/loader.py`：Pack 加载与校验。
- `src/report_packs/registry.py`：Pack 注册与发现。
- `src/scenario/router.py`：需求理解与 skill 注入路由。

### 重点改造模块
- `run_report.py`：从线性优先级执行切到任务图入口。  
- `src/agents/*`：统一执行协议与状态返回结构。  
- `src/memory/variable_memory.py`：支持节点级状态与trace记录。  
- `src/utils/prompt_loader.py`：支持 pack 级 prompt 解析策略。  
- `demo/backend/app.py` + `demo/frontend`：协同控制入口。

---

## 5. 核心数据模型（建议）

```python
TaskNode:
  id: str
  kind: str  # collect/analyze/report/section/chart/post
  deps: list[str]
  agent_role: str
  input_payload: dict
  timeout_s: int
  retry_policy: dict
  checkpoint_key: str

RequirementSpec:
  objective: str
  focus_topics: list[str]
  writing_style: str
  granularity: str
  budget: dict
  user_path: list[str]
  constraints: dict

RunMetrics:
  run_id: str
  token_in: int
  token_out: int
  api_calls: int
  tool_calls: int
  retries: int
  errors: int
  latency_ms: int
  estimated_cost: float
```

---

## 6. 风险与缓解

1. **重构风险（影响主流程稳定性）**  
   - 缓解：先保留兼容模式；新老路径双跑灰度一段时间。  
2. **并发引入状态竞争**  
   - 缓解：节点状态机 + 原子 checkpoint + 幂等节点设计。  
3. **需求理解偏差导致构图错误**  
   - 缓解：运行前展示 plan 供用户确认；提供快速修正入口。  
4. **插件化后配置复杂度上升**  
   - 缓解：提供 pack schema 校验器 + 最小示例模板。

---

## 7. 测试与验收计划

### 自动化测试
- 单测：pipeline models/planner/scheduler/runtime、pack loader、requirement parser。
- 集成测试：从需求 -> 构图 -> 执行 -> 报告产出全链路。
- 回归测试：对比旧流程质量与性能。

### 质量门禁（建议）
- 主流程测试通过率 100%。
- 新增模块覆盖率 >= 70%。
- 关键路径无 P0/P1 缺陷。

---

## 8. 最终里程碑验收清单

- [ ] Stage 0：每次运行可输出完整指标与成本画像。  
- [ ] Stage 1：任务图调度稳定上线，性能显著提升。  
- [ ] Stage 2：可通过 Pack + 自然语言需求扩展新报告类型。  
- [ ] Stage 3：Demo 具备人机协同干预能力并可回放。

---

## 9. 下一步建议（立刻可做）

1. 先完成 Stage 0 的事件模型和埋点接口（不改业务行为）。  
2. 同时搭建 `src/pipeline/` 的最小 TaskGraph 骨架（先兼容现有三阶段）。  
3. 用一个最小 pack（如 `general_company_light`）验证插件加载链路。

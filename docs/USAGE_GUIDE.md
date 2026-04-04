# FinSight v2.0 使用指南

本文档介绍 FinSight v2.0 重构后的系统架构、使用方法、配置说明和自定义模板流程。

---

## 目录

- [快速开始](#快速开始)
- [系统架构概览](#系统架构概览)
- [配置文件详解](#配置文件详解)
- [报告类型与 Plugin](#报告类型与-plugin)
- [运行报告](#运行报告)
- [Lite 模式（快速低成本测试）](#lite-模式快速低成本测试)
- [自定义模板](#自定义模板)
- [自定义 Prompt](#自定义-prompt)
- [新增报告类型](#新增报告类型)
- [检查点与断点续跑](#检查点与断点续跑)
- [Web Demo](#web-demo)
- [调试技巧](#调试技巧)

---

## 快速开始

### 1. 安装依赖

推荐使用 [uv](https://docs.astral.sh/uv/) 管理项目依赖（更快、更可靠）：

```bash
# 安装 uv（如未安装）
pip install uv

# 安装项目及全部依赖
uv sync

# 安装可选依赖组
uv sync --extra chinese    # 中国A股金融数据（akshare 等）
uv sync --extra web        # Web 爬虫（crawl4ai, playwright）
uv sync --extra demo       # Web Demo（FastAPI, uvicorn）

# 安装全部可选依赖
uv sync --all-extras
```

也兼容传统 pip：

```bash
pip install -e .
pip install -e ".[chinese,web,demo]"  # 含可选依赖
```

### 2. 配置环境变量

在项目根目录创建 `.env` 文件：

```env
# LLM 配置（必须）
DS_MODEL_NAME=deepseek-chat
DS_API_KEY=your-api-key
DS_BASE_URL=https://api.deepseek.com/v1

# 嵌入模型（必须）
EMBEDDING_MODEL_NAME=qwen/qwen3-embedding-0.6b
EMBEDDING_API_KEY=your-api-key
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1

# 视觉语言模型（图表生成时必须）
VLM_MODEL_NAME=qwen/qwen3-vl-235b-a22b-instruct
VLM_API_KEY=your-api-key
VLM_BASE_URL=https://api.siliconflow.cn/v1
```

### 3. 编写配置文件

创建 `my_config.yaml`：

```yaml
target_name: "商汤科技"
stock_code: "00020.hk"
target_type: "financial_company"
output_dir: "./outputs/my-report"
language: "zh"

llm_config_list:
  - model_name: "${DS_MODEL_NAME}"
    api_key: "${DS_API_KEY}"
    base_url: "${DS_BASE_URL}"
    generation_params:
      temperature: 0.7
      max_tokens: 32768
  - model_name: "${EMBEDDING_MODEL_NAME}"
    api_key: "${EMBEDDING_API_KEY}"
    base_url: "${EMBEDDING_BASE_URL}"
  - model_name: "${VLM_MODEL_NAME}"
    api_key: "${VLM_API_KEY}"
    base_url: "${VLM_BASE_URL}"
```

### 4. 运行

```bash
python run_report.py --config my_config.yaml
```

产物位于 `outputs/my-report/商汤科技/`，包括 `.md`、`.docx`、`.pdf` 格式。

---

## 系统架构概览

```
用户 YAML 配置
    │
    ▼
 Pipeline.run()
    ├── generate_tasks()            ← LLM 规划 + 自定义任务
    ├── Plugin.build_task_graph()   ← 构建 DAG（软/硬依赖）
    └── _execute_graph()            ← 并发调度 + 检查点保存
            │
            ├── DataCollector × N     → ctx.put("collected_data", ...)
            │       │ (软依赖, min=1)
            ├── DataAnalyzer  × M     → ctx.put("analysis_results", ...)
            │       │ (软依赖, min=1)
            └── ReportGenerator × 1   → 输出 Markdown → DOCX → PDF
```

### 核心组件

| 组件 | 位置 | 职责 |
|------|------|------|
| **Pipeline** | `src/core/pipeline.py` | DAG 调度器，控制并发、重试、事件回调 |
| **TaskContext** | `src/core/task_context.py` | 线程安全的数据总线，Agent 间通过 `put/get` 传递数据 |
| **TaskGraph** | `src/core/task_graph.py` | DAG 引擎，支持硬依赖/软依赖 + `min_soft_deps` |
| **Plugin** | `src/plugins/*/plugin.py` | 报告类型配置：工具类别、后处理选项、DAG 拓扑 |
| **CheckpointManager** | `src/core/checkpoint.py` | Pipeline → JSON + Agent → dill 检查点 |
| **PromptLoader** | `src/utils/prompt_loader.py` | 分层 Prompt 加载：Plugin → `_base/` → Agent |

### Agent 执行流程

1. **DataCollector**：调用金融 API、Web 搜索等工具收集原始数据
2. **DataAnalyzer**：分析数据 → 生成分析报告小节 → 绘制图表（VLM 评审循环）
3. **ReportGenerator**：生成大纲 → 逐章撰写 → 替换图表 → 添加摘要/封面/引用 → 渲染

---

## 配置文件详解

### 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `target_name` | string | 研究对象名称，如 `"商汤科技"` `"中国AI行业研究"` |
| `target_type` | string | 报告类型，见 [报告类型](#报告类型与-plugin) |
| `llm_config_list` | list | LLM 配置列表（至少需要一个文本 LLM） |

### 可选字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `stock_code` | `""` | 股票代码，如 `"00020.hk"`、`"600519"` |
| `market` | `"A"` | 市场：`A`（A 股）、`HK`（港股）、`US`（美股） |
| `output_dir` | `"./outputs"` | 输出目录 |
| `language` | `"zh"` | 报告语言：`zh`（中文）、`en`（英文） |
| `custom_collect_tasks` | `[]` | 自定义数据收集任务 |
| `custom_analysis_tasks` | `[]` | 自定义分析任务 |
| `reference_doc_path` | *自动* | Word 模板路径（覆盖 Plugin 默认值） |
| `outline_template_path` | *自动* | 大纲模板路径（覆盖 Plugin 默认值） |
| `save_note` | `null` | 输出子目录前缀 |

### 环境变量替换

配置文件中 `${VAR_NAME}` 会自动替换为对应环境变量。在 `.env` 文件或系统环境中设置 API 密钥，避免硬编码敏感信息。

### LLM 配置

至少需配置三个模型：

```yaml
llm_config_list:
  # 1. 文本 LLM（必须）— 用于任务规划、分析、写作
  - model_name: "${DS_MODEL_NAME}"
    api_key: "${DS_API_KEY}"
    base_url: "${DS_BASE_URL}"
    generation_params:
      temperature: 0.7      # 创造性（0.0-2.0）
      max_tokens: 32768      # 最大输出长度
      top_p: 0.95

  # 2. Embedding 模型（必须）— 用于数据选择和引用匹配
  - model_name: "${EMBEDDING_MODEL_NAME}"
    api_key: "${EMBEDDING_API_KEY}"
    base_url: "${EMBEDDING_BASE_URL}"

  # 3. 视觉语言模型（图表评审使用，无图表报告可不配）
  - model_name: "${VLM_MODEL_NAME}"
    api_key: "${VLM_API_KEY}"
    base_url: "${VLM_BASE_URL}"
```

### 速率限制

```yaml
rate_limits:
  search_engines: 1.0    # 搜索引擎调用间隔（秒）
  financial_apis: 0.5    # 金融 API 调用间隔
  fred_api: 0.5          # FRED 数据 API 间隔
  yfinance: 0.2          # yfinance 调用间隔
```

---

## 报告类型与 Plugin

系统通过 Plugin 机制支持多种报告类型。`target_type` 决定加载哪个 Plugin：

| `target_type` | 说明 | 工具类别 | 图表 | 封面页 |
|----------------|------|----------|------|--------|
| `financial_company` | 个股深度研报 | 金融+宏观+行业+Web | ✅ | ✅ |
| `financial_industry` | 行业研究报告 | 金融+宏观+行业+Web | ✅ | ❌ |
| `financial_macro` | 宏观经济分析 | 宏观+Web | ✅ | ❌ |
| `general` | 通用研究报告 | 仅 Web 搜索 | ❌ | ❌ |
| `governance` | 公司治理报告 | 仅 Web 搜索 | ❌ | ❌ |

> 兼容旧名称：`company` → `financial_company`、`macro` → `financial_macro`、`industry` → `financial_industry`

### 自定义任务

通过 `custom_collect_tasks` 和 `custom_analysis_tasks` 指定任务列表。LLM 会在此基础上自动补充任务。

#### 个股研报配置示例

```yaml
target_name: "商汤科技"
stock_code: "00020.hk"
target_type: "financial_company"
language: "zh"

custom_collect_tasks:
  - "资产负债表, 利润表, 现金流量表三大财务报表"
  - "股票基本信息以及股价数据"
  - "股东结构"
  - "投资评级"

custom_analysis_tasks:
  - "梳理公司发展历程、关键里程碑事件"
  - "分析历年营收趋势、各业务板块占比变化"
  - "进行同行业竞争对手对比分析"
```

#### 行业研究配置示例

```yaml
target_name: "中国金融智能体发展研究"
stock_code: ""
target_type: "financial_industry"
language: "zh"

custom_collect_tasks:
  - "行业相关政策文件与指导意见"
  - "行业市场规模及未来增长预测数据"

custom_analysis_tasks:
  - "梳理行业发展历程与技术演进路径"
  - "构建行业竞争格局图谱"
```

#### 通用研究配置示例

```yaml
target_name: "大语言模型在教育领域的应用"
stock_code: ""
target_type: "general"
language: "en"

custom_analysis_tasks:
  - "Survey current applications of LLMs in education"
  - "Analyze effectiveness and limitations"
```

---

## 运行报告

### CLI 命令

```bash
# 基本用法
python run_report.py --config my_config.yaml

# Dry-Run 模式 — 打印 DAG 拓扑，不执行
python run_report.py --config my_config.yaml --dry-run

# 从头开始，忽略已有检查点
python run_report.py --config my_config.yaml --no-resume

# 设置最大并发 Agent 数
python run_report.py --config my_config.yaml --max-concurrent 5
```

### 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--config` | `my_config.yaml` | 配置文件路径 |
| `--dry-run` | `false` | 仅打印 DAG，不执行 |
| `--no-resume` | `false` | 忽略检查点，全部重新执行 |
| `--max-concurrent` | `3` | 最大并发 Agent 数量 |
| `--lite` | `false` | Lite 模式：少任务、少迭代、无图表，快速低成本 |

### 输出结构

```
outputs/<target_name>/
├── config.json           # 运行时配置快照
├── checkpoints/          # 检查点文件
│   ├── pipeline.json     # Pipeline DAG 状态
│   └── agents/           # 各 Agent 检查点（dill）
├── agent_working/        # 各 Agent 工作目录
│   ├── collect_0/        #   DataCollector 工作区
│   ├── analyze_0/        #   DataAnalyzer 工作区（含 images/）
│   └── report/           #   ReportGenerator 工作区
├── logs/                 # 运行日志
├── <报告标题>.md         # Markdown 报告
├── <报告标题>.docx       # Word 报告
└── <报告标题>.pdf        # PDF 报告
```

---

## Lite 模式（快速低成本测试）

完整生成一份研报通常需要大量 LLM 调用（多个 collector × 20轮 + 多个 analyzer × 20轮 + report × 20轮），耗时长、成本高。**Lite 模式** 专门为快速验证和开发调试设计：

### 与正常模式的对比

| 维度 | 正常模式 | Lite 模式 |
|------|----------|-----------|
| 数据收集任务 | 全部（custom + LLM 生成） | 最多 2 个 |
| 分析任务 | 全部 | 最多 1 个 |
| 每个 Agent 最大迭代轮数 | 20 | 5 |
| 图表生成 | ✅（需 VLM） | ❌（跳过，省 VLM 成本） |
| 封面页 | 按 Plugin 配置 | ❌（跳过，省 API 调用） |
| 预计耗时 | 30-60 分钟 | 5-10 分钟 |
| 预计 Token 消耗 | ~100 万 | ~10-15 万 |

### 使用方法

```bash
# 方式一：用已有的完整配置 + --lite 标志
python run_report.py --config my_config.yaml --lite

# 方式二：用精简配置 + --lite 标志（最省成本）
python run_report.py --config docs/example_configs/lite_test.yaml --lite

# 可以先 dry-run 查看 DAG
python run_report.py --config my_config.yaml --lite --dry-run
```

### 精简配置示例

```yaml
# docs/example_configs/lite_test.yaml
target_name: "商汤科技"
stock_code: "00020.hk"
target_type: "financial_company"
output_dir: "./outputs/lite-test"
language: "zh"

# 只定义 1-2 个最核心的任务
custom_collect_tasks:
  - "公司基本信息和主营业务"
  - "最近一年股价数据"

custom_analysis_tasks:
  - "分析公司核心业务和行业地位"

llm_config_list:
  - model_name: "${DS_MODEL_NAME}"
    api_key: "${DS_API_KEY}"
    base_url: "${DS_BASE_URL}"
    generation_params:
      temperature: 0.5
      max_tokens: 8192
  - model_name: "${EMBEDDING_MODEL_NAME}"
    api_key: "${EMBEDDING_API_KEY}"
    base_url: "${EMBEDDING_BASE_URL}"
  - model_name: "${VLM_MODEL_NAME}"
    api_key: "${VLM_API_KEY}"
    base_url: "${VLM_BASE_URL}"
```

### 省钱技巧

1. **`--lite` + 精简 config**：只写 1-2 个 `custom_collect_tasks` + 1 个 `custom_analysis_tasks`，`--lite` 会确保不超过 2+1
2. **用 `general` 类型**：`target_type: "general"` 只用 Web 搜索，不调金融 API，且默认无图表
3. **先 dry-run**：`--dry-run` 免费查看 DAG，确认任务数符合预期
4. **利用检查点**：中断后再次运行，已完成的任务不会重复执行
5. **降低 `max_tokens`**：在 `generation_params` 中设 `max_tokens: 8192` 减少输出

---

## 自定义模板

### Word 样式模板 (`reference_doc_path`)

控制生成 Word 文档的排版样式（字体、间距、标题样式等）。

**使用方法**：

1. 复制 Plugin 内置模板作为基础：
   ```
   src/plugins/financial_company/templates/report_template.docx
   ```
2. 在 Word 中修改样式（标题 1-3、正文、表格等）
3. 将模板放到项目任意位置
4. 在配置中指定路径：
   ```yaml
   reference_doc_path: 'my_templates/custom_style.docx'
   ```

> 如不指定，系统自动使用对应 Plugin 目录下的模板。

### 大纲模板 (`outline_template_path`)

控制报告大纲的结构（章节标题、内容要求）。LLM 会参考此模板生成大纲。

**使用方法**：

1. 查看现有模板了解格式：
   ```
   src/plugins/financial_company/templates/outline_zh.md
   ```
2. 创建自定义大纲模板（Markdown 格式）：
   ```markdown
   # {target_name} 研究报告

   ## 1. 公司概况
   - 公司基本信息
   - 主营业务

   ## 2. 财务分析
   - 营收分析
   - 盈利能力

   ## 3. 行业分析
   - 行业地位
   - 竞争格局

   ## 4. 投资建议
   - 估值分析
   - 风险提示
   ```
3. 在配置中指定：
   ```yaml
   outline_template_path: 'my_templates/custom_outline.md'
   ```

> 如不指定，系统自动使用对应 Plugin 目录下的大纲模板（如有）。若 Plugin 也无大纲模板，则完全由 LLM 自主生成。

---

## 自定义 Prompt

系统采用三层 Prompt 加载机制：

```
Plugin prompts/  →  _base/ prompts  →  Agent prompts/（旧版兼容）
    (高优先级)        (共享基础)           (最低优先级)
```

### Prompt 文件位置

| 层级 | 路径 | 说明 |
|------|------|------|
| Plugin | `src/plugins/<type>/prompts/<agent>.yaml` | 报告类型特有的 Prompt |
| Base | `src/prompts/_base/*.yaml` | 所有类型共享的通用 Prompt |
| Agent | `src/agents/<agent>/prompts/*.yaml` | 旧版兼容，最低优先级 |

### 修改 Prompt

**修改某个报告类型独有的 Prompt**（如 `financial_company` 的分析提示词）：

编辑 `src/plugins/financial_company/prompts/data_analyzer.yaml`

**修改所有报告类型共享的 Prompt**（如数据选择、图表评审）：

编辑 `src/prompts/_base/` 下的对应 YAML 文件

### 常用 Prompt Key

| Key | 用途 | 位置 |
|-----|------|------|
| `data_analysis` | DataAnalyzer 主提示词 | Plugin |
| `section_writing` | 章节撰写提示词 | Plugin |
| `outline_draft` | 大纲生成提示词 | Plugin |
| `generate_collect_task` | 数据收集任务规划 | Plugin (`memory.yaml`) |
| `generate_task` | 分析任务规划 | Plugin (`memory.yaml`) |
| `select_data` | 数据选择 | `_base/` |
| `select_analysis` | 分析结果选择 | `_base/` |
| `vlm_critique` | 图表 VLM 评审 | `_base/` |
| `table_beautify` | 表格美化 | `_base/` |

---

## 新增报告类型

只需 3 步即可新增一种报告类型（如 `esg`）：

### 1. 创建 Plugin 目录

```
src/plugins/esg/
├── __init__.py          # 空文件
├── plugin.py            # Plugin 类定义（~20 行）
├── prompts/             # 类型特有 Prompt
│   ├── data_analyzer.yaml
│   ├── memory.yaml
│   └── report_generator.yaml
└── templates/           # 模板文件
    ├── report_template.docx
    └── outline_zh.md
```

### 2. 编写 `plugin.py`

```python
from src.plugins import register_plugin
from src.plugins.base_plugin import PostProcessFlags, ReportPlugin


@register_plugin("esg")
class ESGPlugin(ReportPlugin):
    name = "esg"

    def get_tool_categories(self) -> list[str]:
        return ["financial", "web"]  # 根据需要选择工具类别

    def get_post_process_flags(self) -> PostProcessFlags:
        return PostProcessFlags(
            add_introduction=True,
            add_cover_page=False,
            add_references=True,
            enable_chart=True,
        )

    def get_prompt_defaults(self) -> dict[str, str]:
        return {"analyst_role": "ESG-research", "domain": "sustainability"}
```

### 3. 在配置中使用

```yaml
target_type: "esg"
```

> 可选：如需添加到 Pydantic 校验白名单，在 `src/config/config.py` 的 `_VALID_TARGET_TYPES` 中加入 `"esg"`。

### 工具类别参考

| 类别 | 包含工具 |
|------|----------|
| `financial` | 个股财务数据、K线、财报 |
| `macro` | 宏观经济指标（如 FRED） |
| `industry` | 行业数据 |
| `web` | Web 搜索 + 网页抓取 |

---

## 检查点与断点续跑

系统在执行过程中自动保存检查点，支持中断后从上次位置恢复。

### 自动恢复

默认行为：如果检测到 `checkpoints/pipeline.json`，自动从上次中断点恢复。

```bash
# 正常运行（自动恢复）
python run_report.py --config my_config.yaml

# 强制从头开始
python run_report.py --config my_config.yaml --no-resume
```

### 审查检查点

Pipeline 状态保存为 JSON，直接可读：

```bash
# 查看 DAG 状态
cat outputs/商汤科技/checkpoints/pipeline.json | python -m json.tool
```

输出示例：

```json
{
  "version": 2,
  "graph": {
    "collect_0": { "state": "done" },
    "collect_1": { "state": "done" },
    "analyze_0": { "state": "done" },
    "analyze_1": { "state": "failed", "error": "..." },
    "report": { "state": "pending" }
  }
}
```

### 恢复粒度

- **Pipeline 级别**：已完成的 Agent 不会重新执行
- **Agent 级别**：每个 Agent 的 phase（阶段）有独立检查点，中断后从上次完成的 phase 恢复
- **ReportGenerator**：按章节保存进度，中断后续写未完成的章节

---

## Web Demo

### 启动后端

```bash
cd demo/backend
python app.py
```

服务启动在 `http://localhost:8000`。

### 启动前端

```bash
cd demo/frontend
npm install
npm run dev
```

前端访问 `http://localhost:5173`。

### 功能

- 可视化配置编辑
- 实时日志流（WebSocket）
- 任务管理（保存/加载任务集）
- 报告预览和下载
- 断点续跑

---

## 调试技巧

### Dry-Run 模式

打印完整 DAG 拓扑，不执行任何 Agent：

```bash
python run_report.py --config my_config.yaml --dry-run
```

### 单 Agent 调试

从已有的检查点加载数据，单独运行某个 Agent：

```python
import asyncio
from src.config import Config
from src.core.task_context import TaskContext
from src.agents import DataAnalyzer

config = Config(config_file_path="my_config.yaml")
ctx = TaskContext.from_config(config)

# 从检查点加载已收集的数据
ctx.load_artifacts_from("outputs/商汤科技/checkpoints/pipeline.json")

analyzer = DataAnalyzer(config=config, task_context=ctx)
result = asyncio.run(analyzer.async_run(
    input_data={
        "task": "Research target: 商汤科技",
        "analysis_task": "分析营收结构"
    }
))
```

### 日志

运行日志保存在 `outputs/<target>/logs/` 目录下，包含每个 Agent 的详细执行记录。

DAG 状态变化会自动记录：

```
INFO DAG state: {collect_0: done, collect_1: running, analyze_0: pending, report: pending}
```

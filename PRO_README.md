# 玉兰融观 Pro（FinSight Pro）

基于 RUC-NLPIR/FinSight 的实质性改造版本，重点解决公司研报“外观完整但事实、预测、估值和结论未封口”的问题。原项目及本修改版均按 GPLv3 提供；上游 README 中“Apache 2.0”的表述与仓库 LICENSE 文件不一致，本版以 LICENSE 为准。

## 改造内容

### 1. 投资研究写作约束

公司研报提示词新增：

- 归母净利润与合并净利润不得混用；跨 IPO、增发等资本变动时不得直接沿用历史 PB。
- 历史披露、数据库观测、分析师预测分层；预测必须有驱动因素和正常化年份。
- 目标价必须能由预测、估值倍数、摊薄股本和单位复算；基本面增长不能替代估值判断。
- 数据缺失时收窄结论；论坛和社交媒体不能作为财务事实来源；示意图不能冒充实证图。

### 2. 全局统稿

分段写作完成后、图表与参考文献处理前，新增一次整篇终审，统一评级、基准日、预测、目标价、单位和风险结论。若模型返回异常短稿，系统保留原稿，后续由硬验收拦截问题。

### 3. 确定性导出闸门

`src/utils/pro_quality.py` 不使用 LLM，导出 DOCX 前检查：

- 数据待接入、待补图、未替换引用、TODO 等占位痕迹；
- 数字引文是否越界，是否存在参考文献表；
- 图片链接是否指向存在的本地文件；
- “买入/增持”与“不评级”、数值目标价与“不设目标价”是否冲突；
- 同段明确写出的“收入 × PS = 市值”能否复算；
- 公司投资研报是否包含预测、估值和风险内容。

失败时阻止 DOCX 导出，错误写入输出目录的 `pro_quality_audit.json`。警告不阻断交付，供人工复核。该闸门不能替代金融专业判断，也不能证明所有引文语义都正确。

## 使用

配置和运行方式与上游一致：

```bash
pip install -r requirements.txt
cp .env.example .env
# 填写模型与检索 API 配置
python run_report.py
```

`my_config.yaml` 新增：

```yaml
pro_global_edit: true
pro_quality_gate: true
```

对已有 Markdown 研报单独验收：

```bash
python src/utils/pro_quality.py report.md --investment-case
```

通过返回 0，失败返回 1；默认生成 `report.pro_audit.json`。

## 验证范围

新增 `tests/test_pro_quality.py`，覆盖有效报告通过、引文越界、评级和目标价矛盾、估值算术错误、缺失图片和 JSON 审计输出。上述单元测试已通过。由于本交付环境没有上游所需的完整模型密钥和数据服务配置，未执行“从股票代码到最终 DOCX”的端到端生成，也未声称 Pro 已达到特定投资评级质量。

## 主要修改文件

- `src/agents/report_generator/report_generator.py`
- `src/agents/report_generator/prompts/financial_company_prompts.yaml`
- `src/utils/pro_quality.py`
- `my_config.yaml`
- `tests/test_pro_quality.py`

本项目不会自动向原仓库提交代码或创建 PR。

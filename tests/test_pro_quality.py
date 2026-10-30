from pathlib import Path

import importlib.util
import sys
MODULE_PATH = Path(__file__).parents[1] / "src" / "utils" / "pro_quality.py"
SPEC = importlib.util.spec_from_file_location("pro_quality", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
audit_markdown, write_audit = MODULE.audit_markdown, MODULE.write_audit


VALID = """# 某公司研究报告

## 投资结论

评级：中性。基准目标价 45.96 元，预测与估值采用正常化年份，不以峰值利润直接定价。

## 财务分析

2025A 收入 100 亿元。[1] 2026E 收入预计 120 亿元，归母净利润预计 20 亿元。

![真实图](chart.png)

图中显示收入增长，但需结合毛利率判断质量。

## 估值与风险

2027E 收入 100 亿元 × 8 倍 PS = 800 亿元，对应目标市值。主要风险是需求和价格回落。

## 参考文献

1. 公司 2025 年年度报告
"""


def test_valid_report_passes(tmp_path: Path):
    (tmp_path / "chart.png").write_bytes(b"real-image")
    issues = audit_markdown(VALID * 6, tmp_path, require_investment_case=True)
    assert [i for i in issues if i.severity == "error"] == []


def test_original_style_failures_are_blocked(tmp_path: Path):
    bad = (VALID * 6).replace("1. 公司 2025 年年度报告", "1. 公司公告")
    bad = bad.replace("[1]", "[82]").replace("评级：中性", "评级：买入", 1)
    bad += "\n\n数据待接入，回归关系显示显著上行。不给评级，不设目标价。\n"
    bad += "\n2027E 收入 3000 亿元 × 11.9 倍 PS = 32800 亿元。\n"
    codes = {i.code for i in audit_markdown(bad, tmp_path)}
    assert "CITATION_OUT_OF_RANGE" in codes
    assert "PENDING_DATA" in codes
    assert "RATING_CONTRADICTION" in codes
    assert "TARGET_PRICE_CONTRADICTION" in codes
    assert "VALUATION_ARITHMETIC" in codes


def test_missing_image_is_blocked(tmp_path: Path):
    codes = {i.code for i in audit_markdown(VALID, tmp_path)}
    assert "FIGURE_MISSING_FILE" in codes


def test_audit_file_is_machine_readable(tmp_path: Path):
    payload = write_audit([], tmp_path / "audit.json", "report.md")
    assert payload["passed"] is True
    assert (tmp_path / "audit.json").exists()

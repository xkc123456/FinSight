# New file for FinSight Pro, 2026-10-30; distributed under GNU GPL v3.
"""FinSight Pro: deterministic pre-export quality gates.

This module deliberately does not use an LLM. A model may judge prose, but it
must not be the only thing standing between a broken draft and a delivered
report. The checks here are conservative, reproducible and return diagnostics
that can be fixed by a writer or a human analyst.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import math
import re
from typing import Iterable


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str


PLACEHOLDER_PATTERNS = {
    "PENDING_DATA": r"数据待接入|待补充数据|数据待补|placeholder\s+data|data\s+pending",
    "PENDING_CITATION": r"\[(?:source|来源)\s*[：:]|\[待引| citation needed",
    "PENDING_IMAGE": r"@import\s+|图片待|图表待|\[chart not available",
    "TEMPLATE_REMAINDER": r"这里填写|请输入|Lorem ipsum|TODO\b|TBD\b",
}

FIGURE_CLAIM = re.compile(r"从图(?:中|表)?(?:可以|可)看|如图所示|回归关系显示|图中显示")
RATING_POSITIVE = re.compile(r"(?:投资评级|评级)[:：\s]*(?:买入|增持|强烈推荐)|(?:给予|维持)(?:买入|增持)评级")
RATING_NONE = re.compile(r"不(?:给|作|设置?|提供)(?:投资)?评级|不设投资评级|无投资评级")
TARGET_NONE = re.compile(r"不设(?:置)?目标价|不提供目标价|无目标价")


def _line_no(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _paragraphs(text: str) -> list[str]:
    return re.split(r"\n\s*\n", text)


def _reference_count(text: str) -> int:
    matches = list(re.finditer(r"(?im)^#{1,3}\s*(?:references?|reference data sources|参考文献|参考资料|数据来源)\s*$", text))
    if not matches:
        return 0
    tail = text[matches[-1].end():]
    return len(re.findall(r"(?m)^\s*\d+[.)、]\s+\S", tail))


def _citation_issues(text: str) -> list[Issue]:
    issues: list[Issue] = []
    ref_count = _reference_count(text)
    body = text
    ref_heading = list(re.finditer(r"(?im)^#{1,3}\s*(?:references?|reference data sources|参考文献|参考资料|数据来源)\s*$", text))
    if ref_heading:
        body = text[:ref_heading[-1].start()]
    cited: set[int] = set()
    for group in re.findall(r"(?<!\!)\[(\d+(?:\s*[,，、-]\s*\d+)*)\]", body):
        for token in re.split(r"[,，、]", group):
            token = token.strip()
            if token.isdigit():
                cited.add(int(token))
    if cited and ref_count == 0:
        issues.append(Issue("CITATION_NO_BIBLIOGRAPHY", "error", "正文含数字引文，但未找到编号参考文献表。"))
    elif cited:
        missing = sorted(n for n in cited if n < 1 or n > ref_count)
        if missing:
            issues.append(Issue("CITATION_OUT_OF_RANGE", "error", f"引文编号越界：{missing}；文后仅识别到 {ref_count} 条。"))
        unused = sorted(set(range(1, ref_count + 1)) - cited)
        if unused:
            issues.append(Issue("BIBLIOGRAPHY_UNUSED", "warning", f"文后条目未在正文引用：{unused[:30]}。"))
    return issues


def _figure_issues(text: str, base_dir: str | Path | None = None) -> list[Issue]:
    issues: list[Issue] = []
    image_re = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    paths = image_re.findall(text)
    if not paths and FIGURE_CLAIM.search(text):
        issues.append(Issue("FIGURE_CLAIM_WITHOUT_FIGURE", "error", "正文声称图中展示了关系，但全文没有有效图片链接。"))
    for match in image_re.finditer(text):
        raw = match.group(1).strip().strip("<>")
        if re.match(r"https?://", raw):
            continue
        if base_dir is not None:
            path = Path(raw)
            if not path.is_absolute():
                path = Path(base_dir) / path
            if not path.exists() or path.stat().st_size == 0:
                issues.append(Issue("FIGURE_MISSING_FILE", "error", f"图片文件不存在或为空：{raw}（第 {_line_no(text, match.start())} 行）。"))
    for para in _paragraphs(text):
        if FIGURE_CLAIM.search(para) and not image_re.search(para):
            issues.append(Issue("FIGURE_NARRATIVE_UNLINKED", "warning", f"图文论断未与图片置于同段，请人工核验：{para[:100]}"))
    return issues


def _rating_issues(text: str) -> list[Issue]:
    issues: list[Issue] = []
    if RATING_POSITIVE.search(text) and RATING_NONE.search(text):
        issues.append(Issue("RATING_CONTRADICTION", "error", "报告同时给出正面投资评级和“不给评级”的结论。"))
    if TARGET_NONE.search(text) and re.search(r"目标价[:：\s]*(?:人民币|RMB|HKD|\$)?\s*\d", text, re.I):
        issues.append(Issue("TARGET_PRICE_CONTRADICTION", "error", "报告同时给出数值目标价和“不设目标价”的结论。"))
    return issues


def _valuation_issues(text: str) -> list[Issue]:
    """Recalculate explicit revenue × PS = market cap statements."""
    issues: list[Issue] = []
    patterns = [
        re.compile(r"(?P<revenue>\d+(?:\.\d+)?)\s*(?P<unit>亿元|亿)[^\n。；]{0,60}?(?:×|x|X|\*)\s*(?P<multiple>\d+(?:\.\d+)?)\s*倍\s*P/?S[^\n。；]{0,60}?(?:=|＝|对应|得到|市值(?:为|约)?)\s*(?P<cap>\d+(?:\.\d+)?)\s*(?P=unit)", re.I),
        re.compile(r"(?P<multiple>\d+(?:\.\d+)?)\s*倍\s*P/?S[^\n。；]{0,60}?(?:×|x|X|\*)\s*(?P<revenue>\d+(?:\.\d+)?)\s*(?P<unit>亿元|亿)[^\n。；]{0,60}?(?:=|＝|对应|得到|市值(?:为|约)?)\s*(?P<cap>\d+(?:\.\d+)?)\s*(?P=unit)", re.I),
    ]
    seen: set[tuple[float, float, float]] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            revenue = float(match.group("revenue"))
            multiple = float(match.group("multiple"))
            cap = float(match.group("cap"))
            key = (revenue, multiple, cap)
            if key in seen:
                continue
            seen.add(key)
            expected = revenue * multiple
            tolerance = max(0.02 * expected, 1.0)
            if not math.isclose(expected, cap, abs_tol=tolerance):
                issues.append(Issue("VALUATION_ARITHMETIC", "error", f"估值等式不成立：{revenue:g} × {multiple:g} = {expected:g}，正文写 {cap:g}（第 {_line_no(text, match.start())} 行）。"))
    return issues


def audit_markdown(text: str, base_dir: str | Path | None = None, require_investment_case: bool = False) -> list[Issue]:
    issues: list[Issue] = []
    if len(text.strip()) < 1500:
        issues.append(Issue("REPORT_TOO_SHORT", "error", "正文过短，不构成完整研究报告。"))
    for code, pattern in PLACEHOLDER_PATTERNS.items():
        match = re.search(pattern, text, re.I)
        if match:
            issues.append(Issue(code, "error", f"发现未完成标记“{match.group(0)}”（第 {_line_no(text, match.start())} 行）。"))
    issues.extend(_citation_issues(text))
    issues.extend(_figure_issues(text, base_dir))
    issues.extend(_rating_issues(text))
    issues.extend(_valuation_issues(text))
    if require_investment_case:
        if not re.search(r"20\d{2}\s*[EeＦF]|预测|预计", text):
            issues.append(Issue("FORECAST_MISSING", "error", "公司投资研报缺少明确预测年份或预测说明。"))
        if not re.search(r"估值|valuation|target price|目标价", text, re.I):
            issues.append(Issue("VALUATION_MISSING", "error", "公司投资研报缺少估值章节。"))
        if not re.search(r"风险|risk", text, re.I):
            issues.append(Issue("RISK_SECTION_MISSING", "error", "公司投资研报缺少风险分析。"))
    return issues


def write_audit(issues: Iterable[Issue], output_path: str | Path, report_path: str | Path) -> dict:
    items = list(issues)
    errors = sum(i.severity == "error" for i in items)
    warnings = sum(i.severity == "warning" for i in items)
    payload = {"passed": errors == 0, "errors": errors, "warnings": warnings,
               "report": str(report_path), "issues": [asdict(i) for i in items]}
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="FinSight Pro deterministic report audit")
    parser.add_argument("report", help="Markdown report to audit")
    parser.add_argument("--audit", help="JSON output path; defaults beside the report")
    parser.add_argument("--investment-case", action="store_true", help="Require forecast, valuation and risk content")
    args = parser.parse_args()
    report = Path(args.report)
    issues = audit_markdown(report.read_text(encoding="utf-8"), report.parent, args.investment_case)
    output = args.audit or report.with_suffix(".pro_audit.json")
    payload = write_audit(issues, output, report)
    for issue in issues:
        print(f"{issue.severity.upper()} {issue.code}: {issue.message}")
    print(f"Pro audit: {'PASSED' if payload['passed'] else 'FAILED'}; details: {output}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

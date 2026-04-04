"""Plugin for industry-level financial research reports."""
from src.plugins import register_plugin
from src.plugins.base_plugin import PostProcessFlags, ReportPlugin


@register_plugin("financial_industry")
class FinancialIndustryPlugin(ReportPlugin):
    name = "financial_industry"

    def get_tool_categories(self) -> list[str]:
        return ["financial", "macro", "industry", "web"]

    def get_post_process_flags(self) -> PostProcessFlags:
        return PostProcessFlags(
            add_introduction=True,
            add_cover_page=True,
            add_references=True,
            enable_chart=True,
        )

    def get_prompt_defaults(self) -> dict[str, str]:
        return {"analyst_role": "financial-research", "domain": "financial"}

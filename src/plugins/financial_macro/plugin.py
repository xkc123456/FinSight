"""Plugin for macro-economic research reports."""
from src.plugins import register_plugin
from src.plugins.base_plugin import PostProcessFlags, ReportPlugin


@register_plugin("financial_macro")
class FinancialMacroPlugin(ReportPlugin):
    name = "financial_macro"

    def get_tool_categories(self) -> list[str]:
        # Macro reports don't need individual-stock APIs.
        return ["macro", "web"]

    def get_post_process_flags(self) -> PostProcessFlags:
        return PostProcessFlags(
            add_introduction=True,
            add_cover_page=False,
            add_references=True,
            enable_chart=True,
        )

    def get_prompt_defaults(self) -> dict[str, str]:
        return {"analyst_role": "financial-research", "domain": "financial"}

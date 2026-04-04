"""Plugin for corporate governance research reports."""
from src.plugins import register_plugin
from src.plugins.base_plugin import PostProcessFlags, ReportPlugin


@register_plugin("governance")
class GovernancePlugin(ReportPlugin):
    name = "governance"

    def get_tool_categories(self) -> list[str]:
        # Governance reports only need web search.
        return ["web"]

    def get_post_process_flags(self) -> PostProcessFlags:
        return PostProcessFlags(
            add_introduction=True,
            add_cover_page=False,
            add_references=True,
            enable_chart=False,
        )

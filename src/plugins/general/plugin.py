"""Plugin for general (non-financial) research reports."""
from src.plugins import register_plugin
from src.plugins.base_plugin import PostProcessFlags, ReportPlugin


@register_plugin("general")
class GeneralPlugin(ReportPlugin):
    name = "general"

    def get_tool_categories(self) -> list[str]:
        # General research relies on web search only.
        return ["web"]

    def get_post_process_flags(self) -> PostProcessFlags:
        return PostProcessFlags(
            add_introduction=True,
            add_cover_page=False,
            add_references=True,
            enable_chart=False,
        )

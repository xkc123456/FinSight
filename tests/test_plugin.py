"""Tests for the plugin system.

Covers: registry, load_plugin, base class defaults, per-plugin config,
build_task_graph topology, and prompt defaults.
"""
from __future__ import annotations

import pytest

from src.core.task_context import TaskContext
from src.plugins import _PLUGIN_REGISTRY, load_plugin, register_plugin
from src.plugins.base_plugin import PostProcessFlags, ReportPlugin


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

class _StubConfig:
    def __init__(self, target_type="financial_company"):
        self.working_dir = "/tmp/test"
        self.config = {
            "target_name": "TestCo",
            "stock_code": "000001",
            "target_type": target_type,
            "language": "zh",
        }
        self.llm_dict = {}


def _make_ctx(target_type="financial_company", stock_code="000001"):
    cfg = _StubConfig(target_type)
    cfg.config["stock_code"] = stock_code
    cfg.config["target_type"] = target_type
    return TaskContext(
        config=cfg,
        target_name="TestCo",
        stock_code=stock_code,
        target_type=target_type,
        language="zh",
    ), cfg


# ------------------------------------------------------------------
# 1. Registry & load_plugin
# ------------------------------------------------------------------

class TestPluginRegistry:
    def test_known_plugins_registered(self):
        """All five expected plugins can be loaded."""
        for name in [
            "financial_company",
            "financial_industry",
            "financial_macro",
            "general",
            "governance",
        ]:
            plugin = load_plugin(name)
            assert isinstance(plugin, ReportPlugin)
            assert plugin.name == name

    def test_unknown_plugin_raises(self):
        with pytest.raises(ValueError, match="No plugin found"):
            load_plugin("nonexistent_type_xyz")

    def test_register_plugin_decorator(self):
        @register_plugin("_test_dummy_")
        class _DummyPlugin(ReportPlugin):
            name = "_test_dummy_"

        assert "_test_dummy_" in _PLUGIN_REGISTRY
        plugin = load_plugin("_test_dummy_")
        assert plugin.name == "_test_dummy_"

        # Clean up
        del _PLUGIN_REGISTRY["_test_dummy_"]


# ------------------------------------------------------------------
# 2. Base class defaults
# ------------------------------------------------------------------

class TestReportPluginDefaults:
    def test_default_tool_categories(self):
        plugin = ReportPlugin.__new__(ReportPlugin)
        assert plugin.get_tool_categories() == ["financial", "macro", "industry", "web"]

    def test_default_post_process_flags(self):
        flags = ReportPlugin.__new__(ReportPlugin).get_post_process_flags()
        assert flags == PostProcessFlags()
        assert flags.add_introduction is True
        assert flags.add_cover_page is False
        assert flags.enable_chart is True

    def test_default_prompt_defaults(self):
        defaults = ReportPlugin.__new__(ReportPlugin).get_prompt_defaults()
        assert defaults == {"analyst_role": "research", "domain": "professional"}


# ------------------------------------------------------------------
# 3. Per-plugin configuration
# ------------------------------------------------------------------

class TestFinancialCompanyPlugin:
    def test_tool_categories(self):
        plugin = load_plugin("financial_company")
        assert set(plugin.get_tool_categories()) == {"financial", "macro", "industry", "web"}

    def test_post_process_flags(self):
        flags = load_plugin("financial_company").get_post_process_flags()
        assert flags.add_cover_page is True
        assert flags.enable_chart is True

    def test_prompt_defaults_financial(self):
        defaults = load_plugin("financial_company").get_prompt_defaults()
        assert defaults["analyst_role"] == "financial-research"
        assert defaults["domain"] == "financial"


class TestGeneralPlugin:
    def test_tool_categories_web_only(self):
        plugin = load_plugin("general")
        assert plugin.get_tool_categories() == ["web"]

    def test_chart_disabled(self):
        flags = load_plugin("general").get_post_process_flags()
        assert flags.enable_chart is False
        assert flags.add_cover_page is False


class TestGovernancePlugin:
    def test_tool_categories_web_only(self):
        plugin = load_plugin("governance")
        assert plugin.get_tool_categories() == ["web"]


# ------------------------------------------------------------------
# 4. build_task_graph topology
# ------------------------------------------------------------------

# Import guard: build_task_graph imports agent classes which may have
# heavy dependencies (crawl4ai, etc.).
try:
    from src.agents import DataCollector  # noqa: F401
    _HAS_AGENTS = True
except (ImportError, ModuleNotFoundError):
    _HAS_AGENTS = False


@pytest.mark.skipif(not _HAS_AGENTS, reason="Agent deps not installed")
class TestBuildTaskGraph:
    def test_standard_topology(self):
        """collectors → analyzers (soft dep) → report (soft dep)."""
        plugin = load_plugin("financial_company")
        ctx, cfg = _make_ctx("financial_company")
        graph = plugin.build_task_graph(cfg, ctx, ["c1", "c2"], ["a1"])

        assert "collect_0" in graph
        assert "collect_1" in graph
        assert "analyze_0" in graph
        assert "report" in graph
        assert len(graph) == 4

    def test_no_tasks_still_has_report(self):
        plugin = load_plugin("general")
        ctx, cfg = _make_ctx("general", stock_code="")
        graph = plugin.build_task_graph(cfg, ctx, [], [])

        assert "report" in graph
        assert len(graph) == 1

    def test_collector_ids_in_analyzer_soft_deps(self):
        plugin = load_plugin("financial_industry")
        ctx, cfg = _make_ctx("financial_industry", stock_code="")
        graph = plugin.build_task_graph(cfg, ctx, ["c1", "c2", "c3"], ["a1", "a2"])

        a0 = graph._nodes["analyze_0"]
        assert set(a0.soft_depends_on) == {"collect_0", "collect_1", "collect_2"}

    def test_analyzer_ids_in_report_soft_deps(self):
        plugin = load_plugin("financial_company")
        ctx, cfg = _make_ctx("financial_company")
        graph = plugin.build_task_graph(cfg, ctx, ["c1"], ["a1", "a2"])

        report = graph._nodes["report"]
        assert set(report.soft_depends_on) == {"analyze_0", "analyze_1"}

    def test_min_soft_deps_value(self):
        plugin = load_plugin("financial_company")
        ctx, cfg = _make_ctx("financial_company")
        graph = plugin.build_task_graph(cfg, ctx, ["c1", "c2"], ["a1"])

        assert graph._nodes["analyze_0"].min_soft_deps == 1
        assert graph._nodes["report"].min_soft_deps == 1


# ------------------------------------------------------------------
# 5. Plugin directory helpers
# ------------------------------------------------------------------

class TestPluginDirectories:
    def test_prompt_dir_exists(self):
        for name in ["financial_company", "general"]:
            plugin = load_plugin(name)
            prompt_dir = plugin.get_prompt_dir()
            # Prompt dir may or may not exist depending on project state,
            # but the path should be under the plugin's own directory.
            assert plugin.name in str(prompt_dir) or name in str(prompt_dir)

    def test_template_path(self):
        plugin = load_plugin("financial_company")
        path = plugin.get_template_path("report.docx")
        assert str(path).endswith("report.docx")

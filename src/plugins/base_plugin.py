"""Abstract base class for report-type plugins.

A plugin encapsulates the *differences* between report types:
* Which tool categories to load.
* Where to find prompts and templates.
* How to post-process the final report.
* (Optionally) a custom DAG topology.

Most plugins only need to override a few declarative attributes.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config.config import Config
    from src.core.task_context import TaskContext
    from src.core.task_graph import TaskGraph


@dataclass
class PostProcessFlags:
    """Declarative toggles applied during report post-processing."""

    add_introduction: bool = True
    add_cover_page: bool = False
    add_references: bool = True
    enable_chart: bool = True


class ReportPlugin(ABC):
    """Base class that every report-type plugin must extend.

    Subclasses **must** set :attr:`name` (equal to the ``target_type`` config
    value, e.g. ``"financial_company"``).
    """

    # -- Declarative attributes (override in subclass) -------------------
    name: str = ""

    def get_tool_categories(self) -> list[str]:
        """Tool categories to load for :class:`DataCollector`.

        Defaults to *all* registered categories.
        """
        return ["financial", "macro", "industry", "web"]

    def get_post_process_flags(self) -> PostProcessFlags:
        """Flags consumed by post-processing steps in ReportGenerator."""
        return PostProcessFlags()

    def get_prompt_defaults(self) -> dict[str, str]:
        """Default format-string values injected into every prompt.

        Plugins override this to supply role/domain parameters used by
        parameterized ``_base/`` prompts (e.g. ``{analyst_role}``,
        ``{domain}``).  Callers can still override at ``get_prompt()`` time.
        """
        return {"analyst_role": "research", "domain": "professional"}

    # -- Directory helpers -----------------------------------------------
    def get_plugin_dir(self) -> Path:
        """Root directory of this plugin (where ``plugin.py`` lives)."""
        return Path(os.path.dirname(os.path.abspath(self._source_file())))

    def get_prompt_dir(self) -> Path:
        """Directory containing plugin-specific prompt YAML files."""
        return self.get_plugin_dir() / "prompts"

    def get_template_dir(self) -> Path:
        """Directory containing plugin-specific templates."""
        return self.get_plugin_dir() / "templates"

    def get_template_path(self, name: str) -> Path:
        """Return the full path to a named template file."""
        return self.get_template_dir() / name

    # -- DAG builder -----------------------------------------------------
    def build_task_graph(
        self,
        config: "Config",
        ctx: "TaskContext",
        collect_tasks: list[str],
        analyze_tasks: list[str],
    ) -> "TaskGraph":
        """Build the task DAG for this report type.

        The default implementation creates the standard
        ``collector → analyzer → report`` topology.  Override in a
        subclass only if you need a genuinely different DAG shape.
        """
        from src.core.task_graph import TaskGraph, TaskNode
        from src.agents import DataAnalyzer, DataCollector, ReportGenerator

        use_llm_name = os.getenv("DS_MODEL_NAME", "deepseek-chat")
        use_vlm_name = os.getenv("VLM_MODEL_NAME", "qwen/qwen3-vl-235b-a22b-instruct")
        use_embedding_name = os.getenv("EMBEDDING_MODEL_NAME", "qwen/qwen3-embedding-0.6b")

        graph = TaskGraph()
        collector_ids: list[str] = []
        analyzer_ids: list[str] = []

        target_desc = (
            f"Research target: {ctx.target_name}"
            + (f" (ticker: {ctx.stock_code})" if ctx.stock_code else "")
        )

        # -- Collectors (all parallel) -----------------------------------
        for idx, task in enumerate(collect_tasks):
            tid = f"collect_{idx}"
            collector_ids.append(tid)
            graph.add_task(TaskNode(
                task_id=tid,
                agent_class=DataCollector,
                agent_kwargs={
                    "use_llm_name": use_llm_name,
                    "tool_categories": self.get_tool_categories(),
                },
                run_kwargs={
                    "input_data": {"task": f"{target_desc}, task: {task}"},
                    "echo": True,
                    "max_iterations": 20,
                },
            ))

        # -- Analyzers (soft-depend on all collectors, min=1) ------------
        for idx, task in enumerate(analyze_tasks):
            tid = f"analyze_{idx}"
            analyzer_ids.append(tid)
            graph.add_task(TaskNode(
                task_id=tid,
                agent_class=DataAnalyzer,
                agent_kwargs={
                    "use_llm_name": use_llm_name,
                    "use_vlm_name": use_vlm_name,
                    "use_embedding_name": use_embedding_name,
                },
                run_kwargs={
                    "input_data": {
                        "task": target_desc,
                        "analysis_task": task,
                    },
                    "echo": True,
                    "max_iterations": 20,
                },
                soft_depends_on=list(collector_ids),
                min_soft_deps=min(1, len(collector_ids)),
            ))

        # -- Report (soft-depend on all analyzers, min=1) ----------------
        graph.add_task(TaskNode(
            task_id="report",
            agent_class=ReportGenerator,
            agent_kwargs={
                "use_llm_name": use_llm_name,
                "use_embedding_name": use_embedding_name,
            },
            run_kwargs={
                "input_data": {
                    "task": target_desc,
                    "task_type": ctx.target_type,
                },
                "echo": True,
                "max_iterations": 20,
            },
            soft_depends_on=list(analyzer_ids),
            min_soft_deps=min(1, len(analyzer_ids)),
        ))

        return graph

    # -- Private helpers -------------------------------------------------
    def _source_file(self) -> str:
        """Return the file path of the concrete plugin module.

        Used by :meth:`get_plugin_dir` to resolve relative paths.
        Subclasses should **not** override this — it relies on
        ``__init_subclass__`` capturing the file at class-definition time.
        """
        return getattr(self, "_plugin_source_file", __file__)

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Capture the file where the subclass is defined so that
        # get_plugin_dir() can resolve relative paths correctly.
        import inspect
        src = inspect.getfile(cls)
        cls._plugin_source_file = src  # type: ignore[attr-defined]

"""Pure-function helpers extracted from variable_memory.py.

These functions handle LLM-based task planning and data selection without
requiring a Memory instance — they operate on TaskContext + Config instead.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import json_repair

from src.utils.prompt_loader import PromptLoader

if TYPE_CHECKING:
    from src.config.config import Config
    from src.core.task_context import TaskContext


# =====================================================================
# Task Planning  (called by Pipeline)
# =====================================================================

async def generate_collect_tasks(
    ctx: TaskContext,
    config: Config,
    prompt_loader: PromptLoader,
    query: str,
    existing_tasks: list[str] | None = None,
    max_num: int = 10,
    use_llm_name: str = "deepseek-chat",
) -> list[str]:
    """Generate data-collection tasks via LLM."""
    llm = config.llm_dict[use_llm_name]
    existing_tasks = existing_tasks or []
    existing_tasks_str = (
        "\n".join(f"- {t}" for t in existing_tasks) if existing_tasks else "None"
    )

    prompt = prompt_loader.get_prompt(
        "generate_collect_task",
        query=query,
        existing_tasks=existing_tasks_str,
        max_num=max_num,
    )
    output = await llm.generate(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    output = json_repair.loads(output)

    if isinstance(output, dict):
        output = output.get(
            "tasks", output.get("collect_tasks", output.get("collection_tasks", []))
        )
    if not isinstance(output, list):
        output = []
    return output[:max_num]


async def generate_analyze_tasks(
    ctx: TaskContext,
    config: Config,
    prompt_loader: PromptLoader,
    query: str,
    existing_tasks: list[str] | None = None,
    max_num: int = 10,
    use_llm_name: str = "deepseek-chat",
) -> list[str]:
    """Generate analysis tasks via LLM."""
    llm = config.llm_dict[use_llm_name]
    existing_tasks = existing_tasks or []
    existing_tasks_str = (
        "\n".join(f"- {t}" for t in existing_tasks) if existing_tasks else "None"
    )

    prompt = prompt_loader.get_prompt(
        "generate_task",
        query=query,
        existing_tasks=existing_tasks_str,
        max_num=max_num,
    )
    output = await llm.generate(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    output = json_repair.loads(output)

    if isinstance(output, dict):
        output = output.get("tasks", output.get("analysis_tasks", []))
    if not isinstance(output, list):
        output = []
    return output[:max_num]


# =====================================================================
# Data Selection  (called by Agents)
# =====================================================================

def _format_data_description(data_list: list) -> str:
    """Build a text description of collected ToolResult items."""
    try:
        from src.tools.web.base_search import SearchResult
        filtered = [item for item in data_list if not isinstance(item, SearchResult)]
    except Exception:
        filtered = data_list
    return "\n\n".join(str(item) for item in filtered)


def _format_analysis_description(analysis_list: list) -> str:
    """Build a text description of AnalysisResult items."""
    parts = []
    for idx, item in enumerate(analysis_list):
        parts.append(f"Analysis report {idx + 1}:\n{item}\n")
    return "\n".join(parts)


async def select_data_by_llm(
    ctx: TaskContext,
    config: Config,
    prompt_loader: PromptLoader,
    query: str,
    max_k: int = -1,
    use_llm_name: str = "deepseek-chat",
) -> tuple[list, str]:
    """Use LLM to select relevant collected data for a query.

    Returns ``(selected_data_list, formatted_description)``.
    """
    from src.tools.base import ToolResult

    try:
        from src.agents.search_agent.search_agent import DeepSearchResult
    except Exception:
        DeepSearchResult = None

    collected = [
        item for item in ctx.get("collected_data")
        if isinstance(item, ToolResult)
        and (DeepSearchResult is None or not isinstance(item, DeepSearchResult))
    ]

    model = config.llm_dict[use_llm_name]
    prompt = prompt_loader.get_prompt(
        "select_data",
        data_description=_format_data_description(collected),
        section_description=query,
    )
    output = await model.generate(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    if output is None:
        return [], ""

    match = re.search(r"```json([\s\S]*?)```", output)
    if match:
        output = match.group(1).strip()
    names = json_repair.loads(output).get("selected_data_list", [])
    if max_k > 0:
        names = names[:max_k]

    selected = [item for item in collected if item.name in names]
    return selected, _format_data_description(selected)


async def select_analysis_by_llm(
    ctx: TaskContext,
    config: Config,
    prompt_loader: PromptLoader,
    query: str,
    max_k: int = -1,
    use_llm_name: str = "deepseek-chat",
) -> tuple[list, str]:
    """Use LLM to select relevant analysis results for a query.

    Returns ``(selected_analysis_list, formatted_description)``.
    """
    try:
        from src.agents import AnalysisResult
    except Exception:
        AnalysisResult = type(None)  # no match possible

    analyses = [
        item for item in ctx.get("analysis_results")
        if isinstance(item, AnalysisResult)
    ]

    model = config.llm_dict[use_llm_name]
    prompt = prompt_loader.get_prompt(
        "select_analysis",
        analysis_description=_format_analysis_description(analyses),
        section_description=query,
    )
    output = await model.generate(
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    if output is None:
        return [], ""

    match = re.search(r"```json([\s\S]*?)```", output)
    if match:
        output = match.group(1).strip()
    names = json_repair.loads(output).get("selected_analysis_list", [])
    if max_k > 0:
        names = names[:max_k]

    selected = [item for item in analyses if item.title in names]
    return selected, _format_analysis_description(selected)

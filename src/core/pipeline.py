"""Unified orchestrator for the report-generation pipeline.

Replaces the duplicated scheduling logic in ``run_report.py`` and
``demo/backend/app.py`` with a single DAG-driven executor.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from src.config.config import Config
from src.core.checkpoint import CheckpointManager
from src.core.llm_helpers import generate_analyze_tasks, generate_collect_tasks
from src.core.task_context import TaskContext
from src.core.task_graph import AgentResult, AgentStatus, TaskGraph, TaskNode, TaskState
from src.plugins import load_plugin
from src.plugins.base_plugin import ReportPlugin
from src.utils.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Event
# ------------------------------------------------------------------
@dataclass
class PipelineEvent:
    """Lightweight event emitted during pipeline execution."""

    type: str  # "task_started" | "task_completed" | "task_failed"
    task_id: str
    error: Optional[str] = None


# ------------------------------------------------------------------
# Task generation
# ------------------------------------------------------------------
async def generate_tasks(
    ctx: TaskContext,
    config: Config,
) -> tuple[list[str], list[str]]:
    """Generate collect + analyze task lists via LLM, merged with custom ones.

    Returns ``(all_collect_tasks, all_analyze_tasks)``.
    """
    prompt_loader = PromptLoader.create_loader_for_memory(ctx.target_type)
    query = (
        f"Research target: {ctx.target_name}"
        + (f" (ticker: {ctx.stock_code})" if ctx.stock_code else "")
        + f", target type: {ctx.target_type}"
    )

    custom_collect: list[str] = config.config.get("custom_collect_tasks", [])
    custom_analyze: list[str] = config.config.get("custom_analysis_tasks", [])

    use_llm_name = config.default_llm_name

    llm_collect = await generate_collect_tasks(
        ctx, config, prompt_loader, query,
        existing_tasks=custom_collect, use_llm_name=use_llm_name,
    )
    llm_analyze = await generate_analyze_tasks(
        ctx, config, prompt_loader, query,
        existing_tasks=custom_analyze, use_llm_name=use_llm_name,
    )

    all_collect = list(custom_collect) + [t for t in llm_collect if t not in custom_collect]
    all_analyze = list(custom_analyze) + [t for t in llm_analyze if t not in custom_analyze]
    return all_collect, all_analyze


# ------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------
class Pipeline:
    """DAG-driven orchestrator that schedules agents concurrently."""

    def __init__(
        self,
        config: Config,
        max_concurrent: int = 3,
        on_event: Callable[[PipelineEvent], Awaitable[None]] | None = None,
        max_retries: int = 0,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.max_concurrent = max_concurrent
        self.on_event = on_event
        self.max_retries = max_retries
        self.dry_run = dry_run
        self.checkpoint_mgr = CheckpointManager(config.working_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def run(
        self,
        task_context: TaskContext,
        resume: bool = True,
        plugin: ReportPlugin | None = None,
    ) -> TaskGraph:
        """Generate tasks, build a DAG, and execute it.

        Args:
            task_context: Shared data bus.
            resume: Whether to restore from the latest checkpoint.
            plugin: The :class:`ReportPlugin` that provides DAG topology
                and tool-category configuration.  When *None*, the plugin
                is loaded automatically from ``task_context.target_type``.

        Returns:
            The completed :class:`TaskGraph` (useful for inspection).
        """
        if plugin is None:
            plugin = load_plugin(task_context.target_type)

        all_collect, all_analyze = await generate_tasks(task_context, self.config)
        logger.info(
            "Tasks — collect: %d, analyze: %d", len(all_collect), len(all_analyze)
        )

        graph = plugin.build_task_graph(
            self.config, task_context, all_collect, all_analyze
        )

        if self.dry_run:
            print("=== Dry Run ===")
            print(f"Collect tasks ({len(all_collect)}): {all_collect}")
            print(f"Analyze tasks ({len(all_analyze)}): {all_analyze}")
            print(f"DAG:\n{json.dumps(graph.summary(), indent=2, ensure_ascii=False)}")
            return graph

        if resume:
            restored = self.checkpoint_mgr.restore_pipeline(graph, task_context)
            if restored:
                logger.info("Resumed from checkpoint.")
                await self._repopulate_artifacts(graph, task_context)

        await self._execute_graph(graph, task_context)
        self.checkpoint_mgr.save_pipeline(graph, task_context)
        return graph

    # ------------------------------------------------------------------
    # Checkpoint artifact recovery
    # ------------------------------------------------------------------
    async def _repopulate_artifacts(
        self, graph: TaskGraph, ctx: TaskContext
    ) -> None:
        """Re-populate task_context artifacts from completed agents' checkpoints.

        After a pipeline resume, the TaskContext is fresh — artifacts from
        previously completed agents are lost.  This method restores each
        completed agent from its own checkpoint and re-pushes its outputs
        into *ctx* so that downstream agents see upstream data.
        """
        for node in graph._nodes.values():
            if node.state != TaskState.DONE:
                continue
            try:
                agent = await self._create_or_restore_agent(node, ctx)
                agent._repopulate_task_context()
            except Exception as e:
                logger.warning(
                    "Could not repopulate artifacts for %s: %s",
                    node.task_id, e,
                )

    # ------------------------------------------------------------------
    # Internal scheduling
    # ------------------------------------------------------------------
    async def _execute_graph(self, graph: TaskGraph, ctx: TaskContext) -> None:
        sem = asyncio.Semaphore(self.max_concurrent)
        running: dict[str, asyncio.Task] = {}

        while not graph.is_complete():
            for node in graph.get_ready_tasks():
                if node.task_id in running:
                    continue
                node.state = TaskState.RUNNING
                await self._emit("task_started", node.task_id)
                running[node.task_id] = asyncio.create_task(
                    self._run_node(node, ctx, sem, graph)
                )

            if not running:
                break

            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                tid = next((k for k, v in running.items() if v is task), None)
                if tid is None:
                    logger.error("Completed asyncio.Task not found in running dict")
                    continue
                del running[tid]
                exc = task.exception()
                if exc is not None:
                    graph.mark_failed(tid, str(exc))
                    await self._emit("task_failed", tid, error=str(exc))
                    logger.error("Task %s failed: %s", tid, exc)
                else:
                    graph.mark_done(tid, task.result())
                    await self._emit("task_completed", tid)

            logger.info("DAG state: %s", graph.summary())
            self.checkpoint_mgr.save_pipeline(graph, ctx)

    async def _run_node(
        self,
        node: TaskNode,
        ctx: TaskContext,
        sem: asyncio.Semaphore,
        graph: TaskGraph,
    ) -> AgentResult:
        async with sem:
            agent = await self._create_or_restore_agent(node, ctx)

            failed_deps = graph.get_failed_soft_deps(node.task_id)
            if failed_deps:
                node.run_kwargs["missing_dependencies"] = failed_deps
                logger.warning("%s: soft deps failed: %s", node.task_id, failed_deps)

            last_err: BaseException | None = None
            for attempt in range(1 + self.max_retries):
                try:
                    await agent.async_run(**node.run_kwargs)
                    return AgentResult(node.task_id, AgentStatus.SUCCESS)
                except Exception as e:
                    last_err = e
                    if attempt < self.max_retries:
                        logger.warning(
                            "Retry %d/%d for %s: %s",
                            attempt + 1, self.max_retries, node.task_id, e,
                        )
            raise last_err  # type: ignore[misc]

    async def _create_or_restore_agent(self, node: TaskNode, ctx: TaskContext):
        """Instantiate (or restore) the agent for *node*."""
        saved = self.checkpoint_mgr.load_agent(node.task_id, phase=None)
        if saved is not None:
            agent = await node.agent_class.from_checkpoint(
                config=self.config,
                task_context=ctx,
                agent_id=node.task_id,
                **node.agent_kwargs,
            )
            if agent is not None:
                agent.checkpoint_mgr = self.checkpoint_mgr
                return agent

        agent = node.agent_class(
            config=self.config,
            task_context=ctx,
            agent_id=node.task_id,
            **node.agent_kwargs,
        )
        agent.checkpoint_mgr = self.checkpoint_mgr
        return agent

    async def _emit(
        self, event_type: str, task_id: str, **kwargs: Any
    ) -> None:
        if self.on_event is not None:
            try:
                await self.on_event(
                    PipelineEvent(type=event_type, task_id=task_id, **kwargs)
                )
            except Exception as e:
                logger.error("Event callback error: %s", e)

"""DAG engine for task scheduling with hard/soft dependencies."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AgentStatus(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class AgentResult:
    agent_id: str
    status: AgentStatus
    error: Optional[str] = None


class TaskState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class TaskNode:
    """A single node in the task DAG.

    Attributes:
        task_id:         Unique identifier.
        agent_class:     The agent class to instantiate for this task.
        agent_kwargs:    Kwargs forwarded to the agent constructor.
        run_kwargs:      Kwargs forwarded to ``agent.async_run()``.
        depends_on:      Hard dependencies — all must be DONE.
        soft_depends_on: Soft dependencies — must all reach a terminal state.
        min_soft_deps:   Minimum number of soft deps that must be DONE.
        state:           Current execution state.
        result:          Outcome after execution.
    """

    task_id: str
    agent_class: type
    agent_kwargs: dict = field(default_factory=dict)
    run_kwargs: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    soft_depends_on: list[str] = field(default_factory=list)
    min_soft_deps: int = 0
    state: TaskState = TaskState.PENDING
    result: Optional[AgentResult] = None


_TERMINAL_STATES = frozenset({TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED})


class TaskGraph:
    """Directed acyclic graph of :class:`TaskNode` instances."""

    def __init__(self) -> None:
        self._nodes: dict[str, TaskNode] = {}

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def add_task(self, node: TaskNode) -> TaskGraph:
        self._nodes[node.task_id] = node
        return self

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def get_ready_tasks(self) -> list[TaskNode]:
        """Return PENDING tasks whose dependencies are satisfied."""
        ready: list[TaskNode] = []
        for n in self._nodes.values():
            if n.state != TaskState.PENDING:
                continue
            # Hard deps must all be DONE
            if not all(self._nodes[d].state == TaskState.DONE for d in n.depends_on):
                continue
            # Soft deps must all have reached a terminal state
            if not all(self._nodes[d].state in _TERMINAL_STATES for d in n.soft_depends_on):
                continue
            # Check minimum number of successful soft deps
            done_soft = sum(
                1 for d in n.soft_depends_on if self._nodes[d].state == TaskState.DONE
            )
            if done_soft < n.min_soft_deps:
                n.state = TaskState.SKIPPED
                self._cascade_skip(n.task_id)
                continue
            ready.append(n)
        return ready

    def get_failed_soft_deps(self, task_id: str) -> list[str]:
        """Return soft-dependency task IDs that FAILED or were SKIPPED."""
        node = self._nodes[task_id]
        return [
            d
            for d in node.soft_depends_on
            if self._nodes[d].state in (TaskState.FAILED, TaskState.SKIPPED)
        ]

    def is_complete(self) -> bool:
        return all(n.state in _TERMINAL_STATES for n in self._nodes.values())

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def mark_done(self, task_id: str, result: AgentResult) -> None:
        self._nodes[task_id].state = TaskState.DONE
        self._nodes[task_id].result = result

    def mark_failed(self, task_id: str, error: str) -> None:
        self._nodes[task_id].state = TaskState.FAILED
        self._nodes[task_id].result = AgentResult(task_id, AgentStatus.FAILED, error)
        self._cascade_skip(task_id)

    def _cascade_skip(self, failed_id: str) -> None:
        """Recursively SKIP any PENDING node that hard-depends on *failed_id*."""
        for node in self._nodes.values():
            if failed_id in node.depends_on and node.state == TaskState.PENDING:
                node.state = TaskState.SKIPPED
                self._cascade_skip(node.task_id)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def summary(self) -> dict[str, str]:
        return {tid: n.state.value for tid, n in self._nodes.items()}

    def to_dict(self) -> dict:
        return {
            tid: {
                "state": n.state.value,
                "depends_on": n.depends_on,
                "soft_depends_on": n.soft_depends_on,
                "min_soft_deps": n.min_soft_deps,
                "error": n.result.error if n.result else None,
            }
            for tid, n in self._nodes.items()
        }

    def restore_from_dict(self, data: dict) -> None:
        for tid, info in data.items():
            if tid in self._nodes:
                self._nodes[tid].state = TaskState(info["state"])
                error = info.get("error")
                if error and info["state"] == TaskState.FAILED.value:
                    self._nodes[tid].result = AgentResult(tid, AgentStatus.FAILED, error)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._nodes

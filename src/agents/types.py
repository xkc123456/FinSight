"""Shared type definitions for the agent framework.

These lightweight data classes standardize the communication protocol
between agents, the orchestrator, and the shared context.
"""

from dataclasses import dataclass, field
from typing import Any, Optional
from datetime import datetime


@dataclass
class Action:
    """Parsed action extracted from an LLM response.

    Only two canonical action types are supported:
      - ``"code"``  — execute Python in the sandbox
      - ``"final"`` — return the final result and stop the loop
    """
    type: str
    content: str


@dataclass
class ToolCallRecord:
    """Structured log entry for a single tool invocation."""
    tool_name: str
    kwargs: dict
    result: Any
    error: bool = False
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class AgentResult:
    """Unified output produced by every agent.

    Attributes:
        agent_id: Unique identifier of the producing agent.
        result_type: Semantic tag, e.g. ``"collected_data"``,
            ``"analysis"``, ``"search"``, ``"report_section"``.
        content: The actual payload (DataFrame, text, dict, etc.).
        metadata: Provenance info — timing, tool-call records, model used.
        summary: Short LLM-generated digest for downstream consumers.
    """
    agent_id: str
    result_type: str
    content: Any
    metadata: dict = field(default_factory=dict)
    summary: str = ""

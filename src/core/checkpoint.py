"""Checkpoint management for Pipeline and Agent state persistence."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import dill

from src.utils.logger import get_logger

logger = get_logger()

CHECKPOINT_VERSION = 2


class CheckpointManager:
    """Handles saving / restoring pipeline-level and agent-level checkpoints.

    * Pipeline state → JSON  (human-readable, ``cat`` to inspect)
    * Agent state    → dill  (supports lambda / closure serialisation)
    """

    def __init__(self, working_dir: str) -> None:
        self.checkpoint_dir = os.path.join(working_dir, "checkpoints")
        os.makedirs(self.checkpoint_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Pipeline-level (JSON)
    # ------------------------------------------------------------------
    def save_pipeline(self, graph, ctx) -> None:
        """Atomically write pipeline state to ``pipeline.json``."""
        data = {
            "version": CHECKPOINT_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "graph": graph.to_dict(),
            "task_context": ctx.to_dict(),
        }
        path = os.path.join(self.checkpoint_dir, "pipeline.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # atomic on POSIX; best-effort on Windows

    def restore_pipeline(self, graph, ctx) -> bool:
        """Restore pipeline state from ``pipeline.json``. Returns True on success."""
        path = os.path.join(self.checkpoint_dir, "pipeline.json")
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version", 1) != CHECKPOINT_VERSION:
            logger.warning("Checkpoint version mismatch — starting fresh.")
            return False
        graph.restore_from_dict(data["graph"])
        ctx.restore_from_dict(data["task_context"])
        return True

    # ------------------------------------------------------------------
    # Agent-level (dill)
    # ------------------------------------------------------------------
    def save_agent(self, agent_id: str, phase: str, data: Any) -> None:
        """Persist per-agent state keyed by *agent_id* and *phase*."""
        agent_dir = os.path.join(self.checkpoint_dir, "agents", agent_id)
        os.makedirs(agent_dir, exist_ok=True)
        path = os.path.join(agent_dir, f"{phase}.dill")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            dill.dump(data, f)
        os.replace(tmp, path)

    def load_agent(self, agent_id: str, phase: Optional[str] = None) -> Any:
        """Load agent checkpoint.  If *phase* is None, load the latest."""
        agent_dir = os.path.join(self.checkpoint_dir, "agents", agent_id)
        if not os.path.exists(agent_dir):
            return None

        if phase is not None:
            path = os.path.join(agent_dir, f"{phase}.dill")
            if not os.path.exists(path):
                return None
            with open(path, "rb") as f:
                return dill.load(f)

        # Find the latest checkpoint file by modification time
        files = [f for f in os.listdir(agent_dir) if f.endswith(".dill")]
        if not files:
            return None
        latest = max(files, key=lambda f: os.path.getmtime(os.path.join(agent_dir, f)))
        path = os.path.join(agent_dir, latest)
        with open(path, "rb") as f:
            return dill.load(f)

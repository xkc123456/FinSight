"""Shared data bus for inter-agent communication."""
from __future__ import annotations

import json
import threading
from typing import Any

from src.config.config import Config


class TaskContext:
    """Thread-safe shared data bus that replaces Memory as the data conduit.

    Agents write artifacts via ``put(key, value)`` and read via ``get(key)``.
    The context is serialisable to / from dict for checkpoint support.
    """

    def __init__(
        self,
        config: Config,
        target_name: str,
        stock_code: str,
        target_type: str,
        language: str,
    ):
        self.config = config
        self.target_name = target_name
        self.stock_code = stock_code
        self.target_type = target_type
        self.language = language
        self._artifacts: dict[str, list[Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    _LANGUAGE_DISPLAY = {"zh": "Chinese (中文)", "en": "English"}

    @property
    def language_display_name(self) -> str:
        """Human-readable language name for use in prompts."""
        return self._LANGUAGE_DISPLAY.get(self.language, self.language)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config: Config) -> TaskContext:
        c = config.config
        return cls(
            config=config,
            target_name=c["target_name"],
            stock_code=c.get("stock_code", ""),
            target_type=c["target_type"],
            language=c.get("language", "zh"),
        )

    # ------------------------------------------------------------------
    # Artifact read / write
    # ------------------------------------------------------------------
    def put(self, key: str, value: Any) -> None:
        """Append *value* to the list stored under *key*."""
        with self._lock:
            self._artifacts.setdefault(key, []).append(value)

    def get(self, key: str) -> list[Any]:
        """Return a shallow copy of the list stored under *key*."""
        with self._lock:
            return list(self._artifacts.get(key, []))

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Serialise the context to a JSON-friendly dict (for checkpoints)."""
        with self._lock:
            return {
                "target_name": self.target_name,
                "stock_code": self.stock_code,
                "target_type": self.target_type,
                "language": self.language,
                "artifacts": {
                    k: [str(v) for v in vs] for k, vs in self._artifacts.items()
                },
            }

    def restore_from_dict(self, data: dict) -> None:
        """Restore scalar fields from a checkpoint dict.

        Note: artifacts are **not** restored here because their types are
        lost during JSON serialisation.  Use ``load_artifacts_from`` for
        single-agent debugging instead.
        """
        self.target_name = data["target_name"]
        self.stock_code = data.get("stock_code", "")
        self.target_type = data.get("target_type", self.target_type)
        self.language = data.get("language", self.language)

    def load_artifacts_from(self, json_path: str) -> None:
        """Load artifacts from a pipeline checkpoint JSON (for single-agent debugging)."""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        artifacts = data.get("task_context", {}).get("artifacts", {})
        with self._lock:
            self._artifacts = artifacts

"""Prompt loader with Plugin → _base/ → Agent fallback resolution.

Lookup order for ``get_prompt(key)``:

1. **Plugin overrides** – ``src/plugins/<plugin_name>/prompts/<agent_name>.yaml``
2. **Shared base**     – ``src/prompts/_base/<key>.yaml``
3. **Agent defaults**  – ``src/agents/<agent_name>/prompts/`` (legacy fallback)

Parsed YAML files are cached by path so repeated construction is cheap.
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional

import yaml


# src/ directory (resolved once)
_SRC_DIR = Path(__file__).resolve().parent.parent
_BASE_PROMPT_DIR = _SRC_DIR / "prompts" / "_base"


class PromptLoader:
    """Load and resolve prompt templates across plugin / _base / agent layers."""

    # Class-level YAML file cache: file path → parsed dict
    _yaml_cache: ClassVar[Dict[str, Dict[str, Any]]] = {}

    def __init__(
        self,
        agent_name: str,
        plugin_name: str = "general",
        prompt_defaults: Optional[Dict[str, str]] = None,
        *,
        # Legacy support: if prompts_dir is given, use the old single-dir mode.
        prompts_dir: Optional[str] = None,
        report_type: Optional[str] = None,
    ):
        self.agent_name = agent_name
        self.plugin_name = plugin_name
        self._defaults: Dict[str, str] = prompt_defaults or {}

        # Merged prompt dict (base → plugin, plugin wins)
        self.prompts: Dict[str, Any] = {}

        if prompts_dir is not None:
            # ---- Legacy single-directory mode ----
            self._legacy_dir = Path(prompts_dir)
            self._load_legacy(report_type or "general")
        else:
            # ---- New layered resolution ----
            self._legacy_dir = None
            self._load_layered()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_layered(self) -> None:
        """Load prompts: _base/ first, then plugin overrides on top."""
        # 1. _base/ – one key per YAML file
        if _BASE_PROMPT_DIR.is_dir():
            for yaml_file in _BASE_PROMPT_DIR.glob("*.yaml"):
                data = self._read_yaml(yaml_file)
                if isinstance(data, dict):
                    self.prompts.update(data)

        # 2. Plugin overrides – per-agent YAML in plugin's prompts/
        plugin_file = (
            _SRC_DIR / "plugins" / self.plugin_name / "prompts"
            / f"{self.agent_name}.yaml"
        )
        if plugin_file.is_file():
            data = self._read_yaml(plugin_file)
            if isinstance(data, dict):
                self.prompts.update(data)

        # 3. Agent defaults (legacy fallback for agent-only prompts like
        #    search_agent/deep_search, data_collector/data_collect).
        agent_dir = _SRC_DIR / "agents" / self.agent_name / "prompts"
        if agent_dir.is_dir():
            # Try the same resolution order as the old loader:
            # {plugin_name}_prompts.yaml → {parent}_prompts.yaml →
            # general_prompts.yaml → prompts.yaml
            candidates = [
                agent_dir / f"{self.plugin_name}_prompts.yaml",
                agent_dir / f"{self.plugin_name.split('_')[0]}_prompts.yaml",
                agent_dir / "general_prompts.yaml",
                agent_dir / "prompts.yaml",
            ]
            for candidate in candidates:
                if candidate.is_file():
                    data = self._read_yaml(candidate)
                    if isinstance(data, dict):
                        for k, v in data.items():
                            self.prompts.setdefault(k, v)
                    break

    def _load_legacy(self, report_type: str) -> None:
        """Old resolution: single directory with report-type file selection."""
        d = self._legacy_dir
        if not d.is_dir():
            raise ValueError(f"Prompts directory not found: {d}")

        specific = d / f"{report_type}_prompts.yaml"
        parent = d / f"{report_type.split('_')[0]}_prompts.yaml"
        default = d / "prompts.yaml"

        for candidate in (specific, parent, default):
            if candidate.is_file():
                self.prompts = self._read_yaml(candidate)
                return

        raise FileNotFoundError(
            f"No prompt file found in {d}. "
            f"Tried: {specific.name}, {parent.name}, {default.name}"
        )

    @classmethod
    def _read_yaml(cls, path: Path) -> Dict[str, Any]:
        key = str(path)
        if key not in cls._yaml_cache:
            with open(path, "r", encoding="utf-8") as f:
                cls._yaml_cache[key] = yaml.safe_load(f) or {}
        # Return a shallow copy so callers don't mutate the cache.
        cached = cls._yaml_cache[key]
        return dict(cached) if isinstance(cached, dict) else cached

    @classmethod
    def clear_cache(cls) -> None:
        """Flush the class-level YAML cache (useful in tests)."""
        cls._yaml_cache.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_prompt(self, prompt_key: str, **kwargs: Any) -> Optional[str]:
        """Return a formatted prompt template, or *None* if the key is missing."""
        if prompt_key not in self.prompts:
            warnings.warn(
                f"Prompt '{prompt_key}' not found. "
                f"Available: {list(self.prompts.keys())}"
            )
            return None

        template: str = self.prompts[prompt_key]

        # Merge defaults (from plugin) with caller-supplied kwargs.
        merged = {**self._defaults, **kwargs}
        if merged:
            try:
                return template.format(**merged)
            except KeyError as exc:
                raise KeyError(
                    f"Missing format parameter {exc} for prompt '{prompt_key}'"
                ) from exc
        return template

    def get_all_prompts(self) -> Dict[str, str]:
        return dict(self.prompts)

    def list_available_prompts(self) -> list[str]:
        return list(self.prompts.keys())

    def reload(self, report_type: Optional[str] = None) -> None:
        self.prompts = {}
        if self._legacy_dir is not None:
            self._load_legacy(report_type or "general")
        else:
            self._load_layered()

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @staticmethod
    def create_loader(
        agent_name: str,
        plugin_name: str = "general",
        prompt_defaults: Optional[Dict[str, str]] = None,
    ) -> "PromptLoader":
        """Primary factory: layered resolution (plugin → _base/ → agent)."""
        return PromptLoader(
            agent_name=agent_name,
            plugin_name=plugin_name,
            prompt_defaults=prompt_defaults,
        )

    @staticmethod
    def create_loader_for_agent(
        agent_name: str, report_type: str = "general"
    ) -> "PromptLoader":
        """Backward-compatible factory — delegates to layered resolution."""
        return PromptLoader.create_loader(agent_name, plugin_name=report_type)

    @staticmethod
    def create_loader_for_memory(report_type: str = "general") -> "PromptLoader":
        """Backward-compatible factory for memory/pipeline prompts."""
        return PromptLoader.create_loader("memory", plugin_name=report_type)


def get_prompt_loader(
    module_name: str, report_type: str = "general"
) -> PromptLoader:
    """Convenience function — backward compatible."""
    if module_name == "memory":
        return PromptLoader.create_loader_for_memory(report_type)
    return PromptLoader.create_loader_for_agent(module_name, report_type)


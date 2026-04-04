"""Tests for TaskContext: thread safety, serialisation, factory."""
import json
import os
import threading
from unittest.mock import MagicMock

import pytest

from src.core.task_context import TaskContext


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_config(**overrides):
    """Return a lightweight mock Config for tests."""
    cfg = MagicMock()
    cfg.config = {
        "target_name": "贵州茅台",
        "stock_code": "600519",
        "target_type": "financial_company",
        "language": "zh",
    }
    cfg.config.update(overrides)
    return cfg


# ------------------------------------------------------------------
# Basic API
# ------------------------------------------------------------------

class TestTaskContextBasic:
    def test_put_and_get(self):
        ctx = TaskContext(_make_config(), "茅台", "600519", "financial_company", "zh")
        ctx.put("collected_data", {"a": 1})
        ctx.put("collected_data", {"b": 2})
        assert len(ctx.get("collected_data")) == 2

    def test_get_returns_copy(self):
        ctx = TaskContext(_make_config(), "茅台", "600519", "financial_company", "zh")
        ctx.put("k", "v")
        result = ctx.get("k")
        result.append("extra")
        assert len(ctx.get("k")) == 1  # original unmodified

    def test_get_missing_key(self):
        ctx = TaskContext(_make_config(), "茅台", "600519", "financial_company", "zh")
        assert ctx.get("nonexistent") == []


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------

class TestFromConfig:
    def test_from_config(self):
        cfg = _make_config()
        ctx = TaskContext.from_config(cfg)
        assert ctx.target_name == "贵州茅台"
        assert ctx.stock_code == "600519"
        assert ctx.target_type == "financial_company"
        assert ctx.language == "zh"

    def test_from_config_defaults(self):
        cfg = _make_config()
        cfg.config.pop("stock_code", None)
        cfg.config.pop("language", None)
        ctx = TaskContext.from_config(cfg)
        assert ctx.stock_code == ""
        assert ctx.language == "zh"


# ------------------------------------------------------------------
# Serialisation
# ------------------------------------------------------------------

class TestSerialisation:
    def test_to_dict_roundtrip(self):
        ctx = TaskContext(_make_config(), "茅台", "600519", "financial_company", "zh")
        ctx.put("data", "hello")
        d = ctx.to_dict()
        assert d["target_name"] == "茅台"
        assert "data" in d["artifacts"]

    def test_restore_from_dict(self):
        ctx = TaskContext(_make_config(), "茅台", "600519", "financial_company", "zh")
        ctx.restore_from_dict({
            "target_name": "泡泡玛特",
            "stock_code": "9992.HK",
            "target_type": "financial_company",
            "language": "en",
        })
        assert ctx.target_name == "泡泡玛特"
        assert ctx.stock_code == "9992.HK"
        assert ctx.language == "en"

    def test_load_artifacts_from(self, tmp_path):
        # Prepare a fake pipeline.json
        checkpoint = {
            "task_context": {
                "artifacts": {
                    "collected_data": ["item1", "item2"],
                }
            }
        }
        p = tmp_path / "pipeline.json"
        p.write_text(json.dumps(checkpoint), encoding="utf-8")

        ctx = TaskContext(_make_config(), "茅台", "600519", "financial_company", "zh")
        ctx.load_artifacts_from(str(p))
        assert ctx.get("collected_data") == ["item1", "item2"]


# ------------------------------------------------------------------
# Thread safety
# ------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_puts(self):
        ctx = TaskContext(_make_config(), "茅台", "600519", "financial_company", "zh")
        n_threads = 10
        n_puts = 100

        def writer(tid):
            for i in range(n_puts):
                ctx.put("data", f"{tid}-{i}")

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(ctx.get("data")) == n_threads * n_puts

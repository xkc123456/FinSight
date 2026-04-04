"""Tests for CheckpointManager: round-trip, version mismatch, atomic write, agent-level."""
import json
import os
import time

import dill
import pytest

from src.core.checkpoint import CHECKPOINT_VERSION, CheckpointManager


# ------------------------------------------------------------------
# Helpers — minimal stubs for graph and ctx
# ------------------------------------------------------------------

class _StubGraph:
    def __init__(self):
        self._data = {}

    def to_dict(self):
        return self._data

    def restore_from_dict(self, data):
        self._data = data


class _StubCtx:
    def __init__(self):
        self.target_name = "茅台"
        self.stock_code = "600519"

    def to_dict(self):
        return {"target_name": self.target_name, "stock_code": self.stock_code}

    def restore_from_dict(self, data):
        self.target_name = data["target_name"]
        self.stock_code = data.get("stock_code", "")


# ------------------------------------------------------------------
# Pipeline checkpoint tests
# ------------------------------------------------------------------

class TestPipelineCheckpoint:
    def test_roundtrip(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        graph = _StubGraph()
        graph._data = {"A": {"state": "done"}, "B": {"state": "pending"}}
        ctx = _StubCtx()

        mgr.save_pipeline(graph, ctx)

        # Verify JSON file exists and is valid
        path = os.path.join(str(tmp_path), "checkpoints", "pipeline.json")
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        assert raw["version"] == CHECKPOINT_VERSION
        assert raw["graph"]["A"]["state"] == "done"

        # Restore into fresh stubs
        g2 = _StubGraph()
        c2 = _StubCtx()
        c2.target_name = "other"
        result = mgr.restore_pipeline(g2, c2)
        assert result is True
        assert g2._data["A"]["state"] == "done"
        assert c2.target_name == "茅台"

    def test_restore_returns_false_when_missing(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        assert mgr.restore_pipeline(_StubGraph(), _StubCtx()) is False

    def test_version_mismatch(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        # Write a checkpoint with wrong version
        ckpt_dir = os.path.join(str(tmp_path), "checkpoints")
        os.makedirs(ckpt_dir, exist_ok=True)
        path = os.path.join(ckpt_dir, "pipeline.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 999, "graph": {}, "task_context": {}}, f)

        result = mgr.restore_pipeline(_StubGraph(), _StubCtx())
        assert result is False

    def test_atomic_write_no_tmp_leftover(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        mgr.save_pipeline(_StubGraph(), _StubCtx())
        ckpt_dir = os.path.join(str(tmp_path), "checkpoints")
        files = os.listdir(ckpt_dir)
        assert not any(f.endswith(".tmp") for f in files)


# ------------------------------------------------------------------
# Agent checkpoint tests
# ------------------------------------------------------------------

class TestAgentCheckpoint:
    def test_save_and_load_by_phase(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        data = {"phase": "analyze", "results": [1, 2, 3]}
        mgr.save_agent("agent_1", "analyze", data)

        loaded = mgr.load_agent("agent_1", phase="analyze")
        assert loaded == data

    def test_load_latest(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        mgr.save_agent("agent_1", "phase_a", {"step": 1})
        time.sleep(0.05)  # ensure distinct mtime
        mgr.save_agent("agent_1", "phase_b", {"step": 2})

        # Latest by mtime should be phase_b
        loaded = mgr.load_agent("agent_1")
        assert loaded["step"] == 2

    def test_load_nonexistent_agent(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        assert mgr.load_agent("nonexistent") is None

    def test_load_nonexistent_phase(self, tmp_path):
        mgr = CheckpointManager(str(tmp_path))
        mgr.save_agent("agent_1", "x", {"v": 1})
        assert mgr.load_agent("agent_1", phase="zzz") is None

"""Manual integration test: run a single DataCollector task.

Usage:
    python tests/agents/test_data_collector.py
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

root = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, root)

from src.config import Config
from src.core.task_context import TaskContext
from src.agents import DataCollector
from src.utils import setup_logger, get_logger

get_logger().set_agent_context("runner", "main")

if __name__ == "__main__":
    config = Config(config_file_path="tests/my_config.yaml")
    log_dir = os.path.join(config.working_dir, "logs")
    setup_logger(log_dir=log_dir, log_level=logging.DEBUG)

    ctx = TaskContext.from_config(config)
    agent = DataCollector(
        config=config,
        task_context=ctx,
        use_llm_name=os.getenv("DS_MODEL_NAME"),
    )
    result = asyncio.run(
        agent.async_run(
            input_data={"task": "浪潮信息（000977）的股价信息"},
            echo=True,
            max_iterations=5,
        )
    )
    print(result["final_result"])
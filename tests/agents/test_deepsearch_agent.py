"""Manual integration test: run a single DeepSearchAgent task.

Usage:
    python tests/agents/test_deepsearch_agent.py
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
from src.agents import DeepSearchAgent
from src.utils import setup_logger, get_logger

get_logger().set_agent_context("runner", "main")

if __name__ == "__main__":
    config = Config(config_file_path="my_config.yaml")

    ctx = TaskContext.from_config(config)

    log_dir = os.path.join(config.working_dir, "logs")
    setup_logger(log_dir=log_dir, log_level=logging.INFO)

    agent = DeepSearchAgent(
        config=config,
        task_context=ctx,
        use_llm_name=os.getenv("DS_MODEL_NAME"),
    )
    result = asyncio.run(
        agent.async_run(
            input_data={
                "task": "商汤科技",
                "query": (
                    "SenseTime government contracts enterprise customers "
                    "financial services healthcare clients 2024"
                ),
            },
            echo=True,
            max_iterations=5,
        )
    )
    print(result["final_result"])
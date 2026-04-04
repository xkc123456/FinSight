"""Manual integration test: run a ReportGenerator from checkpoint.

Usage:
    python tests/test_report_gen.py

Not collected by pytest (no test functions).
"""
import asyncio
import logging
import os
import sys
from pathlib import Path


if __name__ == "__main__":
    root = str(Path(__file__).resolve().parents[1])
    sys.path.insert(0, root)

    from src.config import Config
    from src.core.task_context import TaskContext
    from src.agents.base_agent import BaseAgent
    from src.utils import setup_logger, get_logger

    get_logger().set_agent_context("runner", "main")

    config = Config(config_file_path="my_config.yaml")

    log_dir = os.path.join(config.working_dir, "logs")
    setup_logger(log_dir=log_dir, log_level=logging.INFO)

    ctx = TaskContext.from_config(config)
    # Optionally restore artifacts from a previous checkpoint
    # ctx.load_artifacts_from("outputs/.../checkpoints/pipeline.json")

    agent = asyncio.run(
        BaseAgent.from_checkpoint(
            config=config,
            task_context=ctx,
            agent_id="agent_report_generator_795e8d0a",
        )
    )
    agent.use_embedding_name = "qwen3-embedding-0.6b"
    result = asyncio.run(
        agent.async_run(
            input_data={
                "task": f'研究目标: {config.config["target_name"]}'
                        f'(股票代码: {config.config["stock_code"]})',
                "task_type": "company",
            },
            echo=True,
            max_iterations=5,
        )
    )
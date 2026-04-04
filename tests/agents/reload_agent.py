"""Manual test: reload an agent from checkpoint.

Usage:
    python tests/agents/reload_agent.py
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
from src.agents.base_agent import BaseAgent
from src.utils import setup_logger, get_logger

get_logger().set_agent_context("runner", "main")

if __name__ == "__main__":
    config = Config(
        config_file_path="tests/my_config.yaml",
        config_dict={
            "output_dir": "outputs/tests",
            "target_name": "商汤科技",
            "stock_code": "00020",
            "reference_doc_path": "src/config/report_template.docx",
            "outline_template_path": "src/template/company_outline.md",
        },
    )

    ctx = TaskContext.from_config(config)
    log_dir = os.path.join(config.working_dir, "logs")
    setup_logger(log_dir=log_dir, log_level=logging.DEBUG)

    agent = asyncio.run(
        BaseAgent.from_checkpoint(
            config=config,
            task_context=ctx,
            agent_id="agent_data_collector_a8e4b96b",
            checkpoint_name="latest.pkl",
        )
    )
    print(agent.current_checkpoint)
"""Manual integration test: run a single DataAnalyzer task.

Usage:
    python tests/agents/test_data_analyzer.py
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

root = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, root)

from src.config import Config
from src.core.task_context import TaskContext
from src.agents import DataAnalyzer

if __name__ == "__main__":
    config = Config(config_file_path="my_config.yaml")

    ctx = TaskContext.from_config(config)

    agent = DataAnalyzer(
        config=config,
        task_context=ctx,
        use_llm_name=os.getenv("DS_MODEL_NAME"),
        use_vlm_name=os.getenv("VLM_MODEL_NAME"),
        use_embedding_name=os.getenv("EMBEDDING_MODEL_NAME"),
    )
    result = asyncio.run(
        agent.async_run(
            input_data={
                "task": "商汤科技",
                "analysis_task": "商汤科技的主要营收来源",
            },
            echo=True,
            max_iterations=10,
            enable_chart=True,
        )
    )
    print(result)
    print(result["final_result"][:100000])
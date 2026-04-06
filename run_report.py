"""CLI entry-point for report generation using the Pipeline orchestrator."""
import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from src.config import Config
from src.core.pipeline import Pipeline
from src.core.task_context import TaskContext
from src.plugins import load_plugin
from src.utils import setup_logger, get_logger


async def run_report(
    config_path: str = "my_config.yaml",
    resume: bool = True,
    max_concurrent: int = 3,
    dry_run: bool = False,
    lite: bool = False,
) -> None:
    config = Config(config_file_path=config_path)

    logger = setup_logger(
        log_dir=os.path.join(config.working_dir, "logs"),
        log_level=logging.INFO,
    )
    get_logger().set_agent_context("runner", "main")

    ctx = TaskContext.from_config(config)
    plugin = load_plugin(ctx.target_type)

    # Warn early if the search API is unlikely to work
    if not os.getenv("SERPER_API_KEY"):
        logger.warning(
            "SERPER_API_KEY is not set. Web search is disabled — "
            "the report will lack web-sourced citations."
        )

    logger.section(f"FinSight — {ctx.target_name}")
    logger.info("Target type: %s  |  Language: %s  |  LLM: %s",
                ctx.target_type, ctx.language_display_name, config.default_llm_name)
    logger.info("Working dir: %s", config.working_dir)

    pipeline = Pipeline(
        config=config,
        max_concurrent=max_concurrent,
        dry_run=dry_run,
        lite=lite,
    )

    graph = await pipeline.run(ctx, resume=resume, plugin=plugin)
    logger.section("Pipeline Complete")
    logger.dag_state(graph.summary())


def main() -> None:
    parser = argparse.ArgumentParser(description="FinSight report generation")
    parser.add_argument("--config", default="my_config.yaml", help="Path to YAML config")
    parser.add_argument("--dry-run", action="store_true", help="Print DAG without executing")
    parser.add_argument("--no-resume", action="store_true", help="Start fresh, ignore checkpoints")
    parser.add_argument("--max-concurrent", type=int, default=3, help="Max concurrent agents")
    parser.add_argument("--lite", action="store_true",
                        help="Lite mode: 2 collectors + 1 analyzer, fewer iterations, no charts. Fast & cheap.")
    args = parser.parse_args()

    asyncio.run(
        run_report(
            config_path=args.config,
            resume=not args.no_resume,
            max_concurrent=args.max_concurrent,
            dry_run=args.dry_run,
            lite=args.lite,
        )
    )


if __name__ == "__main__":
    main()


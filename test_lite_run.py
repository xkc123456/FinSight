"""Quick test script to verify lite mode works end-to-end."""
import sys
import traceback

def main():
    try:
        print("[1] Importing modules...", flush=True)
        from src.config import Config
        from src.core.task_context import TaskContext
        from src.core.pipeline import Pipeline
        from src.plugins import load_plugin
        print("    OK", flush=True)

        print("[2] Loading config...", flush=True)
        config = Config(config_file_path='my_config.yaml')
        print(f"    target={config.config['target_name']}, type={config.config['target_type']}", flush=True)

        print("[3] Creating TaskContext...", flush=True)
        ctx = TaskContext.from_config(config)
        print(f"    ctx.target_type={ctx.target_type}", flush=True)

        print("[4] Loading plugin...", flush=True)
        plugin = load_plugin(ctx.target_type)
        print(f"    plugin={plugin.name}", flush=True)

        print("[5] Testing DataCollector instantiation...", flush=True)
        from src.agents import DataCollector
        dc = DataCollector(
            config=config,
            task_context=ctx,
            agent_id='test_dc_check',
            tool_categories=plugin.get_tool_categories(),
        )
        print(f"    tools={len(dc.tools)}, _tool_categories={dc._tool_categories}", flush=True)

        print("[6] Testing Pipeline dry-run...", flush=True)
        import asyncio
        pipeline = Pipeline(config=config, lite=True, dry_run=True)
        graph = asyncio.run(pipeline.run(ctx, resume=False, plugin=plugin))
        print(f"    DAG summary: {graph.summary()}", flush=True)

        print("\n=== ALL CHECKS PASSED ===", flush=True)
    except Exception as e:
        print(f"\n!!! FAILED at step: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()

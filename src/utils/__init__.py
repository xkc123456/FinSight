from src.utils.llm import LLM, AsyncLLM
from src.utils.code_executor_async import AsyncCodeExecutor
from src.utils.index_builder import IndexBuilder
from src.utils.helper import *
from src.utils.logger import get_logger, setup_logger


def __getattr__(name: str):
    if name == "CodeExecutor":
        from src.utils.code_executor import CodeExecutor
        return CodeExecutor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "LLM",
    "AsyncLLM",
    "CodeExecutor",
    "AsyncCodeExecutor",
    "IndexBuilder",
    "get_logger",
    "setup_logger"
]
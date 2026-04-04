"""Thread-safe logging utilities with colored console output."""
import logging
import os
import sys
from typing import Optional
from logging.handlers import RotatingFileHandler
import contextvars


# ---------------------------------------------------------------------------
# Enable ANSI escape sequences on Windows 10+
# ---------------------------------------------------------------------------
def _enable_win_ansi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # STD_OUTPUT_HANDLE = -11, ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass

_enable_win_ansi()


# ---------------------------------------------------------------------------
# ANSI color definitions
# ---------------------------------------------------------------------------
class _C:
    """ANSI escape code constants."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"

    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"

    BRIGHT_BLACK   = "\033[90m"
    BRIGHT_RED     = "\033[91m"
    BRIGHT_GREEN   = "\033[92m"
    BRIGHT_YELLOW  = "\033[93m"
    BRIGHT_BLUE    = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN    = "\033[96m"
    BRIGHT_WHITE   = "\033[97m"

    BG_RED    = "\033[41m"
    BG_GREEN  = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE   = "\033[44m"


# Mapping: log level  -> (level label color, message color or None)
_LEVEL_STYLES: dict[str, tuple[str, str | None]] = {
    "DEBUG":    (_C.BRIGHT_BLACK,                                None),
    "INFO":     (_C.BRIGHT_GREEN,                                None),
    "WARNING":  (_C.BRIGHT_YELLOW,                               _C.BRIGHT_YELLOW),
    "ERROR":    (_C.BRIGHT_RED,                                  _C.BRIGHT_RED),
    "CRITICAL": (_C.BOLD + _C.BRIGHT_WHITE + _C.BG_RED,         _C.BOLD + _C.BRIGHT_RED),
}

# Mapping: agent name -> color
_AGENT_COLORS: dict[str, str] = {
    "main":              _C.WHITE,
    "data_collector":    _C.GREEN,
    "data_analyzer":     _C.CYAN,
    "report_generator":  _C.MAGENTA,
    "deepsearch agent":  _C.BLUE,
}

# Fallback palette for unknown agent names
_PALETTE = [_C.BRIGHT_CYAN, _C.BRIGHT_MAGENTA, _C.BRIGHT_BLUE, _C.BRIGHT_GREEN, _C.BRIGHT_YELLOW]

def _agent_color(name: str) -> str:
    if name in _AGENT_COLORS:
        return _AGENT_COLORS[name]
    # Deterministic color based on hash
    return _PALETTE[hash(name) % len(_PALETTE)]


# ---------------------------------------------------------------------------
# Context variables for async agent identification
# ---------------------------------------------------------------------------
_cv_agent_id: contextvars.ContextVar[str] = contextvars.ContextVar('agent_id', default='N/A')
_cv_agent_name: contextvars.ContextVar[str] = contextvars.ContextVar('agent_name', default='N/A')


class AgentContextFilter(logging.Filter):
    """Injects agent context into log records."""

    def filter(self, record):
        record.agent_id = _cv_agent_id.get()
        record.agent_name = _cv_agent_name.get()
        return True


# ---------------------------------------------------------------------------
# Colored console formatter
# ---------------------------------------------------------------------------
class ColoredFormatter(logging.Formatter):
    """Formatter that emits colored, column-aligned log lines to the console.

    Format:  ``HH:MM:SS │ LEVEL    │ agent_name │ message``
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, self.datefmt)

        # Level label
        level = record.levelname
        lbl_color, msg_color = _LEVEL_STYLES.get(level, (_C.WHITE, None))
        level_str = f"{lbl_color}{level:<8s}{_C.RESET}"

        # Agent context
        agent_name = getattr(record, "agent_name", "N/A")
        agent_id   = getattr(record, "agent_id",   "N/A")
        ac = _agent_color(agent_name)
        agent_str = f"{ac}{agent_name:<18s}{_C.RESET} {_C.DIM}{agent_id}{_C.RESET}"

        # Message (apply level color for warnings / errors)
        msg = record.getMessage()
        if msg_color:
            msg = f"{msg_color}{msg}{_C.RESET}"

        # Exception info
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = msg + "\n" + f"{_C.BRIGHT_RED}{record.exc_text}{_C.RESET}"

        return (
            f"{_C.DIM}{ts}{_C.RESET} │ {level_str} │ {agent_str} │ {msg}"
        )


# ---------------------------------------------------------------------------
# Plain-text file formatter (no ANSI codes)
# ---------------------------------------------------------------------------
_FILE_FMT = "%(asctime)s [%(levelname)-8s] [%(agent_name)s:%(agent_id)s] %(message)s"
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Logger singleton
# ---------------------------------------------------------------------------
class Logger:
    """Thread-safe singleton logger with colored console output."""

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Logger, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if not Logger._initialized:
            self._setup_logger()
            Logger._initialized = True

    def _setup_logger(self, log_dir: Optional[str] = None, log_level: int = logging.INFO):
        self.logger = logging.getLogger("finsight")
        self.logger.setLevel(log_level)

        if self.logger.handlers:
            return

        ctx_filter = AgentContextFilter()

        # --- Console handler (colored) ---
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(log_level)
        console.setFormatter(ColoredFormatter(datefmt="%H:%M:%S"))
        console.addFilter(ctx_filter)
        self.logger.addHandler(console)

        # --- File handler (plain text) ---
        if log_dir:
            self._add_file_handler(log_dir, log_level)

    def _add_file_handler(self, log_dir: str, log_level: int = logging.INFO):
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "finsight.log")

        # Avoid duplicate file handlers for the same path
        norm = os.path.normpath(os.path.abspath(log_file))
        for h in self.logger.handlers:
            if isinstance(h, RotatingFileHandler):
                if os.path.normpath(os.path.abspath(h.baseFilename)) == norm:
                    return

        ctx_filter = AgentContextFilter()
        fh = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        fh.setLevel(log_level)
        fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_FILE_DATEFMT))
        fh.addFilter(ctx_filter)
        self.logger.addHandler(fh)

    def set_log_dir(self, log_dir: str):
        self._add_file_handler(log_dir)

    # -- Agent context --------------------------------------------------
    def set_agent_context(self, agent_id: str, agent_name: str):
        _cv_agent_id.set(agent_id)
        _cv_agent_name.set(agent_name)

    def clear_agent_context(self):
        _cv_agent_id.set("N/A")
        _cv_agent_name.set("N/A")

    # -- Standard log methods (support %-style format args) -------------
    def debug(self, message: str, *args, **kwargs):
        self.logger.debug(message, *args, **kwargs)

    def info(self, message: str, *args, **kwargs):
        self.logger.info(message, *args, **kwargs)

    def warning(self, message: str, *args, **kwargs):
        self.logger.warning(message, *args, **kwargs)

    def error(self, message: str, *args, exc_info: bool = False, **kwargs):
        self.logger.error(message, *args, exc_info=exc_info, **kwargs)

    def exception(self, message: str, *args, **kwargs):
        self.logger.exception(message, *args, **kwargs)

    def critical(self, message: str, *args, **kwargs):
        self.logger.critical(message, *args, **kwargs)

    # -- Visual helpers for pipeline / agent lifecycle ------------------
    def section(self, title: str):
        """Print a prominent section banner (e.g. pipeline phase)."""
        w = max(len(title) + 4, 50)
        border = "═" * w
        pad = (w - len(title) - 2) // 2
        line = " " * pad + title + " " * (w - pad - len(title) - 2)
        # Bypass formatter – write directly so banner stands out
        raw_banner = (
            f"\n{_C.BOLD}{_C.BRIGHT_CYAN}"
            f"╔{border}╗\n"
            f"║ {line} ║\n"
            f"╚{border}╝"
            f"{_C.RESET}"
        )
        plain_banner = (
            f"\n╔{border}╗\n"
            f"║ {line} ║\n"
            f"╚{border}╝"
        )
        self._emit_styled(raw_banner, plain_banner)

    def task_start(self, task_id: str, extra: str = ""):
        """Log a task start event with a visual marker."""
        msg = f"▶ Task Started: {task_id}"
        if extra:
            msg += f"  ({extra})"
        self.info(msg)

    def task_done(self, task_id: str, extra: str = ""):
        """Log a task completion event with a visual marker."""
        msg = f"✔ Task Done: {task_id}"
        if extra:
            msg += f"  ({extra})"
        self.info(msg)

    def task_fail(self, task_id: str, error: str = ""):
        """Log a task failure event with a visual marker."""
        msg = f"✘ Task Failed: {task_id}"
        if error:
            msg += f"  — {error}"
        self.error(msg)

    def progress(self, done: int, total: int, label: str = ""):
        """Log a compact progress line."""
        pct = (done / total * 100) if total else 0
        bar_len = 20
        filled = int(bar_len * done / total) if total else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        msg = f"[{bar}] {done}/{total} ({pct:.0f}%)"
        if label:
            msg = f"{label}  {msg}"
        self.info(msg)

    def dag_state(self, summary: dict):
        """Log a compact, colored DAG state summary.
        
        Accepts the dict from ``TaskGraph.summary()`` mapping
        ``{task_id: state_value}`` and aggregates into counts.
        """
        from collections import Counter
        counts = Counter(summary.values())
        color_map = {
            "done": _C.GREEN, "running": _C.CYAN,
            "pending": _C.YELLOW, "failed": _C.RED,
            "skipped": _C.BRIGHT_BLACK,
        }
        order = ["done", "running", "pending", "failed", "skipped"]
        parts_c, parts_p = [], []
        for state in order:
            n = counts.get(state, 0)
            if n == 0:
                continue
            c = color_map.get(state, _C.WHITE)
            parts_c.append(f"{c}{state}={n}{_C.RESET}")
            parts_p.append(f"{state}={n}")
        colored_line = "DAG state:  " + "  ".join(parts_c) + f"  ({len(summary)} total)"
        plain_line   = "DAG state:  " + "  ".join(parts_p) + f"  ({len(summary)} total)"
        self._emit_styled(colored_line, plain_line, level=logging.INFO)

    def _emit_styled(self, colored_text: str, plain_text: str, level: int = logging.INFO):
        """Write *colored_text* to console handlers and *plain_text* to file handlers."""
        for handler in self.logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.stream.write(colored_text + "\n")
                handler.stream.flush()
            elif isinstance(handler, (logging.FileHandler, RotatingFileHandler)):
                handler.stream.write(plain_text + "\n")
                handler.stream.flush()

    # -- Handler management ---------------------------------------------
    def addHandler(self, handler):
        self.logger.addHandler(handler)

    def removeHandler(self, handler):
        self.logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Module-level API
# ---------------------------------------------------------------------------
_logger_instance = None


def get_logger() -> Logger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = Logger()
    return _logger_instance


def setup_logger(log_dir: Optional[str] = None, log_level: int = logging.INFO) -> Logger:
    logger = get_logger()
    logger._setup_logger(log_dir=log_dir, log_level=log_level)
    if log_dir:
        logger.set_log_dir(log_dir)
    return logger


# Convenience wrappers
def debug(message: str, *args, **kwargs):
    get_logger().debug(message, *args, **kwargs)

def info(message: str, *args, **kwargs):
    get_logger().info(message, *args, **kwargs)

def warning(message: str, *args, **kwargs):
    get_logger().warning(message, *args, **kwargs)

def error(message: str, *args, exc_info: bool = False, **kwargs):
    get_logger().error(message, *args, exc_info=exc_info, **kwargs)

def exception(message: str, *args, **kwargs):
    get_logger().exception(message, *args, **kwargs)


def critical(message: str):
    """Log a CRITICAL-level message."""
    get_logger().critical(message)


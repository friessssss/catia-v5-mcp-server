"""Per-user state directory and logging setup.

The server may run as an installed package (site-packages is not writable)
or windowless under pythonw.exe (no console: sys.stderr is None), so logs
and runtime state live in a per-user directory:

- Windows: %LOCALAPPDATA%\\catia-mcp
- elsewhere: ~/.catia-mcp
- override: CATIA_MCP_HOME
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"


def state_dir() -> Path:
    override = os.environ.get("CATIA_MCP_HOME")
    if override:
        base = Path(override)
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "catia-mcp"
    else:
        base = Path.home() / ".catia-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def log_path() -> Path:
    return state_dir() / "catia_mcp.log"


def configure_logging(console: bool = True) -> Path:
    """Log to a rotating file in the state directory, plus stderr when one
    exists. Under pythonw.exe stdout/stderr are None; they are pointed at the
    log so third-party prints (uvicorn, pywin32) cannot crash the process."""
    path = log_path()
    if sys.stderr is None or sys.stdout is None:
        stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 -- process lifetime
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
        console = False
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    return path

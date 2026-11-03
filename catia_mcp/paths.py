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
    exists. Under pythonw.exe stdout/stderr are None; they are pointed at a
    separate stdio.log (never the rotated log: Windows cannot rename a file
    another handle keeps open) so stray prints cannot crash the process."""
    path = log_path()
    if sys.stderr is None or sys.stdout is None:
        stdio = state_dir() / "stdio.log"
        stream = open(stdio, "w", encoding="utf-8", buffering=1)  # noqa: SIM115 -- process lifetime
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
    # The proxy and `status` poll /health; per-request client logs are noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return path


_COMMIT: list[str | None] = []


def build_commit() -> str | None:
    """Git commit this code was installed from, so a tester can confirm they
    run the latest push. Read from the PEP 610 direct_url.json of a
    `uv tool install git+...` / `pip install git+...`, else from the
    checkout of an editable install."""
    if _COMMIT:
        return _COMMIT[0]
    commit = None
    try:
        import json
        from importlib.metadata import distribution

        text = distribution("catia-v5-mcp-server").read_text("direct_url.json")
        if text:
            info = json.loads(text)
            commit = (info.get("vcs_info") or {}).get("commit_id")
    except Exception:  # noqa: BLE001 -- informational
        commit = None
    if commit is None:
        repo = Path(__file__).resolve().parent.parent
        if (repo / ".git").exists():
            import subprocess

            try:
                done = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                commit = done.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                commit = None
    _COMMIT.append(commit[:12] if commit else None)
    return _COMMIT[0]

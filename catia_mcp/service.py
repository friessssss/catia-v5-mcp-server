"""Run the server as an always-on per-user background service.

CATIA registers its COM server in the interactive user's desktop session,
so a real Windows Service (session 0) cannot reach it. The service is an
ordinary windowless process (pythonw.exe) in the user's session instead:

- ``catia-mcp install`` registers it to start at logon (HKCU Run key, no
  admin rights needed), starts it now and points Claude Desktop / Claude
  Code at it.
- ``catia-mcp serve`` is the service itself: Streamable HTTP on
  127.0.0.1:8765 plus a /health probe. CATIA does not need to be running;
  the server attaches whenever a tool is called and CATIA is up.
- ``catia-mcp proxy`` is the stdio entry point for clients that only spawn
  local commands (Claude Desktop): it starts the service if needed and
  relays to it, so every client shares one CATIA connection.
- ``catia-mcp status`` / ``stop`` / ``uninstall`` manage it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from catia_mcp import __version__
from catia_mcp.http_transport import DEFAULT_HOST, DEFAULT_PORT, HEALTH_PATH, MCP_PATH
from catia_mcp.paths import log_path, state_dir

SERVER_NAME = "catia"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "CATIA MCP Server"


def base_url(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> str:
    return f"http://{host}:{port}"


def mcp_url(port: int = DEFAULT_PORT) -> str:
    return base_url(port) + MCP_PATH


def health(port: int = DEFAULT_PORT, timeout: float = 2.0, token: str | None = None) -> dict | None:
    """The running service's /health payload, or None if nothing answers."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        # trust_env=False: a corporate proxy from the registry or HTTP_PROXY
        # would otherwise receive this loopback request.
        response = httpx.get(
            base_url(port) + HEALTH_PATH, timeout=timeout, headers=headers, trust_env=False
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("service") == "catia-mcp" else None


def acquire_instance_lock(port: int) -> Any | None:
    """Hold a per-user, per-port lock for the life of the process, or return
    None when another serve process holds it (logon autostart and a proxy
    can race to start the service)."""
    if os.name == "nt":
        try:
            import win32api
            import win32event
            import winerror
        except ImportError:
            return object()
        handle = win32event.CreateMutex(None, False, f"Local\\catia-mcp-{port}")
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            return None
        return handle
    import fcntl

    lock = open(state_dir() / f"serve-{port}.lock", "w")  # noqa: SIM115 -- process lifetime
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


def pid_file() -> Path:
    return state_dir() / "server.json"


def write_pid_file(port: int) -> None:
    pid_file().write_text(
        json.dumps({"pid": os.getpid(), "port": port, "version": __version__}), encoding="utf-8"
    )


def clear_pid_file() -> None:
    try:
        data = json.loads(pid_file().read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid():
            pid_file().unlink()
    except (OSError, ValueError):
        pass


def _pythonw() -> str:
    """The windowless interpreter next to the running one (Windows)."""
    exe = Path(sys.executable)
    if os.name == "nt":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def serve_command(port: int = DEFAULT_PORT) -> list[str]:
    return [_pythonw(), "-m", "catia_mcp", "serve", "--port", str(port)]


def start_log_path() -> Path:
    return state_dir() / "service-start.log"


def start_detached(port: int = DEFAULT_PORT) -> subprocess.Popen:
    flags = 0
    if os.name == "nt":
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    # The child's own stdout/stderr go to a file: a crash before logging is
    # configured (import error, bad DLL) would otherwise leave no trace.
    with open(start_log_path(), "wb") as console:
        return subprocess.Popen(
            serve_command(port),
            stdin=subprocess.DEVNULL,
            stdout=console,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
            # Not the caller's directory: `-m` puts the working directory first
            # on sys.path, where a checkout or stray .py file would shadow modules.
            cwd=str(state_dir()),
            start_new_session=os.name != "nt",
        )


def _tail(path: Path, lines: int = 25) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no file)"
    return "\n".join(text.splitlines()[-lines:]) or "(empty)"


def start_diagnostics() -> str:
    return (
        f"--- {start_log_path()} ---\n{_tail(start_log_path())}\n"
        f"--- {log_path()} (last lines) ---\n{_tail(log_path(), 15)}"
    )


# The first start after an install compiles every module while antivirus
# scans each file, which can take far longer than later starts.
START_WAIT_S = 120.0


def ensure_running(port: int = DEFAULT_PORT, wait: float = START_WAIT_S) -> dict:
    """Return the service's health, starting it first if nothing answers."""
    status = health(port)
    if status is not None:
        return status
    process = start_detached(port)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.25)
        status = health(port, timeout=1.0)
        if status is not None:
            return status
        code = process.poll()
        if code is not None:
            status = health(port, timeout=1.0)  # lost a start race to another instance
            if status is not None:
                return status
            raise RuntimeError(
                f"The CATIA MCP service exited during startup (exit code {code}).\n"
                + start_diagnostics()
            )
    raise RuntimeError(
        f"The CATIA MCP service did not answer on port {port} within {wait:.0f} s "
        f"(still starting: PID {process.pid}).\n" + start_diagnostics()
    )


def stop(port: int = DEFAULT_PORT) -> str:
    """Stop the service on `port`. Only the PID the live service reports in
    /health is killed: a PID file left by a crash or a logoff may name an
    unrelated process by now (CATIA itself, with unsaved work)."""
    live = health(port)
    try:
        pid_file().unlink()
    except OSError:
        pass
    if live is None:
        return "The CATIA MCP service is not running."
    pid = int(live["pid"])
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False
        )
    else:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(40):
        if health(port, timeout=0.5) is None:
            break
        time.sleep(0.25)
    return f"Stopped the CATIA MCP service (PID {pid})."


# ── Autostart (HKCU Run key) ──


def _run_command_line(port: int) -> str:
    return subprocess.list2cmdline(serve_command(port))


def set_autostart(enabled: bool, port: int = DEFAULT_PORT) -> str:
    if os.name != "nt":
        return "Autostart is only configured on Windows; skipped."
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _run_command_line(port))
            return f"Starts at logon (HKCU Run '{RUN_VALUE}')."
        try:
            winreg.DeleteValue(key, RUN_VALUE)
        except FileNotFoundError:
            return "Autostart was not registered."
        return "Autostart removed."


def autostart_command() -> str | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, RUN_VALUE)[0])
    except OSError:
        return None


# ── Claude client configuration ──


def claude_desktop_configs() -> list[Path]:
    """claude_desktop_config.json files to write for Claude Desktop.

    On Windows the classic location %APPDATA%\\Claude is always included
    (created if needed): the installer must not depend on Claude Desktop
    having run before, and a Microsoft Store (MSIX) build reads it too when
    it has no private copy. Every Store package whose name contains
    "Claude" adds its private LocalCache copy (live laptop: the package was
    not named Claude_*, so nothing was written)."""
    found: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata and (sys.platform == "win32" or Path(appdata, "Claude").is_dir()):
        found.append(Path(appdata, "Claude", "claude_desktop_config.json"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        packages = Path(local, "Packages")
        if packages.is_dir():
            for package in sorted(packages.glob("*Claude*")):
                roaming = package / "LocalCache" / "Roaming" / "Claude"
                if roaming.is_dir():
                    found.append(roaming / "claude_desktop_config.json")
    if sys.platform == "darwin":
        mac = Path.home() / "Library" / "Application Support" / "Claude"
        if mac.is_dir():
            found.append(mac / "claude_desktop_config.json")
    return found


def proxy_entry(port: int = DEFAULT_PORT) -> dict[str, Any]:
    args = ["-m", "catia_mcp", "proxy"]
    if port != DEFAULT_PORT:
        args += ["--port", str(port)]
    # python.exe, not pythonw: the client talks to the proxy over stdio.
    return {"command": sys.executable, "args": args}


def update_desktop_config(path: Path, entry: dict[str, Any] | None) -> str:
    """Add (entry) or remove (None) the 'catia' server in a Claude Desktop
    config, keeping everything else and a .bak copy of the original."""
    data: dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8-sig")  # PowerShell 5.1 writes a BOM
        if text.strip():
            try:
                data = json.loads(text)
            except ValueError:
                return f"{path}: not valid JSON, left unchanged. Add the entry by hand."
        shutil.copyfile(path, path.with_suffix(".json.bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    servers = data.setdefault("mcpServers", {})
    if entry is None:
        if servers.pop(SERVER_NAME, None) is None:
            return f"{path}: no '{SERVER_NAME}' entry."
    else:
        servers[SERVER_NAME] = entry
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    action = "removed from" if entry is None else "configured in"
    return f"'{SERVER_NAME}' {action} {path} (restart Claude Desktop to load it)."


def configure_claude_code(port: int, remove: bool = False) -> str:
    claude = shutil.which("claude")
    if claude is None:
        if remove:
            return "Claude Code CLI not found; nothing to remove."
        return (
            "Claude Code CLI not found. If you use it later, run:\n"
            f"    claude mcp add --scope user --transport http {SERVER_NAME} {mcp_url(port)}"
        )
    subprocess.run(
        [claude, "mcp", "remove", "--scope", "user", SERVER_NAME],
        capture_output=True,
        check=False,
    )
    if remove:
        return f"Removed '{SERVER_NAME}' from Claude Code."
    done = subprocess.run(
        [claude, "mcp", "add", "--scope", "user", "--transport", "http", SERVER_NAME, mcp_url(port)],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        return f"Claude Code: 'claude mcp add' failed: {(done.stderr or done.stdout).strip()}"
    return f"Claude Code: '{SERVER_NAME}' -> {mcp_url(port)} (user scope)."


# ── Commands ──


def install(port: int = DEFAULT_PORT, configure_clients: bool = True) -> list[str]:
    lines = [f"CATIA MCP server {__version__} ({sys.executable})"]
    lines.append(set_autostart(True, port))
    if health(port) is not None:
        lines.append(stop(port))  # restart onto the freshly installed code
    try:
        status = ensure_running(port)
        lines.append(f"Service running: {mcp_url(port)} (PID {status.get('pid')}).")
    except RuntimeError as error:
        # Configure the clients anyway: their proxy starts the service on
        # first use, and the error below says why it is not up yet.
        lines.append(f"SERVICE NOT RUNNING: {error}")
    if configure_clients:
        configs = claude_desktop_configs()
        if not configs:
            lines.append("Claude Desktop not found; skipped.")
        for path in configs:
            lines.append(update_desktop_config(path, proxy_entry(port)))
        lines.append(configure_claude_code(port))
    lines.append(f"Logs: {log_path()}")
    return lines


def uninstall(port: int = DEFAULT_PORT) -> list[str]:
    lines = [set_autostart(False, port), stop(port)]
    for path in claude_desktop_configs():
        if path.exists():
            lines.append(update_desktop_config(path, None))
    lines.append(configure_claude_code(port, remove=True))
    return lines


def status_lines(port: int = DEFAULT_PORT) -> list[str]:
    data = health(port)
    autostart = autostart_command()
    lines = []
    if data is None:
        lines.append(f"Service: NOT running on {mcp_url(port)}.")
    else:
        catia = data.get("catia", {})
        lines.append(
            f"Service: running (PID {data.get('pid')}, version {data.get('version')}, "
            f"up {data.get('uptime_s', 0):.0f} s) at {mcp_url(port)}"
        )
        lines.append(
            "CATIA: "
            + (
                f"connected ({catia.get('platform')}, {catia.get('caption')})"
                if catia.get("connected")
                else "not connected yet (attaches on the next tool call)"
            )
        )
        busy = data.get("busy")
        if busy:
            lines.append(f"Busy: {busy['tool']} for {busy['seconds']:.0f} s")
        lines.append(f"Tool calls served: {data.get('calls', 0)}")
        lines.append(
            f"Commit: {data.get('commit') or 'unknown'}; scripts "
            f"{'on' if data.get('scripts') else 'off'}; toolset {data.get('toolset')}"
        )
    if os.name == "nt":
        lines.append(f"Autostart: {'on' if autostart else 'off'}")
    lines.append(f"Logs: {log_path()}")
    return lines

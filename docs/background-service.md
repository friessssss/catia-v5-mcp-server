# Background service

Since 0.3.0 the server can run as an always-on local service, the way
Autodesk's Fusion MCP is always there while Fusion runs: install once, and
every MCP client on the machine (Claude Code, Claude Desktop, Cursor, ...)
talks to one shared CATIA connection at `http://127.0.0.1:8765/mcp`. CATIA
does not have to be running when the service starts; it attaches on the
first tool call after you start CATIA (and log in, on 3DEXPERIENCE).

```
Claude Code ──HTTP──────────────────────────┐
Cursor / VS Code ──HTTP─────────────────────┤
Claude Desktop ──stdio── catia-mcp proxy ───┤──> catia-mcp serve ──COM──> CATIA
                                            │    127.0.0.1:8765
                                   /health ─┘    (one STA worker, one connection)
```

## Install

On the Windows machine that runs CATIA, in PowerShell (no admin rights):

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/main/scripts/install.ps1 | iex"
```

The installer ([scripts/install.ps1](../scripts/install.ps1)):

1. installs [uv](https://docs.astral.sh/uv/) into your profile if it is missing;
2. stops a running service, then installs the server with a private Python
   3.13: `uv tool install --force --python 3.13 "catia-v5-mcp-server[images] @ git+https://github.com/friessssss/catia-v5-mcp-server@main"`
   (from the GitHub source archive when `git` is not installed);
3. runs `catia-mcp install`;
4. prints `catia-mcp status`.

Re-run it to upgrade. Options (pass them through `scripts\install.cmd` from
a checkout, or with the scriptblock form below when piping from the web):

| Option | Meaning |
|--------|---------|
| `-Branch NAME` | Install a branch, tag or commit other than `main` (also `$env:CATIA_MCP_BRANCH`) |
| `-Port N` | Service port (default 8765) |
| `-NoClients` | Do not touch the Claude Desktop / Claude Code configuration |
| `-Python 3.12` | Python version for the tool environment |
| `-Uninstall` | Remove the service, autostart, client entries and the tool |

```powershell
# A test branch, straight from GitHub:
powershell -ExecutionPolicy Bypass -c "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/main/scripts/install.ps1))) -Branch my-branch"
# A branch whose installer is not on main yet: fetch the script from that
# branch too (replace BRANCH in both places; slashes in the name are fine):
powershell -ExecutionPolicy Bypass -c "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/BRANCH/scripts/install.ps1))) -Branch BRANCH"
# From a checkout:
scripts\install.cmd -Branch my-branch
scripts\install.cmd -Uninstall
```

Where things land (all per user):

| What | Where |
|------|-------|
| `catia-mcp.exe`, `catia-mcpw.exe` | `%USERPROFILE%\.local\bin` |
| The tool's Python (`sys.executable`) | `<uv tool dir>\catia-v5-mcp-server\Scripts\python.exe` (usually `%APPDATA%\uv\tools\...`; `uv tool dir` prints it) |
| Logs, PID file, captures, API-doc cache | `%LOCALAPPDATA%\catia-mcp` (override: `CATIA_MCP_HOME`) |
| Autostart | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value `CATIA MCP Server` |

Configs written by `catia-mcp install` use absolute paths to that Python,
so they keep working whatever the PATH is.

### What `catia-mcp install` does

- Registers the logon autostart: `pythonw.exe -m catia_mcp serve --port 8765`
  (windowless; no console pops up).
- Stops a service that is already running (so the new code is loaded) and
  starts it now.
- Adds a `catia` server to every Claude Desktop config it finds (classic
  installer and Microsoft Store/MSIX), running `python.exe -m catia_mcp proxy`.
  The previous file is kept as `claude_desktop_config.json.bak`; other servers
  are left untouched.
- If the Claude Code CLI is installed, runs
  `claude mcp add --scope user --transport http catia http://127.0.0.1:8765/mcp`.
- `--no-clients` skips both client steps; `--port N` changes the port
  everywhere (autostart, proxy entry, Claude Code URL).

Restart Claude Desktop afterwards (quit it from the tray icon, not just the
window).


## Updating

```powershell
catia-mcp update            # newest commit of the branch this copy was installed from
catia-mcp update --check    # only compare the installed commit with GitHub
catia-mcp update --branch main
```

Windows locks the files of a running `catia-mcp.exe`, so `update` cannot
reinstall itself: it starts `scripts/install.ps1` for the same repository
and branch (read from the install's `direct_url.json`) in a new PowerShell
window and exits. The installer stops the service and any copies Claude
Desktop started, reinstalls, restarts the service and rewrites the client
configuration. A running Claude Desktop is closed before the reinstall
(it holds the old `catia-mcp proxy`) and reopened at the end, also when the
install fails; only the Desktop app is matched by its install folder, never
the Claude Code CLI (also `claude.exe`). `install.ps1 -NoRestartClaude`
skips this.

## Why a per-user process and not a Windows Service

CATIA registers its COM server in the Running Object Table of the logged-in
user's interactive session. A real Windows Service runs in session 0 and
cannot see that table, so it could never attach to the CATIA you are looking
at (and could not take screenshots of its window). The service is therefore
an ordinary windowless process started at logon in your own session, with
your rights. It needs no administrator rights, and it stops when you log off.

## Commands

| Command | What it does |
|---------|--------------|
| `catia-mcp install [--port N] [--no-clients]` | Autostart + start now + client configuration |
| `catia-mcp status` | Is it running, PID, version, commit, uptime, CATIA connection, current busy call, autostart, log path |
| `catia-mcp start` | Start the service in the background if it is not running |
| `catia-mcp stop` | Stop it (it comes back at next logon, or on the next proxy call) |
| `catia-mcp serve` | Run the service in the foreground (logs to the console too); `--http` is the same |
| `catia-mcp proxy` | stdio bridge to the service; what Claude Desktop runs |
| `catia-mcp uninstall` | Stop, remove autostart and client entries (the tool stays; `uv tool uninstall catia-v5-mcp-server` removes it) |
| `catia-mcp` | No command: classic stdio server in the client's process, as before 0.3.0 |

`catia-mcpw` is the same program without a console window (useful in a
shortcut: `catia-mcpw serve`).

## Health, logs and versions

- `http://127.0.0.1:8765/health` returns JSON: `service` (`catia-mcp`),
  `version`, `commit` (the git commit installed, when installed from git),
  `pid`, `uptime_s`, `tools`, `calls`, `busy` (the tool currently running and
  for how long), `catia` (`connected`, `platform`, `caption`), `scripts`,
  `toolset` and `log`. It never touches COM, so it answers even while CATIA
  is blocked.
- Log: `%LOCALAPPDATA%\catia-mcp\catia_mcp.log` (rotated at 5 MB, 3 kept).
  Every tool call, result code and traceback is there. Stray console output
  of a windowless process goes to `stdio.log` next to it.
- `catia-mcp status` prints the commit: compare it with the latest commit on
  the branch to confirm a tester runs the latest push.

## Proxy and stateless HTTP

The HTTP endpoint is stateless with plain JSON responses: every request
stands alone, so restarting the service (or CATIA) never strands a client on
an expired session. `catia-mcp proxy` reads newline-delimited JSON-RPC on
stdin, POSTs each message to `/mcp` and writes the reply on stdout. It starts
the service when nothing answers, so Claude Desktop works even before the
first logon autostart. Requests are relayed concurrently, so a ping is
answered while a long CATIA call runs.

The proxy is for clients on the same machine. A remote Claude Desktop uses
`mcp-remote` instead (see [remote-access.md](remote-access.md)).

## Timeouts, busy CATIA and dialogs

All COM calls run on one worker thread, one at a time.

- `CATIA_MCP_CALL_TIMEOUT` (default 600 s): a call still running after this
  returns `CATIA_TIMEOUT` to the client. The COM call itself cannot be
  cancelled; it keeps running in CATIA and may still complete, so inspect
  the model before repeating a write.
- `CATIA_MCP_BUSY_GRACE` (default 30 s): while the worker has been stuck on
  one call for longer than this, new calls fail at once with `CATIA_BUSY`
  (`effects.modified=false`) instead of queueing behind it.
- Both errors name the open CATIA dialog windows when they can find them
  (a modal dialog is the usual cause). Close the dialog in CATIA; the stuck
  call then finishes and the service carries on.
- `CATIA_NOT_RUNNING`: CATIA is not started (or not logged in, on
  3DEXPERIENCE) or closed during a call. Start it and retry; the service
  reattaches by itself. By default the service only attaches to a running
  CATIA; set `CATIA_MCP_LAUNCH=1` to let a tool call launch CATIA V5.

## Script tools

`catia_run_python` (Python in the server with the live COM objects, like
Fusion's script execution) and `catia_run_script` (VBScript inside CATIA)
can do anything you can do in CATIA. Policy:

- On for stdio and for a service listening on loopback only (the default).
- Off when the service listens beyond this machine (`--host 0.0.0.0`),
  unless started with `--allow-scripts`.
- `--no-scripts` or `CATIA_MCP_ALLOW_SCRIPTS=0` turns them off;
  `CATIA_MCP_ALLOW_SCRIPTS=1` turns them on.

## Environment variables

Set them as user environment variables (System Properties > Environment
Variables, or `setx NAME value`), then `catia-mcp stop` and `catia-mcp start`
so the service picks them up.

| Variable | Default | Meaning |
|----------|---------|---------|
| `CATIA_MCP_LAUNCH` | off | `1`: a tool call may launch CATIA V5 when none runs |
| `CATIA_MCP_CALL_TIMEOUT` | 600 | Seconds before a call returns `CATIA_TIMEOUT` |
| `CATIA_MCP_BUSY_GRACE` | 30 | Seconds of a stuck call before new calls get `CATIA_BUSY` |
| `CATIA_MCP_ALLOW_SCRIPTS` | see above | `1`/`0` forces the script tools on/off |
| `CATIA_MCP_PLATFORM` | auto | `v5` or `3dx` to skip detection |
| `CATIA_MCP_DEFAULT_PLM_TYPE` | auto | 3DEXPERIENCE: `3DShape` to skip the 3D Part attempt in `catia_new_part` |
| `CATIA_MCP_TOOLSET` | full | `core`: list only the Fusion-style core tools (the rest stay callable by name) |
| `CATIA_MCP_HOME` | `%LOCALAPPDATA%\catia-mcp` | State and log directory |
| `CATIA_MCP_TOKEN` | none | Bearer token (required when listening beyond loopback) |

## Troubleshooting

**`catia-mcp` is not recognized.** uv puts it in `%USERPROFILE%\.local\bin`,
which the uv installer adds to your user PATH; terminals opened before the
install do not see it. Open a new terminal, or run
`$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"`, or call it by full
path. `uv tool update-shell` repairs the PATH entry.

**Port 8765 is in use.** `catia-mcp status` says NOT running but the service
fails to start, and the log shows `address already in use`. Find the owner
with `netstat -ano | findstr :8765` (last column is the PID). Either stop
it, or reinstall on another port: `catia-mcp install --port 8766` (pass the
same `--port` to `status`, `stop` and `start`).

**Claude Desktop does not show the `catia` tools.**
- Quit Claude Desktop completely (tray icon > Quit) and start it again.
- The Microsoft Store/MSIX build reads
  `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`,
  not `%APPDATA%\Claude\...`; `catia-mcp install` writes both when they
  exist. If Claude Desktop was installed after the server, run
  `catia-mcp install` again.
- Claude Desktop's own log (`%APPDATA%\Claude\logs\mcp-server-catia.log`)
  shows the proxy's errors.

**The installer fails in `uv tool install`.**
- *Access is denied* / *file in use*: a process of the old install still
  runs (the service, or Claude Desktop's proxy). Quit Claude Desktop, run
  `catia-mcp stop`, and re-run the installer.
- TLS or certificate errors behind a corporate proxy: the installer sets
  `UV_NATIVE_TLS=1` so uv trusts the Windows certificate store; set
  `HTTPS_PROXY` if your network needs an explicit proxy.
- Python download blocked: pass `-Python 3.12` (or any version already
  installed; `uv python list` shows what uv can see).

**Tools fail with `CATIA_NOT_RUNNING`.** Start CATIA (3DEXPERIENCE: from the
launcher, and log in). With both V5 and 3DEXPERIENCE installed,
`CATIA.Application` points to whichever was registered last.

**Everything returns `CATIA_BUSY`.** Look at the CATIA window for a dialog
and close it. `catia-mcp status` shows which call is stuck and for how long.
If CATIA itself hangs, close it; the service recovers when CATIA comes back.

**The service starts but dies.** Read `%LOCALAPPDATA%\catia-mcp\catia_mcp.log`
and `stdio.log`. Run `catia-mcp serve` in a terminal to see the error live.

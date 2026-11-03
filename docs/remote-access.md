# Remote access: agent on a Mac, CATIA on Windows

CATIA V5 only runs on Windows, and the default stdio transport needs the
agent on that same machine. The HTTP transport lifts that limit: the server
runs next to CATIA, and Claude Code, Cursor, VS Code or Claude Desktop
connect to it from a Mac, another PC, or while CATIA runs on a cloud
workstation (Vagon, Shadow, a GPU VM).

```
Mac / laptop                      Windows machine (local or cloud)
AI client  --HTTP + token-->      catia-mcp serve  --COM-->  CATIA
```

## 1. On the Windows machine

Install the server as usual ([background-service.md](background-service.md)):

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/main/scripts/install.ps1 | iex"
```

The installed background service listens on `127.0.0.1:8765` only. Two ways
to reach it from another machine:

- **SSH tunnel (no token, nothing to change on Windows):** keep the installed
  service as it is and tunnel to it (section 2). The service still only
  listens on loopback.
- **Listen on the network (token required):** stop the loopback service and
  run one bound to all interfaces with a token:

  ```bat
  catia-mcp stop
  catia-mcp --new-token
  set CATIA_MCP_TOKEN=<paste the token>
  catia-mcp serve --host 0.0.0.0 --port 8765
  ```

  This runs in the foreground; keep the window open. The logon autostart
  still starts the loopback-only service at the next logon; run
  `catia-mcp install --no-clients` again to go back to it, or
  `catia-mcp uninstall` to remove the autostart.

`python -m catia_mcp serve ...` works the same if the `catia-mcp` command is
not on the PATH.

- Without `--host`, the server only listens on `127.0.0.1` (same machine).
- On any other address it refuses to start without a token of at least
  16 characters. Every request must send `Authorization: Bearer <token>`.
- The script tools (`catia_run_python`, `catia_run_script`) are **off** on a
  server listening beyond loopback unless you add `--allow-scripts`: anyone
  with the token could then run code as you on that machine.
- `catia-mcp status` does not send the token, so it reports a
  token-protected service as not running; check it with
  `curl -H "Authorization: Bearer <token>" http://127.0.0.1:8765/health`.
- Allow the port in the Windows firewall if Windows asks.
- Keep CATIA open and visible in that Windows session: the server drives
  the running CATIA, it does not launch a hidden one.

## 2. Connect the two machines privately

The transport is plain HTTP. Never expose the port directly on the
internet; anyone holding the URL and the token would drive your CATIA.

- **Recommended: Tailscale** (free for personal use). Install it on both
  machines, sign in with the same account, and use the Windows machine's
  Tailscale name or `100.x.y.z` address. Works the same for a cloud PC.
- **SSH tunnel**: with the installed loopback service (or `catia-mcp serve`
  without `--host`), run `ssh -L 8765:127.0.0.1:8765 user@windows-host` from
  the Mac (Windows needs the OpenSSH server feature). The URL is then
  `http://127.0.0.1:8765/mcp` on the Mac, and no token is needed.
- **Same office network**: the Windows machine's LAN IP works, with the token.

## 3. Configure the client (on the Mac)

Replace `HOST` with the Tailscale name, IP, or `127.0.0.1` for a tunnel.

**Claude Code**

```bash
claude mcp add --scope user --transport http catia http://HOST:8765/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

**Cursor** (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "catia": {
      "url": "http://HOST:8765/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`)

```json
{
  "servers": {
    "catia": {
      "type": "http",
      "url": "http://HOST:8765/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

**Claude Desktop** reads only local (stdio) servers from its config file.
On the CATIA machine itself, `catia-mcp install` configures it with
`catia-mcp proxy`, a stdio bridge to the local service; that bridge only
reaches `127.0.0.1` (`--port` picks the port). From another machine, Claude
Desktop goes through the `mcp-remote` bridge instead (needs Node.js on the
Mac):

```json
{
  "mcpServers": {
    "catia": {
      "command": "npx",
      "args": [
        "mcp-remote", "http://HOST:8765/mcp",
        "--allow-http",
        "--header", "Authorization:Bearer ${CATIA_MCP_TOKEN}"
      ],
      "env": { "CATIA_MCP_TOKEN": "YOUR_TOKEN" }
    }
  }
}
```

`--allow-http` is required by `mcp-remote` for a non-HTTPS URL; keep it to a
private network as described above.

## Limits

- One CATIA session serves every connected client; calls are serialized on
  one worker, so two agents never drive CATIA at the same time.
- No TLS in the server itself. Use a private network, or put a TLS reverse
  proxy (Caddy, nginx) in front if you need HTTPS.
- Web chat connectors (claude.ai, ChatGPT) need a public HTTPS URL with
  OAuth, which this transport does not provide; use a desktop or CLI client.
- Exports are written on the Windows machine's disk, at the paths the tools
  receive. `catia_screenshot` also returns the picture itself to the client,
  so the agent sees it wherever it runs.

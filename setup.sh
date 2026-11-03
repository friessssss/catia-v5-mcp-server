#!/bin/bash
# ============================================================================
#  CATIA MCP Server - setup.sh (DEPRECATED)
#
#  The supported installer is scripts/install.ps1 (Windows PowerShell, no admin
#  rights): it installs the server with uv, registers the always-on background
#  service and configures Claude Desktop / Claude Code. From PowerShell:
#
#    powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/main/scripts/install.ps1 | iex"
#
#  This wrapper is kept for Git Bash / MSYS2 users. From a checkout it installs
#  THIS working tree (editable, so local edits apply after `catia-mcp stop`),
#  then runs `catia-mcp install`. Elsewhere it prints the instructions above.
# ============================================================================

set -euo pipefail

CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONE_LINER='powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/main/scripts/install.ps1 | iex"'

echo -e "${YELLOW}setup.sh is deprecated; the installer is scripts/install.ps1.${NC}"

case "${OSTYPE:-}" in
    msys*|cygwin*|win32*) ;;
    *)
        echo -e "${RED}The CATIA MCP server runs on the Windows machine that runs CATIA.${NC}"
        echo "On that machine, open PowerShell and run:"
        echo -e "  ${CYAN}${ONE_LINER}${NC}"
        echo "To drive CATIA from this machine, see docs/remote-access.md."
        exit 1
        ;;
esac

if ! command -v uv >/dev/null 2>&1 && [[ -x "$HOME/.local/bin/uv" || -x "$HOME/.local/bin/uv.exe" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
fi

if [[ -f "$HERE/pyproject.toml" ]] && command -v uv >/dev/null 2>&1; then
    echo -e "${CYAN}Installing this checkout ($HERE) with uv...${NC}"
    # Stop a running service first: Windows will not replace a tool
    # environment whose python is in use.
    if command -v catia-mcp >/dev/null 2>&1; then
        catia-mcp stop || true
    fi
    # uv is a Windows program: give it a C:/... path, not Git Bash's /c/...
    SRC="$(cygpath -m "$HERE" 2>/dev/null || echo "$HERE")"
    uv tool install --force --python 3.13 --editable "catia-v5-mcp-server[images] @ ${SRC}"
    export PATH="$HOME/.local/bin:$PATH"
    catia-mcp install
    catia-mcp status
    exit 0
fi

if command -v powershell.exe >/dev/null 2>&1; then
    echo -e "${CYAN}Running scripts/install.ps1 (installs uv, then the server from GitHub)...${NC}"
    if [[ -f "$HERE/scripts/install.ps1" ]]; then
        WIN_PS1="$(cygpath -w "$HERE/scripts/install.ps1" 2>/dev/null || echo "$HERE/scripts/install.ps1")"
        exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$WIN_PS1" "$@"
    fi
    exec powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \
        "irm https://raw.githubusercontent.com/friessssss/catia-v5-mcp-server/main/scripts/install.ps1 | iex"
fi

echo "Open PowerShell and run:"
echo -e "  ${CYAN}${ONE_LINER}${NC}"
exit 1

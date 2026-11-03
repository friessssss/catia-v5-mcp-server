"""Every test gets its own state directory: logs, PLM refusals, captures and
API-doc caches must never leak between tests or into the developer's home."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CATIA_MCP_HOME", str(tmp_path / "catia-mcp-home"))

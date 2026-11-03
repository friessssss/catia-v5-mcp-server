"""Live smoke test against a running CATIA V5 or CATIA 3DEXPERIENCE session.

Runs the MCP tools in-process (exactly as an MCP client would call them) on a
throw-away part and checks measurable postconditions against known geometry:
a 100 x 60 x 40 mm block must measure 240000 mm3 and a 100/60/40 bounding
box. That check also settles the volume/area unit convention on
3DEXPERIENCE, which offline tests cannot establish.

Nothing is saved unless --save is passed (on 3DEXPERIENCE, saving writes to
the shared 3DSpace database). The part is left open for inspection.

With --url the same steps go through the running background service
(`catia-mcp serve`, installed by `catia-mcp install`) over Streamable HTTP,
exactly like Claude Code reaches it; that exercises the service, its
timeouts and the image transport end to end.

Steps marked EYES ran without error but their effect cannot be checked from
the tool result (undo/redo, view orientation): look at the CATIA window and
note in the report whether it did what the step says.

Usage (Windows, CATIA started and, for 3DEXPERIENCE, logged in):
    python scripts/live_smoke_test.py
    python scripts/live_smoke_test.py --drawing --report smoke_report.json
    python scripts/live_smoke_test.py --drawing --plm-type 3DShape   (no 3D Part licence)
    python scripts/live_smoke_test.py --url http://127.0.0.1:8765/mcp  (via the service)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

BLOCK = (100.0, 60.0, 40.0)


class _Loop:
    """One event loop on a background thread for the whole run: the HTTP
    client session must live on a single loop, and the in-process server's
    worker thread is reused across calls."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro: Awaitable[Any], timeout: float | None = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


class InProcess:
    """Tools called on a CATIAMCPServer built in this process."""

    mode = "in-process"

    def __init__(self, platform: str) -> None:
        from catia_mcp.server import CATIAMCPServer

        self.server = CATIAMCPServer(platform, allow_scripts=True)

    async def open(self) -> None:
        return None

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        return await self.server.call_tool(name, args)

    def is_3dx(self, _connect_text: str) -> bool:
        return bool(self.server.connection.is_3dx)

    def platform(self, _connect_text: str) -> str:
        return self.server.connection.platform_label

    async def close(self) -> None:
        await self.server.close()


class OverHttp:
    """Tools called through the background service (MCP Streamable HTTP).

    The MCP client's context managers use anyio cancel scopes, which must be
    entered and exited in the same task, so one long-lived task owns the
    session and serves calls from a queue."""

    mode = "http"

    def __init__(self, url: str, token: str | None) -> None:
        self.url = url
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._queue: asyncio.Queue[tuple[str, dict[str, Any], asyncio.Future] | None] | None = None
        self._task: asyncio.Task | None = None

    async def open(self) -> None:
        self._queue = asyncio.Queue()
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._serve(ready))
        await ready

    async def _serve(self, ready: asyncio.Future) -> None:
        import httpx
        from mcp import ClientSession

        try:
            async with contextlib.AsyncExitStack() as stack:
                try:  # mcp >= 1.28 renamed the client and takes an httpx client
                    from mcp.client.streamable_http import streamable_http_client

                    # No read timeout: a CATIA call may legitimately take
                    # minutes; the service's CATIA_MCP_CALL_TIMEOUT bounds it.
                    client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers=self.headers, timeout=httpx.Timeout(None, connect=10.0)
                        )
                    )
                    streams = await stack.enter_async_context(
                        streamable_http_client(self.url, http_client=client)
                    )
                except ImportError:  # older SDKs
                    from mcp.client.streamable_http import streamablehttp_client

                    streams = await stack.enter_async_context(
                        streamablehttp_client(self.url, headers=self.headers)
                    )
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                info = await session.initialize()
                server = getattr(info, "serverInfo", None)
                print(
                    f"Connected to {self.url}: {getattr(server, 'name', '?')} "
                    f"{getattr(server, 'version', '?')}"
                )
                ready.set_result(None)
                assert self._queue is not None
                while (item := await self._queue.get()) is not None:
                    name, args, future = item
                    try:
                        future.set_result(await session.call_tool(name, args))
                    except Exception as e:  # noqa: BLE001 -- handed to the caller
                        future.set_exception(e)
                    except BaseException:
                        # The session's task group was cancelled (service went
                        # away mid-call): resolve the waiting caller, whose
                        # result() has no timeout, before unwinding.
                        future.set_exception(RuntimeError("the HTTP client session ended"))
                        raise
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(str(e)))
            else:
                raise

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        assert self._queue is not None and self._task is not None
        if self._task.done():
            raise RuntimeError("the HTTP client session has ended")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((name, args, future))
        return await future

    def is_3dx(self, connect_text: str) -> bool:
        return "3DEXPERIENCE" in connect_text

    def platform(self, connect_text: str) -> str:
        if "3DEXPERIENCE" in connect_text:
            return "CATIA 3DEXPERIENCE"
        return "CATIA V5" if "V5" in connect_text else "unknown"

    async def close(self) -> None:
        if self._queue is not None and self._task is not None and not self._task.done():
            await self._queue.put(None)
            await self._task


def _text_of(result: Any) -> str:
    parts = [getattr(item, "text", "") for item in (result.content or [])]
    return "\n".join(p for p in parts if p)


def _images_of(result: Any) -> list[Any]:
    return [item for item in (result.content or []) if getattr(item, "type", None) == "image"]


class Runner:
    def __init__(self, backend: InProcess | OverHttp, loop: _Loop) -> None:
        self.backend = backend
        self.loop = loop
        self.rows: list[dict[str, Any]] = []

    def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        check: Callable[[str], str | None] | None = None,
        allow_unsupported: bool = False,
        check_result: Callable[[Any], str | None] | None = None,
        eyes: str | None = None,
    ) -> tuple[bool, str]:
        """Run one tool and record a row. Never raises: a transport or
        server exception is a FAIL row, so one broken step cannot end the
        run. `check` gets the text, `check_result` the whole CallToolResult
        (for image content). `eyes` marks a step whose effect only a human
        can confirm in the CATIA window."""
        started = time.perf_counter()
        code = None
        try:
            result = self.loop.run(self.backend.call(name, args or {}))
        except Exception as e:  # noqa: BLE001 -- recorded, the run goes on
            result = None
            text = f"{type(e).__name__}: {e}"
            ok = False
        else:
            text = _text_of(result)
            ok = not result.isError
            if result.isError:
                code = (result.structuredContent or {}).get("code")
                if code is None:
                    match = re.search(r'"code": "([A-Z_]+)"', text)
                    code = match.group(1) if match else None
        note = ""
        if ok and (check is not None or check_result is not None):
            try:
                if check is not None:
                    note = check(text) or ""
                if check_result is not None:
                    note = " ".join(filter(None, [note, check_result(result) or ""]))
            except AssertionError as e:
                ok, note = False, f"postcondition failed: {e}"
            except Exception as e:  # noqa: BLE001
                ok, note = False, f"postcondition error: {e}"
        if code == "FEATURE_NO_EFFECT":
            note = "feature had no effect"
        status = "PASS" if ok else "FAIL"
        if not ok and allow_unsupported and code == "UNSUPPORTED_CAPABILITY":
            status, note = "SKIP", "unsupported on this platform (expected)"
        if not ok and code == "UNKNOWN_TOOL":
            status, note = "SKIP", "tool not in this server build"
        if ok and eyes:
            status, note = "EYES", " ".join(filter(None, [note, f"check in CATIA: {eyes}"]))
        row = {
            "tool": name,
            "args": args or {},
            "ok": ok,
            "status": status,
            "seconds": round(time.perf_counter() - started, 2),
            "note": note,
            "output": text[:4000],
        }
        if code:
            row["code"] = code
        if result is not None:
            images = _images_of(result)
            if images:
                row["images"] = [
                    {"mimeType": i.mimeType, "base64_chars": len(i.data or "")} for i in images
                ]
        self.rows.append(row)
        print(f"[{status}] {name} {json.dumps(args or {})[:80]} {note}")
        if status == "FAIL":
            print("       " + text.replace("\n", "\n       ")[:1200])
        return ok, text


def check_volume(text: str) -> str:
    data = json.loads(text)
    volume = data.get("volume_mm3")
    expected = BLOCK[0] * BLOCK[1] * BLOCK[2]
    assert isinstance(volume, (int, float)), f"volume unavailable: {volume}"
    ratio = volume / expected
    assert abs(ratio - 1) < 0.01, (
        f"volume {volume} mm3, expected {expected} (ratio {ratio:g}; 1e-9 or 1e9 "
        "means the unit convention differs on this platform)"
    )
    return f"volume {volume:.1f} mm3 OK"


def check_removed(text: str) -> str:
    assert "Removed" in text, "no material removal reported"
    return "material removed"


def feature_name(text: str, default: str) -> str:
    match = re.search(r"'([^']+)'", text)
    return match.group(1) if match else default


def check_bbox(text: str) -> str:
    dims = json.loads(text)["dimensions"]
    got = sorted([dims["length_x"], dims["length_y"], dims["length_z"]])
    assert all(abs(a - b) < 0.01 for a, b in zip(got, sorted(BLOCK))), f"bbox {got}"
    return f"bbox {got} OK"


def check_distance(expected: float):
    def check(text: str) -> str:
        match = re.search(r": ([-\d.]+) mm", text)
        assert match, "no distance in the result"
        got = float(match.group(1))
        assert abs(got - expected) < 0.01, f"distance {got} mm, expected {expected}"
        return f"distance {got} mm OK"

    return check


def check_faces(minimum: int):
    def check(text: str) -> str:
        assert text.lstrip().startswith("["), text[:200]
        count = len(json.loads(text))
        assert count >= minimum, f"{count} faces, expected at least {minimum}"
        return f"{count} faces"

    return check


def check_image(result: Any) -> str:
    images = _images_of(result)
    assert images, "no image content in the result (expected the screenshot itself)"
    image = images[0]
    mime = getattr(image, "mimeType", "") or ""
    assert mime.startswith("image/"), f"content type {mime!r}"
    raw = base64.b64decode(image.data or "")
    assert len(raw) > 1000, f"image only {len(raw)} bytes"
    return f"{mime} {len(raw) // 1024} KB"


def check_python_result(text: str) -> str:
    data = json.loads(text)
    value = (data.get("data") or {}).get("result")
    assert isinstance(value, str) and value, f"result {value!r}, expected the part name"
    return f"part().Name = {value!r}"


def check_search_hits(text: str) -> str:
    assert "AddNewPad" in text, "no hit mentions AddNewPad"
    return "AddNewPad found"


def summary_table(rows: list[dict[str, Any]]) -> str:
    """One line per tool: how often it passed, failed, was skipped or needs eyes."""
    order: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        if row["tool"] not in counts:
            order.append(row["tool"])
            counts[row["tool"]] = {"PASS": 0, "FAIL": 0, "SKIP": 0, "EYES": 0}
        counts[row["tool"]][row["status"]] += 1
    width = max([len(t) for t in order] + [4])
    lines = [f"{'tool'.ljust(width)}  PASS  FAIL  SKIP  EYES", "-" * (width + 26)]
    for tool in order:
        c = counts[tool]
        flag = "  <-- FAIL" if c["FAIL"] else ""
        lines.append(
            f"{tool.ljust(width)}  {c['PASS']:>4}  {c['FAIL']:>4}  {c['SKIP']:>4}  "
            f"{c['EYES']:>4}{flag}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--platform", default="auto", help="auto, v5 or 3dx (in-process runs only)"
    )
    parser.add_argument("--save", action="store_true", help="save the test part at the end")
    parser.add_argument("--drawing", action="store_true", help="also test drafting tools")
    parser.add_argument(
        "--plm-type",
        choices=["auto", "3DPart", "3DShape"],
        default="auto",
        help=(
            "3DEXPERIENCE only: type of the test part. 3DShape skips the 3D Part attempt, "
            "whose licence refusal shows a modal dialog that blocks the run"
        ),
    )
    parser.add_argument("--report", default="smoke_report.json", help="JSON report path")
    parser.add_argument(
        "--url",
        help=(
            "Call the tools through a running service instead of in-process, e.g. "
            "http://127.0.0.1:8765/mcp (start it with: catia-mcp start)"
        ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CATIA_MCP_TOKEN"),
        help="Bearer token for --url (default: CATIA_MCP_TOKEN)",
    )
    args = parser.parse_args()
    report_path = os.path.abspath(args.report)

    loop = _Loop()
    backend: InProcess | OverHttp = (
        OverHttp(args.url, args.token) if args.url else InProcess(args.platform)
    )
    try:
        loop.run(backend.open(), timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"Cannot reach {args.url}: {type(e).__name__}: {e}")
        print("Is the service running? Check with: catia-mcp status")
        loop.stop()
        return 2
    run = Runner(backend, loop)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    connect_text = ""
    is_3dx = False
    platform = "unknown"
    try:
        ok, connect_text = run.call("catia_connect")
        if not ok:
            return 2
        is_3dx = backend.is_3dx(connect_text)
        platform = backend.platform(connect_text)
        run.call("catia_status")
        run.call("catia_diagnose")
        part_args = {"name": f"MCP_Smoke_{stamp}"}
        if args.plm_type != "auto":
            part_args["plm_type"] = args.plm_type
        ok, _ = run.call("catia_new_part", part_args)
        if not ok:
            print("Cannot create a part; stopping. See catia_diagnose output above.")
            return 3
        run.call("catia_get_active_document_info")
        run.call("catia_status")
        # Solid: 100 x 60 x 40 block.
        run.call("catia_create_sketch", {"plane": "xy"})
        run.call("catia_sketch_centered_rectangle", {"width": BLOCK[0], "height": BLOCK[1]})
        run.call("catia_close_sketch")
        run.call("catia_pad", {"height": BLOCK[2]})
        run.call("catia_get_inertia", check=check_volume)
        run.call("catia_get_bounding_box", check=check_bbox)
        run.call("catia_list_faces")
        run.call("catia_list_edges")
        run.call("catia_measure_element", {"element": "Face.1"})
        run.call("catia_measure_distance", {"element1": "Face.1", "element2": "Face.2"})

        # Material removal with volume verification, then a circular
        # pattern of it (before any geometrical set exists: V5 refuses
        # circular patterns in parts holding GSD geometry).
        run.call("catia_create_sketch", {"plane": "xy"})
        run.call("catia_sketch_circle", {"cx": 20, "cy": 0, "radius": 4})
        run.call("catia_close_sketch")
        _, pocket_text = run.call("catia_pocket", {"depth": 20}, check=check_removed)
        pocket = feature_name(pocket_text, "Pocket.1")
        run.call("catia_circ_pattern", {"count": 4, "feature_name": pocket})
        run.call("catia_fillet", {"radius": 2, "edge_name": "Edge.1"})
        run.call("catia_list_features")

        # Bodies and booleans.
        run.call("catia_add_body", {"name": "Boss"})
        run.call("catia_create_sketch", {"plane": "zx"})
        run.call("catia_sketch_circle", {"cx": 30, "cy": 20, "radius": 5})
        run.call("catia_close_sketch")
        run.call("catia_pad", {"height": 10})
        run.call("catia_set_active_body")
        run.call("catia_boolean", {"operation": "add", "body_name": "Boss"})
        # Topology must stay reachable when the last feature is a boolean
        # (block 6 + 4 blind pockets x 2 + fillet 1 = 15 faces).
        run.call("catia_list_faces", check=check_faces(15))
        run.call("catia_measure_element", {"element": "Face.1"})

        # GSD.
        run.call("catia_gsd_create_geoset", {"name": "Smoke_GSD"})
        run.call("catia_gsd_point", {"x": 0, "y": 0, "z": 60, "name": "P1"})
        run.call("catia_gsd_point", {"x": 50, "y": 0, "z": 60, "name": "P2"})
        run.call("catia_gsd_line", {"point1": "P1", "point2": "P2", "name": "L1"})
        run.call("catia_gsd_point_on_curve", {"curve": "L1", "ratio": 0.5, "name": "Mid"})
        run.call("catia_gsd_plane_offset", {"base_plane": "xy", "offset": 70, "name": "Top70"})
        run.call("catia_gsd_list_elements")
        run.call("catia_measure_distance", {"element1": "P1", "element2": "P2"},
                 check=check_distance(50.0))
        run.call("catia_measure_distance", {"element1": "P1", "element2": "Top70"},
                 check=check_distance(10.0))

        # Knowledgeware.
        run.call("catia_create_parameter", {"name": "SmokeThickness", "type": "length", "value": 5})
        run.call("catia_get_parameters", {"filter": "SmokeThickness"})

        # View, screenshot, export.
        run.call("catia_set_view", {"view": "isometric"})
        run.call("catia_fit_all")
        shot = os.path.join(tempfile.gettempdir(), f"mcp_smoke_{stamp}.jpg")
        run.call("catia_screenshot", {"file_path": shot}, check_result=check_image)
        # Fusion-parity: the agent sees the model. The picture must come back
        # as MCP image content, not only as a file path.
        run.call("catia_screenshot", {"view": "isometric"}, check_result=check_image)
        run.call(
            "catia_set_view",
            {"view": "front"},
            eyes="the view looks along +Y with Z pointing up (CATIA's Front view)",
        )
        run.call("catia_get_selection")
        step = os.path.join(tempfile.gettempdir(), f"mcp_smoke_{stamp}.stp")
        run.call("catia_export", {"file_path": step}, allow_unsupported=True)

        # API discovery.
        run.call("catia_inspect_api", {"path": "part.ShapeFactory", "filter": "AddNewPad"})
        if is_3dx:
            run.call("catia_get_plm_attributes")
            run.call("catia_inspect_api", {"path": "editor.GetService(\"MeasurableService\")"})
        run.call("catia_run_python", {"code": "result = part().Name"}, check=check_python_result)
        # The first search decompiles the installation's CHM; allow it time.
        run.call("catia_api_search", {"query": "AddNewPad"}, check=check_search_hits)

        # Undo / redo on a throw-away feature. CATIA's Undo is not verified by
        # the tool, so a human confirms the effect.
        run.call("catia_gsd_point", {"x": 0, "y": 0, "z": 90, "name": "UndoProbe"})
        run.call("catia_undo", eyes="point UndoProbe disappeared from the tree")
        run.call("catia_gsd_list_elements")
        run.call("catia_redo", eyes="point UndoProbe is back in the tree")

        if args.drawing:
            part_title = None
            if is_3dx:
                info = json.loads(run.call("catia_get_active_document_info")[1])
                part_title = info.get("title")
            run.call("catia_new_drawing")
            view_args = {"plane": "xy"}
            if part_title:
                view_args["part_name"] = part_title
            # On 3DEXPERIENCE generative views project product instances; a
            # standalone 3D Shape (the fallback without a 3D Part licence) has
            # none, which the tool reports as UNSUPPORTED_CAPABILITY.
            view_ok, _ = run.call(
                "catia_drawing_add_view", view_args, allow_unsupported=is_3dx
            )
            if view_ok:
                run.call("catia_drawing_projection_view", {"direction": "right"})
            run.call("catia_drawing_add_text", {"text": "MCP smoke test", "x": 20, "y": 20})
            run.call("catia_drawing_list_views")

        if args.save:
            run.call("catia_save_document")
    finally:
        if isinstance(backend, InProcess):
            # Read before close(): disconnecting resets the detected platform.
            platform = backend.platform(connect_text)
        try:
            loop.run(backend.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            print(f"(closing the {backend.mode} backend failed: {e})")
        loop.stop()
        failed = [r for r in run.rows if r["status"] == "FAIL"]
        report = {
            "platform": platform,
            "mode": backend.mode,
            "url": args.url,
            "passed": sum(r["status"] == "PASS" for r in run.rows),
            "skipped": sum(r["status"] == "SKIP" for r in run.rows),
            "needs_eyes": sum(r["status"] == "EYES" for r in run.rows),
            "failed": len(failed),
            "results": run.rows,
        }
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print("\n" + summary_table(run.rows))
        print(
            f"\n{platform} ({backend.mode}): {report['passed']} passed, "
            f"{report['skipped']} skipped, {report['needs_eyes']} need eyes, "
            f"{report['failed']} failed."
        )
        if report["needs_eyes"]:
            print("EYES rows: look at CATIA and note what you saw (see 'note' in the report).")
        print(f"JSON report written to: {report_path}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

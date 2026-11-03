"""Export tools for CATIA V5 and CATIA 3DEXPERIENCE.

Export to STEP, IGES, STL, 3DXML, and other formats.
Also includes screenshot capture and view control, which use the
Window/Viewer API shared by both platforms.

3DEXPERIENCE content lives in the database, not in documents, and its
automation API documents no file export for it (DSYAutomation.chm R428 has
no ExportData). catia_export tries Document.ExportData where the
installation still exposes a compatibility document; when there is none, or
CATIA refuses the call (live-observed on R428 SP6 for a 3D Shape), it
reports UNSUPPORTED_CAPABILITY with the manual route. On both platforms an
export only succeeds once the output file is verified on disk.
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp.types import ImageContent, TextContent

from catia_mcp.automation import name_exists, safe_get
from catia_mcp.connection import CATIAConnection
from catia_mcp.errors import CATIAToolError, InvalidArgument, UnsupportedCapability
from catia_mcp.paths import state_dir

_MANUAL_EXPORT_3DX = (
    "Use CATIA's Export command (Action bar / 3DEXPERIENCE menu > Export, STEP needs "
    "the exchange licence, e.g. ST1). catia_screenshot works on 3DEXPERIENCE."
)

# CATIA export format identifiers
FORMAT_MAP = {
    "step": "stp",
    "stp": "stp",
    "iges": "igs",
    "igs": "igs",
    "stl": "stl",
    "3dxml": "3dxml",
    "wrl": "wrl",
    "vrml": "wrl",
    "pdf": "pdf",
    "cgr": "cgr",
}


# CatCaptureFormat: 2 = TIFF, 4 = BMP, 5 = JPEG (no PNG in CATIA V5).
_CAPTURE_FORMATS = {".jpg": 5, ".jpeg": 5, ".bmp": 4, ".tif": 2, ".tiff": 2}
_KEEP_CAPTURES = 50

Vec = tuple[float, float, float]


def _norm(v: Any) -> Vec:
    try:
        x, y, z = (float(c) for c in v)
    except Exception as e:
        raise InvalidArgument(f"A direction must be three numbers [x, y, z], got {v!r}") from e
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-9:
        raise InvalidArgument("A direction vector must not be zero.")
    return (x / length, y / length, z / length)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def orthogonal_up(sight: Vec, up: Any = None) -> Vec:
    """`up` projected onto the plane normal to `sight` (Gram-Schmidt).
    Default up is +Z, or +Y when looking along Z."""
    s = _norm(sight)
    if up is None:
        up = (0.0, 0.0, 1.0) if abs(s[2]) < 0.999 else (0.0, 1.0, 0.0)
    u = _norm(up)
    d = _dot(u, s)
    projected = (u[0] - d * s[0], u[1] - d * s[1], u[2] - d * s[2])
    if math.sqrt(_dot(projected, projected)) < 1e-6:
        raise InvalidArgument("The up vector must not be parallel to the sight direction.")
    return _norm(projected)


def _clean(v: Vec) -> Vec:
    return tuple(0.0 if abs(c) < 1e-12 else round(c, 12) for c in v)  # type: ignore[return-value]


def _build_presets() -> dict[str, tuple[Vec, Vec]]:
    # CATIA is Z-up. A view looks along `sight`; the camera sits at -sight.
    # front: camera at -Y looking +Y. These vectors follow the usual Z-up
    # convention and still need live confirmation against CATIA's own view
    # toolbar buttons (CATIA's Front button may look along X instead).
    presets: dict[str, tuple[Vec, Vec]] = {
        "front": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        "back": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
        "left": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        "right": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        "top": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
        "bottom": ((0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
    }
    iso = _norm((-1, -1, -1))
    presets["isometric"] = (iso, orthogonal_up(iso))
    # Corner isometrics: the camera sits at the named corner and looks at
    # the origin. Camera x: right = +X; y: front = -Y; z: top = +Z.
    for fb, cy in (("front", -1), ("back", 1)):
        for lr, cx in (("left", -1), ("right", 1)):
            for tb, cz in (("top", 1), ("bottom", -1)):
                sight = _norm((-cx, -cy, -cz))
                presets[f"iso_{fb}_{lr}_{tb}"] = (sight, orthogonal_up(sight))
    return {name: (_clean(s), _clean(u)) for name, (s, u) in presets.items()}


VIEW_PRESETS: dict[str, tuple[Vec, Vec]] = _build_presets()


def resolve_view(view: str | None, sight: Any, up: Any) -> tuple[str, Vec, Vec]:
    """(label, sight, up) from a preset name or explicit vectors."""
    if sight is not None:
        s = _norm(sight)
        return "custom", _clean(s), _clean(orthogonal_up(s, up))
    if view is None:
        raise InvalidArgument("Pass a 'view' preset or an explicit 'sight' vector.")
    if view not in VIEW_PRESETS:
        raise InvalidArgument(
            f"Unknown view '{view}'. Presets: {', '.join(VIEW_PRESETS)}"
        )
    s, u = VIEW_PRESETS[view]
    return view, s, u


def _fmt(v: Vec) -> str:
    return "(" + ", ".join(f"{c:.3g}" for c in v) + ")"


def _int_arg(arguments: dict[str, Any], name: str, default: int, lo: int, hi: int) -> int:
    value = arguments.get(name)
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise InvalidArgument(f"{name} must be an integer, got {value!r}.") from e
    if not lo <= number <= hi:
        raise InvalidArgument(f"{name} must be between {lo} and {hi}.")
    return number


def _pillow() -> Any:
    """PIL.Image when Pillow is installed (optional dependency), else None."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return None
    return Image


def _captures_dir() -> Path:
    path = state_dir() / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune_captures(keep: int) -> None:
    """Keep only the newest default captures so the folder cannot grow forever."""
    try:
        files = sorted(
            (p for p in _captures_dir().glob("capture-*") if p.is_file()),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        for old in files[keep:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _encode_image(path: Path, max_size: int) -> tuple[str, str, list[int] | None]:
    """(base64 data, mime type, [width, height] or None) for an image file.
    Downscales to max_size with Pillow when available; otherwise returns the
    file bytes unchanged (JPEG/PNG only)."""
    image_module = _pillow()
    if image_module is None:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return base64.b64encode(path.read_bytes()).decode("ascii"), mime, None
    with image_module.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_size, max_size))
        buf = io.BytesIO()
        if path.suffix.lower() == ".png":
            img.save(buf, format="PNG", optimize=True)
            mime = "image/png"
        else:
            img.save(buf, format="JPEG", quality=85)
            mime = "image/jpeg"
        return base64.b64encode(buf.getvalue()).decode("ascii"), mime, list(img.size)


class ExportTools:
    """Tools for exporting CATIA data to external formats."""

    def __init__(self, connection: CATIAConnection) -> None:
        self.conn = connection

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "catia_export",
                "description": (
                    "Export the active document to a file. "
                    "Supported formats: STEP (.stp), IGES (.igs), STL (.stl), "
                    "3DXML (.3dxml), VRML (.wrl), PDF (2D drawings). Succeeds only "
                    "when the file is verified on disk. On 3DEXPERIENCE this works "
                    "only where the installation exposes a document whose ExportData "
                    "CATIA accepts; otherwise it fails with UNSUPPORTED_CAPABILITY "
                    "and CATIA's Export command is the route (needs the exchange "
                    "licence, e.g. ST1 for STEP)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": (
                                "Output file path. The format is determined by the extension. "
                                "Example: 'C:/export/my_part.stp'"
                            ),
                        },
                        "format": {
                            "type": "string",
                            "description": (
                                "Export format (optional if file extension is provided). "
                                "One of: step, iges, stl, 3dxml, vrml"
                            ),
                            "enum": ["step", "iges", "stl", "3dxml", "vrml"],
                        },
                    },
                    "required": ["file_path"],
                },
            },
            {
                "name": "catia_screenshot",
                "description": (
                    "Capture the active 3D view and return it to you as an image, so you "
                    "can see the model. Optionally orients the view first ('view', same "
                    "presets as catia_set_view) and fits all geometry ('fit', default "
                    "true). file_path is optional (default: a fresh file in the server's "
                    "captures folder); its extension picks the format: .jpg, .bmp, .tif, "
                    "or .png (PNG needs Pillow on the server; without it the file is "
                    "saved as .jpg). The image size is the CATIA viewer's size: CATIA "
                    "cannot render at an arbitrary resolution, so the returned copy is "
                    "only downscaled to max_size when Pillow is installed. Fails if no "
                    "new file appears on disk."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": (
                                "Optional output path, e.g. 'C:/shots/part.jpg'. "
                                "Default: <state dir>/captures/capture-<time>.jpg"
                            ),
                        },
                        "view": {
                            "type": "string",
                            "description": "Optional view preset applied before capture",
                            "enum": list(VIEW_PRESETS),
                        },
                        "fit": {
                            "type": "boolean",
                            "description": "Fit all geometry before capture (default true)",
                            "default": True,
                        },
                        "return_image": {
                            "type": "boolean",
                            "description": (
                                "Return the image content (default true). False returns "
                                "only the path and size."
                            ),
                            "default": True,
                        },
                        "max_size": {
                            "type": "integer",
                            "description": (
                                "Longest side in px of the returned image (default 1280; "
                                "applied only when Pillow is installed). The file on disk "
                                "is never resized."
                            ),
                            "default": 1280,
                            "minimum": 64,
                            "maximum": 4096,
                        },
                    },
                },
                "annotations": {
                    "title": "Screenshot the CATIA view",
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
            },
            {
                "name": "catia_set_view",
                "description": (
                    "Orient the 3D view (CATIA is Z-up). Presets: front, back, left, "
                    "right, top, bottom, isometric and the 8 corner isometrics "
                    "iso_<front|back>_<left|right>_<top|bottom>. Or pass explicit "
                    "'sight' (look direction) and optional 'up' vectors. Fits all "
                    "geometry unless fit=false. Use catia_screenshot with 'view' to "
                    "orient and look in one call."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "view": {
                            "type": "string",
                            "description": "View preset",
                            "enum": list(VIEW_PRESETS),
                        },
                        "sight": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 3,
                            "maxItems": 3,
                            "description": "Explicit sight direction [x, y, z] (overrides view)",
                        },
                        "up": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 3,
                            "maxItems": 3,
                            "description": (
                                "Explicit up direction; made orthogonal to sight. "
                                "Default: +Z (or +Y when looking along Z)"
                            ),
                        },
                        "fit": {
                            "type": "boolean",
                            "description": "Fit all geometry after orienting (default true)",
                            "default": True,
                        },
                    },
                },
                "annotations": {
                    "title": "Set the CATIA view orientation",
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            },
            {
                "name": "catia_fit_all",
                "description": "Fit all geometry in the current 3D view (zoom to fit).",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        match tool_name:
            case "catia_export":
                return self._export(arguments["file_path"], arguments.get("format"))
            case "catia_screenshot":
                return self._screenshot(arguments)
            case "catia_set_view":
                return self._set_view_tool(arguments)
            case "catia_fit_all":
                return self._fit_all()
            case _:
                raise ValueError(f"Unknown export tool: {tool_name}")

    def _export_target(self) -> Any:
        if not self.conn.is_3dx:
            return self.conn.active_document
        app = self.conn.app
        doc = safe_get(app, "ActiveDocument") if name_exists(app, "ActiveDocument") else None
        if doc is not None and name_exists(doc, "ExportData"):
            return doc
        raise UnsupportedCapability(
            "3DEXPERIENCE automation exposes no file export for database content on "
            "this installation (no document with ExportData). " + _MANUAL_EXPORT_3DX
        )

    def _export(self, file_path: str, fmt: str | None = None) -> str:
        self.conn.ensure_connected()
        doc = self._export_target()

        # Determine format from extension if not specified
        if fmt is None:
            ext = os.path.splitext(file_path)[1].lstrip(".").lower()
            fmt = ext

        fmt_key = fmt.lower()
        if fmt_key not in FORMAT_MAP:
            supported = ", ".join(sorted(set(FORMAT_MAP.keys())))
            raise ValueError(
                f"Unsupported export format: '{fmt}'. Supported: {supported}"
            )

        # Ensure output directory exists
        output_dir = os.path.dirname(file_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        # An overwritten file must be rewritten, not merely present.
        before = os.path.getmtime(file_path) if os.path.exists(file_path) else None

        try:
            doc.ExportData(file_path, FORMAT_MAP[fmt_key])
        except Exception as e:
            if not self.conn.is_3dx:
                raise
            raise UnsupportedCapability(
                f"CATIA refused ExportData ({e}). The 3DEXPERIENCE automation reference "
                "documents no file export for database content. " + _MANUAL_EXPORT_3DX
            ) from e

        if not os.path.exists(file_path) or (
            before is not None and os.path.getmtime(file_path) <= before
        ):
            raise CATIAToolError(
                f"ExportData returned without error but wrote no file at {file_path}. "
                "Check the format licence and the path, then retry."
            )

        size_bytes = os.path.getsize(file_path)
        if size_bytes > 1024 * 1024:
            size_info = f" ({size_bytes / (1024*1024):.1f} MB)"
        elif size_bytes > 1024:
            size_info = f" ({size_bytes / 1024:.1f} KB)"
        else:
            size_info = f" ({size_bytes} bytes)"

        return f"Exported to {file_path}{size_info} (format: {fmt_key.upper()})"

    # ── View and capture ──

    def _viewer(self) -> Any:
        self.conn.ensure_connected()
        try:
            return self.conn.active_window.ActiveViewer
        except Exception as e:
            raise CATIAToolError(
                f"CATIA has no active 3D viewer ({e}). Open or activate a part, "
                "product or 3D editor window first."
            ) from e

    @staticmethod
    def _apply_view(viewer: Any, sight: Vec, up: Vec) -> None:
        viewpoint = viewer.Viewpoint3D
        viewpoint.PutSightDirection(list(sight))
        viewpoint.PutUpDirection(list(up))

    def _set_view_tool(self, arguments: dict[str, Any]) -> str:
        label, sight, up = resolve_view(
            arguments.get("view"), arguments.get("sight"), arguments.get("up")
        )
        viewer = self._viewer()
        self._apply_view(viewer, sight, up)
        if arguments.get("fit", True):
            viewer.Reframe()
        return (
            f"View set to: {label} (sight {_fmt(sight)}, up {_fmt(up)}). "
            "Verify with catia_screenshot."
        )

    def _screenshot(self, arguments: dict[str, Any]) -> list[Any]:
        view = arguments.get("view")
        fit = arguments.get("fit", True)
        return_image = arguments.get("return_image", True)
        max_size = _int_arg(arguments, "max_size", 1280, 64, 4096)
        orientation = resolve_view(view, None, None) if view else None

        requested = arguments.get("file_path")
        if requested:
            path = Path(requested)
        else:
            path = _captures_dir() / f"capture-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}.jpg"
        path = path.absolute()
        path.parent.mkdir(parents=True, exist_ok=True)

        has_pil = _pillow() is not None
        ext = path.suffix.lower()
        warnings: list[str] = []
        convert_to_png = False
        if ext == ".png":
            if has_pil:
                # CATIA cannot write PNG: capture lossless BMP, convert below.
                convert_to_png = True
            else:
                path = path.with_suffix(".jpg")
                ext = ".jpg"
                warnings.append("CATIA cannot capture PNG and Pillow is not installed; saved as .jpg")
        elif ext not in _CAPTURE_FORMATS:
            path = path.with_suffix(".jpg")
            ext = ".jpg"
            warnings.append("Unknown image extension; saved as .jpg")

        viewer = self._viewer()
        if orientation is not None:
            self._apply_view(viewer, orientation[1], orientation[2])
        if fit:
            viewer.Reframe()
        # Viewer.Update forces a redraw before capturing so the image shows
        # the new orientation. Documented on V5 Viewer; needs live validation
        # on 3DEXPERIENCE R428 (skipped silently when absent).
        if name_exists(viewer, "Update"):
            try:
                viewer.Update()
            except Exception:  # noqa: BLE001, S110 -- redraw is best effort
                pass

        if convert_to_png:
            # A private intermediate name: <name>.bmp may be a user's own file
            # next to the requested PNG and must not be overwritten or deleted.
            capture_path = path.with_name(f"{path.stem}.{uuid4().hex[:8]}.capture.bmp")
            try:
                self._capture(viewer, capture_path)
                with _pillow().open(capture_path) as img:
                    img.save(path, format="PNG")
            finally:
                capture_path.unlink(missing_ok=True)
        else:
            self._capture(viewer, path)

        info: dict[str, Any] = {
            "path": str(path),
            "view": orientation[0] if orientation else None,
            "bytes": path.stat().st_size,
        }
        if warnings:
            info["warnings"] = warnings
        content: list[Any] = []
        if return_image:
            image_path = path
            if path.suffix.lower() in (".bmp", ".tif", ".tiff") and not has_pil:
                # BMP/TIFF are not image types MCP clients render; without
                # Pillow to convert, capture a JPEG preview next to the file.
                image_path = path.with_name(path.stem + ".preview.jpg")
                self._capture(viewer, image_path)
                info["preview_path"] = str(image_path)
            data, mime, size = _encode_image(image_path, max_size)
            info["returned_image"] = {"mime_type": mime, "size": size}
            content.append(ImageContent(type="image", data=data, mimeType=mime))
        if not requested:
            _prune_captures(keep=_KEEP_CAPTURES)
        content.append(TextContent(type="text", text=json.dumps(info)))
        return content

    @staticmethod
    def _capture(viewer: Any, path: Path) -> None:
        """CaptureToFile and verify a new file landed on disk."""
        before = path.stat().st_mtime_ns if path.exists() else None
        viewer.CaptureToFile(_CAPTURE_FORMATS[path.suffix.lower()], str(path))
        if not path.exists() or path.stat().st_size == 0 or (
            before is not None and path.stat().st_mtime_ns <= before
        ):
            raise CATIAToolError(
                f"CaptureToFile returned without error but wrote no new image at {path}. "
                "Make sure a 3D window is active and visible (not minimized), then retry."
            )

    def _fit_all(self) -> str:
        self._viewer().Reframe()
        return "View fitted to all geometry"

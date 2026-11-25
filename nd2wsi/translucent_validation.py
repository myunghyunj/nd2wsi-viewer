"""Validation and normalization helpers for the translucent tracing overlay.

Pure, side-effect-free validation for handoff contexts, ROI geometries,
laser-shot annotations, overlay views, stage-follow options, follow flips,
and locked interactive hit-test rectangles.
"""

from __future__ import annotations

import copy
import math
import os
import re
import sys
import urllib.parse
from pathlib import Path

from . import follow_flips, laser_target, session_log
from .roi_list import roi_pair_paths as _roi_pair_paths

OPACITY = 0.55
MIN_OPACITY = 0.10
OPACITY_TIMEOUT = 5
MIN_SIZE = (220, 160)
MAX_SIZE = 8192
DEFAULT_STEP_UM = 10.0
MAX_STEP_UM = 10000.0
# Client-error forwarding is rate limited so a looping page cannot flood the log.
CLIENT_LOG_MAX = 20
CLIENT_LOG_WINDOW = 10.0
CLIENT_LOG_FIELD_LIMIT = 1000
CLIENT_LOG_STACK_LIMIT = 4000
MAX_LOCKED_INTERACTIVE_RECTS = 128

# What the page may report of its current view (report_overlay_view).
TARGET_KEYS = (
    "target_px",
    "target_offset_css",
    "target_side_um",
    "target_rotation_deg",
    "target_corners_px",
    "target_calibrated",
)
VIEW_KEYS = frozenset((
    "version",
    "center_px",
    "zoom",
    "rotation",
    "flip",
    "locked",
    "follow_session",
    "follow_seq",
    "follow_displacement_um",
    "container",
    "window",
    *TARGET_KEYS,
))
# The overlay's state in each slacs_key record (_mark_context), in record order.
MARK_CONTEXT_KEYS = (
    "slacs_session",
    "image_name",
    "source_format",
    "image_px",
    *TARGET_KEYS,
    "view",
    "view_age_ms",
    "view_follow_seq",
    "view_displacement_um",
    "locked",
    "follow_status",
    "follow_seq",
    "displacement_um",
    "counted",
    "step_source",
    "step_um",
    "effective_step_um",
    "roughness_um",
    "mirror_h",
    "mirror_v",
    "reading_age_ms",
    "slacs_busy",
    "flip_x",
    "flip_y",
)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Match nd2wsi/render.py without eagerly importing numpy at module load time.
_FLOAT32_SAFE_BOUND = 3e38
_HEX6_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
_ROI_BOUND_EPS = 1e-6
_MAX_QUERY_BYTES = 16384


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range")
    return float(value)


def _optional_numbers(value, length, name, minimum=-math.inf, maximum=math.inf):
    """None, or ``length`` finite numbers within the bounds, as floats."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != length:
        raise ValueError(f"Invalid {name}")
    return [_number(item, name, minimum, maximum) for item in value]


def _polygon_area(points):
    return abs(sum(
        points[i][0] * points[(i + 1) % len(points)][1]
        - points[(i + 1) % len(points)][0] * points[i][1]
        for i in range(len(points))
    )) / 2.0


def validate_roi(store, payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid ROI rectangle")
    level = store.attrs["nd2wsi"]["levels"][0]
    width, height = level["width"], level["height"]
    rectangle = {
        key: _number(payload.get(key), "ROI rectangle", 0, limit)
        for key, limit in (("x", width), ("y", height), ("w", width), ("h", height))
    }
    if (
        rectangle["w"] <= 0
        or rectangle["h"] <= 0
        or rectangle["x"] + rectangle["w"] > width + _ROI_BOUND_EPS
        or rectangle["y"] + rectangle["h"] > height + _ROI_BOUND_EPS
    ):
        raise ValueError("ROI rectangle is outside the slide")
    rectangle["w"] = min(rectangle["w"], width - rectangle["x"])
    rectangle["h"] = min(rectangle["h"], height - rectangle["y"])
    if rectangle["w"] <= 0 or rectangle["h"] <= 0:
        raise ValueError("ROI rectangle is outside the slide")
    return rectangle


def validate_laser_item(store, payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid laser annotation")
    slacs = payload.get("slacs")
    is_slacs_dict = isinstance(slacs, dict) and slacs.get("kind") == "laser_shot"
    if not is_slacs_dict and payload.get("kind") != "laser_shot":
        raise ValueError("Invalid laser annotation kind")
    item_id = payload.get("id")
    if not isinstance(item_id, str) or not 1 <= len(item_id) <= 256:
        raise ValueError("Invalid laser annotation id")
    raw_text = payload.get("text") if "text" in payload else payload.get("label", "")
    if not isinstance(raw_text, str) or len(raw_text) > 65536:
        raise ValueError("Invalid laser annotation label")
    raw_label = payload.get("label", raw_text)
    if not isinstance(raw_label, str) or len(raw_label) > 65536:
        raise ValueError("Invalid laser annotation label")
    raw_note = payload.get("note", "")
    if not isinstance(raw_note, str) or len(raw_note) > 65536:
        raise ValueError("Invalid laser annotation note")
    display_text = (raw_text or raw_label)[:1000]
    display_label = (raw_label or raw_text)[:1000]
    display_note = raw_note[:1000]
    color = payload.get("color", "#a0a6ae")
    if not isinstance(color, str) or not _HEX_COLOR_RE.match(color):
        raise ValueError("Invalid laser annotation color")
    shots = slacs.get("shots", 1) if isinstance(slacs, dict) else payload.get("shots", 1)
    if type(shots) is not int or not 1 <= shots <= 1_000_000:
        raise ValueError("Invalid laser shot count")
    cleaned_slacs = {"kind": "laser_shot", "shots": shots}
    if isinstance(slacs, dict) and "isolation" in slacs:
        isolation = slacs["isolation"]
        if type(isolation) is not int or not 1 <= isolation <= 2**53 - 1:
            raise ValueError("Invalid laser isolation number")
        cleaned_slacs["isolation"] = isolation
    item_type = payload.get("type")
    level = store.attrs["nd2wsi"]["levels"][0]
    width, height = level["width"], level["height"]
    if item_type == "box":
        rectangle = validate_roi(store, payload)
        return {
            "id": item_id,
            "kind": "laser_shot",
            "type": "box",
            **rectangle,
            "text": display_text,
            "label": display_label,
            "note": display_note,
            "color": color,
            "slacs": cleaned_slacs,
        }
    if item_type == "geojson":
        geojson = payload.get("geojson")
        geometry = geojson.get("geometry") if isinstance(geojson, dict) else None
        coords = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if (
            not isinstance(geojson, dict)
            or geojson.get("type") != "Feature"
            or not isinstance(geometry, dict)
            or geometry.get("type") != "Polygon"
            or not isinstance(coords, list)
            or len(coords) != 1
            or not isinstance(coords[0], list)
            or not 4 <= len(coords[0]) <= 32
        ):
            raise ValueError("Invalid laser polygon geometry")
        ring = []
        for point in coords[0]:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("Invalid laser polygon vertex")
            px = _number(point[0], "Laser vertex", 0, width)
            py = _number(point[1], "Laser vertex", 0, height)
            ring.append([px, py])
        if ring[0] != ring[-1] or _polygon_area(ring[:-1]) <= 0:
            raise ValueError("Invalid laser polygon ring")
        return {
            "id": item_id,
            "kind": "laser_shot",
            "type": "geojson",
            "text": display_text,
            "label": display_label,
            "note": display_note,
            "color": color,
            "slacs": cleaned_slacs,
            "geojson": {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {"name": display_text},
            },
        }
    raise ValueError("Invalid laser annotation type")


def validate_display_params(store, params) -> list[tuple[str, str]]:
    """Validate and canonicalize display-only parameters (c, win, col)."""
    if not isinstance(params, dict):
        raise ValueError("Invalid display settings")
    channels = store.attrs["omero"]["channels"]
    n_channels = len(channels)
    result: list[tuple[str, str]] = []

    selected = params.get("c")
    if selected not in (None, ""):
        if not isinstance(selected, str):
            raise ValueError("Invalid channel selection")
        if selected == "none":
            result.append(("c", "none"))
        else:
            indices = selected.split(",")
            seen: set[int] = set()
            for token in indices:
                if (
                    not token
                    or not token.isascii()
                    or not token.isdecimal()
                    or (len(token) > 1 and token.startswith("0"))
                ):
                    raise ValueError("Invalid channel selection")
                idx = int(token)
                if not 0 <= idx < n_channels or idx in seen:
                    raise ValueError("Invalid channel selection")
                seen.add(idx)
            result.append(("c", selected))

    raw_win = params.get("win")
    if raw_win is not None:
        if not isinstance(raw_win, str):
            raise ValueError("Invalid channel window")
        slots = raw_win.split(",")
        if len(slots) > n_channels:
            raise ValueError("Too many channel windows")
        for slot in slots:
            if slot == "":
                continue
            parts = slot.split(":")
            if len(parts) not in (2, 3):
                raise ValueError("Invalid channel window")
            values: list[float] = []
            for tok in parts:
                if not tok or tok != tok.strip() or any(ch.isspace() for ch in tok) or "_" in tok:
                    raise ValueError("Invalid channel window")
                try:
                    val = float(tok)
                except ValueError as exc:
                    raise ValueError("Invalid channel window") from exc
                if not math.isfinite(val):
                    raise ValueError("Invalid channel window")
                values.append(val)
            lo, hi = values[0], values[1]
            if (
                abs(lo) >= _FLOAT32_SAFE_BOUND
                or abs(hi) >= _FLOAT32_SAFE_BOUND
                or not math.isfinite(hi - lo)
                or hi <= lo
            ):
                raise ValueError("Invalid channel window")
            if len(values) == 3 and not 0.1 <= values[2] <= 10.0:
                raise ValueError("Invalid channel gamma")
        if any(slot != "" for slot in slots):
            result.append(("win", raw_win))

    raw_col = params.get("col")
    if raw_col is not None:
        if not isinstance(raw_col, str):
            raise ValueError("Invalid channel color")
        tokens = raw_col.split(",")
        if len(tokens) > n_channels:
            raise ValueError("Too many channel colors")
        cleaned_tokens: list[str] = []
        for token in tokens:
            if token == "":
                cleaned_tokens.append("")
                continue
            cleaned = token[1:] if token.startswith("#") else token
            if not _HEX6_RE.fullmatch(cleaned):
                raise ValueError("Invalid channel color")
            cleaned_tokens.append(cleaned)
        if any(cleaned_tokens):
            result.append(("col", ",".join(cleaned_tokens)))

    return result


def validate_display_query(store, display_query: str) -> list[tuple[str, str]]:
    """Validate a display-only query string containing only c, win, and/or col."""
    if not isinstance(display_query, str) or len(display_query) > _MAX_QUERY_BYTES:
        raise ValueError("Invalid display settings")
    body = display_query.removeprefix("?")
    pairs = urllib.parse.parse_qsl(
        body,
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=3,
    )
    params = dict(pairs)
    if len(params) != len(pairs) or set(params) - {"c", "win", "col"}:
        raise ValueError("Unsupported display setting")
    return validate_display_params(store, params)


def encode_validated_query(pairs) -> str:
    """Encode validated query pairs and enforce the post-encode 16,384-byte limit."""
    cleaned = urllib.parse.urlencode(pairs)
    if not cleaned:
        return ""
    encoded = "?" + cleaned
    if len(encoded) > _MAX_QUERY_BYTES:
        raise ValueError("Query too long")
    return encoded


def merge_display_query(store, base_query: str, display_query: str) -> str:
    """Merge validated display overrides onto immutable launch base keys (g, t, p, z)."""
    display_pairs = validate_display_query(store, display_query)
    if not isinstance(base_query, str):
        raise ValueError("Invalid base query")
    base_pairs = urllib.parse.parse_qsl(base_query.removeprefix("?"), keep_blank_values=True)
    base_map = dict(base_pairs)
    preserved = [(key, base_map[key]) for key in ("g", "t", "p", "z") if key in base_map]
    return encode_validated_query(display_pairs + preserved)


def validate_context(store, payload, *, slide_url):
    """Accept only a camera and tile-render settings for a registered slide."""
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Unsupported translucent-viewer state")
    meta = store.attrs["nd2wsi"]
    width, height = (meta["levels"][0][key] for key in ("width", "height"))
    center = payload.get("center")
    if not isinstance(center, list) or len(center) != 2:
        raise ValueError("Invalid image center")
    # OSD permits a little space outside an image while panning. Preserve it.
    center = [
        _number(value, "Image center", -8 * limit, 9 * limit)
        for value, limit in zip(center, (width, height))
    ]
    zoom = _number(payload.get("zoom"), "Image scale", 1e-12, 1024)
    rotation = _number(payload.get("rotation", 0), "Rotation", -360000, 360000) % 360
    flip = payload.get("flip", False)
    if type(flip) is not bool:
        raise ValueError("Flip must be boolean")
    # The overlay shows exactly the main viewer's rotation and flip; it has no
    # orientation settings of its own. An older handoff's "mirror_scan" is
    # ignored. Follow's flips reverse its movement only, never the orientation.
    query = payload.get("query", "")
    if not isinstance(query, str) or len(query) > 16384:
        raise ValueError("Invalid display settings")
    pairs = urllib.parse.parse_qsl(
        query.removeprefix("?"),
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=7,
    )
    params = dict(pairs)
    if len(params) != len(pairs) or set(params) - {"c", "win", "col", "g", "t", "p", "z"}:
        raise ValueError("Unsupported display setting")
    display_map = dict(validate_display_params(store, params))
    generation = getattr(store, "generation", "")
    if params.get("g", "") != generation:
        raise ValueError("The image changed; refresh the slide before opening the overlay")
    plate = getattr(store, "plate", None)
    if plate is not None:
        for name, size in (("t", plate.T), ("p", plate.P), ("z", plate.Z)):
            value = params.get(name, "")
            if not value.isascii() or not value.isdecimal() or not 0 <= int(value) < size:
                raise ValueError("Choose a valid plate site, time and Z plane first")
    elif set(params) & {"t", "p", "z"}:
        raise ValueError("Plane selection does not match this slide")
    canonical_pairs = []
    for key, value in pairs:
        if key in ("c", "win", "col"):
            if key in display_map:
                canonical_pairs.append((key, display_map[key]))
        else:
            canonical_pairs.append((key, value))
    encoded_query = encode_validated_query(canonical_pairs)
    plate_meta = meta.get("plate") if isinstance(meta.get("plate"), dict) else {}
    store_scope = "plane" if plate is not None and plate_meta.get("annotationScope") == "plane" else "site"
    if "annotation_scope" in payload and payload["annotation_scope"] is not None:
        if payload["annotation_scope"] not in ("plane", "site"):
            raise ValueError("Invalid annotation scope")
        if plate is None and payload["annotation_scope"] != "site":
            raise ValueError("Invalid annotation scope")
        if plate is not None and "annotationScope" in plate_meta and payload["annotation_scope"] != store_scope:
            raise ValueError("Annotation scope does not match this slide")
        annotation_scope = payload["annotation_scope"]
    else:
        annotation_scope = store_scope
    source_path = getattr(store, "source_path", None)
    raw_source = meta.get("source")
    image_name = (
        Path(str(source_path)).name
        if isinstance(source_path, (str, os.PathLike)) and str(source_path)
        else (
            Path(str(raw_source)).name
            if isinstance(raw_source, (str, os.PathLike)) and str(raw_source)
            else "image"
        )
    )
    context = {
        "version": 1,
        "slide_url": slide_url,
        "query": encoded_query,
        "center": center,
        "zoom": zoom,
        "rotation": rotation,
        "flip": flip,
        "dimensions": [width, height],
        "pixel_size_um": copy.deepcopy(meta.get("pixel_size_um")),
        "image_name": image_name,
        # Follow's flips are remembered per format of the original file.
        "source_format": follow_flips.source_format(store.attrs, getattr(store, "source_path", None)),
        "annotation_scope": annotation_scope,
        "opacity": OPACITY,
        "opacity_mode": "native" if sys.platform == "win32" else "layer",
        "locked": False,
    }
    if payload.get("roi") is not None:
        context["roi"] = validate_roi(store, payload["roi"])
    if "rois" in payload:
        rois = payload["rois"]
        if not isinstance(rois, list) or len(rois) > 2000:
            raise ValueError("The translucent viewer supports at most 2000 imported ROIs")
        cleaned_rois, indices = [], set()
        for roi in rois:
            rectangle = validate_roi(store, roi)
            label, index = roi.get("label"), roi.get("index")
            if not isinstance(label, str) or len(label) > 4096:
                raise ValueError("Invalid ROI label")
            if type(index) is not int or not 1 <= index <= 2**53 - 1 or index in indices:
                raise ValueError("ROI indices must be positive, unique safe integers")
            indices.add(index)
            cleaned_rois.append({**rectangle, "label": label, "index": index})
        context["rois"] = cleaned_rois
    if "roiIndex" in payload:
        selected = payload["roiIndex"]
        if type(selected) is not int or not 0 <= selected < len(context.get("rois", [])):
            raise ValueError("Invalid selected ROI")
        context["roiIndex"] = selected
    if "laser_items" in payload and payload["laser_items"] is not None:
        laser_items = payload["laser_items"]
        if not isinstance(laser_items, list) or len(laser_items) > 10000:
            raise ValueError("The translucent viewer supports at most 10000 laser annotations")
        cleaned_laser, laser_ids = [], set()
        for item in laser_items:
            cleaned_item = validate_laser_item(store, item)
            if cleaned_item["id"] in laser_ids:
                raise ValueError("Laser annotation IDs must be unique")
            laser_ids.add(cleaned_item["id"])
            cleaned_laser.append(cleaned_item)
        context["laser_items"] = cleaned_laser
    return context


def validate_overlay_view(payload):
    """Validate and normalize a view snapshot reported by the overlay page."""
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("Unsupported overlay view")
    if set(payload) - VIEW_KEYS:
        raise ValueError("Unknown overlay view field")
    center = payload.get("center_px")
    if not isinstance(center, list) or len(center) != 2:
        raise ValueError("Invalid view centre")
    flip, locked = payload.get("flip", False), payload.get("locked", False)
    if type(flip) is not bool or type(locked) is not bool:
        raise ValueError("View flip and lock must be boolean")
    session = payload.get("follow_session")
    if session is not None and (not isinstance(session, str) or len(session) > 128):
        raise ValueError("Invalid Follow session")
    seq = payload.get("follow_seq")
    if seq is not None and (type(seq) is not int or not 0 <= seq <= 2**53 - 1):
        raise ValueError("Invalid Follow sequence number")
    target = {}
    if any(key in payload for key in TARGET_KEYS):
        if not all(key in payload for key in TARGET_KEYS):
            raise ValueError("Incomplete laser target view")
        calibrated = payload["target_calibrated"]
        if type(calibrated) is not bool:
            raise ValueError("Invalid target calibration flag")
        point = _optional_numbers(payload["target_px"], 2, "target point", -1e9, 1e9)
        corners = payload["target_corners_px"]
        if corners is not None:
            if not isinstance(corners, list) or len(corners) != 4:
                raise ValueError("Invalid target corners")
            corners = [laser_target.vector(p, 2, -1e9, 1e9) for p in corners]
        if (point is None) != (corners is None) or (calibrated and point is None):
            raise ValueError("Incomplete target geometry")
        target = {
            "target_px": point,
            "target_offset_css": laser_target.vector(payload["target_offset_css"], 2, -16384, 16384),
            "target_side_um": _number(payload["target_side_um"], "target side", 2, 200),
            "target_rotation_deg": _number(payload["target_rotation_deg"], "target angle", 0, 90) % 90,
            "target_corners_px": corners,
            "target_calibrated": calibrated,
        }
    # The centre is only logged, never used to place a camera, so it is bounded
    # generously: the page can pan the image far outside validate_context's slack.
    return {
        "center_px": [_number(value, "View centre", -1e9, 1e9) for value in center],
        "zoom": _number(payload.get("zoom"), "View scale", 1e-12, 1e6),
        "rotation": _number(payload.get("rotation", 0), "View rotation", -360000, 360000) % 360,
        "flip": flip,
        "locked": locked,
        "follow_session": session,
        "follow_seq": seq,
        "follow_displacement_um": _optional_numbers(
            payload.get("follow_displacement_um"), 2, "Follow displacement"
        ),
        "container": _optional_numbers(payload.get("container"), 2, "view size", 0),
        "window": _optional_numbers(payload.get("window"), 4, "window frame", -1e6, 1e6),
        **target,
    }


def validate_laser_target_request(payload):
    """Validate a save_laser_target payload -> ``(target, calibrate, zoom)``."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"target", "calibrate", "zoom"}
        or type(payload["calibrate"]) is not bool
    ):
        raise ValueError("Invalid laser target request")
    target = laser_target.validate(payload["target"])
    zoom = _number(payload["zoom"], "Target scale", 1e-12, 1e6)
    if payload["calibrate"] and target["window"] is None:
        raise ValueError("Calibration needs the current window frame")
    return target, payload["calibrate"], zoom


def validate_stage_follow_options(options, *, platform: str | None = None):
    """Resolve stage-follow source, per-key step and step source; never raise.

    The step source is "slacs" (each press moves by SLACS's Roughness, with
    ``step_um`` as the fallback while it cannot be read) or "manual" (always
    ``step_um``). Roughness is read only on Windows, so by default only the
    Windows key source reads it.
    """
    resolved_platform = sys.platform if platform is None else platform
    default_source = "keys" if resolved_platform == "win32" else "adapter"

    def default_step_source(source):
        return "slacs" if resolved_platform == "win32" and source == "keys" else "manual"

    source, step_um = default_source, DEFAULT_STEP_UM
    if options is None:
        return source, step_um, default_step_source(source), None
    if not isinstance(options, dict):
        return source, step_um, default_step_source(source), "Invalid stage follow options."
    requested = options.get("source", default_source)
    if requested not in ("keys", "adapter"):
        return source, step_um, default_step_source(source), "Unknown stage follow source."
    source = requested
    step_source = options.get("step_source", default_step_source(source))
    if step_source not in ("slacs", "manual"):
        return source, step_um, default_step_source(source), "Unknown stage follow step source."
    if "step_um" in options:
        value = options["step_um"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= MAX_STEP_UM
        ):
            return (
                source,
                step_um,
                step_source,
                "Step per key press must be a number between 0.001 and 10000 µm.",
            )
        step_um = float(value)
    return source, step_um, step_source, None


def validate_follow_flips(payload) -> tuple[bool, bool]:
    """Validate ``{"flip_x": bool, "flip_y": bool}`` payload."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"flip_x", "flip_y"}
        or type(payload["flip_x"]) is not bool
        or type(payload["flip_y"]) is not bool
    ):
        raise ValueError("Follow flips must be two true/false values.")
    return payload["flip_x"], payload["flip_y"]


def validate_locked_interactive_rects(rects) -> list[dict[str, float]]:
    """Validate CSS-pixel client-area rectangles kept clickable while locked.

    Each entry must be a dict with finite numeric ``x``, ``y``, ``width``, and
    ``height`` (non-negative width/height). Zero-area rects are omitted from the
    returned list so hidden DOM controls cannot leave dead hit-test zones.
    """
    if not isinstance(rects, (list, tuple)):
        raise ValueError("Interactive rects must be a list")
    if len(rects) > MAX_LOCKED_INTERACTIVE_RECTS:
        raise ValueError(f"At most {MAX_LOCKED_INTERACTIVE_RECTS} interactive rects are supported")
    clean: list[dict[str, float]] = []
    for rect in rects:
        if not isinstance(rect, dict):
            raise ValueError("Invalid interactive rect")
        x = _number(rect.get("x"), "Interactive rect x", -MAX_SIZE, MAX_SIZE)
        y = _number(rect.get("y"), "Interactive rect y", -MAX_SIZE, MAX_SIZE)
        width = _number(rect.get("width"), "Interactive rect width", 0, MAX_SIZE)
        height = _number(rect.get("height"), "Interactive rect height", 0, MAX_SIZE)
        if width <= 0 or height <= 0:
            continue
        clean.append({"x": x, "y": y, "width": width, "height": height})
    return clean


def roi_pair_paths(paths, *, require_pair: bool = True) -> list[Path]:
    """Resolve one explicitly selected JPG/PNG + TXT pair; never guess among siblings."""
    return _roi_pair_paths(paths, require_pair=require_pair)


def laser_scope(context: dict) -> tuple:
    query = dict(urllib.parse.parse_qsl(str(context.get("query", "")).lstrip("?")))
    if context.get("annotation_scope") == "plane":
        return (
            context.get("slide_url"),
            query.get("t", ""),
            query.get("p", ""),
            query.get("z", ""),
            query.get("g", ""),
        )
    return (context.get("slide_url"), query.get("p", ""), query.get("g", ""))


def normalize_source_format(value) -> str:
    return value if value in follow_flips.FORMATS else "other"


def normalize_laser_items(items) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for item in items or ():
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            result[item["id"]] = copy.deepcopy(item)
    return result


def validate_pixel_size_um(pixels) -> tuple[float, float] | None:
    """Return ``(x_um, y_um)`` from context ``[y_um, x_um]``, or None if uncalibrated."""
    if (
        not isinstance(pixels, (list, tuple))
        or len(pixels) != 2
        or any(
            isinstance(v, bool)
            or not isinstance(v, (float, int))
            or not math.isfinite(v)
            or v <= 0
            for v in pixels
        )
    ):
        return None
    # The context's calibration is [Y, X]; the follower takes (X, Y).
    return (float(pixels[1]), float(pixels[0]))


def format_client_log_fields(
    payload: dict,
    *,
    slide_url,
    page_url,
    field_limit: int = CLIENT_LOG_FIELD_LIMIT,
    stack_limit: int = CLIENT_LOG_STACK_LIMIT,
) -> dict:
    """Normalize, redact capability URLs, and cap client-error fields."""
    slide_base = (
        slide_url.split("/s/", 1)[0] + "/"
        if isinstance(slide_url, str) and "/s/" in slide_url
        else slide_url
    )
    page_base = (
        page_url.split("/static/", 1)[0] + "/"
        if isinstance(page_url, str) and "/static/" in page_url
        else page_url
    )

    def capped(key, limit, *, redact=False):
        value = payload.get(key)
        if value is None:
            return None
        text = str(value)
        if redact:
            text = session_log.redact_capability_urls(text, slide_base, page_base)
        return text[:limit]

    # "client_kind" rather than "kind": session_log.event's first positional
    # parameter is itself named "kind", so a "kind" field would collide.
    return {
        "source": capped("source", field_limit),
        "client_kind": capped("kind", field_limit),
        "message": capped("message", field_limit, redact=True),
        "where": capped("where", field_limit, redact=True),
        "stack": capped("stack", stack_limit, redact=True),
    }

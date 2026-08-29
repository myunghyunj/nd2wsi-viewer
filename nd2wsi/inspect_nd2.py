"""`nd2wsi info`: report how an ND2 file is laid out internally.

This answers, per file, the question this project started from: does the
stitched ND2 contain independently addressable tiles, or one flattened
raster?  It prints:

* dimensions / dtype / compression / calibration / channels,
* the chunk census: how many ``ImageDataSeq|N!`` pixel blobs exist and how
  large they are (a stitched scan -> exactly one giant blob per frame),
* whether NIS-Elements embedded a ``DownsampledColorData_*`` preview pyramid
  (it sometimes does -- that is what the NIS viewer itself pans/zooms with),
* stage positions when the file is an *unstitched* multipoint acquisition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .reader import nice_bytes


def collect_info(path: str | Path) -> dict[str, Any]:
    import nd2

    path = Path(path)
    out: dict[str, Any] = {"file": str(path), "size_bytes": path.stat().st_size}
    with nd2.ND2File(str(path)) as f:
        out["version"] = tuple(f.version)
        out["legacy"] = bool(f.is_legacy)
        out["sizes"] = dict(f.sizes)
        out["dtype"] = str(f.dtype)
        out["rgb"] = bool(f.is_rgb)
        out["compression"] = getattr(f.attributes, "compressionType", None) or "none"
        vs = f.voxel_size()
        out["pixel_size_um"] = {"x": vs.x, "y": vs.y, "z": vs.z}
        try:
            out["channels"] = [
                {
                    "name": c.channel.name,
                    "color": [c.channel.color.r, c.channel.color.g, c.channel.color.b],
                }
                for c in (f.metadata.channels or [])
            ]
        except Exception:
            out["channels"] = []

        # ---- chunk census -------------------------------------------------
        chunk_info: dict[str, Any] = {}
        if f.is_legacy:
            chunk_info = {
                "error": "legacy JPEG2000-era container: frames are JP2 "
                "codestreams, the modern ImageDataSeq/Downsampled chunk "
                "layout does not apply"
            }
            out["chunks"] = chunk_info
            _positions(f, out)
            return out
        try:
            cm = f._rdr.chunkmap  # {name: (offset, size)} for modern files
            img = {k: v for k, v in cm.items() if k.startswith(b"ImageDataSeq")}
            ds = sorted(
                k.decode() for k in cm if b"DownsampledColorData" in k
            )
            sizes = [v[1] for v in img.values()]
            chunk_info = {
                "total_chunks": len(cm),
                "image_data_chunks": len(img),
                "image_data_bytes": sizes,
                "largest_image_chunk": max(sizes) if sizes else 0,
                "downsampled_chunks": ds,
                "other_notable": sorted(
                    {
                        k.decode().split("|")[0]
                        for k in cm
                        if not k.startswith(b"ImageDataSeq")
                    }
                )[:20],
            }
        except Exception as e:  # legacy JP2-based files have no chunkmap
            chunk_info = {"error": f"no modern chunkmap ({type(e).__name__}: {e})"}
        out["chunks"] = chunk_info

        _positions(f, out)
    return out


def _positions(f: Any, out: dict[str, Any]) -> None:
    try:
        xy = [e for e in f.experiment if getattr(e, "type", "") == "XYPosLoop"]
        if xy:
            pts = xy[0].parameters.points
            out["positions"] = [
                {
                    "name": getattr(p, "name", None),
                    "x_um": p.stagePositionUm.x,
                    "y_um": p.stagePositionUm.y,
                    "z_um": p.stagePositionUm.z,
                }
                for p in pts
            ]
    except Exception:
        pass


def format_info(info: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"file            {info['file']}  ({nice_bytes(info['size_bytes'])})")
    a(f"nd2 version     {info['version']}  {'(legacy JPEG2000 era)' if info['legacy'] else '(modern chunked container)'}")
    a(f"dimensions      {info['sizes']}")
    a(f"dtype           {info['dtype']}   rgb={info['rgb']}   compression={info['compression']}")
    p = info["pixel_size_um"]
    a(f"pixel size      x={p['x']} um  y={p['y']} um")
    if info.get("channels"):
        chs = ", ".join(
            f"{c['name'] or '(unnamed)'} rgb{tuple(c['color'])}" for c in info["channels"]
        )
        a(f"channels        {chs}")

    ck = info["chunks"]
    a("")
    a("internal layout")
    if "error" in ck:
        a(f"  {ck['error']}")
    else:
        n = ck["image_data_chunks"]
        a(f"  pixel-data chunks (ImageDataSeq|N!): {n}")
        if n:
            a(
                f"  largest pixel chunk: {nice_bytes(ck['largest_image_chunk'])}"
                + (
                    "   <- one flattened raster per frame; a stitched scan is ONE blob"
                    if n <= 4
                    else "   (one blob per T/P/Z frame)"
                )
            )
        ds = ck["downsampled_chunks"]
        if ds:
            a(f"  embedded preview pyramid: YES ({len(ds)} DownsampledColorData chunks)")
            for name in ds[:8]:
                a(f"      {name}")
        else:
            a("  embedded preview pyramid: none (no DownsampledColorData chunks)")

    pos = info.get("positions")
    if pos:
        a("")
        a(f"stage positions ({len(pos)} points -- unstitched multipoint metadata)")
        for pt in pos[:6]:
            a(
                f"  {pt['name'] or '(point)'}: x={pt['x_um']:.1f}  y={pt['y_um']:.1f}  z={pt['z_um']:.1f} um"
            )
        if len(pos) > 6:
            a(f"  ... {len(pos) - 6} more")
    return "\n".join(lines)

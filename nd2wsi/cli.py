"""nd2wsi command line interface.

    nd2wsi info    slide.nd2|.svs            # how is this file laid out inside?
    nd2wsi convert slide.nd2 [out.ome.zarr]  # build the OME-Zarr pyramid
    nd2wsi crop    slide.nd2 roi.nd2 --x --y --w --h   # native-res ND2 crop
    nd2wsi serve   out.ome.zarr              # serve the browser viewer
    nd2wsi view    slide.nd2|.svs            # convert (if needed) + serve
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .reader import PlaneSelection, parse_z


def _add_selection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--t", type=int, default=0, help="timepoint index (default 0)")
    p.add_argument(
        "--z",
        type=str,
        default="mid",
        help="Z plane: index, 'mid' (default) or 'max' (max-intensity projection)",
    )
    p.add_argument(
        "--position", type=int, default=0, help="position index for multipoint files"
    )


def _add_convert_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tile", type=int, default=512, help="pyramid tile size (default 512)")
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="dask worker threads (default: auto from this machine's CPU count)",
    )
    p.add_argument("--overwrite", action="store_true", help="replace existing store")
    _add_selection_args(p)


def _add_serve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--max-render-mpx",
        type=float,
        default=100.0,
        help="cap for rendered (png/jpg) ROI exports in megapixels; "
        "tiff export streams and has no cap",
    )


def _selection(args: argparse.Namespace) -> PlaneSelection:
    return PlaneSelection(t=args.t, p=args.position, z=parse_z(args.z))


def main(argv: list[str] | None = None) -> int:
    try:  # be a polite unix citizen when piped into `head` etc.
        import signal

        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        prog="nd2wsi",
        description="Nikon ND2 -> OME-Zarr pyramid -> browser slide viewer with "
        "native-resolution ROI export.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="inspect an ND2/SVS file's internal layout")
    p_info.add_argument("nd2")
    p_info.add_argument("--json", action="store_true", help="machine-readable output")

    p_conv = sub.add_parser("convert", help="ND2/SVS -> OME-Zarr multiscale pyramid")
    p_conv.add_argument("nd2")
    p_conv.add_argument("out", nargs="?", help="output store (default: <nd2>.ome.zarr)")
    _add_convert_args(p_conv)

    p_crop = sub.add_parser(
        "crop",
        help="crop a native-resolution ROI straight out of an ND2, as a new .nd2",
    )
    p_crop.add_argument("nd2")
    p_crop.add_argument("out", help="output .nd2 file")
    p_crop.add_argument("--x", type=int, required=True, help="left edge (px)")
    p_crop.add_argument("--y", type=int, required=True, help="top edge (px)")
    p_crop.add_argument("--w", type=int, required=True, help="width (px)")
    p_crop.add_argument("--h", type=int, required=True, help="height (px)")
    p_crop.add_argument(
        "--c", type=str, default=None, help="channel subset, e.g. 0 or 0,2 (default all)"
    )
    _add_selection_args(p_crop)

    p_serve = sub.add_parser("serve", help="serve the viewer for a converted store")
    p_serve.add_argument("store")
    _add_serve_args(p_serve)

    p_view = sub.add_parser("view", help="one shot: convert if needed, then serve")
    p_view.add_argument("nd2")
    p_view.add_argument("--store", help="pyramid location (default: <nd2>.ome.zarr)")
    _add_convert_args(p_view)
    _add_serve_args(p_view)

    args = parser.parse_args(argv)

    if args.cmd == "info":
        from .svs import collect_svs_info, format_svs_info, is_svs

        if is_svs(args.nd2):
            collect_info, format_info = collect_svs_info, format_svs_info
        else:
            from .inspect_nd2 import collect_info, format_info

        info = collect_info(args.nd2)
        if args.json:
            import json

            print(json.dumps(info, indent=2, default=str))
        else:
            print(format_info(info))
        return 0

    if args.cmd == "convert":
        from .convert import convert, default_store_path

        out = Path(args.out) if args.out else default_store_path(args.nd2)
        convert(
            args.nd2,
            out,
            tile=args.tile,
            selection=_selection(args),
            overwrite=args.overwrite,
            workers=args.workers,
        )
        return 0

    if args.cmd == "crop":
        from .export_nd2 import crop_nd2_to_nd2

        channels = [int(t) for t in args.c.split(",") if t.strip()] if args.c else None
        try:
            res = crop_nd2_to_nd2(
                args.nd2,
                args.out,
                args.x,
                args.y,
                args.w,
                args.h,
                selection=_selection(args),
                channels=channels,
            )
        except (RuntimeError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        wum, hum = res["um"]
        print(
            f"wrote {args.out}: {res['w']} x {res['h']} px "
            f"({wum:.0f} x {hum:.0f} um) at x={res['x']} y={res['y']}, "
            f"channels {res['channels']}, {res['pixel_size_um']:.4f} um/px"
        )
        return 0

    if args.cmd == "serve":
        from .server import serve

        serve(
            args.store,
            host=args.host,
            port=args.port,
            max_render_mpx=args.max_render_mpx,
        )
        return 0

    if args.cmd == "view":
        from .convert import convert, default_store_path
        from .server import serve

        store = Path(args.store) if args.store else default_store_path(args.nd2)
        if args.overwrite or not store.exists():
            convert(
                args.nd2,
                store,
                tile=args.tile,
                selection=_selection(args),
                overwrite=args.overwrite,
                workers=args.workers,
            )
        else:
            print(f"reusing existing pyramid {store} (use --overwrite to rebuild)")
        serve(
            store,
            host=args.host,
            port=args.port,
            max_render_mpx=args.max_render_mpx,
        )
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())

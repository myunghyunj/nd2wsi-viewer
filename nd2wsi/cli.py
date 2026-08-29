"""nd2wsi command line interface.

    nd2wsi info    slide.nd2                 # how is this file laid out inside?
    nd2wsi convert slide.nd2 [out.ome.zarr]  # build the OME-Zarr pyramid
    nd2wsi serve   out.ome.zarr              # serve the browser viewer
    nd2wsi view    slide.nd2                 # convert (if needed) + serve
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
    p.add_argument("--workers", type=int, default=4, help="dask worker threads")
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

    p_info = sub.add_parser("info", help="inspect an ND2 file's internal layout")
    p_info.add_argument("nd2")
    p_info.add_argument("--json", action="store_true", help="machine-readable output")

    p_conv = sub.add_parser("convert", help="ND2 -> OME-Zarr multiscale pyramid")
    p_conv.add_argument("nd2")
    p_conv.add_argument("out", nargs="?", help="output store (default: <nd2>.ome.zarr)")
    _add_convert_args(p_conv)

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

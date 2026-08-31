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


def confirm_pyramid(slide: str | Path, assume_yes: bool = False) -> bool:
    """Report what an SVS pyramid will cost and ask whether to build it.

    ND2 pyramids land near the size of the file itself, so only SVS gets the
    question. Non-interactive runs go ahead, which keeps scripts working.
    """
    from .convert import estimate_store_bytes
    from .reader import nice_bytes
    from .svs import is_svs

    if assume_yes or not is_svs(slide):
        return True
    est = estimate_store_bytes(slide)
    times = est["bytes"] / max(1, est["source_bytes"])
    print(
        f"{Path(slide).name} is {nice_bytes(est['source_bytes'])} of compressed "
        f"tiles. Its pyramid needs about {est['human']}, roughly {times:.0f} "
        f"times that, next to the slide."
    )
    print(f"  {nice_bytes(est['free_bytes'])} free on this volume.")
    if not sys.stdin.isatty():
        return True
    return input("build it? [Y/n] ").strip().lower() in ("", "y", "yes")


def _add_convert_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--tile",
        type=int,
        default=None,
        help="pyramid tile size (default picks for the volume, 1024 on "
        "big-block disks such as exFAT, else 512)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=None,
        help="dask worker threads (default: auto from this machine's CPU count)",
    )
    p.add_argument("--overwrite", action="store_true", help="replace existing store")
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the disk-space question asked before converting an SVS",
    )
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

    p_tidy = sub.add_parser(
        "tidy", help="collect scattered pyramid stores into one pyramids/ folder"
    )
    p_tidy.add_argument("folder", nargs="+", help="folder(s) holding slides")
    p_tidy.add_argument(
        "--dry-run", action="store_true", help="list the moves without making them"
    )

    p_serve = sub.add_parser("serve", help="serve the viewer for converted store(s)")
    p_serve.add_argument("store", nargs="+", help="one tab per store")
    _add_serve_args(p_serve)

    p_view = sub.add_parser("view", help="one shot: convert if needed, then serve")
    p_view.add_argument("nd2", nargs="+", help="one tab per slide")
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
        if not out.exists() and not confirm_pyramid(args.nd2, args.yes):
            print("nothing built")
            return 0
        convert(
            args.nd2,
            out,
            tile=args.tile,
            selection=_selection(args),
            overwrite=args.overwrite,
            workers=args.workers,
        )
        return 0

    if args.cmd == "tidy":
        from .convert import CACHE_DIR_NAME, tidy_caches
        from .reader import nice_bytes

        total = swept = freed = 0
        for folder in args.folder:
            res = tidy_caches(folder, dry_run=args.dry_run)
            total += len(res["moved"])
            swept += res["swept"]
            freed += res["freed"]
            for src, dst in res["moved"]:
                verb = "would move" if args.dry_run else "moved"
                print(f"{verb} {src.name} -> {CACHE_DIR_NAME}/{dst.name}")
        print(f"{total} store(s) {'to move' if args.dry_run else 'collected'}")
        if swept:
            print(f"swept {swept} AppleDouble files, {nice_bytes(freed)} recovered")
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
            [Path(s) for s in args.store],
            host=args.host,
            port=args.port,
            max_render_mpx=args.max_render_mpx,
        )
        return 0

    if args.cmd == "view":
        from .convert import convert, default_store_path
        from .server import serve

        stores = []
        for slide in args.nd2:
            store = default_store_path(slide)
            if args.store and len(args.nd2) == 1:
                store = Path(args.store)
            from .svs import is_svs

            if is_svs(slide) and not store.exists() and not args.overwrite:
                print(f"{Path(slide).name}: serving straight from the file")
                stores.append(Path(slide))
                continue
            if args.overwrite or not store.exists():
                if not confirm_pyramid(slide, args.yes):
                    print(f"skipped {Path(slide).name}")
                    continue
                convert(
                    slide,
                    store,
                    tile=args.tile,
                    selection=_selection(args),
                    overwrite=args.overwrite,
                    workers=args.workers,
                )
            else:
                print(f"reusing existing pyramid {store} (use --overwrite to rebuild)")
            stores.append(store)
        if not stores:
            return 0
        serve(
            stores,
            host=args.host,
            port=args.port,
            max_render_mpx=args.max_render_mpx,
        )
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())

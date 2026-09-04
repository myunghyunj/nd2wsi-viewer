#!/usr/bin/env python3
"""Copy Sparkle.framework into a built app and set safe updater defaults."""

from __future__ import annotations

import argparse
import base64
import plistlib
import shutil
from pathlib import Path


def inject(
    *,
    app: Path,
    framework: Path,
    feed_url: str,
    public_ed_key: str,
    interval: int = 86_400,
    expected_version: str = "2.9.6",
) -> Path:
    app = app.resolve()
    framework = framework.resolve()
    plist_path = app / "Contents" / "Info.plist"
    if not plist_path.is_file():
        raise FileNotFoundError(plist_path)
    if framework.name != "Sparkle.framework" or not framework.is_dir():
        raise ValueError(f"expected Sparkle.framework directory, got {framework}")
    if not feed_url.startswith("https://"):
        raise ValueError("Sparkle feed URL must use HTTPS")
    framework_info = framework / "Versions" / "Current" / "Resources" / "Info.plist"
    if not framework_info.is_file():
        raise ValueError("Sparkle framework has no version Info.plist")
    with framework_info.open("rb") as handle:
        framework_version = str(plistlib.load(handle).get("CFBundleShortVersionString", ""))
    if framework_version != expected_version:
        raise ValueError(
            f"expected Sparkle {expected_version}, got {framework_version or 'unknown'}"
        )
    try:
        public_key_bytes = base64.b64decode(public_ed_key.strip(), validate=True)
    except Exception as exc:
        raise ValueError("Sparkle public Ed25519 key is not valid base64") from exc
    if len(public_key_bytes) != 32:
        raise ValueError("Sparkle public Ed25519 key must decode to 32 bytes")
    if interval < 3600:
        raise ValueError("Sparkle check interval must be at least one hour")

    destination = app / "Contents" / "Frameworks" / "Sparkle.framework"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    shutil.copytree(framework, destination, symlinks=True)

    with plist_path.open("rb") as handle:
        info = plistlib.load(handle)
    info.update(
        {
            "SUFeedURL": feed_url,
            "SUPublicEDKey": public_ed_key.strip(),
            "SUEnableAutomaticChecks": True,
            "SUAutomaticallyUpdate": False,
            "SUAllowsAutomaticUpdates": False,
            "SUScheduledCheckInterval": int(interval),
        }
    )
    with plist_path.open("wb") as handle:
        plistlib.dump(info, handle, sort_keys=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--framework", type=Path, required=True)
    parser.add_argument("--feed-url", required=True)
    parser.add_argument("--public-ed-key", required=True)
    parser.add_argument("--interval", type=int, default=86_400)
    args = parser.parse_args()
    destination = inject(
        app=args.app,
        framework=args.framework,
        feed_url=args.feed_url,
        public_ed_key=args.public_ed_key,
        interval=args.interval,
    )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

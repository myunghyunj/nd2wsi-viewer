"""The distribution line this source tree builds.

This is the SLACS-module line: the public viewer plus SLACS stage follow and
session logging, released to the lab as ``X.Y.Z_SLACS-module`` after each
public release. A public release lacks this line's SLACS module and logging,
so builds from this line never check for, offer or open public updates.
``main`` has no copy of this file; merging ``main`` into this line keeps it.
"""

NAME = "SLACS-module"
UPDATES_OFF = True
UPDATES_OFF_MESSAGE = ("SLACS-module build: update checks are turned off. "
                       "New SLACS-module builds are released separately.")


def release_version(version: str) -> str:
    """Version label for this line's tag and asset names, e.g. ``2.5.4_SLACS-module``."""
    return f"{version}_{NAME}"

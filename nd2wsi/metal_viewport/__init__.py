"""Opt-in native Metal viewport; the cross-platform viewer is unchanged.

Automation must open a new Agent window/session before using this viewport.
Never retarget or modify the user's existing window or shared research data.
"""

from .source import RawTileSource, create_source_server

__all__ = ["RawTileSource", "create_source_server"]

"""The extracted runtime retains one cache and compatible reader primitives."""

from nd2wsi import direct, direct_common, plate, region_export, sdpc


def test_direct_runtime_aliases_and_consumers_share_the_same_objects():
    for name in (
        "MAX_EXTRA",
        "TILE_CACHE_BYTES",
        "_Lifecycle",
        "_Root",
        "_SvsLevel",
        "_TileCache",
        "_TILE_CACHE",
        "_parse_cyx_index",
    ):
        assert getattr(direct, name) is getattr(direct_common, name)

    for module, names in (
        (sdpc, ("_TILE_CACHE", "MAX_EXTRA", "_Lifecycle", "_Root", "_SvsLevel")),
        (plate, ("_Lifecycle", "_parse_cyx_index", "_Root", "_TileCache")),
        (region_export, ("_parse_cyx_index",)),
    ):
        for name in names:
            assert getattr(module, name) is getattr(direct_common, name)

"""Native reading leaves coordinate semantics to the shared AstroMapper importer."""

import pytest
from PIL import Image

from nd2wsi.roi_list import MAX_BYTES, load_roi_file

TEXT = "INDEX\tX1\tY1\tX2\tY2\tNOTE\n7\t10\t20\t13\t25\tfirst\n"


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_pair_is_read_only_and_returns_stored_axes(tmp_path, encoding):
    path = tmp_path / "image.txt"
    content = TEXT.encode(encoding)
    path.write_bytes(content)
    jpg = path.with_suffix(".jpg")
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (80, 60)).save(jpg, exif=exif)
    image_bytes = jpg.read_bytes()
    result = load_roi_file(path, width=800, height=600)
    assert result == {"ok": True, "name": "image.txt", "text": TEXT,
                      "coordinateSize": {"width": 80, "height": 60},
                      "reference": {"name": "image.jpg", "width": 80, "height": 60}}
    assert path.read_bytes() == content and jpg.read_bytes() == image_bytes


def test_missing_or_unmatched_pair_is_rejected(tmp_path):
    path = tmp_path / "image.txt"
    path.write_text(TEXT)
    with pytest.raises(ValueError, match="together"):
        load_roi_file(path, width=800, height=600)
    other = tmp_path / "other.jpg"
    Image.new("RGB", (80, 60)).save(other)
    with pytest.raises(ValueError, match="same filename"):
        load_roi_file(path, width=800, height=600, image_path=other)


def test_bounded_file_and_extension(tmp_path):
    path = tmp_path / "bad.txt"
    Image.new("RGB", (80, 60)).save(path.with_suffix(".jpg"))
    path.write_bytes(b"x" * (MAX_BYTES + 1))
    with pytest.raises(ValueError, match="16 MiB"):
        load_roi_file(path, width=80, height=60)
    with pytest.raises(ValueError, match=".txt"):
        load_roi_file(path.with_suffix(".json"), width=80, height=60)


def test_explicit_uppercase_named_pair_and_png(tmp_path):
    path, image = tmp_path / "image.txt", tmp_path / "IMAGE.PNG"
    path.write_text(TEXT)
    Image.new("RGB", (80, 60)).save(image)
    result = load_roi_file(path, width=800, height=600, image_path=image)
    assert result["reference"]["name"] == "IMAGE.PNG"

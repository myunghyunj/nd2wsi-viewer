"""Append independent PDF pages without retaining their image/font objects.

ReportLab produces one bounded page at a time. This writer imports each page's
object graph directly to disk with new reference numbers, then drops its reader.
Only cross-reference offsets, page/bookmark metadata, and image hashes survive
an append. Repeated overview JPEGs share one encoded image XObject.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)


def _image_digest(image: StreamObject) -> bytes | None:
    """Hash encoded bytes AND rendering attributes, resolving local references.

    Images with equal JPEG data but different color spaces/masks must not share
    an object. Hash streams without decoding; an unusual cyclic image graph is
    simply not deduplicated. No source objects or pixel buffers are cached.
    """
    digest = hashlib.sha256()
    active = set()

    def part(data):
        digest.update(str(len(data)).encode("ascii") + b":" + data)

    def visit(value):
        if isinstance(value, IndirectObject):
            key = (id(value.pdf), value.idnum, value.generation)
            if key in active:
                raise ValueError("cyclic image metadata")
            active.add(key)
            try:
                visit(value.get_object())
            finally:
                active.remove(key)
        elif isinstance(value, DictionaryObject):
            part(b"stream" if isinstance(value, StreamObject) else b"dictionary")
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
                if str(key) == "/Length":
                    continue
                part(str(key).encode("utf-8"))
                visit(item)
            part(b"end-dictionary")
            if isinstance(value, StreamObject):
                part(value._data)
        elif isinstance(value, ArrayObject):
            part(b"array")
            for item in value:
                visit(item)
            part(b"end-array")
        else:
            data = io.BytesIO()
            value.write_to_stream(data)
            part(data.getvalue())

    try:
        visit(image)
    except (ValueError, RecursionError):
        return None
    return digest.digest()


class StreamingPdf:
    """Disk-backed assembly of single-page PDFs; use as a context manager.

    ``append(page_bytes, title)`` accepts exactly one unencrypted PDF page.
    ``finish(title=...)`` writes Unicode document metadata and page bookmarks.
    A successful context exit also finishes automatically if needed. Failure
    closes and removes this writer's incomplete output.
    """

    def __init__(self, destination: str | Path):
        self.destination = Path(destination)
        self.file = self.destination.open("wb")
        self.file.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        self._offsets: dict[int, int] = {}
        self._pages: list[tuple[int, str]] = []
        self._images: dict[bytes, int] = {}
        self._next = 3  # catalog and root page tree have stable numbers 1 and 2
        self._finished = False
        self._failed = False

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        try:
            if kind is None and not self._failed and not self._finished:
                self.finish()
        finally:
            self.file.close()
            if not self._finished:
                self.destination.unlink(missing_ok=True)
        return False

    @property
    def page_count(self):
        return len(self._pages)

    @property
    def image_count(self):
        return len(self._images)

    def _reference(self, number):
        # write_to_stream only needs idnum/generation, not a resident object.
        return IndirectObject(number, 0, self)

    def _reserve(self):
        number = self._next
        self._next += 1
        return number

    def _write(self, number, value):
        if number in self._offsets:
            raise ValueError("PDF object was already written")
        offset = self.file.tell()
        if offset >= 10_000_000_000:
            raise ValueError("PDF exceeds the classic cross-reference offset limit")
        self._offsets[number] = offset
        self.file.write(f"{number} 0 obj\n".encode("ascii"))
        value.write_to_stream(self.file)
        self.file.write(b"\nendobj\n")

    def append(self, page_bytes: bytes, title: str):
        if self._finished or self.file.closed or self._failed:
            raise ValueError("PDF writer is finished or has failed")
        reader = None
        try:
            reader = PdfReader(io.BytesIO(page_bytes), strict=True)
            if reader.is_encrypted or len(reader.pages) != 1:
                raise ValueError("Append exactly one unencrypted PDF page")
            page = reader.pages[0]
            page_number = self._reserve()
            references = {}
            if page.indirect_reference is not None:
                ref = page.indirect_reference
                references[(ref.idnum, ref.generation)] = page_number

            def copy_object(source, *, is_page=False):
                if isinstance(source, StreamObject):
                    target = StreamObject()
                    # Sharing this immutable byte string lasts only this call.
                    # Encoded stream filters are copied unchanged below.
                    target._data = source._data
                elif isinstance(source, DictionaryObject):
                    target = DictionaryObject()
                elif isinstance(source, ArrayObject):
                    return ArrayObject([copy_value(value) for value in source])
                else:
                    return source
                for key, value in source.items():
                    if str(key) == "/Length" and isinstance(source, StreamObject):
                        continue  # StreamObject writes the actual encoded length.
                    if is_page and str(key) == "/Parent":
                        continue
                    target[key] = copy_value(value)
                if is_page:
                    target[NameObject("/Parent")] = self._reference(2)
                return target

            def copy_value(source):
                if isinstance(source, IndirectObject):
                    key = (source.idnum, source.generation)
                    if key in references:
                        return self._reference(references[key])
                    original = source.get_object()
                    image_hash = None
                    if isinstance(original, StreamObject) and original.get("/Subtype") == "/Image":
                        image_hash = _image_digest(original)
                        if image_hash is not None and image_hash in self._images:
                            references[key] = self._images[image_hash]
                            return self._reference(references[key])
                    number = self._reserve()
                    references[key] = number  # Reserve first so cyclic refs work.
                    self._write(number, copy_object(original))
                    if image_hash is not None:
                        self._images[image_hash] = number
                    return self._reference(number)
                if isinstance(source, StreamObject):
                    # PDF streams must be indirect even if a producer supplies
                    # an unusual direct stream in an otherwise valid page tree.
                    number = self._reserve()
                    self._write(number, copy_object(source))
                    return self._reference(number)
                return copy_object(source)

            self._write(page_number, copy_object(page, is_page=True))
            self._pages.append((page_number, str(title)))
        except BaseException:
            self._failed = True
            raise
        finally:
            if reader is not None:
                # Reader graphs contain self-references. Clear its cache now,
                # rather than letting many image pages wait for cyclic GC.
                reader.close()
                reader.stream.close()

    def finish(self, title: str = "ROI navigation"):
        if self._finished:
            return self.destination
        if self._failed or self.file.closed:
            raise ValueError("Cannot finish a failed or closed PDF writer")
        if not self._pages:
            raise ValueError("PDF requires at least one page")
        try:
            outlines = self._reserve()
            entries = [self._reserve() for _ in self._pages]
            for index, ((page, name), number) in enumerate(zip(self._pages, entries)):
                entry = DictionaryObject({
                    NameObject("/Title"): TextStringObject(name),
                    NameObject("/Parent"): self._reference(outlines),
                    NameObject("/Dest"): ArrayObject([self._reference(page), NameObject("/Fit")]),
                })
                if index:
                    entry[NameObject("/Prev")] = self._reference(entries[index - 1])
                if index + 1 < len(entries):
                    entry[NameObject("/Next")] = self._reference(entries[index + 1])
                self._write(number, entry)
            self._write(outlines, DictionaryObject({
                NameObject("/Type"): NameObject("/Outlines"),
                NameObject("/Count"): NumberObject(len(entries)),
                NameObject("/First"): self._reference(entries[0]),
                NameObject("/Last"): self._reference(entries[-1]),
            }))
            info = self._reserve()
            self._write(info, DictionaryObject({
                NameObject("/Title"): TextStringObject(str(title)),
                NameObject("/Creator"): TextStringObject("nd2wsi-viewer ROI navigation"),
            }))
            self._write(1, DictionaryObject({
                NameObject("/Type"): NameObject("/Catalog"),
                NameObject("/Pages"): self._reference(2),
                NameObject("/Outlines"): self._reference(outlines),
            }))
            self._write(2, DictionaryObject({
                NameObject("/Type"): NameObject("/Pages"),
                NameObject("/Count"): NumberObject(len(self._pages)),
                NameObject("/Kids"): ArrayObject([self._reference(page) for page, _ in self._pages]),
            }))
            if set(self._offsets) != set(range(1, self._next)):
                raise ValueError("PDF contains unresolved object references")
            start = self.file.tell()
            self.file.write(f"xref\n0 {self._next}\n0000000000 65535 f \n".encode("ascii"))
            for number in range(1, self._next):
                self.file.write(f"{self._offsets[number]:010d} 00000 n \n".encode("ascii"))
            trailer = DictionaryObject({
                NameObject("/Size"): NumberObject(self._next),
                NameObject("/Root"): self._reference(1),
                NameObject("/Info"): self._reference(info),
            })
            self.file.write(b"trailer\n")
            trailer.write_to_stream(self.file)
            self.file.write(f"\nstartxref\n{start}\n%%EOF\n".encode("ascii"))
            self.file.flush()
            self._finished = True
            return self.destination
        except BaseException:
            self._failed = True
            raise

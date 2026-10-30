"""Staged, source-bound replacement of an ND2 display pyramid.

Preparation owns the existing cache build lock, reads the original ND2 through
its registered native adapter, and writes only unique staging siblings. The
caller quiesces its readers before publish(); cancellation never changes the
live cache. The original ND2 and annotation sidecars are never written.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .bffree_serialization import (
    NATIVE_CONTRACT,
    SUPPORTED_SCHEMAS,
    contract_for_schema,
)
from .cache import (
    CORRECTED_SINGLE_FILE_FORMAT,
    MANIFEST_NAME,
    SINGLE_FILE_FORMAT,
    STORE_NAME,
    CacheLock,
    commit_container,
    fingerprints_match,
    quick_fingerprint,
    read_manifest,
    write_manifest,
)
from .storage import DEFAULT_STORAGE
from .storage.single_file import CORRECTED_FORMAT_VERSION, SQLiteStore, read_entry

MODEL_KEY = "h-correction/model.npz"
PROVENANCE_KEY = "h-correction/provenance.json"
SCHEMA = "nd2wsi-h-correction/1"
_MODEL_LIMIT = 32 * 1024**2
CONSENSUS_NATIVE_CONTRACT = NATIVE_CONTRACT
LEGACY_BFFREE_NATIVE_CONTRACT = "BF-free-32x-bin-center-interpolation(native)-then-box-mean"
# Level-1 tiles prepared at once. The viewer keeps serving tiles meanwhile, so
# one core stays free. One task peaks near 30 bytes per native pixel of its
# 2x window (uint16 read, float32 gain and quotient, row-band temporaries), so
# a 256 MiB budget allows 8 tasks at tile 512 but only 2 at tile 1024.
_APPLY_MAX_WORKERS = max(1, min(8, (os.cpu_count() or 1) - 1))
_APPLY_TASK_BUDGET = 256 * 1024**2
_APPLY_TASK_BYTES_PER_PIXEL = 32


class PreparationCancelled(Exception):
    """No replacement was published; the previous container remains usable."""


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _identity(path, *, single_link=True):
    st = Path(path).stat(follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or (single_link and st.st_nlink != 1):
        raise ValueError("Persistent correction requires a regular, singly linked cache/source file")
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _bffree_source_binding(model, labels, shape):
    """Bind original fluorescence indices and native geometry before use."""
    from .h_correction import _mapping

    if (not isinstance(labels, list) or len(labels) != int(shape[0])
            or not all(isinstance(label, str) for label in labels)):
        raise ValueError("Missing BF-free source channel provenance")
    mapping = _mapping(labels, "bffree", require_correctable=False)
    if not set(model.indices) <= set(mapping.values()):
        raise ValueError("Invalid BF-free target channel mapping")
    if tuple(model.native_shape_yx) != tuple(shape[-2:]):
        raise ValueError("BF-free model native geometry differs from source")
    if model.model_schema in SUPPORTED_SCHEMAS:
        if (any(mapping.get(name) != index for name, index in zip(model.channel_names, model.indices))
                or any(name == "DAPI" and enabled for name, enabled in zip(model.channel_names, model.enabled))):
            raise ValueError("Consensus model channels differ from original source channels")
    return mapping


def _consensus_fit_binding(model, record):
    """Do not save an independent-x/h1 label beside consensus image pixels."""
    if (not isinstance(record, dict) or record.get("schema") != model.model_schema
            or record.get("config") != model.prediction["diagnostics"]["config"]
            or record.get("uses_bf") is not False or record.get("BF_modified") is not False
            or record.get("DAPI_modified") is not False
            or record.get("native_shape_yx") != model.native_shape_yx.tolist()
            or record.get("target_channels") != model.channel_names):
        raise ValueError("Consensus fit provenance differs from the saved model")
    selected = record.get("selected")
    if (not isinstance(selected, list) or len(selected) != len(model.indices)
            or any(not isinstance(row, dict) or row.get("channel") != name
                   or row.get("channel_index") != index or row.get("enabled") is not enabled
                   for row, name, index, enabled in zip(selected, model.channel_names, model.indices, model.enabled))):
        raise ValueError("Consensus fit channel provenance differs from the saved model")


def load_model(container, manifest):
    """Load a bounded, hash-verified model without extracting a sidecar."""
    from .bffree_model import FIELD_NAMES, LEGACY_FIELD_NAMES, LEGACY_SCHEMA
    from .bffree_model import Model as BFFreeModel
    from .h_model import Model

    correction = manifest.get("h_correction") or {}
    if (manifest.get("format") != CORRECTED_SINGLE_FILE_FORMAT
            or correction.get("schema") != SCHEMA or correction.get("applied") is not True
            or correction.get("model_key") != MODEL_KEY
            or correction.get("provenance_key") != PROVENANCE_KEY):
        raise ValueError("Unsupported persisted Shading Correction capability")
    with Path(container).open("rb") as handle:
        header = handle.read(100)
    if int.from_bytes(header[60:64], "big") != CORRECTED_FORMAT_VERSION:
        raise ValueError("Corrected cache lacks its required storage capability guard")
    payload = read_entry(container, MODEL_KEY)
    provenance_payload = read_entry(container, PROVENANCE_KEY)
    if (len(payload) > _MODEL_LIMIT or _sha(payload) != correction.get("model_sha256")
            or _sha(provenance_payload) != correction.get("provenance_sha256")):
        raise ValueError("Persisted Shading Correction model/provenance changed")
    provenance = json.loads(provenance_payload)
    if (provenance.get("schema") != SCHEMA
            or provenance.get("correction_id") != correction.get("correction_id")
            or not fingerprints_match(provenance.get("source", {}), manifest.get("source", {}))
            or provenance.get("selection") != manifest.get("selection")
            or provenance.get("method", "h") != correction.get("method", "h")):
        raise ValueError("Persisted Shading Correction does not identify this source/selection")
    expected_base = {name + ".npy" for name in Model.BASE_FIELDS}
    expected_adaptive = {name + ".npy" for name in Model.ADAPTIVE_FIELDS}
    expected_bffree = {name + ".npy" for name in FIELD_NAMES}
    expected_legacy_bffree = {name + ".npy" for name in LEGACY_FIELD_NAMES}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        entry_names = {entry.filename for entry in entries}
        if (entry_names not in (expected_base, expected_adaptive, expected_bffree, expected_legacy_bffree)
                or len(entries) != len(entry_names)
                or sum(entry.file_size for entry in entries) > _MODEL_LIMIT):
            raise ValueError("Unsupported or oversized persisted correction model")
    bffree = entry_names in (expected_bffree, expected_legacy_bffree)
    if correction.get("method", "h") != ("bffree" if bffree else "h"):
        raise ValueError("Correction method and persisted model disagree")
    model = (BFFreeModel if bffree else Model)(io.BytesIO(payload))
    try:
        count = len(model.indices)
        channels = int(manifest["image"]["shape_cyx"][0])
        if (not 1 <= count <= 3 or len(set(model.indices)) != count
                or any(c < 0 or c >= channels for c in model.indices)
                or len(model.enabled) != count
                or [c for c, enabled in zip(model.indices, model.enabled) if enabled]
                != correction.get("channel_indices")):
            raise ValueError("Invalid persisted correction channel mapping")
        if bffree:
            labels = provenance.get("source_channel_names")
            shape = manifest["image"]["shape_cyx"]
            mapping = _bffree_source_binding(model, labels, shape)
            # Read only the small metadata entry, never extract or rewrite a
            # research cache to establish this source-channel binding.
            stored_attrs = json.loads(read_entry(container, STORE_NAME + "/.zattrs"))
            stored_labels = [row.get("label") for row in stored_attrs.get("omero", {}).get("channels", [])]
            if stored_labels != labels:
                raise ValueError("Stored BF-free source channel provenance differs")
            if model.model_schema in SUPPORTED_SCHEMAS:
                if (correction.get("model_schema") != model.model_schema
                        or provenance.get("model_schema") != model.model_schema
                        or correction.get("experimental") is not False
                        or provenance.get("experimental") is not False
                        or correction.get("native_coordinate_contract") != contract_for_schema(model.model_schema)["native_contract"]
                        or provenance.get("native_coordinate_contract") != contract_for_schema(model.model_schema)["native_contract"]
                        or provenance.get("source_shape_cyx") != shape
                        or provenance.get("source_channel_mapping") != mapping
                        or provenance.get("config") != model.prediction["diagnostics"]["config"]
                        or provenance.get("config_sha256") != _sha(_json_bytes(provenance["config"]))):
                    raise ValueError("Consensus persisted model/provenance contract differs")
                _consensus_fit_binding(model, provenance.get("fit"))
            elif (model.model_schema != LEGACY_SCHEMA
                    or correction.get("model_schema", LEGACY_SCHEMA) != LEGACY_SCHEMA
                    or provenance.get("model_schema", LEGACY_SCHEMA) != LEGACY_SCHEMA
                    or correction.get("experimental") is not True
                    or correction.get("native_coordinate_contract") != LEGACY_BFFREE_NATIVE_CONTRACT):
                raise ValueError("Missing legacy experimental BF-free provenance")
        # Model validates the exact schema/fields, geometry and analytical gain
        # bounds across every stored template/envelope value before this return.
        return model
    except BaseException:
        model.data.close()
        raise


class PreparedReplacement:
    """Own staging and CacheLock until publish/close; publishing is irreversible.

    The caller must close its SQLite readers before publish on Windows. An
    external reader/rename denial leaves the old file intact and raises.
    """

    def __init__(self, state, event):
        self.state, self.event = state, event
        self.final = Path(state.container_path or state.store_path)
        self.source = Path(state.source_path)
        self.staging = self.final.with_name(self.final.name + ".building-h-" + uuid.uuid4().hex)
        self.directory = None
        self.manifest = None
        self.prior_manifest = None
        self.published = False
        self._owned_file = False
        self._closed = False
        self._lock = CacheLock(self.final)
        if self.final.suffix.lower() != ".nd2svs" or self.source.suffix.lower() != ".nd2":
            raise ValueError("Persistent correction requires the original ND2 and its .nd2svs cache")
        self._lock.acquire(timeout=0)
        try:
            self._container_identity = _identity(self.final)
            self._source_identity = _identity(self.source, single_link=False)
            self.prior_manifest = read_manifest(self.final)
            if (not self.prior_manifest
                    or self.prior_manifest.get("format") not in (SINGLE_FILE_FORMAT, CORRECTED_SINGLE_FILE_FORMAT)
                    or self.prior_manifest.get("kind") not in ("full", "overview")
                    or self.prior_manifest.get("generation") != state.manifest.get("generation")):
                raise ValueError("The registered cache changed; reopen the original ND2")
            self._fingerprint = quick_fingerprint(self.source)
            if not fingerprints_match(self.prior_manifest.get("source", {}), self._fingerprint):
                raise ValueError("The original ND2 no longer matches its cache")
            # An unexpected embedded annotation or user entry is never lost in
            # a rebuild. Only known image-cache entries are replaceable.
            with SQLiteStore(self.final, read_only=True) as store:
                for key in store.list_keys():
                    name = key.rsplit("/", 1)[-1]
                    known = key in (MANIFEST_NAME, MODEL_KEY, PROVENANCE_KEY) or (
                        key.startswith(STORE_NAME + "/") and (
                            name in (".zattrs", ".zgroup", ".zarray", ".zmetadata")
                            or re.fullmatch(r"\d+(?:\.\d+)*", name)
                        )
                    )
                    if not known:
                        raise ValueError(f"Cache contains an unrecognized/user-owned entry; preserved: {key}")
            self.check()
            self.directory = Path(tempfile.mkdtemp(prefix=self.final.name + ".building-h-", dir=self.final.parent))
        except BaseException:
            self._lock.release()
            raise

    def check(self):
        if self.event.is_set():
            raise PreparationCancelled("Pyramid replacement cancelled")
        if _identity(self.source, single_link=False) != self._source_identity:
            raise ValueError("The original ND2 changed during pyramid preparation")

    def _validate_before_publish(self):
        if (_identity(self.final) != self._container_identity
                or read_manifest(self.final) != self.prior_manifest):
            raise ValueError("The cache changed during preparation; replacement refused")
        if (not fingerprints_match(self._fingerprint, quick_fingerprint(self.source))
                or _identity(self.source, single_link=False) != self._source_identity):
            raise ValueError("The original ND2 changed during preparation; replacement refused")
        if not self.manifest or read_manifest(self.staging) != self.manifest:
            raise ValueError("The prepared replacement is incomplete or changed")

    def publish(self):
        if self._closed or self.published:
            raise ValueError("This pyramid replacement is no longer pending")
        self.check()
        self._validate_before_publish()
        # All writer handles are closed and the packer has already fsynced and
        # independently verified its bytes. From this point cancellation cannot
        # interrupt the one atomic filesystem operation.
        commit_container(self.staging, self.final)
        self.published = True
        self._owned_file = False
        return self.manifest

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._owned_file:
                self.staging.unlink(missing_ok=True)
            if self.directory is not None:
                shutil.rmtree(self.directory)
        finally:
            self._lock.release()

    abort = close


def _read_native(state, replacement, ci, y0, y1, x0, x1):
    replacement.check()
    with state.busy:
        replacement.check()
        native = getattr(state.root, "raw_level0", state.root["0"])
        return np.array(native[ci, y0:y1, x0:x1], copy=True)


def _apply_workers(tile):
    per_task = _APPLY_TASK_BYTES_PER_PIXEL * (2 * int(tile)) ** 2
    return max(1, min(_APPLY_MAX_WORKERS, _APPLY_TASK_BUDGET // per_task))


def _checked(replacement, build, args):
    replacement.check()
    return build(*args)


def _build_tiles(replacement, build, tiles, done, workers=1):
    """Build every tile and report each result to ``done`` in tile order.

    With more than one worker the tiles run on a small thread pool: each
    writes its own chunk file, and the gain and reduction work releases the
    GIL. Each task checks cancellation itself (and ``build`` enters
    ``state.busy`` for its reads). On any failure the queued tasks are
    cancelled and the running ones finish before this returns, so no worker
    outlives the staging directory or still holds ``state.busy`` when the
    caller closes the replacement.
    """
    if workers <= 1:
        for args in tiles:
            replacement.check()
            done(build(*args))
        return
    pending = deque()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="nd2wsi-h-apply") as pool:
        try:
            for args in tiles:
                replacement.check()
                if len(pending) >= 2 * workers:
                    done(pending.popleft().result())
                pending.append(pool.submit(_checked, replacement, build, args))
            while pending:
                done(pending.popleft().result())
        except BaseException:
            for future in pending:
                future.cancel()
            raise


def _reduce2(values, height, width):
    if values.shape[0] == 1 and height == 1:
        values = np.repeat(values, 2, axis=0)
    if values.shape[1] == 1 and width == 1:
        values = np.repeat(values, 2, axis=1)
    return values.reshape(height, 2, width, 2).mean(axis=(1, 3), dtype=np.float64).astype(np.float32)


def _pack(replacement, progress, *, storage_version=2):
    from .migration import pack_directory

    replacement._owned_file = True
    try:
        result = pack_directory(
            replacement.directory, replacement.staging, source=replacement.source,
            storage_version=storage_version,
            on_progress=lambda value: (replacement.check(), progress(value)),
        )
    except FileExistsError:
        replacement._owned_file = False
        raise
    replacement.manifest = result["manifest"]


def prepare_apply(state, model, model_record, event, progress):
    """Build all corrected reduced levels; leave publication to the controller."""
    replacement = PreparedReplacement(state, event)
    try:
        if replacement.prior_manifest.get("h_correction") is not None:
            raise ValueError("Shading Correction is already applied; revoke before fitting again")
        # Refuse an invalid model before rebuilding a potentially large pyramid.
        model.validate()
        native = getattr(state.root, "raw_level0", state.root["0"])
        if np.dtype(native.dtype) not in (np.dtype("uint8"), np.dtype("uint16")):
            raise ValueError("Correction requires original uint8/uint16 ND2 pixels")
        indices = [int(c) for c, enabled in zip(model.indices, model.enabled) if enabled]
        if not indices:
            raise ValueError("No enabled correction channel is available")
        channels, height, width = native.shape
        model_payload = Path(model.path).read_bytes()
        method = "bffree" if getattr(model, "model_schema", "").startswith("BF_free_") else "h"
        consensus = False
        source_labels = source_mapping = None
        native_contract = "H(native)-then-box-mean"
        if method == "bffree":
            consensus = model.model_schema in SUPPORTED_SCHEMAS
            source_labels = [channel["label"] for channel in state.attrs["omero"]["channels"]]
            source_mapping = _bffree_source_binding(model, source_labels, native.shape)
            native_contract = (contract_for_schema(model.model_schema)["native_contract"]
                               if consensus else LEGACY_BFFREE_NATIVE_CONTRACT)
            if consensus:
                _consensus_fit_binding(model, model_record)
        config_payload = (_json_bytes(model_record["config"]) if method == "bffree" else
                          Path(__file__).with_name("h_config.json").read_bytes())
        revision = uuid.uuid4().hex
        provenance = {
            "schema": SCHEMA, "correction_id": revision, "method": method,
            "source": replacement._fingerprint,
            "selection": replacement.prior_manifest["selection"],
            "original_cache_generation": replacement.prior_manifest["generation"],
            "config": json.loads(config_payload), "config_sha256": _sha(config_payload),
            "fit": model_record,
        }
        if method == "bffree":
            provenance.update(source_channel_names=source_labels, source_channel_mapping=source_mapping,
                              source_shape_cyx=list(native.shape), model_schema=model.model_schema,
                              experimental=not consensus, native_coordinate_contract=native_contract)
        provenance_payload = _json_bytes(provenance)
        correction = {
            "schema": SCHEMA, "applied": True, "correction_id": revision, "method": method,
            "experimental": method == "bffree" and not consensus,
            "model_key": MODEL_KEY, "model_sha256": _sha(model_payload),
            "provenance_key": PROVENANCE_KEY, "provenance_sha256": _sha(provenance_payload),
            "channel_indices": indices, "raw_dtype": str(native.dtype),
            "raw_kind": replacement.prior_manifest["kind"],
            "native_coordinate_contract": native_contract,
        }
        if method == "bffree":
            correction["model_schema"] = model.model_schema
        attrs = copy.deepcopy(state.attrs)
        meta = attrs["nd2wsi"]
        levels = [copy.deepcopy(lv) for lv in meta["levels"] if str(lv["path"]) != "0"]
        if not levels or levels[0]["path"] != "1":
            raise ValueError("A complete halving pyramid is required for persistent correction")
        tile = int(replacement.prior_manifest["pyramid"]["tile"])
        root = DEFAULT_STORAGE.create_group(replacement.directory / STORE_NAME)
        arrays = {
            str(lv["path"]): DEFAULT_STORAGE.create_array(
                root, str(lv["path"]), (channels, lv["height"], lv["width"]), np.dtype("float32"), tile,
            )
            for lv in levels
        }
        total = channels * sum(lv["height"] * lv["width"] for lv in levels)
        completed = 0

        def done(count):
            nonlocal completed
            completed += count
            progress(0.8 * completed / total)

        for lv in levels:
            key = str(lv["path"])
            target = DEFAULT_STORAGE.chunk_array(arrays[key])

            def build(ci, y, y1, x, x1, key=key, target=target):
                if ci not in indices:
                    with state.busy:
                        block = np.asarray(state.root[key][ci, y:y1, x:x1], np.float32)
                elif key == "1":
                    raw = _read_native(state, replacement, ci, y * 2, y1 * 2, x * 2, x1 * 2)
                    block = _reduce2(model.correct(ci, raw, y * 2, x * 2), y1 - y, x1 - x)
                else:
                    previous = arrays[str(int(key) - 1)]
                    ph, pw = previous.shape[-2:]
                    raw = np.asarray(previous[ci, y * 2:min(y1 * 2, ph), x * 2:min(x1 * 2, pw)])
                    block = _reduce2(raw, y1 - y, x1 - x)
                if not np.isfinite(block).all():
                    raise ValueError("Nonfinite values in the corrected pyramid")
                target.write((ci, y // tile, x // tile), block[None])
                return (y1 - y) * (x1 - x)

            height_k, width_k = lv["height"], lv["width"]
            tiles = (
                (ci, y, min(y + tile, height_k), x, min(x + tile, width_k))
                for ci in range(channels)
                for y in range(0, height_k, tile)
                for x in range(0, width_k, tile)
            )
            # Level 1 carries nearly all of the work (native reads and the
            # model's gain); deeper levels read the level above, so they
            # follow it serially once it is complete.
            _build_tiles(replacement, build, tiles, done, _apply_workers(tile) if key == "1" else 1)
        meta.update(kind="overview", overview_of={"channels": channels, "height": height, "width": width},
                    dtype=str(native.dtype), display_dtype="float32", levels=levels, h_correction=correction)
        for multiscale in attrs["multiscales"]:
            multiscale["datasets"] = [ds for ds in multiscale["datasets"] if str(ds["path"]) != "0"]
        root.attrs.update(attrs)
        write_manifest(
            replacement.directory, replacement.source, replacement._fingerprint,
            replacement.prior_manifest["selection"], replacement.prior_manifest["selection"].get("z_resolved"),
            tile, tuple(native.shape), "float32", kind="overview", storage=meta.get("storage"),
        )
        replacement.check()
        _pack(replacement, lambda value: progress(0.8 + 0.18 * value), storage_version=CORRECTED_FORMAT_VERSION)
        replacement.manifest.update(format=CORRECTED_SINGLE_FILE_FORMAT, h_correction=correction)
        with SQLiteStore(replacement.staging) as store:
            with store.batch():
                store.write_bytes(MODEL_KEY, model_payload)
                store.write_bytes(PROVENANCE_KEY, provenance_payload)
                store.write_bytes(MANIFEST_NAME, _json_bytes(replacement.manifest))
        verified = load_model(replacement.staging, replacement.manifest)
        verified.data.close()
        with replacement.staging.open("r+b") as handle:
            os.fsync(handle.fileno())
        replacement.check()
        replacement._validate_before_publish()
        progress(1.0)
        return replacement
    except BaseException:
        replacement.close()
        raise


def prepare_revoke(state, event, progress):
    """Regenerate RAW pixels from original ND2, never toggle a retained RAW cache."""
    from .convert import convert, open_store
    from .reader import PlaneSelection

    replacement = PreparedReplacement(state, event)
    try:
        correction = replacement.prior_manifest.get("h_correction") or {}
        if correction.get("schema") != SCHEMA or correction.get("applied") is not True:
            raise ValueError("There is no persistent Shading Correction to revoke")
        selected = replacement.prior_manifest["selection"]
        selection = PlaneSelection(t=selected.get("t", 0), p=selected.get("p", 0), z=selected.get("z", "mid"))
        overview = correction.get("raw_kind") == "overview"
        tile = int(replacement.prior_manifest["pyramid"]["tile"])
        convert(
            replacement.source, replacement.directory / STORE_NAME,
            selection=selection, tile=tile, overview=overview, progress=False,
            on_progress=lambda value: (replacement.check(), progress(0.8 * value)),
        )
        replacement.check()
        root, attrs = open_store(replacement.directory / STORE_NAME)
        meta = attrs["nd2wsi"]
        if hasattr(root, "close"):
            root.close()
        if overview:
            ov = meta["overview_of"]
            shape = (ov["channels"], ov["height"], ov["width"])
        else:
            shape = (len(attrs["omero"]["channels"]), meta["levels"][0]["height"], meta["levels"][0]["width"])
        write_manifest(
            replacement.directory, replacement.source, replacement._fingerprint,
            selection.describe(), meta.get("selection", {}).get("z"), tile, shape,
            meta["dtype"], kind="overview" if overview else "full", storage=meta.get("storage"),
        )
        _pack(replacement, lambda value: progress(0.8 + 0.2 * value))
        replacement.check()
        replacement._validate_before_publish()
        return replacement
    except BaseException:
        replacement.close()
        raise

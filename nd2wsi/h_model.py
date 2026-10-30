"""Frozen H fit with the established periodic template family and sampler,
extended with:
1. Budgeted active sampling in chunks of at most 32 cells. Admission includes
   concurrently retained coordinates, observations and aligned training cells,
   as well as coarse, normalization and candidate-scoring temporaries. The
   estimate concerns fitting-owned workspace, not total application RSS.
2. Optional fallback to `H_adaptive_hybrid_subcell/1` (`h_adaptive.py`) when
   fixed-box validation regions have `insufficient_repeat_cells_*` due to
   tissue-border clipping (e.g., 1-2.nd2).
3. Support in `Model` for spatially varying cell-centered amplitude envelopes
   (`amplitude_nodes`, `envelope_cell_origin`) while remaining 100% compatible
   with legacy 9-entry `H_auto_refit/1` model archives.
"""

import json
from pathlib import Path

import numpy as np
from scipy import ndimage as ndi
from scipy.optimize import minimize, minimize_scalar


class Unsupported(RuntimeError):
    pass


def phase_delta(phase, delta):
    return ndi.map_coordinates(delta, [phase[0] - 0.5, phase[1] - 0.5], order=1, mode="nearest")


def sample_template(t, phase):
    return ndi.map_coordinates(
        t,
        [np.mod(phase[0], 1) * t.shape[0] - 0.5, np.mod(phase[1], 1) * t.shape[1] - 0.5],
        order=1,
        mode="grid-wrap",
    )


def field_phase(basis, delta, ys, xs, local=True):
    yy = (np.asarray(ys) - 15.5) / 32
    xx = (np.asarray(xs) - 15.5) / 32
    phase = np.array([basis[0, 0] * yy + basis[0, 1] * xx, basis[1, 0] * yy + basis[1, 1] * xx])
    if local:
        phase[1] += phase_delta(phase, delta)
    return phase


def field_at(t, basis, delta, ys, xs, local=True):
    return sample_template(t, field_phase(basis, delta, ys, xs, local))


def template(a, sigma):
    t = np.nanmedian(a, axis=0)
    ok = np.isfinite(t)
    if ok.mean() < 0.99:
        raise Unsupported("insufficient_finite_phase_template")
    num = ndi.gaussian_filter(np.where(ok, t, 0), sigma, mode="wrap")
    den = ndi.gaussian_filter(ok.astype(float), sigma, mode="wrap")
    t = num / den
    return t - t.mean()


def norm(a, strict=True):
    med = np.nanmedian(a, axis=(-2, -1), keepdims=True)
    invalid = (~np.isfinite(med)) | (med <= 0)
    if strict and np.any(invalid):
        raise Unsupported("nonpositive_cell_median")
    return (a - np.nanmean(a, axis=(-2, -1), keepdims=True)) / np.where(invalid, np.nan, med)


def r2(a, b):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    ok = np.isfinite(a) & np.isfinite(b)
    a = a[ok]
    b = b[ok]
    a -= a.mean()
    b -= b.mean()
    den = np.dot(a, a)
    return float(1 - np.dot(a - b, a - b) / den) if den > 1e-15 else None


def geometry(bf, box, pitch_native_yx, check=lambda: None):
    y0, y1, x0, x1 = box
    im = np.asarray(bf[y0:y1, x0:x1], float)
    z = ndi.gaussian_filter(im, 1) - ndi.gaussian_filter(im, 30)
    h, w = z.shape
    yy, xx = np.indices(z.shape)
    target = z * np.hanning(h)[:, None] * np.hanning(w)[None, :]
    fy, fx = np.fft.fftfreq(h), np.fft.fftfreq(w)
    power = np.abs(np.fft.fft2(target)) ** 2
    basis = []
    diagnostics = []
    for axis in (0, 1):
        low, high = np.asarray(pitch_native_yx[axis]) / 32
        main = fy[:, None] if axis == 0 else fx[None, :]
        cross = fx[None, :] if axis == 0 else fy[:, None]
        allowed = (main > 1 / high) & (main < 1 / low) & (np.abs(cross) < 0.004)
        if not np.any(allowed):
            raise Unsupported("insufficient_frequency_resolution")
        ij = np.unravel_index(np.argmax(np.where(allowed, power, -1)), power.shape)
        initial = np.array([fy[ij[0]], fx[ij[1]]])

        def loss(q):
            check()
            if not 1 / high < q[axis] < 1 / low or abs(q[1 - axis]) > 0.004:
                return 1e20
            s = np.sum(target * np.exp(-2j * np.pi * (q[0] * yy + q[1] * xx)))
            return -(abs(s) ** 2) / 1e10

        fit = minimize(
            loss, initial, method="Nelder-Mead", options={"maxiter": 180, "xatol": 1e-10}
        )
        if not fit.success:
            raise Unsupported("geometry_optimizer_failure")
        pitch = 32 / fit.x[axis]
        frac = (pitch - pitch_native_yx[axis][0]) / (
            pitch_native_yx[axis][1] - pitch_native_yx[axis][0]
        )
        if min(frac, 1 - frac) < 0.01:
            raise Unsupported("geometry_at_search_boundary")
        diagnostics.append(
            dict(
                axis=axis,
                initial=initial.tolist(),
                fitted=fit.x.tolist(),
                native_pitch=float(pitch),
                iterations=int(fit.nit),
            )
        )
        basis.append(fit.x)
    basis = np.array(basis)
    if np.linalg.det(basis) <= 0:
        raise Unsupported("invalid_phase_basis")
    return basis, diagnostics


def coords_for(basis, records, ny=192, nx=256, delta=None, factor=8):
    yy, xx = np.meshgrid((np.arange(ny) + 0.5) / ny, (np.arange(nx) + 0.5) / nx, indexing="ij")
    inv = np.linalg.inv(basis)
    coords = []
    for r in records:
        phase = np.array([yy + int(r["row"]), xx + int(r["column"])])
        base = phase.copy()
        if delta is not None:
            for _ in range(3):
                base[1] = phase[1] - phase_delta(base, delta)
        ov32 = (inv @ base.reshape(2, -1)).reshape(2, ny, nx)
        coords.append((ov32 * 32 + 15.5 - (factor - 1) / 2) / factor)
    return np.asarray(coords)


def sample(raw, coords):
    return np.stack(
        [
            np.stack(
                [ndi.map_coordinates(im, xy, order=1, mode="constant", cval=np.nan) for im in raw]
            )
            for xy in coords
        ]
    )


def _sample_and_norm_active_chunked(
    raw8,
    basis,
    records,
    active_mask,
    *,
    ny=192,
    nx=256,
    delta=None,
    chunk_size=32,
    check=lambda: None,
):
    """Stream `coords_for` -> `sample` -> `norm` in bounded chunks of `chunk_size`
    cells. Verify finiteness across ALL `records` while storing `coords` and
    `observed` only for active fit/selection cells (`active_mask`).
    """
    n_total = len(records)
    n_active = int(np.sum(active_mask))
    n_ch = raw8.shape[0]
    coords_active = np.empty((n_active, 2, ny, nx), dtype=np.float64) if delta is None else None
    obs_active = np.empty((n_active, n_ch, ny, nx), dtype=raw8.dtype)
    finite_by_ch = np.ones(n_ch, dtype=bool)

    write_pos = 0
    for start in range(0, n_total, chunk_size):
        check()
        end = min(n_total, start + chunk_size)
        rec_chunk = records[start:end]
        mask_chunk = active_mask[start:end]
        c_chunk = coords_for(basis, rec_chunk, ny, nx, delta=delta, factor=8)
        s_chunk = sample(raw8, c_chunk)
        n_chunk = norm(s_chunk, strict=False)
        for ch in range(n_ch):
            if not np.isfinite(n_chunk[:, ch]).all():
                finite_by_ch[ch] = False
        k = int(np.sum(mask_chunk))
        if k > 0:
            if coords_active is not None:
                coords_active[write_pos : write_pos + k] = c_chunk[mask_chunk]
            obs_active[write_pos : write_pos + k] = n_chunk[mask_chunk].astype(
                raw8.dtype, copy=False
            )
            write_pos += k
    return coords_active, obs_active, finite_by_ch


def phase_cells(raw32, basis, boxes, bfi, reference, saturation, fit_indices, check=lambda: None):
    h, w = raw32.shape[1:]
    corners = np.array([[0, 0], [0, w], [h, 0], [h, w]], float).T
    ph = basis @ corners
    records = []
    cells = []
    inv = np.linalg.inv(basis)
    yy, xx = np.meshgrid((np.arange(48) + 0.5) / 48, (np.arange(64) + 0.5) / 64, indexing="ij")
    for r in range(int(np.floor(ph[0].min())) - 1, int(np.ceil(ph[0].max())) + 1):
        check()
        for k in range(int(np.floor(ph[1].min())) - 1, int(np.ceil(ph[1].max())) + 1):
            co = (inv @ np.array([yy + r, xx + k]).reshape(2, -1)).reshape(2, 48, 64)
            lo = co.min(axis=(1, 2))
            hi = co.max(axis=(1, 2))
            group = next(
                (
                    g
                    for g, (y0, y1, x0, x1) in boxes.items()
                    if lo[0] >= y0 + 1 and hi[0] < y1 - 1 and lo[1] >= x0 + 1 and hi[1] < x1 - 1
                ),
                None,
            )
            if group is None:
                continue
            vals = np.stack(
                [ndi.map_coordinates(im, co, order=1, mode="constant", cval=np.nan) for im in raw32]
            )
            valid = (
                np.isfinite(vals[fit_indices]).all(axis=0)
                & (vals[bfi] > 0.6 * reference)
                & (vals[fit_indices] < saturation).all(axis=0)
            )
            if valid.mean() < 0.97:
                continue
            vals[:, ~valid] = np.nan
            records.append(dict(row=r, column=k, group=group, coverage=float(valid.mean())))
            cells.append(vals)
    if not cells:
        raise Unsupported("no_complete_supported_repeat_cells")
    return np.array(cells), records


def smooth_node_envelope_at(nodes_yx, origin_rc, phase_yx):
    """Evaluate a C1 smoothstep 2D half-cell node lattice at continuous phase coordinates."""
    gy, gx = np.broadcast_arrays(
        2.0 * (np.asarray(phase_yx[0], dtype=np.float64) - float(origin_rc[0])),
        2.0 * (np.asarray(phase_yx[1], dtype=np.float64) - float(origin_rc[1])),
    )
    max_y = float(nodes_yx.shape[0] - 1)
    max_x = float(nodes_yx.shape[1] - 1)

    cy = np.clip(gy, 0.0, max_y)
    cx = np.clip(gx, 0.0, max_x)
    dist_out = np.hypot(gy - cy, gx - cx)
    t_out = np.clip(1.0 - dist_out / 1.5, 0.0, 1.0)
    taper = t_out * t_out * (3.0 - 2.0 * t_out)

    iy = np.clip(np.floor(cy).astype(np.int64), 0, nodes_yx.shape[0] - 2)
    ix = np.clip(np.floor(cx).astype(np.int64), 0, nodes_yx.shape[1] - 2)
    ty = np.clip(cy - iy, 0.0, 1.0)
    tx = np.clip(cx - ix, 0.0, 1.0)
    sy = ty * ty * (3.0 - 2.0 * ty)
    sx = tx * tx * (3.0 - 2.0 * tx)

    val = (
        (1.0 - sy) * ((1.0 - sx) * nodes_yx[iy, ix] + sx * nodes_yx[iy, ix + 1])
        + sy * ((1.0 - sx) * nodes_yx[iy + 1, ix] + sx * nodes_yx[iy + 1, ix + 1])
    )
    return val * taper


class Model:
    LEGACY_SCHEMA = "H_auto_refit/1"
    ADAPTIVE_SCHEMA = "H_adaptive_hybrid_subcell/1"
    BASE_FIELDS = frozenset((
        "basis", "templates", "delta", "local", "old", "oldbasis", "oldtemplates",
        "channel_indices", "enabled",
    ))
    ADAPTIVE_FIELDS = BASE_FIELDS | frozenset((
        "amplitude_nodes", "envelope_cell_origin", "model_schema",
    ))

    def __init__(self, path):
        # Persisted models are small, verified NPZ entries inside .nd2svs.
        # A BytesIO keeps reopen read-only and avoids temporary sidecar files.
        self.path = None if hasattr(path, "read") else Path(path)
        self.data = np.load(path, allow_pickle=False)
        try:
            files = set(self.data.files)
            if (files not in (self.BASE_FIELDS, self.ADAPTIVE_FIELDS)
                    or len(self.data.files) != len(files)):
                raise ValueError("Unsupported correction model fields")
            self.model_schema = self.LEGACY_SCHEMA
            if files == self.ADAPTIVE_FIELDS:
                schema = self.data["model_schema"]
                if (schema.shape != () or schema.dtype.kind != "U"
                        or schema.item() != self.ADAPTIVE_SCHEMA):
                    raise ValueError("Unsupported adaptive correction model schema")
                self.model_schema = schema.item()
            self.basis = self.data["basis"]
            self.templates = self.data["templates"]
            self.delta = self.data["delta"]
            self.local = self.data["local"]
            self.old = self.data["old"]
            self.oldbasis = self.data["oldbasis"]
            self.oldtemplates = self.data["oldtemplates"]
            indices = self.data["channel_indices"]
            enabled = self.data["enabled"]
            if (indices.ndim != 1 or indices.dtype.kind not in "iu"
                    or not 1 <= len(indices) <= 3
                    or len(np.unique(indices)) != len(indices) or np.any(indices < 0)
                    or enabled.shape != indices.shape or enabled.dtype.kind != "b"):
                raise ValueError("Invalid correction channel mapping")
            self.indices = list(map(int, indices))
            self.enabled = list(map(bool, enabled))
            self.has_envelope = files == self.ADAPTIVE_FIELDS
            self.amplitude_nodes = self.data["amplitude_nodes"] if self.has_envelope else None
            self.envelope_cell_origin = self.data["envelope_cell_origin"] if self.has_envelope else None
            self.envelope_origin = self.envelope_cell_origin
            self.validate()
        except BaseException:
            self.data.close()
            raise

    def validate(self):
        """Prove gain bounds for every coordinate, not just a sampled tile.

        Linear periodic template interpolation is a convex combination of its
        stored values. The smoothstep envelope interpolates nonnegative nodes
        with convex weights, then multiplies by a taper in [0, 1]. Therefore
        its range lies in [0, max(nodes)] over the entire plane, including the
        stored lattice boundary and its exterior. These conservative bounds
        need not assume that template and node extrema coincide in an image.

        Legacy archives retain their original >=0.25 contract. The adaptive
        schema additionally promises <=1.75; no archive is silently clipped.
        Disabled channels return identity and have no active gain restriction.
        """
        count = len(self.indices)
        if (self.basis.shape != (2, 2) or self.oldbasis.shape != (2, 2)
                or self.delta.ndim != 2 or min(self.delta.shape, default=0) < 1
                or self.templates.ndim != 3 or self.templates.shape[0] != count
                or min(self.templates.shape[1:], default=0) < 1
                or self.oldtemplates.ndim != 3 or self.oldtemplates.shape[0] != count
                or min(self.oldtemplates.shape[1:], default=0) < 1
                or self.local.shape != (count,) or self.local.dtype.kind != "b"
                or self.old.shape != (count,) or self.old.dtype.kind != "b"):
            raise ValueError("Invalid correction model geometry")
        arrays = [self.basis, self.oldbasis, self.delta, self.templates, self.oldtemplates]
        if self.has_envelope:
            if (self.amplitude_nodes.ndim != 3 or self.amplitude_nodes.shape[0] != count
                    or min(self.amplitude_nodes.shape[1:], default=0) < 2
                    or self.envelope_cell_origin.shape != (2,)):
                raise ValueError("Invalid adaptive amplitude envelope geometry")
            arrays.extend((self.amplitude_nodes, self.envelope_cell_origin))
        for value in arrays:
            if value.dtype.kind not in "iuf" or not np.isfinite(value).all():
                raise ValueError("Nonfinite or non-real correction model")
        determinants = [np.linalg.det(self.basis), np.linalg.det(self.oldbasis)]
        if not np.isfinite(determinants).all() or min(determinants) <= 0:
            raise ValueError("Invalid correction phase basis")
        if self.has_envelope and np.any(self.old):
            raise ValueError("Adaptive correction cannot select a legacy template")
        if self.has_envelope and np.any(self.amplitude_nodes < 0):
            raise ValueError("Invalid negative adaptive amplitude envelope")
        bounds = []
        for ci, enabled in enumerate(self.enabled):
            if not enabled:
                bounds.append((1.0, 1.0))
                continue
            t = self.oldtemplates[ci] if self.old[ci] else self.templates[ci]
            low, high = float(np.min(t)), float(np.max(t))
            if self.has_envelope:
                amplitude_max = float(np.max(self.amplitude_nodes[ci]))
                low, high = min(0.0, low * amplitude_max), max(0.0, high * amplitude_max)
            low, high = 1.0 + low, 1.0 + high
            if (not np.isfinite([low, high]).all() or low < 0.25
                    or (self.has_envelope and high > 1.75)):
                raise ValueError("Invalid correction gain bounds over stored model domain")
            bounds.append((low, high))
        self.gain_bounds = bounds

    _shared_gains = None

    def share_gains(self, cache, tag, tile):
        """Reuse the gains of whole pyramid tiles through ``cache``.

        ``cache`` is a bytes-bounded LRU with ``get(key)`` and ``put(key,
        array)``; the viewer passes its process-wide decoded-tile cache and
        purges ``tag`` when the slide closes. Only tile-aligned windows of at
        most one tile are kept: that is what the tile renderer reads again on
        every contrast change, while strips and export windows would only
        evict them. Kept gains are read-only and bit-identical to fresh ones.
        """
        self._shared_gains = (cache, tag, int(tile))

    def gain(self, c, shape, origin):
        shared = self._shared_gains
        if shared is not None:
            cache, tag, tile = shared
            h, w = (int(v) for v in shape)
            y, x = (int(v) for v in origin)
            if 0 < h <= tile and 0 < w <= tile and y % tile == 0 and x % tile == 0:
                key = (tag, int(c), y, x, h, w)
                out = cache.get(key)
                if out is None:
                    out = self._gain(c, (h, w), (y, x))
                    out.setflags(write=False)
                    cache.put(key, out)
                return out
        return self._gain(c, shape, origin)

    def _gain(self, c, shape, origin):
        ci = self.indices.index(int(c))
        h, w = shape
        y, x = origin
        if not self.enabled[ci]:
            return np.ones(shape, np.float32)
        xx = x + np.arange(w)[None, :]
        out = np.empty(shape, np.float32)
        has_envelope = (
            self.amplitude_nodes is not None and self.envelope_cell_origin is not None
        )
        old = bool(self.old[ci])
        local = bool(self.local[ci])
        for row in range(0, h, 128):
            yy = y + np.arange(row, min(h, row + 128))[:, None]
            # The current template and the adaptive envelope sample the same
            # current-basis phase, local phase_delta included: compute it once.
            phase = (
                field_phase(self.basis, self.delta, yy, xx, local)
                if has_envelope or not old else None
            )
            if old:
                f = field_at(self.oldtemplates[ci], self.oldbasis, self.delta, yy, xx, False)
            else:
                f = sample_template(self.templates[ci], phase)
            if has_envelope:
                amp = smooth_node_envelope_at(
                    self.amplitude_nodes[ci], self.envelope_cell_origin, phase
                )
                f = amp * f
            out[row : row + len(yy)] = 1 + f
        if (not np.isfinite(out).all() or (out.size and out.min() < 0.25)
                or (self.has_envelope and out.size and out.max() > 1.75)):
            raise Unsupported("invalid_fitted_gain")
        return out

    def correct(self, c, raw, y, x, support=None):
        out = np.asarray(raw, np.float32) / self.gain(c, raw.shape, (y, x))
        if support is not None:
            out = np.where(support, out, 0).astype(np.float32)
        return out


def fit(
    raw8,
    raw32,
    meta,
    config,
    output,
    *,
    check=lambda: None,
    max_working_bytes=None,
    progress=lambda _fraction: None,
):
    """Release the original fitter's temporaries before adaptive fallback."""
    fallback_reason = None
    try:
        return _fit_impl(raw8, raw32, meta, config, output, check=check,
                         max_working_bytes=max_working_bytes, progress=progress)
    except Unsupported as exc:
        if not str(exc).startswith("insufficient_repeat_cells_"):
            raise
        fallback_reason = str(exc)
    # Dispatch outside the exception handler so its traceback does not retain
    # the original frame's coarse samples while adaptive allocations are live.
    h_adapt = config.get("h_adaptive")
    if h_adapt is None:
        local_cfg_path = Path(__file__).with_name("h_config.json")
        h_adapt = (json.loads(local_cfg_path.read_text(encoding="utf-8")).get("h_adaptive", {})
                   if local_cfg_path.is_file() else {})
        config = {**config, "h_adaptive": h_adapt}
    if not h_adapt.get("enabled_on_insufficient_repeat_cells", False):
        raise Unsupported(fallback_reason)
    from .h_adaptive import fit_adaptive_from_config
    return fit_adaptive_from_config(
        raw8, raw32, meta, config, output, fallback_reason=fallback_reason,
        check=check, max_working_bytes=max_working_bytes, progress=progress,
    )


def _legacy_fit_workspace_plan(
    *, n_total, n_active, n_train, n_selection, n_channels, input_itemsize,
    coarse_itemsize, n_targets, geometry_bytes=0, discovery_bytes=0,
    lattice_cell_upper_bound=0, max_working_bytes=None,
):
    """Reserve the high-water of each allocation phase before fitting.

    Caller-owned input arrays and decoder/allocator/runtime memory are excluded.
    Floating temporary arrays are budgeted at at least float64 width even when
    NumPy preserves float32. Sampling accounts for old loop temporaries while
    the next chunk is assembled; scoring includes the old estimate plus the
    new per-cell list and stacked estimate. Retained arrays are charged during
    *both* aligned sampling and candidate evaluation, not just the first pass.
    """
    from .h_adaptive import _require_workspace
    pixels = 192 * 256
    scratch_itemsize = max(8, int(input_itemsize), int(coarse_itemsize))
    # Python records, small arrays, lattice grids and numerical-library scratch.
    reserve = 32 * 1024**2 + n_total * 32 * 1024 + lattice_cell_upper_bound * 4096
    active = n_active * pixels * (16 + n_channels * input_itemsize)
    aligned = n_train * pixels * n_channels * input_itemsize
    retained = active + aligned
    cell_scratch = 256 * pixels
    candidate_arrays = (32 + 4 * n_targets) * pixels * scratch_itemsize
    stages = {
        "geometry_bytes": int(geometry_bytes),
        "coarse_discovery_bytes": int(discovery_bytes),
        # Raw and normalized coarse stacks coexist with indexed template inputs,
        # nanmedian copies/masks and reduction temporaries until explicitly freed.
        "coarse_templates_bytes": reserve + 5 * n_total * 48 * 64 * n_channels * scratch_itemsize,
        "geometry_refinement_bytes": reserve + n_train * 48 * 96 * (32 + 6 * scratch_itemsize),
        "active_template_and_phase_bytes": reserve + active + 3 * n_train * pixels * scratch_itemsize + cell_scratch,
        "candidate_build_bytes": reserve + retained + n_train * pixels * (n_channels * input_itemsize + 3 * scratch_itemsize) + candidate_arrays + cell_scratch,
        "candidate_scoring_bytes": reserve + retained + 3 * n_selection * pixels * scratch_itemsize + candidate_arrays + cell_scratch,
    }
    # Smaller sampling chunks help only when retained arrays and scoring fit.
    _require_workspace(max(stages.values()), max_working_bytes)
    for chunk in range(min(32, max(1, n_total)), 0, -1):
        # Up to three coordinate copies and eight sampled/normalization scalar
        # buffers per channel, including temporaries from the prior iteration.
        chunk_scratch = min(chunk, n_total) * pixels * (48 + 8 * n_channels * scratch_itemsize)
        aligned_scratch = min(chunk, n_train) * pixels * (48 + 8 * n_channels * scratch_itemsize)
        sampling = reserve + active + chunk_scratch
        alignment = reserve + retained + aligned_scratch
        peak = max(*stages.values(), sampling, alignment)
        if max_working_bytes is None or peak <= max_working_bytes:
            return {
                "scope": "additional_fitting_workspace_excluding_inputs_decoder_and_process_overhead",
                "limit_bytes": None if max_working_bytes is None else int(max_working_bytes),
                "estimated_peak_bytes": int(peak),
                "chunk_cells": chunk,
                "retained_active_bytes": active,
                "retained_aligned_bytes": aligned,
                "retained_active_and_aligned_bytes": retained,
                "phase_estimates": {**stages, "active_sampling_bytes": sampling, "aligned_sampling_bytes": alignment},
            }
    raise Unsupported("fit_working_memory_limit")


def _fit_impl(
    raw8,
    raw32,
    meta,
    config,
    output,
    *,
    check=lambda: None,
    max_working_bytes=None,
    progress=lambda _fraction: None,
):
    check()
    output = Path(output)
    mapping = meta["channel_mapping"]
    bfi = mapping.get("BF")
    if bfi is None:
        raise Unsupported("BF_absent_no_frozen_fallback")
    h, w = raw32.shape[1:]
    hc = config["h_auto"]
    configured_targets = hc.get("target_channels", ["FITC", "CY3", "BF"])
    if (not isinstance(configured_targets, list) or not configured_targets
            or any(name not in ("FITC", "CY3", "BF") for name in configured_targets)
            or len(set(configured_targets)) != len(configured_targets)):
        raise ValueError("H_auto target_channels must contain unique FITC, CY3 or BF names")
    fixed = {
        "geometry_search_boundary_fraction_guard": 0.01,
        "BF_support_relative_to_train_median": 0.6,
        "BF_support_minimum_repeat_coverage": 0.97,
        "BF_support_minimum_phase_coverage": 0.98,
        "minimum_valid_gain": 0.25,
        "gain_clipping": False,
        "phase_template_shape_yx": [192, 256],
        "candidate_sigma_phase_bins": [0.5, 1.0, 2.0],
        "candidate_BF_residual_weights": [0.0, 0.5, 1.0],
        "local_phase_candidates": [False, True],
        "coarse_own32_comparator": True,
    }
    for key, val in fixed.items():
        if hc[key] != val:
            raise ValueError("Configuration disagrees with fixed original H constant: " + key)
    boxes = {
        g: [int(f[0] * h), min(h, int(f[1] * h)), int(f[2] * w), min(w, int(f[3] * w))]
        for g, f in hc["split_fractions_y0y1x0x1"].items()
    }
    # The preliminary fixed-box attempt precedes adaptive dispatch and must
    # obey the same cap before geometry/coarse-sampling allocations as well.
    from .h_adaptive import _require_workspace, adaptive_workspace_plan
    initial_plan = adaptive_workspace_plan(raw8, raw32, boxes["train"])
    _require_workspace(initial_plan["estimated_peak_bytes"], max_working_bytes)
    y0, y1, x0, x1 = boxes["train"]
    bf = raw32[bfi, y0:y1, x0:x1]
    v = bf[bf > 0]
    if len(v) < 100:
        raise Unsupported("insufficient_BF_support")
    ref = float(np.median(v))
    del v
    sensor = meta["channel_acquisition_settings"][bfi]["sensor_pixels_xy"]
    if not isinstance(sensor, dict) or min(sensor.get("cx", 0), sensor.get("cy", 0)) <= 0:
        raise Unsupported("sensor_geometry_unknown")
    pitch = [
        [sensor[k] * hc["sensor_pitch_fraction"][0], sensor[k] * hc["sensor_pitch_fraction"][1]]
        for k in ["cy", "cx"]
    ]
    progress(0.05)
    oldbasis, gd = geometry(raw32[bfi], boxes["train"], pitch, check)
    coarse_plan = adaptive_workspace_plan(raw8, raw32, boxes["train"], oldbasis)
    _require_workspace(coarse_plan["discovery_bytes"], max_working_bytes)
    saturation = (
        float(np.iinfo(np.dtype(meta["dtype"])).max)
        if np.dtype(meta["dtype"]).kind in "ui"
        else np.inf
    )
    fit_indices = [bfi] + [mapping[n] for n in ["FITC", "CY3"] if n in mapping]
    progress(0.12)
    cells32, records = phase_cells(raw32, oldbasis, boxes, bfi, ref, saturation, fit_indices, check)
    groups = np.array([r["group"] for r in records])
    train = groups == "train"
    for g in ["train", "heldout_columns", "heldout_rows"]:
        if int(np.sum(groups == g)) < hc["minimum_repeat_cells_per_fit_group"]:
            raise Unsupported("insufficient_repeat_cells_" + g)
    active_mask = np.isin(groups, ["train", "heldout_columns", "heldout_rows"])
    memory_plan = _legacy_fit_workspace_plan(
        n_total=len(records), n_active=int(np.sum(active_mask)), n_train=int(np.sum(train)),
        n_selection=max(int(np.sum(groups == group)) for group in ("heldout_columns", "heldout_rows")),
        n_channels=raw8.shape[0], input_itemsize=raw8.dtype.itemsize,
        coarse_itemsize=raw32.dtype.itemsize, n_targets=len(configured_targets),
        geometry_bytes=initial_plan["geometry_bytes"], discovery_bytes=coarse_plan["discovery_bytes"],
        lattice_cell_upper_bound=coarse_plan["lattice_cell_upper_bound"],
        max_working_bytes=max_working_bytes,
    )
    chunk_cells = memory_plan["chunk_cells"]
    n32 = norm(cells32, strict=False)
    oldtemplates = np.zeros((raw32.shape[0], 48, 64))
    unavailable = {}
    for c in fit_indices:
        try:
            oldtemplates[c] = template(n32[train, c], 0.45)
        except Unsupported as e:
            if c == bfi:
                raise
            unavailable[c] = str(e)
    evidence = {
        g: r2(template(n32[groups == g, bfi], 0.45), oldtemplates[bfi])
        for g in ["heldout_columns", "heldout_rows"]
    }
    if any(v is None or v <= 0 for v in evidence.values()):
        raise Unsupported("no_reproducible_BF_periodicity:" + json.dumps(evidence))
    # Neither coarse stack is used after the channel templates/evidence exist.
    del cells32, n32
    progress(0.22)
    trainrec = [r for r in records if r["group"] == "train"]

    def basisof(q):
        return oldbasis + np.asarray(q).reshape(2, 2) * np.array([[1e-5, 1e-6], [1e-6, 1e-5]])

    def loss(q):
        check()
        co = coords_for(basisof(q), trainrec, 48, 96)
        a = norm(np.stack([ndi.map_coordinates(raw8[bfi], xy, order=1) for xy in co]))
        res = a - np.median(a, axis=0)
        return float(np.mean(np.minimum(res**2, 0.0025)))

    start = loss(np.zeros(4))
    opt = minimize(
        loss,
        np.zeros(4),
        method="Powell",
        bounds=[(-8, 8)] * 4,
        options={"maxiter": 12, "xtol": 0.005, "ftol": 1e-5},
    )
    basis = basisof(opt.x) if np.isfinite(opt.fun) and opt.fun < start else oldbasis
    check()
    progress(0.4)
    coords, observed, obs_finite = _sample_and_norm_active_chunked(
        raw8, basis, records, active_mask, chunk_size=chunk_cells, check=check
    )
    active_groups = groups[active_mask]
    active_train = active_groups == "train"
    if not obs_finite[bfi]:
        raise Unsupported("nonfinite_refined_BF_cells")
    basebf = template(observed[active_train, bfi], 0.7)
    profile = np.median(basebf, axis=0)
    profile -= profile.mean()
    ny, nx = meta["sizes"]["Y"], meta["sizes"]["X"]
    corners = np.array([[0, 0], [0, nx], [ny, 0], [ny, nx]], float).T
    maxphase = (basis @ (corners - 15.5) / 32).max(axis=1)
    grid = np.full((int(np.ceil(maxphase[0])) + 2, int(np.ceil(maxphase[1])) + 2), np.nan)
    shifts = []
    for row in range(grid.shape[0]):
        check()
        progress(0.45 + 0.2 * row / max(1, grid.shape[0]))
        for col in range(grid.shape[1]):
            xy = coords_for(basis, [dict(row=row, column=col)], 48, 256)[0]
            if xy.min() < 1 or xy[0].max() >= raw8.shape[1] - 2 or xy[1].max() >= raw8.shape[2] - 2:
                continue
            a = ndi.map_coordinates(raw8[bfi], xy, order=1)
            if np.mean(a > 0.6 * ref) < 0.98:
                continue
            target = np.median(norm(a), axis=0)
            target -= target.mean()

            def shiftloss(d):
                check()
                t = ndi.map_coordinates(
                    profile, [np.arange(256) + d * 256], order=1, mode="grid-wrap"
                )
                scale = np.dot(t, target) / max(np.dot(t, t), 1e-12)
                return float(np.mean(np.minimum((target - scale * t) ** 2, 0.0025)))

            f = minimize_scalar(
                shiftloss, bounds=(-0.035, 0.035), method="bounded", options={"xatol": 1e-5}
            )
            grid[row, col] = f.x
            shifts.append(dict(row=row, column=col, x_shift_cycles=float(f.x)))
    finite = np.isfinite(grid)
    if not finite.any():
        raise Unsupported("no_BF_local_phase_cells")
    nearest = ndi.distance_transform_edt(~finite, return_distances=False, return_indices=True)
    delta = ndi.median_filter(grid[tuple(nearest)], size=3, mode="nearest")
    progress(0.65)
    _, aligned, aligned_finite = _sample_and_norm_active_chunked(
        raw8,
        basis,
        trainrec,
        np.ones(len(trainrec), dtype=bool),
        delta=delta,
        chunk_size=chunk_cells,
        check=check,
    )
    selected = []
    all_scores = []
    templates = []
    locals_ = []
    olds = []
    indices = []
    enabled = []
    oldout = []
    ids_by_group = {g: np.where(active_groups == g)[0] for g in ["heldout_columns", "heldout_rows"]}
    target_names = [name for name in configured_targets if name in mapping]
    for target_i, name in enumerate(target_names):
        check()
        if name not in mapping:
            continue
        c = mapping[name]
        if (
            c in unavailable
            or not obs_finite[c]
            or not aligned_finite[c]
        ):
            selected.append(
                dict(
                    channel=name,
                    channel_index=c,
                    model=None,
                    outcome="unchanged_nonpositive_or_unavailable_channel_template",
                    reason=unavailable.get(c, "nonfinite_normalized_channel_cells"),
                )
            )
            templates.append(np.zeros((192, 256)))
            locals_.append(False)
            olds.append(False)
            indices.append(c)
            enabled.append(False)
            oldout.append(oldtemplates[c])
            continue
        opts = [("previous_own32", oldtemplates[c], False, True)]
        for local in [False, True]:
            tr = aligned if local else observed[active_train]
            for sigma in [0.5, 1.0, 2.0]:
                own = template(tr[:, c], sigma)
                bf = template(tr[:, bfi], sigma)
                den = np.sum(bf * bf)
                if den <= 1e-15:
                    raise Unsupported("zero_BF_template_energy")
                alpha = np.sum(own * bf) / den
                for weight in [0.0, 0.5, 1.0]:
                    opts.append(
                        (
                            f"hires_local{int(local)}_sigma{sigma}_residual{weight}",
                            alpha * bf + weight * (own - alpha * bf),
                            local,
                            False,
                        )
                    )
        best = None
        for candidate_i, (key, t, local, isold) in enumerate(opts):
            check()
            progress(0.68 + 0.3 * (target_i + candidate_i / len(opts)) / len(target_names))
            scores = {}
            for g, ids in ids_by_group.items():
                est = np.stack(
                    [
                        field_at(
                            t,
                            oldbasis if isold else basis,
                            delta,
                            coords[i, 0] * 8 + 3.5,
                            coords[i, 1] * 8 + 3.5,
                            local,
                        )
                        for i in ids
                    ]
                )
                scores[g] = r2(template(observed[ids, c], 0.35), template(est, 0.35))
            score = (
                float(np.mean(list(scores.values())))
                if all(v is not None for v in scores.values())
                else -np.inf
            )
            all_scores.append(
                dict(
                    channel=name,
                    model=key,
                    selection_mean_r2=None if not np.isfinite(score) else score,
                    **scores,
                )
            )
            if best is None or score > best[0]:
                best = (score, key, t, local, isold, scores)
        score, key, t, local, isold, scores = best
        ok = (
            np.isfinite(score)
            and all(v is not None and v > 0 for v in scores.values())
            and np.isfinite(t).all()
            and np.min(1 + t) >= 0.25
        )
        selected.append(
            dict(
                channel=name,
                channel_index=c,
                model=key,
                development_mean_r2=None if not np.isfinite(score) else score,
                scores=scores,
                outcome="fitted" if ok else "unchanged_no_reproducible_or_valid_channel_template",
                gain_min=float(np.min(1 + t)),
                gain_max=float(np.max(1 + t)),
            )
        )
        templates.append(np.zeros((192, 256)) if isold else t)
        locals_.append(local)
        olds.append(isold)
        indices.append(c)
        enabled.append(ok)
        oldout.append(oldtemplates[c])
    check()
    np.savez_compressed(
        output,
        basis=basis,
        templates=np.asarray(templates),
        delta=delta,
        local=np.array(locals_),
        old=np.array(olds),
        oldbasis=oldbasis,
        oldtemplates=np.asarray(oldout),
        channel_indices=np.array(indices),
        enabled=np.array(enabled),
    )
    rec = dict(
        schema="H_auto_refit/1",
        native_pitch_yx=(32 / np.diag(basis)).tolist(),
        initial_geometry=gd,
        initial_basis=oldbasis.tolist(),
        basis=basis.tolist(),
        bf_reference=ref,
        bf_periodicity_selection_r2=evidence,
        split_boxes_overview32=boxes,
        repeat_cells=records,
        geometry_loss_before=start,
        geometry_loss_after=float(opt.fun),
        geometry_optimizer_success=bool(opt.success),
        bf_local_phase_cells=shifts,
        selected=selected,
        candidates=all_scores,
        working_memory=memory_plan,
        adaptation="metadata channels, sensor-based pitch prior, normalized original partitions, relative BF support, explicit repeat evidence gates; original H candidate family and global sampler retained",
        diagnostic_sampling_factors=[32, 8],
        fits_7_map_transferred=False,
        target_channels=target_names,
        DAPI_modified=False,
        BF_modified=any(item["channel"] == "BF" and item["outcome"] == "fitted" for item in selected),
    )
    output.with_suffix(".json").write_text(json.dumps(rec, indent=2, allow_nan=False) + "\n")
    progress(1.0)
    return Model(output), rec

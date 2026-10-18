"""Frozen H fit with the established periodic template family and sampler.

Adapted from validate_transfer.py, fit_best.py and model.py. This module never
loads slide-7 fitted assets. Geometry and templates are independently estimated.

Source: ExM_Generality_Benchmark/code/h_auto.py, SHA-256
15e1ca080d3a808feff90e7d03e87155a2363e1b646605de57fef012666a28b0.
Only cooperative cancellation and an operational memory guard are added here;
the fit, candidate family, thresholds, and coordinate sampler are unchanged.
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


def field_at(t, basis, delta, ys, xs, local=True):
    yy = (np.asarray(ys) - 15.5) / 32
    xx = (np.asarray(xs) - 15.5) / 32
    phase = np.array([basis[0, 0] * yy + basis[0, 1] * xx, basis[1, 0] * yy + basis[1, 1] * xx])
    if local:
        phase[1] += phase_delta(phase, delta)
    return sample_template(t, phase)


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


class Model:
    def __init__(self, path):
        self.path = Path(path)
        self.data = np.load(path)
        self.basis = self.data["basis"]
        self.templates = self.data["templates"]
        self.delta = self.data["delta"]
        self.local = self.data["local"]
        self.old = self.data["old"]
        self.oldbasis = self.data["oldbasis"]
        self.oldtemplates = self.data["oldtemplates"]
        self.indices = list(map(int, self.data["channel_indices"]))
        self.enabled = list(map(bool, self.data["enabled"]))

    def gain(self, c, shape, origin):
        ci = self.indices.index(int(c))
        h, w = shape
        y, x = origin
        if not self.enabled[ci]:
            return np.ones(shape, np.float32)
        xx = x + np.arange(w)[None, :]
        out = np.empty(shape, np.float32)
        for row in range(0, h, 128):
            yy = y + np.arange(row, min(h, row + 128))[:, None]
            if self.old[ci]:
                f = field_at(self.oldtemplates[ci], self.oldbasis, self.delta, yy, xx, False)
            else:
                f = field_at(
                    self.templates[ci], self.basis, self.delta, yy, xx, bool(self.local[ci])
                )
            out[row : row + len(yy)] = 1 + f
        if not np.isfinite(out).all() or out.min() < 0.25:
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
    check()
    output = Path(output)
    mapping = meta["channel_mapping"]
    bfi = mapping.get("BF")
    if bfi is None:
        raise Unsupported("BF_absent_no_frozen_fallback")
    h, w = raw32.shape[1:]
    hc = config["h_auto"]
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
    y0, y1, x0, x1 = boxes["train"]
    bf = raw32[bfi, y0:y1, x0:x1]
    v = bf[bf > 0]
    if len(v) < 100:
        raise Unsupported("insufficient_BF_support")
    ref = float(np.median(v))
    sensor = meta["channel_acquisition_settings"][bfi]["sensor_pixels_xy"]
    if not isinstance(sensor, dict) or min(sensor.get("cx", 0), sensor.get("cy", 0)) <= 0:
        raise Unsupported("sensor_geometry_unknown")
    pitch = [
        [sensor[k] * hc["sensor_pitch_fraction"][0], sensor[k] * hc["sensor_pitch_fraction"][1]]
        for k in ["cy", "cx"]
    ]
    progress(0.05)
    oldbasis, gd = geometry(raw32[bfi], boxes["train"], pitch, check)
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
    # Bound the largest simultaneous coordinate/sample/normalization arrays.
    # This is an operational abstention, not a scientific acceptance rule.
    work_bytes = len(records) * 192 * 256 * (16 + 12 * raw32.shape[0])
    if max_working_bytes is not None and work_bytes > max_working_bytes:
        raise Unsupported("fit_working_memory_limit")
    check()
    progress(0.4)
    coords = coords_for(basis, records)
    observed = norm(sample(raw8, coords), strict=False)
    if not np.isfinite(observed[:, bfi]).all():
        raise Unsupported("nonfinite_refined_BF_cells")
    basebf = template(observed[train, bfi], 0.7)
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
    aligned = norm(sample(raw8, coords_for(basis, trainrec, delta=delta)), strict=False)
    selected = []
    all_scores = []
    templates = []
    locals_ = []
    olds = []
    indices = []
    enabled = []
    oldout = []
    ids_by_group = {g: np.where(groups == g)[0] for g in ["heldout_columns", "heldout_rows"]}
    target_names = [name for name in ["FITC", "CY3"] if name in mapping]
    for target_i, name in enumerate(target_names):
        check()
        if name not in mapping:
            continue
        c = mapping[name]
        if (
            c in unavailable
            or not np.isfinite(observed[:, c]).all()
            or not np.isfinite(aligned[:, c]).all()
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
            tr = aligned if local else observed[train]
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
        adaptation="metadata channels, sensor-based pitch prior, normalized original partitions, relative BF support, explicit repeat evidence gates; original H candidate family and global sampler retained",
        diagnostic_sampling_factors=[32, 8],
        fits_7_map_transferred=False,
        DAPI_BF_modified=False,
    )
    output.with_suffix(".json").write_text(json.dumps(rec, indent=2, allow_nan=False) + "\n")
    progress(1.0)
    return Model(output), rec

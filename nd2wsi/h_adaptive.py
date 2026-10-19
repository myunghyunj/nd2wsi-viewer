"""Adaptive sub-cell shading correction (`H_adaptive_hybrid_subcell/1`) for stitched ND2 scans.

When a scan has reproducible periodic vertical shading in BF (`r2 > 0` across
all held-out spatial groups) but contains too few complete repeat cells in a
fixed validation box for 2D `H_auto_refit/1` (such as `1-2.nd2`), this module:
  1. Estimates the X-periodic geometry (`supported_x_geometry`) and enumerates
     all complete and partially-supported lattice cells across the acquired
     tissue area (`assign_group` over `train`, `heldout_columns`, `heldout_rows`,
     `heldout_both`).
  2. Estimates the 1D periodic X-template from complete `train` cells and
     verifies positive template reproducibility across all held-out groups.
  3. Cleans 2D localized fluorescence objects from sub-cell estimation strips
     (`Strip A`: rows 8..68, `Strip B`: rows 68..128; background mask computed
     strictly on rows 0..132) while holding out `Strip C` (rows 136..176)
     strictly for candidate evaluation and admission.
  4. Restores locally supported amplitudes with A/B half-strip trough bounds and
     an empirical template-mismatch allowance. Missing input retains each
     channel's leave-neighborhood-out prediction bound. The candidate
     (`M9_local_trough_bound_hybrid_subcell_x`) is admitted on held-out `Strip C`
     across all spatial groups before the target channels (`FITC`, `CY3`, `BF`)
     are persisted into the `.nd2svs` model archive.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage as ndi
from scipy.optimize import minimize

from . import h_model as base

ADAPTIVE_SCHEMA = "H_adaptive_hybrid_subcell/1"
POLY2_SCHEMA = "H_poly2_envelope_x/1"
BF_COUPLED_SCHEMA = "H_bf_coupled_smooth_x/1"
GLOBAL_X_SCHEMA = "H_vertical_supported/1"

DEFAULT_JUSTIFIED_CRITERIA = {
    "reduced_resolution_gates": {
        "min_supported_cells_per_split_group": 4,
        "min_template_reproducibility_r2": 0.0,
        "active_band_cell_definition": {
            "min_raw_block_c_amplitude": 0.20,
            "min_raw_block_c_shape_r2": 0.50,
        },
        "min_active_cells_per_heldout_group": 2,
        "min_median_block_c_amplitude_reduction_per_heldout_group": 0.25,
        "max_median_block_c_energy_ratio_per_heldout_group": 0.85,
        "min_median_block_c_residual_amplitude_per_heldout_group": -0.05,
        "min_p10_block_c_residual_amplitude_all_heldout": -0.10,
        "min_worst_cell_block_c_residual_amplitude_all_heldout": -0.15,
        "max_abs_residual_amplitude_on_weak_cells": 0.15,
    },
    "gain_and_engineering_invariants": {
        "minimum_valid_gain": 0.25,
        "maximum_valid_gain": 1.75,
    },
}


def validated_acceptance_criteria(config: dict[str, Any]) -> dict[str, Any]:
    """Require an explicit complete contract; never silently substitute defaults."""
    criteria = config.get("acceptance_criteria")

    def validate(value, reference, path):
        if isinstance(reference, dict):
            if not isinstance(value, dict) or value.keys() != reference.keys():
                raise ValueError("Invalid adaptive acceptance criteria keys: " + path)
            for key in reference:
                validate(value[key], reference[key], path + "." + key)
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
            raise ValueError("Invalid adaptive acceptance criterion: " + path)
        if isinstance(reference, int) and (int(value) != value or value < 1):
            raise ValueError("Adaptive cell count must be a positive integer: " + path)
        if not -1.0 <= value <= 2.0 and not isinstance(reference, int):
            raise ValueError("Adaptive acceptance criterion out of range: " + path)

    validate(criteria, DEFAULT_JUSTIFIED_CRITERIA, "acceptance_criteria")
    gates = criteria["reduced_resolution_gates"]
    for key in ("min_template_reproducibility_r2", "min_median_block_c_amplitude_reduction_per_heldout_group",
                "max_median_block_c_energy_ratio_per_heldout_group", "max_abs_residual_amplitude_on_weak_cells"):
        if not 0 <= gates[key] <= 1:
            raise ValueError("Adaptive acceptance criterion must be in [0, 1]: " + key)
    active = gates["active_band_cell_definition"]
    if not 0 < active["min_raw_block_c_amplitude"] <= 1 or not 0 <= active["min_raw_block_c_shape_r2"] <= 1:
        raise ValueError("Invalid adaptive active-band definition")
    gain = criteria["gain_and_engineering_invariants"]
    if gain != DEFAULT_JUSTIFIED_CRITERIA["gain_and_engineering_invariants"]:
        raise ValueError("Adaptive gain criteria must match the enforced bounds [0.25, 1.75]")
    return copy.deepcopy(criteria)


def _require_workspace(required: int, maximum: int | None) -> None:
    if maximum is not None:
        if isinstance(maximum, bool) or not isinstance(maximum, (int, np.integer)) or maximum < 1:
            raise ValueError("max_working_bytes must be a positive integer or None")
        if required > maximum:
            raise base.Unsupported("fit_working_memory_limit")


def _lattice_cell_bound(shape_yx, basis) -> int:
    h, w = shape_yx
    phase = basis @ np.array([[0, 0, h, h], [0, w, 0, w]], dtype=np.float64)
    counts = np.ceil(phase.max(axis=1)) - np.floor(phase.min(axis=1)) + 2
    if not np.isfinite(counts).all() or np.any(counts <= 0):
        raise base.Unsupported("invalid_phase_basis")
    return int(counts[0]) * int(counts[1])


def adaptive_workspace_plan(raw8, raw32, box, basis=None) -> dict[str, Any]:
    """Conservative peak reservation for fitting-owned arrays, not process RSS.

    Counts cover retained per-cell coordinates/values/masks, one-cell numerical
    scratch, coarse cell discovery, profile stacks, envelopes and Python records.
    Input arrays, ND2 decoder, allocator pools and library/runtime overhead are
    caller owned and are deliberately outside this limit. The 32 MiB reserve and
    256 bytes/pixel numerical scratch allowance cover simultaneous NumPy/SciPy
    temporaries; no stage materializes a full high-resolution cell stack.
    """
    y0, y1, x0, x1 = box
    overview_pixels = int(raw32.shape[1]) * int(raw32.shape[2])
    train_pixels = max(0, y1 - y0) * max(0, x1 - x0)
    reserve = 32 * 1024**2
    geometry = reserve + 256 * train_pixels
    plan = {
        "scope": "additional_fitting_workspace_excluding_inputs_decoder_and_process_overhead",
        "geometry_bytes": geometry,
        "estimated_peak_bytes": geometry,
        "normalization_chunk_cells": 1,
    }
    if basis is not None:
        n = _lattice_cell_bound(raw32.shape[1:], basis)
        nfit = min(3, int(raw32.shape[0]))
        pixels = 192 * 256
        # All full-cell background-mask views retain their complete parent mask.
        retained = n * (pixels * (8 + 4 * nfit + 1 + nfit) + 32 * 1024)
        coarse = n * 48 * 64 * (3 * raw32.shape[0] * raw32.dtype.itemsize + 16)
        discovery = reserve + coarse + 64 * overview_pixels
        # One current sampled cell + normalization/evaluation temporaries,
        # reduced profiles, output templates and conservative node-grid reserve.
        working = reserve + retained + 256 * pixels + n * (nfit * 256 * 8 + 4096)
        plan.update(lattice_cell_upper_bound=n, retained_cells_bytes=retained,
                    discovery_bytes=discovery, cell_processing_bytes=working,
                    estimated_peak_bytes=max(geometry, discovery, working))
    return plan


def supported_x_geometry(raw32, box, mapping, reference, pitch, saturation, check=lambda: None):
    check()
    y0, y1, x0, x1 = box
    bfi = mapping["BF"]
    im = np.asarray(raw32[bfi, y0:y1, x0:x1], float)
    ci = [bfi] + [mapping[n] for n in ("FITC", "CY3") if n in mapping]
    vals = raw32[ci, y0:y1, x0:x1]
    valid = np.isfinite(vals).all(axis=0) & (im > 0.6 * reference) & (vals < saturation).all(axis=0)

    def smooth(sigma):
        den = ndi.gaussian_filter(valid.astype(float), sigma)
        return ndi.gaussian_filter(np.where(valid, im, 0), sigma) / np.maximum(den, 1e-12)

    bg = smooth(30)
    z = np.where(valid, (smooth(1) - bg) / bg, 0)
    hh, ww = z.shape
    yy, xx = np.indices(z.shape)
    target = z * np.hanning(hh)[:, None] * np.hanning(ww)[None, :]
    fy, fx = np.fft.fftfreq(hh), np.fft.fftfreq(ww)
    power = np.abs(np.fft.fft2(target)) ** 2
    low, high = np.asarray(pitch) / 32
    allowed = (fx[None, :] > 1 / high) & (fx[None, :] < 1 / low) & (np.abs(fy[:, None]) < 0.004)
    if not np.any(allowed):
        raise base.Unsupported("insufficient_frequency_resolution")
    ij = np.unravel_index(np.argmax(np.where(allowed, power, -1)), power.shape)
    initial = np.array([fy[ij[0]], fx[ij[1]]])

    def loss(q):
        check()
        if not 1 / high < q[1] < 1 / low or abs(q[0]) > 0.004:
            return 1e20
        return -abs(np.sum(target * np.exp(-2j * np.pi * (q[0] * yy + q[1] * xx)))) ** 2

    fitted = minimize(loss, initial, method="Nelder-Mead", options={"maxiter": 180, "xatol": 1e-10})
    if not fitted.success:
        raise base.Unsupported("geometry_optimizer_failure")
    native_pitch = 32 / fitted.x[1]
    fraction = (native_pitch - pitch[0]) / (pitch[1] - pitch[0])
    if min(fraction, 1 - fraction) < 0.01:
        raise base.Unsupported("geometry_at_search_boundary")
    return fitted.x, dict(
        initial=initial.tolist(),
        fitted=fitted.x.tolist(),
        native_pitch=float(native_pitch),
        training_support_fraction=float(valid.mean()),
        iterations=int(fitted.nit),
    )


def assign_group(lo, hi, box):
    y0, y1, x0, x1 = box
    yin = lo[0] >= y0 + 1 and hi[0] < y1 - 1
    xin = lo[1] >= x0 + 1 and hi[1] < x1 - 1
    yout = hi[0] < y0 - 1 or lo[0] >= y1 + 1
    xout = hi[1] < x0 - 1 or lo[1] >= x1 + 1
    return (
        "train"
        if yin and xin
        else "heldout_columns"
        if yin and xout
        else "heldout_rows"
        if yout and xin
        else "heldout_both"
        if yout and xout
        else None
    )


def cell_2d_background_mask(
    cell_yx: np.ndarray,
    valid_yx: np.ndarray,
    mad_mult: float = 2.5,
) -> np.ndarray:
    msk = np.asarray(valid_yx, dtype=bool) & np.isfinite(cell_yx)
    if float(np.mean(msk)) < 0.20:
        return msk
    vals = np.where(msk, cell_yx.astype(np.float64), np.nan)
    med = float(np.nanmedian(vals))
    if not np.isfinite(med) or med <= 0.0:
        return msk

    row_ok = np.sum(msk, axis=1) >= 24
    if int(np.count_nonzero(row_ok)) < 12:
        return msk
    rmed = np.full(cell_yx.shape[0], np.nan, dtype=np.float64)
    rmed[row_ok] = np.nanmedian(vals[row_ok], axis=1)
    rel = (vals - rmed[:, None]) / med

    col_ok = np.sum(msk & row_ok[:, None], axis=0) >= 8
    if int(np.count_nonzero(col_ok)) < 64:
        return msk
    cmed = np.full(cell_yx.shape[1], np.nan, dtype=np.float64)
    cmed[col_ok] = np.nanmedian(rel[:, col_ok], axis=0)
    dev_2d = rel - cmed[None, :]
    dev_med = float(np.nanmedian(dev_2d))
    dev_mad = max(float(np.nanmedian(np.abs(dev_2d - dev_med))) * 1.4826, 1e-4)
    return msk & row_ok[:, None] & np.isfinite(dev_2d) & (np.abs(dev_2d - dev_med) <= mad_mult * dev_mad)


def robust_strip_profile(
    sub_yx: np.ndarray,
    valid_yx: np.ndarray,
    sigma: float = 1.8,
    min_valid_fraction: float = 0.25,
    min_col_fraction: float = 0.65,
) -> tuple[np.ndarray | None, float]:
    H, W = sub_yx.shape
    msk = np.asarray(valid_yx, dtype=bool) & np.isfinite(sub_yx)
    cov = float(np.mean(msk))
    if cov < min_valid_fraction:
        return None, 0.0

    vals = np.where(msk, sub_yx.astype(np.float64), np.nan)
    med = float(np.nanmedian(vals))
    if not np.isfinite(med) or med <= 0.0:
        return None, 0.0

    row_valid = np.sum(msk, axis=1) >= max(16, W // 6)
    n_good_rows = int(np.count_nonzero(row_valid))
    if n_good_rows < max(4, H // 8):
        return None, 0.0

    row_med = np.full(H, np.nan, dtype=np.float64)
    row_med[row_valid] = np.nanmedian(vals[row_valid], axis=1)
    rel = (vals - row_med[:, None]) / med

    valid_rel = msk & row_valid[:, None] & np.isfinite(rel)
    col_counts = np.sum(valid_rel, axis=0)
    min_rows = max(3, int(0.12 * n_good_rows))
    good_cols = col_counts >= min_rows
    col_frac = float(np.mean(good_cols))
    if col_frac < min_col_fraction:
        return None, col_frac

    col_prof = np.full(W, np.nan, dtype=np.float64)
    rel_good = np.where(valid_rel[:, good_cols], rel[:, good_cols], np.nan)
    col_prof[good_cols] = np.nanmedian(rel_good, axis=0)

    ok = np.isfinite(col_prof)
    num = ndi.gaussian_filter1d(np.where(ok, col_prof, 0.0), sigma, mode="wrap")
    den = ndi.gaussian_filter1d(ok.astype(np.float64), sigma, mode="wrap")
    if float(np.min(den)) <= 1e-6:
        return None, col_frac
    prof = num / den
    prof = prof - float(np.mean(prof))
    return prof, col_frac


def project_amplitude(
    prof: np.ndarray | None,
    template: np.ndarray,
) -> dict[str, float | None]:
    if prof is None:
        return {
            "amplitude": None,
            "shape_r2": None,
            "pos_shape_r2": None,
            "correlation": None,
            "profile_energy": None,
        }
    denom = float(np.dot(template, template))
    alpha = float(np.dot(prof, template) / denom) if denom > 1e-15 else 0.0
    shape_r2 = base.r2(prof, alpha * template)
    pos_r2 = base.r2(prof, max(0.0, alpha) * template) if alpha > 0.0 else 0.0
    corr = float(np.corrcoef(prof, template)[0, 1]) if np.std(prof) > 1e-12 else 0.0
    energy = float(np.dot(prof, prof))
    return {
        "amplitude": alpha,
        "shape_r2": shape_r2,
        "pos_shape_r2": float(max(0.0, pos_r2)) if pos_r2 is not None else 0.0,
        "correlation": corr,
        "profile_energy": energy,
    }


def enumerate_all_lattice_cells(
    raw8: np.ndarray,
    raw32: np.ndarray,
    basis: np.ndarray,
    mapping: dict[str, int],
    bf_reference: float,
    saturation: float,
    train_box_ov32: list[int],
    check=lambda: None,
) -> tuple[list[dict[str, Any]], tuple[int, int], tuple[int, int]]:
    bfi = mapping["BF"]
    fit_indices = [bfi] + [mapping[n] for n in ("FITC", "CY3") if n in mapping]
    height, width = raw32.shape[1:]

    complete_samples, complete_records = base.phase_cells(
        raw32, basis, {"all": [0, height, 0, width]}, bfi, bf_reference, saturation, fit_indices, check
    )
    del complete_samples
    coarse_coords = base.coords_for(basis, complete_records, 48, 64, factor=32)
    complete_lookup: dict[tuple[int, int], str] = {}
    for rec, co in zip(complete_records, coarse_coords):
        check()
        grp = assign_group(co.min(axis=(1, 2)), co.max(axis=(1, 2)), train_box_ov32) or "deployment_only"
        complete_lookup[(int(rec["row"]), int(rec["column"]))] = grp
    del coarse_coords

    yy_ov, xx_ov = np.nonzero(raw32[bfi] > 0.6 * bf_reference)
    ph_y = basis[0, 0] * (yy_ov - 0.5) + basis[0, 1] * (xx_ov - 0.5)
    ph_x = basis[1, 0] * (yy_ov - 0.5) + basis[1, 1] * (xx_ov - 0.5)
    r_min = max(0, int(np.floor(ph_y.min())))
    r_max = int(np.floor(ph_y.max()))
    c_min = max(0, int(np.floor(ph_x.min())))
    c_max = int(np.floor(ph_x.max()))
    # Full-overview support coordinates belong only to discovery, not to the
    # retained-cell peak reserved by adaptive_workspace_plan.
    del yy_ov, xx_ov, ph_y, ph_x

    h8, w8 = raw8.shape[1], raw8.shape[2]
    out_cells = []

    for r in range(r_min, r_max + 1):
        check()
        for c in range(c_min, c_max + 1):
            check()
            grp = complete_lookup.get((r, c), "partial_or_border")
            is_comp = (r, c) in complete_lookup
            co = base.coords_for(basis, [{"row": r, "column": c}], 192, 256, factor=8)[0]
            in_bounds = (co[0] >= 0) & (co[0] < h8 - 1) & (co[1] >= 0) & (co[1] < w8 - 1)
            if float(np.mean(in_bounds)) < 0.15:
                continue
            values = np.stack([
                ndi.map_coordinates(raw8[ci], co, order=1, mode="constant", cval=np.nan)
                for ci in fit_indices
            ]).astype(np.float32)
            valid = (
                in_bounds
                & np.isfinite(values).all(axis=0)
                & (values[0] > 0.6 * bf_reference)
                & (values < saturation).all(axis=0)
            )
            whole_cov = float(np.mean(valid))
            if whole_cov < 0.15 and not is_comp:
                continue

            out_cells.append({
                "row": r,
                "column": c,
                "group": grp,
                "is_complete_97": is_comp,
                "whole_coverage": whole_cov,
                "est_coverage": whole_cov,
                "coords_raw8": co.astype(np.float32),
                "values_fit": values,
                "valid_mask": valid,
                "valid_est_mask": valid,
            })

    origin_rc = (r_min, c_min)
    extent_rc = (r_max - r_min + 1, c_max - c_min + 1)
    return out_cells, origin_rc, extent_rc


def compute_subcell_observations(
    cells: list[dict[str, Any]],
    names: list[str],
    mapping: dict[str, int],
    fit_indices: list[int],
    templates: dict[str, np.ndarray],
    check=lambda: None,
) -> None:
    strip_slices = {
        "A": slice(8, 68),
        "B": slice(68, 128),
        "AB": slice(8, 128),
        "C": slice(136, 176),
    }
    for cell in cells:
        check()
        vals = cell["values_fit"]
        v_est = cell["valid_est_mask"]
        v_eval = cell["valid_mask"]
        cell["obs"] = {}
        cell["bg_eval_mask"] = {}
        for name in names:
            check()
            src_i = fit_indices.index(mapping[name])
            tmpl = templates[name]

            bg_est_top = cell_2d_background_mask(vals[src_i, :132], v_est[:132], mad_mult=2.5)
            bg_eval_full = cell_2d_background_mask(vals[src_i], v_eval, mad_mult=2.5)
            cell["bg_eval_mask"][name] = bg_eval_full[136:176]

            ch_obs = {}
            for s_key, sl in strip_slices.items():
                if s_key == "C":
                    msk = bg_eval_full[sl]
                    raw_vf = float(np.mean(v_eval[sl]))
                    min_vf, min_cf = 0.40, 0.75
                else:
                    msk = bg_est_top[sl]
                    raw_vf = float(np.mean(v_est[sl]))
                    min_vf, min_cf = 0.20, 0.65
                prof, col_frac = robust_strip_profile(
                    vals[src_i, sl], msk, sigma=1.8, min_valid_fraction=min_vf, min_col_fraction=min_cf
                )
                stats = project_amplitude(prof, tmpl)
                stats["col_fraction"] = col_frac
                stats["valid_fraction"] = raw_vf
                stats["bg_fraction"] = float(np.mean(msk))
                ch_obs[s_key] = stats
            cell["obs"][name] = ch_obs


def _build_conservative_subcell_field(
    cells: list[dict[str, Any]],
    origin_rc: tuple[int, int],
    extent_rc: tuple[int, int],
    subcell_getter,
    sigma_nodes: float = 0.80,
    damping: float = 0.84,
    check=lambda: None,
) -> np.ndarray:
    r0, c0 = origin_rc
    R, K = extent_rc
    ny, nx = 2 * R + 1, 2 * K + 1

    cell_aA = np.full((R, K), np.nan, dtype=np.float64)
    cell_wA = np.zeros((R, K), dtype=np.float64)
    cell_aB = np.full((R, K), np.nan, dtype=np.float64)
    cell_wB = np.zeros((R, K), dtype=np.float64)

    for cell in cells:
        check()
        ri = cell["row"] - r0
        ci = cell["column"] - c0
        aA, wA, aB, wB = subcell_getter(cell)
        if aA is not None and wA > 0:
            cell_aA[ri, ci] = aA
            cell_wA[ri, ci] = wA
        if aB is not None and wB > 0:
            cell_aB[ri, ci] = aB
            cell_wB[ri, ci] = wB

    num = np.zeros((ny, nx), dtype=np.float64)
    den = np.zeros((ny, nx), dtype=np.float64)
    upper_bound = np.full((ny, nx), np.inf, dtype=np.float64)

    for ri in range(R):
        check()
        for ci in range(K):
            aA, wA = cell_aA[ri, ci], cell_wA[ri, ci]
            aB, wB = cell_aB[ri, ci], cell_wB[ri, ci]
            iy_top = 2 * ri
            iy_mid = 2 * ri + 1
            iy_bot = 2 * ri + 2
            ix_mid = 2 * ci + 1

            if np.isfinite(aA) and np.isfinite(aB):
                a_center = min(aB, 0.30 * aA + 0.70 * aB)
                w_center = 0.5 * (wA + wB)
                if aB < aA:
                    a_bot = max(0.0, aB + 0.45 * (aB - aA))
                else:
                    a_bot = 0.25 * aA + 0.75 * aB
                w_bot = wB
                a_top = aA
                w_top = wA
                node_deposits = [
                    (iy_top, a_top, 0.65 * w_top),
                    (iy_mid, a_center, 1.00 * w_center),
                    (iy_bot, a_bot, 0.75 * w_bot),
                ]
            elif np.isfinite(aB):
                node_deposits = [
                    (iy_mid, aB, 1.00 * wB),
                    (iy_bot, aB, 0.35 * wB),
                ]
            elif np.isfinite(aA):
                node_deposits = [
                    (iy_top, aA, 0.80 * wA),
                    (iy_mid, aA, 0.15 * wA),
                ]
            else:
                continue

            for ny_i, a_val, w_val in node_deposits:
                for nx_i, wx in [(ix_mid - 1, 0.45), (ix_mid, 1.0), (ix_mid + 1, 0.45)]:
                    w = w_val * wx
                    num[ny_i, nx_i] += w * a_val
                    den[ny_i, nx_i] += w
                    if w_val >= 0.35:
                        upper_bound[ny_i, nx_i] = min(upper_bound[ny_i, nx_i], a_val + 0.04)

    support = den > 1e-6
    if not np.any(support):
        return np.zeros((ny, nx), dtype=np.float64)

    raw_nodes = np.where(support, num / np.maximum(den, 1e-12), 0.0)
    bounded_raw = np.where(np.isfinite(upper_bound), np.minimum(raw_nodes, upper_bound), raw_nodes)

    s_num = ndi.gaussian_filter(bounded_raw * den, sigma=sigma_nodes, mode="nearest")
    s_den = ndi.gaussian_filter(den, sigma=sigma_nodes, mode="nearest")
    w_num = ndi.gaussian_filter(bounded_raw * den, sigma=max(1.8, 2.2 * sigma_nodes), mode="nearest")
    w_den = ndi.gaussian_filter(den, sigma=max(1.8, 2.2 * sigma_nodes), mode="nearest")
    nearest_idx = ndi.distance_transform_edt(~support, return_distances=False, return_indices=True)
    fallback = np.where(w_den > 1e-5, w_num / np.maximum(w_den, 1e-12), bounded_raw[tuple(nearest_idx)])
    alpha_local = np.clip(s_den / 0.25, 0.0, 1.0)
    local_est = np.where(s_den > 1e-5, s_num / np.maximum(s_den, 1e-12), fallback)
    smoothed = alpha_local * local_est + (1.0 - alpha_local) * fallback

    final_nodes = np.where(np.isfinite(upper_bound), np.minimum(smoothed, upper_bound), smoothed)
    return np.clip(final_nodes * damping, 0.0, 2.2)


def compute_train_channel_coupling(
    cells: list[dict[str, Any]],
    names: list[str],
    check=lambda: None,
) -> dict[str, float]:
    gammas = {"BF": 1.0}
    train_cells = [c for c in cells if c["group"] == "train"]
    for name in names:
        if name == "BF":
            continue
        ratios = []
        for c in train_cells:
            check()
            a_bf = c["obs"]["BF"]["AB"]["amplitude"]
            r2_bf = c["obs"]["BF"]["AB"]["pos_shape_r2"]
            a_ch = c["obs"][name]["AB"]["amplitude"]
            r2_ch = c["obs"][name]["AB"]["pos_shape_r2"]
            if (
                a_bf is not None and a_ch is not None
                and r2_bf is not None and r2_ch is not None
                and a_bf > 0.25 and r2_bf >= 0.80 and r2_ch >= 0.75
            ):
                ratios.append(float(a_ch / a_bf))
        gammas[name] = float(np.median(ratios)) if ratios else 1.0
    return gammas


def build_m5_adaptive_hybrid_nodes(
    cells: list[dict[str, Any]],
    names: list[str],
    origin_rc: tuple[int, int],
    extent_rc: tuple[int, int],
    gammas: dict[str, float],
    damping: float = 0.84,
    sigma_nodes: float = 0.75,
    check=lambda: None,
) -> tuple[np.ndarray, dict[str, Any]]:
    R, K = extent_rc
    nodes = np.zeros((len(names), 2 * R + 1, 2 * K + 1), dtype=np.float64)

    for ch_i, name in enumerate(names):
        check()
        gamma = float(gammas[name])

        def ch_subcell(cell: dict[str, Any]):
            out = []
            for s_key in ("A", "B"):
                st_own = cell["obs"][name][s_key]
                st_bf = cell["obs"]["BF"][s_key]
                vf = st_own["valid_fraction"]
                if vf < 0.25:
                    out.extend([None, 0.0])
                    continue
                a_own = st_own["amplitude"]
                r2_own = st_own["pos_shape_r2"]
                a_bf = st_bf["amplitude"]
                r2_bf = st_bf["pos_shape_r2"]

                a_own_pos = max(0.0, float(a_own)) if (a_own is not None and np.isfinite(a_own)) else None
                a_bf_pos = max(0.0, float(a_bf)) if (a_bf is not None and np.isfinite(a_bf)) else None

                if a_bf_pos is not None and (a_bf_pos < 0.15 or r2_bf < 0.35):
                    if vf >= 0.65:
                        bf_cap = gamma * a_bf_pos * (r2_bf ** 2)
                        a_est = bf_cap if a_own_pos is None else min(a_own_pos * (r2_own ** 2), bf_cap + 0.05)
                        out.extend([a_est, vf * 0.80])
                    else:
                        out.extend([None, 0.0])
                    continue

                prior_a = (gamma * a_bf_pos) if (a_bf_pos is not None and r2_bf >= 0.45) else None
                w_prior = vf * (r2_bf ** 2) if prior_a is not None else 0.0

                if a_own_pos is not None and r2_own >= 0.65:
                    w_own = vf * (r2_own ** 3)
                    if prior_a is not None:
                        a_bounded = float(np.clip(a_own_pos, 0.70 * prior_a, 1.20 * prior_a))
                        a_fused = (w_own * a_bounded + 0.50 * w_prior * prior_a) / (w_own + 0.50 * w_prior)
                        out.extend([a_fused, max(w_own, w_prior)])
                    else:
                        out.extend([a_own_pos, w_own])
                elif prior_a is not None:
                    out.extend([prior_a, 0.85 * w_prior])
                else:
                    out.extend([None, 0.0])
            return out[0], out[1], out[2], out[3]

        nodes[ch_i] = _build_conservative_subcell_field(
            cells, origin_rc, extent_rc, ch_subcell, sigma_nodes=sigma_nodes, damping=damping, check=check
        )

    meta = {"gammas": gammas, "damping": damping, "sigma_nodes": sigma_nodes}
    return nodes, meta


def calibrate_channel_prediction_bound(
    cells, origin_rc, extent_rc, template, criteria, *, damping=0.84,
    sigma_nodes=0.75, channel="BF", gamma=1.0, check=lambda: None,
):
    """Bound extrapolation with each cell and its immediate neighbours withheld.

    A/B observations fit the field; C remains exclusively admission evidence.
    With a 3-by-3 neighbourhood excluded, compare the centre cell's A/B amplitude with
    the surrounding field's template-energy-weighted prediction. The smallest
    observed/predicted ratio can only attenuate this channel's final envelope.
    Each channel uses its own observations and the same coupling as its field,
    without selecting a constant from a particular native validation ROI.

    This is an empirical calibration bound, not a guarantee at unseen pixels.
    Independent C admission and native validation remain necessary.
    """
    gates = criteria["reduced_resolution_gates"]
    active = gates["active_band_cell_definition"]
    min_amplitude = active["min_raw_block_c_amplitude"]
    min_shape_r2 = active["min_raw_block_c_shape_r2"]
    weight = np.asarray(template, dtype=np.float64) ** 2
    energy = float(np.sum(weight))
    if not np.isfinite(energy) or energy <= 1e-15:
        raise base.Unsupported(f"zero_{channel}_template_energy")
    observations = []
    assessed_cells = 0
    for cell in cells:
        check()
        if not cell["is_complete_97"]:
            continue
        obs = cell["obs"][channel]
        eligible = [key for key in ("A", "B") if (
            obs[key]["amplitude"] is not None
            and np.isfinite(obs[key]["amplitude"])
            and obs[key]["amplitude"] >= min_amplitude
            and obs[key]["pos_shape_r2"] is not None
            and obs[key]["pos_shape_r2"] >= min_shape_r2
            and obs[key]["valid_fraction"] >= 0.65
        )]
        if not eligible:
            continue
        # A missing native region can remove multiple adjacent repeat cells.
        # Remove immediate neighbours as well, so the bound assesses spatial
        # extrapolation rather than prediction with all nearby support intact.
        remaining = [other for other in cells if (
            abs(other["row"] - cell["row"]) > 1
            or abs(other["column"] - cell["column"]) > 1
        )]
        predicted_nodes, _ = build_m5_adaptive_hybrid_nodes(
            remaining, [channel], origin_rc, extent_rc, {channel: gamma},
            damping=damping, sigma_nodes=sigma_nodes, check=check,
        )
        assessed = False
        for key in eligible:
            check()
            py = cell["row"] + {"A": 38, "B": 98}[key] / 192
            px = cell["column"] + (np.arange(256) + 0.5) / 256
            phase = np.array([np.full(256, py), px])
            envelope = base.smooth_node_envelope_at(predicted_nodes[0], origin_rc, phase)
            predicted = float(np.sum(weight * envelope) / energy)
            if predicted <= min_amplitude / 4:
                continue
            actual = float(obs[key]["amplitude"])
            observations.append({
                "row": int(cell["row"]), "column": int(cell["column"]), "strip": key,
                "observed_amplitude": actual, "predicted_amplitude": predicted,
                "ratio": actual / predicted,
            })
            assessed = True
        assessed_cells += assessed
    if assessed_cells < gates["min_supported_cells_per_split_group"]:
        raise base.Unsupported(f"insufficient_{channel}_predictive_calibration")
    limiting = min(observations, key=lambda observation: observation["ratio"])
    return {
        "schema": "channel_leave_neighborhood_out_prediction_bound/1",
        "channel": channel, "coupling_gamma": gamma,
        "excluded_neighborhood_radius_cells": 1,
        "fit_strips": ["A", "B"], "admission_strip_C_used": False,
        "minimum_valid_strip_fraction": 0.65,
        "active_observation_definition": dict(active),
        "assessed_cells": assessed_cells, "assessed_observations": len(observations),
        "scale": min(1.0, limiting["ratio"]), "limiting_observation": limiting,
    }


def finite_node_support(cells, origin_rc, extent_rc, check=lambda: None):
    """Require finite input across each half-cell node's interpolation footprint.

    Missing samples invalidate every node that could influence them. Only input
    availability is inspected outside A/B, never C intensity or C observations.
    No smoothing fills this support mask.
    """
    check()
    rows, columns = extent_rc
    support = np.ones((2 * rows + 1, 2 * columns + 1), dtype=np.float64)
    covered = np.zeros_like(support, dtype=bool)
    for cell in cells:
        check()
        r, c = cell["row"] - origin_rc[0], cell["column"] - origin_rc[1]
        finite = np.isfinite(cell["values_fit"]).all(axis=0)
        height, width = finite.shape
        for j, (y0, y1) in enumerate(((0, height // 2), (0, height), (height // 2, height))):
            check()
            for k, (x0, x1) in enumerate(((0, width // 2), (0, width), (width // 2, width))):
                check()
                iy, ix = 2 * r + j, 2 * c + k
                support[iy, ix] = min(support[iy, ix], float(finite[y0:y1, x0:x1].all()))
                covered[iy, ix] = True
    support[~covered] = 0.0
    return support


def local_trough_amplitude_cap(profile, template, check=lambda: None):
    """Leave an empirical template-mismatch allowance in the measured trough.

    The trough geometry comes only from the training template. The residual MAD
    is a local mismatch allowance, not a statistical confidence interval or a
    guarantee at unseen pixels. Search gains stay in the enforced [0.25, 1.75]
    domain, including for strong templates.
    """
    check()
    phase = (np.arange(len(template)) + 0.5) / len(template)
    centre = phase[np.argmin(template)]
    distance = (phase - centre + 0.5) % 1 - 0.5
    interior = np.abs(distance) <= 0.08
    flank = np.abs(distance) >= 0.30
    alpha = project_amplitude(profile, template)["amplitude"]
    residual = profile - max(0.0, alpha) * template
    reserve = 1.4826 * float(np.median(np.abs(residual - np.median(residual))))

    def signed_depth(amplitude):
        corrected = (1 + profile) / (1 + amplitude * template)
        return float(np.mean(corrected[flank]) - np.mean(corrected[interior]))

    if signed_depth(0) <= reserve:
        return 0.0
    limit = 2.2
    tmin, tmax = float(np.min(template)), float(np.max(template))
    if tmin < 0:
        limit = min(limit, 0.75 / -tmin)
    if tmax > 0:
        limit = min(limit, 0.75 / tmax)
    lo, hi = 0.0, max(0.0, min(limit, alpha))
    if signed_depth(hi) >= reserve:
        return hi
    for _ in range(36):
        check()
        mid = 0.5 * (lo + hi)
        if signed_depth(mid) >= reserve:
            lo = mid
        else:
            hi = mid
    return lo


def build_local_trough_bound_nodes(
    cells, names, origin_rc, extent_rc, gammas, calibrations, *,
    templates, mapping, fit_indices, m5_nodes=None, damping=0.84,
    sigma_nodes=0.75, check=lambda: None,
):
    """Restore supported local amplitudes, with local trough and missing-data bounds.

    The global leave-neighborhood bound remains the fallback for extrapolation.
    A/B half-strips constrain every node influencing their observations; C is
    still used only by the unchanged admission evaluator.
    """
    check()
    local_damping = 0.95
    if m5_nodes is None:
        m5_nodes, _ = build_m5_adaptive_hybrid_nodes(
            cells, names, origin_rc, extent_rc, gammas,
            damping=damping, sigma_nodes=sigma_nodes, check=check,
        )
    result = np.zeros_like(m5_nodes)
    finite_support = finite_node_support(cells, origin_rc, extent_rc, check=check)
    metadata = {
        "schema": "local_trough_prediction_bound/1",
        "fit_strips": ["A", "B"],
        "admission_strip_C_used": False,
        "outside_fit_strips_input_finiteness_only": True,
        "finite_support_nodes": int(np.sum(finite_support)),
        "total_nodes_per_channel": int(finite_support.size),
        "method": {
            "local_damping": local_damping,
            "confidence_r2_interval": [0.35, 0.80],
            "confidence_valid_fraction_interval": [0.65, 0.90],
            "confidence_sigma_nodes": 0.65,
            "cap_row_slices": [[8, 38], [38, 68], [68, 98], [98, 128]],
            "cap_min_valid_fraction": 0.65,
            "cap_min_shape_r2": 0.65,
            "phase_inner_halfwidth": 0.08,
            "phase_flank_min_abs_distance": 0.30,
            "mismatch_allowance": "1.4826 * MAD(profile - max(0, LS_amplitude) * template)",
            "mismatch_allowance_interpretation": "empirical; not a statistical confidence interval",
            "bisection_iterations": 36,
            "search_gain_bounds": [0.25, 1.75],
        },
        "channels": {},
    }

    def quality(stats):
        return (float(np.clip((stats["pos_shape_r2"] - 0.35) / 0.45, 0, 1))
                * float(np.clip((stats["valid_fraction"] - 0.65) / 0.25, 0, 1)))

    for channel_i, name in enumerate(names):
        check()
        calibration = calibrations.get(name, {})
        scale = calibration.get("scale")
        if scale is None:
            metadata["channels"][name] = {"status": "unchanged_no_admitted_prediction_calibration"}
            continue
        original = m5_nodes[channel_i]
        fallback = original * scale
        support = np.zeros_like(original)

        def own_subcell(cell):
            out = []
            for key in ("A", "B"):
                check()
                stats = cell["obs"][name][key]
                amplitude, r2, valid = stats["amplitude"], stats["pos_shape_r2"], stats["valid_fraction"]
                if amplitude is None or r2 is None or valid < 0.65:
                    out.extend([None, 0.0])
                    continue
                weight = quality(stats)
                if amplitude < 0.2:
                    weight = max(weight, 0.8)
                out.extend([max(0.0, float(amplitude)), max(0.01, weight) * valid])
            return out[0], out[1], out[2], out[3]

        own = _build_conservative_subcell_field(
            cells, origin_rc, extent_rc, own_subcell,
            sigma_nodes=sigma_nodes, damping=local_damping, check=check,
        )
        for cell in cells:
            check()
            r, c = cell["row"] - origin_rc[0], cell["column"] - origin_rc[1]
            for key, rows in (("A", (2 * r, 2 * r + 1)), ("B", (2 * r + 1, 2 * r + 2))):
                check()
                stats = cell["obs"][name][key]
                if stats["amplitude"] is None or stats["pos_shape_r2"] is None or stats["valid_fraction"] < 0.65:
                    continue
                weight = quality(stats)
                for y in rows:
                    check()
                    support[y, 2 * c:2 * c + 3] = np.maximum(support[y, 2 * c:2 * c + 3], weight)
        confidence = ndi.gaussian_filter(support, 0.65, mode="constant")
        direct = np.minimum(original / damping * local_damping, own)
        local = fallback * (1 - confidence) + direct * confidence

        caps = np.full_like(original, np.inf)
        cap_observations = 0
        for cell in cells:
            check()
            r, c = cell["row"] - origin_rc[0], cell["column"] - origin_rc[1]
            values = cell["values_fit"][fit_indices.index(mapping[name])]
            mask = cell_2d_background_mask(values[:132], cell["valid_est_mask"][:132], mad_mult=2.5)
            # A median over a whole strip can hide coherent within-strip changes.
            for start, stop in ((8, 38), (38, 68), (68, 98), (98, 128)):
                check()
                if np.mean(cell["valid_est_mask"][start:stop]) < 0.65:
                    continue
                profile, _ = robust_strip_profile(
                    values[start:stop], mask[start:stop],
                    min_valid_fraction=0.20, min_col_fraction=0.65,
                )
                stats = project_amplitude(profile, templates[name])
                if profile is None or stats["pos_shape_r2"] < 0.65:
                    continue
                cap = local_trough_amplitude_cap(profile, templates[name], check=check)
                cap = min(cap, max(0.0, stats["amplitude"])) * local_damping
                lo, hi = int(np.floor(2 * start / 192)), int(np.ceil(2 * stop / 192))
                region = np.s_[2 * r + lo:2 * r + hi + 1, 2 * c:2 * c + 3]
                caps[region] = np.minimum(caps[region], cap)
                cap_observations += 1
        # No smoothing follows caps or support gating: neighbors cannot restore
        # trust in a missing region or lift a reliable weak local bound.
        capped = np.minimum(local, caps)
        result[channel_i] = fallback + (capped - fallback) * finite_support
        effective_confidence = confidence * finite_support
        assessed = original > 0.05
        ratios = result[channel_i][assessed] / original[assessed]
        metadata["channels"][name] = {
            "status": "fitted",
            "fallback_scale": float(scale),
            "cap_observations": cap_observations,
            "bounded_nodes": int(np.count_nonzero(np.isfinite(caps))),
            "cap_reduced_nodes": int(np.count_nonzero(local > caps)),
            "restored_above_fallback_nodes": int(np.count_nonzero(result[channel_i] > fallback)),
            "confidence_median": float(np.median(effective_confidence)),
            "strength_relative_m5_p10_median_p90": np.quantile(ratios, [0.1, 0.5, 0.9]).tolist() if ratios.size else None,
        }
    return result, metadata


def _summary_stats(vals: list[float]) -> dict[str, Any] | None:
    arr = np.asarray([v for v in vals if v is not None and np.isfinite(v)], dtype=np.float64)
    if len(arr) == 0:
        return None
    return {
        "n": int(len(arr)),
        "min": float(np.min(arr)),
        "p10": float(np.quantile(arr, 0.10)),
        "median": float(np.median(arr)),
        "mean": float(np.mean(arr)),
        "p90": float(np.quantile(arr, 0.90)),
        "max": float(np.max(arr)),
    }


def _gain_at_coords(model: base.Model, c: int, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    yy, xx = np.broadcast_arrays(
        (np.asarray(ys, dtype=np.float64) - 15.5) / 32.0,
        (np.asarray(xs, dtype=np.float64) - 15.5) / 32.0,
    )
    if int(c) not in model.indices:
        return np.ones(yy.shape, dtype=np.float64)
    ci = model.indices.index(int(c))
    if not model.enabled[ci]:
        return np.ones(yy.shape, dtype=np.float64)
    if not model.has_envelope:
        isold = bool(model.old[ci])
        t = model.oldtemplates[ci] if isold else model.templates[ci]
        b = model.oldbasis if isold else model.basis
        return 1.0 + base.field_at(t, b, model.delta, ys, xs, local=bool(model.local[ci]))
    phase = np.array([
        model.basis[0, 0] * yy + model.basis[0, 1] * xx,
        model.basis[1, 0] * yy + model.basis[1, 1] * xx,
    ])
    if model.local[ci]:
        phase[1] = phase[1] + base.phase_delta(phase, model.delta)
    env = base.smooth_node_envelope_at(model.amplitude_nodes[ci], model.envelope_cell_origin, phase)
    tmpl = base.sample_template(model.templates[ci], phase)
    return 1.0 + env * tmpl


def evaluate_candidate_on_block_c(
    model: base.Model,
    cells: list[dict[str, Any]],
    names: list[str],
    mapping: dict[str, int],
    fit_indices: list[int],
    templates: dict[str, np.ndarray],
    criteria: dict[str, Any],
    check=lambda: None,
) -> dict[str, Any]:
    criteria = validated_acceptance_criteria({"acceptance_criteria": criteria})
    gates = criteria["reduced_resolution_gates"]
    act_def = gates["active_band_cell_definition"]
    min_raw_a = float(act_def["min_raw_block_c_amplitude"])
    min_raw_r2 = float(act_def["min_raw_block_c_shape_r2"])

    complete_cells = [c for c in cells if c["is_complete_97"] and c["whole_coverage"] >= 0.97]
    heldout_groups = ["heldout_columns", "heldout_rows", "heldout_both"]
    all_groups = ["train", *heldout_groups, "deployment_only"]

    channel_reports: dict[str, Any] = {}
    for name in names:
        check()
        src_i = fit_indices.index(mapping[name])
        tmpl = templates[name]
        per_cell_records = []

        for cell in complete_cells:
            check()
            raw_c = cell["obs"][name]["C"]
            co_c = cell["coords_raw8"][:, 136:176] * 8.0 + 3.5
            gain_c = _gain_at_coords(model, mapping[name], co_c[0], co_c[1])
            vals_c = cell["values_fit"][src_i, 136:176]
            msk_c = cell["bg_eval_mask"][name]

            prof_corr, _ = robust_strip_profile(
                vals_c / gain_c, msk_c, sigma=1.8, min_valid_fraction=0.40, min_col_fraction=0.75
            )
            corr_stats = project_amplitude(prof_corr, tmpl)

            a_raw = raw_c["amplitude"]
            r2_raw = raw_c["pos_shape_r2"]
            e_raw = raw_c["profile_energy"]
            a_res = corr_stats["amplitude"]
            e_res = corr_stats["profile_energy"]

            is_active = bool(
                a_raw is not None and r2_raw is not None
                and a_raw >= min_raw_a and r2_raw >= min_raw_r2
            )
            if is_active and a_res is not None:
                signed_red = float(1.0 - a_res / a_raw)
                abs_red = float(1.0 - abs(a_res) / a_raw)
                energy_ratio = float(e_res / e_raw) if (e_raw is not None and e_raw > 1e-12 and e_res is not None) else None
            else:
                signed_red = None
                abs_red = None
                energy_ratio = float(e_res / e_raw) if (e_raw is not None and e_raw > 1e-12 and e_res is not None) else None

            per_cell_records.append({
                "row": int(cell["row"]),
                "column": int(cell["column"]),
                "group": cell["group"],
                "is_active_band_cell": is_active,
                "raw_C": raw_c,
                "residual_C": {
                    **corr_stats,
                    "signed_amplitude_reduction": signed_red,
                    "abs_amplitude_reduction": abs_red,
                    "profile_energy_ratio": energy_ratio,
                },
            })

        group_summaries = {}
        for grp in all_groups:
            g_rows = [r for r in per_cell_records if r["group"] == grp]
            g_active = [r for r in g_rows if r["is_active_band_cell"] and r["residual_C"]["amplitude"] is not None]
            g_weak = [r for r in g_rows if (not r["is_active_band_cell"]) and r["residual_C"]["amplitude"] is not None]
            g_all_assessed = [r for r in g_rows if r["residual_C"]["amplitude"] is not None]

            group_summaries[grp] = {
                "n_complete_cells": len(g_rows),
                "n_assessed": len(g_all_assessed),
                "n_active_band_cells": len(g_active),
                "n_weak_band_cells": len(g_weak),
                "raw_amplitude_active": _summary_stats([r["raw_C"]["amplitude"] for r in g_active]),
                "residual_amplitude_all": _summary_stats([r["residual_C"]["amplitude"] for r in g_all_assessed]),
                "residual_amplitude_active": _summary_stats([r["residual_C"]["amplitude"] for r in g_active]),
                "residual_amplitude_weak": _summary_stats([r["residual_C"]["amplitude"] for r in g_weak]),
                "abs_amplitude_reduction_active": _summary_stats([r["residual_C"]["abs_amplitude_reduction"] for r in g_active]),
                "signed_amplitude_reduction_active": _summary_stats([r["residual_C"]["signed_amplitude_reduction"] for r in g_active]),
                "profile_energy_ratio_active": _summary_stats([r["residual_C"]["profile_energy_ratio"] for r in g_active]),
            }

        failures = []
        all_heldout_res = [
            r["residual_C"]["amplitude"]
            for r in per_cell_records
            if r["group"] in heldout_groups and r["residual_C"]["amplitude"] is not None
        ]
        all_heldout_weak_res = [
            abs(r["residual_C"]["amplitude"]) - abs(r["raw_C"]["amplitude"])
            for r in per_cell_records
            if (
                r["group"] in heldout_groups
                and (not r["is_active_band_cell"])
                and r["residual_C"]["amplitude"] is not None
                and r["raw_C"]["amplitude"] is not None
            )
        ]

        for grp in heldout_groups:
            gs = group_summaries[grp]
            if gs["n_active_band_cells"] < int(gates["min_active_cells_per_heldout_group"]):
                failures.append(f"{grp}:insufficient_active_cells({gs['n_active_band_cells']})")
            red_stat = gs["abs_amplitude_reduction_active"]
            if red_stat is None or red_stat["median"] < float(gates["min_median_block_c_amplitude_reduction_per_heldout_group"]):
                val_s = "None" if red_stat is None else f"{red_stat['median']:.3f}"
                failures.append(f"{grp}:insufficient_median_C_reduction({val_s})")
            er_stat = gs["profile_energy_ratio_active"]
            if er_stat is None or er_stat["median"] > float(gates["max_median_block_c_energy_ratio_per_heldout_group"]):
                val_s = "None" if er_stat is None else f"{er_stat['median']:.3f}"
                failures.append(f"{grp}:high_median_C_energy_ratio({val_s})")
            res_stat = gs["residual_amplitude_all"]
            if res_stat is None or res_stat["median"] < float(gates["min_median_block_c_residual_amplitude_per_heldout_group"]):
                val_s = "None" if res_stat is None else f"{res_stat['median']:.3f}"
                failures.append(f"{grp}:negative_median_residual({val_s})")

        if all_heldout_res:
            p10_res = float(np.quantile(all_heldout_res, 0.10))
            min_res = float(np.min(all_heldout_res))
            if p10_res < float(gates["min_p10_block_c_residual_amplitude_all_heldout"]):
                failures.append(f"heldout_all:p10_reversal({p10_res:.3f})")
            if min_res < float(gates["min_worst_cell_block_c_residual_amplitude_all_heldout"]):
                failures.append(f"heldout_all:worst_cell_reversal({min_res:.3f})")

        if all_heldout_weak_res and max(all_heldout_weak_res) > float(gates["max_abs_residual_amplitude_on_weak_cells"]):
            failures.append(f"heldout_weak:false_band_injection({max(all_heldout_weak_res):.3f})")

        dev_reds = []
        dev_reversals = []
        for grp in ("heldout_columns", "heldout_rows"):
            gs = group_summaries[grp]
            if gs["abs_amplitude_reduction_active"] is not None:
                dev_reds.append(gs["abs_amplitude_reduction_active"]["median"])
            if gs["residual_amplitude_all"] is not None:
                dev_reversals.append(min(0.0, gs["residual_amplitude_all"]["min"]))
        dev_score = (float(np.mean(dev_reds)) + 1.5 * float(np.min(dev_reversals))) if dev_reds else -999.0

        channel_reports[name] = {
            "channel_admitted": len(failures) == 0,
            "admission_failures": failures,
            "development_selection_score": dev_score,
            "groups": group_summaries,
        }

    all_admitted = all(channel_reports[n]["channel_admitted"] for n in names)
    mean_dev_score = float(np.mean([channel_reports[n]["development_selection_score"] for n in names]))
    return {
        "all_channels_admitted": all_admitted,
        "mean_development_selection_score": mean_dev_score,
        "channels": channel_reports,
    }


def _normalized_cell_profiles(cells, check=lambda: None):
    if not cells:
        raise base.Unsupported("no_complete_supported_repeat_cells")
    shape = cells[0]["values_fit"].shape
    result = np.empty((len(cells), shape[0], shape[-1]), dtype=cells[0]["values_fit"].dtype)
    for index, cell in enumerate(cells):
        check()
        observation = np.where(cell["valid_mask"][None, :, :], cell["values_fit"], np.nan)
        normalized = base.norm(observation, strict=False)
        result[index] = np.nanmedian(normalized, axis=-2)
    return result


def fit_adaptive_from_config(
    raw8: np.ndarray,
    raw32: np.ndarray,
    meta: dict[str, Any],
    config: dict[str, Any],
    output: str | Path,
    *,
    fallback_reason: str | None = None,
    check=lambda: None,
    max_working_bytes=None,
    progress=lambda _fraction: None,
) -> tuple[base.Model, dict[str, Any]]:
    """Fit `H_adaptive_hybrid_subcell/1` when fixed-box `H_auto_refit/1` abstains due
    to `insufficient_repeat_cells_*`. Persists target channels (`FITC`, `CY3`, `BF`)
    so the model NPZ is 100% compatible with `nd2wsi.h_persistence`.
    """
    check()
    output = Path(output)
    mapping = meta["channel_mapping"]
    bfi = mapping.get("BF")
    if bfi is None:
        raise base.Unsupported("BF_absent_no_frozen_fallback")

    hc = config["h_auto"]
    ac = config.get("h_adaptive", {})
    criteria = validated_acceptance_criteria(ac)
    gates = criteria["reduced_resolution_gates"]

    height, width = raw32.shape[1:]
    boxes = {
        g: [int(f[0] * height), min(height, int(f[1] * height)), int(f[2] * width), min(width, int(f[3] * width))]
        for g, f in hc["split_fractions_y0y1x0x1"].items()
    }
    box = boxes["train"]
    memory_plan = adaptive_workspace_plan(raw8, raw32, box)
    _require_workspace(memory_plan["estimated_peak_bytes"], max_working_bytes)
    y0, y1, x0, x1 = box
    trainbf = raw32[bfi, y0:y1, x0:x1]
    pos = trainbf[trainbf > 0]
    if len(pos) < 100:
        raise base.Unsupported("insufficient_BF_support")
    reference = float(np.median(pos))
    del pos

    sensor = meta["channel_acquisition_settings"][bfi]["sensor_pixels_xy"]
    prior = [
        [sensor[k] * hc["sensor_pitch_fraction"][0], sensor[k] * hc["sensor_pitch_fraction"][1]]
        for k in ["cy", "cx"]
    ]
    saturation = (
        float(np.iinfo(np.dtype(meta["dtype"])).max)
        if np.dtype(meta["dtype"]).kind in "ui"
        else np.inf
    )

    oldbasis, orig_geom = base.geometry(raw32[bfi], box, prior, check)
    xbasis, xgeom = supported_x_geometry(raw32, box, mapping, reference, prior[1], saturation, check)
    basis = oldbasis.copy()
    basis[1] = xbasis
    if np.linalg.det(basis) <= 0:
        raise base.Unsupported("invalid_phase_basis")
    memory_plan = adaptive_workspace_plan(raw8, raw32, box, basis)
    _require_workspace(memory_plan["estimated_peak_bytes"], max_working_bytes)
    memory_plan["limit_bytes"] = None if max_working_bytes is None else int(max_working_bytes)
    check()
    progress(0.30)

    all_names = [n for n in ("FITC", "CY3", "BF") if n in mapping]
    fit_indices = [bfi] + [mapping[n] for n in ("FITC", "CY3") if n in mapping]

    cells, origin_rc, extent_rc = enumerate_all_lattice_cells(
        raw8=raw8,
        raw32=raw32,
        basis=basis,
        mapping=mapping,
        bf_reference=reference,
        saturation=saturation,
        train_box_ov32=box,
        check=check,
    )
    complete_97 = [c for c in cells if c["is_complete_97"] and c["whole_coverage"] >= 0.97]
    group_counts = Counter(c["group"] for c in complete_97)
    for grp in ("train", "heldout_columns", "heldout_rows", "heldout_both"):
        if group_counts[grp] < gates["min_supported_cells_per_split_group"]:
            raise base.Unsupported(f"insufficient_repeat_cells_{grp}")

    # Normalize each cell with the original arithmetic, retaining only its
    # per-column profile. This removes the complete-cells x channels x 192 x
    # 256 observation/normalization stacks without changing their reductions.
    profiles_complete = _normalized_cell_profiles(complete_97, check=check)
    groups_arr = np.array([c["group"] for c in complete_97])

    templates_dict: dict[str, np.ndarray] = {}
    template_reproducibility: dict[str, Any] = {}
    for name in all_names:
        check()
        src_i = fit_indices.index(mapping[name])
        prof_by_grp = {}
        for grp in ("train", "heldout_columns", "heldout_rows", "heldout_both"):
            check()
            reduced = profiles_complete[groups_arr == grp, src_i]
            prof_by_grp[grp] = base.template(reduced, 1.8)
        tr_prof = prof_by_grp["train"]
        scores = {
            grp: base.r2(prof_by_grp[grp], tr_prof)
            for grp in ("heldout_columns", "heldout_rows", "heldout_both")
        }
        reproducible = bool(all(v is not None and np.isfinite(v) and v > gates["min_template_reproducibility_r2"] for v in scores.values()))
        if name == "BF" and not reproducible:
            raise base.Unsupported("no_reproducible_BF_periodicity:" + json.dumps(scores))
        templates_dict[name] = tr_prof
        template_reproducibility[name] = {
            "scores": scores,
            "reproducible": reproducible,
        }

    progress(0.60)
    compute_subcell_observations(cells, all_names, mapping, fit_indices, templates_dict, check=check)
    gammas = compute_train_channel_coupling(cells, all_names, check=check)
    damping = float(ac.get("damping", 0.84))
    sigma_nodes = float(ac.get("sigma_nodes", 0.75))
    m5_nodes_all, _ = build_m5_adaptive_hybrid_nodes(
        cells, all_names, origin_rc, extent_rc, gammas, damping=damping, sigma_nodes=sigma_nodes, check=check
    )

    requested_targets = ac.get("target_channels", ["FITC", "CY3", "BF"])
    if (not isinstance(requested_targets, list) or not requested_targets
            or any(n not in ("FITC", "CY3", "BF") for n in requested_targets)
            or len(set(requested_targets)) != len(requested_targets)):
        raise ValueError("Adaptive target channels must be distinct FITC, CY3 or BF labels")
    target_names = [n for n in requested_targets if n in mapping]
    if not target_names:
        raise base.Unsupported("no_requested_adaptive_channels")
    candidate_failures = {n: [] for n in target_names}
    for name in target_names:
        if not template_reproducibility[name]["reproducible"]:
            candidate_failures[name].append("template_not_reproducible")
    calibrations = {}
    for name in target_names:
        if candidate_failures[name]:
            continue
        try:
            calibration = calibrate_channel_prediction_bound(
                cells, origin_rc, extent_rc, templates_dict[name], criteria,
                damping=damping, sigma_nodes=sigma_nodes, channel=name,
                gamma=gammas[name], check=check,
            )
        except base.Unsupported as error:
            if str(error) != f"insufficient_{name}_predictive_calibration":
                raise
            candidate_failures[name].append(str(error))
            calibration = {"status": "rejected", "reason": str(error)}
        calibrations[name] = calibration
    bf_calibration = calibrations.get("BF")
    candidate_id = "M9_local_trough_bound_hybrid_subcell_x"
    target_indices = [mapping[n] for n in target_names]
    target_templates = np.asarray(
        [np.broadcast_to(templates_dict[n][None, :], (192, 256)).copy() for n in target_names],
        dtype=np.float64,
    )
    target_nodes, local_calibration = build_local_trough_bound_nodes(
        cells, target_names, origin_rc, extent_rc, gammas, calibrations,
        templates=templates_dict, mapping=mapping, fit_indices=fit_indices,
        m5_nodes=np.asarray([m5_nodes_all[all_names.index(n)] for n in target_names]),
        damping=damping, sigma_nodes=sigma_nodes, check=check,
    )

    check()
    output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        output,
        model_schema=np.asarray(ADAPTIVE_SCHEMA),
        basis=np.asarray(basis, dtype=np.float64),
        oldbasis=np.asarray(oldbasis, dtype=np.float64),
        templates=target_templates,
        oldtemplates=np.zeros((len(target_indices), 48, 64), dtype=np.float64),
        delta=np.zeros((1, 1), dtype=np.float64),
        local=np.zeros(len(target_indices), dtype=bool),
        old=np.zeros(len(target_indices), dtype=bool),
        channel_indices=np.asarray(target_indices, dtype=np.int64),
        enabled=np.zeros(len(target_indices), dtype=bool),
        amplitude_nodes=target_nodes,
        envelope_cell_origin=np.asarray(origin_rc, dtype=np.int64),
    )
    prov_model = base.Model(output)
    try:
        # Validate each candidate independently before its Strip C evaluation.
        # The archive loader remains strict for every enabled channel.
        for i, name in enumerate(target_names):
            check()
            if candidate_failures[name]:
                continue
            prov_model.enabled[i] = True
            try:
                prov_model.validate()
            except ValueError as error:
                if str(error) != "Invalid correction gain bounds over stored model domain":
                    raise
                prov_model.enabled[i] = False
                candidate_failures[name].append("invalid_candidate_gain_bounds")
        evaluated_names = [n for n in target_names if not candidate_failures[n]]
        eval_res = evaluate_candidate_on_block_c(
            prov_model, cells, evaluated_names, mapping, fit_indices, templates_dict, criteria, check=check
        ) if evaluated_names else {
            "all_channels_admitted": False, "mean_development_selection_score": None, "channels": {},
        }
        for name in target_names:
            if candidate_failures[name]:
                eval_res["channels"][name] = {
                    "channel_admitted": False,
                    "admission_failures": candidate_failures[name],
                    "development_selection_score": None,
                    "groups": {},
                }
        eval_res["all_channels_admitted"] = all(
            eval_res["channels"][n]["channel_admitted"] for n in target_names
        )
    finally:
        prov_model.data.close()

    final_enabled = [
        bool(template_reproducibility[n]["reproducible"] and eval_res["channels"][n]["channel_admitted"])
        for n in target_names
    ]
    check()
    np.savez_compressed(
        output,
        model_schema=np.asarray(ADAPTIVE_SCHEMA),
        basis=np.asarray(basis, dtype=np.float64),
        oldbasis=np.asarray(oldbasis, dtype=np.float64),
        templates=target_templates,
        oldtemplates=np.zeros((len(target_indices), 48, 64), dtype=np.float64),
        delta=np.zeros((1, 1), dtype=np.float64),
        local=np.zeros(len(target_indices), dtype=bool),
        old=np.zeros(len(target_indices), dtype=bool),
        channel_indices=np.asarray(target_indices, dtype=np.int64),
        enabled=np.asarray(final_enabled, dtype=bool),
        amplitude_nodes=target_nodes,
        envelope_cell_origin=np.asarray(origin_rc, dtype=np.int64),
    )

    selected = []
    for i, name in enumerate(target_names):
        ci = mapping[name]
        ok = final_enabled[i]
        ch_ev = eval_res["channels"][name]
        selected.append({
            "channel": name,
            "channel_index": ci,
            "model": candidate_id if ok else None,
            "outcome": "fitted" if ok else "unchanged_failed_admission_criteria",
            "development_mean_r2": ch_ev["development_selection_score"],
            "scores": template_reproducibility[name]["scores"],
            "failures": ch_ev["admission_failures"],
        })

    rec = {
        "status": "supported" if any(final_enabled) else "unchanged",
        "schema": ADAPTIVE_SCHEMA,
        "model_schema": ADAPTIVE_SCHEMA,
        "fallback_from_h_auto_reason": fallback_reason,
        "selected_candidate": candidate_id,
        "bf_reference": reference,
        "initial_geometry": orig_geom,
        "supported_x_geometry": xgeom,
        "initial_basis": oldbasis.tolist(),
        "basis": basis.tolist(),
        "repeat_cell_counts": dict(group_counts),
        "total_lattice_cells_sampled": len(cells),
        "bf_periodicity_selection_r2": template_reproducibility["BF"]["scores"],
        "train_channel_coupling_gammas": gammas,
        "bf_prediction_calibration": bf_calibration,
        "channel_prediction_calibration": calibrations,
        "local_amplitude_calibration": local_calibration,
        "acceptance_criteria": criteria,
        "admission_evaluation": eval_res,
        "working_memory": memory_plan,
        "selected": selected,
    }
    output.with_suffix(".json").write_text(json.dumps(rec, indent=2, allow_nan=False) + "\n")
    progress(1.0)
    return base.Model(output), rec

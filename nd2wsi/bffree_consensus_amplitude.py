"""Pinned fluorescence-only research mathematics, packaged without external paths.

Ported from the verified ExM consensus implementation. Numerical
operations and frozen defaults are retained; checkpoints support cancellation.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import distance_transform_edt, gaussian_filter, label
from scipy.optimize import minimize

from .bffree_consensus_math import checkpoint

METHOD_DESCRIPTION = (
    "Confidence-weighted robust nonnegative low-rank amplitude decomposition "
    "A = U @ V.T, jointly optimized over orthogonal positions and complete "
    "periods. Quadratic amplitude-gradient penalties use physical distance in "
    "units of P. T and its "
    "P-spaced phase lattice are not changed. Invalid nodes stay zero. Valid "
    "zero-weight nodes can be predicted within two periods of supported "
    "evidence in their valid connected component. Only the inherited generic "
    "positive-gain amplitude cap applies."
)

_BASE = dict(
    orthogonal_smoothness_periods=0.25,
    periodic_smoothness_periods=0.50,
    huber_scale_multiplier=1.345,
    minimum_huber_scale_cap_fraction=0.005,
    support_radius_periods=2.0,
    maximum_iterations=500,
    function_tolerance=1e-11,
    gradient_tolerance=1e-8,
)
DEFAULT_CONFIG = {**_BASE, "rank": 1}


def _weighted_median(values, weights):
    checkpoint()
    order = np.argsort(values, kind="stable")
    values, weights = values[order], weights[order]
    return float(values[np.searchsorted(np.cumsum(weights), weights.sum() / 2)])


def _configuration(config):
    checkpoint()
    cfg = dict(config)
    if set(cfg) != set(_BASE) | {"rank"}:
        raise ValueError("Configuration must specify exactly the declared options")
    if cfg["rank"] not in (1, 2) or isinstance(cfg["rank"], bool):
        raise ValueError("Only predeclared ranks 1 and 2 are supported")
    for key in _BASE:
        value = cfg[key]
        if isinstance(value, bool) or not np.isfinite(value) or value <= 0:
            raise ValueError(f"Invalid {key}")
    if int(cfg["maximum_iterations"]) != cfg["maximum_iterations"]:
        raise ValueError("maximum_iterations must be integral")
    return cfg


def _geometry(evidence, shape):
    checkpoint()
    phase = np.asarray(evidence["phase_nodes"], dtype=float)
    orthogonal = np.asarray(evidence["orthogonal_native_nodes"], dtype=float)
    if phase.shape != (shape[1],) or orthogonal.shape != (shape[0],):
        raise ValueError("Coordinate lengths differ from the amplitude lattice")
    if not np.all(np.isfinite(phase)) or not np.all(np.isfinite(orthogonal)):
        raise ValueError("Coordinates must be finite")
    if len(phase) > 1 and not np.allclose(np.diff(phase), 1, rtol=0, atol=1e-10):
        raise ValueError("Periodic-axis amplitude lattice must retain spacing P")
    period = float(evidence["period_native_pixels"])
    step = float(evidence["orthogonal_step_in_periods"])
    downsample = float(evidence["downsample"])
    if not all(np.isfinite(v) and v > 0 for v in (period, step, downsample)):
        raise ValueError("Invalid physical spacing")
    if not np.isclose(step, downsample / period, rtol=1e-8, atol=1e-12):
        raise ValueError("Orthogonal period spacing does not match source geometry")
    if len(orthogonal) > 1 and not np.allclose(
        np.diff(orthogonal) / period, step, rtol=1e-8, atol=1e-12
    ):
        raise ValueError("Orthogonal coordinates do not match physical spacing")
    return step


def _nonnegative_svd(seed, rank):
    """Deterministic nonnegative SVD initialization, with tiny live components."""
    checkpoint()
    left, singular, right = np.linalg.svd(seed, full_matrices=False)
    rows, columns = seed.shape
    u = np.zeros((rows, rank), dtype=float)
    v = np.zeros((columns, rank), dtype=float)
    for j in range(rank):
        if j == 0:
            u[:, j] = np.sqrt(singular[j]) * np.abs(left[:, j])
            v[:, j] = np.sqrt(singular[j]) * np.abs(right[j])
            continue
        positive_left = np.maximum(left[:, j], 0)
        negative_left = np.maximum(-left[:, j], 0)
        positive_right = np.maximum(right[j], 0)
        negative_right = np.maximum(-right[j], 0)
        p = np.linalg.norm(positive_left) * np.linalg.norm(positive_right)
        n = np.linalg.norm(negative_left) * np.linalg.norm(negative_right)
        a, b, strength = (
            (positive_left, positive_right, p)
            if p >= n else (negative_left, negative_right, n)
        )
        if strength > 0:
            scale = np.sqrt(singular[j] * strength)
            u[:, j] = scale * a / np.linalg.norm(a)
            v[:, j] = scale * b / np.linalg.norm(b)
    # Exact zeros lock projected bilinear updates. This seed affects only the
    # optimization start; signed observations still allow the optimum A=0.
    epsilon = max(float(seed.mean()), 1e-12) ** 0.5 * 1e-4
    u = np.maximum(u, epsilon)
    v = np.maximum(v, epsilon)
    return u, v


def fit_amplitude(evidence, config):
    """Return a finite nonnegative amplitude lattice and plain diagnostics.

    The loss operates on signed observations, so negative/zero evidence is not
    discarded before fitting. Spatial penalties are integrated in P units:
    h*sum(data loss), sum(d_y A**2)/h, and h*sum(d_phi A**2).
    """
    checkpoint()
    cfg = _configuration(config)
    y = np.asarray(evidence["amplitude"], dtype=float)
    weight = np.asarray(evidence["weight"], dtype=float)
    valid = np.asarray(evidence["valid"], dtype=bool)
    if y.ndim != 2 or weight.shape != y.shape or valid.shape != y.shape:
        raise ValueError("Evidence must have matching orthogonal-by-cycle arrays")
    if not np.all(np.isfinite(weight)) or np.any(weight < 0):
        raise ValueError("Confidence weights must be finite and nonnegative")
    if np.any(valid & (weight > 0) & ~np.isfinite(y)):
        raise ValueError("Supported amplitude observations must be finite")
    cap = float(evidence["amplitude_cap"])
    if not np.isfinite(cap) or cap <= 0:
        raise ValueError("amplitude_cap must be finite and positive")
    h = _geometry(evidence, y.shape)
    active = valid & (weight > 0) & np.isfinite(y)
    result = np.zeros(y.shape, dtype=float)
    diagnostics = dict(
        method="robust_nonnegative_low_rank_joint_amplitude",
        config=cfg,
        rank_requested=int(cfg["rank"]),
        shape=list(y.shape),
        supported_node_count=int(active.sum()),
        valid_node_count=int(valid.sum()),
        zero_weight_node_count=int((valid & (weight == 0)).sum()),
        orthogonal_step_in_periods=h,
        periodic_step_in_periods=1.0,
        amplitude_cap=cap,
        uses_bf=False,
        changes_template=False,
        support_policy="Invalid nodes and valid components lacking evidence are zero. Zero-weight valid nodes may receive a prediction within two P of supported evidence in the same component",
        limitations=[
            "A low-rank field may underfit a genuine complex amplitude pattern.",
            "A specimen feature coherent with the fixed T can still imitate illumination.",
            "The nonnegative factor fit is nonconvex and uses a deterministic initialization; global optimality is not claimed.",
            "Zero-confidence predictions are interpolations from supported evidence and are not independent measurements.",
            "Tests are engineering and synthetic-method checks, not biological validation.",
        ],
    )
    if y.size == 0 or not np.any(active):
        diagnostics.update(status="no_evidence", solver_success=True, iterations=0)
        return result, diagnostics
    if not np.any(y[active] > 0):
        diagnostics.update(status="nonpositive_evidence", solver_success=True, iterations=0)
        return result, diagnostics

    components, count = label(valid)
    allowed = np.zeros_like(valid)
    # Distances are calculated separately, so a nearby but disconnected valid
    # region cannot lend support across an invalid boundary.
    for component in range(1, count + 1):
        checkpoint()
        region = components == component
        support = region & active
        if np.any(support):
            distance = distance_transform_edt(~support, sampling=(h, 1.0))
            allowed |= region & (distance <= cfg["support_radius_periods"])
    diagnostics["predicted_zero_weight_nodes"] = int(np.sum(allowed & ~active))

    # Relative confidence is retained; scale normalization prevents a common
    # arbitrary multiplier on every weight from changing regularization.
    w = np.where(active, weight, 0).copy()
    w /= _weighted_median(w[active], np.ones(int(active.sum())))
    data = np.where(active, y, 0)
    sigma = (
        cfg["orthogonal_smoothness_periods"] / h,
        cfg["periodic_smoothness_periods"],
    )
    numerator = gaussian_filter(w * np.clip(data, 0, cap), sigma, mode="nearest")
    denominator = gaussian_filter(w, sigma, mode="nearest")
    seed = np.divide(numerator, denominator, out=np.zeros_like(data), where=denominator > 0)
    residual0 = data[active] - seed[active]
    center = _weighted_median(residual0, w[active])
    robust_sigma = 1.4826 * _weighted_median(np.abs(residual0 - center), w[active])
    delta = max(
        cfg["huber_scale_multiplier"] * robust_sigma,
        cfg["minimum_huber_scale_cap_fraction"] * cap,
        np.finfo(float).eps,
    )
    rank = min(int(cfg["rank"]), *y.shape)
    rows, columns = y.shape
    u, v = _nonnegative_svd(seed, rank)
    split = rows * rank
    mask_y = allowed[1:, :] & allowed[:-1, :]
    mask_x = allowed[:, 1:] & allowed[:, :-1]
    ly = cfg["orthogonal_smoothness_periods"] ** 2 / h
    lx = cfg["periodic_smoothness_periods"] ** 2 * h
    normalization = max(float(h * w.sum()), np.finfo(float).eps)

    def objective(theta):
        checkpoint()
        u_local = theta[:split].reshape(rows, rank)
        v_local = theta[split:].reshape(columns, rank)
        amplitude = u_local @ v_local.T
        residual = amplitude - data
        scaled = residual / delta
        root = np.hypot(1.0, scaled)
        # Stable form of sqrt(1+s^2)-1 near zero.
        cost = h * np.sum(w * residual**2 / (root + 1))
        gradient = h * w * residual / root
        dy = np.diff(amplitude, axis=0) * mask_y
        dx = np.diff(amplitude, axis=1) * mask_x
        cost += 0.5 * ly * np.sum(dy**2) + 0.5 * lx * np.sum(dx**2)
        gradient[1:, :] += ly * dy
        gradient[:-1, :] -= ly * dy
        gradient[:, 1:] += lx * dx
        gradient[:, :-1] -= lx * dx
        gu = gradient @ v_local
        gv = gradient.T @ u_local
        return float(cost / normalization), np.concatenate((gu.ravel(), gv.ravel())) / normalization

    fit = minimize(
        objective,
        np.concatenate((u.ravel(), v.ravel())),
        jac=True,
        method="L-BFGS-B",
        bounds=[(0, None)] * (rows + columns) * rank,
        options=dict(
            maxiter=int(cfg["maximum_iterations"]),
            ftol=cfg["function_tolerance"],
            gtol=cfg["gradient_tolerance"],
            maxls=40,
        ),
    )
    u = fit.x[:split].reshape(rows, rank)
    v = fit.x[split:].reshape(columns, rank)
    latent = u @ v.T
    if not np.all(np.isfinite(latent)):
        raise RuntimeError("Low-rank solver returned a nonfinite amplitude field")
    result[allowed] = np.clip(latent[allowed], 0, cap)
    diagnostics.update(
        status="fitted" if fit.success else "solver_limit",
        solver_success=bool(fit.success),
        solver_message=str(fit.message),
        iterations=int(fit.nit),
        objective=float(fit.fun),
        rank_effective=rank,
        robust_residual_sigma=float(robust_sigma),
        pseudo_huber_delta=float(delta),
        weighted_rmse=float(np.sqrt(np.sum(w * (result - data)**2) / w.sum())),
        cap_fraction=float(np.mean(latent[active] > cap)),
        amplitude_min=float(result.min()),
        amplitude_max=float(result.max()),
    )
    return result, diagnostics

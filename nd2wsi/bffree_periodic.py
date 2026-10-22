"""Frozen independent x/h1 equations for the experimental native preview.

Adapted from tools/shading_benchmark/candidates.py. The numerical equations and
DEFAULTS are unchanged; only axes=x and harmonics=1 are selected. Each target
channel is screened independently, and callbacks add cancellation boundaries.
No BF data, masks, acquisition priors or geometry are accepted here.
"""
from __future__ import annotations

import warnings
from types import MappingProxyType

import numpy as np
from scipy.optimize import minimize_scalar
from scipy.signal import detrend


class Abstain(RuntimeError):
    """A candidate cannot produce a supported field for the supplied inputs."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


DEFAULTS = {
    "axes": ["x"],
    "period_min_px": 12.0,
    "period_max_px": 128.0,
    "min_repeats": 6.0,
    "harmonics": 3,
    "peak_ratio": 8.0,
    "min_modulation": 0.005,
    "frequency_tolerance": 0.10,
    "split_correlation": 0.65,
    "gain_min": 0.5,
    "gain_max": 2.0,
    "min_valid_fraction": 0.70,
    "min_images": 5,
    "smooth_sigma_px": 12.0,
    "multiimage_split_rmse": 0.15,
}

# An immutable source for every fit; result dictionaries receive fresh copies.
_DEFAULTS = MappingProxyType({**DEFAULTS, "axes": tuple(DEFAULTS["axes"])})


def frozen_parameters():
    return {**_DEFAULTS, "axes": ["x"], "harmonics": 1}


def _median(values, axis=None, keepdims=False):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(values, axis=axis, keepdims=keepdims)


def _profile(image, axis):
    p = _median(image, axis=0 if axis == "x" else 1)
    finite = np.isfinite(p)
    if finite.mean() < 0.9:
        raise Abstain("insufficient_profile_support")
    p = np.interp(np.arange(p.size), np.flatnonzero(finite), p[finite])
    return p


def _frequency(profile, cfg, *, check=lambda: None):
    check()
    n = len(profile)
    low = max(1 / cfg["period_max_px"], cfg["min_repeats"] / (2 * n))
    high = 1 / cfg["period_min_px"]
    if low >= high or n < 32:
        raise Abstain("insufficient_repeats")
    signal = detrend(profile)
    if float(np.std(signal)) < cfg["min_modulation"]:
        raise Abstain("weak_periodic_evidence")
    window = np.hanning(n)
    power = np.abs(np.fft.rfft(signal * window)) ** 2
    freq = np.fft.rfftfreq(n)
    allowed = (freq >= low) & (freq <= high)
    if allowed.sum() < 3:
        raise Abstain("insufficient_frequency_resolution")
    indices = np.flatnonzero(allowed)
    best = int(indices[np.argmax(power[allowed])])
    # The Hann window spreads even a pure sinusoid into its immediate adjacent
    # bins. Those are signal, not an independent estimate of the noise floor.
    noise_bins = allowed & (abs(np.arange(power.size) - best) > 1)
    if noise_bins.sum() < 2:
        raise Abstain("insufficient_frequency_resolution")
    noise = float(np.median(power[noise_bins]))
    ratio = float(power[best] / max(noise, 1e-20))
    if ratio < cfg["peak_ratio"]:
        raise Abstain("no_dominant_periodic_frequency")
    coords = np.arange(n)
    def loss(f):
        check()
        return -abs(np.sum(signal * window * np.exp(-2j * np.pi * f * coords))) ** 2
    result = minimize_scalar(loss, bounds=(max(low, freq[best] - 1/n),
                                           min(high, freq[best] + 1/n)),
                             method="bounded", options={"xatol": 1e-10})
    if not result.success:
        raise Abstain("frequency_optimizer_failure")
    f = float(result.x)
    if f <= low * 1.001 or f >= high * 0.999:
        raise Abstain("frequency_at_search_boundary")
    return f, ratio


def _frequency_pair(profile, cfg, *, check=lambda: None):
    split = len(profile) // 2
    f0, r0 = _frequency(profile[:split], cfg, check=check)
    f1, r1 = _frequency(profile[split:], cfg, check=check)
    if abs(f0 - f1) / min(f0, f1) > cfg["frequency_tolerance"]:
        raise Abstain("inconsistent_split_frequencies")
    f, ratio = _frequency(profile, cfg, check=check)
    if len(profile) * f < cfg["min_repeats"]:
        raise Abstain("insufficient_repeats")
    return f, {"frequency": f, "period_px": 1 / f, "peak_ratio": ratio,
               "split_frequencies": [f0, f1], "split_peak_ratios": [r0, r1]}


def _harmonic(profile, f, cfg, *, check=lambda: None):
    check()
    x = np.arange(len(profile), dtype=float)
    cols = [np.cos(2*np.pi*k*f*x) for k in range(1, int(cfg["harmonics"]) + 1)]
    cols += [np.sin(2*np.pi*k*f*x) for k in range(1, int(cfg["harmonics"]) + 1)]
    waves = np.stack(cols, axis=1)
    design = np.column_stack((np.ones(x.size), (x - x.mean()) / x.size, waves))
    def solve(mask):
        check()
        beta = np.linalg.lstsq(design[mask], profile[mask], rcond=None)[0]
        # Robust reweighting only sees the provided target-channel profile.
        for _ in range(4):
            check()
            residual = profile[mask] - design[mask] @ beta
            scale = max(1.4826 * float(np.median(abs(residual - np.median(residual)))), 1e-6)
            weights = np.sqrt(np.minimum(1., 1.345 * scale / np.maximum(abs(residual), 1e-12)))
            beta = np.linalg.lstsq(design[mask] * weights[:, None], profile[mask] * weights,
                                   rcond=None)[0]
        return waves @ beta[2:]
    left = x < x.size // 2
    right = ~left
    a, b = solve(left), solve(right)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-14:
        raise Abstain("weak_periodic_evidence")
    corr = float(np.dot(a, b) / denom)
    ratio = float(np.linalg.norm(a) / np.linalg.norm(b))
    if corr < cfg["split_correlation"] or not 0.5 <= ratio <= 2.0:
        raise Abstain("inconsistent_split_phase_or_amplitude")
    field = solve(np.ones(x.size, dtype=bool))
    field -= field.mean()
    if float(np.std(field)) < cfg["min_modulation"]:
        raise Abstain("weak_periodic_evidence")
    return field, {"split_wave_correlation": corr, "split_amplitude_ratio": ratio}


def _check_gain(gain, cfg):
    if not np.isfinite(gain).all() or np.any(gain < cfg["gain_min"]) or np.any(gain > cfg["gain_max"]):
        raise Abstain("gain_outside_frozen_bounds")


def fit_channel(raw_yx, *, check=lambda: None):
    """Return the compact field after the caller admits this channel's memory.

    With one input image, this is the frozen benchmark's normalization,
    median profile, frequency selection and robust harmonic fit in the same
    order. Global input/gain failures become this channel's abstention only.
    """
    check()
    cfg = frozen_parameters()
    raw = np.asarray(raw_yx, dtype=np.float64)
    if raw.ndim != 2 or min(raw.shape) < 8:
        raise ValueError("A target channel must have Y,X >= 8")
    if np.any(np.isfinite(raw) & (raw < 0)):
        raise Abstain("negative_input_values")
    valid = np.isfinite(raw) & (raw > 0)
    fraction = float(valid.mean())
    if fraction < cfg["min_valid_fraction"]:
        raise Abstain("insufficient_positive_target_support")
    check()
    median = _median(np.where(valid, raw, np.nan), axis=(-2, -1), keepdims=True)
    if not np.isfinite(median).all() or np.any(median <= 0):
        raise Abstain("empty_target_frame_or_channel")
    normalized = np.where(valid, raw / median, np.nan)
    check()
    profile = _profile(normalized, "x")
    del normalized, valid, raw
    frequency, evidence = _frequency_pair(profile, cfg, check=check)
    wave, consistency = _harmonic(profile, frequency, cfg, check=check)
    gain = 1.0 + wave
    _check_gain(gain, cfg)
    check()
    return gain, {"axis": "x", "status": "fitted", **evidence,
                  "applied_frequency": frequency, **consistency,
                  "positive_target_fraction": fraction}

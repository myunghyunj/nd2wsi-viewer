"""Pinned fluorescence-only research mathematics, packaged without external paths.

Ported from the verified ExM consensus rank-1 implementation. Numerical
operations and frozen defaults are retained; checkpoints support cancellation.
"""
from __future__ import annotations

import warnings
from contextlib import contextmanager
from contextvars import ContextVar

import numpy as np
from scipy.signal import detrend, find_peaks

_CHECK = ContextVar("bffree_consensus_check", default=lambda: None)

def checkpoint():
    _CHECK.get()()

@contextmanager
def cancellation(check):
    token = _CHECK.set(check)
    try:
        check()
        yield
    finally:
        _CHECK.reset(token)

ALLOWED_CHANNELS = frozenset(("FITC", "CY3", "DAPI"))
DEFAULT_CONFIG = {
    "axes": ["x", "y"],
    "period_min_px": 12.0,
    "period_max_px": 128.0,
    "min_repeats": 6.0,
    "harmonics": 3,
    "min_channels": 2,
    "min_valid_fraction": 0.70,
    "min_profile_fraction": 0.90,
    "min_modulation": 0.005,
    "peak_ratio": 8.0,
    "min_explained_fraction": 0.08,
    "harmonic_relative_threshold": 0.15,
    "frequency_tolerance": 0.05,
    "split_correlation": 0.75,
    "split_amplitude_ratio_min": 0.5,
    "split_amplitude_ratio_max": 2.0,
    "waveform_correlation": 0.80,
    "axis_score_ratio": 1.25,
    "period_score_ratio": 1.15,
    "gain_min": 0.5,
    "gain_max": 2.0,
    "spectral_peaks_per_channel": 6,
    "huber_iterations": 4,
    "huber_constant": 1.345,
}

class Abstain(RuntimeError):
    def __init__(self, reason):
        checkpoint()
        self.reason = reason
        super().__init__(reason)


def _median(values, axis=None, keepdims=False):
    # Adapted from tools/shading_benchmark/candidates.py::_median/_profile.
    checkpoint()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(values, axis=axis, keepdims=keepdims)


def _correlation(a, b):
    checkpoint()
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a - a.mean(), b - b.mean()
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / norm) if norm > 1e-16 else 0.0


def _centers(length, factor):
    checkpoint()
    starts = np.arange(0, length, factor, dtype=float)
    return starts + (np.minimum(factor, length-starts)-1)/2


def _profile(image, axis, cfg):
    checkpoint()
    profile = _median(image, axis=0 if axis == "x" else 1)
    finite = np.isfinite(profile)
    if finite.mean() < cfg["min_profile_fraction"]:
        raise Abstain("insufficient_profile_support")
    if not finite.all():
        profile = np.interp(np.arange(len(profile)), np.flatnonzero(finite), profile[finite])
    return profile


def _basis(coordinates, frequency, harmonics=3):
    checkpoint()
    angle = 2*np.pi*frequency*np.asarray(coordinates, float)
    return np.column_stack([np.cos(k*angle) for k in range(1, harmonics+1)] +
                           [np.sin(k*angle) for k in range(1, harmonics+1)])


def _range(length, cfg):
    checkpoint()
    low = max(1/cfg["period_max_px"], cfg["min_repeats"]/length)
    high = 1/cfg["period_min_px"]
    if length < 48 or low >= high:
        raise Abstain("insufficient_repeats_or_frequency_range")
    return low, high


def _spectrum(profile):
    checkpoint()
    signal = detrend(np.asarray(profile, float))
    return np.fft.rfftfreq(len(signal)), abs(np.fft.rfft(signal*np.hanning(len(signal))))**2


def _seeds(profiles, cfg):
    checkpoint()
    n = len(profiles[0])
    low, high = _range(n, cfg)
    seeds = []
    for profile in profiles:
        if float(np.std(detrend(profile))) < cfg["min_modulation"]:
            continue
        frequencies, power = _spectrum(profile)
        allowed = (frequencies >= low) & (frequencies <= min(3*high, .25))
        peaks = find_peaks(power)[0]
        peaks = [i for i in peaks if allowed[i]]
        peaks = sorted(peaks, key=lambda i: power[i], reverse=True)[:int(cfg["spectral_peaks_per_channel"])]
        for peak in peaks:
            for harmonic in (1, 2, 3):
                f = float(frequencies[peak]/harmonic)
                if low < f < high:
                    seeds.append(f)
    # Full-image FFT peaks are only proposals, never acceptance evidence.
    return sorted(set(round(f, 12) for f in seeds)), low, high

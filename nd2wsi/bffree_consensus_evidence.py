"""Pinned fluorescence-only research mathematics, packaged without external paths.

Ported from the verified ExM consensus implementation. Numerical
operations and frozen defaults are retained; checkpoints support cancellation.
"""
from __future__ import annotations

import hashlib
import json
import warnings

import numpy as np

from .bffree_consensus_math import checkpoint

SCHEMA = 'exm-joint-amplitude-benchmark/1'
EVIDENCE_CONFIG = dict(min_positive_fraction=.70, min_samples=10,
    alternating_iterations=12, huber_constant=1.345,
    confidence_floor=.02, split_scale=.02, edge_taper_cycles=1.0)
FROZEN_KEYS = ('axis', 'frequency_cycles_per_native_pixel', 'period_native_pixels',
               'phase_grid', 'template_values', 'supported_harmonics')

def json_hash(value):
    checkpoint()
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def centers(length, factor):
    checkpoint()
    starts = np.arange(0, length, factor, dtype=float)
    ends = np.minimum(starts+factor, length)
    return (starts+ends-1)/2

def smoothstep(x):
    checkpoint()
    t = np.clip(x, 0, 1)
    return t*t*(3-2*t)

def _median(a, axis):
    checkpoint()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        return np.nanmedian(a, axis=axis)

def _amplitude_limits(template):
    checkpoint()
    lo, hi = float(np.min(template)), float(np.max(template))
    positive = min(.5, .5/max(-lo, 1e-12), 1/max(hi, 1e-12))
    negative = min(.5, .5/max(hi, 1e-12), 1/max(-lo, 1e-12))
    return -negative, positive

def _baseline_fit(z, t, u, a, weights):
    checkpoint()
    g = 1+a[:, None]*t
    d0, d1 = g, g*u
    s00 = (weights*d0*d0).sum(1)+1e-10
    s01 = (weights*d0*d1).sum(1)
    s11 = (weights*d1*d1).sum(1)+1e-10
    r0 = (weights*d0*z).sum(1)
    r1 = (weights*d1*z).sum(1)
    det = np.maximum(s00*s11-s01*s01, 1e-20)
    b0 = (r0*s11-r1*s01)/det
    b1 = (r1*s00-r0*s01)/det
    return b0[:, None]+b1[:, None]*u

def project_rows(values, template, coordinate):
    """Robust alternating fit of I=(b0+b1*u)*(1+a*T), per actual row.

    Signed a is retained as evidence; a negative projection is evidence against
    positive shading, rather than an absent observation to be filled by neighbors.
    """
    checkpoint()
    z0 = np.asarray(values, float)
    t = np.asarray(template, float)[None, :]
    valid = np.isfinite(z0)&(z0 > 0)
    z = np.where(valid, z0, 0.)
    u0 = np.asarray(coordinate, float)
    u = ((u0-u0.mean())/max(float(np.ptp(u0)), 1.))[None, :]
    lower, upper = _amplitude_limits(t)
    weights = valid.astype(float)
    # Additive initialization is used only to start the exact multiplicative fit.
    design = np.column_stack((np.ones(len(u0)), u.ravel(), t.ravel()))
    gram = np.einsum('rs,si,sj->rij', weights, design, design)
    gram += np.eye(3)[None]*1e-8
    rhs = np.einsum('rs,si,rs->ri', weights, design, z)
    beta = np.linalg.solve(gram, rhs[..., None])[..., 0]
    a = np.clip(beta[:, 2]/np.maximum(beta[:, 0], 1e-8), lower, upper)
    for _ in range(EVIDENCE_CONFIG['alternating_iterations']):
        checkpoint()
        baseline = _baseline_fit(z, t, u, a, weights)
        good = valid & np.isfinite(baseline) & (baseline > 1e-8)
        bt = baseline*t
        a = np.clip((weights*good*bt*(z-baseline)).sum(1)/
                    np.maximum((weights*good*bt*bt).sum(1), 1e-20), lower, upper)
        residual = z-baseline*(1+a[:, None]*t)
        center = _median(np.where(good, residual, np.nan), 1)
        scale = np.maximum(1.4826*_median(np.where(good, abs(residual-center[:, None]), np.nan), 1), 1e-6)
        huber = np.minimum(1., EVIDENCE_CONFIG['huber_constant']*scale[:, None]/
                           np.maximum(abs(residual), 1e-12))
        weights = good*np.nan_to_num(huber, nan=0.)
    baseline = _baseline_fit(z, t, u, a, weights)
    good = valid & np.isfinite(baseline) & (baseline > 1e-8)
    relative = np.divide(z, baseline, out=np.zeros_like(z), where=good)-1
    total = np.maximum(weights.sum(1), 1e-20)
    q = relative-(weights*relative).sum(1)[:, None]/total[:, None]
    tc = t-(weights*t).sum(1)[:, None]/total[:, None]
    corr = (weights*q*tc).sum(1)/np.sqrt(np.maximum((weights*q*q).sum(1)*(weights*tc*tc).sum(1), 1e-30))
    residual_scale = np.sqrt((weights*(relative-a[:, None]*t)**2).sum(1)/total)
    return dict(amplitude=np.nan_to_num(a), correlation=np.clip(np.nan_to_num(corr), -1, 1),
                residual_scale=np.nan_to_num(residual_scale), positive_fraction=good.mean(1),
                robust_weight_fraction=weights.sum(1)/max(z.shape[1], 1))

def prepare_channel(raw_image, model, native_shape_yx, downsample):
    checkpoint()
    native = tuple(native_shape_yx)
    factor = int(downsample)
    fit_shape = tuple(n//factor for n in native)
    image = np.asarray(raw_image, float)[:fit_shape[0], :fit_shape[1]]
    axis = model['axis']
    orth_axis = 0 if axis == 'x' else 1
    data = image if axis == 'x' else image.T
    coordinates = centers(native[1-orth_axis], factor)[:data.shape[1]]
    orthogonal = centers(native[orth_axis], factor)[:data.shape[0]]
    phase = coordinates*model['frequency_cycles_per_native_pixel']
    nodes = np.asarray(model['amplitude_phase_nodes'], float)
    if len(nodes) < 2 or not np.allclose(np.diff(nodes), 1., rtol=0, atol=1e-10):
        raise ValueError('The inherited phase lattice must have P spacing')
    shape = (len(orthogonal), len(nodes))
    out = {k:np.zeros(shape, float) for k in ('amplitude','weight','correlation',
            'residual_scale','positive_fraction','split_disagreement')}
    out['valid'] = np.zeros(shape, bool)
    for j, center in enumerate(nodes):
        checkpoint()
        ids = np.flatnonzero((phase >= center-.5)&(phase < center+.5))
        if len(ids) < EVIDENCE_CONFIG['min_samples']:
            continue
        t = np.interp(np.mod(phase[ids], 1), model['phase_grid'], model['template_values'], period=1)
        stats = project_rows(data[:, ids], t, coordinates[ids])
        split = [project_rows(data[:, ids[p::2]], t[p::2], coordinates[ids[p::2]]) for p in (0,1)]
        delta = abs(split[0]['amplitude']-split[1]['amplitude'])
        good = stats['positive_fraction'] >= EVIDENCE_CONFIG['min_positive_fraction']
        confidence = stats['positive_fraction']*(EVIDENCE_CONFIG['confidence_floor']+
            (1-EVIDENCE_CONFIG['confidence_floor'])*stats['correlation']**2)
        confidence *= np.exp(-delta/(EVIDENCE_CONFIG['split_scale']+abs(stats['amplitude'])))
        confidence *= np.clip(stats['robust_weight_fraction'], 0, 1)
        out['amplitude'][:, j] = stats['amplitude']
        out['weight'][:, j] = np.where(good, np.clip(confidence, 0, 1), 0)
        out['valid'][:, j] = good
        for k in ('correlation','residual_scale','positive_fraction'):
            out[k][:, j] = stats[k]
        out['split_disagreement'][:, j] = delta
    out.update(phase_nodes=nodes.copy(), orthogonal_native_nodes=orthogonal,
        period_native_pixels=float(model['period_native_pixels']), downsample=factor,
        orthogonal_step_in_periods=float(factor/model['period_native_pixels']),
        amplitude_cap=_amplitude_limits(model['template_values'])[1],
        axis=axis, schema='exm-joint-amplitude-evidence/1')
    return out

def _interpolate_nodes(nodes, orth, phase, orth_nodes, phase_nodes):
    # Separable C1 convex interpolation. No native-coordinate refitting.
    checkpoint()
    yy = np.interp(orth, orth_nodes, np.arange(len(orth_nodes), dtype=float))
    xx = np.clip(phase-phase_nodes[0], 0, len(phase_nodes)-1)
    y0 = np.floor(yy).astype(int)
    y1 = np.minimum(y0+1, len(orth_nodes)-1)
    x0 = np.floor(xx).astype(int)
    x1 = np.minimum(x0+1, len(phase_nodes)-1)
    fy = smoothstep(yy-y0)[:,None]
    fx = smoothstep(xx-x0)[None,:]
    return ((1-fy)*((1-fx)*nodes[y0[:,None],x0[None,:]]+fx*nodes[y0[:,None],x1[None,:]])+
            fy*((1-fx)*nodes[y1[:,None],x0[None,:]]+fx*nodes[y1[:,None],x1[None,:]]))


def components_at(model, ys, xs):
    checkpoint()
    axis = model['axis']
    q = np.asarray(xs if axis=='x' else ys, float)
    orth = np.asarray(ys if axis=='x' else xs, float)
    phase = q*model['frequency_cycles_per_native_pixel']
    t = np.interp(np.mod(phase,1), model['phase_grid'], model['template_values'], period=1)
    nodes = np.asarray(model['joint_amplitude_nodes'], float)
    pn = np.asarray(model['joint_phase_nodes'], float)
    on = np.asarray(model['joint_orthogonal_native_nodes'], float)
    a = _interpolate_nodes(nodes, orth, phase, on, pn)
    taper = float(EVIDENCE_CONFIG['edge_taper_cycles'])
    edge = np.minimum(smoothstep((phase-(pn[0]-taper))/taper),
                      smoothstep(((pn[-1]+taper)-phase)/taper))
    a *= edge[None,:]
    if axis=='y':
        a = a.T
    shape = (len(ys),len(xs))
    template = np.broadcast_to(t[None,:] if axis=='x' else t[:,None], shape)
    phasefield = np.broadcast_to(phase[None,:] if axis=='x' else phase[:,None], shape)
    return dict(T=template, A=a, G=1+a*template, phase_unwrapped=phasefield)

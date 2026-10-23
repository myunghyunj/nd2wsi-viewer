"""Pinned fluorescence-only research mathematics, packaged without external paths.

Ported from the verified ExM consensus implementation. Numerical
operations and frozen defaults are retained; checkpoints support cancellation.
"""
from __future__ import annotations

import itertools
import math

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.optimize import minimize_scalar

from . import bffree_consensus_math as v1
from .bffree_consensus_math import checkpoint

SCHEMA = "exm-channel-specific-sampled-template-amplitude/2"
DEFAULT_CONFIG = {key:value for key,value in v1.DEFAULT_CONFIG.items()
                  if key not in ("min_channels","waveform_correlation","split_amplitude_ratio_min","split_amplitude_ratio_max")}
DEFAULT_CONFIG.update(harmonic_relative_threshold=.10, harmonic_split_correlation=.85,
                      harmonic_peak_ratio=8.0, max_native_axis_pixels=2_000_000,
                      phase_bins=64, template_smoothing_sigma_bins=1.0,
                      template_max_harmonics=8, min_original_samples_per_harmonic=4,
                      template_split_correlation=.80, cycle_correlation=.50,
                      cycle_explained_fraction=.30, min_supported_cycles=4,
                      min_supported_cycle_fraction=.50, amplitude_sigma_cycles=.75,
                      amplitude_max=.50, edge_taper_cycles=1.0)
LIMITATIONS = [
    "A stable repeating specimen pattern can be mistaken for illumination in an individual channel.",
    "A channel-specific fit is not proof that the fitted function is optical shading.",
    "Only one stationary x- or y-aligned phase function per channel is represented; amplitude may vary across cycles.",
    "Mean-bin sampling and interpolation attenuate modulation; native evaluation does not perform optical deconvolution.",
    "Split checks share the acquisition and are not independent biological validation.",
    "No BF amplitude, sign, phase, mask, prior, geometry or result enters fitting.",
    "DAPI contributes optional frequency proposals and diagnostics but is never corrected.",
]


def _config(config):
    checkpoint()
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    if set(cfg) != set(DEFAULT_CONFIG):
        raise ValueError("Unknown channel-specific configuration fields")
    cfg["axes"] = list(cfg["axes"])
    if not cfg["axes"] or set(cfg["axes"])-{"x","y"} or len(set(cfg["axes"]))!=len(cfg["axes"]):
        raise ValueError("axes must be unique x and/or y")
    for key,value in cfg.items():
        if key!="axes" and (isinstance(value,bool) or not np.isfinite(value) or value<=0):
            raise ValueError(f"{key} must be finite and positive")
    for key in ("harmonics","spectral_peaks_per_channel","huber_iterations","max_native_axis_pixels","phase_bins","min_supported_cycles","template_max_harmonics","min_original_samples_per_harmonic"):
        if int(cfg[key])!=cfg[key]:
            raise ValueError(f"{key} must be integral")
    if cfg["harmonics"]!=3 or not 12<=cfg["period_min_px"]<cfg["period_max_px"] or cfg["min_repeats"]<6:
        raise ValueError("Fixed search requires 1..3 harmonics, >=12 samples per fundamental and >=6 repeats")
    for key in ("min_valid_fraction","min_profile_fraction","min_modulation","min_explained_fraction",
                "harmonic_relative_threshold","frequency_tolerance","split_correlation","harmonic_split_correlation",
                "template_split_correlation","cycle_correlation","cycle_explained_fraction","min_supported_cycle_fraction"):
        if cfg[key]>1:
            raise ValueError(f"{key} must be <=1")
    if not cfg["gain_min"]<1<cfg["gain_max"]:
        raise ValueError("Gain bounds must contain one")
    return cfg


def _fit(profile,x,frequency,harmonics,cfg,robust=True):
    checkpoint()
    all_waves = v1._basis(x,frequency)
    columns = [k-1 for k in harmonics]+[k+2 for k in harmonics]
    nuisance = np.column_stack((np.ones(len(x)),(x-x.mean())/max(np.ptp(x),1)))
    design = np.column_stack((nuisance,all_waves[:,columns]))
    beta = np.linalg.lstsq(design,profile,rcond=None)[0]
    if robust:
        for _ in range(int(cfg["huber_iterations"])):
            residual = profile-design@beta
            scale = max(1.4826*float(np.median(abs(residual-np.median(residual)))),1e-6)
            weights = np.sqrt(np.minimum(1,cfg["huber_constant"]*scale/np.maximum(abs(residual),1e-12)))
            beta = np.linalg.lstsq(design*weights[:,None],profile*weights,rcond=None)[0]
    coefficients = np.zeros(6)
    coefficients[columns] = beta[2:]
    wave = all_waves@coefficients
    base = profile-nuisance@np.linalg.lstsq(nuisance,profile,rcond=None)[0]
    fraction = float(np.clip(1-np.mean((profile-design@beta)**2)/max(np.mean(base**2),1e-20),0,1))
    return coefficients,wave-wave.mean(),fraction


def _harmonic_snr(profile,frequency,k,cfg):
    checkpoint()
    frequencies,power = v1._spectrum(profile)
    band = ((frequencies>=max(1/cfg["period_max_px"],1/len(profile))) &
            (frequencies<=min(3/cfg["period_min_px"],.25)))
    index = int(np.argmin(abs(frequencies-k*frequency)))
    signal = float(np.max(power[max(0,index-1):index+2]))
    for harmonic in (1,2,3):
        j = int(np.argmin(abs(frequencies-harmonic*frequency)))
        band[max(0,j-2):j+3] = False
    return signal/max(float(np.median(power[band])),1e-20) if band.sum()>=3 else 0.0


def _supported_harmonics(profile,x,frequency,harmonics,cfg):
    checkpoint()
    split = len(profile)//2
    full,wave,fraction = _fit(profile,x,frequency,harmonics,cfg)
    left = _fit(profile[:split],x[:split],frequency,harmonics,cfg)[0]
    right = _fit(profile[split:],x[split:],frequency,harmonics,cfg)[0]
    amplitudes = np.hypot(full[:3],full[3:])
    cutoff = max(cfg["min_modulation"],cfg["harmonic_relative_threshold"]*float(max(amplitudes)))
    accepted,records = [],[]
    for k in harmonics:
        indices = [k-1,k+2]
        a,b = left[indices],right[indices]
        na,nb = float(np.linalg.norm(a)),float(np.linalg.norm(b))
        agreement = float(np.dot(a,b)/max(na*nb,1e-20))
        ratio = na/max(nb,1e-20)
        snr = [_harmonic_snr(part,frequency,k,cfg) for part in (profile,profile[:split],profile[split:])]
        reasons = []
        if amplitudes[k-1]<cutoff or min(na,nb)<cfg["min_modulation"]:
            reasons.append("weak_harmonic")
        if agreement<cfg["harmonic_split_correlation"]:
            reasons.append("inconsistent_harmonic_phase")
        if min(snr)<cfg["harmonic_peak_ratio"]:
            reasons.append("weak_harmonic_spectrum")
        records.append({"harmonic":k,"supported":not reasons,"reasons":reasons,
                        "amplitude_fraction":float(amplitudes[k-1]),"split_phase_correlation":agreement,
                        "split_amplitude_ratio":ratio,"spectral_peak_ratio_full_left_right":snr})
        if not reasons:
            accepted.append(k)
    return accepted,records,full,wave,fraction,left,right


def _refine(profile,x,frequency,bounds,harmonics,cfg):
    checkpoint()
    result = minimize_scalar(lambda f:-_fit(profile,x,f,harmonics,cfg,robust=False)[2],
                             bounds=bounds,method="bounded",options={"xatol":1e-9,"maxiter":40})
    return float(result.x) if result.success else float(frequency)


def _candidate(profile,x,frequency,cfg):
    checkpoint()
    selected = [1,2,3]
    attempts = []
    for _ in range(3):
        accepted,records,coeff,wave,fraction,left,right = _supported_harmonics(profile,x,frequency,selected,cfg)
        attempts.append(records)
        if accepted==selected or not accepted:
            break
        selected = accepted
    selected = accepted
    record = {"frequency_cycles_per_sample":frequency,"period_samples":1/frequency,
              "harmonic_selection_passes":attempts,"supported_harmonics":selected,
              "accepted":False,"reasons":[]}
    if not selected:
        record.update(score=0.0,diagnostic_score=float(np.var(wave)*fraction))
        record["reasons"].append("no_split_supported_harmonic")
        return record,coeff
    coeff,wave,fraction = _fit(profile,x,frequency,selected,cfg)
    split = len(profile)//2
    left = _fit(profile[:split],x[:split],frequency,selected,cfg)[0]
    right = _fit(profile[split:],x[split:],frequency,selected,cfg)[0]
    a,b = v1._basis(x,frequency)@left,v1._basis(x,frequency)@right
    correlation,ratio = v1._correlation(a,b),float(np.std(a)/max(np.std(b),1e-20))
    low,high = v1._range(len(profile),cfg)
    half_frequencies = [_refine(part,c,frequency,(max(low,frequency*.9),min(high,frequency*1.1)),selected,cfg)
                        for part,c in ((profile[:split],x[:split]),(profile[split:],x[split:]))]
    drift = max(abs(half_frequencies[0]-half_frequencies[1])/frequency,
                *(abs(f-frequency)/frequency for f in half_frequencies))
    strength = float(np.std(wave))
    if math.gcd(*selected)!=1:
        record["reasons"].append("fundamental_not_identified")
    if strength<cfg["min_modulation"] or fraction<cfg["min_explained_fraction"]:
        record["reasons"].append("weak_target_channel_evidence")
    if correlation<cfg["split_correlation"]:
        record["reasons"].append("inconsistent_target_split_waveform")
    if drift>cfg["frequency_tolerance"]:
        record["reasons"].append("inconsistent_target_split_frequency")
    score = strength**2*fraction
    record.update(accepted=not record["reasons"],strength_fraction_rms=strength,explained_fraction=fraction,
                  split_wave_correlation=correlation,split_amplitude_ratio=ratio,
                  split_frequencies=half_frequencies,split_max_relative_frequency_drift=drift,
                  score=score,diagnostic_score=score)
    return record,coeff


def _search(profile,x,proposed,cfg):
    checkpoint()
    low,high = v1._range(len(profile),cfg)
    if np.std(profile-np.polyval(np.polyfit(x,profile,1),x))<cfg["min_modulation"]:
        raise v1.Abstain("weak_target_channel_modulation")
    seeds = sorted(set(v1._seeds([profile],cfg)[0]+proposed))
    results,frequencies = [],[]
    for seed in seeds:
        if not low<seed<high:
            continue
        f = _refine(profile,x,seed,(max(low,seed-1/len(profile)),min(high,seed+1/len(profile))),[1,2,3],cfg)
        if f<=low*1.001 or f>=high*.999 or any(abs(f-prior)/f<.005 for prior in frequencies):
            continue
        frequencies.append(f)
        results.append(_candidate(profile,x,f,cfg))
    if not results:
        raise v1.Abstain("no_interior_periodic_proposal")
    accepted = sorted([item for item in results if item[0]["accepted"]],key=lambda item:item[0]["score"],reverse=True)
    chosen = (accepted or sorted(results,key=lambda item:item[0]["diagnostic_score"],reverse=True))[0]
    alternate = next((item for item in accepted[1:] if abs(item[0]["frequency_cycles_per_sample"]-chosen[0]["frequency_cycles_per_sample"])/chosen[0]["frequency_cycles_per_sample"]>cfg["frequency_tolerance"]),None)
    if alternate is not None and chosen[0]["score"]<cfg["period_score_ratio"]*alternate[0]["score"]:
        chosen[0]["accepted"] = False
        chosen[0]["reasons"].append("ambiguous_target_fundamental")
    return chosen,[item[0] for item in results]


def _template(cycles,phase,harmonics,cfg):
    checkpoint()
    sampled = np.median(cycles,axis=0)
    smoothed = gaussian_filter1d(sampled,cfg["template_smoothing_sigma_bins"],mode="wrap")
    maximum = max(harmonics)
    waves = v1._basis(phase,1.0,harmonics=maximum)
    columns = [k-1 for k in harmonics]+[k+maximum-1 for k in harmonics]
    coefficients = np.linalg.lstsq(waves[:,columns],smoothed,rcond=None)[0]
    template = waves[:,columns]@coefficients
    template -= template.mean()
    scale = float(np.sqrt(np.mean(template**2)))
    if scale<cfg["min_modulation"]:
        raise v1.Abstain("weak_phase_folded_template")
    return template/scale,sampled,smoothed


def _template_harmonics(cycles,phase,groups,frequency,cfg):
    # Period candidates use 1..3 harmonics; sampled T independently retains
    # reproducible finer shape only when original (not interpolated) sampling permits it.
    checkpoint()
    maximum = min(int(cfg["template_max_harmonics"]),
                  int(np.floor(1/frequency/cfg["min_original_samples_per_harmonic"])),
                  len(phase)//4)
    if maximum<1:
        raise v1.Abstain("insufficient_original_sampling_for_template")
    waves = v1._basis(phase,1.0,harmonics=maximum)
    coefficients = []
    for subset in (cycles,cycles[groups==0],cycles[groups==1]):
        folded = gaussian_filter1d(np.median(subset,axis=0),cfg["template_smoothing_sigma_bins"],mode="wrap")
        coefficients.append(np.linalg.lstsq(waves,folded,rcond=None)[0])
    full,left,right = coefficients
    amplitudes = np.hypot(full[:maximum],full[maximum:])
    cutoff = max(cfg["min_modulation"],cfg["harmonic_relative_threshold"]*float(amplitudes.max()))
    selected,records = [],[]
    for k in range(1,maximum+1):
        ids = [k-1,k+maximum-1]
        a,b = left[ids],right[ids]
        na,nb = float(np.linalg.norm(a)),float(np.linalg.norm(b))
        agreement = float(np.dot(a,b)/max(na*nb,1e-20))
        reasons = []
        if amplitudes[k-1]<cutoff or min(na,nb)<cfg["min_modulation"]:
            reasons.append("weak_phase_folded_harmonic")
        if agreement<cfg["harmonic_split_correlation"]:
            reasons.append("inconsistent_disjoint_cycle_harmonic_phase")
        records.append({"harmonic":k,"supported":not reasons,"reasons":reasons,
                        "amplitude_fraction":float(amplitudes[k-1]),
                        "split_amplitude_fraction":[na,nb],"split_phase_correlation":agreement,
                        "original_samples_per_harmonic":1/(frequency*k)})
        if not reasons:
            selected.append(k)
    if not selected:
        raise v1.Abstain("no_supported_sampled_template_harmonics")
    return selected,records,maximum


def _smoothstep(t):
    checkpoint()
    t = np.clip(t,0,1)
    return t*t*(3-2*t)


def _envelope_at(phase,model):
    checkpoint()
    nodes = np.asarray(model["amplitude_phase_nodes"],float)
    amplitudes = np.asarray(model["amplitude_values"],float)
    indices = np.clip(np.searchsorted(nodes,phase,side="right")-1,0,len(nodes)-2)
    fraction = _smoothstep((phase-nodes[indices])/(nodes[indices+1]-nodes[indices]))
    values = amplitudes[indices]*(1-fraction)+amplitudes[indices+1]*fraction
    before,after = phase<nodes[0],phase>nodes[-1]
    taper = model["edge_taper_cycles"]
    values = np.where(before,amplitudes[0]*_smoothstep((phase-(nodes[0]-taper))/taper),values)
    values = np.where(after,amplitudes[-1]*_smoothstep(((nodes[-1]+taper)-phase)/taper),values)
    return values


def _components_at(model,coordinates):
    checkpoint()
    phase = np.asarray(coordinates,float)*model["frequency_cycles_per_native_pixel"]
    template = np.interp(np.mod(phase,1.0),model["phase_grid"],model["template_values"],period=1.0)
    amplitude = _envelope_at(phase,model)
    return template,amplitude,1+amplitude*template,phase


def _model(profile,coordinates,frequency,axis,native,factor,harmonics,cfg):
    checkpoint()
    length = native[1 if axis=="x" else 0]
    if length>cfg["max_native_axis_pixels"]:
        raise v1.Abstain("native_axis_resource_bound_exceeded")
    native_frequency = frequency/factor
    phase = np.arange(int(cfg["phase_bins"]),dtype=float)/cfg["phase_bins"]
    first = int(np.ceil(coordinates[0]*native_frequency))
    last = int(np.floor(coordinates[-1]*native_frequency-phase[-1]))
    cycles = np.arange(first,last+1,dtype=int)
    if len(cycles)<int(np.ceil(cfg["min_repeats"])):
        raise v1.Abstain("insufficient_complete_cycles_for_template")
    sample_coordinates = (cycles[:,None]+phase[None,:])/native_frequency
    sampled = np.interp(sample_coordinates,coordinates,profile)
    baselines = sampled.mean(axis=1)
    if np.any(~np.isfinite(baselines)) or np.any(baselines<=0):
        raise v1.Abstain("nonpositive_cycle_baseline")
    normalized = sampled/baselines[:,None]-1.0
    groups = cycles%2
    period_harmonics = list(harmonics)
    harmonics,harmonic_evidence,max_harmonic = _template_harmonics(normalized,phase,groups,frequency,cfg)
    split_templates = [_template(normalized[groups==group],phase,harmonics,cfg)[0] for group in (0,1)]
    agreement = v1._correlation(*split_templates)
    if agreement<cfg["template_split_correlation"]:
        raise v1.Abstain("disjoint_cycle_templates_disagree")
    template,raw_template,smoothed_template = _template(normalized,phase,harmonics,cfg)
    projections,heldout,correlations,explained,support = [],[],[],[],[]
    for q,group in zip(normalized,groups):
        other = split_templates[1-group]
        alpha = float(np.dot(q,other)/np.dot(other,other))
        correlation = v1._correlation(q,other)
        fraction = float(1-np.mean((q-alpha*other)**2)/max(np.mean(q**2),1e-20))
        valid = alpha>=cfg["min_modulation"] and correlation>=cfg["cycle_correlation"] and fraction>=cfg["cycle_explained_fraction"]
        projections.append(float(np.dot(q,template)/np.dot(template,template)))
        heldout.append(alpha)
        correlations.append(correlation)
        explained.append(fraction)
        support.append(valid)
    support = np.asarray(support,bool)
    if support.sum()<cfg["min_supported_cycles"] or support.mean()<cfg["min_supported_cycle_fraction"]:
        raise v1.Abstain("insufficient_crossfit_cycle_support")
    projections = np.asarray(projections,float)
    weights = gaussian_filter1d(support.astype(float),cfg["amplitude_sigma_cycles"],mode="nearest")
    amplitudes = gaussian_filter1d(np.maximum(projections,0)*support,cfg["amplitude_sigma_cycles"],mode="nearest")/np.maximum(weights,1e-20)
    cap = min(cfg["amplitude_max"],(1-cfg["gain_min"])/max(-float(template.min()),1e-20),
              (cfg["gain_max"]-1)/max(float(template.max()),1e-20))
    amplitudes = np.where(support,np.clip(amplitudes,0,cap),0)
    phase_native = coordinates*native_frequency
    observed_counts = np.histogram(np.mod(phase_native,1.0),bins=np.linspace(0,1,len(phase)+1))[0]
    model = {"axis":axis,"frequency_cycles_per_native_pixel":native_frequency,
        "period_native_pixels":1/native_frequency,"supported_harmonics":harmonics,
        "period_proposal_supported_harmonics":period_harmonics,
        "template_harmonic_evidence":harmonic_evidence,"template_max_harmonic_for_original_sampling":max_harmonic,
        "phase_grid":phase.tolist(),"template_values":template.tolist(),
        "template_mean":float(template.mean()),"template_rms":float(np.sqrt(np.mean(template**2))),
        "amplitude_phase_nodes":(cycles+.5).tolist(),"amplitude_values":amplitudes.tolist(),
        "amplitude_cap":cap,"edge_taper_cycles":cfg["edge_taper_cycles"],
        "native_shape_yx":list(native),"formula":"G_c(x,y) = 1 + A_c(phi_unwrapped) * T_c(phi_mod_1)",
        "coordinate_contract":"Native pixel centers; phi = native_coordinate / period_native_pixels; T circular linear interpolation; A C1 smoothstep interpolation with zero support taper",
        "template_normalization":"zero mean and unit RMS on the phase grid fixes the A/T scale ambiguity",
        "baseline_policy":"Per-cycle arithmetic mean is a fitting nuisance only; never applied as brightness normalization",
        "sampling":{"profile_native_coordinates":coordinates.tolist(),"raw_intensity_profile":profile.tolist(),
            "phase_grid":phase.tolist(),"complete_cycle_ids":cycles.tolist(),"cycle_sample_native_coordinates":sample_coordinates.tolist(),
            "cycle_baseline_intensity":baselines.tolist(),"cycle_normalized_intensity":normalized.tolist(),
            "raw_folded_median":raw_template.tolist(),"smoothed_folded_median":smoothed_template.tolist(),
            "raw_folded_q10":np.quantile(normalized,.10,axis=0).tolist(),"raw_folded_q90":np.quantile(normalized,.90,axis=0).tolist(),
            "observed_profile_sample_count_by_phase_bin":observed_counts.tolist(),
            "interpolation_note":"Each complete cycle is resampled at 64 phase locations; these interpolated values are not independent observations"},
        "crossfit":{"split":"disjoint even/odd complete cycles; each cycle checked against opposite-group template",
            "split_templates":[t.tolist() for t in split_templates],"split_template_correlation":agreement,
            "cycle_amplitude_projection":projections.tolist(),"heldout_amplitude_projection":heldout,
            "heldout_correlation":correlations,"heldout_explained_fraction":explained,
            "cycle_supported":support.tolist(),"supported_fraction":float(support.mean()),
            "frequency_selection_scope":"Within-acquisition full-profile selection; cycle checks are not independent dataset validation"}}
    _,_,gain,_ = _components_at(model,np.arange(length,dtype=float))
    if not np.isfinite(gain).all() or gain.min()<cfg["gain_min"]-1e-12 or gain.max()>cfg["gain_max"]+1e-12:
        raise v1.Abstain("gain_outside_frozen_bounds")
    model["native_gain_bounds"] = [float(gain.min()),float(gain.max())]
    return model


def fit_templates(raw_ncyx,channel_names,*,native_shape_yx=None,downsample=1,config=None):
    checkpoint()
    cfg = _config(config)
    names = list(channel_names) if isinstance(channel_names,(list,tuple)) else []
    if not 1<=len(names)<=3 or len(set(names))!=len(names) or any(name not in v1.ALLOWED_CHANNELS for name in names):
        raise ValueError("Only unique exact FITC/CY3/DAPI labels are accepted; BF is forbidden")
    raw = np.asarray(raw_ncyx)
    if raw.ndim!=4 or raw.shape[1]!=len(names) or min(raw.shape)<1 or min(raw.shape[-2:])<8 or raw.dtype.kind not in "iuf":
        raise ValueError("Supply a real nonempty N,C,Y,X fluorescence array with Y,X>=8")
    if isinstance(downsample,bool) or int(downsample)!=downsample or downsample<1:
        raise ValueError("downsample must be a positive integer")
    factor = int(downsample)
    native_values = tuple(n*factor for n in raw.shape[-2:]) if native_shape_yx is None else tuple(native_shape_yx)
    if len(native_values)!=2 or any(isinstance(n,bool) or int(n)!=n or n<1 for n in native_values):
        raise ValueError("Native dimensions must be positive integers")
    native = tuple(map(int,native_values))
    if tuple((n+factor-1)//factor for n in native)!=raw.shape[-2:]:
        raise ValueError("Native dimensions differ from the actual mean-bin array")
    if np.any(np.isfinite(raw)&(raw<0)):
        raise ValueError("Negative intensities are unsupported")
    centers = {"y":v1._centers(native[0],factor),"x":v1._centers(native[1],factor)}
    fit_shape = tuple(n//factor for n in native)
    profiles,raw_profiles,records = {},{},[]
    for c,name in enumerate(names):
        a = np.asarray(raw[:,c,:fit_shape[0],:fit_shape[1]],float)
        valid = np.isfinite(a)&(a>0)
        record = {"channel_index":c,"channel_name":name,"status":"abstained","reason":None,
                  "positive_fraction":float(valid.mean()),"axis":None,"model":None}
        if record["positive_fraction"]<cfg["min_valid_fraction"] or min(fit_shape)<8:
            record["reason"] = "insufficient_positive_support_or_complete_bins"
        else:
            med = v1._median(np.where(valid,a,np.nan),axis=(-2,-1),keepdims=True)
            if not np.isfinite(med).all() or np.any(med<=0):
                record["reason"] = "empty_target_frame"
            else:
                image = v1._median(np.where(valid,a,np.nan),axis=0)
                for axis in cfg["axes"]:
                    try:
                        raw_profile = v1._profile(image,axis,cfg)
                        baseline = float(np.median(raw_profile))
                        if not np.isfinite(baseline) or baseline<=0:
                            raise v1.Abstain("nonpositive_profile_baseline")
                        raw_profiles[c,axis] = raw_profile
                        profiles[c,axis] = raw_profile/baseline-1
                    except v1.Abstain:
                        pass
                if not any(key[0]==c for key in profiles):
                    record["reason"] = "insufficient_profile_support"
        records.append(record)
    proposals = {axis:[] for axis in cfg["axes"]}
    for (c,axis),profile in profiles.items():
        try:
            proposals[axis] += v1._seeds([profile],cfg)[0]
        except v1.Abstain:
            pass
    result = {"channel_models": records}
    for c,record in enumerate(records):
        if record["reason"]:
            continue
        candidates,axes = [],[]
        for axis in cfg["axes"]:
            if (c,axis) not in profiles:
                continue
            profile = profiles[c,axis]
            try:
                chosen,details = _search(profile,centers[axis][:len(profile)]/factor,proposals[axis],cfg)
                chosen[0]["axis"] = axis
                candidates.append(chosen)
                axes.append({"axis":axis,"selected":chosen[0],"candidates":details})
            except v1.Abstain as exc:
                axes.append({"axis":axis,"reason":exc.reason})
        record["axis_candidates"] = axes
        if not candidates:
            record["reason"] = "no_supported_target_periodic_proposal"
            continue
        accepted = sorted([item for item in candidates if item[0]["accepted"]],key=lambda item:item[0]["score"],reverse=True)
        proposal,coeff = (accepted or sorted(candidates,key=lambda item:item[0]["diagnostic_score"],reverse=True))[0]
        record["diagnostic_candidate"] = proposal
        if not accepted:
            record["reason"] = "target_evidence_failed:"+",".join(proposal["reasons"])
            continue
        if len(accepted)>1 and proposal["score"]<cfg["axis_score_ratio"]*accepted[1][0]["score"]:
            record["reason"] = "ambiguous_target_direction"
            continue
        axis,f = proposal["axis"],proposal["frequency_cycles_per_sample"]
        try:
            model = _model(raw_profiles[c,axis],centers[axis][:len(raw_profiles[c,axis])],f,axis,native,factor,proposal["supported_harmonics"],cfg)
        except v1.Abstain as exc:
            record["reason"] = exc.reason
            continue
        record.update(status="accepted",reason=None,axis=axis,model=model,applied=names[c]!="DAPI")
    agreement = []
    for a,b in itertools.combinations(records,2):
        if a["status"]==b["status"]=="accepted":
            pa,pb = a["model"]["period_native_pixels"],b["model"]["period_native_pixels"]
            agreement.append({"channels":[a["channel_name"],b["channel_name"]],"same_axis":a["axis"]==b["axis"],
                              "relative_period_difference":abs(pa-pb)/min(pa,pb),
                              "period_agrees":a["axis"]==b["axis"] and abs(pa-pb)/min(pa,pb)<=cfg["frequency_tolerance"]})
    result["diagnostics"] = {"schema":SCHEMA,"status":"accepted" if any(r["status"]=="accepted" for r in records) else "abstained",
        "uses_bf":False,"channel_names":names,"input_shape_ncyx":list(raw.shape),"native_shape_yx":list(native),
        "downsample":factor,"parameters":cfg,"channels":records,"shared_period_context":agreement,
        "shared_period_policy":"Frequency proposals only; direction, phase, waveform, strength and acceptance belong to each target channel",
        "DAPI_application":"exact_identity","short_final_bins_excluded_from_fit_yx":[n%factor!=0 for n in native],
        "higher_harmonic_policy":"Period search uses 1..3 harmonics; sampled T independently preserves disjoint-cycle-supported harmonics up to 8, with at least 4 original profile samples per harmonic cycle",
        "application":"raw / gains_cyx; gains are exactly 1 + A*T; unsupported channels and DAPI remain unchanged",
        "amplitude_policy":"Positive, cycle-dependent projections onto a zero-mean unit-RMS own-channel sampled template; unsupported cycle nodes are zero",
        "limitations":list(LIMITATIONS)}
    return result

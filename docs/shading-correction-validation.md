# Shading Correction: method and validation limits

Version 2.2 evaluates FITC, CY3 and BF separately using the same scan's BF channel and recorded camera geometry. DAPI remains unchanged. When the original H fit cannot find enough complete repeated fields in its fixed validation regions, an adaptive fallback fits a periodic template with an amplitude that varies smoothly across the scan. Each target channel must pass its own evidence checks before it is corrected.

Visible shading does not ensure that a scan contains enough usable repeated fields in the required validation regions. At least four supported fields per spatial group are required. If no target channel passes fitting and validation, no replacement pyramid is prepared or published. The viewer retains an explanation until it is dismissed. The unchanged-pyramid notice is limited to a confirmed unsuccessful attempt with the original cache generation; a lost connection alone does not establish that outcome.

## Adaptive model checks

The acceptance criteria used for evaluation are read from the configuration and copied into the fit record. The adaptive fitter receives the same working-memory budget as the original fitter. This budget bounds estimated fitting allocations; it is not a measurement or limit on the whole application's resident memory, which also includes the ND2 decoder and pyramid generation.

The original H fitter now budgets each allocation phase, including coordinates and observations retained while aligned training samples and candidate-scoring arrays are created. Coarse samples are released once their templates and evidence have been extracted. Sampling uses at most 32 cells per chunk and reduces that number when the budget permits a smaller chunk; a fit is refused if its retained arrays or scoring phase cannot fit. The selected chunk size and phase estimates are recorded with the fit. The former 289 MiB example counted only part of this workspace and is not a supported peak-memory claim.

Adaptive models are accepted only in the known `H_adaptive_hybrid_subcell/1` format. Their nonnegative amplitude envelope and interpolated template provide conservative gain bounds over the complete stored domain, including its boundary and exterior. Enabled adaptive channels must remain within 0.25–1.75 without clipping. Unknown formats, malformed envelopes and unbounded gains are rejected. Legacy `H_auto_refit/1` archives retain their original lower bound of 0.25 and do not acquire a new upper bound.

Before channel admission, a spatial prediction check limits each adaptive envelope. It withholds each supported repeat cell and its immediate neighbours, predicts the centre cell's fitting strips A and B from the remaining field, and compares that prediction with the observed amplitude. The smallest supported observed/predicted ratio can only reduce the channel's correction. Strip C is reserved for admission. A channel with insufficient calibration evidence or invalid gain bounds remains unchanged while the other channels are assessed independently.

This empirical bound does not guarantee improvement at every unseen pixel. Synthetic tests cover three-channel save/reopen/Revoke, exact original-value and DAPI preservation, malformed models, working-memory admission, and cancellation/publication behaviour. These engineering checks are distinct from the native-image results below.

## Native validation of the 2.2 candidate

The final candidate was evaluated on one four-channel acquisition using 19 exposed development/regression regions and eight additional regions selected from RAW geometry before their corrected results were examined. Earlier candidates and their failures are retained: BF-only attenuation exposed a fluorescence reversal, and single-cell prediction calibration did not fully resolve it. Those exposed regions were used only as development evidence for the final spatial prediction check.

Across the final eight regions, median absolute trough-depth suppression was 58.5% for FITC, 34.6% for CY3 and 36.8% for BF. No channel exceeded the predeclared dark-to-bright reversal threshold. The 19 earlier regions also passed that threshold. These percentages describe those selected regions, not whole-slide or between-acquisition performance.

Additional fits excluded each test region from both geometry and fitting inputs. Nineteen such fits remained supported and passed the native reversal checks; two lost reproducible BF geometry and correctly refused fitting. Across 46 assessed region/fit combinations (138 channel comparisons), DAPI and unsupported zeros were unchanged, nested crops agreed exactly, and gains stayed within 0.25–1.75. No polarity change occurred among 7,438 RAW-qualified BF object comparisons; these include repeated objects across fit scenarios and are not independent samples.

A preserved second acquisition exercised the original H fitter with all three channels enabled, with FITC/CY3 gains exactly equal to its previous two-channel fit. This is a regression check using preserved mean arrays, not a fresh decoder or independent-acquisition validation of the adaptive method. The native results above do not establish biological preservation or quantitative assay validity. Real-file application and delivered-package checks are recorded separately with each build.

## Historical experiments excluded from 2.1.15

A four-channel development acquisition (30,415 × 19,146 native pixels) was rejected by the original method because its fixed row-validation region contained too few usable repeated fields. The following earlier extensions were evaluated to determine whether other fields could support a correction. None was included in 2.1.15; this table is historical evidence, not validation of the 2.2 adaptive model.

| Experimental approach | Evidence and decision |
|:--|:--|
| Global vertical profile with complete-field validation | Reduced band depth in one native region, but a second reference region showed dark-to-bright band reversal in BF, FITC and CY3. Rejected for local overcorrection. |
| Spatial amplitude predicted from training fields | Training-only prediction did not bound the weaker shading in held-out fields reliably. Not adopted. |
| Local constant amplitude with separate fitting, admission and evaluation blocks | Too few fields passed column validation and the required held-out reduction was not reached. Rejected. |
| Local linear amplitude gradient with separate interior admission and evaluation blocks | Only one column-validation field passed per channel, below the four-field requirement. Its held-out amplitude reduction was 15.6% for FITC, 14.9% for CY3 and 15.4% for BF, below the frozen 20% requirement. All channels were disabled. Rejected. |

The final percentages each describe one accepted field, not whole-slide performance. The experiments used internal spatial partitions of development data, not independent acquisitions. No threshold was relaxed to admit a candidate, and no experimental model or corrected pyramid was installed. The extensions' code, frozen protocols and field-level evidence are retained separately from the application.

## Interpretation and storage

The README comparison is a selected development example of the original H method. It illustrates a result on that acquisition; it does not establish improvement on other scans. Visual preference and local contrast checks do not establish biological preservation or quantitative assay validity.

An accepted correction replaces the viewing pyramid and persists its model, source identity, settings and selection record. Revoke rebuilds the original pyramid from the ND2. Native ND2/TIFF exports and pixel readouts retain original values. The original ND2, annotations and saved LUTs remain unchanged.

# Shading Correction: method and validation limits

## Periodic fluorescence correction in 2.4.1

The viewer applies the fluorescence-only consensus procedure from the Shading Correction confirmation. It estimates repeating illumination from the current slide's fluorescence. FITC and CY3 are the only channels eligible for correction. DAPI may contribute frequency proposals and diagnostics, but its applied gain remains exactly one. BF and all other channels also remain unchanged. No BF pixels, BF-derived masks or geometry, camera priors, or model from another slide enter this fit.

The fitter uses actual means of 32 × 32 native-pixel bins. Short final bins are excluded from fitting. Each eligible channel must support its own repeating template before a spatial amplitude field is fitted. The native gain is `G = 1 + A(x,y) × T(phase)`, and corrected display values are calculated as `raw / G` using float64 division before float32 storage. Intensity baselines are fitting terms only; the method does not normalize overall brightness or a time series. Channels without sufficient evidence retain identity gain. If none is corrected, the existing pyramid is retained.

The saved model records its channel mapping, native dimensions, template, amplitude field, configuration and fit decisions in the `BF_free_consensus/1` format. Existing archives with the earlier name remain readable through the compatibility loader. On loading, the implementation checks these fields and rejects malformed, incompatible or oversized archives. The fitting admission budget is an estimate of working allocations, currently 1 GiB; it is not a limit on the entire application's memory use, which also includes decoding and pyramid construction.

Correction requires a supported stitched, single-frame ND2 slide with the original source available and at least one FITC or CY3 channel. Plate/time-series acquisitions, Z projections, RGB data and other source formats are not supported for this correction. Existing H corrections remain readable and can be revoked. New applications use the fluorescence-only method; an unsupported fit does not switch to H.

Source tests cover equivalence to the frozen reference procedure, channel decisions, native gain interpolation, subregion consistency, float64 division, exact identity for BF/DAPI, model reopen, malformed archives, memory admission and cancellation. Transaction tests cover cache publication, reopening, Revoke and original-value export paths. These are engineering checks. Within-acquisition periodic consistency cannot establish whether a repeating structure is illumination or specimen signal, and it does not establish biological preservation or quantitative assay validity. Inspect the corrected image before using rendered exports.

The numerical results in the following H sections describe the older BF-guided method; they are not performance evidence for the current fluorescence-only method. Packaged platform checks are recorded with the release artifacts.

## BF-guided (H): legacy corrections and historical evidence

Version 2.2.1 evaluates FITC, CY3 and BF separately using the same scan's BF channel and recorded camera geometry. DAPI remains unchanged. When the original H fit cannot find enough complete repeated fields in its fixed validation regions, an adaptive fallback fits a periodic template with an amplitude that varies smoothly across the scan. Each target channel must pass its own evidence checks before it is corrected.

Visible shading does not ensure that a scan contains enough usable repeated fields in the required validation regions. At least four supported fields per spatial group are required. If no target channel passes fitting and validation, no replacement pyramid is prepared or published. The viewer retains an explanation until it is dismissed. The unchanged-pyramid notice is limited to a confirmed unsuccessful attempt with the original cache generation; a lost connection alone does not establish that outcome.

### H adaptive model checks

The acceptance criteria used for evaluation are read from the configuration and copied into the fit record. The adaptive fitter receives the same working-memory budget as the original fitter. This budget bounds estimated fitting allocations; it is not a measurement or limit on the whole application's resident memory, which also includes the ND2 decoder and pyramid generation.

The original H fitter now budgets each allocation phase, including coordinates and observations retained while aligned training samples and candidate-scoring arrays are created. Coarse samples are released once their templates and evidence have been extracted. Sampling uses at most 32 cells per chunk and reduces that number when the budget permits a smaller chunk; a fit is refused if its retained arrays or scoring phase cannot fit. The selected chunk size and phase estimates are recorded with the fit. The former 289 MiB example counted only part of this workspace and is not a supported peak-memory claim.

Adaptive models are accepted only in the known `H_adaptive_hybrid_subcell/1` format. Their nonnegative amplitude envelope and interpolated template provide conservative gain bounds over the complete stored domain, including its boundary and exterior. Enabled adaptive channels must remain within 0.25–1.75 without clipping. Unknown formats, malformed envelopes and unbounded gains are rejected. Legacy `H_auto_refit/1` archives retain their original lower bound of 0.25 and do not acquire a new upper bound.

Before channel admission, a spatial prediction check establishes a conservative fallback envelope. It withholds each supported repeat cell and its immediate neighbours, predicts the centre cell's fitting strips A and B from the remaining field, and compares that prediction with the observed amplitude. The smallest supported observed/predicted ratio reduces the fallback correction. Version 2.2.0 applied that reduction throughout the scan, leaving substantial visible bands in strongly shaded regions.

Version 2.2.1 restores correction strength locally where the channel's own A/B profiles support it. Each fitting strip is also divided into two halves so a broad median cannot hide a local reduction in shading. The training template fixes the trough and flank positions. A multiplicative depth calculation caps every envelope node affecting those measurements, reserving one robust scale of measured template mismatch. This is an empirical allowance, not a statistical confidence interval. Smoothing cannot lift the resulting caps. Nodes whose interpolation footprint includes missing input retain the conservative fallback. Finite input availability may be inspected throughout the cell; Strip C intensity and derived measurements remain reserved for admission.

A channel with insufficient calibration evidence or invalid gain bounds remains unchanged while the other channels are assessed independently. The configured admission criteria and the stored model format are unchanged.

This empirical bound does not guarantee improvement at every unseen pixel. Synthetic tests cover three-channel save/reopen/Revoke, exact original-value and DAPI preservation, malformed models, working-memory admission, and cancellation/publication behaviour. These engineering checks are distinct from the native-image results below.

### Native validation of the H 2.2.1 refinement

The refinement addresses the visible residual bands in the same development acquisition. Development used the 27 previously exposed regions. In regions with a RAW-identifiable trough and complete flanks, median absolute depth suppression increased from 57.5% to 76.9% for FITC (16 regions), 37.7% to 70.7% for CY3 (18), and 36.7% to 78.2% for BF (16). No full-band region exceeded the unchanged dark-to-bright reversal threshold. These are development results, not independent validation.

Four further native regions were selected and frozen from RAW data before their corrected outputs were inspected. They do not overlap the previous regions or the template-training rectangle. With the final method fixed, all four regions passed each channel's reversal checks on both background and all-acquired profiles. Median background trough-depth suppression was:

| Channel | 2.2.0 | 2.2.1 |
|:--|--:|--:|
| FITC | 52.0% | 61.8% |
| CY3 | 33.3% | 52.5% |
| BF | 34.7% | 70.6% |

This is prospective selection of locations within one acquisition. The adaptive fitter can use A/B observations at these locations, so it is not validation on fully independent fitting data. In three difficult previously exposed tests with regions removed from the inputs, the refinement's native gains equal the prior conservative model exactly throughout the missing region; the earlier passing native checks therefore remain applicable there.

The narrow `dim_FITC` and `full_band` crops retain signed-depth flags when their truncated flanks are treated as a complete band. Their full-band contexts pass the unchanged reversal check: the existing `extended_full_band` region and a new 3,405-pixel-wide context using the original `dim_FITC` vertical rows. The latter gives 75.5%, 73.8% and 73.1% suppression for FITC, CY3 and BF. The cropped diagnostics remain in the evidence; expanding the context does not change the corrected pixels.

Across the 27 earlier regions, the four new regions and the expanded context, no polarity change occurred in 5,301 RAW-qualified BF object comparisons. DAPI, unsupported zeros and nested crop values remain exact. Some regions still have weak reduction, and one CY3 background profile has a larger residual depth than RAW. Whole-scan comparisons use identical saved LUTs; neither those images nor the selected-region percentages establish uniform improvement or biological preservation. Real-file application and package checks accompany the delivered build.

### Native validation of the H 2.2.0 candidate

The final candidate was evaluated on one four-channel acquisition using 19 exposed development/regression regions and eight additional regions selected from RAW geometry before their corrected results were examined. Earlier candidates and their failures are retained: BF-only attenuation exposed a fluorescence reversal, and single-cell prediction calibration did not fully resolve it. Those exposed regions were used only as development evidence for the final spatial prediction check.

Across the final eight regions, median absolute trough-depth suppression was 58.5% for FITC, 34.6% for CY3 and 36.8% for BF. No channel exceeded the predeclared dark-to-bright reversal threshold. The 19 earlier regions also passed that threshold. These percentages describe those selected regions, not whole-slide or between-acquisition performance.

Additional fits excluded each test region from both geometry and fitting inputs. Nineteen such fits remained supported and passed the native reversal checks; two lost reproducible BF geometry and correctly refused fitting. Across 46 assessed region/fit combinations (138 channel comparisons), DAPI and unsupported zeros were unchanged, nested crops agreed exactly, and gains stayed within 0.25–1.75. No polarity change occurred among 7,438 RAW-qualified BF object comparisons; these include repeated objects across fit scenarios and are not independent samples.

A preserved second acquisition exercised the original H fitter with all three channels enabled, with FITC/CY3 gains exactly equal to its previous two-channel fit. This is a regression check using preserved mean arrays, not a fresh decoder or independent-acquisition validation of the adaptive method. The native results above do not establish biological preservation or quantitative assay validity. Real-file application and delivered-package checks are recorded separately with each build.

### Historical H experiments excluded from 2.1.15

A four-channel development acquisition (30,415 × 19,146 native pixels) was rejected by the original method because its fixed row-validation region contained too few usable repeated fields. The following earlier extensions were evaluated to determine whether other fields could support a correction. None was included in 2.1.15; this table is historical evidence, not validation of the 2.2 adaptive model.

| Experimental approach | Evidence and decision |
|:--|:--|
| Global vertical profile with complete-field validation | Reduced band depth in one native region, but a second reference region showed dark-to-bright band reversal in BF, FITC and CY3. Rejected for local overcorrection. |
| Spatial amplitude predicted from training fields | Training-only prediction did not bound the weaker shading in held-out fields reliably. Not adopted. |
| Local constant amplitude with separate fitting, admission and evaluation blocks | Too few fields passed column validation and the required held-out reduction was not reached. Rejected. |
| Local linear amplitude gradient with separate interior admission and evaluation blocks | Only one column-validation field passed per channel, below the four-field requirement. Its held-out amplitude reduction was 15.6% for FITC, 14.9% for CY3 and 15.4% for BF, below the frozen 20% requirement. All channels were disabled. Rejected. |

The final percentages each describe one accepted field, not whole-slide performance. The experiments used internal spatial partitions of development data, not independent acquisitions. No threshold was relaxed to admit a candidate, and no experimental model or corrected pyramid was installed. The extensions' code, frozen protocols and field-level evidence are retained separately from the application.

## Interpretation and storage

The historical H examples describe selected development acquisitions; they do not establish performance of the current fluorescence-only method or improvement on other scans. Visual preference and local contrast checks do not establish biological preservation or quantitative assay validity.

An accepted correction replaces the viewing pyramid and persists its model, source identity, settings and selection record. Reopening uses that saved correction. Revoke rebuilds the original pyramid from the ND2. PNG/JPEG/SVG exports render the corrected display and use a `shading-corrected` filename suffix; native ND2/TIFF exports, including reduced-resolution exports, and pixel readouts retain original values. The original ND2, annotations and saved LUTs remain unchanged. Cancellation or failure before publication leaves the current committed cache intact.

Updating the application retains existing saved corrections. The loader still reads earlier H formats and the BF-free preview format `BF_free_periodic_x1/1`; that preview fitter is not the 2.4.0 default. New consensus caches require this release or later. Revoke and Apply are required to refit an already corrected cache with the current method.

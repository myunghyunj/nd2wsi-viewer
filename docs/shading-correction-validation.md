# Shading correction

Shading correction reduces the repeating dark bands that can make a stitched
microscopy image look like a grid. It learns a recurring illumination pattern
from the slide's fluorescence and adjusts the strength of that pattern across
the image. It is intended for retrospective images with this acquisition problem.

In the development scans, the grid was associated with a loosely fitted
microscope aperture. The algorithm estimates the image pattern; it does not
diagnose the optical cause of shading in a new scan.

## How it works

A single fixed correction can be too strong in one region and too weak in
another. The viewer therefore estimates two things separately: **the repeating
shape of the shading** and **how strongly it appears at each location**.

1. **Find the pattern.** Actual means of 32 × 32 native-pixel bins provide a
   compact view of the fluorescence. The fitter searches for supported repeating
   spacing and estimates an aligned template. Each target channel must support
   its own template; short bins at the image edge are excluded from fitting.
2. **Estimate its local strength.** With the template and spacing fixed, local
   observations are fitted jointly. The amplitude is the product of two smooth,
   nonnegative factors, describing variation across the image and across
   successive repeats. Confidence weights and robust fitting reduce the influence
   of inconsistent observations. Unsupported source regions are not filled in.
3. **Apply an illumination map.** At native resolution, the map is
   `G(x, y) = 1 + A(x, y) × T(phase)`. The corrected value is `raw / G`.
   The template can describe both darker and brighter parts of a repeat, while
   the amplitude changes smoothly with position. Bounds keep the gain positive.

This is a spatial correction. It does not equalise the slide's overall brightness
or normalise a time series. Intensity baselines help fit the pattern but are not
applied as a separate brightness adjustment.

## Where it applies

- Supported stitched, single-frame **ND2** scans with the original source
  available and at least one **FITC** or **CY3** channel.
- FITC and CY3 are assessed separately. A channel without enough evidence keeps
  its original values. If neither channel is supported, the existing pyramid
  is retained.
- **BF and DAPI remain unchanged.** BF pixels, BF masks, camera-geometry priors
  and models from other slides do not enter the fit. DAPI can help propose
  repeating frequencies, but its applied gain is exactly one.
- Plate/time-series acquisitions, Z projections, RGB images and other file
  formats are outside the current correction scope.

## What the development checks show

The README images of `7.nd2` and `7-1.nd2` are separate illustrative applications
with a new fit for each source and identical display settings within each pair.
They are not included in the four-acquisition results below.

The following results describe four previously inspected acquisitions. They
are development evidence, not independent biological validation. Repeated
regions, channels and pixels are not independent samples.

### Selected native regions — 22 October 2026

Five native strips per acquisition were evaluated in FITC and CY3, giving
**40 region–channel pairs**. Mean power in the RAW-identified periodic band fell
by **39.70%**; none of the 40 pairs crossed the specified reversal threshold.
Mean gradient correlation was **0.9941**, and the mean corrected-to-RAW signal
sum ratio was **1.00043**.

The method nevertheless **did not meet all of that benchmark's selection
criteria**: the mean new-band relative power was **0.10071**, above the
predeclared limit of **0.03905**. Reduced original bands and preserved global
summaries do not rule out newly introduced local structure.

### Whole-slide native application — 23 October 2026

The same saved models were applied across the same four acquisitions without
refitting. The check covered **6,631,195,254 FITC/CY3 pixel values** and
**3,315,597,627 DAPI pixel values**. No DAPI values changed.

| Descriptive measure | Result |
| --- | ---: |
| Corrected / RAW signal sum | 0.999955 |
| Gradient correlation with RAW | 0.999564 |
| Remaining periodic-band power, whole-image x profiles | 0.5196 of RAW |
| Remaining periodic-band power, local x profiles | 0.9233 of RAW |

These summaries first average assessed FITC/CY3 channel results within each
acquisition, then weight the four acquisitions equally. They are not pooled
pixel-level estimates. The difference between whole-image and local profiles
shows why a cleaner overview does not imply equal improvement everywhere.

This was a more complete spatial check of existing data, **not a new independent
test set**. It reported descriptive magnitudes and coverage and did not reapply
the earlier automatic selection gate. It therefore does not overturn the
criterion failure reported above.

## Source values, saved corrections and limits

The original ND2 is not rewritten. The fitted model and corrected viewing
pyramid are saved locally. **Revoke** restores uncorrected viewing, and native
source-data exports retain the original values. Rendered images use the
currently displayed correction and LUTs.

Engineering checks cover the frozen calculation, native interpolation and crop
consistency, exact BF/DAPI identity, save/reopen, malformed models, cancellation,
cache replacement, Revoke and original-value export. The fitting admission
budget estimates working allocations; it is not a limit on the whole app's
memory use.

There is no independent illumination ground truth in these development scans.
A repeating specimen structure can resemble an illumination pattern, and weak
or locally changing shading can remain. Global signal sums and correlations do
not establish preservation of every cell, quantitative assay validity or
biological equivalence. Review local detail as well as the overview before using
corrected rendered images for interpretation.

The implementation is documented in the
[model calculation](../nd2wsi/bffree_consensus.py),
[amplitude fitting](../nd2wsi/bffree_consensus_amplitude.py) and
[channel policy](../nd2wsi/bffree_model.py).

# nd2wsi-viewer 2.2.1

Shading Correction can leave strong vertical bands when a weak region limits the correction across an entire scan. The adaptive method now restores strength from local FITC, CY3 and BF measurements. Local trough-depth limits account for variation within fitting strips and differences from the common shading template. Missing-data regions retain the conservative fallback.

DAPI, original pixel readouts, native ND2/TIFF exports, source files, annotations and saved LUTs retain the existing preservation contract. The model format, gain limits and channel admission criteria are unchanged.

Existing saved corrections remain in place after updating. Use **Revoke**, then **Apply**, to fit them again with this version.

In four new RAW-selected regions of the development acquisition, median band-depth suppression increased from 52.0% to 61.8% for FITC, 33.3% to 52.5% for CY3, and 34.7% to 70.6% for BF, without exceeding the existing reversal criterion. Identical-LUT whole-scan comparisons accompany the build.

Validation results and remaining limitations are described in the [method notes](shading-correction-validation.md). Improvement is not uniform across the scan, and spatial checks within one acquisition do not establish biological preservation or generality across independent acquisitions.

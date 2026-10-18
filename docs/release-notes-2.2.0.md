# nd2wsi-viewer 2.2.0

Shading Correction evaluates FITC, CY3 and BF, while preserving DAPI, native ND2/TIFF exports and original pixel readouts. When the original fit lacks enough repeated fields in its fixed validation regions, an adaptive fallback estimates how shading amplitude changes across the scan. Channels without sufficient evidence remain unchanged.

The adaptive fitter uses the configured acceptance criteria and working-memory budget, including arrays retained during aligned sampling and scoring. A spatial prediction check reduces correction strength when surrounding fields overpredict local shading. Saved models validate their format, channel mapping, amplitude envelope and gain bounds across the complete stored domain. Invalid models are rejected without clipping. Three corrected channels can be saved and reopened; Revoke rebuilds the original pyramid from the source ND2.

Caches containing adaptive or BF correction require 2.2.0 or later. Existing 2.1.15 corrected caches remain readable. The original ND2, annotations and saved LUTs are not modified.

Native validation covered 27 regions of one acquisition, including eight regions withheld from development of the final candidate. All three channels passed the predeclared reversal check; selected-region median suppression on the final eight was 58.5% FITC, 34.6% CY3 and 36.8% BF. Spatial holdout tests and exact DAPI/crop checks also passed where fitting remained supported. This does not establish improvement across independent acquisitions or biological preservation. See the [method and validation limits](shading-correction-validation.md); real-file application and platform-package checks accompany the built artifacts.

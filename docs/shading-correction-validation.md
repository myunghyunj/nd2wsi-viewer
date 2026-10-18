# Shading Correction: method and validation limits

The current method estimates repeating illumination from the same scan's BF channel and recorded camera geometry, then evaluates FITC and CY3 separately. DAPI and BF remain unchanged. The original H model, settings and acceptance criteria are unchanged by the unsuccessful-result notice update.

Visible shading does not ensure that a scan contains enough usable repeated fields in the required validation regions. At least four supported fields per spatial group are required. If fitting or validation fails, no replacement pyramid is prepared or published. The viewer retains an explanation until it is dismissed. The unchanged-pyramid notice is limited to a confirmed unsuccessful attempt with the original cache generation; a lost connection alone does not establish that outcome.

## Experimental extensions not included

A four-channel development acquisition (30,415 × 19,146 native pixels) was rejected by the original method because its fixed row-validation region contained too few usable repeated fields. We evaluated extensions to determine whether other fields could support a correction. None was accepted for release.

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

# Milestone 1 Validation Protocol & Scale Acceptance Criteria

[Back to the README](../README.md) · [Development](development.md) · [Architecture](architecture.md) · [Shading correction validation](shading-correction-validation.md)

This document defines the quantitative engineering gates, multi-gigabyte benchmark protocol, 5-external-lab observational validation study, and methodological boundaries for **Milestone 1**:

> A new external user can install `nd2wsi-viewer`, open their own lab's acquisition, inspect channels and scenes, export a native-value ROI, and reopen that ROI with pixel calibration, channel metadata, and annotation sidecars intact.

---

## 1. Milestone 1 Acceptance Criteria & Measured Scale Results

### 1.1 Release Gate Thresholds

Before distributing an external validation build to partner laboratories, the release candidate must pass four engineering gates:

| Gate | Requirement | Verification Command / Artifact |
| :--- | :--- | :--- |
| **Core Round-Trip Parity** | Native open → scene selection → native OME-TIFF ROI export → reopen `.ome.tif` → save and reload plane-scoped annotation sidecar across `.nd2`, `.czi`, `.lif`, and `.ome.tif`. | `uv run python scripts/benchmark_milestone.py --suite standard` |
| **Multi-GB 4-Tab Memory Ceiling** | **4-concurrent-slide Compare Peak RSS `<= 1,228.8 MiB` (`1.2 GiB`)**, zero OOMs, zero HTTP socket errors, and `0` leaked Z-prefetch worker threads across `> 4.5 GiB` combined raw pixel volume. | `uv run python scripts/benchmark_milestone.py --suite scale --max-4tab-rss-mib 1228.8` |
| **Untiled Large-TIFF Streaming** | Untiled 2D TIFFs exceeding `UNTILED_EAGER_MAX_BYTES` (`64 MiB`) stream bands lazily on demand (`_LazyUntiledLevel`) without materializing full uncompressed planes in RAM. | Verified at both `72.0 MiB` (`standard`) and `1,536.0 MiB` / `1.50 GiB` (`scale`) |
| **OS Installer Trust** | Developer ID signature + Apple `notarytool` / `stapler` ticket (`spctl -a -vvv -t exec` on `.app` and `spctl -a -vvv -t open --context context:primary-signature` on `.dmg`) on macOS; NSIS installer verification on Windows without manual terminal workarounds. | Clean-machine installation check in Step 1 of the [5-lab study](#2-5-external-lab-observational-study-protocol) |

### 1.2 Standard Multi-Vendor Suite (`--suite standard`, Commit `c9eb0c3`)

Measured on Apple M4 Pro (`24 GiB` RAM, macOS arm64, Python `3.13.14`) using [benchmark_milestone.py](../scripts/benchmark_milestone.py), where each scenario runs in a fresh child Python process against isolated temporary copies:

| Workload / Acquisition | Cold Open (`med` / `p95`) | Warm Open (`med` / `p95`) | 16-Tile Initial (`med` / `p95`) | 16-Tile LUT Drag (`med` / `p95`) | Round-Trip (`med`) | Clean Peak RSS (`med` / `max`) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `example_cell.nd2` (`28.1 MiB`, 3-ch `uint16`) | `128.3` / `1084.4 ms` | `28.0` / `32.4 ms` | `10.14` / `20.11 ms` | `10.30` / `19.77 ms` | `174.8 ms` | — |
| `example_tissue.nd2` (`47.5 MiB`, 2-ch `uint16`) | `150.3` / `1099.4 ms` | `27.0` / `40.3 ms` | `9.00` / `17.96 ms` | `8.30` / `17.35 ms` | — | `346.4` / `354.1 MiB` |
| `sample_2d.czi` (`19.1 MiB` Zstd, `2048²` 3-ch) | `197.4` / `1146.6 ms` | `13.8` / `21.0 ms` | `19.81` / `25.78 ms` | `13.37` / `21.10 ms` | `287.4 ms` | — |
| `sample_multiseries.lif` (`33.0 MiB`, 2 scenes) | `159.5` / `1109.3 ms` | `15.5` / `22.1 ms` | `17.42` / `25.83 ms` | `12.49` / `21.61 ms` | `239.2 ms` | — |
| `sample_2d.ome.tif` (`23.4 MiB` tiled, 2 scenes) | `93.5` / `425.6 ms` | `100.4` / `108.5 ms` | `13.23` / `16.89 ms` | `10.98` / `15.10 ms` | `204.5 ms` | — |
| `sample_large_untiled.ome.tif` (`72.0 MiB` raw) | `47.0` / `372.0 ms` | `58.2` / `66.5 ms` | `24.53` / `27.14 ms` | `12.58` / `18.42 ms` | — | `328.2` / `330.8 MiB` |
| **Z-stacks (`Z=22` `.czi` / `.lif`)** | `z+1`: `6.15` / `6.87 ms` med | `p95`: `10.33` / `8.06 ms` | 20-step scrub (`3 ms`/step): | **`3` planes decoded** (`0` leaked threads) | — | `237.4 MiB` (`.czi` Z) |
| **4-Slide Standard Compare** (`4` tabs + `32`-tile bursts) | — | — | — | — | — | **`649.4` / `655.1 MiB`** |

### 1.3 Multi-GB Scale Stress Suite (`--suite scale`, Commit `c9eb0c3`)

Measured with `uv run python scripts/benchmark_milestone.py --suite scale --max-4tab-rss-mib 1228.8` (`63.81 s` total wall time, `100%` temporary file cleanup verified). Four synthetic multi-hundred-MB to multi-GB acquisitions (`4,704.0 MiB` = **`4.59 GiB` combined raw pixels**) were synthesized and exercised in isolated subprocesses:

| Scale Fixture | Dimensions & Layout | Raw / Disk Size | Cold Open (`med` / `p95`) | Warm Open (`med` / `p95`) | 16-Tile Initial (`med` / `p95`) | 16-Tile LUT Drag (`med` / `p95`) | Clean Single-Slide Peak RSS (`med` / `max`) |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `scale_pyr_1536mib.ome.tif` | `16384 × 16384`, 3-ch `uint16`, 5-level tiled BigTIFF | `1,536.0 MiB` (`1.50 GiB`) / `1,658.1 MiB` | `44.26` / `603.45 ms` | `37.01` / `40.03 ms` | `16.33` / `26.51 ms` | `12.19` / `25.17 ms` | **`305.4` / `309.7 MiB`** |
| `scale_untiled_1536mib.ome.tif` | `16384 × 12288`, 4-ch `uint16`, uncompressed strip BigTIFF (`_LazyUntiledLevel`) | `1,536.0 MiB` (`1.50 GiB`) / `1,536.0 MiB` | `896.47` / `1,029.84 ms` | `700.71` / `728.85 ms` | `41.63` / `42.38 ms` | `15.06` / `23.00 ms` | **`395.3` / `413.8 MiB`** |
| `scale_mosaic_864mib.czi` | `12288 × 12288`, 3-ch `uint16`, `6×6` (`36`-tile) Zstd0 mosaic (`.nd2svs` pyramid) | `864.0 MiB` / `693.9 MiB` | `5,131.58` / `5,864.11 ms` *(builds `.nd2svs`)* | **`8.26` / `9.14 ms`** | `23.55` / `29.69 ms` | **`11.01` / `20.90 ms`** | **`248.5` / `269.0 MiB`** *(warm; cold Dask build: `1,265.1 MiB`)* |
| `scale_zstack_z32.czi` | `2048 × 2048`, 3-ch `uint16`, `Z=32` confocal Z-stack (`PlateSource`) | `768.0 MiB` / `616.8 MiB` | Cold Z jump: `34.07` / `35.80 ms` | `z+1` step: **`6.72` / `9.57 ms`** | 30-step scrub (`z=0→30` @ `3 ms`): | **`3` planes decoded** (`med` & `max`; `0` leaked threads) | **`454.2 MiB`** |

**Multi-GB Round-Trip & 4-Tab RSS Gate Summary (`--suite scale`):**

- **Multi-GB ROI Round-Trip (`open → export 512×512 native OME-TIFF → reopen → sidecar save & reload`):**
  - `1.50 GiB` pyramidal `.ome.tif`: **`105.98 ms` median** (`294.00 ms` p95; ROI export `23.64 ms` med; `151.1 MiB` Peak RSS).
  - `864.0 MiB` mosaic `.czi` (warm `.nd2svs`): **`63.37 ms` median** (`260.01 ms` p95; ROI export `12.06 ms` med; `113.3 MiB` Peak RSS).
- **4-Concurrent-Large-Slide Compare Gate (`4.59 GiB` combined raw open across 4 tabs + 32-tile viewport bursts + live LUT drag + `Z=32` scrub):**
  - **Observed Peak RSS:** **`611.2 MiB` median / `627.1 MiB` p95 / `628.9 MiB` max** (`n = 3` clean subprocesses).
  - **Acceptance Gate (`<= 1,228.8 MiB` / `1.2 GiB`):** **PASS** with **`+599.9 MiB` (`48.8%`) headroom** *(note: one-time initial Dask `.nd2svs` pyramid construction on uncached `864 MiB` CZI mosaics peaks transiently at `1,229.2–1,265.1 MiB` across worker threads before dropping to `< 270 MiB` on steady open)*.

### 1.4 Benchmarking External `5–20 GB` Bench Acquisitions (`--files`)

When external drives containing real `5–20 GB` stitched `.nd2`, `.czi`, `.lif`, `.ome.tif`, or `.svs` bench acquisitions are mounted, run the harness in `--files` mode:

```bash
ND2WSI_BENCH_TMP=/tmp/nd2wsi-ext-bench \
  uv run python scripts/benchmark_milestone.py \
  --files \
    "/Volumes/BenchData/slide_stitched_12gb.nd2" \
    "/Volumes/BenchData/brain_mosaic_8gb.czi" \
    "/Volumes/BenchData/tile_scan_6gb.lif" \
    "/Volumes/BenchData/whole_slide_10gb.ome.tif" \
  --max-4tab-rss-mib 1228.8 \
  --json-out /tmp/milestone1_external_bench.json
```

`--files` stages independent copies/clones inside `ND2WSI_BENCH_TMP`, verifies via pre/post directory snapshots that **zero `nd2wsi/` cache or annotation files are created, modified, or deleted beside the user's real slides**, cleans up all temporary artifacts in `finally`, and exits non-zero (`exit 2`) if 4-tab Compare Peak RSS exceeds `--max-4tab-rss-mib`.

### 1.5 Measured Real-Scanner Acquisitions (`--files`)

Measured on Apple M4 Pro (`24 GiB` RAM, macOS arm64, Python `3.14.6`) across **8 anonymized real scanner acquisitions** (`10.39 GiB` on disk spanning Nikon `.nd2`, Zeiss `.czi`, Leica `.lif`, uncompressed 16-bit `.tif`, Aperio `.svs`, and HEVC `.sdpc`) staged in isolated local NVMe sandboxes with verified pre/post non-mutation of the read-only external acquisition drive (`74.98 s` total wall time):

| Anonymized Acquisition | Modality, Dimensions & Storage | Disk Size (`.nd2svs` Cache) | Cold Open (`med`) | Warm Open (`med` / `p95`) | 16-Tile Initial (`med` / `p95`) | 16-Tile LUT Drag (`med` / `p95`) | Round-Trip (`med`) | Clean Single-Slide Peak RSS |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `Scanner ND2 #1` | `23975 × 22336`, 4-ch `uint16` stitched WSI (`10×`, compact `.nd2svs`) | `3.99 GiB` (`815.5 MiB`) | `7,397.55 ms` *(builds `.nd2svs`)* | `27.53` / `29.52 ms` | `19.84` / `28.12 ms` | `12.92` / `24.55 ms` | `152.19 ms` | **`300.9 MiB`** *(warm; cold build: `5,185.1 MiB`)* |
| `Scanner CZI #1` | `11323 × 11338`, 2-scene `27`-tile `JPEG-XR` RGB mosaic (`10×`, full `.nd2svs`) | `65.6 MiB` (`319.1 MiB`) | `7,056.94 ms` *(builds `.nd2svs`)* | `24.11` / `34.81 ms` | `14.95` / `21.63 ms` | `9.42` / `16.38 ms` | `79.06 ms` | **`153.5 MiB`** *(warm; cold build: `733.6 MiB`)* |
| `Scanner TIFF #1` | `14555 × 16076`, 3-ch `uint16` uncompressed untiled (`_LazyUntiledLevel`) | `1.31 GiB` *(direct, `0 B`)* | `812.46 ms` | `784.59` / `793.02 ms` | `116.78` / `119.53 ms` | **`10.12` / `13.34 ms`** | `870.07 ms` | **`205.6 MiB`** |
| `Scanner CZI #2` | `2048 × 2048`, 4-ch `uint16` `Z=31` confocal stack (`63×`, `PlateSource`) | `992.6 MiB` (`2.2 MiB`) | `141.38 ms` | `26.32` / `30.69 ms` | `19.92` / `26.85 ms` | `16.58` / `24.19 ms` | `113.52 ms` | **`389.7 MiB`** |
| `Scanner LIF #1` | `16`-series mixed `1024 × 1024` 2-ch `Z=9` & `644 × 644` 3-ch 2D confocal (`40×`) | `68.5 MiB` (`0.2 MiB`) | `121.95 ms` | `18.54` / `21.88 ms` | `11.25` / `16.40 ms` | `9.96` / `15.84 ms` | `59.41 ms` | **`199.7 MiB`** |
| `Scanner ND2 #2` | `2048 × 2048`, 5-ch `uint16` `Z=23` confocal stack (`20×`, `PlateSource`) | `920.7 MiB` (`1.4 MiB`) | `165.91 ms` | `46.09` / `48.98 ms` | `23.13` / `25.46 ms` | `21.50` / `25.49 ms` | `143.72 ms` | **`439.9 MiB`** |
| `Scanner SVS #1` | `89999 × 77410` (`6.97 GPx`), 3-ch `uint8` 4-level tiled WSI (`40×`, direct) | `1.43 GiB` *(direct, `0 B`)* | `88.68 ms` | `8.34` / `9.70 ms` | `10.59` / `14.97 ms` | `8.84` / `15.34 ms` | `54.73 ms` | **`179.9 MiB`** |
| `Scanner SDPC #1` | `249600 × 173696` (`43.35 GPx`), 8-level HEVC tiled WSI (`40×`, direct) | `1.66 GiB` *(direct, `0 B`)* | `155.93 ms` | `6.41` / `7.93 ms` | `10.34` / `17.01 ms` | `8.74` / `15.27 ms` | `36.06 ms` | **`195.6 MiB`** |

**Real-Scanner Multi-Series, 3D Z-Stack, 4-Tab RSS Gate & `v2.6.4` (`6f3a2cc`) Baseline Summary:**

- **Multi-Series Scene Switching (`POST /s/<sid>/api/select-scene`):**
  - `Scanner CZI #1` (`2` stitched mosaic scenes): **`10.35 ms` median / `17.46 ms` p95** warm (`7,183.85 ms` initial cold `.nd2svs` build of Scene 1).
  - `Scanner LIF #1` (`16` confocal series, switching across 3D `plate` and 2D `slide` scenes): **`4.99 ms` median / `9.16 ms` p95** warm (`212.20 ms` initial cold build of Series 1).
- **Real 3D Confocal Z-Stack Navigation (`z + 1` Prefetched Step & Rapid `3 ms`/step Z-Scrub):**
  - `Scanner CZI #2` (`Z=31`, `992.6 MiB` 4-ch `uint16`): `z+1` step **`8.37` / `10.85 ms`** (`med` / `p95`); 29-step rapid scrub decoded **`3.0` planes median (`4` max)**, `0` leaked threads (`455.6 MiB` Peak RSS).
  - `Scanner ND2 #2` (`Z=23`, `920.7 MiB` 5-ch `uint16`): `z+1` step **`8.91` / `11.75 ms`**; 21-step rapid scrub decoded **`3.0` planes median (`3` max)**, `0` leaked threads (`521.7 MiB` Peak RSS).
  - `Scanner LIF #1` (`Z=9`, `68.5 MiB` 2-ch `uint8`): `z+1` step **`9.27` / `12.03 ms`**; 7-step rapid scrub decoded **`3.0` planes median (`3` max)**, `0` leaked threads (`149.9 MiB` Peak RSS).
- **4-Concurrent-Real-Slide Compare Gate (`Scanner ND2 #1` + `Scanner CZI #1` + `Scanner TIFF #1` + `Scanner CZI #2` = `6.63 GiB` combined raw pixels across 4 tabs + 32-tile bursts + LUT drag + `Z=31` scrub):**
  - **Observed Peak RSS:** **`709.3 MiB` median / `713.9 MiB` p95 / `714.4 MiB` max** (`n = 3` clean subprocesses).
  - **Acceptance Gate (`<= 1,228.8 MiB` / `1.2 GiB`):** **PASS** with **`+514.4 MiB` (`41.9%`) headroom** *(one-time uncached Dask `.nd2svs` overview build on the `3.99 GiB` 4-ch `uint16` `Scanner ND2 #1` peaks at `5,185.1 MiB` during its `7.40 s` conversion before dropping to `300.9 MiB` steady-state)*.
- **Measured `v2.6.4` (`6f3a2cc`) vs Milestone 1 Baseline Comparison on Real Scanner Files:**
  - **Native `.tif` / `.ome.tif` Open & ROI Round-Trip Reopen:** `HTTP 400` unsupported across all slides in `6f3a2cc` → **`100%` (`8 / 8`) `200 OK`** native open, `.ome.tif` ROI reopen, and sidecar persistence in Milestone 1.
  - **Multi-Scene Access:** `seriesCount = 1` only in `6f3a2cc` → **`2` scenes** (`Scanner CZI #1`), **`16` series** (`Scanner LIF #1`), and **`18` multipoint positions** recovered on single-`<Image>` multipoint OME-TIFF exports (`5.78 GiB`, `18 × 3 × 7578²` `uint16`).
  - **16-Tile Live LUT Drag (`win=` median):** `21.63 → 12.92 ms` (`-40.3%`) on `Scanner ND2 #1`; `19.82 → 9.42 ms` (`-52.5%`) on `Scanner CZI #1`; `20.10 → 9.96 ms` (`-50.4%`) on `Scanner LIF #1`; `22.35 → 16.58 ms` (`-25.8%`) on `Scanner CZI #2`.
  - **3D Confocal `Scanner CZI #2` (`Z=31`) Step & Scrub:** `z+1` step **`22.09 → 8.37 ms` (`2.64×` faster)**, rapid-scrub final frame `29.14 → 16.66 ms` (`-42.8%`), and Z-stack Peak RSS `581.6 → 455.6 MiB` (`-126.0 MiB`).
- **Real-Scanner Container Quirks Resolved During Hardware Validation:**
  - *NIS-Elements OME-TIFF metadata & multipoint IFDs (`nd2wsi/ometiff.py`):* Resolved per-`<Image>` `<ObjectiveSettings>` / `<InstrumentRef>` before global `<Objective>` fallback (avoiding Nikon's backward-compatibility `Instrument:0` `1×` placeholder), supported unsigned 24-bit `0x00RRGGBB` `<Channel Color="...">` integers alongside standard signed 32-bit RGBA OME-XML colors, and recovered trailing homogeneous multipoint IFDs when an exporter writes a single `<Image>` header for `N × C` planes.
  - *Zeiss ZEN CZI `StartC` & disabled-track channel mapping (`nd2wsi/czi.py`):* Prioritized ZISRAW `StartC` attributes when XML `<Channel Id="...">` values repeat, and mapped strictly ascending equal-count XML channel definitions `1:1` when an intermediate acquisition track was disabled (`Channel:0, Channel:1, Channel:3` → SubBlocks `[0, 1, 2]`).

---

## 2. 5-External-Lab Observational Study Protocol

Historical GitHub release download counts (`1–3` downloads per tag) cannot separate poor discoverability, macOS Gatekeeper / Windows SmartScreen installation friction, unsupported vendor container edge cases, or lack of recurring workflow utility. Broad community promotion is gated on an empirical **5-lab observational cohort**.

### 2.1 Cohort Composition (`N = 5` External Microscopy Users)

Recruit 5 active wet-lab or imaging-core researchers outside the development group spanning all primary formats and both desktop operating systems:

| Slot | Primary Acquisition Format | Modality & Typical Scale | Target OS |
| :--- | :--- | :--- | :--- |
| **Lab 1** | Nikon `.nd2` | Stitched multichannel fluorescence or multipoint Z-stack (`1–15 GB`) | macOS (Apple Silicon) |
| **Lab 2** | Zeiss `.czi` | Multi-tile `M` mosaic (`Zstd0`/`Zstd1`/`JPEG-XR`) or ApoTome Z-stack | Windows 11 (`x86_64`) |
| **Lab 3** | Leica `.lif` | Multi-series confocal (`Series 0..N` + interactive `Z`-stack) | macOS or Windows 11 |
| **Lab 4** | `.ome.tif` / `.qptiff` / `.svs` | Exported pyramidal OME-TIFF, multiplex pathology QPTIFF, or brightfield WSI | Windows 11 (`x86_64`) |
| **Lab 5** | Mixed multi-vendor (`.nd2` / `.czi` / `.ome.tif`) | Serial-section Compare alignment or scalebar figure preparation | macOS (Apple Silicon / Intel) |

*Require at least **2 macOS** and **2 Windows** participants across the 5 slots.*

### 2.2 Unassisted 4-Step Task Script

Provide each participant **only** the release download URL and the [README](../README.md) (zero live coaching, zero terminal commands, and zero pre-converted demo files):

1. **Step 1 — Install & Launch:** Download the signed macOS `.dmg` or Windows installer `.exe`, install, and launch `nd2wsi-viewer` without terminal overrides (`xattr`, Security Settings bypasses, or manual archive extraction).
2. **Step 2 — Open Own Acquisition:** Open a real acquisition recorded in the participant's own lab (`>= 500 MB` preferred) and verify physical pixel size (`µm/px`), objective magnification, and channel names/colors against the acquisition software.
3. **Step 3 — Inspect Channels, Scenes & Z-Planes:** Switch scenes/series (if multi-series), adjust per-channel LUT contrast windows or solo channels, and scrub Z-planes (if 3D) while observing responsiveness.
4. **Step 4 — Export & Reopen ROI:** Draw or select a region of interest, export both a native-value `.ome.tif` crop and a scalebar figure (`PNG`/`SVG`/`PDF`), then reopen the exported `.ome.tif` in `nd2wsi-viewer` (and optionally Fiji or QuPath), add one annotation, and verify persistence on reopen.

### 2.3 Exact Failure Taxonomy

Log every friction event or failure against one of six root-cause codes before triaging engineering follow-ups:

| Code | Category | Concrete Failure Definition |
| :--- | :--- | :--- |
| **`F1-INSTALL`** | OS Trust / Packaging | Gatekeeper block, SmartScreen block, broken `.exe` / `.dmg` launch, or missing bundled codec (`imagecodecs`, `tifffile`). |
| **`F2-DECODE`** | Container / Codec | `HTTP 400/500` on open, unsupported compression/pixel type, or truncated mosaic/SubIFD parsing on real scanner output. |
| **`F3-CALIB`** | Metadata / Calibration | Missing or wrong `µm/px` scalebar, swapped X/Y or RGB/BGR channels, missing scene in `#scene-picker`, or wrong channel names. |
| **`F4-PERF`** | Latency / Memory | Cold open perceived as hung, jerky LUT drag / Z-scrub (`> 100 ms` frame stall), or 4-tab memory exceeding `1.2 GiB`. |
| **`F5-UX`** | Workflow Discoverability | User cannot locate scene switcher, LUT solo/window controls, ROI native `.ome.tif` export, or annotation save/undo without assistance. |
| **`F6-EXPORT`** | Downstream Interop | Reopened `.ome.tif` loses physical pixel size, channel names, native bit depth (`uint8`/`uint16`/`float32`), or sidecar annotations in `nd2wsi-viewer`, Fiji, or QuPath. |

### 2.4 Week-2 Retention Gate & Outreach / JOSS Schedule

- **Session Completion Criterion:** `>= 4 / 5` participants complete Steps 1–4 unassisted with zero `F1-INSTALL`, `F2-DECODE`, `F3-CALIB`, or `F6-EXPORT` blockers.
- **Week-2 Unprompted Retention Gate (`>= 3 / 5`):** Check back 14 days after onboarding without intermediate reminders. Require **`>= 3 / 5` participants** to have voluntarily reopened `nd2wsi-viewer` on additional bench acquisitions during routine work **before** posting announcements on `forum.image.sc` or contacting institutional microscopy core facilities. If `< 3 / 5` retain usage, resolve the top logged `F1–F6` causes (e.g., cold-open pyramid latency or post-Milestone 1 scientific candidates such as deterministic `MIP`/`Mean` Z-projections, calibrated line/polygon raw intensity readouts, or split-channel montage extensions of [scalebar.py](../nd2wsi/scalebar.py)) and re-verify.
- **JOSS Submission Eligibility (`March 2027+`):** Defer any *Journal of Open Source Software* (JOSS) submission until **March 2027 or later**—requiring at least 6 months of public repository history from the `2026-08-29` public start, clean SPDX `MIT` licensing, and documented external research usage beyond the author's lab.

---

## 3. Separate Validation-Gated Workstreams

The three capabilities below carry high risk of silent scientific or hardware error if generalized purely in software. They are **excluded from the core Milestone 1+ engineering roadmap** and gated on dedicated experimental pre-conditions:

### 3.1 Cross-Vendor Shading Correction Generalization

- **Current Empirical Status:** As documented in [shading-correction-validation.md](shading-correction-validation.md#what-the-development-checks-show), the existing retrospective ND2 `FITC`/`CY3` periodic-band estimator reduced primary periodic-band power by `39.70%` across 40 native region–channel pairs, **but failed its predeclared new-band artifact criterion (`0.10071` observed mean new-band relative power vs `0.03905` predeclared limit)** and relies on scans with **no independent illumination ground truth**.
- **Mandatory Pre-Conditions Before Any Expansion (`.czi` / `.lif` / `.ome.tif` or Additional Fluorophores):**
  1. Acquire paired **uniform fluorescent reference slides** (e.g., concentrated dye / Chroma plastic reference) and camera **dark-frame** ground truth across target vendor optics alongside biological tissue sections.
  2. Pre-register quantitative pass/fail bounds on both residual shading error against true flat-field division *and* newly introduced spatial-frequency power (`<= 0.03905` ceiling), demonstrating non-amplification of periodic biological structures (e.g., striatal patch/matrix, cortical barrels, or muscle sarcomeres) on held-out test slides.

### 3.2 Quantitative Colocalization Metrics (`Pearson` / `Manders`)

- **Methodological Risk:** Computing `Pearson` $r$ or `Manders` $M_1 / M_2$ directly on viewer ROIs produces misleading biological conclusions when camera offset/dark noise is unsubtracted, background thresholds are chosen ad hoc, lateral/axial PSF chromatic aberration shifts channels by `100–300 nm`, or widefield out-of-focus Z haze dominates both channels.
- **Mandatory Pre-Conditions Before Any Implementation:**
  1. Define a separate methodological specification covering mandatory background/noise-floor estimation (e.g., Costes automatic threshold + pixel-randomization significance test $P$-value), explicit saturation clipping warnings, and modality suitability guards (widefield vs deconvolved/confocal optical sections).
  2. Validate numerical agreement and negative-control specificity on dual-labeled sub-resolution fluorescent bead slides (known `100%` colocalization + chromatic shift measurement) and spatially mutually exclusive biological marker pairs (known `0%` colocalization).

### 3.3 Physical Motorized-Stage Coordinate Calibration (`NIS-Elements` / `ZEN` / `LAS X`)

- **Hardware Risk:** Translating viewer pixel ROIs into live motorized-stage `(X, Y, Z)` move commands across Nikon `NIS-Elements`, Zeiss `ZEN`, and Leica `LAS X` cannot be verified by software unit tests. Vendor stage controllers differ in stage origin conventions, axis sign inversions, stage-insert rotation, objective parcentricity/parfocality offsets, and mosaic stitching coordinate transforms; an uncalibrated sign or origin error can drive high-NA immersion objectives into slide clips or coverslip edges.
- **Mandatory Pre-Conditions Before Any Core Integration:**
  1. Execute physical bench calibration runs using fiducial grid reticle slides across each target microscope stand (`NIS-Elements`, `ZEN`, `LAS X`) across multiple objectives (`10×`, `20×`, `40×`/`63×` oil/water).
  2. Enforce mandatory per-session 3-point fiducial registration, soft Z-travel safety interlocks, and measured re-centering error bounds (`<= 2 µm` lateral) on physical hardware before exposing stage-drive controls outside isolated experimental branches.

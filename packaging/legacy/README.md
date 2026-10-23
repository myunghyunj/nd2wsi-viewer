# Legacy packaging recipes

Frozen historical build recipes, kept for reference. CI does not run them and
they are not part of the current release path, which is
`packaging/build_mac_app.sh` driven by the workflows under `.github/workflows`
(with `packaging/build_windows.py` for Windows).

| Recipe | Produced | Superseded by |
| --- | --- | --- |
| `build_metal_beta.sh` + `launch_metal_beta.py` | `nd2wsi-viewer Metal Beta` 2.1.0b2, the isolated local Metal pyramid beta | the 2.1 release candidates (`packaging/build_metal_rc.sh`) and the 2.1.x releases |
| `build_astromap_beta.sh` + `launch_astromap_beta.py` | `nd2wsi-viewer Astromap Beta` 2.4.0b1, the local Astromap annotation beta | the regular app from 2.4.1, which ships Astromap |

Each script keeps a single `VERSION` at the top that feeds the bundle's
Info.plist, the packaged-source verifier (`scripts/verify_packaged_source.py`)
and the archive name. The launchers read `ND2WSIPackageVersion` from their own
bundle when frozen and honour `ND2WSI_BETA_VERSION` when run from a checkout;
otherwise `nd2wsi.__version__` is left alone. Both betas keep their own bundle
identifiers and disable Sparkle automatic checks; the Astromap beta also sets
`ND2WSI_DISABLE_UPDATES=1` and marks the bundle `ND2WSIDevelopmentOnly`. Nothing
here touches the stable app, its file associations, the appcast or Windows.

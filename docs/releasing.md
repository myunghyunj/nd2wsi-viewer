# Release completion contract

For this project, a requested fix is complete only after the validated change is committed and pushed to main, a new GitHub Release and signed Sparkle feed are published, the local /Applications app is updated and verified, and development-only caches are cleaned. Do not stop at a source push or local-only installation unless the user explicitly requests that narrower scope or a release gate is blocked.

1. Work in the user-designated canonical checkout. Preserve unrelated changes; never force-push or create an approval branch unless requested.
2. Bump the package and lockfile version. Run focused tests and the full suite; inspect actual UI behavior for UI fixes.
3. Commit and push the candidate. Require all CI jobs green and dispatch the realdata workflow against its full source SHA before tagging.
4. Build with pinned Sparkle and a disposable copy of a known test ND2. Never ship with smoke/updater skip flags. Verify packaged smoke, code signatures, version and source assets.
5. Publish the verified artifact and checksum at a new version tag. Verify the downloaded release artifact; update the signed Sparkle appcast without changing its signing identity.
6. Update /Applications, verify the installed bundle and run its smoke test. The user prefers the previous GitHub Release as the rollback source; do not create redundant local app backups unless requested. Never replace an app with unsaved work in progress.
7. Inventory exact development-cache targets, exclude tracked files, research sources, real viewing caches, annotations and useful virtual environments, then clean only confirmed generated data. Report the cleanup boundary and recovery location when used.
8. Report the public release link, installed version, validation results and remaining limitations. An attempted upload, source-only commit or unverified local build is not a completed release.

No private research data or sample paths belong in public release assets or notes. Signed updates are not the same as Developer ID notarization; disclose the actual signing status.

## macOS code signing, notarization, and Gatekeeper verification

`packaging/sign_release.sh` and `.github/workflows/macos-release.yml` support three macOS signing modes, recorded verbatim in `dist/macos/build-info.json` under `"signing"`:

- `"ad-hoc"` (default when signing secrets are absent): strips file-provider extended attributes (`xattr -cr`), preserves official Sparkle helper signatures and entitlements, ad-hoc signs all remaining Mach-O binaries and the outer `.app` bundle, and verifies `codesign --verify --deep --strict --verbose=2`.
- `"developer-id"` (Developer ID certificate configured without Notary credentials, or `NOTARIZE_RELEASE=0`): signs nested Mach-O binaries, Sparkle helpers (`--preserve-metadata=entitlements`), the `.app` bundle, and the `.dmg` disk image with `--force --options runtime --timestamp --sign "$CODESIGN_IDENTITY"`.
- `"notarized"` (Developer ID certificate plus Apple Notary credentials configured): performs `"developer-id"` signing on `.app` and `.dmg`, submits `.app` (as a `ditto -c -k --keepParent` archive) and `.dmg` via `xcrun notarytool submit --wait`, retrieves `xcrun notarytool log <submission-id>` automatically on rejection, staples and validates tickets (`xcrun stapler staple` / `xcrun stapler validate`), and verifies Gatekeeper acceptance (`spctl`).

### GitHub Actions secrets (`.github/workflows/macos-release.yml`)

| Secret | Required for | Description |
| --- | --- | --- |
| `MACOS_CERT_P12_BASE64` | `developer-id`, `notarized` | Base64-encoded `.p12` archive containing the `Developer ID Application` certificate and private key (`base64 -i DeveloperIDApplication.p12 \| tr -d '\n'`). |
| `MACOS_CERT_PASSWORD` | `developer-id`, `notarized` | Export password for decrypting `MACOS_CERT_P12_BASE64` into the ephemeral runner keychain. |
| `CODESIGN_IDENTITY` | `developer-id`, `notarized` | Exact code-signing identity string, e.g. `Developer ID Application: Your Name (TEAMID1234)`. |
| `APPLE_NOTARY_KEY_BASE64` | `notarized` (API key mode) | Base64-encoded App Store Connect API private key (`AuthKey_<KEYID>.p8`). |
| `APPLE_NOTARY_KEY_ID` | `notarized` (API key mode) | App Store Connect API Key ID (e.g. `ABC1234567`). |
| `APPLE_NOTARY_ISSUER_ID` | `notarized` (API key mode) | App Store Connect API Issuer UUID. |
| `APPLE_NOTARY_PROFILE` | `notarized` (profile mode) | Optional alternative keychain profile name when using a runner keychain profile instead of `APPLE_NOTARY_KEY_*`. |

When all secrets above are empty, `macos-release.yml` completes in `"ad-hoc"` mode. If partial signing secrets are present, or if `APPLE_NOTARY_*` secrets are set without `MACOS_CERT_P12_BASE64` + `MACOS_CERT_PASSWORD` + `CODESIGN_IDENTITY`, the workflow fails closed (`exit 1`) rather than silently falling back to an ad-hoc build. Ephemeral keychains and decoded `.p8` keys live under `$RUNNER_TEMP` and are removed in an `if: always()` cleanup step.

### Local signing and notarization commands

Store a reusable Notary keychain profile once on the release Mac:

```bash
xcrun notarytool store-credentials "nd2wsi-notary" \
  --apple-id "you@example.edu" \
  --team-id "TEAMID1234"
```

Build, Developer-ID sign, smoke-test, notarize, staple, and Gatekeeper-verify both `.app` and `.dmg` using a keychain profile:

```bash
export CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID1234)"
export APPLE_NOTARY_PROFILE="nd2wsi-notary"
export NOTARIZE_RELEASE=1

ND2WSI_SMOKE_FILE=docs/example_cell.nd2 packaging/build_mac_app.sh dist
packaging/sign_release.sh dist/nd2wsi-viewer.dmg
```

Or run `packaging/sign_release.sh` directly with an App Store Connect API key (`.p8`):

```bash
CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID1234)" \
APPLE_NOTARY_KEY_PATH="/path/to/AuthKey_ABC1234567.p8" \
APPLE_NOTARY_KEY_ID="ABC1234567" \
APPLE_NOTARY_ISSUER_ID="00000000-0000-0000-0000-000000000000" \
NOTARIZE_RELEASE=1 \
packaging/sign_release.sh dist/nd2wsi-viewer.app dist/nd2wsi-viewer.dmg
```

If `NOTARIZE_RELEASE=1` is passed with `CODESIGN_IDENTITY="-"` (or unset) or without Notary credentials, `packaging/sign_release.sh` exits non-zero immediately.

Independent local verification of signed and stapled artifacts:

```bash
codesign --verify --deep --strict --verbose=2 dist/nd2wsi-viewer.app
xcrun stapler validate -v dist/nd2wsi-viewer.app
xcrun stapler validate -v dist/nd2wsi-viewer.dmg
spctl -a -vvv -t exec dist/nd2wsi-viewer.app
spctl -a -vvv -t open --context context:primary-signature dist/nd2wsi-viewer.dmg
```

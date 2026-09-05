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

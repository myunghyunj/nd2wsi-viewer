# Windows Setup build

`windows-installer.nsi` wraps an already verified Windows portable build into a
per-user Setup executable (`nd2wsi-viewer-<version>-windows-x64-setup.exe`). It
does not rebuild CPython or publish a GitHub Release on its own. Compile with
NSIS 3.x (`makensis`) using its default x86 Unicode stub (`NSIS_PTR_SIZE == 4`).
The installed application remains the x64 PyInstaller bundle (`nd2wsi-viewer.exe`
plus `_internal`).

Supported and CI-tested targets are Windows 11 x64, Windows 11 ARM64 (running
the x64 executable through Windows x64 emulation), and Windows Server 2025 x64
(Desktop Experience CI). While Setup's `.onInit` preflight gate permits 64-bit
Windows build 16299+ (Windows 10 version 1709+) when `.NET Framework 4.6.2+` and
WebView2 are present, Windows 10 is not tested in CI and is not a supported
target; Windows 10 ARM64, headless `Server Core` / `Nano Server`, and 32-bit
Windows are refused outright. Acceptance by the OS gate is not a substitute for
executing `verify_windows_installer.ps1` on the target Windows environment.

## Compiler inputs

Pass each input as a `makensis` define (`-D` works on Windows, macOS, and Linux):

| Define | Value |
| --- | --- |
| `APP_VERSION` | Portable build manifest's display/ownership version, for example `2.6.4` |
| `APP_VERSION_QUAD` | The same version plus `.0`, for example `2.6.4.0` |
| `OUTPUT_FILE` | Absolute destination of the generated `*-windows-x64-setup.exe` |
| `PAYLOAD_INSTALL_INCLUDE` | Absolute path of generated installation commands |
| `PAYLOAD_UNINSTALL_INCLUDE` | Absolute path of generated uninstall commands |
| `PAYLOAD_GUARD_INCLUDE` | Absolute path of generated existing-path safety checks |
| `PAYLOAD_SIZE_KIB` | Total uncompressed payload size rounded up to KiB |
| `WEBVIEW2_BOOTSTRAPPER` | Absolute path of the official Microsoft bootstrapper |
| `APP_ICON` | Optional absolute `.ico` path; otherwise NSIS's standard icons |

Generate all three includes from the same complete hash-verified portable file
manifest. Reject symlinks, reparse points, absolute/traversing relative names,
reserved Windows filenames, wildcard characters, CR/LF, and names that collide
under Windows's case-insensitive rules. Reject collisions with installer-owned
`.nd2wsi-install.ini` and `Uninstall.exe`. Escape NSIS dollar signs and quotes in
both source and relative paths. Never use a recursive `File /r` input that can
accidentally absorb unrelated files or omit the reviewed manifest.

`build_windows_installer.py` derives version values from the portable
`_internal/nd2wsi-viewer-about/build-info.json`. `verify_windows_installer.ps1`
derives its expected version and signing state from the exact `.build.json` and
`.payload.json` manifests, so no historical release number is used as an
implicit fallback.

The install include emits a `SetOutPath` for each parent directory and one `File`
instruction for each exact source file:

```nsis
SetOutPath "$INSTDIR\_internal\example"
File "/oname=sample.dll" "/absolute/verified/payload/_internal/example/sample.dll"
```

The uninstall include emits script-provided macros for every file and then for
each payload directory, deepest first. Omit the root directory and
installer-owned metadata; the script handles them separately:

```nsis
!insertmacro ND2WSI_DELETE_FILE "_internal\example\sample.dll"
!insertmacro ND2WSI_REMOVE_EMPTY_DIR "_internal\example"
!insertmacro ND2WSI_REMOVE_EMPTY_DIR "_internal"
```

The guard include emits `!insertmacro ND2WSI_GUARD_PATH "relative\path"` once
for every shipped file and every payload parent directory. The script also
checks the installation root and its own metadata paths. Each check inspects
the named path and all existing ancestors using `GetFullPathNameW` with a
separate checked buffer. Reparse points (including directory junctions and file
symlinks) or unexpected path-inspection errors stop Setup or Uninstall before
app files or registry records are changed.

Download the WebView2 bootstrapper only from Microsoft's official Evergreen link
(`https://go.microsoft.com/fwlink/p/?LinkId=2124703`). Record its SHA-256 and
verify its Authenticode signature belongs to `Microsoft Corporation` on Windows
before accepting the Setup artifact. The bootstrapper is embedded separately
from the app manifest and extracted into NSIS's temporary plugin directory only
when the WebView2 Runtime is absent.

## Optional Authenticode signing

`packaging/build_windows_installer.py` provides `sign_windows_binary()` for
optional SHA-256 Authenticode code signing:

- **Default (unsigned)**: When `WINDOWS_SIGN_PFX_PATH`,
  `WINDOWS_SIGN_THUMBPRINT`, `WINDOWS_SIGNTOOL`, and `WINDOWS_REQUIRE_SIGNING`
  are unset, `build_windows_installer.py` leaves `nd2wsi-viewer.exe` and
  `*-windows-x64-setup.exe` unsigned and records `"status": "unsigned"` in
  `.build.json` and `.payload.json`.
- **Configured signing**: When `WINDOWS_SIGN_PFX_PATH` (optional
  `WINDOWS_SIGN_PFX_PASSWORD`) or `WINDOWS_SIGN_THUMBPRINT` is set,
  `sign_windows_binary()` locates `signtool.exe` (via `WINDOWS_SIGNTOOL`,
  `PATH`, or Windows 10/11 SDK `bin\*\x64\signtool.exe`) and invokes:
  `signtool sign /fd sha256 /tr <WINDOWS_TIMESTAMP_URL> /td sha256 ...`
  (default timestamp server `http://timestamp.digicert.com`).
  1. `nd2wsi-viewer.exe` in the staged payload is signed **before** `makensis`
     runs, and `_internal/nd2wsi-viewer-about/build-info.json` plus
     `.payload.json` are updated with both `unsigned_executable_sha256` and the
     post-signature `executable_sha256`.
  2. The generated `*-windows-x64-setup.exe` is signed **after** `makensis`
     completes and before writing `.build.json` and `.exe.sha256`.
  3. Passwords are redacted from captured subprocess output and never written to
     manifests.
- **Fail-closed enforcement**: Setting `WINDOWS_REQUIRE_SIGNING=1` without valid
  signing credentials (or setting `WINDOWS_SIGNTOOL` without credentials, or any
  `signtool` non-zero exit) raises immediately so a release build that expects
  signing cannot silently fall back to unsigned output.

## Per-user installation, file capabilities, and ownership

Setup uses `RequestExecutionLevel user` (zero UAC administrator prompt) and
defaults to `%LOCALAPPDATA%\Programs\nd2wsi-viewer`. Start Menu shortcuts are
required; the Desktop shortcut is enabled by default and selectable on the
Components page. No `HKLM` keys or system `PATH` entries are modified.

Setup registers per-user Windows `Open with` and Default Apps capabilities in the
64-bit `HKCU` view for all Milestone 1 microscopy and cache formats:
`.nd2`, `.czi`, `.lif`, `.tif`, `.tiff`, `.qptiff`, `.svs`, `.sdpc`, and `.nd2svs`:

- `HKCU\Software\Classes\nd2wsi-viewer.Slide` (`DefaultIcon` and
  `shell\open\command` -> `"$INSTDIR\nd2wsi-viewer.exe" "%1"`)
- `HKCU\Software\Classes\Applications\nd2wsi-viewer.exe` (`FriendlyAppName`,
  `DefaultIcon`, `shell\open\command`, and `SupportedTypes`)
- `HKCU\Software\nd2wsi-viewer\Capabilities` (`ApplicationName`,
  `ApplicationDescription`, and `FileAssociations`)
- `HKCU\Software\RegisteredApplications` (`nd2wsi-viewer` ->
  `Software\nd2wsi-viewer\Capabilities`)
- `HKCU\Software\Classes\<ext>\OpenWithProgids` value `nd2wsi-viewer.Slide`

Registering via `OpenWithProgids` and `Capabilities\FileAssociations` exposes
`nd2wsi-viewer` in Explorer's **Open with** menu and Windows **Default apps**
without clobbering a user's existing default `.tif` / `.tiff` handler.

The uninstall record lives in the 64-bit view of
`HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\nd2wsi-viewer`.
Its `InstallLocation` must agree with the selected destination and the UTF-16LE
BOM ownership marker `.nd2wsi-install.ini`:

```ini
[Installation]
ProductId=nd2wsi-viewer.windows.x64.current-user
Version=2.6.4
InstallLocation=C:\Users\example\AppData\Local\Programs\nd2wsi-viewer
UninstallState=
```

An initial installation requires an empty or nonexistent destination; an
unrecognized nonempty folder is refused before any app file is written.
Reinstallation accepts only the same version's marker together with its matching
uninstall registry location. Uninstall the previous installation before changing
version or location; user-created files are retained.

The ownership marker, `Uninstall.exe` (staged via a unique same-folder temp file
and replaced atomically with `MoveFileExW`), and uninstall registry records are
created before copying the payload. If initial metadata creation fails on a
fresh folder, Setup rolls back its newly created metadata and registry key (or
retains a recoverable `in-progress` retry identity if file/registry locks block
rollback) and exits with code `2`.

Both installation and uninstallation refuse to continue while any process named
`nd2wsi-viewer.exe` is running (checked via `CreateToolhelp32Snapshot` without
terminating user processes) and serialize via `Local\nd2wsi-viewer-Setup-current-user`.

Setup verifies `.NET Framework 4.6.2+` (`Release >= 394802`) and checks the
WebView2 `pv` registry values (`HKCU`/`HKLM`), running the embedded Microsoft
bootstrapper without elevation if WebView2 is missing. The shared WebView2
runtime is retained on uninstall.

## Clean uninstall and verification (`verify_windows_installer.ps1`)

Uninstall verifies the ownership marker and registered location, rejects reparse
points across payload and shortcut chains, deletes only literal manifest-listed
payload files, removes empty payload directories deepest-first, removes its own
shortcuts, removes its own `HKCU` `RegisteredApplications` / `Capabilities` /
`nd2wsi-viewer.Slide` / `Applications\nd2wsi-viewer.exe` entries, and removes
only the `nd2wsi-viewer.Slide` value under each extension's `OpenWithProgids`
(deleting `OpenWithProgids` and `<ext>` keys via `RegQueryInfoKeyW` **only**
when zero subkeys and zero values remain, so other applications' handlers on
shared extensions like `.tif` are never disturbed). It then marks
`UninstallState=in-progress`, deletes the `Uninstall` registry key, removes
`.nd2wsi-install.ini` and `Uninstall.exe`, and removes `$INSTDIR` only if empty.

`packaging/verify_windows_installer.ps1` validates the generated
`*-windows-x64-setup.exe` on Windows (`windows-2025` x64 in CI and local QA):

1. Preflight checks OS/64-bit/`.NET 4.6.2+` readiness, `.build.json`,
   `.payload.json`, `.exe.sha256`, Microsoft's Authenticode signature on the
   WebView2 bootstrapper, and (when `.build.json` reports `"signed": true`)
   valid Authenticode signatures on `Setup.exe` and the installed
   `nd2wsi-viewer.exe`.
2. Silently installs `Setup.exe` into an isolated Unicode/space-containing QA
   directory (`/S /D=...`), verifies `nd2wsi-viewer.exe` and `_internal`, checks
   every payload file's SHA-256, shortcuts, `HKCU` uninstall metadata, and
   per-user `HKCU` file-association capabilities across all 9 Milestone 1
   extensions (`.nd2`, `.czi`, `.lif`, `.tif`, `.tiff`, `.qptiff`, `.svs`,
   `.sdpc`, `.nd2svs`).
3. Runs frozen `nd2wsi-viewer.exe --help`, frozen `nd2wsi.ometiff` +
   `nd2wsi.cli --help` verification, headless scientific `--smoke` (ND2, SVS,
   SDPC, Astromap), and native WebView2 `--gui-smoke`.
4. In full mode (default), exercises same-version reinstall, directory-junction
   rejection, four locked-file uninstall recovery cycles (`payload`,
   `desktop-shortcut`, `marker`, `uninstaller`), interrupted-cleanup marker
   recovery with `.nd2` and `.nd2svs` sentinels preserved, AND a clean silent
   `Uninstall.exe /S` cycle verifying complete directory and registry removal.
   `-Quick` runs steps 1–3 followed immediately by direct silent
   `Uninstall.exe /S` clean-removal verification.

References: [NSIS scripting reference](https://nsis.sourceforge.io/Docs/Chapter4.html),
[NSIS process checks](https://nsis.sourceforge.io/Check_whether_your_application_is_running),
[NSIS exit codes and temporary uninstall copy](https://nsis.sourceforge.io/Docs/AppendixD.html),
[Microsoft WebView2 deployment](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution),
[Windows process snapshot API](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-createtoolhelp32snapshot),
[WebView2 platform requirements](https://learn.microsoft.com/en-us/microsoft-edge/webview2/),
[.NET Framework detection](https://learn.microsoft.com/en-us/dotnet/framework/install/how-to-determine-which-versions-are-installed),
[Windows ARM emulation](https://learn.microsoft.com/en-us/windows/arm/apps-on-arm-x86-emulation),
[Windows Unicode INI behavior](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-writeprivateprofilestringw).

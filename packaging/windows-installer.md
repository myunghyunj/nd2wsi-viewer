# Local Windows Setup build

`windows-installer.nsi` wraps an already verified Windows portable build. It does
not build Python, modify the portable payload, publish a GitHub Release, or alter
the macOS app. Use NSIS 3.12 with its default x86 Unicode stub. The installed
application remains the existing x64 executable. Setup accepts Windows 10
version 1709 (build 16299) or later and Windows 11 on x64, including Windows 10
Education. Windows 11 ARM64 runs this executable
through Windows x64 emulation; Windows 10 ARM64 is refused because its emulator
supports only x86 apps.

The intended lab target is Windows 10 Education x64, preferably version 22H2.
Acceptance by the installer's OS gate is not a completed compatibility test.
Run the supplied QA on that actual lab computer and retain its machine/build
details and results. Earlier Windows 11 ARM64 portable-build results do not
establish that this Setup or app has passed Windows 10 testing.
Compatibility claims cover the tested microscopy paths; optional codecs need
separate validation.

## Compiler inputs

Pass each input as a makensis define (use `-D` on macOS/Linux, `/D` on Windows):

| Define | Value |
| --- | --- |
| `APP_VERSION` | Display/ownership version, for example `1.2.8` |
| `APP_VERSION_QUAD` | Four-component Windows version, for example `1.2.8.0` |
| `OUTPUT_FILE` | Absolute destination of the generated Setup `.exe` |
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

The install include emits a `SetOutPath` for each parent directory and one `File`
instruction for each exact source file. For example:

```nsis
SetOutPath "$INSTDIR\_internal\example"
File "/oname=sample.dll" "/absolute/verified/payload/_internal/example/sample.dll"
```

The uninstall include emits these script-provided macros for every file and
then for each payload directory, deepest first. Omit the root directory and
installer-owned metadata; the script handles them separately.

```nsis
!insertmacro ND2WSI_DELETE_FILE "_internal\example\sample.dll"
!insertmacro ND2WSI_REMOVE_EMPTY_DIR "_internal\example"
!insertmacro ND2WSI_REMOVE_EMPTY_DIR "_internal"
```

The guard include emits `!insertmacro ND2WSI_GUARD_PATH "relative\path"` once
for every shipped file and every payload parent directory. The script also
checks the installation root and its own metadata paths. Each check inspects
the named path and all existing ancestors. Reparse points (including junctions
and file symlinks) or unexpected path-inspection errors stop Setup before app
files or registry records are changed. The same preflight runs before uninstall
deletes any files, preventing a replaced payload directory from redirecting
manifest-listed deletion into a user's other folder.

Destination and ancestor-check normalization uses `GetFullPathNameW` with a
separate checked output buffer, so a not-yet-created path remains usable and
its existing ancestors are still inspected. Zero-length and oversized results
stop preflight without overwriting the chosen destination. The NSIS
`GetFullPathName` instruction can clear its output for a nonexistent path and
is therefore reserved here for already recorded installation locations.

Download the bootstrapper only from Microsoft's documented WebView2 link. Record
its SHA-256 and verify its Authenticode signature is valid and belongs to
Microsoft Corporation on Windows before accepting the Setup artifact. The
bootstrapper is embedded separately from the app manifest and only extracted
into NSIS's temporary plugin directory if the WebView2 Runtime is absent.

## Installation and ownership

Setup requests the current user's privileges and defaults to
`%LOCALAPPDATA%\Programs\nd2wsi-viewer`. It rejects Windows versions before 10
and Windows 10 builds before 16299,
actual Windows Server editions, architectures other than x64/ARM64, and Windows
10 on ARM64. A machine used as a lab server but running Windows 10 Education is
a desktop OS and passes the edition check. Start Menu shortcuts are
required. The Desktop shortcut is selected by default and can be unchecked on
the Components page. No file associations or system PATH changes are made.

The stable uninstall key is in the 64-bit view of
`HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\nd2wsi-viewer`.
Its `InstallLocation` must agree with the selected destination and the marker:

```ini
[Installation]
ProductId=nd2wsi-viewer.windows.x64.current-user
Version=1.2.8
InstallLocation=C:\Users\example\AppData\Local\Programs\nd2wsi-viewer
UninstallState=
```

The marker's filename is `.nd2wsi-install.ini`. It is written as UTF-16LE with a
BOM, so a Unicode installation path is preserved independently of the Windows
ANSI code page. A complete replacement is written to a unique temporary file in
the same folder, renamed over the marker, then read back and checked. This also
converts legacy ASCII markers when repairing or uninstalling the same version.
An initial installation requires
an empty or nonexistent destination; an unrecognized nonempty folder is refused
before any app file is written. Reinstallation only accepts the same version's
marker together with its matching uninstall registry location. This local
installer does not implement migration between versions or two simultaneous
registered installations. Uninstall the previous installation before changing
version or location. User-created files are retained.

The ownership marker, uninstall program, and uninstall registry records are
created before copying the payload. Setup records whether the destination was
new/empty and whether the exact 64-bit uninstall registry key was absent after
the last safety check. A fresh attempt with an existing or inaccessible
unrecognized registry key is refused before metadata creation; it is not adopted.

If initial folder, marker, uninstaller, or registration creation fails before
payload extraction, a fresh attempt rolls back only its new metadata and its
newly created exact registry key, including an empty partially created key. It
also removes the folder if empty. Every removal is checked. Existing
installations never enter this rollback. `Uninstall.exe` is written to a unique
same-folder temporary file and atomically replaced only after writing succeeds,
so a failed same-version repair does not truncate/delete the old executable.
The `WriteUninstaller` error is captured before any later helper clears it.

If permissions or locks prevent rollback, Setup returns 2, reports the remaining
paths, and attempts to retain a complete uninstaller and path-bound in-progress
marker. When its newly created registry key survives, it also attempts to restore
the matching retry registration. Access failures can prevent that restoration
too; Setup reports the limitation instead of claiming rollback succeeded. This
does not allow normal Setup to adopt a marker-only folder.

If payload extraction is interrupted after metadata creation succeeds, the same
Setup can be rerun, or the partial installation can be uninstalled using the
explicit payload list. Payload write errors cannot be silently skipped.

Both installation and uninstallation refuse to continue while any process named
`nd2wsi-viewer.exe` is running, including portable copies. This conservative
check uses Windows's process snapshot API and does not terminate processes.
Interactive users can close the app and retry. Silent execution fails with exit
code 2. A setup mutex also prevents concurrent installers/uninstallers.

Setup checks the documented WebView2 `pv` registry values. If absent, interactive
Setup offers to download the runtime from Microsoft; silent Setup installs it
automatically. The embedded bootstrapper runs without requesting elevation, and
Setup checks the runtime registry again before installing the application.
Microsoft's updater can use an existing machine-wide updater even when invoked
by a current-user process. An unavailable network or failed prerequisite stops
Setup before app installation. The shared runtime is retained on uninstall.

The bundled pywebview WinForms renderer requires .NET Framework 4.6.2 or later,
matching Microsoft's WebView2 requirement. Setup checks the `Release` DWORD in
`HKLM\Software\Microsoft\NET Framework Setup\NDP\v4\Full` for at least `394802`,
using the 64-bit view and then the 32-bit view as a fallback. A missing/older
Framework stops Setup before app changes and directs the user to update Windows
or install Microsoft's .NET Framework 4.8. Setup does not attempt a machine-wide
.NET installation. Windows 10 22H2 includes .NET Framework 4.8, so that target
normally needs no separate Framework preparation. Python.NET requires at least
4.6.1 and recommends 4.7.2 or later; its shipped 3.1.0 assembly targets .NET
Standard 2.0.

The Windows 10 minimum is version 1709 (build 16299), matching Microsoft's
documented WebView2 SAC desktop support. Some earlier LTSC editions are listed
by Microsoft, but are outside this installer target. Prefer Windows 10
Education 22H2 for lab QA and record its actual OS build.
NSIS 3.12's `IsNativeAMD64`/`IsNativeARM64` macros try `IsWow64Process2` and fall
back to native machine data when that API is unavailable, so architecture
detection does not itself require Windows 10 1709.

## Uninstall and validation

Uninstall checks the marker and registered location before removing files. It
deletes only the literal manifest-listed payload files, its own marker and
uninstaller, its own shortcuts, and its own uninstall registry key. It removes
payload directories only when empty; it never recursively deletes the install
folder, AppData, caches, annotations, or scientific data. Shortcut paths and their
ancestors are checked for reparse points before removal as well.

Every file removal checks the Windows result. Missing files/directories are
accepted, and `ERROR_DIR_NOT_EMPTY` deliberately retains a directory containing
user files. Other errors, including sharing violations and denied access, are
reported in the details and cause exit code 2. This covers payload files, empty
payload directories, shortcuts, the Start Menu folder, the ownership marker,
the installed uninstaller, the uninstall registry key, and the installation
root. A successful retry accepts manifest-listed files already removed during
the previous attempt.

Uninstall removes payload files and shortcuts before changing retry records.
If those operations fail, the existing marker, installed `Uninstall.exe`, and
uninstall registry entry are retained. It then writes and verifies a marker
with `UninstallState=in-progress` and the normalized `InstallLocation`, removes
the registry entry, and removes the marker and installed uninstaller. A late
cleanup failure attempts to restore the marker and full Windows Apps uninstall
entry with `ProductId` and `UninstallState=in-progress`; if the installed
uninstaller was already removed, it is copied back from the running NSIS
temporary copy. If restoration itself fails, exit code 2 and the details explain
that Setup may be needed to restore the uninstall entry.

For a retry after partial metadata cleanup, only the uninstaller accepts one
surviving record: a marker with exact product/version/state/path when the
registry key is absent, or a registry entry with exact product/version/state/path
when the marker is absent. Denied access does not count as absence. A present
but mismatching marker or a different registered path is refused. Normal
installation and reinstallation continue to require their paired records.

NSIS supports `Setup.exe /S /D=C:\dedicated\app folder`. `/D=` must be the last
argument and its path is not separately quoted by NSIS. Use a dedicated Unicode
path to verify Windows PowerShell 5.1 argument forwarding. Silent Setup uses the
default component selection, including the Desktop shortcut.

For local diagnostics, set `ND2WSI_INSTALLER_LOG` in the child process's
environment to a unique file path whose parent already exists. Setup and the
uninstaller append UTF-16LE text with one initial BOM. Leave the variable unset
for normal use. The trace records initialization, temporary paths, prerequisites,
directory and running-process checks, every scripted dialog/abort, initial
metadata writes, and rollback results. Every line includes the NSIS error flag
as it was on entry to the logger. Logging preserves its caller's registers and
the exact incoming NSIS error flag, including when the log file cannot be opened.
Log failure never authorizes or cancels an installation operation.

The `WriteUninstaller` trace records the temporary target immediately before the
command, its saved NSIS failure flag afterward, and `GetLastError` before any
other plugin or file instruction. A Windows error read after an NSIS command is
diagnostic evidence, since the command itself may have performed cleanup and
changed it. Atomic rename traces instead capture the Win32 result and error
directly from `MoveFileExW`. The verifier should retain a separate trace for each
invocation and include its path/content when a silent process exits nonzero.

To obtain a reliable uninstall exit code, copy `Uninstall.exe` to an isolated
temporary test location and execute that copy as
`Uninstall.exe /S _?=C:\dedicated\app folder`. `_?=` must be last and unquoted.
The copy can then remove the original uninstaller. Do not pass `_?=` while
executing the original in place: that prevents NSIS's normal temporary-copy
behavior and locks the file it needs to delete. Normal removal through Windows
Settings or by opening `Uninstall.exe` handles this copying automatically.

Validate the resulting Setup in Windows: Unicode-path fresh install, exact
payload SHA-256 comparison, both packaged smoke modes, shortcut targets, Apps
uninstall records, same-version reinstall, running-app rejection, nonempty
unowned-folder rejection, linked-payload-path rejection, and uninstall with
arbitrary nested sentinel files inside the installation and AppData. Confirm only shipped files and installer
records disappear and every sentinel remains unchanged. Recompute the portable
archive/source identities after building to prove the wrapper did not alter
the verified app.

Additionally hold a Windows file handle that denies deletion on a shipped
payload file, the Desktop shortcut, the marker, and installed `Uninstall.exe` in
separate uninstall cycles. Each attempt must return 2, retain a usable retry
route, and preserve every user sentinel. Verify payload/shortcut failures leave
the registry snapshot and installed uninstaller bytes unchanged. Release each
handle and require a successful retry with no leftover installer-owned files or
registry entry. Exercise the marker-only and registry-only interrupted-cleanup
paths explicitly, plus refusal of a wrong product/version/path marker and a
normal marker with an absent registry key. Retain the test report as evidence;
script review alone is not a Windows runtime result.

For initial-record failure QA, use a separate disposable fixture registry
namespace with the same script logic. Deny child-key creation on its parent,
start with no product key and an empty Unicode destination, and run silent
Setup. Require exit 2, no extracted payload, no marker/uninstaller/temporary
metadata left, and an absent product key. Restore the parent's permissions and
require a successful install into that same destination. Also test a first
registry write that creates a key but cannot set values, confirming the empty
partial key is removed. Keep an existing installation's `Uninstall.exe` open
without delete sharing during same-version repair: require exit 2 with its old
uninstaller hash and payload unchanged and its paired ownership records still
usable. Do not alter the real shared Windows uninstall parent for a fixture.

References: [NSIS scripting reference](https://nsis.sourceforge.io/Docs/Chapter4.html),
[NSIS process checks](https://nsis.sourceforge.io/Check_whether_your_application_is_running),
[NSIS exit codes and temporary uninstall copy](https://nsis.sourceforge.io/Docs/AppendixD.html),
[Microsoft WebView2 deployment](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution),
[Windows process snapshot API](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-createtoolhelp32snapshot),
[WebView2 platform requirements](https://learn.microsoft.com/en-us/microsoft-edge/webview2/),
[.NET Framework detection](https://learn.microsoft.com/en-us/dotnet/framework/install/how-to-determine-which-versions-are-installed),
[.NET Framework supplied with Windows](https://learn.microsoft.com/en-us/dotnet/framework/get-started/system-requirements),
[Python.NET runtime requirements](https://pythonnet.github.io/pythonnet/python.html),
[Python.NET 3.1.0 project target](https://github.com/pythonnet/pythonnet/blob/v3.1.0/src/runtime/Python.Runtime.csproj),
[Windows ARM emulation](https://learn.microsoft.com/en-us/windows/arm/apps-on-arm-x86-emulation),
[NSIS 3.12 architecture fallback](https://github.com/NSIS-Dev/nsis/blob/v312/Include/x64.nsh).
[Windows Unicode INI behavior](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-writeprivateprofilestringw)
explains why the marker is explicitly encoded rather than created with
`WriteINIStr`.

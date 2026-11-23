; Local Windows installer for an already verified portable payload.
; See windows-installer.md for the build inputs and ownership contract.
Unicode true
RequestExecutionLevel user
ManifestSupportedOS all
ManifestDPIAware true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

!ifndef APP_VERSION
  !error "APP_VERSION is required"
!endif
!ifndef APP_VERSION_QUAD
  !error "APP_VERSION_QUAD is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif
!ifndef PAYLOAD_INSTALL_INCLUDE
  !error "PAYLOAD_INSTALL_INCLUDE is required"
!endif
!ifndef PAYLOAD_UNINSTALL_INCLUDE
  !error "PAYLOAD_UNINSTALL_INCLUDE is required"
!endif
!ifndef PAYLOAD_GUARD_INCLUDE
  !error "PAYLOAD_GUARD_INCLUDE is required"
!endif
!ifndef PAYLOAD_SIZE_KIB
  !error "PAYLOAD_SIZE_KIB is required"
!endif
!ifndef WEBVIEW2_BOOTSTRAPPER
  !error "WEBVIEW2_BOOTSTRAPPER is required; verify its Microsoft signature before building"
!endif

!ifndef APP_NAME
  !define APP_NAME "nd2wsi-viewer"
!endif
!ifndef APP_DISPLAY_NAME
  !define APP_DISPLAY_NAME "${APP_NAME}"
!endif
!ifndef APP_RELEASE_VERSION
  !define APP_RELEASE_VERSION "${APP_VERSION}"
!endif
!define APP_EXE "nd2wsi-viewer.exe"
!ifndef PRODUCT_ID
  !define PRODUCT_ID "nd2wsi-viewer.windows.x64.current-user"
!endif
!define MARKER ".nd2wsi-install.ini"
!ifndef UNINSTALL_KEY
  !define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\nd2wsi-viewer"
!endif
!ifndef PROG_ID
  !define PROG_ID "nd2wsi-viewer.Slide"
!endif
!ifndef CAPABILITIES_ROOT_KEY
  !define CAPABILITIES_ROOT_KEY "Software\nd2wsi-viewer"
!endif
!ifndef CAPABILITIES_KEY
  !define CAPABILITIES_KEY "${CAPABILITIES_ROOT_KEY}\Capabilities"
!endif
!define REGISTERED_APPS_KEY "Software\RegisteredApplications"
!ifndef REGISTERED_APP_NAME
  !define REGISTERED_APP_NAME "${APP_DISPLAY_NAME}"
!endif
!define APP_REG_KEY "Software\Classes\Applications\${APP_EXE}"
!ifndef SHORTCUT_DIR
  !define SHORTCUT_DIR "${APP_DISPLAY_NAME}"
!endif
!ifndef SHORTCUT_NAME
  !define SHORTCUT_NAME "${APP_DISPLAY_NAME}"
!endif
!ifndef SETUP_MUTEX_NAME
  !define SETUP_MUTEX_NAME "Local\nd2wsi-viewer-Setup-current-user"
!endif
!define WEBVIEW2_KEY "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

Name "${APP_DISPLAY_NAME} ${APP_RELEASE_VERSION}"
OutFile "${OUTPUT_FILE}"
!ifdef UNINST_SIGN_PYTHON
  !uninstfinalize '""${UNINST_SIGN_PYTHON}" "${UNINST_SIGN_SCRIPT}" --sign-file "%1" --sign-description "${APP_DISPLAY_NAME} Uninstaller""'
!endif
!ifndef INSTALL_DIR_NAME
  !define INSTALL_DIR_NAME "nd2wsi-viewer"
  InstallDir "$LOCALAPPDATA\Programs\nd2wsi-viewer"
!else
  InstallDir "$LOCALAPPDATA\Programs\${INSTALL_DIR_NAME}"
!endif
BrandingText "${APP_DISPLAY_NAME}"
SetCompressor /SOLID lzma
SetCompressorDictSize 32
SetDatablockOptimize on
CRCCheck on
AllowSkipFiles off
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "${APP_VERSION_QUAD}"
VIAddVersionKey /LANG=1033 "ProductName" "${APP_DISPLAY_NAME}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${APP_RELEASE_VERSION}"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "FileDescription" "${APP_DISPLAY_NAME} Setup"
VIAddVersionKey /LANG=1033 "LegalCopyright" "nd2wsi-viewer contributors"

!ifdef APP_ICON
  !define MUI_ICON "${APP_ICON}"
  !define MUI_UNICON "${APP_ICON}"
!endif
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Install ${APP_DISPLAY_NAME} ${APP_RELEASE_VERSION}"
!ifdef BUNDLED_WEBVIEW2
  !define MUI_WELCOMEPAGE_TEXT "View Nikon ND2, Zeiss CZI, Leica LIF, OME-TIFF / TIFF / QPTIFF, Aperio SVS, SDPC, and ND2SVS microscopy slides with ${APP_DISPLAY_NAME}.$\r$\n$\r$\nSetup installs the app for your Windows account without administrator privileges. Close ${APP_DISPLAY_NAME} before continuing.$\r$\n$\r$\nUse 64-bit Windows 10 or Windows 11 on x64 (or Windows Server 2016 or later with Desktop Experience). ARM64 requires Windows 11 and uses x64 emulation.$\r$\n$\r$\nMicrosoft .NET Framework 4.6.2 or later is required. Microsoft Edge WebView2 Fixed Version Runtime is bundled for offline installation."
!else
  !define MUI_WELCOMEPAGE_TEXT "View Nikon ND2, Zeiss CZI, Leica LIF, OME-TIFF / TIFF / QPTIFF, Aperio SVS, SDPC, and ND2SVS microscopy slides with ${APP_DISPLAY_NAME}.$\r$\n$\r$\nSetup installs the app for your Windows account without administrator privileges. Close ${APP_DISPLAY_NAME} before continuing.$\r$\n$\r$\nUse 64-bit Windows 10 or Windows 11 on x64 (or Windows Server 2016 or later with Desktop Experience). ARM64 requires Windows 11 and uses x64 emulation.$\r$\n$\r$\nMicrosoft .NET Framework 4.6.2 or later is required. If WebView2 Runtime is missing, Setup can download it from Microsoft using an Internet connection."
!endif
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
; Never silently grey out the Install button on the directory page; validate
; path syntax, 64-bit free disk space, and ownership with explicit dialogs on leave.
!define MUI_DIRECTORYPAGE_VERIFYONLEAVE
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE VerifyDirectoryPage
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Open ${APP_DISPLAY_NAME}"
!insertmacro MUI_PAGE_FINISH
!define MUI_UNCONFIRMPAGE_TEXT_TOP "Uninstall ${APP_DISPLAY_NAME} from this folder? Setup removes only the app files and shortcuts it installed. Your images, annotations, caches, and other user files are kept."
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Var GuardError
Var ProcessState
Var WebView2Version
Var UninstallHadErrors
Var RecoveryHadErrors
Var RegistryState
Var MarkerWriteFailed
Var MarkerTempPath
Var UninstallerTempPath
Var UninstallerWriteFailed
Var InitialInstallFresh
Var InitialRegistryAbsent
Var InitialRegistryAttempted
Var InitialRecordError
Var RollbackHadErrors
Var SetupMutex
Var InstallerLogPath
Var ProcessInspectError
Var ProcessCheckedCount
Var ExistingUpgrade
Var UpgradeExitCode
Var WinMajor
Var WinMinor
Var WinBuild

!macro ND2WSI_TRACE PREFIX MESSAGE
  Push "${MESSAGE}"
  Call ${PREFIX}LogInstaller
!macroend

!macro ND2WSI_GUARD_PATH RELATIVE_PATH
  Push "$INSTDIR\${RELATIVE_PATH}"
  Call ${ND2WSI_GUARD_FUNCTION}
  ${If} $GuardError != ""
    Return
  ${EndIf}
!macroend

; Generated includes must name literal relative payload paths, never wildcards.
!macro ND2WSI_DELETE_FILE RELATIVE_PATH
  Push "$INSTDIR\${RELATIVE_PATH}"
  Call un.DeleteOwnedFile
!macroend

!macro ND2WSI_REMOVE_EMPTY_DIR RELATIVE_PATH
  Push "$INSTDIR\${RELATIVE_PATH}"
  Call un.RemoveEmptyOwnedDirectory
!macroend

!macro ND2WSI_WRITE_REGISTRATION STATE
  WriteRegStr HKCU "${UNINSTALL_KEY}" "ProductId" "${PRODUCT_ID}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallState" "${STATE}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_DISPLAY_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "nd2wsi-viewer"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE},0"
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" ${PAYLOAD_SIZE_KIB}
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
!macroend

!macro ND2WSI_REGISTER_EXTENSION EXT
  WriteRegStr HKCU "${CAPABILITIES_KEY}\FileAssociations" "${EXT}" "${PROG_ID}"
  WriteRegStr HKCU "${APP_REG_KEY}\SupportedTypes" "${EXT}" ""
  WriteRegStr HKCU "Software\Classes\${EXT}\OpenWithProgids" "${PROG_ID}" ""
!macroend

!macro ND2WSI_UNREGISTER_EXTENSION EXT
  DeleteRegValue HKCU "Software\Classes\${EXT}\OpenWithProgids" "${PROG_ID}"
  Push "Software\Classes\${EXT}\OpenWithProgids"
  Call un.DeleteKeyIfTrulyEmpty
  Push "Software\Classes\${EXT}"
  Call un.DeleteKeyIfTrulyEmpty
!macroend

; Shared checks exist in both executable contexts. No process is terminated.
!macro DEFINE_SHARED_FUNCTIONS PREFIX
Function ${PREFIX}InitInstallerLog
  Push $R0
  Push $R1
  Push $R2
  StrCpy $R0 0
  ${If} ${Errors}
    StrCpy $R0 1
  ${EndIf}
  ReadEnvStr $InstallerLogPath "ND2WSI_INSTALLER_LOG"
  ReadEnvStr $R1 "TEMP"
  ReadEnvStr $R2 "TMP"
  ClearErrors
  ${If} $R0 != 0
    SetErrors
  ${EndIf}
  !insertmacro ND2WSI_TRACE "${PREFIX}" "InitInstallerLog.temporary_paths nsis_temp=$TEMP env_TEMP=$R1 env_TMP=$R2"
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function ${PREFIX}LogInstaller
  ; Optional diagnostic output only. Preserve every register used here and
  ; the exact incoming NSIS sticky error flag, including a failed FileOpen.
  ; The caller supplies a unique log path with an already existing parent.
  Exch $R0
  Push $R1
  Push $R2
  Push $R3
  StrCpy $R2 0
  ${If} ${Errors}
    StrCpy $R2 1
  ${EndIf}
  ClearErrors
  ${If} $InstallerLogPath != ""
    FileOpen $R1 "$InstallerLogPath" a
    ${IfNot} ${Errors}
      FileSeek $R1 0 END $R3
      ${If} $R3 == 0
        FileWriteUTF16LE /BOM $R1 "nsis_errors=$R2 $R0$\r$\n"
      ${Else}
        FileWriteUTF16LE $R1 "nsis_errors=$R2 $R0$\r$\n"
      ${EndIf}
      FileClose $R1
    ${EndIf}
  ${EndIf}
  ClearErrors
  ${If} $R2 != 0
    SetErrors
  ${EndIf}
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function ${PREFIX}NormalizePath
  ; Normalize a path with GetFullPathNameW and expand 8.3 short names via
  ; GetLongPathNameW (falling back to the nearest existing ancestor when the
  ; leaf does not exist yet). Returns "" on Win32 error or overflow.
  Exch $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  Push $R5
  Push $R6
  Push $R7
  System::Call 'kernel32::GetFullPathNameW(w R0, i ${NSIS_MAX_STRLEN}, w .R1, p 0)i.R2'
  ${If} $R2 == 0
  ${OrIf} $R2 >= ${NSIS_MAX_STRLEN}
    StrCpy $R0 ""
    Goto norm_done
  ${EndIf}
  StrCpy $R3 $R1
  StrCpy $R5 ""
  ${Do}
    System::Call 'kernel32::GetLongPathNameW(w R3, w .R4, i ${NSIS_MAX_STRLEN})i.R2'
    ${If} $R2 > 0
    ${AndIf} $R2 < ${NSIS_MAX_STRLEN}
      ${If} $R5 != ""
        StrCpy $R6 $R4 "" -1
        ${If} $R6 == "\"
          StrCpy $R4 $R4 -1
        ${EndIf}
        StrCpy $R1 "$R4$R5"
      ${Else}
        StrCpy $R1 $R4
      ${EndIf}
      ${ExitDo}
    ${EndIf}
    ${GetParent} "$R3" $R4
    ${If} $R4 == ""
    ${OrIf} $R4 == $R3
      ${ExitDo}
    ${EndIf}
    StrLen $R7 $R4
    StrCpy $R6 $R3 "" $R7
    StrCpy $R7 $R6 1
    ${If} $R7 != "\"
      StrCpy $R6 "\$R6"
    ${EndIf}
    StrCpy $R5 "$R6$R5"
    StrCpy $R3 $R4
  ${Loop}
  StrCpy $R6 $R1 "" -1
  ${If} $R6 == "\"
    StrLen $R2 $R1
    ${If} $R2 > 3
      StrCpy $R1 $R1 -1
    ${EndIf}
  ${EndIf}
  StrCpy $R0 $R1
  norm_done:
  Pop $R7
  Pop $R6
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Exch $R0
FunctionEnd

Function ${PREFIX}IsNameSurrogateReparse
  ; Inspect a path with FILE_ATTRIBUTE_REPARSE_POINT set. Returns "1" if its
  ; reparse tag is a name surrogate (symlink, junction, mount point; bit 29 =
  ; 0x20000000) or if FindFirstFileW fails (fail-closed); returns "0" for
  ; non-surrogate tags such as OneDrive Cloud Files placeholders.
  Exch $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  StrCpy $R4 "1"
  StrCpy $R1 $R0 "" -1
  ${If} $R1 == "\"
    StrLen $R2 $R0
    ${If} $R2 > 3
      StrCpy $R0 $R0 -1
    ${EndIf}
  ${EndIf}
  System::Alloc 592
  Pop $R1
  ${If} $R1 != 0
    System::Call 'kernel32::FindFirstFileW(w R0, p R1)p.R2'
    ${If} $R2 != -1
      System::Call 'kernel32::FindClose(p R2)'
      IntOp $R3 $R1 + 36
      System::Call '*$R3(i.R2)'
      IntOp $R2 $R2 & 0x20000000
      ${If} $R2 == 0
        StrCpy $R4 "0"
      ${EndIf}
    ${EndIf}
    System::Free $R1
  ${EndIf}
  StrCpy $R0 $R4
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Exch $R0
FunctionEnd

Function ${PREFIX}IsOsOwnedAncestor
  ; Return "1" if a normalized ancestor path matches an OS- or shell-managed
  ; profile root ($LOCALAPPDATA, $APPDATA, $PROFILE, $SMPROGRAMS, $DESKTOP, or
  ; the parent of $PROFILE such as C:\Users), else "0".
  Exch $R0
  Push $R1
  Push $R2
  StrCpy $R1 "0"
  ${If} $R0 == "$LOCALAPPDATA"
  ${OrIf} $R0 == "$APPDATA"
  ${OrIf} $R0 == "$PROFILE"
  ${OrIf} $R0 == "$SMPROGRAMS"
  ${OrIf} $R0 == "$DESKTOP"
    StrCpy $R1 "1"
  ${Else}
    ${GetParent} "$PROFILE" $R2
    ${If} $R2 != ""
    ${AndIf} $R0 == $R2
      StrCpy $R1 "1"
    ${EndIf}
  ${EndIf}
  ${If} $R1 == "0"
    Push "$LOCALAPPDATA"
    Call ${PREFIX}NormalizePath
    Pop $R2
    ${If} $R2 != ""
    ${AndIf} $R0 == $R2
      StrCpy $R1 "1"
    ${EndIf}
  ${EndIf}
  ${If} $R1 == "0"
    Push "$PROFILE"
    Call ${PREFIX}NormalizePath
    Pop $R2
    ${If} $R2 != ""
      ${If} $R0 == $R2
        StrCpy $R1 "1"
      ${Else}
        ${GetParent} "$R2" $R2
        ${If} $R2 != ""
        ${AndIf} $R0 == $R2
          StrCpy $R1 "1"
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ${If} $R1 == "0"
    Push "$SMPROGRAMS"
    Call ${PREFIX}NormalizePath
    Pop $R2
    ${If} $R2 != ""
    ${AndIf} $R0 == $R2
      StrCpy $R1 "1"
    ${EndIf}
  ${EndIf}
  ${If} $R1 == "0"
    Push "$DESKTOP"
    Call ${PREFIX}NormalizePath
    Pop $R2
    ${If} $R2 != ""
    ${AndIf} $R0 == $R2
      StrCpy $R1 "1"
    ${EndIf}
  ${EndIf}
  ${If} $R1 == "0"
    Push "$APPDATA"
    Call ${PREFIX}NormalizePath
    Pop $R2
    ${If} $R2 != ""
    ${AndIf} $R0 == $R2
      StrCpy $R1 "1"
    ${EndIf}
  ${EndIf}
  StrCpy $R0 $R1
  Pop $R2
  Pop $R1
  Exch $R0
FunctionEnd

Function ${PREFIX}MoveFileExWithRetry
  ; Atomically replace a staged file (MOVEFILE_REPLACE_EXISTING|WRITE_THROUGH=9),
  ; retrying transient Windows Defender / AV locks (ERROR_ACCESS_DENIED=5,
  ; ERROR_SHARING_VIOLATION=32) up to 10 x 150 ms.
  ; Stack in: source, destination. Stack out: result, win32_error (top).
  Exch $R1 ; destination
  Exch
  Exch $R0 ; source
  Push $R2
  Push $R3
  Push $R4
  StrCpy $R4 0
  ${Do}
    System::Call 'kernel32::MoveFileExW(w R0, w R1, i 9)i.R2 ?e'
    Pop $R3
    ${If} $R2 != 0
      ${ExitDo}
    ${EndIf}
    ${If} $R3 != 5
    ${AndIf} $R3 != 32
      ${ExitDo}
    ${EndIf}
    IntOp $R4 $R4 + 1
    ${If} $R4 > 10
      ${ExitDo}
    ${EndIf}
    Sleep 150
  ${Loop}
  StrCpy $R0 $R2
  StrCpy $R1 $R3
  Pop $R4
  Pop $R3
  Pop $R2
  Exch $R0
  Exch
  Exch $R1
FunctionEnd

Function ${PREFIX}DeleteFileWithRetry
  ; Delete a single file, retrying transient Windows Defender / AV locks
  ; (ERROR_ACCESS_DENIED=5, ERROR_SHARING_VIOLATION=32) up to 10 x 150 ms.
  ; Stack in: path. Stack out: result, win32_error (top).
  Exch $R0 ; path
  Push $R1
  Push $R2
  Push $R3
  StrCpy $R3 0
  ${Do}
    System::Call 'kernel32::DeleteFileW(w R0)i.R1 ?e'
    Pop $R2
    ${If} $R1 != 0
      ${ExitDo}
    ${EndIf}
    ${If} $R2 != 5
    ${AndIf} $R2 != 32
      ${ExitDo}
    ${EndIf}
    IntOp $R3 $R3 + 1
    ${If} $R3 > 10
      ${ExitDo}
    ${EndIf}
    Sleep 150
  ${Loop}
  StrCpy $R0 $R1
  StrCpy $R1 $R2
  Pop $R3
  Pop $R2
  Exch $R1
  Exch
  Exch $R0
  Exch
FunctionEnd

Function ${PREFIX}CheckPathChain
  ; Check the leaf and user-owned ancestors up to OS/profile/volume mount roots.
  ; This rejects directory junctions and file symlinks under user-owned segments
  ; without falsely blocking redirected profiles, NTFS volume mount points, or
  ; ACL-restricted OS ancestors such as C:\Users.
  Exch $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  Push $R5
  Push $R6
  ; NSIS GetFullPathName can clear its output for a nonexistent leaf.
  ; Use lexical Win32 normalization so every existing ancestor is still
  ; inspected even when the destination or a shipped child does not exist.
  System::Call 'kernel32::GetFullPathNameW(w R0, i ${NSIS_MAX_STRLEN}, w .R1, p 0)i.R2 ?e'
  Pop $R3
  ${If} $R2 == 0
  ${OrIf} $R2 >= ${NSIS_MAX_STRLEN}
    StrCpy $GuardError "Setup could not resolve this path:$\r$\n$R0$\r$\n$\r$\nChoose a shorter, valid app folder and try again."
    !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPathChain.normalize_failed path=$R0 returned_length=$R2 win32_error=$R3 reason=$GuardError"
    Goto path_done
  ${EndIf}
  StrCpy $R0 $R1
  StrCpy $R2 $R0 "" -1
  ${If} $R2 == "\"
    StrLen $R3 $R0
    ${If} $R3 > 3
      StrCpy $R0 $R0 -1
    ${EndIf}
  ${EndIf}
  ; Locate the nearest NTFS volume mount point so mounted volumes are treated
  ; as OS mount roots when walking ancestors above the target leaf.
  StrCpy $R6 $R0
  StrCpy $R5 ""
  ${Do}
    System::Call 'kernel32::GetVolumePathNameW(w R6, w .R5, i ${NSIS_MAX_STRLEN})i.R2'
    ${If} $R2 != 0
      ${ExitDo}
    ${EndIf}
    StrCpy $R5 ""
    ${GetParent} "$R6" $R2
    ${If} $R2 == ""
    ${OrIf} $R2 == $R6
      ${ExitDo}
    ${EndIf}
    StrCpy $R6 $R2
  ${Loop}
  ${If} $R5 != ""
    StrCpy $R2 $R5 "" -1
    ${If} $R2 == "\"
      StrLen $R3 $R5
      ${If} $R3 > 3
        StrCpy $R5 $R5 -1
      ${EndIf}
    ${EndIf}
  ${EndIf}
  StrCpy $R4 0
  ${Do}
    ${If} $R4 != 0
      ${If} $R5 != ""
      ${AndIf} $R0 == $R5
        !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPathChain.stop_volume_root path=$R0"
        ${ExitDo}
      ${EndIf}
      Push "$R0"
      Call ${PREFIX}IsOsOwnedAncestor
      Pop $R2
      ${If} $R2 != 0
        !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPathChain.stop_os_ancestor path=$R0"
        ${ExitDo}
      ${EndIf}
    ${EndIf}
    System::Call 'kernel32::GetFileAttributesW(w R0)i.R1 ?e'
    Pop $R3
    ${If} $R1 == -1
      ${If} $R3 != 2
      ${AndIf} $R3 != 3
        ${If} $R4 != 0
        ${AndIf} $R3 == 5
          !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPathChain.ancestor_access_denied path=$R0 win32_error=$R3"
          ${ExitDo}
        ${EndIf}
        StrCpy $GuardError "Setup could not safely inspect this path:$\r$\n$R0$\r$\n$\r$\nCheck folder access and try again."
        !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPathChain.reject path=$R0 attributes=$R1 win32_error=$R3 reason=$GuardError"
        ${ExitDo}
      ${EndIf}
    ${Else}
      IntOp $R2 $R1 & 0x400
      ${If} $R2 != 0
        Push "$R0"
        Call ${PREFIX}IsNameSurrogateReparse
        Pop $R2
      ${EndIf}
      ${If} $R2 != 0
        StrCpy $GuardError "Setup found a linked file or folder:$\r$\n$R0$\r$\n$\r$\nRestore the original app folder before reinstalling or uninstalling. No app files have been changed."
        !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPathChain.reject path=$R0 attributes=$R1 reason=$GuardError"
        ${ExitDo}
      ${EndIf}
    ${EndIf}
    ${GetParent} "$R0" $R2
    ${If} $R2 == ""
    ${OrIf} $R2 == $R0
      ${ExitDo}
    ${EndIf}
    StrCpy $R0 $R2
    StrCpy $R4 1
  ${Loop}
  path_done:
  Pop $R6
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function ${PREFIX}CheckLeafPath
  ; Inspect a single path under an already-verified $INSTDIR without
  ; redundantly re-walking every ancestor up to the drive root.
  Exch $R0
  Push $R1
  Push $R2
  Push $R3
  System::Call 'kernel32::GetFileAttributesW(w R0)i.R1 ?e'
  Pop $R3
  ${If} $R1 == -1
    ${If} $R3 != 2
    ${AndIf} $R3 != 3
      StrCpy $GuardError "Setup could not safely inspect this path:$\r$\n$R0$\r$\n$\r$\nCheck folder access and try again."
      !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckLeafPath.reject path=$R0 attributes=$R1 win32_error=$R3 reason=$GuardError"
    ${EndIf}
  ${Else}
    IntOp $R2 $R1 & 0x400
    ${If} $R2 != 0
      Push "$R0"
      Call ${PREFIX}IsNameSurrogateReparse
      Pop $R2
    ${EndIf}
    ${If} $R2 != 0
      StrCpy $GuardError "Setup found a linked file or folder:$\r$\n$R0$\r$\n$\r$\nRestore the original app folder before reinstalling or uninstalling. No app files have been changed."
      !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckLeafPath.reject path=$R0 attributes=$R1 reason=$GuardError"
    ${EndIf}
  ${EndIf}
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function ${PREFIX}CheckPayloadPaths
  !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPayloadPaths.begin instdir=$INSTDIR"
  StrCpy $GuardError ""
  Push "$INSTDIR"
  Call ${PREFIX}CheckPathChain
  ${If} $GuardError != ""
    Return
  ${EndIf}
  ; If $INSTDIR does not exist yet, none of the payload children can exist or be
  ; junctions; skip thousands of redundant Win32 metadata queries on the UI thread.
  Push $R0
  Push $R1
  Push $R2
  StrCpy $R0 "$INSTDIR"
  System::Call 'kernel32::GetFileAttributesW(w R0)i.R1 ?e'
  Pop $R2
  ${If} $R1 == -1
    ${If} $R2 == 2
    ${OrIf} $R2 == 3
      Pop $R2
      Pop $R1
      Pop $R0
      !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPayloadPaths.complete instdir=$INSTDIR missing_root=1 guard="
      Return
    ${EndIf}
  ${EndIf}
  Pop $R2
  Pop $R1
  Pop $R0
  !define ND2WSI_GUARD_FUNCTION "${PREFIX}CheckLeafPath"
  !insertmacro ND2WSI_GUARD_PATH "${MARKER}"
  !insertmacro ND2WSI_GUARD_PATH "Uninstall.exe"
  !include "${PAYLOAD_GUARD_INCLUDE}"
  !undef ND2WSI_GUARD_FUNCTION
  !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPayloadPaths.complete instdir=$INSTDIR guard=$GuardError"
FunctionEnd

Function ${PREFIX}AcquireSetupMutex
  Push $0
  Push $1
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "${SETUP_MUTEX_NAME}")p.r0 ?e'
  Pop $1
  StrCpy $SetupMutex $0
  !insertmacro ND2WSI_TRACE "${PREFIX}" "AcquireSetupMutex handle=$0 win32_error=$1"
  ${If} $0 == 0
  ${OrIf} $1 == 183
    Pop $1
    Pop $0
    !insertmacro ND2WSI_TRACE "${PREFIX}" "MessageBox phase=${PREFIX}AcquireSetupMutex message=Another ${APP_DISPLAY_NAME} installer or uninstaller is running. Close it before continuing."
    MessageBox MB_OK|MB_ICONEXCLAMATION "Another ${APP_DISPLAY_NAME} installer or uninstaller is running. Close it before continuing." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "${PREFIX}" "Abort phase=${PREFIX}AcquireSetupMutex guard=$GuardError"
    Abort
  ${EndIf}
  Pop $1
  Pop $0
FunctionEnd

Function ${PREFIX}FindRunningApp
  ; The default Unicode NSIS stub is x86. PROCESSENTRY32W is 556 bytes,
  ; with th32ProcessID at byte 8 and szExeFile at byte 36. A process snapshot
  ; includes x64/ARM64 apps. Scope matching to $INSTDIR\${APP_EXE} via
  ; OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + QueryFullProcessImageNameW
  ; so public and SLACS installations do not collide, failing safe as running
  ; when a matching executable's image path cannot be queried.
  ; Return ProcessState: 0 absent, 1 present, 2 inspection failed.
  Push $R0
  Push $5
  Push $6
  Push $7
  Push $8
  Push $9
  StrCpy $ProcessState 2
  StrCpy $ProcessCheckedCount 0
  StrCpy $R0 ""
  ${If} $INSTDIR != ""
    Push "$INSTDIR\${APP_EXE}"
    Call ${PREFIX}NormalizePath
    Pop $R0
  ${EndIf}
  System::Call 'kernel32::CreateToolhelp32Snapshot(i 2, i 0)p.r0 ?e'
  Pop $ProcessInspectError
  !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.snapshot handle=$0 win32_error=$ProcessInspectError"
  ${If} $0 == -1
    Goto find_done
  ${EndIf}
  System::Alloc 556
  Pop $1
  ${If} $1 == 0
    !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.allocate_failed bytes=556"
    System::Call 'kernel32::CloseHandle(p r0)'
    Goto find_done
  ${EndIf}
  System::Call '*$1(i 556)'
  System::Call 'kernel32::Process32FirstW(p r0, p r1)i.r2 ?e'
  Pop $ProcessInspectError
  !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.first result=$2 win32_error=$ProcessInspectError buffer=$1 size=556"
  ${If} $2 != 0
    StrCpy $ProcessState 0
    ${Do}
      IntOp $ProcessCheckedCount $ProcessCheckedCount + 1
      System::Call '*$1(i, i, i.r5, p, i, i, i, i, i, &w260.r3)'
      ${If} $3 == "${APP_EXE}"
        ${If} $R0 == ""
          StrCpy $ProcessState 1
          !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.match_unscoped pid=$5 name=$3"
          ${ExitDo}
        ${EndIf}
        System::Call 'kernel32::OpenProcess(i 0x1000, i 0, i r5)p.r6 ?e'
        Pop $7
        ${If} $6 == 0
          ${If} $7 != 87
            StrCpy $ProcessState 1
            !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.match_fallback_open pid=$5 win32_error=$7"
            ${ExitDo}
          ${EndIf}
        ${Else}
          StrCpy $8 ${NSIS_MAX_STRLEN}
          System::Call 'kernel32::QueryFullProcessImageNameW(p r6, i 0, w .r7, *i r8r8)i.r9'
          System::Call 'kernel32::CloseHandle(p r6)'
          ${If} $9 == 0
          ${OrIf} $7 == ""
            StrCpy $ProcessState 1
            !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.match_fallback_query pid=$5"
            ${ExitDo}
          ${EndIf}
          Push "$7"
          Call ${PREFIX}NormalizePath
          Pop $8
          ${If} $8 != ""
            StrCpy $7 $8
          ${EndIf}
          ${If} $7 == $R0
            StrCpy $ProcessState 1
            !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.match pid=$5 path=$7"
            ${ExitDo}
          ${Else}
            !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.other_install pid=$5 path=$7 expected=$R0"
          ${EndIf}
        ${EndIf}
      ${EndIf}
      System::Call 'kernel32::Process32NextW(p r0, p r1)i.r2 ?e'
      Pop $4
      ${If} $2 == 0
        !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.next_end result=$2 win32_error=$4 checked=$ProcessCheckedCount"
        ${If} $4 != 18
          StrCpy $ProcessState 2
        ${EndIf}
        ${ExitDo}
      ${EndIf}
    ${Loop}
  ${EndIf}
  System::Free $1
  System::Call 'kernel32::CloseHandle(p r0)'
  find_done:
  !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.complete state=$ProcessState checked=$ProcessCheckedCount"
  Pop $9
  Pop $8
  Pop $7
  Pop $6
  Pop $5
  Pop $R0
FunctionEnd

Function ${PREFIX}RequireAppClosed
  retry:
  Call ${PREFIX}FindRunningApp
  !insertmacro ND2WSI_TRACE "${PREFIX}" "RequireAppClosed state=$ProcessState"
  ${If} $ProcessState == 0
    Return
  ${EndIf}
  ${If} $ProcessState == 1
    !insertmacro ND2WSI_TRACE "${PREFIX}" "MessageBox phase=${PREFIX}RequireAppClosed message=${APP_DISPLAY_NAME} is running. Save your work and close every ${APP_DISPLAY_NAME} window, then choose Retry."
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "${APP_DISPLAY_NAME} is running. Save your work and close every ${APP_DISPLAY_NAME} window, then choose Retry." /SD IDCANCEL IDRETRY retry
  ${Else}
    !insertmacro ND2WSI_TRACE "${PREFIX}" "MessageBox phase=${PREFIX}RequireAppClosed message=Setup could not check whether ${APP_DISPLAY_NAME} is running. Close the app, then choose Retry."
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Setup could not check whether ${APP_DISPLAY_NAME} is running. Close the app, then choose Retry." /SD IDCANCEL IDRETRY retry
  ${EndIf}
  SetErrorLevel 2
  !insertmacro ND2WSI_TRACE "${PREFIX}" "Abort phase=${PREFIX}RequireAppClosed guard=$GuardError"
  Abort
FunctionEnd

Function ${PREFIX}WriteOwnershipMarker
  ; Write a complete UTF-16LE INI with a BOM. WriteINIStr on a new/legacy
  ; ANSI file would lose install-path characters outside the ANSI code page.
  ; The temporary file is created in the same folder for an atomic rename;
  ; an existing marker is never truncated before the new one is complete.
  Exch $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  !insertmacro ND2WSI_TRACE "${PREFIX}" "WriteOwnershipMarker.begin instdir=$INSTDIR state=$R0"
  StrCpy $MarkerWriteFailed 0
  StrCpy $MarkerTempPath ""
  ClearErrors
  GetTempFileName $R1 "$INSTDIR"
  ${If} ${Errors}
  ${OrIf} $R1 == ""
    StrCpy $MarkerWriteFailed 1
    System::Call 'kernel32::GetLastError()i.R4'
    !insertmacro ND2WSI_TRACE "${PREFIX}" "WriteOwnershipMarker.GetTempFileName_failed win32_error_after_command=$R4"
    Goto marker_done
  ${EndIf}
  StrCpy $MarkerTempPath $R1
  !insertmacro ND2WSI_TRACE "${PREFIX}" "WriteOwnershipMarker.temp path=$R1"
  FileOpen $R2 "$R1" w
  ${If} ${Errors}
    StrCpy $MarkerWriteFailed 1
    System::Call 'kernel32::GetLastError()i.R4'
    !insertmacro ND2WSI_TRACE "${PREFIX}" "WriteOwnershipMarker.FileOpen_failed path=$R1 win32_error_after_command=$R4"
    Goto marker_cleanup
  ${EndIf}
  FileWriteUTF16LE /BOM $R2 "[Installation]$\r$\n"
  FileWriteUTF16LE $R2 "ProductId=${PRODUCT_ID}$\r$\nVersion=${APP_VERSION}$\r$\n"
  FileWriteUTF16LE $R2 "InstallLocation=$INSTDIR$\r$\nUninstallState=$R0$\r$\n"
  ${If} ${Errors}
    StrCpy $MarkerWriteFailed 1
  ${EndIf}
  FileClose $R2
  ${If} ${Errors}
    StrCpy $MarkerWriteFailed 1
  ${EndIf}
  ${If} $MarkerWriteFailed != 0
    Goto marker_cleanup
  ${EndIf}
  Push "$R1"
  Push "$INSTDIR\${MARKER}"
  Call ${PREFIX}MoveFileExWithRetry
  Pop $R4
  Pop $R3
  !insertmacro ND2WSI_TRACE "${PREFIX}" "WriteOwnershipMarker.MoveFileEx result=$R3 win32_error=$R4 source=$R1 destination=$INSTDIR\${MARKER}"
  ${If} $R3 == 0
    StrCpy $MarkerWriteFailed 1
    DetailPrint "Could not replace the ownership marker (Windows error $R4)."
    Goto marker_cleanup
  ${EndIf}
  StrCpy $MarkerTempPath ""
  FlushINI "$INSTDIR\${MARKER}"
  ReadINIStr $R3 "$INSTDIR\${MARKER}" "Installation" "ProductId"
  ${If} $R3 != "${PRODUCT_ID}"
    StrCpy $MarkerWriteFailed 1
  ${EndIf}
  ReadINIStr $R3 "$INSTDIR\${MARKER}" "Installation" "Version"
  ${If} $R3 != "${APP_VERSION}"
    StrCpy $MarkerWriteFailed 1
  ${EndIf}
  ReadINIStr $R3 "$INSTDIR\${MARKER}" "Installation" "UninstallState"
  ${If} $R3 != $R0
    StrCpy $MarkerWriteFailed 1
  ${EndIf}
  ReadINIStr $R3 "$INSTDIR\${MARKER}" "Installation" "InstallLocation"
  ${If} $R3 != ""
    Push "$R3"
    Call ${PREFIX}NormalizePath
    Pop $R3
  ${EndIf}
  ${If} $R3 != $INSTDIR
    StrCpy $MarkerWriteFailed 1
  ${EndIf}
  Goto marker_done
  marker_cleanup:
  Push "$R1"
  Call ${PREFIX}DeleteFileWithRetry
  Pop $R4
  Pop $R3
  ${If} $R3 == 0
  ${AndIf} $R4 != 2
  ${AndIf} $R4 != 3
    DetailPrint "Could not remove temporary ownership record (Windows error $R4): $R1"
  ${Else}
    StrCpy $MarkerTempPath ""
  ${EndIf}
  marker_done:
  !insertmacro ND2WSI_TRACE "${PREFIX}" "WriteOwnershipMarker.complete failed=$MarkerWriteFailed pending_temp=$MarkerTempPath instdir=$INSTDIR"
  ${If} $MarkerWriteFailed != 0
    DetailPrint "Could not write and verify the Unicode ownership marker."
  ${EndIf}
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function ${PREFIX}CheckOwnedDirectory
  !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckOwnedDirectory.begin instdir=$INSTDIR"
  StrCpy $GuardError ""
  ; A fresh installation is allowed to target a nonexistent folder. Keep
  ; the chosen path intact unless Win32 normalization succeeds completely.
  Push "$INSTDIR"
  Call ${PREFIX}NormalizePath
  Pop $0
  ${If} $0 == ""
    StrCpy $GuardError "Setup could not resolve the installation folder:$\r$\n$INSTDIR$\r$\n$\r$\nChoose a shorter, valid app folder and try again."
    !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckOwnedDirectory.normalize_failed instdir=$INSTDIR reason=$GuardError"
    Return
  ${EndIf}
  StrCpy $INSTDIR $0
  !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckOwnedDirectory.normalized instdir=$INSTDIR"
  Push "$INSTDIR"
  Call ${PREFIX}CheckPathChain
  ${If} $GuardError != ""
    Return
  ${EndIf}
  ${GetRoot} "$INSTDIR" $0
  ${If} $INSTDIR == $0
  ${OrIf} $INSTDIR == "$0\"
    StrCpy $GuardError "Choose a dedicated app folder, not the root of a drive."
    Return
  ${EndIf}
  ReadINIStr $0 "$INSTDIR\${MARKER}" "Installation" "ProductId"
  ReadINIStr $1 "$INSTDIR\${MARKER}" "Installation" "Version"
  ReadRegStr $2 HKCU "${UNINSTALL_KEY}" "InstallLocation"
  ${If} $2 != ""
    Push "$2"
    Call ${PREFIX}NormalizePath
    Pop $2
  ${EndIf}
  !if "x${PREFIX}" == "x"
    StrCpy $ExistingUpgrade 0
    ReadINIStr $3 "$INSTDIR\${MARKER}" "Installation" "UninstallState"
    ReadRegStr $4 HKCU "${UNINSTALL_KEY}" "ProductId"
    ${If} $0 == "${PRODUCT_ID}"
    ${AndIf} $2 == $INSTDIR
      ${If} $1 == "${APP_VERSION}"
        ; Same-version reinstall in the registered folder.
      ${ElseIf} $1 != ""
      ${AndIf} $3 == ""
      ${AndIf} $4 == "${PRODUCT_ID}"
      ${AndIf} ${FileExists} "$INSTDIR\Uninstall.exe"
        StrCpy $ExistingUpgrade 1
      ${Else}
        StrCpy $GuardError "This folder is not a registered ${APP_DISPLAY_NAME} ${APP_VERSION} installation. Choose a new, empty folder, or the folder used by this version of Setup."
      ${EndIf}
    ${Else}
      StrCpy $GuardError "This folder is not a registered ${APP_DISPLAY_NAME} ${APP_VERSION} installation. Choose a new, empty folder, or the folder used by this version of Setup."
    ${EndIf}
  !else
    ${If} $0 != "${PRODUCT_ID}"
    ${OrIf} $1 != "${APP_VERSION}"
    ${OrIf} $2 != $INSTDIR
      StrCpy $GuardError "This folder is not a registered ${APP_DISPLAY_NAME} ${APP_VERSION} installation. Choose a new, empty folder, or the folder used by this version of Setup."
    ${EndIf}
  !endif
  !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckOwnedDirectory.complete marker_product=$0 marker_version=$1 registered_path=$2 instdir=$INSTDIR guard=$GuardError"
FunctionEnd
!macroend

!insertmacro DEFINE_SHARED_FUNCTIONS ""
!insertmacro DEFINE_SHARED_FUNCTIONS "un."

; PROCESSENTRY32W above intentionally targets NSIS's portable x86 stub.
!if ${NSIS_PTR_SIZE} != 4
  !error "Compile this installer with the x86 Unicode NSIS stub"
!endif

Function .onInit
  Call InitInstallerLog
  !insertmacro ND2WSI_TRACE "" ".onInit.begin version=${APP_VERSION} instdir=$INSTDIR exepath=$EXEPATH"
  SetShellVarContext current
  SetRegView 64
  ${If} $INSTDIR == ""
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${INSTALL_DIR_NAME}"
  ${EndIf}
  GetWinVer $WinMajor Major
  GetWinVer $WinMinor Minor
  GetWinVer $WinBuild Build
  !insertmacro ND2WSI_TRACE "" ".onInit.windows_version major=$WinMajor minor=$WinMinor build=$WinBuild"
  ${IfNot} ${AtLeastWin10}
  ${OrIfNot} ${AtLeastBuild} 10240
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=.onInit message=${APP_DISPLAY_NAME} requires 64-bit Windows 10 or Windows 11 on x64 (or Windows Server 2016 or later with Desktop Experience). ARM64 requires Windows 11. Detected Windows $WinMajor.$WinMinor (build $WinBuild)."
    MessageBox MB_OK|MB_ICONSTOP "${APP_DISPLAY_NAME} requires 64-bit Windows 10 or Windows 11 on x64 (or Windows Server 2016 or later with Desktop Experience). ARM64 requires Windows 11.$\r$\n$\r$\nDetected Windows $WinMajor.$WinMinor (build $WinBuild)." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=.onInit guard=$GuardError"
    Abort
  ${EndIf}
  ReadRegStr $0 HKLM "Software\Microsoft\Windows NT\CurrentVersion" "InstallationType"
  !insertmacro ND2WSI_TRACE "" ".onInit.installation_type value=$0"
  ${If} $0 == "Server Core"
  ${OrIf} $0 == "Nano Server"
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=.onInit message=${APP_DISPLAY_NAME} requires a Windows desktop shell (Windows 10/11 or Windows Server with Desktop Experience). Headless Server Core editions are not supported."
    MessageBox MB_OK|MB_ICONSTOP "${APP_DISPLAY_NAME} requires a Windows desktop shell (Windows 10/11 or Windows Server with Desktop Experience). Headless Server Core editions are not supported." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=.onInit guard=$GuardError"
    Abort
  ${EndIf}
  ${IfNot} ${IsNativeAMD64}
  ${AndIfNot} ${IsNativeARM64}
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=.onInit message=This installer requires an x64 or ARM64 computer. 32-bit Windows is not supported."
    MessageBox MB_OK|MB_ICONSTOP "This installer requires an x64 or ARM64 computer. 32-bit Windows is not supported." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=.onInit guard=$GuardError"
    Abort
  ${EndIf}
  ${If} ${IsNativeARM64}
  ${AndIfNot} ${AtLeastWin11}
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=.onInit message=This x64 app requires Windows 11 on an ARM64 computer. Windows 10 on ARM64 cannot run x64 apps."
    MessageBox MB_OK|MB_ICONSTOP "This x64 app requires Windows 11 on an ARM64 computer. Windows 10 on ARM64 cannot run x64 apps." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=.onInit guard=$GuardError"
    Abort
  ${EndIf}
  ; NSIS 3.12 x64.nsh falls back to native machine data if the older
  ; Windows 10 system does not expose IsWow64Process2. Do not replace these
  ; native-architecture checks with a mandatory call to that newer API.
  !insertmacro ND2WSI_TRACE "" ".onInit.os_architecture_passed"
  Call RequireDotNetFramework
  Call AcquireSetupMutex
  !insertmacro ND2WSI_TRACE "" ".onInit.complete"
FunctionEnd

Function .onInstFailed
  !insertmacro ND2WSI_TRACE "" ".onInstFailed instdir=$INSTDIR"
FunctionEnd

Function .onInstSuccess
  !insertmacro ND2WSI_TRACE "" ".onInstSuccess instdir=$INSTDIR"
FunctionEnd

Function RequireDotNetFramework
  ; Match the bundled pywebview WinForms/WebView2 prerequisite. Framework
  ; release numbers, rather than an arbitrary Windows build, are decisive.
  SetRegView 64
  ReadRegDWORD $0 HKLM "Software\Microsoft\NET Framework Setup\NDP\v4\Full" "Release"
  !insertmacro ND2WSI_TRACE "" "RequireDotNetFramework.view64 release=$0"
  ${If} $0 >= 394802
    Return
  ${EndIf}
  SetRegView 32
  ReadRegDWORD $0 HKLM "Software\Microsoft\NET Framework Setup\NDP\v4\Full" "Release"
  !insertmacro ND2WSI_TRACE "" "RequireDotNetFramework.view32 release=$0"
  SetRegView 64
  ${If} $0 < 394802
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=RequireDotNetFramework message=${APP_DISPLAY_NAME} requires Microsoft .NET Framework 4.6.2 or later. Install Microsoft .NET Framework 4.6.2 (or 4.8 on Windows 10 version 1607 and later), then run Setup again."
    MessageBox MB_OK|MB_ICONSTOP "${APP_DISPLAY_NAME} requires Microsoft .NET Framework 4.6.2 or later. Install Microsoft .NET Framework 4.6.2 (or 4.8 on Windows 10 version 1607 and later), then run Setup again." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=RequireDotNetFramework guard=$GuardError"
    Abort
  ${EndIf}
FunctionEnd

Function CheckInstallDirectory
  !insertmacro ND2WSI_TRACE "" "CheckInstallDirectory.begin instdir=$INSTDIR"
  StrCpy $InitialInstallFresh 0
  StrCpy $ExistingUpgrade 0
  Call CheckPayloadPaths
  ${If} $GuardError != ""
    Return
  ${EndIf}
  Call CheckOwnedDirectory
  ${If} $GuardError == ""
    !insertmacro ND2WSI_TRACE "" "CheckInstallDirectory.existing_owned instdir=$INSTDIR upgrade=$ExistingUpgrade"
    Return
  ${EndIf}
  ; A registered installation cannot silently be moved or overwritten by a
  ; second one. Uninstall it first; the user's data is retained.
  ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
  ${If} $0 != ""
    StrCpy $GuardError "${APP_DISPLAY_NAME} is already registered in:$\r$\n$0$\r$\n$\r$\nChoose that folder to reinstall or upgrade, or uninstall the existing app first."
    Return
  ${EndIf}
  ${GetRoot} "$INSTDIR" $0
  ${If} $INSTDIR == $0
  ${OrIf} $INSTDIR == "$0\"
    Return
  ${EndIf}
  ; Reject a file or directory junction at the chosen destination.
  StrCpy $1 "$INSTDIR"
  System::Call 'kernel32::GetFileAttributesW(w r1)i.r0'
  !insertmacro ND2WSI_TRACE "" "CheckInstallDirectory.attributes value=$0"
  ${If} $0 != -1
    IntOp $1 $0 & 0x400
    ${If} $1 != 0
      Push "$INSTDIR"
      Call IsNameSurrogateReparse
      Pop $1
    ${EndIf}
    IntOp $2 $0 & 0x10
    ${If} $1 != 0
    ${OrIf} $2 == 0
      StrCpy $GuardError "Choose a normal, empty folder for the app. A file or linked folder cannot be used."
      Return
    ${EndIf}
  ${EndIf}
  StrCpy $3 $0
  ClearErrors
  FindFirst $0 $1 "$INSTDIR\*"
  ${If} ${Errors}
  ${AndIf} $3 != -1
    StrCpy $GuardError "Setup could not inspect this folder's contents. Choose a folder with readable contents and try again."
    Return
  ${EndIf}
  ${DoWhile} $1 != ""
    ${If} $1 != "."
    ${AndIf} $1 != ".."
      !insertmacro ND2WSI_TRACE "" "CheckInstallDirectory.nonempty first_entry=$1 guard=$GuardError"
      FindClose $0
      Return
    ${EndIf}
    FindNext $0 $1
  ${Loop}
  FindClose $0
  StrCpy $InitialInstallFresh 1
  StrCpy $GuardError ""
  !insertmacro ND2WSI_TRACE "" "CheckInstallDirectory.fresh instdir=$INSTDIR"
FunctionEnd

Function VerifyDirectoryPage
  GetInstDirError $0
  !insertmacro ND2WSI_TRACE "" "VerifyDirectoryPage.instdir_error code=$0 instdir=$INSTDIR"
  ${If} $0 == 1
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=VerifyDirectoryPage message=Choose a valid local installation folder (for example, $LOCALAPPDATA\Programs\${INSTALL_DIR_NAME}). A drive root or malformed path cannot be used."
    MessageBox MB_OK|MB_ICONEXCLAMATION "Choose a valid local installation folder (for example, $LOCALAPPDATA\Programs\${INSTALL_DIR_NAME}). A drive root or malformed path cannot be used." /SD IDOK
    Abort
  ${EndIf}
  ${If} $0 == 2
    ; Re-verify free space with 64-bit Win32 GetDiskFreeSpaceExW on the nearest
    ; existing ancestor so nonexistent subfolders, redirected profiles, or large
    ; (>4 TB) drives never falsely block installation.
    Push $R0
    Push $R1
    Push $R2
    Push $R3
    Push $R4
    StrCpy $R0 "$INSTDIR"
    StrCpy $R2 0
    ${Do}
      System::Call 'kernel32::GetDiskFreeSpaceExW(w R0, *l.R1, p 0, p 0)i.R2'
      ${If} $R2 != 0
        ${ExitDo}
      ${EndIf}
      ${GetParent} "$R0" $R3
      ${If} $R3 == ""
      ${OrIf} $R3 == $R0
        ${ExitDo}
      ${EndIf}
      StrCpy $R0 $R3
    ${Loop}
    ${If} $R2 != 0
      System::Int64Op $R1 / 1024
      Pop $R3
      System::Int64Op $R3 < ${PAYLOAD_SIZE_KIB}
      Pop $R4
      ${If} $R4 != 0
        System::Int64Op $R3 / 1024
        Pop $R1
        IntOp $R4 ${PAYLOAD_SIZE_KIB} + 1023
        IntOp $R4 $R4 / 1024
        !insertmacro ND2WSI_TRACE "" "VerifyDirectoryPage.low_disk_space free_kib=$R3 required_kib=${PAYLOAD_SIZE_KIB} root=$R0"
        MessageBox MB_OK|MB_ICONEXCLAMATION "There is not enough free disk space on $R0 to install ${APP_DISPLAY_NAME}.$\r$\n$\r$\nRequired: $R4 MB$\r$\nAvailable: $R1 MB$\r$\n$\r$\nFree up space or choose another folder." /SD IDOK
        Pop $R4
        Pop $R3
        Pop $R2
        Pop $R1
        Pop $R0
        Abort
      ${EndIf}
    ${EndIf}
    Pop $R4
    Pop $R3
    Pop $R2
    Pop $R1
    Pop $R0
  ${EndIf}
  Call CheckInstallDirectory
  ${If} $GuardError != ""
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=VerifyDirectoryPage message=$GuardError"
    MessageBox MB_OK|MB_ICONEXCLAMATION "$GuardError" /SD IDOK
    !insertmacro ND2WSI_TRACE "" "Abort phase=VerifyDirectoryPage guard=$GuardError"
    Abort
  ${EndIf}
FunctionEnd

Function AcceptWebView2Version
  ; Accept $0 into $WebView2Version only when its leading major version integer
  ; before the first '.' is >= 109 (matching pywebview 6.2.1 + viewer CSS/JS floor).
  Push $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  StrLen $R0 $0
  StrCpy $R1 0
  StrCpy $R2 ""
  ${DoWhile} $R1 < $R0
    StrCpy $R3 $0 1 $R1
    ${If} $R3 == "."
      ${ExitDo}
    ${EndIf}
    ${If} $R3 != "0"
    ${AndIf} $R3 != "1"
    ${AndIf} $R3 != "2"
    ${AndIf} $R3 != "3"
    ${AndIf} $R3 != "4"
    ${AndIf} $R3 != "5"
    ${AndIf} $R3 != "6"
    ${AndIf} $R3 != "7"
    ${AndIf} $R3 != "8"
    ${AndIf} $R3 != "9"
      StrCpy $R2 ""
      ${ExitDo}
    ${EndIf}
    StrCpy $R2 "$R2$R3"
    IntOp $R1 $R1 + 1
  ${Loop}
  ${If} $R2 != ""
    IntOp $R4 $R2 + 0
    ${If} $R4 >= 109
      StrCpy $WebView2Version $0
    ${Else}
      !insertmacro ND2WSI_TRACE "" "FindWebView2.below_floor value=$0 major=$R4 min_major=109"
    ${EndIf}
  ${EndIf}
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function FindWebView2
  StrCpy $WebView2Version ""
  SetRegView 64
  ReadRegStr $0 HKCU "${WEBVIEW2_KEY}" "pv"
  !insertmacro ND2WSI_TRACE "" "FindWebView2.HKCU64 value=$0"
  Call AcceptWebView2Version
  ${If} $WebView2Version != ""
    Return
  ${EndIf}
  ReadRegStr $0 HKLM "${WEBVIEW2_KEY}" "pv"
  !insertmacro ND2WSI_TRACE "" "FindWebView2.HKLM64 value=$0"
  Call AcceptWebView2Version
  ${If} $WebView2Version != ""
    Return
  ${EndIf}
  SetRegView 32
  ReadRegStr $0 HKLM "${WEBVIEW2_KEY}" "pv"
  !insertmacro ND2WSI_TRACE "" "FindWebView2.HKLM32 value=$0"
  SetRegView 64
  Call AcceptWebView2Version
FunctionEnd

Function EnsureWebView2
  !insertmacro ND2WSI_TRACE "" "EnsureWebView2.begin"
  Call FindWebView2
  !insertmacro ND2WSI_TRACE "" "EnsureWebView2.detected version=$WebView2Version"
  ${If} $WebView2Version != ""
    DetailPrint "Microsoft Edge WebView2 Runtime: $WebView2Version"
    Return
  ${EndIf}
  !ifdef BUNDLED_WEBVIEW2
    DetailPrint "Using bundled Microsoft Edge WebView2 Fixed Version Runtime."
    !insertmacro ND2WSI_TRACE "" "EnsureWebView2.bundled_fixed_runtime"
    Return
  !else
    IfSilent install_runtime
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=EnsureWebView2 message=${APP_DISPLAY_NAME} needs Microsoft Edge WebView2 Runtime. Choose OK to download and install it from Microsoft. An Internet connection is required."
    MessageBox MB_OKCANCEL|MB_ICONINFORMATION "${APP_DISPLAY_NAME} needs Microsoft Edge WebView2 Runtime. Choose OK to download and install it from Microsoft. An Internet connection is required." IDCANCEL cancelled
    install_runtime:
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=MicrosoftEdgeWebView2Setup.exe "${WEBVIEW2_BOOTSTRAPPER}"
    DetailPrint "Installing Microsoft Edge WebView2 Runtime..."
    !insertmacro ND2WSI_TRACE "" "EnsureWebView2.ExecWait.before bootstrapper=$PLUGINSDIR\MicrosoftEdgeWebView2Setup.exe"
    ClearErrors
    ExecWait '"$PLUGINSDIR\MicrosoftEdgeWebView2Setup.exe" /silent /install' $0
    ${If} ${Errors}
      !insertmacro ND2WSI_TRACE "" "EnsureWebView2.ExecWait.failed exit=$0 nsis_errors=1"
      Goto failed
    ${EndIf}
    !insertmacro ND2WSI_TRACE "" "EnsureWebView2.ExecWait.after exit=$0 nsis_errors=0"
    ${If} $0 != 0
    ${AndIf} $0 != 3010
      Goto failed
    ${EndIf}
    Call FindWebView2
    !insertmacro ND2WSI_TRACE "" "EnsureWebView2.after_install version=$WebView2Version"
    ${If} $WebView2Version == ""
      Goto failed
    ${EndIf}
    Return
    failed:
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=EnsureWebView2 message=Microsoft Edge WebView2 Runtime could not be installed. Check your Internet connection, install the Microsoft WebView2 Evergreen Runtime, and run Setup again."
    MessageBox MB_OK|MB_ICONSTOP "Microsoft Edge WebView2 Runtime could not be installed. Check your Internet connection, install the Microsoft WebView2 Evergreen Runtime, and run Setup again." /SD IDOK
    cancelled:
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=EnsureWebView2 guard=$GuardError"
    Abort
  !endif
FunctionEnd

Function WriteSafeUninstaller
  ; WriteUninstaller can truncate/delete its destination on error. Stage it
  ; first so a failed same-version repair retains the installed copy.
  Push $0
  Push $1
  Push $2
  !insertmacro ND2WSI_TRACE "" "WriteSafeUninstaller.begin instdir=$INSTDIR"
  StrCpy $UninstallerWriteFailed 0
  StrCpy $UninstallerTempPath ""
  ClearErrors
  GetTempFileName $0 "$INSTDIR"
  ${If} ${Errors}
  ${OrIf} $0 == ""
    StrCpy $UninstallerWriteFailed 1
    System::Call 'kernel32::GetLastError()i.r1'
    !insertmacro ND2WSI_TRACE "" "WriteSafeUninstaller.GetTempFileName_failed win32_error_after_command=$1"
    Goto uninstaller_done
  ${EndIf}
  StrCpy $UninstallerTempPath $0
  !insertmacro ND2WSI_TRACE "" "WriteSafeUninstaller.WriteUninstaller.before temporary_target=$0 final_target=$INSTDIR\Uninstall.exe"
  ClearErrors
  WriteUninstaller "$0"
  ; Capture this error before any helper can clear NSIS's sticky flag.
  ${If} ${Errors}
    StrCpy $UninstallerWriteFailed 1
  ${EndIf}
  System::Call 'kernel32::GetLastError()i.r1'
  !insertmacro ND2WSI_TRACE "" "WriteSafeUninstaller.WriteUninstaller.after nsis_errors=$UninstallerWriteFailed win32_error_after_command=$1 temporary_target=$0"
  ${If} $UninstallerWriteFailed != 0
    Goto uninstaller_cleanup
  ${EndIf}
  Push "$0"
  Push "$INSTDIR\Uninstall.exe"
  Call MoveFileExWithRetry
  Pop $2
  Pop $1
  !insertmacro ND2WSI_TRACE "" "WriteSafeUninstaller.MoveFileEx result=$1 win32_error=$2 source=$0 destination=$INSTDIR\Uninstall.exe"
  ${If} $1 == 0
    StrCpy $UninstallerWriteFailed 1
    DetailPrint "Could not replace Uninstall.exe (Windows error $2)."
    Goto uninstaller_cleanup
  ${EndIf}
  StrCpy $UninstallerTempPath ""
  Goto uninstaller_done
  uninstaller_cleanup:
  Push "$0"
  Call DeleteFileWithRetry
  Pop $2
  Pop $1
  !insertmacro ND2WSI_TRACE "" "WriteSafeUninstaller.cleanup result=$1 win32_error=$2 path=$0"
  ${If} $1 == 0
  ${AndIf} $2 != 2
  ${AndIf} $2 != 3
    DetailPrint "Could not remove temporary uninstaller (Windows error $2): $0"
  ${Else}
    StrCpy $UninstallerTempPath ""
  ${EndIf}
  uninstaller_done:
  !insertmacro ND2WSI_TRACE "" "WriteSafeUninstaller.complete failed=$UninstallerWriteFailed pending_temp=$UninstallerTempPath"
  ${If} $UninstallerWriteFailed != 0
    DetailPrint "Could not create a complete installed uninstaller."
  ${EndIf}
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

Function KeepInitialRetryIdentity
  !insertmacro ND2WSI_TRACE "" "KeepInitialRetryIdentity.begin instdir=$INSTDIR"
  ; Called only for this attempt's fresh, initially unregistered folder
  ; after rollback failed. Keep a retry route where access still permits it.
  ${IfNot} ${FileExists} "$INSTDIR\Uninstall.exe"
    Call WriteSafeUninstaller
  ${EndIf}
  Push "in-progress"
  Call WriteOwnershipMarker
  ${If} $MarkerWriteFailed != 0
    DetailPrint "Could not preserve the interrupted-install retry marker."
  ${EndIf}
  Call ReadRegistryState
  ${If} $RegistryState == 1
    ; Never overwrite an unexpected location/product that appeared while
    ; Setup ran. An empty partial key is owned by proven initial absence.
    ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
    ${If} $0 != ""
      Push "$0"
      Call NormalizePath
      Pop $0
      ${If} $0 != $INSTDIR
        DetailPrint "Registration changed during rollback; retry records were not written to it."
        Return
      ${EndIf}
    ${EndIf}
    ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "ProductId"
    ${If} $0 != ""
    ${AndIf} $0 != "${PRODUCT_ID}"
      DetailPrint "Registration ownership changed during rollback; it was retained."
      Return
    ${EndIf}
    ClearErrors
    !insertmacro ND2WSI_WRITE_REGISTRATION "in-progress"
    ${If} ${Errors}
      DetailPrint "Could not restore the Windows Apps retry entry; account permissions must be corrected."
    ${EndIf}
  ${ElseIf} $RegistryState == 2
    DetailPrint "The uninstall registry key cannot be inspected; account permissions must be corrected."
  ${EndIf}
  ; If the key is absent, the complete in-progress marker plus Uninstall.exe
  ; uses the existing uninstaller-only recovery check. No folder adoption.
  !insertmacro ND2WSI_TRACE "" "KeepInitialRetryIdentity.complete registry_state=$RegistryState marker_failed=$MarkerWriteFailed uninstaller_failed=$UninstallerWriteFailed"
FunctionEnd

Function RollBackFreshInitialRecords
  !insertmacro ND2WSI_TRACE "" "RollBackFreshInitialRecords.begin fresh=$InitialInstallFresh registry_initially_absent=$InitialRegistryAbsent registry_write_attempted=$InitialRegistryAttempted"
  StrCpy $RollbackHadErrors 0
  ${If} $InitialInstallFresh != 1
  ${OrIf} $InitialRegistryAbsent != 1
    !insertmacro ND2WSI_TRACE "" "RollBackFreshInitialRecords.skipped_existing_records"
    Return
  ${EndIf}
  Call CheckPayloadPaths
  ${If} $MarkerTempPath != ""
    Push "$MarkerTempPath"
    Call CheckPathChain
  ${EndIf}
  ${If} $UninstallerTempPath != ""
    Push "$UninstallerTempPath"
    Call CheckPathChain
  ${EndIf}
  ${If} $GuardError != ""
    StrCpy $RollbackHadErrors 1
    DetailPrint "Initial-record rollback stopped: $GuardError"
    !insertmacro ND2WSI_TRACE "" "RollBackFreshInitialRecords.guard_failed reason=$GuardError"
    Return
  ${EndIf}
  StrCpy $UninstallHadErrors 0
  SetOutPath "$TEMP"
  ${If} $InitialRegistryAttempted == 1
    Call ReadRegistryState
    ${If} $RegistryState == 1
      ClearErrors
      ; This exact key was absent before this fresh attempt. This includes
      ; an empty key created by a failed first WriteRegStr instruction.
      DeleteRegKey HKCU "${UNINSTALL_KEY}"
      ${If} ${Errors}
        StrCpy $UninstallHadErrors 1
      ${EndIf}
      Call ReadRegistryState
    ${EndIf}
    ${If} $RegistryState != 0
      StrCpy $UninstallHadErrors 1
      DetailPrint "Could not roll back the newly created Windows Apps uninstall key."
    ${EndIf}
  ${EndIf}
  ${If} $UninstallHadErrors != 0
    Goto rollback_incomplete
  ${EndIf}
  ${If} $MarkerTempPath != ""
    Push "$MarkerTempPath"
    Call DeleteOwnedFile
  ${EndIf}
  ${If} $UninstallerTempPath != ""
    Push "$UninstallerTempPath"
    Call DeleteOwnedFile
  ${EndIf}
  ${If} $UninstallHadErrors != 0
    Goto rollback_incomplete
  ${EndIf}
  ; Keep the complete uninstaller until marker deletion has succeeded.
  Push "$INSTDIR\${MARKER}"
  Call DeleteOwnedFile
  ${If} $UninstallHadErrors != 0
    Goto rollback_incomplete
  ${EndIf}
  Push "$INSTDIR\Uninstall.exe"
  Call DeleteOwnedFile
  ${If} $UninstallHadErrors != 0
    Goto rollback_incomplete
  ${EndIf}
  Push "$INSTDIR"
  Call RemoveEmptyOwnedDirectory
  ${If} $UninstallHadErrors != 0
    Goto rollback_incomplete
  ${EndIf}
  DetailPrint "Removed this failed attempt's new installation records."
  !insertmacro ND2WSI_TRACE "" "RollBackFreshInitialRecords.complete failed=0"
  Return
  rollback_incomplete:
  StrCpy $RollbackHadErrors 1
  !insertmacro ND2WSI_TRACE "" "RollBackFreshInitialRecords.incomplete restoring_retry_identity=1"
  Call KeepInitialRetryIdentity
  !insertmacro ND2WSI_TRACE "" "RollBackFreshInitialRecords.complete failed=$RollbackHadErrors"
FunctionEnd

Function FailInitialRecords
  !insertmacro ND2WSI_TRACE "" "FailInitialRecords.begin reason=$InitialRecordError fresh=$InitialInstallFresh registry_initially_absent=$InitialRegistryAbsent registry_write_attempted=$InitialRegistryAttempted"
  DetailPrint "Setup could not create $InitialRecordError. No app payload was extracted."
  Call RollBackFreshInitialRecords
  !insertmacro ND2WSI_TRACE "" "FailInitialRecords.rollback_result failed=$RollbackHadErrors"
  ${If} $RollbackHadErrors != 0
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=FailInitialRecords message=Setup could not create $InitialRecordError and could not completely remove its new installation records. Close programs using this folder and correct this Windows account's folder/registry permissions. Then run Uninstall.exe from this folder if available before retrying Setup. See the details above for the remaining paths."
    MessageBox MB_OK|MB_ICONSTOP "Setup could not create $InitialRecordError and could not completely remove its new installation records. Close programs using this folder and correct this Windows account's folder/registry permissions. Then run Uninstall.exe from this folder if available before retrying Setup. See the details above for the remaining paths." /SD IDOK
  ${ElseIf} $InitialInstallFresh == 1
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=FailInitialRecords message=Setup could not create $InitialRecordError. Its new installation records were removed. Check this Windows account's permissions and run Setup again."
    MessageBox MB_OK|MB_ICONSTOP "Setup could not create $InitialRecordError. Its new installation records were removed. Check this Windows account's permissions and run Setup again." /SD IDOK
  ${Else}
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=FailInitialRecords message=Setup could not create $InitialRecordError. The existing installation records were retained. Close programs using this folder, check this Windows account's permissions, and run Setup again."
    MessageBox MB_OK|MB_ICONSTOP "Setup could not create $InitialRecordError. The existing installation records were retained. Close programs using this folder, check this Windows account's permissions, and run Setup again." /SD IDOK
  ${EndIf}
  SetErrorLevel 2
  !insertmacro ND2WSI_TRACE "" "Abort phase=FailInitialRecords guard=$GuardError"
  Abort
FunctionEnd

Function WriteFileAssociations
  !insertmacro ND2WSI_TRACE "" "WriteFileAssociations.begin instdir=$INSTDIR"
  SetRegView 64
  ClearErrors
  WriteRegStr HKCU "Software\Classes\${PROG_ID}" "" "Microscopy Slide or Image (${APP_DISPLAY_NAME})"
  WriteRegStr HKCU "Software\Classes\${PROG_ID}\DefaultIcon" "" "$INSTDIR\${APP_EXE},0"
  WriteRegStr HKCU "Software\Classes\${PROG_ID}\shell" "" "open"
  WriteRegStr HKCU "Software\Classes\${PROG_ID}\shell\open" "" "Open with ${APP_DISPLAY_NAME}"
  WriteRegStr HKCU "Software\Classes\${PROG_ID}\shell\open\command" "" '"$INSTDIR\${APP_EXE}" "%1"'
  WriteRegStr HKCU "${APP_REG_KEY}" "FriendlyAppName" "${APP_DISPLAY_NAME}"
  WriteRegStr HKCU "${APP_REG_KEY}\DefaultIcon" "" "$INSTDIR\${APP_EXE},0"
  WriteRegStr HKCU "${APP_REG_KEY}\shell\open\command" "" '"$INSTDIR\${APP_EXE}" "%1"'
  WriteRegStr HKCU "${CAPABILITIES_KEY}" "ApplicationName" "${APP_DISPLAY_NAME}"
  WriteRegStr HKCU "${CAPABILITIES_KEY}" "ApplicationDescription" "Local whole-slide and confocal microscopy viewer for ND2, CZI, LIF, OME-TIFF, TIFF, QPTIFF, SVS, SDPC, and ND2SVS"
  !insertmacro ND2WSI_REGISTER_EXTENSION ".nd2"
  !insertmacro ND2WSI_REGISTER_EXTENSION ".czi"
  !insertmacro ND2WSI_REGISTER_EXTENSION ".lif"
  !insertmacro ND2WSI_REGISTER_EXTENSION ".tif"
  !insertmacro ND2WSI_REGISTER_EXTENSION ".tiff"
  !insertmacro ND2WSI_REGISTER_EXTENSION ".qptiff"
  !insertmacro ND2WSI_REGISTER_EXTENSION ".svs"
  !insertmacro ND2WSI_REGISTER_EXTENSION ".sdpc"
  !insertmacro ND2WSI_REGISTER_EXTENSION ".nd2svs"
  WriteRegStr HKCU "${REGISTERED_APPS_KEY}" "${REGISTERED_APP_NAME}" "${CAPABILITIES_KEY}"
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
  ClearErrors
  !insertmacro ND2WSI_TRACE "" "WriteFileAssociations.complete"
FunctionEnd

!ifdef BUNDLED_WEBVIEW2
Function GrantSidRxOnPath
  ; Grant GENERIC_READ|GENERIC_EXECUTE to a well-known SID in-process via advapi32
  ; (no icacls.exe subprocess; safe under AppLocker/SRP). Non-fatal; preserves all registers.
  ; Stack in: path, sid_string, inherit_flags (top).
  Exch $R2 ; inherit_flags
  Exch
  Exch $R1 ; sid_string
  Exch 2
  Exch $R0 ; path
  Push $R3
  Push $R4
  Push $R5
  Push $R6
  Push $R7
  Push $R8
  StrCpy $R3 0 ; psid
  StrCpy $R4 0 ; old_dacl
  StrCpy $R5 0 ; security_descriptor
  StrCpy $R6 0 ; win32 status / bool
  StrCpy $R7 0 ; EXPLICIT_ACCESS_W buffer
  StrCpy $R8 0 ; new_dacl
  System::Call 'advapi32::ConvertStringSidToSidW(w R1, *p.R3)i.R6'
  ${If} $R6 != 0
  ${AndIf} $R3 != 0
    System::Call 'advapi32::GetNamedSecurityInfoW(w R0, i 1, i 4, p 0, p 0, *p.R4, p 0, *p.R5)i.R6'
    ${If} $R6 == 0
      ; x86 EXPLICIT_ACCESS_W (32 bytes): FILE_GENERIC_READ|FILE_GENERIC_EXECUTE (0x001200A9),
      ; GRANT_ACCESS (1), grfInheritance ($R2), TRUSTEE_W (NULL, 0, TRUSTEE_IS_SID=0,
      ; TRUSTEE_IS_WELL_KNOWN_GROUP=5, psid=$R3).
      System::Call '*(i 0x001200A9, i 1, i R2, p 0, i 0, i 0, i 5, p R3)p.R7'
      ${If} $R7 != 0
        System::Call 'advapi32::SetEntriesInAclW(i 1, p R7, p R4, *p.R8)i.R6'
        System::Free $R7
        ${If} $R6 == 0
        ${AndIf} $R8 != 0
          System::Call 'advapi32::SetNamedSecurityInfoW(w R0, i 1, i 4, p 0, p 0, p R8, p 0)i.R6'
          System::Call 'kernel32::LocalFree(p R8)'
        ${EndIf}
      ${EndIf}
      ${If} $R5 != 0
        System::Call 'kernel32::LocalFree(p R5)'
      ${EndIf}
    ${EndIf}
    System::Call 'kernel32::LocalFree(p R3)'
  ${EndIf}
  !insertmacro ND2WSI_TRACE "" "GrantSidRxOnPath path=$R0 sid=$R1 inherit=$R2 status=$R6"
  Pop $R8
  Pop $R7
  Pop $R6
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R0
  Pop $R2
  Pop $R1
FunctionEnd

Function GrantBundledWebView2Acl
  ; Windows 10 (< Win11 build 22000) requires AppContainer groups to have read+execute
  ; access on the bundled Fixed Version WebView2 folder and msedgewebview2.exe.
  ${If} ${AtLeastWin11}
    !insertmacro ND2WSI_TRACE "" "GrantBundledWebView2Acl.skipped_win11"
    Return
  ${EndIf}
  ${If} ${FileExists} "$INSTDIR\_internal\webview2-runtime\msedgewebview2.exe"
    DetailPrint "Configuring Windows 10 AppContainer permissions for bundled WebView2..."
    Push "$INSTDIR\_internal\webview2-runtime"
    Push "S-1-15-2-1"
    Push 3 ; OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
    Call GrantSidRxOnPath
    Push "$INSTDIR\_internal\webview2-runtime"
    Push "S-1-15-2-2"
    Push 3
    Call GrantSidRxOnPath
    Push "$INSTDIR\_internal\webview2-runtime\msedgewebview2.exe"
    Push "S-1-15-2-1"
    Push 0
    Call GrantSidRxOnPath
    Push "$INSTDIR\_internal\webview2-runtime\msedgewebview2.exe"
    Push "S-1-15-2-2"
    Push 0
    Call GrantSidRxOnPath
  ${EndIf}
FunctionEnd
!endif

!ifdef SLACS_EDITION
!define LEGACY_SLACS_UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\nd2wsi-viewer"
!define LEGACY_SLACS_DIR "$LOCALAPPDATA\Programs\nd2wsi-viewer"

Function StrContains
  ; Stack in: haystack, needle (top). Stack out: "1" if found, else "0".
  Exch $R1 ; needle
  Exch
  Exch $R0 ; haystack
  Push $R2
  Push $R3
  Push $R4
  Push $R5
  Push $R6
  StrLen $R2 $R0
  StrLen $R3 $R1
  StrCpy $R4 0
  StrCpy $R5 "0"
  ${If} $R3 == 0
    StrCpy $R5 "1"
  ${ElseIf} $R2 >= $R3
    IntOp $R2 $R2 - $R3
    ${DoWhile} $R4 <= $R2
      StrCpy $R6 $R0 $R3 $R4
      ${If} $R6 == $R1
        StrCpy $R5 "1"
        ${ExitDo}
      ${EndIf}
      IntOp $R4 $R4 + 1
    ${Loop}
  ${EndIf}
  StrCpy $R0 $R5
  Pop $R6
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R2
  Exch $R0
  Exch
  Pop $R1
FunctionEnd

Function FileContainsSlacsEdition
  ; Safely inspect a non-symlink regular JSON file for `"edition": "SLACS-module"`.
  ; Stack in: path. Stack out: "1" if verified SLACS edition, else "0".
  Exch $R0 ; path
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  Push $R5
  Push $GuardError
  StrCpy $R5 "0"
  StrCpy $GuardError ""
  System::Call 'kernel32::GetFileAttributesW(w R0)i.R1'
  ${If} $R1 != -1
    IntOp $R2 $R1 & 0x10 ; FILE_ATTRIBUTE_DIRECTORY
    ${If} $R2 == 0
      IntOp $R2 $R1 & 0x400 ; FILE_ATTRIBUTE_REPARSE_POINT
      ${If} $R2 != 0
        Push "$R0"
        Call IsNameSurrogateReparse
        Pop $R2
      ${EndIf}
      ${If} $R2 == 0
        Push "$R0"
        Call CheckPathChain
        ${If} $GuardError == ""
          ClearErrors
          FileOpen $R1 "$R0" r
          ${IfNot} ${Errors}
            StrCpy $R3 0
            ${DoWhile} $R3 < 64
              ClearErrors
              FileRead $R1 $R2
              ${If} ${Errors}
              ${OrIf} $R2 == ""
                ${ExitDo}
              ${EndIf}
              Push "$R2"
              Push '"edition": "SLACS-module"'
              Call StrContains
              Pop $R4
              ${If} $R4 == "1"
                StrCpy $R5 "1"
                ${ExitDo}
              ${EndIf}
              Push "$R2"
              Push '"edition":"SLACS-module"'
              Call StrContains
              Pop $R4
              ${If} $R4 == "1"
                StrCpy $R5 "1"
                ${ExitDo}
              ${EndIf}
              IntOp $R3 $R3 + 1
            ${Loop}
            FileClose $R1
          ${EndIf}
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ClearErrors
  Pop $GuardError
  StrCpy $R0 $R5
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Exch $R0
FunctionEnd

Function MigrateLegacySlacsInstall
  ; Cleanly migrate legacy 2.6.5-SLACS / 2.6.6-SLACS installs that shared
  ; $LOCALAPPDATA\Programs\nd2wsi-viewer before SLACS isolation in 2.6.7.
  ; NEVER touches a real public nd2wsi-viewer installation: migration runs ONLY
  ; when a non-symlink build-info.json explicitly contains "edition": "SLACS-module".
  Push $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  Push $GuardError
  StrCpy $GuardError ""
  Push "${LEGACY_SLACS_DIR}"
  Call NormalizePath
  Pop $R0
  ${If} $R0 == ""
  ${OrIf} $R0 == $INSTDIR
    !insertmacro ND2WSI_TRACE "" "MigrateLegacySlacsInstall.skip_same_or_empty legacy=$R0 instdir=$INSTDIR"
    Goto migrate_done
  ${EndIf}
  System::Call 'kernel32::GetFileAttributesW(w R0)i.R1'
  ${If} $R1 == -1
    !insertmacro ND2WSI_TRACE "" "MigrateLegacySlacsInstall.absent dir=$R0"
    Goto migrate_done
  ${EndIf}
  IntOp $R2 $R1 & 0x10
  ${If} $R2 == 0
    Goto migrate_done
  ${EndIf}
  Push "$R0"
  Call CheckPathChain
  ${If} $GuardError != ""
    !insertmacro ND2WSI_TRACE "" "MigrateLegacySlacsInstall.unsafe_path dir=$R0 guard=$GuardError"
    Goto migrate_done
  ${EndIf}
  ; Check both _internal\nd2wsi-viewer-about\build-info.json and root build-info.json.
  StrCpy $R1 "$R0\_internal\nd2wsi-viewer-about\build-info.json"
  Push "$R1"
  Call FileContainsSlacsEdition
  Pop $R2
  ${If} $R2 != "1"
    StrCpy $R1 "$R0\build-info.json"
    Push "$R1"
    Call FileContainsSlacsEdition
    Pop $R2
  ${EndIf}
  ${If} $R2 != "1"
    !insertmacro ND2WSI_TRACE "" "MigrateLegacySlacsInstall.preserved_non_slacs dir=$R0"
    Goto migrate_done
  ${EndIf}
  !insertmacro ND2WSI_TRACE "" "MigrateLegacySlacsInstall.verified_slacs dir=$R0 info=$R1"
  DetailPrint "Migrating legacy ${APP_DISPLAY_NAME} installation from $R0..."
  StrCpy $R3 "$R0\Uninstall.exe"
  System::Call 'kernel32::GetFileAttributesW(w R3)i.R1'
  ${If} $R1 != -1
    IntOp $R2 $R1 & 0x10
    ${If} $R2 == 0
      StrCpy $GuardError ""
      Push "$R3"
      Call CheckPathChain
      ${If} $GuardError == ""
        InitPluginsDir
        ClearErrors
        CopyFiles /SILENT "$R3" "$PLUGINSDIR\legacy-slacs-uninstall.exe"
        ${IfNot} ${Errors}
          ClearErrors
          ExecWait '"$PLUGINSDIR\legacy-slacs-uninstall.exe" /S _?=$R0' $R4
          ${If} ${Errors}
            StrCpy $R4 2
          ${EndIf}
          Delete "$PLUGINSDIR\legacy-slacs-uninstall.exe"
          !insertmacro ND2WSI_TRACE "" "MigrateLegacySlacsInstall.uninstall_complete dir=$R0 exit=$R4"
        ${Else}
          !insertmacro ND2WSI_TRACE "" "MigrateLegacySlacsInstall.copy_uninstaller_failed dir=$R0"
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ; Clean up stale legacy SLACS uninstall entry ONLY when bound to this verified
  ; legacy SLACS folder and its Uninstall.exe is no longer present.
  SetRegView 64
  ReadRegStr $R4 HKCU "${LEGACY_SLACS_UNINSTALL_KEY}" "InstallLocation"
  ${If} $R4 != ""
    Push "$R4"
    Call NormalizePath
    Pop $R4
    ${If} $R4 == $R0
    ${AndIfNot} ${FileExists} "$R0\Uninstall.exe"
      ClearErrors
      DeleteRegKey HKCU "${LEGACY_SLACS_UNINSTALL_KEY}"
      !insertmacro ND2WSI_TRACE "" "MigrateLegacySlacsInstall.cleaned_stale_reg key=${LEGACY_SLACS_UNINSTALL_KEY} dir=$R0"
    ${EndIf}
  ${EndIf}
  ClearErrors
  migrate_done:
  Pop $GuardError
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd
!endif

Section "${APP_DISPLAY_NAME} (required)" SecApplication
  SectionIn RO
  !insertmacro ND2WSI_TRACE "" "SecApplication.begin instdir=$INSTDIR"
  DetailPrint "Windows version: $WinMajor.$WinMinor (build $WinBuild)"
  Call CheckInstallDirectory
  !insertmacro ND2WSI_TRACE "" "SecApplication.directory_check guard=$GuardError fresh=$InitialInstallFresh"
  ${If} $GuardError != ""
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=SecApplication message=$GuardError"
    MessageBox MB_OK|MB_ICONSTOP "$GuardError" /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=SecApplication guard=$GuardError"
    Abort
  ${EndIf}
  Call RequireAppClosed
  Call EnsureWebView2
  !insertmacro ND2WSI_TRACE "" "SecApplication.prerequisites_passed webview2=$WebView2Version"
  ; Recheck after the prerequisite download, before changing app files.
  Call RequireAppClosed
  Call CheckInstallDirectory
  !insertmacro ND2WSI_TRACE "" "SecApplication.final_directory_check guard=$GuardError fresh=$InitialInstallFresh"
  ${If} $GuardError != ""
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=SecApplication message=$GuardError"
    MessageBox MB_OK|MB_ICONSTOP "$GuardError" /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=SecApplication guard=$GuardError"
    Abort
  ${EndIf}
  ; If upgrading a registered earlier version in this exact folder, cleanly run
  ; its own uninstaller from a temporary copy so stale files from the prior
  ; release are removed while user-created files are preserved.
  ${If} $ExistingUpgrade == 1
    !insertmacro ND2WSI_TRACE "" "SecApplication.upgrade.begin instdir=$INSTDIR"
    DetailPrint "Removing previous ${APP_DISPLAY_NAME} version before upgrading..."
    InitPluginsDir
    ClearErrors
    CopyFiles /SILENT "$INSTDIR\Uninstall.exe" "$PLUGINSDIR\prev-uninstall.exe"
    ${If} ${Errors}
      !insertmacro ND2WSI_TRACE "" "SecApplication.upgrade.copy_failed"
      MessageBox MB_OK|MB_ICONSTOP "Could not prepare the previous uninstaller for upgrade. Close programs using this folder or uninstall the previous version from Windows Settings > Apps, then run Setup again." /SD IDOK
      SetErrorLevel 2
      Abort
    ${EndIf}
    ${If} $SetupMutex != 0
      StrCpy $0 $SetupMutex
      System::Call 'kernel32::CloseHandle(p r0)'
      StrCpy $SetupMutex 0
    ${EndIf}
    ClearErrors
    ExecWait '"$PLUGINSDIR\prev-uninstall.exe" /S _?=$INSTDIR' $UpgradeExitCode
    ${If} ${Errors}
      StrCpy $UpgradeExitCode 2
    ${EndIf}
    Delete "$PLUGINSDIR\prev-uninstall.exe"
    Call AcquireSetupMutex
    !insertmacro ND2WSI_TRACE "" "SecApplication.upgrade.complete exit=$UpgradeExitCode"
    ${If} $UpgradeExitCode != 0
      MessageBox MB_OK|MB_ICONSTOP "Could not remove the previous ${APP_DISPLAY_NAME} version before upgrading (exit code $UpgradeExitCode). Close programs using this folder or uninstall the previous version from Windows Settings > Apps, then run Setup again." /SD IDOK
      SetErrorLevel 2
      Abort
    ${EndIf}
    StrCpy $ExistingUpgrade 0
    StrCpy $InitialInstallFresh 1
  ${EndIf}
  ; The preceding final directory check records whether the destination is
  ; empty/new. Capture actual registry absence before creating any metadata.
  StrCpy $InitialRegistryAbsent 0
  StrCpy $InitialRegistryAttempted 0
  StrCpy $MarkerTempPath ""
  StrCpy $UninstallerTempPath ""
  Call ReadRegistryState
  !insertmacro ND2WSI_TRACE "" "SecApplication.initial_registry state=$RegistryState"
  ${If} $RegistryState == 0
    StrCpy $InitialRegistryAbsent 1
  ${ElseIf} $InitialInstallFresh == 1
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=SecApplication message=Setup found an existing or inaccessible ${APP_DISPLAY_NAME} uninstall registry key without a matching installation. Restore or remove that old registration before installing into a new folder. No installation files were created."
    MessageBox MB_OK|MB_ICONSTOP "Setup found an existing or inaccessible ${APP_DISPLAY_NAME} uninstall registry key without a matching installation. Restore or remove that old registration before installing into a new folder. No installation files were created." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=SecApplication guard=$GuardError"
    Abort
  ${ElseIf} $RegistryState == 2
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=SecApplication message=Setup cannot inspect this account's uninstall registration. Correct its permissions and try again. No installation files were changed."
    MessageBox MB_OK|MB_ICONSTOP "Setup cannot inspect this account's uninstall registration. Correct its permissions and try again. No installation files were changed." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=SecApplication guard=$GuardError"
    Abort
  ${EndIf}
  ClearErrors
  SetOutPath "$INSTDIR"
  !insertmacro ND2WSI_TRACE "" "SecApplication.SetOutPath.after instdir=$INSTDIR"
  ${If} ${Errors}
    StrCpy $InitialRecordError "the installation folder"
    Call FailInitialRecords
  ${EndIf}
  SetOverwrite on
  ; Retain enough ownership data to recover a partially interrupted install.
  Push ""
  Call WriteOwnershipMarker
  ${If} $MarkerWriteFailed != 0
    StrCpy $InitialRecordError "the ownership marker"
    Call FailInitialRecords
  ${EndIf}
  Call WriteSafeUninstaller
  ${If} $UninstallerWriteFailed != 0
    StrCpy $InitialRecordError "the uninstaller"
    Call FailInitialRecords
  ${EndIf}
  StrCpy $InitialRegistryAttempted 1
  !insertmacro ND2WSI_TRACE "" "SecApplication.registration.before"
  ClearErrors
  !insertmacro ND2WSI_WRITE_REGISTRATION ""
  !insertmacro ND2WSI_TRACE "" "SecApplication.registration.after"
  ${If} ${Errors}
    StrCpy $InitialRecordError "the Windows Apps uninstall registration"
    Call FailInitialRecords
  ${EndIf}
  !insertmacro ND2WSI_TRACE "" "SecApplication.payload.before"
  !include "${PAYLOAD_INSTALL_INCLUDE}"
  !insertmacro ND2WSI_TRACE "" "SecApplication.payload.after"
  !ifdef BUNDLED_WEBVIEW2
    Call GrantBundledWebView2Acl
  !endif
  !ifdef SLACS_EDITION
    Call MigrateLegacySlacsInstall
  !endif
  SetOutPath "$INSTDIR"
  CreateDirectory "$SMPROGRAMS\${SHORTCUT_DIR}"
  CreateShortcut "$SMPROGRAMS\${SHORTCUT_DIR}\${SHORTCUT_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\${SHORTCUT_DIR}\Uninstall ${SHORTCUT_NAME}.lnk" "$INSTDIR\Uninstall.exe"
  Call WriteFileAssociations
  !insertmacro ND2WSI_TRACE "" "SecApplication.complete"
SectionEnd

Section "Desktop shortcut" SecDesktop
  SetOutPath "$INSTDIR"
  CreateDirectory "$DESKTOP"
  CreateShortcut "$DESKTOP\${SHORTCUT_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

; These helpers distinguish harmless absence/nonempty user directories from
; access, sharing, and other actual removal failures. NSIS FileExists/RMDir
; alone cannot make that distinction reliably.
!macro DEFINE_REMOVAL_FUNCTIONS PREFIX
Function ${PREFIX}DeleteOwnedFile
  Exch $R0
  Push $R1
  Push $R2
  Push "$R0"
  Call ${PREFIX}DeleteFileWithRetry
  Pop $R2
  Pop $R1
  ${If} $R1 == 0
  ${AndIf} $R2 != 2
  ${AndIf} $R2 != 3
    StrCpy $UninstallHadErrors 1
    !insertmacro ND2WSI_TRACE "${PREFIX}" "DeleteOwnedFile.failed path=$R0 win32_error=$R2"
    DetailPrint "Could not remove file (Windows error $R2): $R0"
  ${EndIf}
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function ${PREFIX}RemoveEmptyOwnedDirectory
  Exch $R0
  Push $R1
  Push $R2
  System::Call 'kernel32::RemoveDirectoryW(w R0)i.R1 ?e'
  Pop $R2
  ${If} $R1 == 0
  ${AndIf} $R2 != 2
  ${AndIf} $R2 != 3
  ${AndIf} $R2 != 145
    StrCpy $UninstallHadErrors 1
    !insertmacro ND2WSI_TRACE "${PREFIX}" "RemoveEmptyOwnedDirectory.failed path=$R0 win32_error=$R2"
    DetailPrint "Could not remove empty folder (Windows error $R2): $R0"
  ${EndIf}
  ; ERROR_DIR_NOT_EMPTY deliberately keeps any added user files.
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function ${PREFIX}ReadRegistryState
  ; 0 absent, 1 present, 2 inaccessible/unknown. Query the same 64-bit HKCU
  ; view used by the installer. An access denial must never mean "absent".
  Push $0
  Push $1
  System::Call 'advapi32::RegOpenKeyExW(p 0x80000001, w "${UNINSTALL_KEY}", i 0, i 0x20119, *p.r0)i.r1'
  StrCpy $RegistryState 2
  ${If} $1 == 0
    StrCpy $RegistryState 1
    System::Call 'advapi32::RegCloseKey(p r0)'
  ${ElseIf} $1 == 2
    StrCpy $RegistryState 0
  ${EndIf}
  !insertmacro ND2WSI_TRACE "${PREFIX}" "ReadRegistryState state=$RegistryState RegOpenKeyEx_result=$1"
  Pop $1
  Pop $0
FunctionEnd
!macroend

!insertmacro DEFINE_REMOVAL_FUNCTIONS ""
!insertmacro DEFINE_REMOVAL_FUNCTIONS "un."

Function un.CheckUninstallDirectory
  Call un.CheckOwnedDirectory
  ${If} $GuardError == ""
    Return
  ${EndIf}
  ; A late interrupted uninstall can have removed either ownership record.
  ; Recovery is uninstaller-only and requires a complete, versioned record
  ; bound to this exact install path. The normal install/reinstall check
  ; above remains strict and still requires both marker and registration.
  Call un.CheckPayloadPaths
  ${If} $GuardError != ""
    Return
  ${EndIf}
  StrCpy $GuardError "This folder is not a registered ${APP_DISPLAY_NAME} ${APP_VERSION} installation or a recoverable interrupted uninstall. No app files have been removed."
  ${GetRoot} "$INSTDIR" $0
  ${If} $INSTDIR == $0
  ${OrIf} $INSTDIR == "$0\"
    Return
  ${EndIf}
  Call un.ReadRegistryState
  ${If} $RegistryState == 0
    ReadINIStr $0 "$INSTDIR\${MARKER}" "Installation" "ProductId"
    ReadINIStr $1 "$INSTDIR\${MARKER}" "Installation" "Version"
    ReadINIStr $2 "$INSTDIR\${MARKER}" "Installation" "UninstallState"
    ReadINIStr $3 "$INSTDIR\${MARKER}" "Installation" "InstallLocation"
    ${If} $3 != ""
      Push "$3"
      Call un.NormalizePath
      Pop $3
    ${EndIf}
    ${If} $0 == "${PRODUCT_ID}"
    ${AndIf} $1 == "${APP_VERSION}"
    ${AndIf} $2 == "in-progress"
    ${AndIf} $3 == $INSTDIR
      StrCpy $GuardError ""
    ${EndIf}
    Return
  ${EndIf}
  ${If} $RegistryState != 1
    Return
  ${EndIf}
  StrCpy $1 "$INSTDIR\${MARKER}"
  System::Call 'kernel32::GetFileAttributesW(w r1)i.r0 ?e'
  Pop $1
  ${If} $0 != -1
    Return
  ${EndIf}
  ${If} $1 != 2
  ${AndIf} $1 != 3
    Return
  ${EndIf}
  ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "ProductId"
  ReadRegStr $1 HKCU "${UNINSTALL_KEY}" "DisplayVersion"
  ReadRegStr $2 HKCU "${UNINSTALL_KEY}" "UninstallState"
  ReadRegStr $3 HKCU "${UNINSTALL_KEY}" "InstallLocation"
  ${If} $3 != ""
    Push "$3"
    Call un.NormalizePath
    Pop $3
  ${EndIf}
  ${If} $0 == "${PRODUCT_ID}"
  ${AndIf} $1 == "${APP_VERSION}"
  ${AndIf} $2 == "in-progress"
  ${AndIf} $3 == $INSTDIR
    StrCpy $GuardError ""
  ${EndIf}
FunctionEnd

Function un.MarkInProgress
  Push "in-progress"
  Call un.WriteOwnershipMarker
  ${If} $MarkerWriteFailed != 0
    StrCpy $UninstallHadErrors 1
    DetailPrint "Could not save the interrupted-uninstall recovery record."
  ${EndIf}
FunctionEnd

Function un.RestoreRetryRecords
  ; The running NSIS temporary copy is usable even if the installed copy
  ; has just been deleted. Keep an existing installed copy byte-for-byte.
  StrCpy $RecoveryHadErrors 0
  ${IfNot} ${FileExists} "$INSTDIR\Uninstall.exe"
    ClearErrors
    CopyFiles /SILENT "$EXEPATH" "$INSTDIR\Uninstall.exe"
    ${If} ${Errors}
      StrCpy $RecoveryHadErrors 1
      DetailPrint "Could not restore Uninstall.exe; run Setup again to repair the uninstall entry."
    ${EndIf}
  ${EndIf}
  Call un.MarkInProgress
  ${If} $MarkerWriteFailed != 0
    StrCpy $RecoveryHadErrors 1
  ${EndIf}
  ClearErrors
  WriteRegStr HKCU "${UNINSTALL_KEY}" "ProductId" "${PRODUCT_ID}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallState" "in-progress"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_DISPLAY_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "nd2wsi-viewer"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE},0"
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" ${PAYLOAD_SIZE_KIB}
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  ${If} ${Errors}
    StrCpy $RecoveryHadErrors 1
    DetailPrint "Could not restore the Windows Apps uninstall entry."
  ${EndIf}
FunctionEnd

Function un.DeleteKeyIfTrulyEmpty
  ; Delete HKCU subkey ONLY when it has zero child subkeys AND zero values,
  ; preserving any preexisting handlers or OpenWithProgids registered by other apps.
  Exch $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  System::Call 'advapi32::RegOpenKeyExW(p 0x80000001, w R0, i 0, i 0x20119, *p.R1)i.R2'
  ${If} $R2 == 0
    System::Call 'advapi32::RegQueryInfoKeyW(p R1, p 0, p 0, p 0, *i.R3, p 0, p 0, *i.R4, p 0, p 0, p 0, p 0)i.R2'
    System::Call 'advapi32::RegCloseKey(p R1)'
    ${If} $R2 == 0
    ${AndIf} $R3 == 0
    ${AndIf} $R4 == 0
      DeleteRegKey /ifempty HKCU "$R0"
    ${EndIf}
  ${EndIf}
  ClearErrors
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function un.RemoveOwnedAppRegKey
  ; Only delete HKCU "${APP_REG_KEY}" if its shell\open\command points into
  ; this $INSTDIR, so uninstalling one installation or edition never wipes
  ; another installation's "Open with" registration.
  Push $R0
  Push $R1
  Push $R2
  Push $R3
  Push $R4
  ReadRegStr $R0 HKCU "${APP_REG_KEY}\shell\open\command" ""
  StrCpy $R4 0
  ${If} $R0 != ""
    ${If} $R0 == '"$INSTDIR\${APP_EXE}" "%1"'
      StrCpy $R4 1
    ${Else}
      StrCpy $R1 $R0 1
      ${If} $R1 == '"'
        StrCpy $R2 1
        StrLen $R3 $R0
        ${DoWhile} $R2 < $R3
          StrCpy $R1 $R0 1 $R2
          ${If} $R1 == '"'
            ${ExitDo}
          ${EndIf}
          IntOp $R2 $R2 + 1
        ${Loop}
        IntOp $R2 $R2 - 1
        StrCpy $R1 $R0 $R2 1
      ${Else}
        StrCpy $R1 $R0
      ${EndIf}
      ${If} $R1 != ""
        Push "$R1"
        Call un.NormalizePath
        Pop $R1
        ${If} $R1 != ""
          ${GetParent} "$R1" $R2
          ${If} $R2 == $INSTDIR
            StrCpy $R4 1
          ${EndIf}
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ${If} $R4 == 1
    DeleteRegKey HKCU "${APP_REG_KEY}"
  ${Else}
    !insertmacro ND2WSI_TRACE "un." "RemoveOwnedAppRegKey.preserved command=$R0 instdir=$INSTDIR"
  ${EndIf}
  ClearErrors
  Pop $R4
  Pop $R3
  Pop $R2
  Pop $R1
  Pop $R0
FunctionEnd

Function un.RemoveFileAssociations
  !insertmacro ND2WSI_TRACE "un." "RemoveFileAssociations.begin"
  SetRegView 64
  DeleteRegValue HKCU "${REGISTERED_APPS_KEY}" "${REGISTERED_APP_NAME}"
  DeleteRegKey HKCU "${CAPABILITIES_KEY}"
  Push "${CAPABILITIES_ROOT_KEY}"
  Call un.DeleteKeyIfTrulyEmpty
  DeleteRegKey HKCU "Software\Classes\${PROG_ID}"
  Call un.RemoveOwnedAppRegKey
  !insertmacro ND2WSI_UNREGISTER_EXTENSION ".nd2"
  !insertmacro ND2WSI_UNREGISTER_EXTENSION ".czi"
  !insertmacro ND2WSI_UNREGISTER_EXTENSION ".lif"
  !insertmacro ND2WSI_UNREGISTER_EXTENSION ".tif"
  !insertmacro ND2WSI_UNREGISTER_EXTENSION ".tiff"
  !insertmacro ND2WSI_UNREGISTER_EXTENSION ".qptiff"
  !insertmacro ND2WSI_UNREGISTER_EXTENSION ".svs"
  !insertmacro ND2WSI_UNREGISTER_EXTENSION ".sdpc"
  !insertmacro ND2WSI_UNREGISTER_EXTENSION ".nd2svs"
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
  ClearErrors
  !insertmacro ND2WSI_TRACE "un." "RemoveFileAssociations.complete"
FunctionEnd

Function un.onInit
  Call un.InitInstallerLog
  !insertmacro ND2WSI_TRACE "un." "un.onInit.begin instdir=$INSTDIR exepath=$EXEPATH"
  GetWinVer $WinMajor Major
  GetWinVer $WinMinor Minor
  GetWinVer $WinBuild Build
  !insertmacro ND2WSI_TRACE "un." "un.onInit.windows_version major=$WinMajor minor=$WinMinor build=$WinBuild"
  SetShellVarContext current
  SetRegView 64
  Call un.AcquireSetupMutex
  Call un.CheckUninstallDirectory
  ${If} $GuardError != ""
    !insertmacro ND2WSI_TRACE "un." "MessageBox phase=un.onInit message=$GuardError$\r$\n$\r$\nNo app files have been removed."
    MessageBox MB_OK|MB_ICONSTOP "$GuardError$\r$\n$\r$\nNo app files have been removed." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "un." "Abort phase=un.onInit guard=$GuardError"
    Abort
  ${EndIf}
  !insertmacro ND2WSI_TRACE "un." "un.onInit.complete"
FunctionEnd

Section "Uninstall"
  !insertmacro ND2WSI_TRACE "un." "Uninstall.begin instdir=$INSTDIR"
  Call un.RequireAppClosed
  Call un.CheckUninstallDirectory
  ${If} $GuardError != ""
    !insertmacro ND2WSI_TRACE "un." "MessageBox phase=Uninstall message=$GuardError"
    MessageBox MB_OK|MB_ICONSTOP "$GuardError" /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "un." "Abort phase=Uninstall guard=$GuardError"
    Abort
  ${EndIf}
  Call un.CheckPayloadPaths
  ${If} $GuardError != ""
    !insertmacro ND2WSI_TRACE "un." "MessageBox phase=Uninstall message=$GuardError"
    MessageBox MB_OK|MB_ICONSTOP "$GuardError" /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "un." "Abort phase=Uninstall guard=$GuardError"
    Abort
  ${EndIf}
  ; Check shortcut ancestors before following a directory path to a file.
  Push "$SMPROGRAMS\${SHORTCUT_DIR}\${SHORTCUT_NAME}.lnk"
  Call un.CheckPathChain
  Push "$SMPROGRAMS\${SHORTCUT_DIR}\Uninstall ${SHORTCUT_NAME}.lnk"
  Call un.CheckPathChain
  Push "$DESKTOP\${SHORTCUT_NAME}.lnk"
  Call un.CheckPathChain
  ${If} $GuardError != ""
    !insertmacro ND2WSI_TRACE "un." "MessageBox phase=Uninstall message=$GuardError"
    MessageBox MB_OK|MB_ICONSTOP "$GuardError" /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "un." "Abort phase=Uninstall guard=$GuardError"
    Abort
  ${EndIf}
  SetOutPath "$TEMP"
  StrCpy $UninstallHadErrors 0
  !include "${PAYLOAD_UNINSTALL_INCLUDE}"
  ${If} $UninstallHadErrors != 0
    Goto removal_failed
  ${EndIf}
  Push "$SMPROGRAMS\${SHORTCUT_DIR}\${SHORTCUT_NAME}.lnk"
  Call un.DeleteOwnedFile
  Push "$SMPROGRAMS\${SHORTCUT_DIR}\Uninstall ${SHORTCUT_NAME}.lnk"
  Call un.DeleteOwnedFile
  Push "$SMPROGRAMS\${SHORTCUT_DIR}"
  Call un.RemoveEmptyOwnedDirectory
  Push "$DESKTOP\${SHORTCUT_NAME}.lnk"
  Call un.DeleteOwnedFile
  ${If} $UninstallHadErrors != 0
    Goto removal_failed
  ${EndIf}
  Call un.RemoveFileAssociations
  ; Do not remove either retry record until all payload/shortcut removals
  ; have succeeded. A path-bound marker can authorize a retry after the
  ; registration is removed but before the following cleanup completes.
  Call un.MarkInProgress
  ${If} $UninstallHadErrors != 0
    Goto removal_failed
  ${EndIf}
  Call un.ReadRegistryState
  ${If} $RegistryState == 1
    ClearErrors
    DeleteRegKey HKCU "${UNINSTALL_KEY}"
    ${If} ${Errors}
      StrCpy $UninstallHadErrors 1
      DetailPrint "Could not remove the Windows Apps uninstall entry."
    ${EndIf}
    Call un.ReadRegistryState
  ${EndIf}
  ${If} $RegistryState != 0
    StrCpy $UninstallHadErrors 1
    DetailPrint "The Windows Apps uninstall entry is still present or could not be inspected."
  ${EndIf}
  ${If} $UninstallHadErrors != 0
    Goto cleanup_failed
  ${EndIf}
  !insertmacro ND2WSI_DELETE_FILE "${MARKER}"
  ${If} $UninstallHadErrors != 0
    Goto cleanup_failed
  ${EndIf}
  !insertmacro ND2WSI_DELETE_FILE "Uninstall.exe"
  ${If} $UninstallHadErrors != 0
    Goto cleanup_failed
  ${EndIf}
  ; Deliberately no recursive deletion: any added user files keep the folder.
  Push "$INSTDIR"
  Call un.RemoveEmptyOwnedDirectory
  ${If} $UninstallHadErrors != 0
    Goto cleanup_failed
  ${EndIf}
  SetErrorLevel 0
  Goto uninstall_done
  cleanup_failed:
  Call un.RestoreRetryRecords
  ${If} $RecoveryHadErrors != 0
    !insertmacro ND2WSI_TRACE "un." "MessageBox phase=Uninstall message=Uninstall could not complete, and some retry records could not be restored. Your user files have been kept. Close programs using this folder and run Uninstall again. If its Windows Apps entry is unavailable, run Setup again for this same folder to restore it."
    MessageBox MB_OK|MB_ICONEXCLAMATION "Uninstall could not complete, and some retry records could not be restored. Your user files have been kept. Close programs using this folder and run Uninstall again. If its Windows Apps entry is unavailable, run Setup again for this same folder to restore it." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "un." "Abort phase=Uninstall guard=$GuardError"
    Abort
  ${EndIf}
  removal_failed:
  !insertmacro ND2WSI_TRACE "un." "MessageBox phase=Uninstall message=Some app files, shortcuts, folders, or uninstall records are in use or could not be removed. Close programs using the app folder and its shortcuts, then run Uninstall again. Your user files have been kept. See the details above for the failed path."
  MessageBox MB_OK|MB_ICONEXCLAMATION "Some app files, shortcuts, folders, or uninstall records are in use or could not be removed. Close programs using the app folder and its shortcuts, then run Uninstall again. Your user files have been kept. See the details above for the failed path." /SD IDOK
  SetErrorLevel 2
  !insertmacro ND2WSI_TRACE "un." "Abort phase=Uninstall guard=$GuardError"
  Abort
  uninstall_done:
  !insertmacro ND2WSI_TRACE "un." "Uninstall.complete failed=$UninstallHadErrors"
SectionEnd

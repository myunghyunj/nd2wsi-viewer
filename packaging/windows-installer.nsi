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

!define APP_NAME "nd2wsi-viewer"
!define APP_EXE "nd2wsi-viewer.exe"
!define PRODUCT_ID "nd2wsi-viewer.windows.x64.current-user"
!define MARKER ".nd2wsi-install.ini"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\nd2wsi-viewer"
!define WEBVIEW2_KEY "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\nd2wsi-viewer"
BrandingText "nd2wsi-viewer"
SetCompressor /SOLID lzma
SetCompressorDictSize 32
SetDatablockOptimize on
CRCCheck on
AllowSkipFiles off
ShowInstDetails show
ShowUninstDetails show
VIProductVersion "${APP_VERSION_QUAD}"
VIAddVersionKey /LANG=1033 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "FileDescription" "nd2wsi-viewer Setup"
VIAddVersionKey /LANG=1033 "LegalCopyright" "nd2wsi-viewer contributors"

!ifdef APP_ICON
  !define MUI_ICON "${APP_ICON}"
  !define MUI_UNICON "${APP_ICON}"
!endif
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Install nd2wsi-viewer ${APP_VERSION}"
!define MUI_WELCOMEPAGE_TEXT "View microscopy images with nd2wsi-viewer.$\r$\n$\r$\nSetup installs the app for your Windows account. Close nd2wsi-viewer before continuing.$\r$\n$\r$\nUse Windows 10 version 1709 or later, or Windows 11, on x64. Windows 10 Education is supported. ARM64 requires Windows 11 and uses x64 emulation.$\r$\n$\r$\nMicrosoft .NET Framework 4.6.2 or later is required. If WebView2 Runtime is missing, Setup can download it from Microsoft using an Internet connection."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE VerifyDirectoryPage
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Open nd2wsi-viewer"
!insertmacro MUI_PAGE_FINISH
!define MUI_UNCONFIRMPAGE_TEXT_TOP "Uninstall nd2wsi-viewer from this folder? Setup removes only the app files and shortcuts it installed. Your images, annotations, caches, and other user files are kept."
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
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "nd2wsi-viewer"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "nd2wsi-viewer"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE},0"
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" ${PAYLOAD_SIZE_KIB}
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
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

Function ${PREFIX}CheckPathChain
  ; Check the leaf as well as every existing ancestor. This rejects both
  ; directory junctions and file symlinks before extraction/deletion can
  ; follow them outside the registered installation folder.
  Exch $R0
  Push $R1
  Push $R2
  Push $R3
  ; NSIS GetFullPathName can clear its output for a nonexistent leaf.
  ; Use lexical Win32 normalization so every existing ancestor is still
  ; inspected even when the destination or a shipped child does not exist.
  System::Call 'kernel32::GetFullPathNameW(w "$R0", i ${NSIS_MAX_STRLEN}, w .R1, p 0)i.R2 ?e'
  Pop $R3
  ${If} $R2 == 0
  ${OrIf} $R2 >= ${NSIS_MAX_STRLEN}
    StrCpy $GuardError "Setup could not resolve this path:$\r$\n$R0$\r$\n$\r$\nChoose a shorter, valid app folder and try again."
    !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPathChain.normalize_failed path=$R0 returned_length=$R2 win32_error=$R3 reason=$GuardError"
    Goto path_done
  ${EndIf}
  StrCpy $R0 $R1
  ${Do}
    System::Call 'kernel32::GetFileAttributesW(w "$R0")i.R1 ?e'
    Pop $R3
    ${If} $R1 == -1
      ${If} $R3 != 2
      ${AndIf} $R3 != 3
        StrCpy $GuardError "Setup could not safely inspect this path:$\r$\n$R0$\r$\n$\r$\nCheck folder access and try again."
        !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPathChain.reject path=$R0 attributes=$R1 win32_error=$R3 reason=$GuardError"
        ${ExitDo}
      ${EndIf}
    ${Else}
      IntOp $R2 $R1 & 0x400
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
  ${Loop}
  path_done:
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
  !define ND2WSI_GUARD_FUNCTION "${PREFIX}CheckPathChain"
  !insertmacro ND2WSI_GUARD_PATH "${MARKER}"
  !insertmacro ND2WSI_GUARD_PATH "Uninstall.exe"
  !include "${PAYLOAD_GUARD_INCLUDE}"
  !undef ND2WSI_GUARD_FUNCTION
  !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckPayloadPaths.complete instdir=$INSTDIR guard=$GuardError"
FunctionEnd

Function ${PREFIX}AcquireSetupMutex
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\nd2wsi-viewer-Setup-current-user")p.r0 ?e'
  Pop $1
  StrCpy $SetupMutex $0
  !insertmacro ND2WSI_TRACE "${PREFIX}" "AcquireSetupMutex handle=$0 win32_error=$1"
  ${If} $0 == 0
  ${OrIf} $1 == 183
    !insertmacro ND2WSI_TRACE "${PREFIX}" "MessageBox phase=${PREFIX}AcquireSetupMutex message=Another nd2wsi-viewer installer or uninstaller is running. Close it before continuing."
    MessageBox MB_OK|MB_ICONEXCLAMATION "Another nd2wsi-viewer installer or uninstaller is running. Close it before continuing." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "${PREFIX}" "Abort phase=${PREFIX}AcquireSetupMutex guard=$GuardError"
    Abort
  ${EndIf}
FunctionEnd

Function ${PREFIX}FindRunningApp
  ; The default Unicode NSIS stub is x86. PROCESSENTRY32W is 556 bytes,
  ; with szExeFile at byte 36. A process snapshot includes x64/ARM64 apps.
  ; Return ProcessState: 0 absent, 1 present, 2 inspection failed.
  StrCpy $ProcessState 2
  StrCpy $ProcessCheckedCount 0
  System::Call 'kernel32::CreateToolhelp32Snapshot(i 2, i 0)p.r0 ?e'
  Pop $ProcessInspectError
  !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.snapshot handle=$0 win32_error=$ProcessInspectError"
  ${If} $0 == -1
    Return
  ${EndIf}
  System::Alloc 556
  Pop $1
  ${If} $1 == 0
    !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.allocate_failed bytes=556"
    System::Call 'kernel32::CloseHandle(p r0)'
    Return
  ${EndIf}
  System::Call '*$1(i 556)'
  System::Call 'kernel32::Process32FirstW(p r0, p r1)i.r2 ?e'
  Pop $ProcessInspectError
  !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.first result=$2 win32_error=$ProcessInspectError buffer=$1 size=556"
  ${If} $2 != 0
    StrCpy $ProcessState 0
    ${Do}
      IntOp $ProcessCheckedCount $ProcessCheckedCount + 1
      System::Call '*$1(i, i, i, p, i, i, i, i, i, &w260.r3)'
      ${If} $3 == "${APP_EXE}"
        StrCpy $ProcessState 1
        !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.match name=$3"
        ${ExitDo}
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
  !insertmacro ND2WSI_TRACE "${PREFIX}" "FindRunningApp.complete state=$ProcessState checked=$ProcessCheckedCount"
FunctionEnd

Function ${PREFIX}RequireAppClosed
  retry:
  Call ${PREFIX}FindRunningApp
  !insertmacro ND2WSI_TRACE "${PREFIX}" "RequireAppClosed state=$ProcessState"
  ${If} $ProcessState == 0
    Return
  ${EndIf}
  ${If} $ProcessState == 1
    !insertmacro ND2WSI_TRACE "${PREFIX}" "MessageBox phase=${PREFIX}RequireAppClosed message=nd2wsi-viewer is running. Save your work and close every nd2wsi-viewer window, then choose Retry."
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "nd2wsi-viewer is running. Save your work and close every nd2wsi-viewer window, then choose Retry." /SD IDCANCEL IDRETRY retry
  ${Else}
    !insertmacro ND2WSI_TRACE "${PREFIX}" "MessageBox phase=${PREFIX}RequireAppClosed message=Setup could not check whether nd2wsi-viewer is running. Close the app, then choose Retry."
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Setup could not check whether nd2wsi-viewer is running. Close the app, then choose Retry." /SD IDCANCEL IDRETRY retry
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
  System::Call 'kernel32::MoveFileExW(w "$R1", w "$INSTDIR\${MARKER}", i 9)i.R3 ?e'
  Pop $R4
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
    GetFullPathName $R3 "$R3"
  ${EndIf}
  ${If} $R3 != $INSTDIR
    StrCpy $MarkerWriteFailed 1
  ${EndIf}
  Goto marker_done
  marker_cleanup:
  System::Call 'kernel32::DeleteFileW(w "$R1")i.R3 ?e'
  Pop $R4
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
  System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r0, p 0)i.r1 ?e'
  Pop $2
  ${If} $1 == 0
  ${OrIf} $1 >= ${NSIS_MAX_STRLEN}
    StrCpy $GuardError "Setup could not resolve the installation folder:$\r$\n$INSTDIR$\r$\n$\r$\nChoose a shorter, valid app folder and try again."
    !insertmacro ND2WSI_TRACE "${PREFIX}" "CheckOwnedDirectory.normalize_failed instdir=$INSTDIR returned_length=$1 win32_error=$2 reason=$GuardError"
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
    GetFullPathName $2 "$2"
  ${EndIf}
  ${If} $0 != "${PRODUCT_ID}"
  ${OrIf} $1 != "${APP_VERSION}"
  ${OrIf} $2 != $INSTDIR
    StrCpy $GuardError "This folder is not a registered nd2wsi-viewer ${APP_VERSION} installation. Choose a new, empty folder, or the folder used by this version of Setup."
  ${EndIf}
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
  ${IfNot} ${AtLeastWin10}
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=.onInit message=nd2wsi-viewer requires Windows 10 version 1709 or later, or Windows 11, on x64. ARM64 requires Windows 11."
    MessageBox MB_OK|MB_ICONSTOP "nd2wsi-viewer requires Windows 10 version 1709 or later, or Windows 11, on x64. ARM64 requires Windows 11." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=.onInit guard=$GuardError"
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin11}
  ${AndIfNot} ${AtLeastBuild} 16299
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=.onInit message=nd2wsi-viewer requires Windows 10 version 1709 (build 16299) or later for Microsoft WebView2. Update Windows before running Setup. Windows 10 Education version 22H2 is recommended."
    MessageBox MB_OK|MB_ICONSTOP "nd2wsi-viewer requires Windows 10 version 1709 (build 16299) or later for Microsoft WebView2. Update Windows before running Setup. Windows 10 Education version 22H2 is recommended." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=.onInit guard=$GuardError"
    Abort
  ${EndIf}
  ${If} ${IsServerOS}
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=.onInit message=This installer supports Windows 10 and 11 desktop editions, including Education. Windows Server editions are not supported."
    MessageBox MB_OK|MB_ICONSTOP "This installer supports Windows 10 and 11 desktop editions, including Education. Windows Server editions are not supported." /SD IDOK
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
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=RequireDotNetFramework message=nd2wsi-viewer requires Microsoft .NET Framework 4.6.2 or later. Install Microsoft .NET Framework 4.8 or update Windows, then run Setup again. Windows 10 version 22H2 already includes .NET Framework 4.8."
    MessageBox MB_OK|MB_ICONSTOP "nd2wsi-viewer requires Microsoft .NET Framework 4.6.2 or later. Install Microsoft .NET Framework 4.8 or update Windows, then run Setup again. Windows 10 version 22H2 already includes .NET Framework 4.8." /SD IDOK
    SetErrorLevel 2
    !insertmacro ND2WSI_TRACE "" "Abort phase=RequireDotNetFramework guard=$GuardError"
    Abort
  ${EndIf}
FunctionEnd

Function CheckInstallDirectory
  !insertmacro ND2WSI_TRACE "" "CheckInstallDirectory.begin instdir=$INSTDIR"
  StrCpy $InitialInstallFresh 0
  Call CheckPayloadPaths
  ${If} $GuardError != ""
    Return
  ${EndIf}
  Call CheckOwnedDirectory
  ${If} $GuardError == ""
    !insertmacro ND2WSI_TRACE "" "CheckInstallDirectory.existing_owned instdir=$INSTDIR"
    Return
  ${EndIf}
  ; A registered installation cannot silently be moved or overwritten by a
  ; second one. Uninstall it first; the user's data is retained.
  ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
  ${If} $0 != ""
    StrCpy $GuardError "nd2wsi-viewer is already registered in:$\r$\n$0$\r$\n$\r$\nChoose that folder to reinstall the same version, or uninstall the existing app first."
    Return
  ${EndIf}
  ${GetRoot} "$INSTDIR" $0
  ${If} $INSTDIR == $0
  ${OrIf} $INSTDIR == "$0\"
    Return
  ${EndIf}
  ; Reject a file or directory junction at the chosen destination.
  System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR")i.r0'
  !insertmacro ND2WSI_TRACE "" "CheckInstallDirectory.attributes value=$0"
  ${If} $0 != -1
    IntOp $1 $0 & 0x400
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
  Call CheckInstallDirectory
  ${If} $GuardError != ""
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=VerifyDirectoryPage message=$GuardError"
    MessageBox MB_OK|MB_ICONEXCLAMATION "$GuardError" /SD IDOK
    !insertmacro ND2WSI_TRACE "" "Abort phase=VerifyDirectoryPage guard=$GuardError"
    Abort
  ${EndIf}
FunctionEnd

Function FindWebView2
  StrCpy $WebView2Version ""
  SetRegView 64
  ReadRegStr $0 HKCU "${WEBVIEW2_KEY}" "pv"
  !insertmacro ND2WSI_TRACE "" "FindWebView2.HKCU64 value=$0"
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $WebView2Version $0
    Return
  ${EndIf}
  ReadRegStr $0 HKLM "${WEBVIEW2_KEY}" "pv"
  !insertmacro ND2WSI_TRACE "" "FindWebView2.HKLM64 value=$0"
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $WebView2Version $0
    Return
  ${EndIf}
  SetRegView 32
  ReadRegStr $0 HKLM "${WEBVIEW2_KEY}" "pv"
  !insertmacro ND2WSI_TRACE "" "FindWebView2.HKLM32 value=$0"
  SetRegView 64
  ${If} $0 != ""
  ${AndIf} $0 != "0.0.0.0"
    StrCpy $WebView2Version $0
  ${EndIf}
FunctionEnd

Function EnsureWebView2
  !insertmacro ND2WSI_TRACE "" "EnsureWebView2.begin"
  Call FindWebView2
  !insertmacro ND2WSI_TRACE "" "EnsureWebView2.detected version=$WebView2Version"
  ${If} $WebView2Version != ""
    DetailPrint "Microsoft Edge WebView2 Runtime: $WebView2Version"
    Return
  ${EndIf}
  IfSilent install_runtime
  !insertmacro ND2WSI_TRACE "" "MessageBox phase=EnsureWebView2 message=nd2wsi-viewer needs Microsoft Edge WebView2 Runtime. Choose OK to download and install it from Microsoft. An Internet connection is required."
  MessageBox MB_OKCANCEL|MB_ICONINFORMATION "nd2wsi-viewer needs Microsoft Edge WebView2 Runtime. Choose OK to download and install it from Microsoft. An Internet connection is required." IDCANCEL cancelled
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
  System::Call 'kernel32::MoveFileExW(w "$0", w "$INSTDIR\Uninstall.exe", i 9)i.r1 ?e'
  Pop $2
  !insertmacro ND2WSI_TRACE "" "WriteSafeUninstaller.MoveFileEx result=$1 win32_error=$2 source=$0 destination=$INSTDIR\Uninstall.exe"
  ${If} $1 == 0
    StrCpy $UninstallerWriteFailed 1
    DetailPrint "Could not replace Uninstall.exe (Windows error $2)."
    Goto uninstaller_cleanup
  ${EndIf}
  StrCpy $UninstallerTempPath ""
  Goto uninstaller_done
  uninstaller_cleanup:
  System::Call 'kernel32::DeleteFileW(w "$0")i.r1 ?e'
  Pop $2
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
      GetFullPathName $0 "$0"
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

Section "nd2wsi-viewer (required)" SecApplication
  SectionIn RO
  !insertmacro ND2WSI_TRACE "" "SecApplication.begin instdir=$INSTDIR"
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
    !insertmacro ND2WSI_TRACE "" "MessageBox phase=SecApplication message=Setup found an existing or inaccessible nd2wsi-viewer uninstall registry key without a matching installation. Restore or remove that old registration before installing into a new folder. No installation files were created."
    MessageBox MB_OK|MB_ICONSTOP "Setup found an existing or inaccessible nd2wsi-viewer uninstall registry key without a matching installation. Restore or remove that old registration before installing into a new folder. No installation files were created." /SD IDOK
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
  SetOutPath "$INSTDIR"
  CreateDirectory "$SMPROGRAMS\nd2wsi-viewer"
  CreateShortcut "$SMPROGRAMS\nd2wsi-viewer\nd2wsi-viewer.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\nd2wsi-viewer\Uninstall nd2wsi-viewer.lnk" "$INSTDIR\Uninstall.exe"
  !insertmacro ND2WSI_TRACE "" "SecApplication.complete"
SectionEnd

Section "Desktop shortcut" SecDesktop
  SetOutPath "$INSTDIR"
  CreateShortcut "$DESKTOP\nd2wsi-viewer.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

; These helpers distinguish harmless absence/nonempty user directories from
; access, sharing, and other actual removal failures. NSIS FileExists/RMDir
; alone cannot make that distinction reliably.
!macro DEFINE_REMOVAL_FUNCTIONS PREFIX
Function ${PREFIX}DeleteOwnedFile
  Exch $R0
  Push $R1
  Push $R2
  System::Call 'kernel32::DeleteFileW(w "$R0")i.R1 ?e'
  Pop $R2
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
  System::Call 'kernel32::RemoveDirectoryW(w "$R0")i.R1 ?e'
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
  StrCpy $GuardError "This folder is not a registered nd2wsi-viewer ${APP_VERSION} installation or a recoverable interrupted uninstall. No app files have been removed."
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
      GetFullPathName $3 "$3"
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
  System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR\${MARKER}")i.r0 ?e'
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
    GetFullPathName $3 "$3"
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
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "nd2wsi-viewer"
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

Function un.onInit
  Call un.InitInstallerLog
  !insertmacro ND2WSI_TRACE "un." "un.onInit.begin instdir=$INSTDIR exepath=$EXEPATH"
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
  Push "$SMPROGRAMS\nd2wsi-viewer\nd2wsi-viewer.lnk"
  Call un.CheckPathChain
  Push "$SMPROGRAMS\nd2wsi-viewer\Uninstall nd2wsi-viewer.lnk"
  Call un.CheckPathChain
  Push "$DESKTOP\nd2wsi-viewer.lnk"
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
  Push "$SMPROGRAMS\nd2wsi-viewer\nd2wsi-viewer.lnk"
  Call un.DeleteOwnedFile
  Push "$SMPROGRAMS\nd2wsi-viewer\Uninstall nd2wsi-viewer.lnk"
  Call un.DeleteOwnedFile
  Push "$SMPROGRAMS\nd2wsi-viewer"
  Call un.RemoveEmptyOwnedDirectory
  Push "$DESKTOP\nd2wsi-viewer.lnk"
  Call un.DeleteOwnedFile
  ${If} $UninstallHadErrors != 0
    Goto removal_failed
  ${EndIf}
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

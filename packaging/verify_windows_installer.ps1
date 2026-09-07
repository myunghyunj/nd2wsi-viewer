#requires -Version 5.1
<#
Verify the exact local Setup.exe without Python or a development environment.
The initial installation is in a private, marked QA directory. Successful
uninstall must preserve scientific sentinels and application data. Pass
-KeepInstalled to finish with a verified installation in the normal user path.
#>
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$PayloadManifest,
    [Parameter(Mandatory = $true)][string]$SmokeFile,
    [Parameter(Mandatory = $true)][string]$ResultsDir,
    [Parameter(Mandatory = $true)][string]$WebView2Bootstrapper,
    [string]$BuildManifest,
    [ValidatePattern('^[0-9a-f]{64}$')][string]$WebView2BootstrapperSha256 = '17debf797a6c737959bc588236e897936ffac1af5f7e515e674ab32f9edfe719',
    [switch]$KeepInstalled
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$productId = 'nd2wsi-viewer.windows.x64.current-user'
$registrySubkey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\nd2wsi-viewer'
$expectedFixtureHash = '51d603317947b04c3b43c8e694f3ba97a16478ce30e795857ec6ae0321d41c33'
$resultPath = [IO.Path]::GetFullPath($ResultsDir)
$defaultInstall = Join-Path $env:LOCALAPPDATA 'Programs\nd2wsi-viewer'
$qaBase = Join-Path $env:LOCALAPPDATA 'nd2wsi-installer-qa'
$runId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$qaRoot = Join-Path $qaBase $runId
$qaMarker = Join-Path $qaRoot '.nd2wsi-installer-qa.json'
# Keep the script ASCII: Windows PowerShell 5.1 does not default to UTF-8.
$koreanPath = [string][char]0xD55C + [char]0xAE00 + ' ' + [char]0xACBD + [char]0xB85C
$koreanCell = [string][char]0xC138 + [char]0xD3EC
$installPath = Join-Path $qaRoot $koreanPath
$fixturePath = Join-Path $qaRoot ($koreanCell + ' sample.nd2')
$runtimeTemp = Join-Path $qaRoot 'runtime-temp'
$appData = Join-Path $env:LOCALAPPDATA 'nd2wsi-viewer'
$appDataSentinel = Join-Path $appData ('installer-qa-' + $runId + '.keep.txt')
$programs = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
$desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
$menuDirectory = Join-Path $programs 'nd2wsi-viewer'
$appShortcut = Join-Path $menuDirectory 'nd2wsi-viewer.lnk'
$uninstallShortcut = Join-Path $menuDirectory 'Uninstall nd2wsi-viewer.lnk'
$desktopShortcut = Join-Path $desktop 'nd2wsi-viewer.lnk'
$script:stepNumber = 0
$script:phase = 'preflight'
$script:steps = [Collections.Generic.List[object]]::new()
$script:sentinels = [Collections.Generic.List[object]]::new()
$script:uninstallFaultChecks = [Collections.Generic.List[object]]::new()
$script:activeProcess = $null
$script:payloadFiles = $null
$script:version = '1.2.8'
$previousLog = [Environment]::GetEnvironmentVariable('ND2WSI_LOG_FILE', 'Process')
$transcriptStarted = $false
$qaCreated = $false
$uninstallVerified = $false
$success = $false

function Test-InsidePath {
    param([string]$Candidate, [string]$Parent)
    $value = [IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $prefix = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return $value.Equals($prefix, [StringComparison]::OrdinalIgnoreCase) -or
        $value.StartsWith($prefix + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-LiteralEntry {
    param([string]$Path)
    # Get-Item also sees reparse entries whose target is missing.
    try { return $null -ne (Get-Item -LiteralPath $Path -Force -ErrorAction Stop) }
    catch {
        if ($_.CategoryInfo.Category -eq [Management.Automation.ErrorCategory]::ObjectNotFound) { return $false }
        throw
    }
}

function Write-State {
    param([string]$State, [string]$Message)
    $value = @{
        state = $State; phase = $script:phase; message = $Message
        run_id = $runId; qa_root = $qaRoot
        updated = (Get-Date).ToUniversalTime().ToString('o')
    }
    if ($null -ne $script:activeProcess) { $value.process_id = $script:activeProcess }
    $value | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $resultPath 'installer-status.json') -Encoding UTF8
    Write-Host $Message
}

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-CheckedProcess {
    param(
        [string]$Program, [string]$Arguments, [string]$Phase,
        [int]$TimeoutSeconds = 300, [string]$WorkingDirectory = $resultPath,
        [hashtable]$EnvironmentOverrides = @{}, [int[]]$ExpectedExitCodes = @(0)
    )
    $script:phase = $Phase
    $script:stepNumber += 1
    $prefix = '{0:D2}-{1}' -f $script:stepNumber, $Phase
    $stdoutPath = Join-Path $resultPath ($prefix + '.stdout.log')
    $stderrPath = Join-Path $resultPath ($prefix + '.stderr.log')
    $installerTracePath = Join-Path $resultPath ($prefix + '.installer-trace.log')
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Program
    $start.Arguments = $Arguments
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    $start.EnvironmentVariables['ND2WSI_INSTALLER_LOG'] = $installerTracePath
    foreach ($key in $EnvironmentOverrides.Keys) { $start.EnvironmentVariables[$key] = [string]$EnvironmentOverrides[$key] }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    $stdout = $null
    $stderr = $null
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $exitCode = $null
    try {
        $stdout = [IO.FileStream]::new($stdoutPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite, 1, [IO.FileOptions]::Asynchronous)
        $stderr = [IO.FileStream]::new($stderrPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite, 1, [IO.FileOptions]::Asynchronous)
        if (-not $process.Start()) { throw "Could not start $Program" }
        $script:activeProcess = $process.Id
        $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
        $stderrTask = $process.StandardError.BaseStream.CopyToAsync($stderr)
        Write-State 'running' ("$Phase started (PID $($process.Id)).")
        $lastUpdate = 0
        while (-not $process.WaitForExit(500)) {
            if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                # Only terminate the exact process started by this invocation.
                $process.Kill()
                $process.WaitForExit(10000) | Out-Null
                throw "$Phase exceeded $TimeoutSeconds seconds. Its process was stopped; QA files are retained."
            }
            if ($timer.Elapsed.TotalSeconds - $lastUpdate -ge 5) {
                $lastUpdate = $timer.Elapsed.TotalSeconds
                Write-State 'running' ("$Phase running: $([int]$lastUpdate) seconds.")
            }
        }
        if (-not [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($stdoutTask, $stderrTask), 10000)) {
            throw "$Phase exited but inherited log pipes did not close within 10 seconds."
        }
        $exitCode = $process.ExitCode
        if ($exitCode -notin $ExpectedExitCodes) {
            $detail = ''
            if (Test-Path -LiteralPath $installerTracePath -PathType Leaf) {
                $detail = "`r`nInstaller detail:`r`n" + ((Get-Content -LiteralPath $installerTracePath -Encoding Unicode | Select-Object -Last 12) -join "`r`n")
            }
            throw "$Phase returned unexpected exit code $exitCode. Expected $($ExpectedExitCodes -join ', '). See $stdoutPath and $stderrPath.$detail"
        }
    }
    finally {
        $timer.Stop()
        $script:steps.Add(@{
            phase = $Phase; program = $Program; arguments = $Arguments
            exit_code = $exitCode; expected_exit_codes = $ExpectedExitCodes; seconds = $timer.Elapsed.TotalSeconds
            stdout = $stdoutPath; stderr = $stderrPath
            installer_trace = $installerTracePath
        })
        $script:activeProcess = $null
        if ($null -ne $stdout) { $stdout.Dispose() }
        if ($null -ne $stderr) { $stderr.Dispose() }
        $process.Dispose()
    }
}

function Get-InstalledRegistration {
    $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Registry64)
    $key = $null
    try {
        $key = $root.OpenSubKey($registrySubkey)
        if ($null -eq $key) { return $null }
        $result = @{}
        foreach ($name in $key.GetValueNames()) { $result[$name] = $key.GetValue($name) }
        return $result
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
        $root.Dispose()
    }
}

function Get-WebView2Version {
    $path = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryHive]::LocalMachine)) {
        foreach ($view in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
            $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
            $key = $null
            try {
                $key = $root.OpenSubKey($path)
                if ($null -ne $key) {
                    $value = [string]$key.GetValue('pv', '')
                    if ($value -and $value -ne '0.0.0.0') { return $value }
                }
            }
            finally {
                if ($null -ne $key) { $key.Dispose() }
                $root.Dispose()
            }
        }
    }
    return $null
}

function Get-TargetReadiness {
    $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
    $osKey = $null
    $netKey = $null
    $vcKey = $null
    try {
        $osKey = $root.OpenSubKey('Software\Microsoft\Windows NT\CurrentVersion')
        if ($null -eq $osKey) { throw 'Windows edition/build information is unavailable.' }
        $netKey = $root.OpenSubKey('Software\Microsoft\NET Framework Setup\NDP\v4\Full')
        $vcKey = $root.OpenSubKey('Software\Microsoft\VisualStudio\14.0\VC\Runtimes\x64')
        $result = @{
            product_name = [string]$osKey.GetValue('ProductName', '')
            edition_id = [string]$osKey.GetValue('EditionID', '')
            display_version = [string]$osKey.GetValue('DisplayVersion', $osKey.GetValue('ReleaseId', ''))
            build = [int]$osKey.GetValue('CurrentBuildNumber', 0)
            update_revision = [int]$osKey.GetValue('UBR', 0)
            installation_type = [string]$osKey.GetValue('InstallationType', '')
            os_is_64_bit = [Environment]::Is64BitOperatingSystem
            verifier_is_64_bit = [Environment]::Is64BitProcess
            net_framework_release = 0
            visual_cpp_x64_registered = $false
            visual_cpp_x64_version = $null
        }
        if ($null -ne $netKey) { $result.net_framework_release = [int]$netKey.GetValue('Release', 0) }
        if ($null -ne $vcKey) {
            $result.visual_cpp_x64_registered = [int]$vcKey.GetValue('Installed', 0) -eq 1
            $result.visual_cpp_x64_version = [string]$vcKey.GetValue('Version', '')
        }
    }
    finally {
        if ($null -ne $vcKey) { $vcKey.Dispose() }
        if ($null -ne $netKey) { $netKey.Dispose() }
        if ($null -ne $osKey) { $osKey.Dispose() }
        $root.Dispose()
    }
    if ($result.net_framework_release -eq 0) {
        $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry32)
        $key = $null
        try {
            $key = $root.OpenSubKey('Software\Microsoft\NET Framework Setup\NDP\v4\Full')
            if ($null -ne $key) { $result.net_framework_release = [int]$key.GetValue('Release', 0) }
        }
        finally {
            if ($null -ne $key) { $key.Dispose() }
            $root.Dispose()
        }
    }
    $result.net_framework_462_or_newer = $result.net_framework_release -ge 394802
    $result.webview2_version = Get-WebView2Version
    return $result
}

function Get-PayloadPath {
    param([string]$Directory, [string]$Relative)
    if (-not $Relative -or $Relative -match '(^|/)\.{1,2}(/|$)|[:\\]|^/|//' -or [IO.Path]::IsPathRooted($Relative)) {
        throw "Unsafe payload path: $Relative"
    }
    $path = [IO.Path]::GetFullPath((Join-Path $Directory $Relative.Replace('/', '\')))
    if (-not (Test-InsidePath $path $Directory) -or $path -eq $Directory) { throw "Payload path escapes installation: $Relative" }
    return $path
}

function Assert-Payload {
    param([string]$Directory)
    $script:phase = 'payload-hashes'
    $count = 0
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $lastUpdate = 0
    foreach ($entry in $script:payloadFiles.PSObject.Properties) {
        $path = Get-PayloadPath $Directory $entry.Name
        $item = Get-Item -LiteralPath $path -Force
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Payload is not a regular file: $path" }
        if ((Get-Sha256 $path) -cne [string]$entry.Value) { throw "Installed payload checksum mismatch: $($entry.Name)" }
        $count += 1
        if ($timer.Elapsed.TotalSeconds - $lastUpdate -ge 5) {
            $lastUpdate = $timer.Elapsed.TotalSeconds
            Write-State 'running' ("Verified $count installed payload files.")
        }
    }
    Write-Host "Verified $count installed payload file hashes."
}

function Get-InstalledFileNames {
    param([string]$Directory)
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Directory)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        $rootItem = Get-Item -LiteralPath $current -Force
        if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Installation contains a directory reparse point: $current" }
        foreach ($entry in Get-ChildItem -LiteralPath $current -Force) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Installation contains a reparse point: $($entry.FullName)" }
            if ($entry.PSIsContainer) { $pending.Push($entry.FullName) }
            else { $entry.FullName.Substring($Directory.TrimEnd('\').Length + 1) }
        }
    }
}

function Assert-NoUnexpectedInstalledFiles {
    param([string]$Directory, [string[]]$Expected)
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($name in $Expected) { $names.Add($name) | Out-Null }
    foreach ($name in Get-InstalledFileNames $Directory) {
        if (-not $names.Remove($name)) { throw "The app wrote an unexpected file inside its installation: $name" }
    }
    if ($names.Count -ne 0) { throw 'An installed file disappeared during runtime verification.' }
}

function Initialize-WideShortcutReader {
    # WScript.Shell can lose non-ANSI target characters when reading a valid
    # Unicode link. Use the explicit wide interface without resolving/saving.
if (-not ('Nd2wsiShortcutReader' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public sealed class Nd2wsiShortcutDetails
{
    public string TargetPath { get; internal set; }
    public string Arguments { get; internal set; }
    public string WorkingDirectory { get; internal set; }
}

public static class Nd2wsiShortcutReader
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellLinkW
    {
        [PreserveSig] int GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path,
            int capacity, IntPtr findData, uint flags);
        [PreserveSig] int GetIDList(out IntPtr itemIdList);
        [PreserveSig] int SetIDList(IntPtr itemIdList);
        [PreserveSig] int GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text,
            int capacity);
        [PreserveSig] int SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        [PreserveSig] int GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path,
            int capacity);
        [PreserveSig] int SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        [PreserveSig] int GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder arguments,
            int capacity);
    }

    public static Nd2wsiShortcutDetails Read(string shortcutPath)
    {
        if (!Path.IsPathRooted(shortcutPath))
            throw new ArgumentException("The shortcut path must be absolute.", "shortcutPath");
        object instance = new ShellLink();
        try
        {
            // STGM_READ. Loading and querying do not resolve, modify, or save the link.
            ((IPersistFile)instance).Load(shortcutPath, 0);
            IShellLinkW link = (IShellLinkW)instance;
            StringBuilder target = new StringBuilder(32768);
            StringBuilder arguments = new StringBuilder(32768);
            StringBuilder working = new StringBuilder(32768);
            Marshal.ThrowExceptionForHR(link.GetPath(target, target.Capacity, IntPtr.Zero, 4));
            Marshal.ThrowExceptionForHR(link.GetArguments(arguments, arguments.Capacity));
            Marshal.ThrowExceptionForHR(link.GetWorkingDirectory(working, working.Capacity));
            return new Nd2wsiShortcutDetails {
                TargetPath = target.ToString(), Arguments = arguments.ToString(),
                WorkingDirectory = working.ToString()
            };
        }
        finally
        {
            if (Marshal.IsComObject(instance)) Marshal.FinalReleaseComObject(instance);
        }
    }
}
'@
}
}

function Assert-Shortcut {
    param([string]$Path, [string]$Target)
    if (-not (Test-LiteralEntry $Path)) { throw "Missing shortcut: $Path" }
    Initialize-WideShortcutReader
    $link = [Nd2wsiShortcutReader]::Read($Path)
    if (-not [string]::Equals($link.TargetPath, $Target, [StringComparison]::OrdinalIgnoreCase)) { throw "Shortcut target mismatch: $Path" }
    if ($link.Arguments -cne '') { throw "Unexpected shortcut arguments: $Path" }
    $expectedWorkingDirectory = [IO.Path]::GetDirectoryName($Target)
    if (-not [string]::Equals($link.WorkingDirectory, $expectedWorkingDirectory, [StringComparison]::OrdinalIgnoreCase)) { throw "Shortcut working directory mismatch: $Path" }
}

function Assert-Installation {
    param([string]$Directory)
    Assert-Payload $Directory
    $registration = Get-InstalledRegistration
    if ($null -eq $registration) { throw 'The current-user uninstall registration is missing.' }
    if (-not [string]::Equals([string]$registration.InstallLocation, $Directory, [StringComparison]::OrdinalIgnoreCase)) { throw 'Uninstall InstallLocation does not match this test installation.' }
    if ($registration.DisplayVersion -ne $script:version -or $registration.Publisher -ne 'nd2wsi-viewer') { throw 'Uninstall display metadata is incorrect.' }
    $uninstaller = Join-Path $Directory 'Uninstall.exe'
    $expectedUninstall = '"' + $uninstaller + '"'
    if ([string]$registration.UninstallString -cne $expectedUninstall -or [string]$registration.QuietUninstallString -cne ($expectedUninstall + ' /S')) { throw 'Uninstall commands are not correctly quoted.' }
    $marker = Join-Path $Directory '.nd2wsi-install.ini'
    $markerText = Get-Content -LiteralPath $marker -Raw
    if ($markerText -notmatch ('(?m)^ProductId=' + [regex]::Escape($productId) + '\s*$') -or $markerText -notmatch ('(?m)^Version=' + [regex]::Escape($script:version) + '\s*$')) { throw 'The installer ownership marker is incorrect.' }
    if (-not (Test-LiteralEntry $uninstaller)) { throw 'The installed uninstaller is missing.' }
    Assert-Shortcut $appShortcut (Join-Path $Directory 'nd2wsi-viewer.exe')
    Assert-Shortcut $uninstallShortcut $uninstaller
    Assert-Shortcut $desktopShortcut (Join-Path $Directory 'nd2wsi-viewer.exe')
    if (-not (Get-WebView2Version)) { throw 'The WebView2 Runtime prerequisite is missing after installation.' }
}

function Add-Sentinel {
    param([string]$Path)
    if (Test-LiteralEntry $Path) { throw "Refusing to replace an existing sentinel path: $Path" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    [IO.File]::WriteAllText($Path, ('Scientific data preservation check: ' + $runId), [Text.UTF8Encoding]::new($false))
    $script:sentinels.Add(@{ path = $Path; sha256 = (Get-Sha256 $Path) })
}

function Assert-Sentinels {
    foreach ($item in $script:sentinels) {
        if (-not (Test-LiteralEntry $item.path) -or (Get-Sha256 $item.path) -cne $item.sha256) { throw "Scientific/application data was changed or removed: $($item.path)" }
    }
    if ((Get-Sha256 $sourcePath) -cne $expectedFixtureHash -or (Get-Sha256 $fixturePath) -cne $expectedFixtureHash) { throw 'The original scientific source or QA fixture was changed.' }
}

function Invoke-AppSmoke {
    param([string]$Directory, [string]$Label, [switch]$GuiOnly)
    $exe = Join-Path $Directory 'nd2wsi-viewer.exe'
    $modes = if ($GuiOnly) { @('gui-smoke') } else { @('smoke', 'gui-smoke') }
    foreach ($mode in $modes) {
        $log = Join-Path $resultPath ($Label + '-' + $mode + '.log')
        $report = Join-Path $resultPath ($Label + '-gui-smoke.json')
        Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
        $env:ND2WSI_LOG_FILE = $log
        $arguments = @("--$mode", $fixturePath)
        if ($mode -eq 'gui-smoke') {
            Remove-Item -LiteralPath $report -Force -ErrorAction SilentlyContinue
            $arguments += @('--smoke-report', $report)
        }
        $commandLine = (($arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
        $timeout = if ($mode -eq 'smoke') { 600 } else { 180 }
        Invoke-CheckedProcess $exe $commandLine ($Label + '-' + $mode) $timeout (Join-Path $env:WINDIR 'System32') -EnvironmentOverrides @{ TEMP = $runtimeTemp; TMP = $runtimeTemp }
        if ($mode -eq 'gui-smoke') {
            if (-not (Test-LiteralEntry $report) -or (Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json).ok -ne $true) { throw 'Installed GUI smoke did not record success.' }
        }
        elseif (-not (Test-LiteralEntry $log) -or (Get-Content -LiteralPath $log -Raw -Encoding UTF8) -notmatch 'smoke ok') { throw 'Installed scientific smoke did not record success.' }
    }
}

function Assert-JunctionRejection {
    $script:phase = 'junction-preservation'
    $internal = Join-Path $installPath '_internal'
    $backup = Join-Path $qaRoot 'payload-internal-backup'
    $outside = Join-Path $qaRoot 'junction-outside'
    $qaUninstaller = Join-Path $qaRoot 'junction-qa-Uninstall.exe'
    $uninstaller = Join-Path $installPath 'Uninstall.exe'
    $installMarker = Join-Path $installPath '.nd2wsi-install.ini'
    $internalFiles = @($script:payloadFiles.PSObject.Properties | Where-Object { $_.Name.StartsWith('_internal/', [StringComparison]::Ordinal) })
    if ($internalFiles.Count -eq 0) { throw 'Cannot exercise the junction guard without an _internal payload.' }
    $relativeTarget = $internalFiles[0].Name.Substring('_internal/'.Length)
    Add-Sentinel (Get-PayloadPath $outside $relativeTarget)
    $markerHash = Get-Sha256 $installMarker
    $uninstallerHash = Get-Sha256 $uninstaller
    $registration = Get-InstalledRegistration
    Copy-Item -LiteralPath $uninstaller -Destination $qaUninstaller
    if ((Get-Sha256 $qaUninstaller) -cne $uninstallerHash) { throw 'Junction-test uninstaller copy checksum mismatch.' }
    if (Test-LiteralEntry $backup) { throw 'Junction-test backup path is already occupied.' }
    $moved = $false
    try {
        [IO.Directory]::Move($internal, $backup)
        $moved = $true
        New-Item -ItemType Junction -Path $internal -Target $outside | Out-Null
        if (-not ((Get-Item -LiteralPath $internal -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'The QA junction was not created.' }
        Invoke-CheckedProcess $installerPath ('/S /D=' + $installPath) 'reject-junction-reinstall' 120 -ExpectedExitCodes @(2)
        Invoke-CheckedProcess $qaUninstaller ('/S _?=' + $installPath) 'reject-junction-uninstall' 120 -ExpectedExitCodes @(2)
        if ((Get-Sha256 $installMarker) -cne $markerHash -or (Get-Sha256 $uninstaller) -cne $uninstallerHash) { throw 'Rejected junction operation changed installer ownership files.' }
        $after = Get-InstalledRegistration
        if ($null -eq $after) { throw 'Rejected junction operation removed uninstall registration.' }
        foreach ($key in $registration.Keys) {
            if ($after[$key] -cne $registration[$key]) { throw "Rejected junction operation changed uninstall registration: $key" }
        }
        Assert-Sentinels
        Assert-Shortcut $appShortcut (Join-Path $installPath 'nd2wsi-viewer.exe')
        Assert-Shortcut $uninstallShortcut $uninstaller
        Assert-Shortcut $desktopShortcut (Join-Path $installPath 'nd2wsi-viewer.exe')
    }
    finally {
        if ($moved) {
            if (Test-LiteralEntry $internal) {
                if (-not ((Get-Item -LiteralPath $internal -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'The QA junction changed unexpectedly; the payload backup was preserved for recovery.' }
                # Directory.Delete(..., false) removes this junction itself;
                # its scientific sentinel target is never traversed.
                [IO.Directory]::Delete($internal, $false)
            }
            [IO.Directory]::Move($backup, $internal)
        }
    }
    Assert-Installation $installPath
    Assert-Sentinels
}

function Get-OwnershipMarker {
    param([string]$Directory)
    $text = Get-Content -LiteralPath (Join-Path $Directory '.nd2wsi-install.ini') -Raw
    $values = @{}
    foreach ($name in @('ProductId', 'Version', 'UninstallState', 'InstallLocation')) {
        $matches = [regex]::Matches($text, ('(?m)^' + $name + '=([^\r\n]*)\r?$'))
        if ($matches.Count -gt 1) { throw "Duplicate installation marker field: $name" }
        $values[$name] = if ($matches.Count -eq 1) { $matches[0].Groups[1].Value } else { '' }
    }
    if ($values.ProductId -cne $productId -or $values.Version -cne $script:version) { throw 'The retry ownership marker lost its product or version identity.' }
    if ($values.InstallLocation -and -not [string]::Equals($values.InstallLocation, $Directory, [StringComparison]::OrdinalIgnoreCase)) { throw 'The retry ownership marker points outside this QA installation.' }
    return $values
}

function Assert-RegistrationUnchanged {
    param([hashtable]$Expected, [hashtable]$Actual)
    if ($null -eq $Actual -or $Actual.Count -ne $Expected.Count) { throw 'Failed uninstall changed or removed its retry registration.' }
    foreach ($name in $Expected.Keys) {
        if (-not $Actual.ContainsKey($name) -or $Actual[$name] -cne $Expected[$name]) { throw "Failed uninstall changed its retry registration: $name" }
    }
}

function Assert-RetryIdentity {
    param([string]$Directory, [string]$UninstallerHash, [hashtable]$Registration, [switch]$AllowFinalCleanupState)
    if ((Get-Sha256 (Join-Path $Directory 'Uninstall.exe')) -cne $UninstallerHash) { throw 'Failed uninstall did not preserve the runnable uninstaller.' }
    $after = Get-InstalledRegistration
    if (Test-LiteralEntry (Join-Path $Directory '.nd2wsi-install.ini')) {
        $marker = Get-OwnershipMarker $Directory
        if ($null -eq $after) {
            if (-not $AllowFinalCleanupState -or $marker.UninstallState -cne 'in-progress' -or -not [string]::Equals($marker.InstallLocation, $Directory, [StringComparison]::OrdinalIgnoreCase)) { throw 'Failed uninstall lost both registered and path-bound retry identity.' }
        }
        elseif ($AllowFinalCleanupState) {
            foreach ($name in $Registration.Keys) {
                if ($name -ne 'UninstallState' -and (-not $after.ContainsKey($name) -or $after[$name] -cne $Registration[$name])) { throw "Final cleanup changed a retained registration field: $name" }
            }
            foreach ($name in $after.Keys) {
                if (-not $Registration.ContainsKey($name) -and $name -notin @('ProductId', 'UninstallState')) { throw "Final cleanup added an unexpected registration field: $name" }
            }
            $stateUnchanged = $Registration.ContainsKey('UninstallState') -and $after.ContainsKey('UninstallState') -and $after['UninstallState'] -ceq $Registration['UninstallState']
            if (($after.ContainsKey('ProductId') -and [string]$after['ProductId'] -cne $productId) -or ($after.ContainsKey('UninstallState') -and [string]$after['UninstallState'] -cne 'in-progress' -and -not $stateUnchanged)) { throw 'Final cleanup retained incorrect registry recovery fields.' }
        }
        else { Assert-RegistrationUnchanged $Registration $after }
    }
    elseif (-not $AllowFinalCleanupState -or $null -eq $after -or [string]$after['ProductId'] -cne $productId -or [string]$after['DisplayVersion'] -cne $script:version -or [string]$after['UninstallState'] -cne 'in-progress' -or -not [string]::Equals([string]$after['InstallLocation'], $Directory, [StringComparison]::OrdinalIgnoreCase)) { throw 'Final cleanup removed the marker without retaining a path-bound registry retry identity.' }
    Assert-Sentinels
}

function Get-QaUninstallerCopy {
    param([string]$Directory, [string]$Label)
    $original = Join-Path $Directory 'Uninstall.exe'
    $copy = Join-Path $qaRoot ($Label + '-Uninstall.exe')
    if (Test-LiteralEntry $copy) { throw 'The new QA uninstaller copy path is occupied.' }
    Copy-Item -LiteralPath $original -Destination $copy
    if ((Get-Sha256 $copy) -cne (Get-Sha256 $original)) { throw 'QA uninstaller copy checksum mismatch.' }
    return $copy
}

function Assert-Uninstalled {
    param([string]$Directory)
    foreach ($entry in $script:payloadFiles.PSObject.Properties) {
        if (Test-LiteralEntry (Get-PayloadPath $Directory $entry.Name)) { throw "Uninstall left a shipped payload file: $($entry.Name)" }
    }
    foreach ($path in @((Join-Path $Directory '.nd2wsi-install.ini'), (Join-Path $Directory 'Uninstall.exe'), $appShortcut, $uninstallShortcut, $desktopShortcut)) {
        if (Test-LiteralEntry $path) { throw "Uninstall left an installer-owned file or shortcut: $path" }
    }
    if ($null -ne (Get-InstalledRegistration)) { throw 'Uninstall left the current-user uninstall registration.' }
    Assert-Sentinels
    if (-not (Get-WebView2Version)) { throw 'The shared WebView2 runtime was removed during uninstall.' }
}

function Install-FaultFixture {
    param([string]$Directory, [string]$Label)
    if (-not $qaCreated -or -not (Test-InsidePath $Directory $qaRoot) -or $Directory -eq $qaRoot -or (Test-LiteralEntry $Directory)) { throw 'Fault injection requires a new private QA installation path.' }
    if ($null -ne (Get-InstalledRegistration)) { throw 'The previous QA uninstall did not release its registration.' }
    Invoke-CheckedProcess $installerPath ('/S /D=' + $Directory) ('install-' + $Label) 900
    Assert-Installation $Directory
    Add-Sentinel (Join-Path $Directory 'scientific-data\keep.nd2')
    Add-Sentinel (Join-Path $Directory 'scientific-data\nd2wsi\annotations\keep.json')
    Add-Sentinel (Join-Path $Directory 'nd2wsi\caches\keep.txt')
    Assert-Sentinels
}

function Assert-LockedUninstallRecovery {
    param([string]$Directory, [ValidateSet('payload', 'desktop-shortcut', 'marker', 'uninstaller')][string]$Kind)
    $paths = @{
        payload = (Join-Path $Directory 'nd2wsi-viewer.exe')
        'desktop-shortcut' = $desktopShortcut
        marker = (Join-Path $Directory '.nd2wsi-install.ini')
        uninstaller = (Join-Path $Directory 'Uninstall.exe')
    }
    $lockedPath = $paths[$Kind]
    Assert-Installation $Directory
    $registration = Get-InstalledRegistration
    $uninstallerHash = Get-Sha256 (Join-Path $Directory 'Uninstall.exe')
    $lockedHash = Get-Sha256 $lockedPath
    $copy = Get-QaUninstallerCopy $Directory ('locked-' + $Kind)
    # Allow normal reads and marker progress updates, but deny deletion. This
    # handle targets only a file just installed by this private QA invocation.
    $lock = [IO.File]::Open($lockedPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        Invoke-CheckedProcess $copy ('/S _?=' + $Directory) ('reject-locked-' + $Kind + '-uninstall') 300 -ExpectedExitCodes @(2)
        if (-not (Test-LiteralEntry $lockedPath)) { throw "Uninstall removed its locked $Kind file." }
        if ($Kind -ne 'marker' -and (Get-Sha256 $lockedPath) -cne $lockedHash) { throw "Failed uninstall changed its locked $Kind file." }
        Assert-RetryIdentity $Directory $uninstallerHash $registration -AllowFinalCleanupState:($Kind -in @('marker', 'uninstaller'))
    }
    finally { $lock.Dispose() }
    # A copied, checksum-identical uninstaller gives a reliable process exit
    # code while allowing the installed Uninstall.exe to be deleted.
    Invoke-CheckedProcess $copy ('/S _?=' + $Directory) ('retry-uninstall-after-' + $Kind + '-unlock') 300
    Assert-Uninstalled $Directory
    $script:uninstallFaultChecks.Add(@{
        case = ('locked-' + $Kind); install_path = $Directory; locked_path = $lockedPath
        failure_exit_code = 2; retry_exit_code = 0; retry_identity_preserved = $true
        scientific_and_appdata_sentinels_preserved = $true
    })
}

function Assert-InterruptedCleanupRecovery {
    param([string]$Directory)
    Assert-Installation $Directory
    if (-not $qaCreated -or -not (Test-InsidePath $Directory $qaRoot) -or $Directory -eq $qaRoot) { throw 'Recovery fault injection requires this private QA installation.' }
    $markerPath = Join-Path $Directory '.nd2wsi-install.ini'
    $originalMarker = [IO.File]::ReadAllBytes($markerPath)
    $uninstallerHash = Get-Sha256 (Join-Path $Directory 'Uninstall.exe')
    $copy = Get-QaUninstallerCopy $Directory 'interrupted-cleanup'
    $registrySnapshot = [Collections.Generic.List[object]]::new()
    $removedRegistration = $false
    $completed = $false
    $root = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Registry64)
    $key = $null
    try {
        # Remove only the leaf registration created by this QA installation,
        # after recording its exact value types for recovery if this test fails.
        $key = $root.OpenSubKey($registrySubkey)
        if ($null -eq $key -or $key.SubKeyCount -ne 0 -or -not [string]::Equals([string]$key.GetValue('InstallLocation', ''), $Directory, [StringComparison]::OrdinalIgnoreCase)) { throw 'Refusing to change registration that does not belong to the private QA installation.' }
        foreach ($name in $key.GetValueNames()) { $registrySnapshot.Add(@{ name = $name; value = $key.GetValue($name); kind = [int]$key.GetValueKind($name) }) }
        $key.Dispose()
        $key = $null
        [IO.File]::WriteAllBytes((Join-Path $qaRoot 'interrupted-cleanup-original-marker.ini'), $originalMarker)
        $registrySnapshot.ToArray() | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $resultPath 'interrupted-cleanup-original-registration.json') -Encoding UTF8
        $root.DeleteSubKey($registrySubkey, $true)
        $removedRegistration = $true
        $variants = @(
            @{ label = 'wrong-location'; product = $productId; state = 'in-progress'; location = (Join-Path $qaRoot 'wrong-recovery-location') },
            @{ label = 'wrong-product'; product = 'unrelated-product'; state = 'in-progress'; location = $Directory },
            @{ label = 'missing-progress'; product = $productId; state = ''; location = $Directory }
        )
        foreach ($variant in $variants) {
            $markerText = "[Installation]`r`nProductId=$($variant.product)`r`nVersion=$($script:version)`r`nUninstallState=$($variant.state)`r`nInstallLocation=$($variant.location)`r`n"
            [IO.File]::WriteAllText($markerPath, $markerText, [Text.UnicodeEncoding]::new($false, $true))
            $markerHash = Get-Sha256 $markerPath
            Invoke-CheckedProcess $copy ('/S _?=' + $Directory) ('reject-recovery-' + $variant.label) 120 -ExpectedExitCodes @(2)
            Assert-Payload $Directory
            if ((Get-Sha256 $markerPath) -cne $markerHash -or (Get-Sha256 (Join-Path $Directory 'Uninstall.exe')) -cne $uninstallerHash -or $null -ne (Get-InstalledRegistration)) { throw 'Rejected recovery marker changed installer ownership files or registration.' }
            Assert-Shortcut $appShortcut (Join-Path $Directory 'nd2wsi-viewer.exe')
            Assert-Shortcut $uninstallShortcut (Join-Path $Directory 'Uninstall.exe')
            Assert-Shortcut $desktopShortcut (Join-Path $Directory 'nd2wsi-viewer.exe')
            Assert-Sentinels
        }
        $markerText = "[Installation]`r`nProductId=$productId`r`nVersion=$($script:version)`r`nUninstallState=in-progress`r`nInstallLocation=$Directory`r`n"
        [IO.File]::WriteAllText($markerPath, $markerText, [Text.UnicodeEncoding]::new($false, $true))
        $markerHash = Get-Sha256 $markerPath
        # This exceptional identity permits completing uninstall only; Setup
        # must not adopt an unregistered nonempty directory for installation.
        Invoke-CheckedProcess $installerPath ('/S /D=' + $Directory) 'reject-install-from-uninstall-recovery' 120 -ExpectedExitCodes @(2)
        Assert-Payload $Directory
        if ((Get-Sha256 $markerPath) -cne $markerHash -or (Get-Sha256 (Join-Path $Directory 'Uninstall.exe')) -cne $uninstallerHash -or $null -ne (Get-InstalledRegistration)) { throw 'Setup modified an uninstall-only recovery installation.' }
        Assert-Sentinels
        Invoke-CheckedProcess $copy ('/S _?=' + $Directory) 'retry-uninstall-with-path-bound-marker' 300
        Assert-Uninstalled $Directory
        $completed = $true
        $script:uninstallFaultChecks.Add(@{
            case = 'interrupted-final-cleanup'; install_path = $Directory
            missing_registration_recovered = $true; wrong_location_rejected = $true
            wrong_product_rejected = $true; missing_progress_rejected = $true
            reinstall_from_uninstall_only_identity_rejected = $true; retry_exit_code = 0
            scientific_and_appdata_sentinels_preserved = $true
        })
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
        try {
            if ($removedRegistration -and -not $completed) {
                # A failed fault-injection assertion leaves the original QA
                # ownership records usable. Never overwrite another install.
                $current = Get-InstalledRegistration
                if ($null -ne $current -and -not [string]::Equals([string]$current['InstallLocation'], $Directory, [StringComparison]::OrdinalIgnoreCase)) { throw 'QA recovery found another installation registration; it was preserved.' }
                if ((Get-Sha256 (Join-Path $Directory 'Uninstall.exe')) -cne $uninstallerHash) { throw 'QA recovery cannot restore runnable uninstall identity; all remaining QA files were retained.' }
                [IO.File]::WriteAllBytes($markerPath, $originalMarker)
                $key = $root.CreateSubKey($registrySubkey)
                try {
                    foreach ($entry in $registrySnapshot) { $key.SetValue($entry.name, $entry.value, [Microsoft.Win32.RegistryValueKind]$entry.kind) }
                }
                finally { $key.Dispose() }
            }
        }
        finally { $root.Dispose() }
    }
}

function Remove-OwnedQaRoot {
    if (-not $qaCreated -or -not (Test-LiteralEntry $qaMarker)) { throw 'QA cleanup requires this invocation ownership marker.' }
    $marker = Get-Content -LiteralPath $qaMarker -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($marker.run_id -cne $runId -or $marker.product -cne 'nd2wsi-installer-qa' -or $marker.qa_root -cne $qaRoot -or -not (Test-InsidePath $qaRoot $qaBase) -or $qaRoot -eq $qaBase) { throw 'QA ownership marker does not match the cleanup boundary.' }
    # Preflight every entry without following directory reparse points. Cleanup
    # removes only these QA files and empty directories; it never recurses into
    # an unexpected junction or into the user's source or AppData directory.
    $directories = [Collections.Generic.List[string]]::new()
    $files = [Collections.Generic.List[string]]::new()
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($qaRoot)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        $rootItem = Get-Item -LiteralPath $directory -Force
        if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "QA cleanup found a directory reparse point; retained: $directory" }
        $directories.Add($directory)
        foreach ($entry in Get-ChildItem -LiteralPath $directory -Force) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "QA cleanup found a reparse point; retained: $($entry.FullName)" }
            if ($entry.PSIsContainer) { $pending.Push($entry.FullName) }
            else { $files.Add($entry.FullName) }
        }
    }
    foreach ($path in $files) {
        if ($path -ine $qaMarker) { Remove-Item -LiteralPath $path -Force }
    }
    foreach ($path in ($directories | Sort-Object -Property Length -Descending)) {
        if ($path -ine $qaRoot) { [IO.Directory]::Delete($path, $false) }
    }
    $ownedAppData = @($script:sentinels | Where-Object { $_.path -ceq $appDataSentinel })
    if ($ownedAppData.Count -eq 1 -and (Get-Sha256 $appDataSentinel) -ceq $ownedAppData[0].sha256) { Remove-Item -LiteralPath $appDataSentinel -Force }
    Remove-Item -LiteralPath $qaMarker -Force
    [IO.Directory]::Delete($qaRoot, $false)
}

try {
    if (Test-InsidePath $resultPath $qaBase) { throw 'ResultsDir must be outside the disposable QA directory.' }
    if (Test-InsidePath $resultPath $defaultInstall) { throw 'ResultsDir must be outside the app installation.' }
    New-Item -ItemType Directory -Force -Path $resultPath | Out-Null
    Start-Transcript -LiteralPath (Join-Path $resultPath 'installer-verification.log') -Force | Out-Null
    $transcriptStarted = $true
    Write-State 'running' 'Checking installer, source fixture, and existing-installation boundaries.'
    $installerPath = (Resolve-Path -LiteralPath $Installer).Path
    $manifestPath = (Resolve-Path -LiteralPath $PayloadManifest).Path
    $sourcePath = (Resolve-Path -LiteralPath $SmokeFile).Path
    $bootstrapperPath = (Resolve-Path -LiteralPath $WebView2Bootstrapper).Path
    if (-not $BuildManifest) { $BuildManifest = [IO.Path]::ChangeExtension($installerPath, '.build.json') }
    $buildManifestPath = (Resolve-Path -LiteralPath $BuildManifest).Path
    $target = Get-TargetReadiness
    $target | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $resultPath 'target-readiness.json') -Encoding UTF8
    if (-not $target.os_is_64_bit -or $target.build -lt 16299 -or $target.installation_type -ne 'Client') { throw 'This verification requires Windows 10 version 1709 or newer, or Windows 11, on a 64-bit desktop edition. Windows 10 Education is a desktop edition.' }
    if (-not $target.net_framework_462_or_newer) { throw '.NET Framework 4.6.2 or newer is required. No system component was installed or changed by this preflight.' }
    $installerHash = Get-Sha256 $installerPath
    $manifestHash = Get-Sha256 $manifestPath
    $bootstrapperHash = Get-Sha256 $bootstrapperPath
    $buildManifestHash = Get-Sha256 $buildManifestPath
    $buildManifestData = Get-Content -LiteralPath $buildManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($name in @('schema_version', 'installer_sha256', 'payload_manifest_sha256', 'webview2_bootstrapper_sha256', 'app_version', 'payload_file_count', 'app_executable_sha256')) {
        if ($null -eq $buildManifestData.PSObject.Properties[$name]) { throw "Installer build manifest omits $name." }
    }
    if ($buildManifestData.schema_version -ne 1) { throw 'Unsupported installer build manifest schema.' }
    if ($installerHash -cne $buildManifestData.installer_sha256 -or $manifestHash -cne $buildManifestData.payload_manifest_sha256 -or $bootstrapperHash -cne $buildManifestData.webview2_bootstrapper_sha256) { throw 'Setup, payload manifest, or WebView2 bootstrapper does not match the exact installer build manifest.' }
    if ($bootstrapperHash -cne $WebView2BootstrapperSha256) { throw 'The official WebView2 bootstrapper checksum does not match the installer build input.' }
    $bootstrapperSignature = Get-AuthenticodeSignature -LiteralPath $bootstrapperPath
    if ($bootstrapperSignature.Status -ne 'Valid' -or $null -eq $bootstrapperSignature.SignerCertificate -or $bootstrapperSignature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)') { throw 'The embedded WebView2 bootstrapper does not have a valid Microsoft Corporation Authenticode signature.' }
    if (-not (Test-LiteralEntry ($installerPath + '.sha256'))) { throw 'The required adjacent Setup.exe.sha256 checksum file is missing.' }
    $expectedInstaller = ((Get-Content -LiteralPath ($installerPath + '.sha256') -Raw).Trim() -split '\s+')[0]
    if ($expectedInstaller -cnotmatch '^[0-9a-f]{64}$' -or $installerHash -cne $expectedInstaller) { throw 'Setup.exe checksum does not match its adjacent checksum file.' }
    if ((Get-Sha256 $sourcePath) -cne $expectedFixtureHash) { throw 'SmokeFile is not the checksum-verified immutable real ND2 fixture.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -ne $manifest.PSObject.Properties['files']) {
        $script:payloadFiles = $manifest.files
        if ($null -ne $manifest.PSObject.Properties['version']) { $script:version = [string]$manifest.version }
    }
    else { $script:payloadFiles = $manifest }
    if ($script:version -cnotmatch '^\d+\.\d+\.\d+$') { throw 'Payload manifest version must contain three numeric components.' }
    if ($null -eq $script:payloadFiles.PSObject.Properties['nd2wsi-viewer.exe']) { throw 'Payload manifest omits the application executable.' }
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $script:payloadFiles.PSObject.Properties) {
        Get-PayloadPath $installPath $entry.Name | Out-Null
        if (-not $names.Add($entry.Name) -or [string]$entry.Value -cnotmatch '^[0-9a-f]{64}$') { throw "Invalid or duplicate payload manifest entry: $($entry.Name)" }
    }
    if ($script:version -cne [string]$buildManifestData.app_version -or $names.Count -ne [int]$buildManifestData.payload_file_count -or [string]$script:payloadFiles.PSObject.Properties['nd2wsi-viewer.exe'].Value -cne [string]$buildManifestData.app_executable_sha256) { throw 'Payload version, file count, or application checksum does not match the installer build manifest.' }
    if ((Test-LiteralEntry $defaultInstall) -or $null -ne (Get-InstalledRegistration)) { throw 'An existing installation or uninstall registration is present. It was preserved; this isolated verification will not replace an unknown installation.' }
    foreach ($path in @($appShortcut, $uninstallShortcut, $desktopShortcut)) {
        if (Test-LiteralEntry $path) { throw "An existing app shortcut was preserved: $path" }
    }
    if (Get-Process -Name 'nd2wsi-viewer' -ErrorAction SilentlyContinue) { throw 'Close the existing viewer before installer verification; no process was stopped.' }
    if (Test-LiteralEntry $qaRoot) { throw 'The new QA run directory already exists.' }
    $webviewBefore = Get-WebView2Version
    New-Item -ItemType Directory -Path $qaRoot | Out-Null
    $qaCreated = $true
    @{
        product = 'nd2wsi-installer-qa'; run_id = $runId; qa_root = $qaRoot
        install_path = $installPath; installer_sha256 = $installerHash
        payload_manifest_sha256 = $manifestHash; app_data_sentinel = $appDataSentinel
    } | ConvertTo-Json | Set-Content -LiteralPath $qaMarker -Encoding UTF8
    New-Item -ItemType Directory -Path $runtimeTemp | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination $fixturePath

    # NSIS /D= is the final argument and intentionally unquoted. NSIS treats
    # every remaining character as the destination, including spaces/Unicode.
    Invoke-CheckedProcess $installerPath ('/S /D=' + $installPath) 'install-isolated' 900
    Assert-Installation $installPath
    Add-Sentinel (Join-Path $installPath 'scientific-data\keep.nd2')
    Add-Sentinel (Join-Path $installPath 'scientific-data\nd2wsi\annotations\keep.json')
    Add-Sentinel (Join-Path $installPath 'nd2wsi\caches\keep.txt')
    Add-Sentinel $appDataSentinel
    $installedNames = @(Get-InstalledFileNames $installPath)
    Invoke-AppSmoke $installPath 'installed'
    Assert-Payload $installPath
    Assert-NoUnexpectedInstalledFiles $installPath $installedNames
    Assert-Sentinels

    Invoke-CheckedProcess $installerPath ('/S /D=' + $installPath) 'reinstall-same-version' 900
    Assert-Installation $installPath
    Assert-NoUnexpectedInstalledFiles $installPath $installedNames
    Assert-Sentinels
    Assert-JunctionRejection
    Assert-NoUnexpectedInstalledFiles $installPath $installedNames

    Assert-LockedUninstallRecovery $installPath 'payload'
    foreach ($kind in @('desktop-shortcut', 'marker', 'uninstaller')) {
        # A completed uninstall deliberately keeps scientific files. Use a
        # fresh sibling QA path for the next case; never delete those files
        # merely to make an old directory acceptable to Setup again.
        $faultInstall = Join-Path $qaRoot ($koreanPath + ' ' + $script:uninstallFaultChecks.Count)
        Install-FaultFixture $faultInstall $kind
        Assert-LockedUninstallRecovery $faultInstall $kind
    }
    $recoveryInstall = Join-Path $qaRoot ($koreanPath + ' 4')
    Install-FaultFixture $recoveryInstall 'interrupted-cleanup'
    Assert-InterruptedCleanupRecovery $recoveryInstall
    $webviewAfter = Get-WebView2Version
    $uninstallVerified = $true
    Write-State 'running' 'Installed smoke, GUI, reinstall, locked-file uninstall retries, and data preservation checks passed.'

    $finalInstall = $null
    if ($KeepInstalled) {
        if (Test-LiteralEntry $defaultInstall) { throw 'The default install directory appeared during QA; it was preserved.' }
        Invoke-CheckedProcess $installerPath '/S' 'install-final-default' 900
        Assert-Installation $defaultInstall
        $defaultNames = @(Get-InstalledFileNames $defaultInstall)
        Invoke-AppSmoke $defaultInstall 'final-installed' -GuiOnly
        Assert-Payload $defaultInstall
        Assert-NoUnexpectedInstalledFiles $defaultInstall $defaultNames
        Assert-Sentinels
        $finalInstall = $defaultInstall
    }
    $script:phase = 'cleanup-owned-qa'
    Remove-OwnedQaRoot
    $report = @{
        state = 'complete'; version = $script:version; run_id = $runId
        installer_sha256 = $installerHash; payload_manifest_sha256 = $manifestHash
        build_manifest_sha256 = $buildManifestHash; exact_build_inputs_verified = $true
        webview2_bootstrapper_sha256 = $bootstrapperHash
        webview2_bootstrapper_authenticode = [string]$bootstrapperSignature.Status
        webview2_bootstrapper_signer = $bootstrapperSignature.SignerCertificate.Subject
        source_sha256 = $expectedFixtureHash; payload_file_count = $names.Count
        unicode_install_and_source_paths = $true; scientific_smoke = $true
        gui_smoke = $true; same_version_reinstall = $true; uninstall_verified = $uninstallVerified
        junction_reinstall_and_uninstall_rejected_without_data_loss = $true
        uninstall_fault_checks = $script:uninstallFaultChecks.ToArray()
        scientific_and_appdata_sentinels_preserved = $true; qa_directory_removed = -not (Test-LiteralEntry $qaRoot)
        no_runtime_files_written_inside_installation = $true
        webview2_before = $webviewBefore; webview2_after_uninstall = $webviewAfter
        target_readiness = $target
        scientific_smoke_scope = 'ND2 read/tile/pixel-exact calibrated ROI export; JPEG and lossless JPEG2000 SVS pyramid/tile/pixel-exact TIFF ROI export'
        optional_codec_dependencies = 'Some additional imagecodecs codecs require separately installed Visual C++ runtime or proprietary DLLs. Those optional formats are outside this verification; no Visual C++ runtime is installed by this check.'
        kept_installed = [bool]$KeepInstalled; final_install_location = $finalInstall
        windows = [Environment]::OSVersion.VersionString
        host_architecture = [Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITEW6432', 'Process')
        process_architecture = $env:PROCESSOR_ARCHITECTURE
        steps = $script:steps.ToArray(); completed = (Get-Date).ToUniversalTime().ToString('o')
    }
    if (-not $report.host_architecture) { $report.host_architecture = $env:PROCESSOR_ARCHITECTURE }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $resultPath 'installer-verification.json') -Encoding UTF8
    $script:phase = 'complete'
    Write-State 'complete' 'Windows installer verification passed.'
    $success = $true
}
catch {
    $message = $_.Exception.Message
    if (Test-Path -LiteralPath $resultPath -PathType Container) {
        Write-State 'failed' $message
        @{
            state = 'failed'; phase = $script:phase; error = $message
            run_id = $runId; qa_root = $qaRoot; qa_retained = (Test-LiteralEntry $qaRoot)
            uninstall_verified = $uninstallVerified; steps = $script:steps.ToArray()
            uninstall_fault_checks = $script:uninstallFaultChecks.ToArray()
            ended = (Get-Date).ToUniversalTime().ToString('o')
        } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $resultPath 'installer-verification.json') -Encoding UTF8
    }
    Write-Host ($_ | Out-String)
}
finally {
    [Environment]::SetEnvironmentVariable('ND2WSI_LOG_FILE', $previousLog, 'Process')
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
if (-not $success) { exit 1 }

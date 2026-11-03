#requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$SmokeFile,
    [string]$Evidence = 'dist/windows/verification'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$sourcePath = (Resolve-Path -LiteralPath $SmokeFile).Path
$evidencePath = [System.IO.Path]::GetFullPath($Evidence)
New-Item -ItemType Directory -Force -Path $evidencePath | Out-Null

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-Packaged {
    param([string]$Executable, [string[]]$Arguments, [int]$Timeout, [string]$Mode)
    # Use Windows CRT argument quoting with .NET Framework on PS5.1.
    # No shell parses this string; no Python is used to run the application.
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Executable
    $start.UseShellExecute = $false
    $start.Arguments = (($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $process = [System.Diagnostics.Process]::Start($start)
    if (-not $process.WaitForExit($Timeout)) {
        $process.Kill()
        $process.WaitForExit(10000) | Out-Null
        throw "Packaged --$Mode timed out."
    }
    $timer.Stop()
    $result = @{ mode = $Mode; exit_code = $process.ExitCode; seconds = $timer.Elapsed.TotalSeconds }
    $process.Dispose()
    return $result
}

# Explorer copies a downloaded ZIP's Mark of the Web onto every extracted file;
# Expand-Archive does not. Mark the files the way users receive them.
function Set-DownloadMark {
    param([string]$Root)
    Get-ChildItem -LiteralPath $Root -Recurse -File -Force | ForEach-Object {
        Set-Content -LiteralPath $_.FullName -Stream 'Zone.Identifier' -Value "[ZoneTransfer]`r`nZoneId=3" -Encoding ascii
    }
}

function Test-DownloadMark {
    param([string]$Path)
    return [bool](Get-Item -LiteralPath $Path -Stream 'Zone.Identifier' -ErrorAction SilentlyContinue)
}

# The folders nd2wsi/windows_bundle.py clears; build_windows.py keeps every
# .NET assembly inside them.
function Get-AssemblyFile {
    param([string]$AppRoot)
    foreach ($folder in @('pythonnet\runtime', 'clr_loader\ffi\dlls', 'webview\lib')) {
        Get-ChildItem -LiteralPath (Join-Path $AppRoot "_internal\$folder") -Recurse -File -Filter '*.dll'
    }
}

$expected = ((Get-Content -LiteralPath "$archivePath.sha256" -Raw).Trim() -split '\s+')[0]
$actual = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expected -ne $actual) { throw 'The distributable ZIP checksum does not match.' }
$sourceHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($sourceHash -ne '51d603317947b04c3b43c8e694f3ba97a16478ce30e795857ec6ae0321d41c33') {
    throw 'The real ND2 fixture checksum does not match the immutable testdata-v1 acquisition.'
}

# Exercise the exact shipped bytes, with non-ASCII and spaces in both paths.
$koreanVerification = [string][char]0xAC80 + [char]0xC99D
$koreanCell = [string][char]0xC138 + [char]0xD3EC
$testRoot = Join-Path $env:TEMP ("nd2wsi $koreanVerification " + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
Expand-Archive -LiteralPath $archivePath -DestinationPath $testRoot
$appRoot = Join-Path $testRoot 'nd2wsi-viewer'
$exe = Join-Path $appRoot 'nd2wsi-viewer.exe'
$about = Join-Path $appRoot '_internal\nd2wsi-viewer-about'
$slide = Join-Path $testRoot ($koreanCell + ' sample.nd2')
Copy-Item -LiteralPath $sourcePath -Destination $slide

# Users see only the executable, the runtime folder and the READ-ME.
$topLevel = @(Get-ChildItem -LiteralPath $appRoot -Force | ForEach-Object Name | Sort-Object)
if (($topLevel -join '|') -cne '_internal|nd2wsi-viewer.exe|READ-ME.txt') {
    throw "Unexpected top-level layout: $($topLevel -join ', ')"
}
$pruned = @(Get-ChildItem -LiteralPath (Join-Path $appRoot '_internal') -Recurse -Directory -Force |
    Where-Object { $_.Name -in @('tests', '__pycache__') })
if ($pruned.Count) { throw "Test or cache folders were shipped: $($pruned[0].FullName)" }
$fileCount = @(Get-ChildItem -LiteralPath $appRoot -Recurse -File -Force).Count
if ($fileCount -gt 3200) { throw "The portable folder holds $fileCount files, more than 3200." }

$manifest = Get-Content -LiteralPath (Join-Path $about 'build-info.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.binary_architecture -ne 'x64') { throw 'Unexpected executable architecture in the build manifest.' }
if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.executable_sha256) {
    throw 'The executable checksum does not match the build manifest.'
}
$codecManifest = Join-Path $about 'sdpc-codec-manifest.json'
if ($manifest.sdpc_codec_bundled -ne $true -or $manifest.distribution_status -ne 'source-built-lgpl-codec') {
    throw 'This release requires the verified source-built LGPL SDPC codec.'
}
if ((Get-FileHash -LiteralPath $codecManifest -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.sdpc_codec_manifest_sha256) {
    throw 'The packaged SDPC codec provenance does not match the build manifest.'
}

$runs = @()
$previousLog = $env:ND2WSI_LOG_FILE
$runtimeAssembly = '_internal\pythonnet\runtime\Python.Runtime.dll'
$controlRoot = Join-Path $testRoot 'blocked'
try {
    foreach ($mode in @('smoke', 'gui-smoke')) {
        # Each launch clears the assembly marks, so mark the folder before each one.
        Set-DownloadMark $appRoot
        if (-not (Test-DownloadMark (Join-Path $appRoot $runtimeAssembly))) { throw 'Could not add the download mark.' }
        $log = Join-Path $evidencePath "$mode.log"
        Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
        $env:ND2WSI_LOG_FILE = $log
        $arguments = @("--$mode", $slide)
        if ($mode -eq 'gui-smoke') {
            $guiReport = Join-Path $evidencePath 'gui-smoke.json'
            Remove-Item -LiteralPath $guiReport -Force -ErrorAction SilentlyContinue
            $arguments += @('--smoke-report', $guiReport)
        }
        $timeout = if ($mode -eq 'smoke') { 600000 } else { 180000 }
        $run = Invoke-Packaged -Executable $exe -Arguments $arguments -Timeout $timeout -Mode $mode
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Encoding UTF8 }
        if ($run.exit_code -ne 0) { throw "Packaged --$mode failed with exit code $($run.exit_code)." }
        $stillMarked = @(Get-AssemblyFile $appRoot | Where-Object { Test-DownloadMark $_.FullName })
        if ($stillMarked.Count) { throw "The app left a download mark on $($stillMarked[0].FullName)." }
        if (-not (Test-DownloadMark $exe)) { throw 'The app removed the executable''s own download mark.' }
        if ($mode -eq 'gui-smoke') {
            if (-not (Test-Path -LiteralPath $guiReport) -or (Get-Content -LiteralPath $guiReport -Raw -Encoding UTF8 | ConvertFrom-Json).ok -ne $true) {
                throw 'Packaged GUI smoke did not record successful completion.'
            }
        }
        elseif (-not (Test-Path -LiteralPath $log) -or (Get-Content -LiteralPath $log -Raw -Encoding UTF8) -notmatch 'smoke ok: SDPC') {
            throw "Packaged --$mode did not record successful SDPC decoder and ROI checks."
        }
        elseif ((Get-Content -LiteralPath $log -Raw -Encoding UTF8) -notmatch 'smoke ok: Astromap JPEG, TXT and PDF') {
            throw "Packaged --$mode did not record successful Astromap export checks."
        }
        $runs += $run
    }

    # Negative control: when the marks cannot be removed, the users' startup
    # failure must appear with its hint. This proves the marked runs above
    # exercised it. If the deny does not hold, the app starts and this fails.
    Expand-Archive -LiteralPath $archivePath -DestinationPath $controlRoot
    $controlApp = Join-Path $controlRoot 'nd2wsi-viewer'
    Set-DownloadMark $controlApp
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $assemblies = @(Get-AssemblyFile $controlApp)
    foreach ($assembly in $assemblies) {
        & attrib.exe +R $assembly.FullName
        & icacls.exe $assembly.FullName /deny "${user}:(D)" /Q | Out-Null
        if ($LASTEXITCODE) { throw "Could not protect $($assembly.FullName)." }
    }
    foreach ($folder in @($assemblies | ForEach-Object DirectoryName | Sort-Object -Unique)) {
        & icacls.exe $folder /deny "${user}:(DC)" /Q | Out-Null
        if ($LASTEXITCODE) { throw "Could not protect $folder." }
    }
    $blockedLog = Join-Path $evidencePath 'gui-smoke-blocked.log'
    $blockedReport = Join-Path $evidencePath 'gui-smoke-blocked.json'
    Remove-Item -LiteralPath $blockedLog, $blockedReport -Force -ErrorAction SilentlyContinue
    $env:ND2WSI_LOG_FILE = $blockedLog
    $blocked = Invoke-Packaged -Executable (Join-Path $controlApp 'nd2wsi-viewer.exe') -Timeout 180000 -Mode 'gui-smoke' `
        -Arguments @('--gui-smoke', $slide, '--smoke-report', $blockedReport)
    if (Test-Path -LiteralPath $blockedLog) { Get-Content -LiteralPath $blockedLog -Encoding UTF8 }
    if ($blocked.exit_code -eq 0) { throw 'The app started although its assemblies kept the download mark.' }
    $blockedResult = Get-Content -LiteralPath $blockedReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($blockedResult.ok -ne $false -or $blockedResult.error -notmatch 'Python\.Runtime' -or $blockedResult.error -notmatch 'Unblock') {
        throw "The blocked launch did not report the startup failure with its hint: $($blockedResult.error)"
    }
    if (-not (Test-DownloadMark (Join-Path $controlApp $runtimeAssembly))) { throw 'The blocked control lost its download mark.' }
    $hostArchitecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $report = @{
        version = $manifest.version
        commit = $manifest.commit
        archive_sha256 = $actual
        executable_sha256 = $manifest.executable_sha256
        binary_architecture = 'x64'
        host_architecture = $hostArchitecture
        windows = [Environment]::OSVersion.VersionString
        source_snapshot_sha256 = $manifest.source.snapshot_sha256
        source_working_tree_dirty = $manifest.source.working_tree_dirty
        unicode_install_and_source_paths = $true
        sdpc_packaged_decoder_and_roi_verified = $true
        astromap_jpeg_txt_pdf_verified = $true
        download_marked_launches_verified = $true
        blocked_assembly_failure_reproduced = $true
        portable_file_count = $fileCount
        sdpc_codec_manifest_sha256 = $manifest.sdpc_codec_manifest_sha256
        sdpc_codec_source_url = $manifest.sdpc_codec_source.source_url
        sdpc_codec_source_archive_sha256 = $manifest.sdpc_codec_source.source_archive_sha256
        runs = $runs
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $evidencePath 'verification.json') -Encoding utf8
    Write-Host 'Windows packaged verification passed.'
}
finally {
    $env:ND2WSI_LOG_FILE = $previousLog
    if (Test-Path -LiteralPath $controlRoot) {
        & icacls.exe $controlRoot /reset /T /C /Q | Out-Null
        & attrib.exe -R "$controlRoot\*" /S /D
    }
    # Only this invocation's private fixture/extraction directory is disposable.
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}

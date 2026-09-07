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
$exe = Join-Path $testRoot 'nd2wsi-viewer\nd2wsi-viewer.exe'
$slide = Join-Path $testRoot ($koreanCell + ' sample.nd2')
Copy-Item -LiteralPath $sourcePath -Destination $slide
$manifest = Get-Content -LiteralPath (Join-Path $testRoot 'nd2wsi-viewer\build-info.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.binary_architecture -ne 'x64') { throw 'Unexpected executable architecture in the build manifest.' }
if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.executable_sha256) {
    throw 'The executable checksum does not match the build manifest.'
}

$runs = @()
$previousLog = $env:ND2WSI_LOG_FILE
try {
    foreach ($mode in @('smoke', 'gui-smoke')) {
        $log = Join-Path $evidencePath "$mode.log"
        Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
        $env:ND2WSI_LOG_FILE = $log
        $arguments = @("--$mode", $slide)
        if ($mode -eq 'gui-smoke') {
            $guiReport = Join-Path $evidencePath 'gui-smoke.json'
            Remove-Item -LiteralPath $guiReport -Force -ErrorAction SilentlyContinue
            $arguments += @('--smoke-report', $guiReport)
        }
        # Use Windows CRT argument quoting with .NET Framework on PS5.1.
        # No shell parses this string; no Python is used to run the application.
        $start = [System.Diagnostics.ProcessStartInfo]::new()
        $start.FileName = $exe
        $start.UseShellExecute = $false
        $start.Arguments = (($arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $process = [System.Diagnostics.Process]::Start($start)
        $timeout = if ($mode -eq 'smoke') { 600000 } else { 180000 }
        if (-not $process.WaitForExit($timeout)) {
            $process.Kill()
            $process.WaitForExit(10000) | Out-Null
            throw "Packaged --$mode timed out."
        }
        $timer.Stop()
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Encoding UTF8 }
        if ($process.ExitCode -ne 0) { throw "Packaged --$mode failed with exit code $($process.ExitCode)." }
        if ($mode -eq 'gui-smoke') {
            if (-not (Test-Path -LiteralPath $guiReport) -or (Get-Content -LiteralPath $guiReport -Raw -Encoding UTF8 | ConvertFrom-Json).ok -ne $true) {
                throw 'Packaged GUI smoke did not record successful completion.'
            }
        }
        elseif (-not (Test-Path -LiteralPath $log) -or (Get-Content -LiteralPath $log -Raw -Encoding UTF8) -notmatch 'smoke ok') {
            throw "Packaged --$mode did not record successful completion."
        }
        $runs += @{ mode = $mode; exit_code = $process.ExitCode; seconds = $timer.Elapsed.TotalSeconds }
        $process.Dispose()
    }
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
        runs = $runs
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $evidencePath 'verification.json') -Encoding utf8
    Write-Host 'Windows packaged verification passed.'
}
finally {
    $env:ND2WSI_LOG_FILE = $previousLog
    # Only this invocation's private fixture/extraction directory is disposable.
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}

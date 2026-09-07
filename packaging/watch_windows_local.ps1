#requires -Version 5.1
<#
Run the defined local Windows build. After a failure, wait for a changed,
checksum-matching source ZIP and retry that same build. Stop on success or
after at most 120 minutes spent waiting for replacements. No background job,
service, listener, remote command channel or persistent setting is created.
#>
param(
    [Parameter(Mandatory = $true)][string]$SourceZip,
    [string]$ResultsDir,
    [ValidateRange(1, 120)][int]$MaxWaitMinutes = 120
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$sourceArchive = [IO.Path]::GetFullPath($SourceZip)
if (-not $ResultsDir) {
    $ResultsDir = Join-Path (Split-Path -Parent $sourceArchive) ([IO.Path]::GetFileNameWithoutExtension($sourceArchive) + '-results')
}
$resultPath = [IO.Path]::GetFullPath($ResultsDir)
New-Item -ItemType Directory -Force -Path $resultPath | Out-Null
$builder = Join-Path $PSScriptRoot 'build_windows_local.ps1'
if (-not (Test-Path -LiteralPath $builder -PathType Leaf)) { throw 'The sibling local Windows build script is missing.' }
# Always call this one fixed script through Windows PowerShell. User/profile
# commands, services and arbitrary retry commands are not accepted.
$engine = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$watchStatus = Join-Path $resultPath 'watch-status.json'
$watchLog = Join-Path $resultPath 'watch.log'
$attemptedHash = $null
$waitedSeconds = 0
$maximumWaitSeconds = $MaxWaitMinutes * 60
$attempt = 0
$waitTimer = [Diagnostics.Stopwatch]::StartNew()

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Write-WatchState {
    param([string]$State, [string]$Message)
    $entry = @{
        state = $State; message = $Message; attempt = $attempt
        source_zip = $sourceArchive; last_attempted_sha256 = $attemptedHash
        waited_seconds = $waitedSeconds; maximum_wait_seconds = $maximumWaitSeconds
        updated = (Get-Date).ToUniversalTime().ToString('o')
    }
    $entry | ConvertTo-Json | Set-Content -LiteralPath $watchStatus -Encoding UTF8
    ((Get-Date).ToString('s') + ' ' + $Message) | Add-Content -LiteralPath $watchLog -Encoding UTF8
    Write-Host $Message
}

try {
    Write-WatchState 'ready' 'Starting the local build; failed attempts will wait for a changed source ZIP.'
    while ($true) {
        $waitedSeconds = [int][Math]::Floor($waitTimer.Elapsed.TotalSeconds)
        if ($waitedSeconds -ge $maximumWaitSeconds) {
            Write-WatchState 'expired' 'The bounded wait for a replacement source ZIP ended. No background work remains.'
            exit 2
        }
        $candidate = $null
        try {
            $checksumFile = $sourceArchive + '.sha256'
            if ((Test-Path -LiteralPath $sourceArchive -PathType Leaf) -and (Test-Path -LiteralPath $checksumFile -PathType Leaf)) {
                $value = ((Get-Content -LiteralPath $checksumFile -Raw).Trim() -split '\s+')[0]
                if ($value -cmatch '^[0-9a-f]{64}$' -and $value -ne $attemptedHash) {
                    if ((Get-FileHash -LiteralPath $sourceArchive -Algorithm SHA256).Hash.ToLowerInvariant() -eq $value) {
                        $candidate = $value
                    }
                }
            }
        }
        catch {
            # A writer may be atomically replacing the ZIP/checksum pair.
            # A partially written or mismatched snapshot is never executed.
        }
        if ($candidate) {
            $attemptedHash = $candidate
            $attempt += 1
            Write-WatchState 'building' "Building local source snapshot $candidate (attempt $attempt)."
            # This child cannot terminate the watcher via the builder's exit 1.
            # Its native tool logs are written by Invoke-Checked in the builder.
            $start = [Diagnostics.ProcessStartInfo]::new()
            $start.FileName = $engine
            $start.UseShellExecute = $false
            $buildArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $builder, '-SourceZip', $sourceArchive, '-ResultsDir', $resultPath)
            $start.Arguments = (($buildArguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
            $waitTimer.Stop()
            $process = [Diagnostics.Process]::Start($start)
            try {
                $process.WaitForExit()
                $buildExit = $process.ExitCode
            }
            finally {
                $process.Dispose()
                $waitTimer.Start()
            }
            $buildStatusPath = Join-Path $resultPath 'build-status.json'
            $built = $null
            if (Test-Path -LiteralPath $buildStatusPath) {
                try { $built = Get-Content -LiteralPath $buildStatusPath -Raw -Encoding UTF8 | ConvertFrom-Json }
                catch { $built = $null }
            }
            if ($buildExit -eq 0 -and $built -and $built.state -eq 'complete' -and $built.source_zip_sha256 -eq $candidate) {
                Write-WatchState 'complete' 'The local Windows build passed. No more retries will run.'
                exit 0
            }
            Write-WatchState 'waiting' "Build failed (exit $buildExit). Waiting for a different checksum-matching source ZIP."
        }
        $waitedSeconds = [int][Math]::Floor($waitTimer.Elapsed.TotalSeconds)
        $interval = [Math]::Max(0, [Math]::Min(5, $maximumWaitSeconds - $waitedSeconds))
        Start-Sleep -Seconds $interval
    }
}
catch {
    Write-WatchState 'failed' ('Local build watcher stopped: ' + $_.Exception.Message)
    exit 1
}

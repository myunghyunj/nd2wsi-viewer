#requires -Version 5.1
<#
Build the local source snapshot entirely inside a Windows VM, without uploading
source or artifacts. Run in Windows PowerShell 5.1 or later:
  powershell -NoProfile -ExecutionPolicy Bypass -File build_windows_local.ps1 -SourceZip path\source.zip

The ZIP must have pyproject.toml and SOURCE-MANIFEST.json at its root. A sibling
source.zip.sha256 is required. Results and logs are copied back next to the ZIP.
The build stays under LOCALAPPDATA, on the guest's local filesystem. This script
does not change the user's PATH, execution policy, Python registry or settings.
#>
param(
    [Parameter(Mandatory = $true)][string]$SourceZip,
    [string]$ResultsDir
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$sourceArchive = (Resolve-Path -LiteralPath $SourceZip).Path
if (-not $ResultsDir) {
    $ResultsDir = Join-Path (Split-Path -Parent $sourceArchive) ([IO.Path]::GetFileNameWithoutExtension($sourceArchive) + '-results')
}
$resultPath = [IO.Path]::GetFullPath($ResultsDir)
New-Item -ItemType Directory -Force -Path $resultPath | Out-Null
$buildRoot = Join-Path $env:LOCALAPPDATA 'nd2wsi-windows-build'
$runId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$stage = Join-Path $buildRoot $runId
$repo = Join-Path $stage 'source'
$output = Join-Path $stage 'output'
$toolsRoot = Join-Path $buildRoot 'tools'
New-Item -ItemType Directory -Force -Path $stage, $repo, $output, $toolsRoot | Out-Null
$log = Join-Path $resultPath 'local-build.log'
$status = Join-Path $resultPath 'build-status.json'
$stepLogs = Join-Path $resultPath (Join-Path 'steps' $runId)
New-Item -ItemType Directory -Force -Path $stepLogs | Out-Null
$script:stepNumber = 0
$environmentBefore = @{}
foreach ($name in @('UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR', 'UV_PYTHON_BIN_DIR', 'UV_LINK_MODE', 'PYTHONUTF8', 'PYTHONIOENCODING', 'PYTHONUNBUFFERED', 'PATH')) {
    $environmentBefore[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$tlsBefore = [Net.ServicePointManager]::SecurityProtocol
$previousLocation = Get-Location
$transcriptStarted = $false
$success = $false

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    # Windows CRT quoting: double backslashes before quotes and before the
    # closing quote. No command shell interprets the resulting argument string.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    $script:stepNumber += 1
    $prefix = '{0:D2}-{1}' -f $script:stepNumber, [IO.Path]::GetFileNameWithoutExtension($Program)
    $stdoutLog = Join-Path $stepLogs ($prefix + '.stdout.log')
    $stderrLog = Join-Path $stepLogs ($prefix + '.stderr.log')
    Write-Host ('=> ' + $Program + ' ' + ($Arguments -join ' '))
    Write-Host "   stdout: $stdoutLog"
    Write-Host "   stderr: $stderrLog"
    @{ state = 'running'; stage = $stage; step = ($Program + ' ' + ($Arguments -join ' ')); stdout_log = $stdoutLog; stderr_log = $stderrLog; updated = (Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $status -Encoding UTF8
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Program
    $start.Arguments = (($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $start.WorkingDirectory = (Get-Location).Path
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    $stdoutFile = $null
    $stderrFile = $null
    try {
        # Drain both pipes asynchronously as raw bytes. This cannot deadlock
        # when one stream fills, and avoids PS5.1's NativeCommandError conversion
        # for harmless stderr messages. FileShare permits live log inspection.
        $stdoutFile = [IO.FileStream]::new($stdoutLog, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite, 1, [IO.FileOptions]::Asynchronous)
        $stderrFile = [IO.FileStream]::new($stderrLog, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite, 1, [IO.FileOptions]::Asynchronous)
        if (-not $process.Start()) { throw "Could not start: $Program" }
        $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($stdoutFile)
        $stderrTask = $process.StandardError.BaseStream.CopyToAsync($stderrFile)
        while (-not $process.WaitForExit(500)) { }
        if (-not [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($stdoutTask, $stderrTask), 10000)) {
            throw 'Native process exited but inherited output pipes did not close within 10 seconds.'
        }
        $exitCode = $process.ExitCode
    }
    finally {
        if ($stdoutFile) { $stdoutFile.Dispose() }
        if ($stderrFile) { $stderrFile.Dispose() }
        $process.Dispose()
    }
    # Keep the transcript concise; complete streams remain in their own files.
    foreach ($nativeLog in @($stdoutLog, $stderrLog)) {
        Get-Content -LiteralPath $nativeLog -Encoding UTF8 -Tail 40 | ForEach-Object { Write-Host $_ }
    }
    if ($exitCode -ne 0) { throw "Command failed with exit $exitCode`: $Program (see $stdoutLog and $stderrLog)" }
}

function Get-VerifiedDownload {
    param([string]$Url, [string]$Destination, [string]$Sha256)
    if (Test-Path -LiteralPath $Destination) {
        if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Sha256) { return }
    }
    $partial = $Destination + '.partial'
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $partial
    if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Sha256) {
        throw "Downloaded tool checksum mismatch: $Url"
    }
    Move-Item -LiteralPath $partial -Destination $Destination -Force
}

try {
    Start-Transcript -LiteralPath $log -Force | Out-Null
    $transcriptStarted = $true
    @{ state = 'running'; stage = $stage; started = (Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $status -Encoding UTF8
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $expectedZip = ((Get-Content -LiteralPath ($sourceArchive + '.sha256') -Raw).Trim() -split '\s+')[0]
    $sourceHash = (Get-FileHash -LiteralPath $sourceArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expectedZip -cnotmatch '^[0-9a-f]{64}$' -or $sourceHash -ne $expectedZip) {
        throw 'Local source ZIP checksum mismatch.'
    }
    Expand-Archive -LiteralPath $sourceArchive -DestinationPath $repo
    $manifestPath = Join-Path $repo 'SOURCE-MANIFEST.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.base_commit -cnotmatch '^[0-9a-f]{40}$') {
        throw 'Invalid local source manifest.'
    }
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $relative = $entry.Name
        if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|/)\.\.(/|$)|[:\\]') {
            throw "Invalid source path: $relative"
        }
        $file = Join-Path $repo $relative
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) {
            throw "Source file checksum mismatch: $relative"
        }
    }
    Write-Host "Verified local snapshot from base $($manifest.base_commit); dirty=$($manifest.working_tree_dirty)"

    # Exact upstream release assets and hashes, verified when this script was written.
    $uvZip = Join-Path $toolsRoot 'uv-0.12.10-windows-x64.zip'
    Get-VerifiedDownload -Url 'https://github.com/astral-sh/uv/releases/download/0.12.10/uv-x86_64-pc-windows-msvc.zip' -Destination $uvZip -Sha256 'f65744f94072152b1f86ba2aace4d01f1124d9a8ecb235805039e3718c36cac2'
    $uvDir = Join-Path $toolsRoot 'uv-0.12.10'
    Expand-Archive -LiteralPath $uvZip -DestinationPath $uvDir -Force
    $uvCandidates = @(Get-ChildItem -LiteralPath $uvDir -Filter uv.exe -Recurse)
    if ($uvCandidates.Count -ne 1) { throw 'Expected one uv executable in the official archive.' }
    $uv = $uvCandidates[0].FullName

    $nodeZip = Join-Path $toolsRoot 'node-v22.23.2-win-x64.zip'
    Get-VerifiedDownload -Url 'https://nodejs.org/dist/v22.23.2/node-v22.23.2-win-x64.zip' -Destination $nodeZip -Sha256 '1177b4137ba5adaa56354ae40f1080c7450e8ae09cecb47da459d1c52ac99f97'
    Expand-Archive -LiteralPath $nodeZip -DestinationPath $toolsRoot -Force
    $nodeDir = Join-Path $toolsRoot 'node-v22.23.2-win-x64'
    $env:PATH = $nodeDir + ';' + $env:PATH
    $env:UV_CACHE_DIR = Join-Path $buildRoot 'uv-cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $buildRoot 'python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $buildRoot 'python-bin'
    $env:UV_LINK_MODE = 'copy'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    Set-Location -LiteralPath $repo
    $pythonRequest = 'cpython-3.13.14-windows-x86_64-none'
    Invoke-Checked $uv @('python', 'install', $pythonRequest, '--no-bin', '--no-registry', '--no-config')
    Invoke-Checked $uv @('sync', '--frozen', '--all-extras', '--python', $pythonRequest)
    $python = Join-Path $repo '.venv\Scripts\python.exe'
    Invoke-Checked $python @('-c', 'import _tkinter, platform, struct; assert struct.calcsize(chr(80)) == 8; print(platform.python_version(), platform.python_compiler(), _tkinter.TK_VERSION)')
    Invoke-Checked $uv @('pip', 'install', '--python', $python, '--require-hashes', '--no-config', '--default-index', 'https://pypi.org/simple', '-r', 'packaging/windows-build-requirements.txt')
    Invoke-Checked $python @('-m', 'pytest', 'tests', '-q', '-m', 'not realdata')
    Invoke-Checked $python @('scripts/fetch_testdata.py')
    Invoke-Checked $python @('-m', 'pytest', 'tests', '-q', '-m', 'realdata')
    # Windows 11 normally has WebView2. This per-user bootstrapper is only used
    # if it is absent, and does not request elevation or change system policies.
    & (Join-Path $repo 'packaging\ensure_webview2.ps1')
    Invoke-Checked $python @('packaging/build_windows.py', '--smoke-file', 'docs/example_cell.nd2', '--source-manifest', $manifestPath, '--out', $output)
    $archives = @(Get-ChildItem -LiteralPath $output -Filter '*.zip')
    if ($archives.Count -ne 1) { throw 'Expected exactly one completed portable ZIP.' }
    $powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    Invoke-Checked $powershell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $repo 'packaging\verify_windows.ps1'), '-Archive', $archives[0].FullName, '-SmokeFile', (Join-Path $repo 'docs\example_cell.nd2'), '-Evidence', (Join-Path $output 'verification'))
    $completed = @{
        state = 'complete'; stage = $stage; source_zip_sha256 = $sourceHash
        base_commit = $manifest.base_commit; working_tree_dirty = $manifest.working_tree_dirty
        binary_architecture = 'x64'; host_architecture = $env:PROCESSOR_ARCHITECTURE
        completed = (Get-Date).ToUniversalTime().ToString('o')
    }
    $completed | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'local-build.json') -Encoding UTF8
    $completed | ConvertTo-Json | Set-Content -LiteralPath $status -Encoding UTF8
    $success = $true
}
catch {
    @{ state = 'failed'; stage = $stage; error = $_.Exception.Message; ended = (Get-Date).ToUniversalTime().ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $status -Encoding UTF8
    Write-Host ($_ | Out-String)
}
finally {
    Set-Location -LiteralPath $previousLocation.Path
    [Net.ServicePointManager]::SecurityProtocol = $tlsBefore
    foreach ($name in $environmentBefore.Keys) {
        [Environment]::SetEnvironmentVariable($name, $environmentBefore[$name], 'Process')
    }
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    Copy-Item -Path (Join-Path $output '*') -Destination $resultPath -Recurse -Force
}
if (-not $success) { exit 1 }
Write-Host "Local Windows build completed: $resultPath"

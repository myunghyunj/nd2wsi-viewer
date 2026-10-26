#requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$SmokeFile,
    [string]$Evidence = 'dist/windows/verification',
    # Require the bundled WebView2 Fixed Version runtime to render every window.
    [switch]$RequireBundledWebView2
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

# Walk a ConvertFrom-Json object under StrictMode; any missing member gives $null.
function Get-JsonMember {
    param($Object, [string[]]$Path)
    $current = $Object
    foreach ($name in $Path) {
        if ($null -eq $current) { return $null }
        $property = $current.PSObject.Properties[$name]
        if ($null -eq $property) { return $null }
        $current = $property.Value
    }
    return $current
}

# The version alone proves nothing (the system Evergreen runtime can match it),
# so the browser process image must be this run's extracted bundled copy.
# $env:TEMP can be an 8.3 short path while the report holds the long path, so
# match this run's unique folder name plus the fixed suffix, not a prefix.
function Assert-BundledWebView2Evidence {
    param($Report, [string]$Label, [string[]]$Path, [string]$RunId, [string]$Version)
    $where = $Path -join '.'
    $proof = Get-JsonMember $Report $Path
    if ($null -eq $proof) { throw "Packaged GUI smoke has no WebView2 runtime evidence for the $Label ($where)." }
    $executable = [string](Get-JsonMember $proof @('browser_executable'))
    if ((Get-JsonMember $proof @('bundled')) -ne $true) {
        $reason = [string](Get-JsonMember $proof @('error'))
        throw "The $Label was not rendered by the bundled WebView2 runtime (${where}: $executable $reason)."
    }
    if ($executable -notlike "*$RunId*" -or $executable -notlike '*\nd2wsi-viewer\_internal\webview2-runtime\msedgewebview2.exe') {
        throw "The $Label's WebView2 browser process is not this extracted bundle's msedgewebview2.exe: $executable"
    }
    $browserVersion = [string](Get-JsonMember $proof @('browser_version'))
    if ($browserVersion -cne $Version) {
        throw "The $Label reported WebView2 $browserVersion, not the bundled $Version."
    }
    return $executable
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
$runId = [guid]::NewGuid().ToString('N')
$testRoot = Join-Path $env:TEMP ("nd2wsi $koreanVerification " + $runId)
New-Item -ItemType Directory -Path $testRoot | Out-Null
# .NET extraction: Expand-Archive draws progress (its module ignores this
# script's $ProgressPreference) and is very slow on a ZIP with a bundled runtime.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $testRoot)
$exe = Join-Path $testRoot 'nd2wsi-viewer\nd2wsi-viewer.exe'
$slide = Join-Path $testRoot ($koreanCell + ' sample.nd2')
Copy-Item -LiteralPath $sourcePath -Destination $slide
$manifest = Get-Content -LiteralPath (Join-Path $testRoot 'nd2wsi-viewer\build-info.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.binary_architecture -ne 'x64') { throw 'Unexpected executable architecture in the build manifest.' }
if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.executable_sha256) {
    throw 'The executable checksum does not match the build manifest.'
}
$codecManifest = Join-Path $testRoot 'nd2wsi-viewer\sdpc-codec-manifest.json'
if ($manifest.sdpc_codec_bundled -ne $true -or $manifest.distribution_status -ne 'source-built-lgpl-codec') {
    throw 'This release requires the verified source-built LGPL SDPC codec.'
}
if ((Get-FileHash -LiteralPath $codecManifest -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.sdpc_codec_manifest_sha256) {
    throw 'The packaged SDPC codec provenance does not match the build manifest.'
}

$webview2Distribution = Get-JsonMember $manifest @('webview2_runtime', 'distribution')
$webview2Version = $null
if ($RequireBundledWebView2) {
    if ($webview2Distribution -ne 'fixed-version-bundled') {
        throw 'This build does not bundle the WebView2 Fixed Version runtime (build-info webview2_runtime.distribution).'
    }
    $webview2 = $manifest.webview2_runtime
    foreach ($name in @('version', 'path', 'file_count', 'files', 'browser_executable_sha256')) {
        if ($null -eq $webview2.PSObject.Properties[$name]) { throw "The build manifest webview2_runtime omits $name." }
    }
    if ($webview2.path -cne '_internal/webview2-runtime') { throw 'The build manifest names an unexpected WebView2 runtime folder.' }
    $webview2Version = [string]$webview2.version
    $runtimeRoot = Join-Path $testRoot 'nd2wsi-viewer\_internal\webview2-runtime'
    if (-not (Test-Path -LiteralPath $runtimeRoot -PathType Container)) { throw 'The extracted app has no bundled WebView2 runtime folder.' }
    # Every shipped runtime file, and nothing else, must match the build manifest.
    $expectedRuntimeFiles = @($webview2.files.PSObject.Properties)
    $actualRuntimeFiles = @(Get-ChildItem -LiteralPath $runtimeRoot -Recurse -File -Force)
    if ($expectedRuntimeFiles.Count -ne [int]$webview2.file_count -or $actualRuntimeFiles.Count -ne $expectedRuntimeFiles.Count) {
        throw "The bundled WebView2 runtime has $($actualRuntimeFiles.Count) files; the build manifest lists $($expectedRuntimeFiles.Count) (file_count $($webview2.file_count))."
    }
    foreach ($entry in $expectedRuntimeFiles) {
        $runtimeFile = Join-Path $runtimeRoot ($entry.Name -replace '/', '\')
        if (-not (Test-Path -LiteralPath $runtimeFile -PathType Leaf)) { throw "The bundled WebView2 runtime is missing $($entry.Name)." }
        if ((Get-FileHash -LiteralPath $runtimeFile -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$entry.Value) {
            throw "The bundled WebView2 runtime file $($entry.Name) does not match the build manifest."
        }
    }
    $bundledBrowser = Join-Path $runtimeRoot 'msedgewebview2.exe'
    if ((Get-FileHash -LiteralPath $bundledBrowser -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$webview2.browser_executable_sha256) {
        throw 'The bundled msedgewebview2.exe does not match the build manifest.'
    }
    $browserSignature = Get-AuthenticodeSignature -LiteralPath $bundledBrowser
    if ($browserSignature.Status -ne 'Valid' -or $null -eq $browserSignature.SignerCertificate -or $browserSignature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)') {
        throw 'The bundled msedgewebview2.exe does not have a valid Microsoft Corporation Authenticode signature.'
    }
    Write-Host "Bundled WebView2 Fixed Version $webview2Version matches the build manifest ($($expectedRuntimeFiles.Count) files)."
}

$runs = @()
$previousLog = $env:ND2WSI_LOG_FILE
$previousWebView2Mode = $env:ND2WSI_WEBVIEW2_RUNTIME
$webview2Main = $null
$webview2Translucent = $null
$webview2Child = $null
$webview2System = $null
$webview2NegativeControl = $null
# Each run: its evidence name, the app flag, and an optional WebView2 mode override.
$plan = @(
    @{ name = 'smoke'; flag = '--smoke'; webview2 = $null },
    @{ name = 'gui-smoke'; flag = '--gui-smoke'; webview2 = $null }
)
if ($RequireBundledWebView2) {
    # Negative control: on the system runtime the same evidence must say "not bundled",
    # which shows the proof can tell the two runtimes apart on this host.
    $plan += @{ name = 'gui-smoke-system'; flag = '--gui-smoke'; webview2 = 'system' }
}
try {
    foreach ($step in $plan) {
        $mode = $step.name
        if ($null -ne $step.webview2) { $env:ND2WSI_WEBVIEW2_RUNTIME = $step.webview2 }
        elseif ($RequireBundledWebView2) { $env:ND2WSI_WEBVIEW2_RUNTIME = 'bundled' }
        $log = Join-Path $evidencePath "$mode.log"
        Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
        $env:ND2WSI_LOG_FILE = $log
        $arguments = @($step.flag, $slide)
        if ($step.flag -eq '--gui-smoke') {
            $guiReport = Join-Path $evidencePath "$mode.json"
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
        # A cold Fixed Version start (and its first antivirus scan) takes longer:
        # 180 s readiness, the overlay, a 240 s cold child and the close fit in 600 s.
        $timeout = if ($step.flag -eq '--smoke') { 600000 } elseif ($RequireBundledWebView2) { 600000 } else { 180000 }
        if (-not $process.WaitForExit($timeout)) {
            $process.Kill()
            $process.WaitForExit(10000) | Out-Null
            throw "Packaged $mode timed out."
        }
        $timer.Stop()
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Encoding UTF8 }
        if ($process.ExitCode -ne 0) { throw "Packaged $mode failed with exit code $($process.ExitCode)." }
        if ($mode -eq 'gui-smoke-system') {
            if (-not (Test-Path -LiteralPath $guiReport)) { throw 'The system-runtime GUI smoke did not record completion.' }
            $gui = Get-Content -LiteralPath $guiReport -Raw -Encoding UTF8 | ConvertFrom-Json
            if ((Get-JsonMember $gui @('ok')) -ne $true) { throw 'The system-runtime GUI smoke did not record successful completion.' }
            $controlProof = Get-JsonMember $gui @('webview2_runtime', 'main')
            if ($null -eq $controlProof) { throw 'The system-runtime GUI smoke has no WebView2 runtime evidence.' }
            $controlExecutable = [string](Get-JsonMember $controlProof @('browser_executable'))
            if ((Get-JsonMember $controlProof @('bundled')) -ne $false -or [string]::IsNullOrEmpty($controlExecutable) -or
                $controlExecutable -like "*$runId*") {
                throw "Negative control failed: on the system runtime the evidence did not report a non-bundled browser ($controlExecutable)."
            }
            $webview2NegativeControl = $controlExecutable
            Write-Host "Negative control: the system runtime rendered from $controlExecutable and was reported as not bundled."
        }
        elseif ($mode -eq 'gui-smoke') {
            if (-not (Test-Path -LiteralPath $guiReport)) { throw 'Packaged GUI smoke did not record successful completion.' }
            $gui = Get-Content -LiteralPath $guiReport -Raw -Encoding UTF8 | ConvertFrom-Json
            if ((Get-JsonMember $gui @('ok')) -ne $true) { throw 'Packaged GUI smoke did not record successful completion.' }
            if ($RequireBundledWebView2) {
                $webview2Main = Assert-BundledWebView2Evidence -Report $gui -Label 'main window' -Path @('webview2_runtime', 'main') -RunId $runId -Version $webview2Version
                $webview2Translucent = Assert-BundledWebView2Evidence -Report $gui -Label 'translucent viewer' -Path @('webview2_runtime', 'translucent') -RunId $runId -Version $webview2Version
                $webview2Child = Assert-BundledWebView2Evidence -Report $gui -Label 'independent window' -Path @('independent_window', 'child_gui', 'webview2_runtime', 'main') -RunId $runId -Version $webview2Version
                $webview2System = Get-JsonMember $gui @('webview2_runtime', 'main', 'system_version')
                Write-Host "Bundled WebView2 $webview2Version rendered every window (system Evergreen: $webview2System)."
            }
        }
        elseif (-not (Test-Path -LiteralPath $log) -or (Get-Content -LiteralPath $log -Raw -Encoding UTF8) -notmatch 'smoke ok: SDPC') {
            throw "Packaged $mode did not record successful SDPC decoder and ROI checks."
        }
        elseif ((Get-Content -LiteralPath $log -Raw -Encoding UTF8) -notmatch 'smoke ok: Astromap JPEG, TXT and PDF') {
            throw "Packaged $mode did not record successful Astromap export checks."
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
        sdpc_packaged_decoder_and_roi_verified = $true
        astromap_jpeg_txt_pdf_verified = $true
        sdpc_codec_manifest_sha256 = $manifest.sdpc_codec_manifest_sha256
        sdpc_codec_source_url = $manifest.sdpc_codec_source.source_url
        sdpc_codec_source_archive_sha256 = $manifest.sdpc_codec_source.source_archive_sha256
        webview2_runtime_distribution = $webview2Distribution
        webview2_runtime_version = $webview2Version
        webview2_bundled_runtime_proven = [bool]$RequireBundledWebView2
        webview2_main_browser_executable = $webview2Main
        webview2_translucent_browser_executable = $webview2Translucent
        webview2_child_browser_executable = $webview2Child
        webview2_system_version = $webview2System
        webview2_negative_control_browser_executable = $webview2NegativeControl
        runs = $runs
    }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $evidencePath 'verification.json') -Encoding utf8
    Write-Host 'Windows packaged verification passed.'
}
finally {
    $env:ND2WSI_LOG_FILE = $previousLog
    $env:ND2WSI_WEBVIEW2_RUNTIME = $previousWebView2Mode
    # Only this invocation's private fixture/extraction directory is disposable.
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}

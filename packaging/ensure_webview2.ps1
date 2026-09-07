# Prepare the official system prerequisite on clean Windows CI runners.
# Windows 11 normally already has this runtime. It is not part of the ZIP.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-WebView2Version {
    $client = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    foreach ($root in @('HKCU:\Software', 'HKLM:\Software', 'HKLM:\Software\WOW6432Node')) {
        $key = "$root\Microsoft\EdgeUpdate\Clients\$client"
        $item = Get-ItemProperty -LiteralPath $key -Name pv -ErrorAction SilentlyContinue
        if ($null -ne $item -and $item.pv -and $item.pv -ne '0.0.0.0') {
            return [string]$item.pv
        }
    }
    return $null
}

$version = Get-WebView2Version
if (-not $version) {
    $installer = Join-Path $env:TEMP 'nd2wsi-MicrosoftEdgeWebview2Setup.exe'
    Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $installer
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
        throw 'The WebView2 bootstrapper does not have a valid Microsoft signature.'
    }
    $process = Start-Process -FilePath $installer -ArgumentList '/silent', '/install' -Wait -PassThru
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "WebView2 setup failed with exit code $($process.ExitCode)."
    }
    $version = Get-WebView2Version
    if (-not $version) { throw 'WebView2 setup completed but the runtime could not be detected.' }
}
Write-Host "Microsoft Edge WebView2 Runtime: $version"

# Build the front end and upload it to SiteGround.
#
#   .\scripts\deploy_frontend.ps1 -ApiUrl https://race-exam-api.onrender.com `
#                                 -SshHost example.siteground.biz `
#                                 -SshUser u1234-abcdef -SshPort 18765
#
# Omit the SSH parameters to build only, then upload dist\ yourself through
# Site Tools -> File Manager.

param(
    [Parameter(Mandatory = $true)][string]$ApiUrl,
    [string]$SshHost,
    [string]$SshUser,
    [int]$SshPort = 18765,
    [string]$RemotePath = "~/www/exam.txglobal.com.au/public_html",
    [string]$IdentityFile
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$frontend = Join-Path $root "frontend"

Write-Host "Building front end against $ApiUrl" -ForegroundColor Cyan

# Baked in at build time; Vite inlines it, so it cannot be changed afterwards
# without rebuilding.
Set-Content -Path (Join-Path $frontend ".env.production") `
            -Value "VITE_API_BASE_URL=$($ApiUrl.TrimEnd('/'))" -Encoding utf8

Push-Location $frontend
try {
    npm install --no-fund --no-audit
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
} finally {
    Pop-Location
}

$dist = Join-Path $frontend "dist"
$htaccess = Join-Path $frontend "public\.htaccess"

# Vite copies public/ into dist/, but a leading-dot file is easy to lose, so
# make sure the SPA rewrite rules actually shipped.
if (-not (Test-Path (Join-Path $dist ".htaccess"))) {
    Copy-Item $htaccess (Join-Path $dist ".htaccess") -Force
    Write-Host "Copied .htaccess into dist" -ForegroundColor Yellow
}

$size = "{0:N1}" -f ((Get-ChildItem $dist -Recurse | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Built $dist ($size MB)" -ForegroundColor Green

if (-not $SshHost) {
    Write-Host ""
    Write-Host "No -SshHost given. Upload the CONTENTS of dist\ to your" -ForegroundColor Yellow
    Write-Host "exam.txglobal.com.au document root via Site Tools > File Manager." -ForegroundColor Yellow
    exit 0
}

$target = "$SshUser@$SshHost`:$RemotePath/"
Write-Host "Uploading to $target" -ForegroundColor Cyan

$scpArgs = @("-P", $SshPort, "-r")
if ($IdentityFile) { $scpArgs += @("-i", $IdentityFile) }
# The trailing \* uploads the contents rather than the dist folder itself.
$scpArgs += @("$dist\*", $target)

& scp @scpArgs
if ($LASTEXITCODE -ne 0) { throw "Upload failed" }

# scp -r skips dotfiles, so the SPA rewrite has to be sent explicitly or every
# deep link on the live site would 404.
$dotArgs = @("-P", $SshPort)
if ($IdentityFile) { $dotArgs += @("-i", $IdentityFile) }
$dotArgs += @((Join-Path $dist ".htaccess"), $target)
& scp @dotArgs

# scp creates assets/ as 700, which Apache cannot traverse - every bundle 404s
# and the site loads blank. Put the web-readable bits back.
$sshArgs = @("-p", $SshPort)
if ($IdentityFile) { $sshArgs += @("-i", $IdentityFile) }
$sshArgs += @("$SshUser@$SshHost", "cd $RemotePath && chmod 755 . assets && chmod 644 index.html .htaccess assets/*")
& ssh @sshArgs

Write-Host "Deployed. Check https://exam.txglobal.com.au" -ForegroundColor Green

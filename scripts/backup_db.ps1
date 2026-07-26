# Back up the production PostgreSQL database.
#
#   .\scripts\backup_db.ps1
#   .\scripts\backup_db.ps1 -OutDir D:\backups -Keep 30
#
# There is no pg_dump on Windows here, but SiteGround's shell has one and the
# database lives on that host, so the dump runs there and the file is pulled
# down over the existing deploy key. The remote copy is removed afterwards:
# a database backup sitting in the web account is a liability, not a spare.
#
# Restore with:
#   gunzip -c race_YYYY-MM-DD.sql.gz | psql "<DATABASE_URL>"

param(
    [string]$OutDir = "$PSScriptRoot\..\backups",
    [int]$Keep = 14,
    [string]$EnvFile = "$PSScriptRoot\..\backend\.env.production",
    [string]$IdentityFile = "$HOME\.ssh\race_siteground"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $EnvFile)) { throw "No env file at $EnvFile" }

$env_ = @{}
Get-Content $EnvFile | Where-Object { $_ -match '^\s*[A-Z_]+\s*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2
    $env_[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
}

foreach ($key in @('DATABASE_URL', 'SITEGROUND_SSH_HOST', 'SITEGROUND_SSH_USER', 'SITEGROUND_SSH_PORT')) {
    if (-not $env_[$key]) { throw "$key is missing from $EnvFile" }
}

# postgresql+psycopg://user:pass@host:port/dbname -> the parts pg_dump wants.
if ($env_['DATABASE_URL'] -notmatch '^[^:]+://([^:]+):([^@]+)@([^:/]+):?(\d+)?/(.+?)(\?.*)?$') {
    throw "Could not parse DATABASE_URL"
}
$dbUser = $Matches[1]; $dbPass = $Matches[2]; $dbHost = $Matches[3]
$dbPort = if ($Matches[4]) { $Matches[4] } else { "5432" }
$dbName = $Matches[5]

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$name = "race_$stamp.sql.gz"
$local = Join-Path $OutDir $name
# Relative to the login home. Not "~/...": inside the quoting below the remote
# shell would take the tilde literally and fail to create the file.
$remote = "db_backup_$name"

$ssh = @("-p", $env_['SITEGROUND_SSH_PORT'], "-i", $IdentityFile,
         "$($env_['SITEGROUND_SSH_USER'])@$($env_['SITEGROUND_SSH_HOST'])")

Write-Host "Dumping $dbName on $dbHost" -ForegroundColor Cyan

# The password goes in the remote environment for the life of one command
# rather than on the command line, where it would show up in that host's
# process list to anyone looking.
$remoteCmd = "PGPASSWORD='$dbPass' pg_dump -h '$dbHost' -p $dbPort -U '$dbUser' " +
             "--no-owner --no-privileges '$dbName' | gzip -9 > '$remote'"
& ssh @ssh $remoteCmd
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed on the remote host" }

& scp -P $env_['SITEGROUND_SSH_PORT'] -i $IdentityFile `
      "$($env_['SITEGROUND_SSH_USER'])@$($env_['SITEGROUND_SSH_HOST']):$remote" $local
if ($LASTEXITCODE -ne 0) { throw "Could not download the dump" }

& ssh @ssh "rm -f '$remote'"

$size = (Get-Item $local).Length
if ($size -lt 50KB) {
    throw "Backup is only $size bytes - that is not a full database. Kept at $local for inspection."
}
Write-Host ("Saved {0} ({1:N1} MB)" -f $local, ($size / 1MB)) -ForegroundColor Green

# Keep the most recent $Keep, drop the rest.
$old = Get-ChildItem $OutDir -Filter "race_*.sql.gz" |
       Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
foreach ($file in $old) {
    Remove-Item $file.FullName -Force
    Write-Host "Removed old backup $($file.Name)" -ForegroundColor DarkGray
}

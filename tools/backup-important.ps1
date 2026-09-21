# =============================================================================
#  HEZIIS NET - stage your irreplaceable files for cloud backup
# =============================================================================
#  Sorts your files into "would be gone forever" and "can be downloaded again",
#  then copies the first group somewhere you can upload.
#
#      # see what it would do, change nothing
#      .\tools\backup-important.ps1 -WhatIf
#
#      # stage into your OneDrive folder (syncs automatically)
#      .\tools\backup-important.ps1
#
#      # stage somewhere else, or pick sources
#      .\tools\backup-important.ps1 -Out D:\backup -Source Desktop,Pictures
#
#  WHY NOT JUST COPY EVERYTHING
#  ----------------------------
#  A full copy is 82 GB on this machine, and most of it is downloaded films.
#  Uploading 39 GB of Guardians of the Galaxy to protect 2 GB of family photos
#  is a waste of your data bundle, and it often means the backup never happens.
#
#  NOTHING IS DELETED. This only copies. Your originals stay exactly as they are.
# =============================================================================

[CmdletBinding()]
param(
    # Folders under your user profile to scan
    [string[]]$Source = @('Desktop', 'Documents', 'Downloads', 'Pictures'),

    # Where to put the copy. Defaults to OneDrive so it syncs by itself.
    [string]$Out,

    # Extra extensions to KEEP, e.g. -IncludeExt .psd,.ai
    [string[]]$IncludeExt = @(),

    # Extra extensions to SKIP, e.g. -ExcludeExt .iso
    [string[]]$ExcludeExt = @(),

    # Maximum size for a single file, in GB. Bigger ones are listed but skipped.
    [double]$MaxFileGB = 2.0,

    # Report only - copy nothing
    [switch]$WhatIf
)

$ErrorActionPreference = 'Continue'

# --- classification ----------------------------------------------------------
# KEEP: things that cannot be re-created - documents, photos, project files.
$KEEP_EXT = @(
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.pdf', '.txt', '.csv',
    '.rtf', '.odt', '.ods', '.odp', '.md',
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.heic', '.heif', '.webp', '.tif', '.tiff', '.svg',
    '.zip', '.rar', '.7z',
    '.py', '.js', '.ts', '.tsx', '.jsx', '.html', '.css', '.json', '.xml', '.yml', '.yaml',
    '.sql', '.db', '.sqlite', '.sqlite3',
    '.pem', '.key', '.crt', '.cer', '.ovpn', '.conf',
    '.apk', '.epub', '.mobi',
    '.ps1', '.sh', '.bat', '.cmd', '.ipynb', '.env'
)

# SKIP: large files that are almost always replaceable.
$SKIP_EXT = @(
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp',
    '.mp3', '.m4a', '.wav', '.flac', '.aac', '.ogg', '.wma',
    '.iso', '.img', '.vhd', '.vhdx', '.vmdk', '.vdi', '.dmg',
    '.exe', '.msi', '.msu', '.cab', '.bin', '.deb', '.rpm',
    '.tmp', '.temp', '.log', '.bak', '.old', '.crdownload', '.part', '.filepart'
)

# SKIP: folders that only ever hold regenerateable junk.
$SKIP_DIR = @(
    'node_modules', '.git', '$RECYCLE.BIN', 'System Volume Information',
    '.venv', 'venv', '__pycache__', '.cache', '.npm', '.gradle', '.m2',
    'Cache', 'CachedData', 'Code Cache', 'GPUCache', 'Temp', 'tmp'
)

if ($IncludeExt) { $KEEP_EXT += ($IncludeExt | ForEach-Object { $_.ToLower() }) }
if ($ExcludeExt) { $SKIP_EXT += ($ExcludeExt | ForEach-Object { $_.ToLower() }) }

if (-not $Out) {
    $oneDrive = Join-Path $env:USERPROFILE 'OneDrive'
    if (Test-Path $oneDrive) {
        $Out = Join-Path $oneDrive ("Backup-" + (Get-Date -Format 'yyyy-MM-dd'))
    } else {
        $Out = Join-Path $env:USERPROFILE ("Backup-" + (Get-Date -Format 'yyyy-MM-dd'))
        Write-Host ""
        Write-Host "  No OneDrive folder found - staging to your user folder instead." -ForegroundColor Yellow
        Write-Host "  You will need to upload that folder yourself." -ForegroundColor Yellow
    }
}

function Head($t) { Write-Host ""; Write-Host $t -ForegroundColor Cyan; Write-Host ('-' * 74) }
function Ok($t)   { Write-Host "  OK    $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  WARN  $t" -ForegroundColor Yellow }
function Info($t) { Write-Host "  $t" }

Write-Host ""
Write-Host "HEZIIS NET - stage irreplaceable files for backup" -ForegroundColor Cyan
if ($WhatIf) { Write-Host "MODE: report only - nothing will be copied" -ForegroundColor Yellow }
Info "scanning : $($Source -join ', ')"
Info "destination: $Out"

# --- scan --------------------------------------------------------------------
function Get-FilesSafe {
    # Manual walk instead of Get-ChildItem -Recurse.
    #
    # -Recurse FOLLOWS junctions and symlinks. Profiles often contain one that
    # points back up the tree (or into a OneDrive placeholder tree), and the scan
    # then either loops or crawls for minutes with no output. A manual stack
    # lets us prune those, and prune the junk folders on the way down instead of
    # filtering after we have already paid to enumerate them.
    param([string]$Root)

    $junkNames = @{}
    foreach ($j in $SKIP_DIR) { $junkNames[$j.ToLower()] = $true }
    $reparse = [IO.FileAttributes]::ReparsePoint

    $stack = New-Object System.Collections.Stack
    $stack.Push($Root)
    while ($stack.Count -gt 0) {
        $cur = $stack.Pop()
        foreach ($item in @(Get-ChildItem -LiteralPath $cur -Force -ErrorAction SilentlyContinue)) {
            # never descend through a junction/symlink
            if ([int]$item.Attributes -band [int]$reparse) { continue }
            if ($item.PSIsContainer) {
                if ($junkNames.ContainsKey($item.Name.ToLower())) { continue }
                $stack.Push($item.FullName)
            } else {
                $item
            }
        }
    }
}

Head 'Scanning'
$keep = @()
$skipBig = @()
$skipJunk = 0
$skippedBytes = 0L
$maxBytes = $MaxFileGB * 1GB

foreach ($name in $Source) {
    $dir = if ([System.IO.Path]::IsPathRooted($name)) { $name } else { Join-Path $env:USERPROFILE $name }
    if (-not (Test-Path $dir)) { Warn "not found: $dir"; continue }

    $files = @(Get-FilesSafe -Root $dir)

    $k = 0
    foreach ($f in $files) {
        $ext = $f.Extension.ToLower()
        if ($SKIP_EXT -contains $ext) {
            $skipJunk++
            $skippedBytes += $f.Length
            if ($f.Length -gt 100MB) { $skipBig += $f }
            continue
        }
        if ($KEEP_EXT -contains $ext) {
            if ($f.Length -gt $maxBytes) {
                $skipBig += $f
                continue
            }
            $keep += $f
            $k++
        }
        # unknown extensions are neither kept nor counted as junk - reported below
    }
    Info ("{0,-12} {1,6} files worth keeping" -f $name, $k)
}

$keepBytes = ($keep | Measure-Object Length -Sum).Sum
if (-not $keepBytes) { $keepBytes = 0 }

Head 'What would be backed up'
Info ("{0} files, {1} GB" -f $keep.Count, [math]::Round($keepBytes / 1GB, 2))
if ($keep.Count -gt 0) {
    $keep | Group-Object Extension |
        ForEach-Object { [pscustomobject]@{ Ext = $_.Name; GB = [math]::Round((($_.Group | Measure-Object Length -Sum).Sum) / 1GB, 2); N = $_.Count } } |
        Sort-Object GB -Descending |
        Select-Object -First 10 |
        ForEach-Object { Info ("  {0,-10} {1,8} GB   {2} files" -f $_.Ext, $_.GB, $_.N) }
}

Head 'What would be left out (re-downloadable or too big)'
Info ("{0} files, {1} GB  (media, installers, archives, temp)" -f $skipJunk, [math]::Round($skippedBytes / 1GB, 2))
if ($skipBig.Count -gt 0) {
    Info ""
    Info "  Individual files over $MaxFileGB GB:"
    $skipBig | Sort-Object Length -Descending | Select-Object -First 10 |
        ForEach-Object { Info ("    {0,9} MB   {1}" -f [math]::Round($_.Length / 1MB, 1), $_.Name) }
    if ($skipBig.Count -gt 10) { Info "    ... and $($skipBig.Count - 10) more" }
}

# --- copy --------------------------------------------------------------------
if ($WhatIf) {
    Head 'Report only'
    Info 'Nothing was copied. Re-run without -WhatIf to stage the files.'
} elseif ($keep.Count -eq 0) {
    Head 'Nothing to copy'
    Warn 'No files matched the keep list. Add types with -IncludeExt .psd'
} else {
    Head "Copying to $Out"
    if (-not (Test-Path $Out)) { New-Item -ItemType Directory -Path $Out -Force | Out-Null }

    $done = 0
    $failed = @()
    $copiedBytes = 0L
    foreach ($f in $keep) {
        # keep the folder shape so you can tell where things came from
        $src = $null
        foreach ($name in $Source) {
            $dir = if ([System.IO.Path]::IsPathRooted($name)) { $name } else { Join-Path $env:USERPROFILE $name }
            if ($f.FullName.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase)) {
                $leaf = Split-Path $dir -Leaf
                $src = Join-Path $leaf $f.FullName.Substring($dir.Length).TrimStart('\')
                break
            }
        }
        if (-not $src) { $src = $f.Name }

        $dest = Join-Path $Out $src
        $destDir = [System.IO.Path]::GetDirectoryName($dest)
        try {
            if (-not (Test-Path -LiteralPath $destDir)) {
                New-Item -ItemType Directory -Path $destDir -Force -ErrorAction Stop | Out-Null
            }
            Copy-Item -LiteralPath $f.FullName -Destination $dest -Force -ErrorAction Stop
            $done++
            $copiedBytes += $f.Length
            if ($done % 50 -eq 0) { Info "$done / $($keep.Count) copied..." }
        } catch {
            $failed += "$($f.FullName)  ->  $($_.Exception.Message)"
        }
    }

    Head 'Result'
    Ok ("copied {0} of {1} files, {2} GB" -f $done, $keep.Count, [math]::Round($copiedBytes / 1GB, 2))
    if ($failed.Count -gt 0) {
        Warn "$($failed.Count) file(s) failed:"
        $failed | Select-Object -First 10 | ForEach-Object { Info "  $_" }
    }
    Write-Host ""
    Ok "Staged at: $Out"
    if ($Out -like "*OneDrive*") {
        Info "OneDrive should start syncing automatically - check the cloud icon in"
        Info "the taskbar, and make sure your OneDrive has room for $([math]::Round($copiedBytes/1GB,2)) GB."
    } else {
        Info "Upload that folder to your cloud storage now."
    }
    Write-Host ""
    Info "Your originals were NOT changed. Nothing was deleted."
}

Head 'Reminder'
Info 'This is a copy, not a backup you have verified. Open a few files from the'
Info 'destination and check they are intact before you rely on it.'
Write-Host ""

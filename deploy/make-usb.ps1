<#
================================================================================
 HEZIIS NET - make a bootable Ubuntu USB stick
================================================================================
 Turns a plain USB stick into a bootable Ubuntu Server installer, using only
 what ships with Windows (diskpart + robocopy). No Rufus, no Etcher, no
 downloads of extra tools.

 RUN THIS IN AN ADMINISTRATOR POWERSHELL. It deliberately does NOT try to
 elevate itself, because a UAC prompt is exactly the thing you should never
 click through on autopilot for a command that erases a disk.

     # 1. see what sticks are plugged in
     .\deploy\make-usb.ps1 -List

     # 2. write the stick (use the DiskNumber from step 1)
     .\deploy\make-usb.ps1 -DiskNumber 2

     # 3. if you do not have the ISO yet, fetch it first
     .\deploy\make-usb.ps1 -Download

 !!! THIS ERASES THE ENTIRE USB STICK. EVERYTHING ON IT WILL BE GONE. !!!
================================================================================
#>

[CmdletBinding()]
param(
    # Disk number of the USB stick, as shown by -List
    [int]$DiskNumber = -1,

    # Just show the drives and exit
    [switch]$List,

    # Download the Ubuntu Server ISO into Downloads\ and exit
    [switch]$Download,

    # Path to the ISO (auto-detected if omitted)
    [string]$Iso,

    # Skip the "type the number back" confirmation (for scripted use)
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$IsoUrl  = 'https://releases.ubuntu.com/24.04/ubuntu-24.04.3-live-server-amd64.iso'
$IsoName = 'ubuntu-24.04.3-live-server-amd64.iso'
$IsoMB   = 3150

function Write-Head($t) { Write-Host "`n$t" -ForegroundColor Cyan; Write-Host ('-' * 72) }
function Write-Ok($t)   { Write-Host "  OK   $t" -ForegroundColor Green }
function Write-Bad($t)  { Write-Host "  FAIL $t" -ForegroundColor Red }
function Write-Warn2($t){ Write-Host "  WARN $t" -ForegroundColor Yellow }

# ── are we elevated? ────────────────────────────────────────────────────────
function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not (Test-Admin)) {
    Write-Host ""
    Write-Bad "This script must run in an ADMINISTRATOR PowerShell."
    Write-Host ""
    Write-Host "  Right-click the Start button -> 'Terminal (Admin)' or" -ForegroundColor White
    Write-Host "  'Windows PowerShell (Admin)', then run:" -ForegroundColor White
    Write-Host ""
    Write-Host "      cd $PSScriptRoot\.." -ForegroundColor Yellow
    Write-Host "      .\deploy\make-usb.ps1 -DiskNumber <n>" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# ── list drives ─────────────────────────────────────────────────────────────
function Get-UsbDisks {
    Get-CimInstance Win32_DiskDrive | Where-Object {
        $_.InterfaceType -eq 'USB' -or $_.MediaType -like '*Removable*'
    }
}

function Show-Disks {
    Write-Head 'Removable (USB) disks'
    $usb = @(Get-UsbDisks)
    if ($usb.Count -eq 0) {
        Write-Warn2 'No USB stick detected.'
        Write-Host ""
        Write-Host "  Plug one in and run this again. It needs to be at least 6 GB" -ForegroundColor White
        Write-Host "  (the ISO is $IsoMB MB)." -ForegroundColor White
        Write-Host ""
        return $false
    }
    Write-Host ""
    foreach ($d in $usb) {
        $gb = [math]::Round($d.Size / 1GB, 1)
        $letters = (Get-CimInstance Win32_LogicalDisk |
                    Where-Object { $_.DeviceID -and $d.DeviceID -and $true } |
                    Select-Object -First 0)
        Write-Host ("   Disk {0}  {1,-34} {2,6} GB" -f $d.Index, $d.Model, $gb) -ForegroundColor White
        if ($gb -lt 5.5) {
            Write-Host "            ^ too small for Ubuntu" -ForegroundColor Red
        }
        $vol = Get-Partition -DiskNumber $d.Index -ErrorAction SilentlyContinue |
               Get-Volume -ErrorAction SilentlyContinue |
               Where-Object DriveLetter
        if ($vol) {
            Write-Host ("            drive letter(s): {0}   (contains: {1})" -f
                (($vol.DriveLetter | ForEach-Object { "$_`:" }) -join ' '),
                (($vol.FileSystemLabel | Where-Object { $_ }) -join ', ')) -ForegroundColor DarkGray
        }
    }
    Write-Host ""
    Write-Host "  Everything on the chosen disk will be DESTROYED." -ForegroundColor Yellow
    return $true
}

Write-Host ""
Write-Host "HEZIIS NET — bootable Ubuntu USB" -ForegroundColor Cyan

if ($List) { Show-Disks | Out-Null; exit 0 }

# ── optional: download the ISO ──────────────────────────────────────────────
if ($Download) {
    $dest = Join-Path $env:USERPROFILE "Downloads\$IsoName"
    Write-Head "Downloading Ubuntu Server 24.04 LTS (~$IsoMB MB)"
    Write-Host "  -> $dest" -ForegroundColor DarkGray
    Write-Host "  This is a big download on a mobile connection. Ctrl+C to stop." -ForegroundColor Yellow
    Write-Host ""
    # curl.exe is bundled with Windows and handles redirects correctly.
    # (Invoke-WebRequest has been seen writing 0-byte files for some redirects.)
    & curl.exe -L --fail --progress-bar -o $dest $IsoUrl
    if ($LASTEXITCODE -ne 0 -and -not (Test-Path $dest)) {
        Write-Bad "Download failed (curl exit $LASTEXITCODE)"
        exit 1
    }
    $mb = [math]::Round((Get-Item $dest).Length / 1MB, 0)
    if ($mb -lt ($IsoMB * 0.95)) {
        Write-Bad "Downloaded only $mb MB — incomplete. Re-run to resume."
        exit 1
    }
    Write-Ok "Downloaded $mb MB"
    exit 0
}

# ── find the ISO ────────────────────────────────────────────────────────────
if (-not $Iso) {
    Write-Head 'Looking for the Ubuntu ISO'
    $roots = @(
        (Join-Path $env:USERPROFILE 'Downloads'),
        (Join-Path $env:USERPROFILE 'Desktop'),
        (Join-Path $env:USERPROFILE 'Documents'),
        'D:\', 'E:\', 'F:\'
    )
    $found = $null
    foreach ($r in $roots) {
        if (-not (Test-Path $r)) { continue }
        $hit = Get-ChildItem -Path $r -Filter '*.iso' -File -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -match 'ubuntu|debian|server' } |
               Sort-Object Length -Descending | Select-Object -First 1
        if ($hit) { $found = $hit; break }
    }
    if (-not $found) {
        Write-Warn2 "No Ubuntu ISO found."
        Write-Host ""
        Write-Host "  Download it first:" -ForegroundColor White
        Write-Host "      .\deploy\make-usb.ps1 -Download" -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }
    $Iso = $found.FullName
}
if (-not (Test-Path $Iso)) { Write-Bad "ISO not found: $Iso"; exit 1 }
$isoItem = Get-Item $Iso
Write-Ok ("ISO: {0}  ({1} MB)" -f $isoItem.Name, [math]::Round($isoItem.Length / 1MB, 0))

# ── pick and validate the target disk ───────────────────────────────────────
if ($DiskNumber -lt 0) {
    if (-not (Show-Disks)) { exit 1 }
    Write-Host "  Re-run with the disk number, e.g.:" -ForegroundColor White
    Write-Host "      .\deploy\make-usb.ps1 -DiskNumber 2" -ForegroundColor Yellow
    Write-Host ""
    exit 0
}

$disk = Get-CimInstance Win32_DiskDrive | Where-Object Index -eq $DiskNumber
if (-not $disk) { Write-Bad "No disk with number $DiskNumber"; exit 1 }

Write-Head "Target disk"
Write-Host ("   Disk {0}: {1}  {2} GB  [{3}]" -f
    $disk.Index, $disk.Model, [math]::Round($disk.Size/1GB,1), $disk.InterfaceType) -ForegroundColor White

# --- refuse to touch anything that is not obviously removable ---------------
if ($disk.InterfaceType -ne 'USB' -and $disk.MediaType -notlike '*Removable*') {
    Write-Host ""
    Write-Bad "Disk $DiskNumber is NOT a USB/removable disk."
    Write-Host "  Refusing to continue - this looks like an internal drive." -ForegroundColor Red
    Write-Host "  InterfaceType=$($disk.InterfaceType)  MediaType=$($disk.MediaType)" -ForegroundColor DarkGray
    exit 1
}
if ($disk.Size -lt 5.5GB) {
    Write-Bad "Only $([math]::Round($disk.Size/1GB,1)) GB. Ubuntu needs ~6 GB."
    exit 1
}

# --- SHOW WHAT IS ABOUT TO BE DESTROYED ------------------------------------
Write-Head "Files currently on that disk (these will be ERASED)"
$existing = Get-Partition -DiskNumber $DiskNumber -ErrorAction SilentlyContinue |
            Get-Volume -ErrorAction SilentlyContinue |
            Where-Object DriveLetter
$anyFiles = $false
foreach ($v in $existing) {
    $p = "$($v.DriveLetter):\"
    Write-Host "  $p  ($($v.FileSystemType), $([math]::Round($v.Size/1GB,1)) GB)" -ForegroundColor White
    $top = Get-ChildItem $p -Force -ErrorAction SilentlyContinue |
           Select-Object -First 12
    foreach ($f in $top) {
        $anyFiles = $true
        $tag = if ($f.PSIsContainer) { '[dir] ' } else { '      ' }
        Write-Host "      $tag$($f.Name)" -ForegroundColor DarkGray
    }
    $count = (Get-ChildItem $p -Force -ErrorAction SilentlyContinue |
              Measure-Object -ErrorAction SilentlyContinue).Count
    if ($count -gt 12) { Write-Host "      ... and $($count - 12) more items" -ForegroundColor DarkGray }
}
if (-not $anyFiles) {
    Write-Host "  (disk appears empty, or Windows cannot read it)" -ForegroundColor DarkGray
}

# ── confirmation ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ┌──────────────────────────────────────────────────────────────┐" -ForegroundColor Red
Write-Host "  │  EVERYTHING ON DISK $DiskNumber WILL BE PERMANENTLY ERASED.        │" -ForegroundColor Red
Write-Host "  └──────────────────────────────────────────────────────────────┘" -ForegroundColor Red
if (-not $Force) {
    $answer = Read-Host "  Type the disk number ($DiskNumber) to confirm, or anything else to abort"
    if ($answer.Trim() -ne "$DiskNumber") {
        Write-Host "`n  Aborted. Nothing was changed.`n" -ForegroundColor Yellow
        exit 1
    }
}

# ── partition and format ────────────────────────────────────────────────────
Write-Head 'Partitioning and formatting (FAT32 — required for UEFI boot)'
$script = @"
select disk $DiskNumber
clean
convert mbr
create partition primary
select partition 1
active
format fs=fat32 quick label=UBUNTU
assign
"@
$tmp = Join-Path $env:TEMP "heziis-diskpart-$PID.txt"
$script | Set-Content -Path $tmp -Encoding ASCII
try {
    $out = & diskpart.exe /s $tmp 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Bad "diskpart failed:"
        Write-Host $out -ForegroundColor DarkGray
        exit 1
    }
    Write-Ok "diskpart completed"
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

# find the fresh drive letter
Start-Sleep -Seconds 1
$vol = Get-Partition -DiskNumber $DiskNumber -ErrorAction SilentlyContinue |
       Get-Volume -ErrorAction SilentlyContinue | Where-Object DriveLetter |
       Select-Object -First 1
if (-not $vol) { Write-Bad "Could not find the new drive letter"; exit 1 }
$target = "$($vol.DriveLetter):"
Write-Ok "Ready at $target"

# ── copy the ISO contents ───────────────────────────────────────────────────
Write-Head "Copying Ubuntu onto the stick (this takes a few minutes)"
$mounted = $null
try {
    $mounted = Mount-DiskImage -ImagePath $Iso -PassThru -ErrorAction Stop
    $isoVol = $mounted | Get-Volume
    $src = "$($isoVol.DriveLetter):\"
    Write-Ok "Mounted the ISO at $src"

    # robocopy exit codes: 0-7 are success, 8+ are real failures
    $null = & robocopy.exe $src $target /E /NFL /NDL /NJH /NJS /R:2 /W:2
    $rc = $LASTEXITCODE
    if ($rc -ge 8) {
        Write-Bad "robocopy failed (exit $rc)"
        exit 1
    }
    Write-Ok "Files copied (robocopy exit $rc)"
} finally {
    if ($mounted) { Dismount-DiskImage -ImagePath $Iso -ErrorAction SilentlyContinue | Out-Null }
}

# ── verify ──────────────────────────────────────────────────────────────────
Write-Head 'Verifying the stick'
$fail = 0
$efi = Join-Path $target 'EFI\BOOT\BOOTX64.EFI'
if (Test-Path $efi) { Write-Ok 'EFI\BOOT\BOOTX64.EFI present (UEFI bootable)' }
else { Write-Bad 'EFI\BOOT\BOOTX64.EFI MISSING — this will not boot'; $fail++ }

foreach ($p in @('casper', 'boot', 'install')) {
    if (Test-Path (Join-Path $target $p)) { Write-Ok "$p\ present" }
    else { Write-Warn2 "$p\ missing" }
}
$du = (Get-ChildItem $target -Recurse -File -Force -ErrorAction SilentlyContinue |
       Measure-Object Length -Sum).Sum
$duGB = [math]::Round($du / 1GB, 2)
Write-Host "  used: $duGB GB of $([math]::Round($vol.Size/1GB,1)) GB" -ForegroundColor DarkGray
if ($duGB -lt 1.5) { Write-Bad "Only $duGB GB copied — clearly incomplete"; $fail++ }

Write-Host ""
if ($fail -eq 0) {
    # ---- also drop a copy of the billing app on the stick -------------------
    # Saves cloning it again on the Linux side (which may not have git yet).
    Write-Head 'Bundling the billing app onto the stick'
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    $stamp = Get-Date -Format 'yyyyMMdd'
    $zip = Join-Path $env:TEMP "heziis-wifi-$stamp.zip"
    try {
        $stage = Join-Path $env:TEMP "heziis-stage-$PID"
        Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        # deliberately excluded: .git, venv, data, output, caches, logs
        $excl = @('.git', '.venv', 'venv', '__pycache__', 'data', 'output', 'node_modules')
        $null = & robocopy.exe $repoRoot (Join-Path $stage 'heziis-wifi') /E /NFL /NDL /NJH /NJS /R:1 /W:1 `
                                /XD $excl /XF '*.pyc' '*.log' '*.sqlite3' '*.sqlite3-wal' '*.sqlite3-shm'
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit $LASTEXITCODE" }
        Compress-Archive -Path (Join-Path $stage 'heziis-wifi') -DestinationPath $zip -Force
        Copy-Item $zip -Destination $target -Force
        $zmb = [math]::Round((Get-Item $zip).Length / 1MB, 2)
        Write-Ok "heziis-wifi.zip copied to the stick ($zmb MB)"
        Write-Warn2 "That zip contains config.json — your Daraja keys. It is your own"
        Write-Warn2 "stick, but do not hand it to anyone else."
    } catch {
        Write-Warn2 "Could not bundle the app: $($_.Exception.Message)"
        Write-Warn2 "You can still clone it on the Linux side instead."
    } finally {
        Remove-Item (Join-Path $env:TEMP "heziis-stage-$PID") -Recurse -Force -ErrorAction SilentlyContinue
    }

    Write-Host ""
    Write-Host "  USB stick is ready." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Next, on the laptop:" -ForegroundColor White
    Write-Host "    1. Plug the stick in and SHUT DOWN fully (not restart)." -ForegroundColor White
    Write-Host "    2. Power on and tap F12 (or F2/Del) for the boot menu." -ForegroundColor White
    Write-Host "    3. Pick the entry with 'UEFI' in front of it." -ForegroundColor White
    Write-Host "    4. Choose 'Try or Install Ubuntu Server'." -ForegroundColor White
    Write-Host "    5. If it offers to erase the disk, STOP and read DEPLOY.md first." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Then follow DEPLOY.md, or the short version:" -ForegroundColor White
    Write-Host "      unzip heziis-wifi.zip && cd heziis-wifi" -ForegroundColor Yellow
    Write-Host "      sudo ./deploy/setup-hotspot.sh" -ForegroundColor Yellow
    Write-Host "      sudo ./deploy/verify-hotspot.sh" -ForegroundColor Yellow
} else {
    Write-Bad "The stick is NOT ready ($fail problem(s) above). Do not boot from it yet."
}
Write-Host ""

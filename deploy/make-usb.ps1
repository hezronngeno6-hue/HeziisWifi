<#
================================================================================
 HEZIIS NET - make a bootable Ubuntu USB stick
================================================================================
 Turns a USB stick into an Ubuntu installer using only what ships with Windows
 (diskpart + raw write). No Rufus, no Etcher, no extra downloads.


  !!!  THIS ERASES THE WHOLE STICK. EVERYTHING ON IT IS GONE.  !!!

 The script prints what is currently on the stick and makes you type the disk
 number back before it writes anything. Read that list.


 RUN IN AN ADMINISTRATOR POWERSHELL. It does not try to elevate itself, because
 a UAC prompt is the last thing you should click through on autopilot for a
 command that erases a disk.

     .\deploy\make-usb.ps1 -List                 see which stick is which
     .\deploy\make-usb.ps1 -Download             fetch the Ubuntu ISO first
     .\deploy\make-usb.ps1 -DiskNumber 2         write the stick

 WHY RAW WRITE AND NOT JUST COPYING THE FILES
 ---------------------------------------------
 Copying an ISO's files onto a FAT32 partition only produces a stick that UEFI
 can boot. A LEGACY BIOS machine cannot: it needs real boot code in the first
 sectors of the disk, which a file copy never writes. You would get
 "no bootable device" and no clue why.

 Ubuntu's ISO is "isohybrid" - it carries BOTH a BIOS boot sector and a UEFI
 bootloader. Writing it byte-for-byte to the stick reproduces both, so the stick
 boots either way. That is exactly what Rufus' "DD mode" does.

 Use -Mode Files only if you specifically need UEFI-only AND want spare space
 on the stick for other files.
================================================================================
#>

[CmdletBinding()]
param(
    # Disk number of the USB stick, from -List
    [int]$DiskNumber = -1,

    # Show removable disks and exit
    [switch]$List,

    # Download the Ubuntu Server ISO and exit
    [switch]$Download,

    # Path to the ISO (auto-detected if omitted)
    [string]$Iso,

    # dd    = raw write, boots legacy BIOS AND UEFI (recommended)
    # files = copy files to FAT32, UEFI only, leaves spare space
    [ValidateSet('dd', 'files')]
    [string]$Mode = 'dd',

    # Skip the confirmation prompt (scripted use only)
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$IsoUrl  = 'https://releases.ubuntu.com/24.04/ubuntu-24.04.3-live-server-amd64.iso'
$IsoName = 'ubuntu-24.04.3-live-server-amd64.iso'
$IsoMB   = 3150

function Write-Head($t) { Write-Host ""; Write-Host $t -ForegroundColor Cyan; Write-Host ('-' * 74) }
function Write-Ok($t)   { Write-Host "  OK    $t" -ForegroundColor Green }
function Write-Bad($t)  { Write-Host "  FAIL  $t" -ForegroundColor Red }
function Write-Warn2($t){ Write-Host "  WARN  $t" -ForegroundColor Yellow }
function Write-Info($t) { Write-Host "  $t" }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-UsbDisks {
    Get-CimInstance Win32_DiskDrive | Where-Object {
        $_.InterfaceType -eq 'USB' -or $_.MediaType -like '*Removable*'
    }
}

function Get-DiskVolumes([int]$n) {
    # Traverse the CIM associations disk -> partition -> logical disk.
    #
    # Do NOT try to string-match the Antecedent/Dependent paths: WMI formats them
    # as  Win32_DiskPartition (DeviceID = "Disk #0, Partition #1")  with SPACES
    # around the '=', so splitting on 'DeviceID=' silently matches nothing and
    # the "here is what will be erased" list comes up empty. That gives false
    # confidence right before a destructive write, which is the worst possible
    # failure mode for this script.
    $out = @()
    $disk = Get-CimInstance Win32_DiskDrive -Filter "Index=$n" -ErrorAction SilentlyContinue
    if (-not $disk) { return $out }
    foreach ($part in @(Get-CimAssociatedInstance -InputObject $disk `
                        -Association Win32_DiskDriveToDiskPartition -ErrorAction SilentlyContinue)) {
        foreach ($ld in @(Get-CimAssociatedInstance -InputObject $part `
                          -Association Win32_LogicalDiskToPartition -ErrorAction SilentlyContinue)) {
            if ($ld.DeviceID) { $out += $ld }
        }
    }
    return $out
}

function Show-VolumeContents($vol) {
    # Print what is on a volume so the operator can see what they are about to lose.
    $p = "$($vol.DeviceID)\"
    $usedGB = [math]::Round(($vol.Size - $vol.FreeSpace) / 1GB, 2)
    Write-Host ("  {0}  {1}, {2} GB used of {3} GB" -f `
        $p, $vol.FileSystem, $usedGB, [math]::Round($vol.Size / 1GB, 2)) -ForegroundColor White
    if ($usedGB -le 0) { Write-Host "      (empty)" -ForegroundColor DarkGray; return }
    $top = @(Get-ChildItem $p -Force -ErrorAction SilentlyContinue | Select-Object -First 15)
    foreach ($f in $top) {
        $tag = if ($f.PSIsContainer) { '[dir]  ' } else { '       ' }
        Write-Host ("      {0}{1}" -f $tag, $f.Name) -ForegroundColor DarkGray
    }
    $count = (Get-ChildItem $p -Force -ErrorAction SilentlyContinue | Measure-Object).Count
    if ($count -gt 15) { Write-Host "      ... and $($count - 15) more items" -ForegroundColor DarkGray }
}

Write-Host ""
Write-Host "HEZIIS NET - bootable Ubuntu USB" -ForegroundColor Cyan
if ($Mode -eq 'dd') { Write-Host "mode: dd  (raw write - boots legacy BIOS AND UEFI)" }
else                { Write-Host "mode: files  (file copy - UEFI only)" }

# --- elevation ---------------------------------------------------------------
# -List and -Download are read-only, so they work in a normal prompt.
if (-not (Test-Admin) -and -not $List -and -not $Download) {
    Write-Host ""
    Write-Bad "Run this in an ADMINISTRATOR PowerShell."
    Write-Host ""
    Write-Info "DOUBLE-CLICK the launcher in the same folder instead:"
    Write-Host "      RUN-AS-ADMIN.cmd make-usb.ps1" -ForegroundColor Yellow
    Write-Host ""
    Write-Info "Click YES on the Windows permission prompt. Right-clicking a .ps1 and"
    Write-Info "choosing 'Run with PowerShell' does NOT elevate."
    Write-Host ""
    exit 1
}

# --- list --------------------------------------------------------------------
function Show-Disks {
    Write-Head 'Removable (USB) disks'
    $usb = @(Get-UsbDisks)
    if ($usb.Count -eq 0) {
        Write-Warn2 'No USB stick detected.'
        Write-Host ""
        Write-Info "Plug one in and run this again. The ISO is $IsoMB MB, so a 4 GB"
        Write-Info "stick is enough (8 GB is comfortable)."
        Write-Host ""
        return $false
    }
    Write-Host ""
    foreach ($d in $usb) {
        $gb = [math]::Round($d.Size / 1GB, 1)
        Write-Host ("   Disk {0}   {1,-32} {2,6} GB" -f $d.Index, $d.Model, $gb) -ForegroundColor White
        if ($gb -lt 3.6) { Write-Host "             ^ too small for the 3.15 GB ISO" -ForegroundColor Red }
        foreach ($v in (Get-DiskVolumes $d.Index)) {
            $used = [math]::Round(($v.Size - $v.FreeSpace) / 1GB, 2)
            $free = [math]::Round($v.FreeSpace / 1GB, 2)
            $mark = if ($used -gt 0.05) { '   <-- HAS DATA' } else { '' }
            Write-Host ("             {0}  {1}   {2} GB used, {3} GB free{4}" -f `
                $v.DeviceID, $v.FileSystem, $used, $free, $mark) -ForegroundColor DarkGray
        }
    }
    Write-Host ""
    Write-Host "  Everything on the chosen disk will be DESTROYED." -ForegroundColor Yellow
    return $true
}

if ($List) { Show-Disks | Out-Null; exit 0 }

# --- download ----------------------------------------------------------------
if ($Download) {
    $dest = Join-Path $env:USERPROFILE "Downloads\$IsoName"
    Write-Head "Downloading Ubuntu Server 24.04 LTS (~$IsoMB MB)"
    Write-Info "-> $dest"
    Write-Host "  Big download on a mobile connection. Ctrl+C to stop." -ForegroundColor Yellow
    Write-Host ""
    & curl.exe -L --fail --show-error -o $dest $IsoUrl
    if (-not (Test-Path $dest)) { Write-Bad "Download failed."; exit 1 }
    $mb = [math]::Round((Get-Item $dest).Length / 1MB, 0)
    if ($mb -lt ($IsoMB * 0.95)) {
        Write-Bad "Only $mb MB - incomplete. Re-run to continue."
        exit 1
    }
    Write-Ok "Downloaded $mb MB"
    exit 0
}

# --- locate the ISO ----------------------------------------------------------
if (-not $Iso) {
    Write-Head 'Looking for the Ubuntu ISO'
    $found = $null
    foreach ($r in @((Join-Path $env:USERPROFILE 'Downloads'),
                     (Join-Path $env:USERPROFILE 'Desktop'),
                     (Join-Path $env:USERPROFILE 'Documents'))) {
        if (-not (Test-Path $r)) { continue }
        $hit = Get-ChildItem -Path $r -Filter '*.iso' -File -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -match 'ubuntu' } |
               Sort-Object Length -Descending | Select-Object -First 1
        if ($hit) { $found = $hit; break }
    }
    if (-not $found) {
        Write-Warn2 'No Ubuntu ISO found.'
        Write-Host ""
        Write-Host "      .\deploy\make-usb.ps1 -Download" -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }
    $Iso = $found.FullName
}
if (-not (Test-Path $Iso)) { Write-Bad "ISO not found: $Iso"; exit 1 }
$isoItem = Get-Item $Iso
Write-Ok ("ISO: {0}  ({1} MB)" -f $isoItem.Name, [math]::Round($isoItem.Length / 1MB, 0))

# --- pick the disk -----------------------------------------------------------
if ($DiskNumber -lt 0) {
    if (-not (Show-Disks)) { exit 1 }
    Write-Host "  Re-run with the disk number, e.g.:" -ForegroundColor White
    Write-Host "      .\deploy\make-usb.ps1 -DiskNumber 2" -ForegroundColor Yellow
    Write-Host ""
    exit 0
}

$disk = Get-CimInstance Win32_DiskDrive | Where-Object Index -eq $DiskNumber
if (-not $disk) { Write-Bad "No disk with number $DiskNumber"; exit 1 }

Write-Head 'Target disk'
Write-Host ("   Disk {0}: {1}   {2} GB   [{3}]" -f `
    $disk.Index, $disk.Model, [math]::Round($disk.Size / 1GB, 1), $disk.InterfaceType) -ForegroundColor White

if ($disk.InterfaceType -ne 'USB' -and $disk.MediaType -notlike '*Removable*') {
    Write-Host ""
    Write-Bad "Disk $DiskNumber is NOT a USB/removable disk."
    Write-Host "  Refusing to continue - this looks like an internal drive." -ForegroundColor Red
    Write-Info "InterfaceType=$($disk.InterfaceType)  MediaType=$($disk.MediaType)"
    exit 1
}

$needBytes = $isoItem.Length
if ($disk.Size -lt $needBytes) {
    Write-Bad ("Stick is {0} GB but the ISO is {1} GB - too small." -f `
        [math]::Round($disk.Size / 1GB, 1), [math]::Round($needBytes / 1GB, 2))
    exit 1
}
$spareMB = [math]::Round(($disk.Size - $isoItem.Length) / 1MB, 0)
Write-Ok "Fits: $spareMB MB left over after the ISO"

# --- SHOW WHAT WILL BE DESTROYED ---------------------------------------------
Write-Head 'Files on that disk right now - THESE WILL BE ERASED'
$vols = @(Get-DiskVolumes $DiskNumber)
if ($vols.Count -eq 0) {
    Write-Info "No readable volume found on disk $DiskNumber."
    Write-Host "  It may be unformatted, or its filesystem may be damaged." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  That does NOT mean the disk is empty. It could hold data Windows" -ForegroundColor Yellow
    Write-Host "  cannot currently read. Be certain before continuing." -ForegroundColor Yellow
} else {
    $totalUsed = 0
    foreach ($v in $vols) {
        Show-VolumeContents $v
        $totalUsed += ($v.Size - $v.FreeSpace)
    }
    Write-Host ""
    if ($totalUsed -gt 0) {
        Write-Host ("  *** THIS STICK HOLDS {0} GB OF DATA THAT WILL BE DESTROYED ***" -f `
            [math]::Round($totalUsed / 1GB, 2)) -ForegroundColor Red
        Write-Host "  If any of it matters, press Ctrl+C NOW and copy it off first." -ForegroundColor Red
    } else {
        Write-Ok 'The stick looks empty.'
    }
}

# --- confirm -----------------------------------------------------------------
Write-Host ""
Write-Host "  +--------------------------------------------------------------+" -ForegroundColor Red
Write-Host "  |  EVERYTHING ON DISK $DiskNumber WILL BE PERMANENTLY ERASED.      |" -ForegroundColor Red
Write-Host "  +--------------------------------------------------------------+" -ForegroundColor Red
Write-Host ""
Write-Info "Close any Explorer window showing this stick first."
if (-not $Force) {
    $answer = Read-Host "  Type the disk number ($DiskNumber) to confirm, or anything else to abort"
    if ($answer.Trim() -ne "$DiskNumber") {
        Write-Host ""
        Write-Host "  Aborted. Nothing was changed." -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }
}

# --- clear the partition table ------------------------------------------------
Write-Head 'Clearing the partition table'
$dp = "select disk $DiskNumber`r`nclean`r`n"
$tmp = Join-Path $env:TEMP "heziis-diskpart-$PID.txt"
$dp | Set-Content -Path $tmp -Encoding ASCII
try {
    $out = & diskpart.exe /s $tmp 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Bad "diskpart failed:"
        Write-Host $out -ForegroundColor DarkGray
        exit 1
    }
    Write-Ok "disk cleaned"
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2

# --- write -------------------------------------------------------------------
if ($Mode -eq 'dd') {

    Write-Head 'Writing the ISO byte-for-byte (takes several minutes)'
    $drivePath = "\\.\PhysicalDrive$DiskNumber"
    $isoLen = $isoItem.Length
    Write-Info "from: $Iso"
    Write-Info "  to: $drivePath   ($([math]::Round($isoLen / 1MB, 0)) MB)"
    Write-Host ""

    $src = $null; $dst = $null
    try {
        $src = [System.IO.File]::OpenRead($Iso)
        $dst = New-Object System.IO.FileStream(
            $drivePath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None)

        $buf = New-Object byte[] (4MB)
        $written = 0L
        $lastPct = -1
        while (($read = $src.Read($buf, 0, $buf.Length)) -gt 0) {
            $writeLen = $read
            # raw device writes must be whole sectors; pad the tail with zeros
            if ($writeLen % 512 -ne 0) {
                $writeLen = [int]([math]::Ceiling($writeLen / 512) * 512)
                for ($i = $read; $i -lt $writeLen; $i++) { $buf[$i] = 0 }
            }
            $dst.Write($buf, 0, $writeLen)
            $written += $writeLen
            $pct = [int](($written * 100) / $isoLen)
            if ($pct -ne $lastPct -and ($pct % 20 -eq 0)) {
                $lastPct = $pct
                Write-Host ("    {0,3}%   {1} MB" -f $pct, [math]::Round($written / 1MB, 0))
            }
        }
        $dst.Flush($true)
        Write-Ok ("Wrote {0} MB" -f [math]::Round($written / 1MB, 0))
    } catch {
        Write-Bad "Raw write failed: $($_.Exception.Message)"
        Write-Info "Close any Explorer window or antivirus holding the stick, then"
        Write-Info "unplug it, plug it back in and try again."
        exit 1
    } finally {
        if ($dst) { $dst.Dispose() }
        if ($src) { $src.Dispose() }
    }

    Write-Head 'Verifying the boot code landed'
    Start-Sleep -Seconds 2
    try {
        $rb = New-Object System.IO.FileStream($drivePath, [System.IO.FileMode]::Open,
                                              [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $check = New-Object byte[] (1MB)
        $null = $rb.Read($check, 0, $check.Length)
        $rb.Dispose()

        $isoHead = New-Object byte[] (1MB)
        $fs = [System.IO.File]::OpenRead($Iso)
        $null = $fs.Read($isoHead, 0, $isoHead.Length)
        $fs.Dispose()

        $same = $true
        for ($i = 0; $i -lt $check.Length; $i++) {
            if ($check[$i] -ne $isoHead[$i]) { $same = $false; break }
        }
        if ($same) {
            Write-Ok 'First 1 MB matches the ISO - boot code is in place'
            Write-Ok 'This stick boots legacy BIOS and UEFI'
        } else {
            Write-Bad 'First 1 MB does NOT match the ISO'
            Write-Info 'The stick will probably not boot. Re-run the write.'
            exit 1
        }
    } catch {
        Write-Warn2 "Could not read back to verify: $($_.Exception.Message)"
    }

    Write-Host ""
    Write-Host "  Note: a raw-written stick holds ONLY the installer, so there is no" -ForegroundColor DarkGray
    Write-Host "  room for extra files on it. Get the billing app in Ubuntu by" -ForegroundColor DarkGray
    Write-Host "  mounting the Windows partition (NTFS is readable) or git clone." -ForegroundColor DarkGray

} else {

    Write-Head 'Partitioning as FAT32 and copying files (UEFI-only)'
    $dp2 = "select disk $DiskNumber`r`ncreate partition primary`r`nselect partition 1`r`nactive`r`nformat fs=fat32 quick label=UBUNTU`r`nassign`r`n"
    $tmp2 = Join-Path $env:TEMP "heziis-diskpart2-$PID.txt"
    $dp2 | Set-Content -Path $tmp2 -Encoding ASCII
    try {
        $out = & diskpart.exe /s $tmp2 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) {
            Write-Bad "diskpart failed:"; Write-Host $out -ForegroundColor DarkGray
            exit 1
        }
    } finally {
        Remove-Item $tmp2 -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    # find a drive letter that has no volume on disk 0
    $target = $null
    foreach ($cand in 'E:\', 'F:\', 'G:\', 'H:\', 'I:\') {
        if (Test-Path $cand) {
            $ln = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($cand.Substring(0,2))'"
            if ($ln -and $ln.FileSystem -eq 'FAT32' -and $ln.VolumeName -eq 'UBUNTU') { $target = $cand; break }
        }
    }
    if (-not $target) {
        $vols2 = @(Get-DiskVolumes $DiskNumber)
        if ($vols2.Count -gt 0) { $target = "$($vols2[0].DeviceID)\" }
    }
    if (-not $target) { Write-Bad "Could not find the new drive letter. Check Explorer."; exit 1 }
    Write-Ok "Ready at $target"

    $mounted = $null
    try {
        $mounted = Mount-DiskImage -ImagePath $Iso -PassThru -ErrorAction Stop
        $isoVol = $mounted | Get-Volume
        $srcPath = "$($isoVol.DriveLetter):\"
        Write-Ok "Mounted the ISO at $srcPath"
        $null = & robocopy.exe $srcPath $target /E /NFL /NDL /NJH /NJS /R:2 /W:2
        if ($LASTEXITCODE -ge 8) { Write-Bad "robocopy failed ($LASTEXITCODE)"; exit 1 }
        Write-Ok "Files copied"
    } finally {
        if ($mounted) { Dismount-DiskImage -ImagePath $Iso -ErrorAction SilentlyContinue | Out-Null }
    }

    Write-Head 'Verifying (UEFI only)'
    $efi = Join-Path $target 'EFI\BOOT\BOOTX64.EFI'
    if (Test-Path $efi) {
        Write-Ok 'EFI\BOOT\BOOTX64.EFI present'
        Write-Warn2 'This stick boots UEFI only. A legacy BIOS machine will NOT boot it.'
    } else {
        Write-Bad 'EFI\BOOT\BOOTX64.EFI missing - this will not boot'
        exit 1
    }
}

# --- done --------------------------------------------------------------------
Write-Head 'Stick is ready'
Write-Host ""
Write-Host "  Next, on the laptop:" -ForegroundColor White
Write-Host "   1. Shut down FULLY (not Restart) - Fast Startup skips the boot menu." -ForegroundColor White
Write-Host "   2. Power on and tap F12 (or F2/Del) until the boot menu appears." -ForegroundColor White
Write-Host "   3. This machine is LEGACY BIOS, so pick the entry WITHOUT 'UEFI:'." -ForegroundColor Yellow
Write-Host "   4. Choose 'Try or Install Ubuntu Server'." -ForegroundColor White
Write-Host "   5. Shrink C: in Windows Disk Management FIRST - see DEPLOY.md 3a." -ForegroundColor Yellow
Write-Host ""

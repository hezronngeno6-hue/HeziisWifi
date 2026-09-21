# =============================================================================
#  HEZIIS NET - dual-boot readiness and data-safety check
# =============================================================================
#  Answers two questions before you touch the disk:
#    1. Is this machine ready to install Ubuntu alongside Windows?
#    2. What data is at risk, and how bad would losing it be?
#
#  Read-only. It changes nothing and needs no administrator rights.
#
#      .\tools\dual-boot-check.ps1
# =============================================================================

$ErrorActionPreference = 'Continue'

function Head($t) { Write-Host ""; Write-Host $t -ForegroundColor Cyan; Write-Host ('-' * 74) }
function Ok($t)   { Write-Host "  OK    $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  WARN  $t" -ForegroundColor Yellow }
function Bad($t)  { Write-Host "  RISK  $t" -ForegroundColor Red }
function Info($t) { Write-Host "  $t" }

$blockers = 0
$warnings = 0

Write-Host ""
Write-Host "HEZIIS NET - dual-boot readiness check" -ForegroundColor Cyan
Write-Host "host: $env:COMPUTERNAME   user: $env:USERNAME"

# --- 1. installer image ------------------------------------------------------
Head '1. Ubuntu installer'
$iso = Join-Path $env:USERPROFILE 'Downloads\ubuntu-24.04.3-live-server-amd64.iso'
$isoBytes = 0
if (Test-Path $iso) {
    $isoBytes = (Get-Item $iso).Length
    $mb = [math]::Round($isoBytes / 1MB, 0)
    Info "found: $iso"
    if ($mb -ge 2990) { Ok "download looks complete ($mb MB of ~3150)" }
    else { Warn "only $mb MB - still downloading, or incomplete" }
} else {
    Bad "ISO not found at $iso"
    Info "fetch it with: .\deploy\make-usb.ps1 -Download"
    $blockers++
}

# --- 2. free space -----------------------------------------------------------
Head '2. Disk space'
$ld = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
if ($ld) {
    $totalGB = [math]::Round($ld.Size / 1GB, 1)
    $freeGB  = [math]::Round($ld.FreeSpace / 1GB, 1)
    $info = Get-CimInstance Win32_DiskDrive |
            Where-Object { $_.Index -eq 0 } |
            Select-Object -First 1
    Info ("disk 0: {0}  {1} GB" -f $info.Model, [math]::Round($info.Size / 1GB, 1))
    Info "C: total $totalGB GB, free $freeGB GB"

    if ($freeGB -ge 45) { Ok "plenty free - Ubuntu needs about 25 GB" }
    elseif ($freeGB -ge 25) { Warn "$freeGB GB free - enough, but tight" ; $warnings++ }
    else { Bad "only $freeGB GB free - not enough for Ubuntu (needs ~25 GB)" ; $blockers++ }

    Info ""
    Info "Windows can only shrink while the space past the end of the partition"
    Info "is free of unmovable files, so you may be able to reclaim less than"
    Info "the $freeGB GB shown. The authoritative number comes from"
    Info "Disk Management (diskmgmt.msc -> right-click C: -> Shrink Volume):"
    Info "the 'Size of available shrink space' box. That number is your limit."
}

# --- 3. partition layout -----------------------------------------------------
Head '3. Partition layout'
try {
    $parts = Get-CimInstance Win32_DiskPartition | Sort-Object DiskIndex, StartingOffset
    foreach ($p in $parts) {
        Info ("disk {0}  {1,-22} {2,8} GB" -f $p.DiskIndex, $p.Type, [math]::Round($p.Size / 1GB, 2))
    }
    $gpt = ($parts | Where-Object { $_.Type -like 'GPT*' }).Count
    $mbr = ($parts | Where-Object { $_.Type -like 'Installable File System*' -or $_.Type -eq 'IFS' }).Count
    if ($gpt -gt 0) { Ok 'GPT partitioned (normal for UEFI)' }
    else { Info 'no GPT partitions detected' }
} catch {
    Warn "could not read the partition table: $($_.Exception.Message)"
}

# --- 4. firmware -------------------------------------------------------------
Head '4. Firmware mode'
$pe = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control' `
       -Name PEFirmwareType -ErrorAction SilentlyContinue).PEFirmwareType
switch ($pe) {
    1 { Warn 'LEGACY BIOS - in the boot menu pick the entry WITHOUT "UEFI:"' ; $warnings++ }
    2 { Ok 'UEFI - pick the boot entry that STARTS with "UEFI:"' }
    default { Warn 'could not determine firmware type' ; $warnings++ }
}
if (Test-Path 'C:\Windows\Boot\EFI\bootmgfw.efi') { Info 'Windows EFI boot manager present' }

# --- 5. Fast Startup ---------------------------------------------------------
Head '5. Fast Startup'
$hb = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
       -Name HiberbootEnabled -ErrorAction SilentlyContinue).HiberbootEnabled
switch ($hb) {
    1 {
        Warn 'Fast Startup is ON - Windows never fully shuts down, which'
        Info '  can stop the USB boot menu appearing and leave NTFS dirty.'
        Info '  Turn it off: Control Panel > Power Options > Choose what the'
        Info '  power buttons do > Change settings that are currently'
        Info '  unavailable > untick "Turn on fast startup".'
        $warnings++
    }
    0 { Ok 'Fast Startup is OFF (good)' }
    default { Warn 'could not read HiberbootEnabled' ; $warnings++ }
}

# --- 6. encryption -----------------------------------------------------------
Head '6. Drive encryption'
$encFound = $false
try {
    $ev = Get-CimInstance -Namespace 'root\cimv2\security\microsoftvolumeencryption' `
          -ClassName Win32_EncryptableVolume -ErrorAction Stop
    foreach ($v in $ev) {
        $encFound = $true
        Info "$($v.DriveLetter) protection status = $($v.ProtectionStatus)  (0 = off)"
        if ($v.ProtectionStatus -ne 0) {
            Bad "BitLocker is ON for $($v.DriveLetter)"
            Info '  Resizing an encrypted partition can lock you out of your'
            Info '  own data. Suspend or turn BitLocker off BEFORE shrinking,'
            Info '  and make sure you have the recovery key written down.'
            $blockers++
        }
    }
    if (-not $encFound) { Ok 'no encrypted volumes reported' }
} catch {
    Info "BitLocker WMI not available ($($_.Exception.Message))"
    Info 'run "manage-bde -status C:" in an admin prompt to be certain'
}

# --- 7. what is actually at risk --------------------------------------------
Head '7. Your data - this is what could be lost'
$folders = @('Downloads', 'Documents', 'Desktop', 'Pictures', 'Videos', 'Music', 'OneDrive')
$totalData = 0
$rows = @()
foreach ($f in $folders) {
    $p = Join-Path $env:USERPROFILE $f
    if (Test-Path $p) {
        $sum = (Get-ChildItem $p -Recurse -File -Force -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum
        if (-not $sum) { $sum = 0 }
        $totalData += $sum
        $rows += [pscustomobject]@{ Folder = $f; GB = [math]::Round($sum / 1GB, 2) }
    }
}
$rows | Sort-Object GB -Descending | ForEach-Object {
    Info ("{0,-12} {1,8} GB" -f $_.Folder, $_.GB)
}
Info ('-' * 34)
Info ("{0,-12} {1,8} GB" -f 'TOTAL', [math]::Round($totalData / 1GB, 2))

# the important bit: is there anywhere to put a backup, and is it BIG ENOUGH?
Head '8. Backup target'
$targets = @(Get-CimInstance Win32_DiskDrive |
             Where-Object { $_.InterfaceType -eq 'USB' -or $_.MediaType -like '*Removable*' })
$dataGB = [math]::Round($totalData / 1GB, 2)
$usableGB = 0.0
foreach ($t in $targets) {
    $capGB = [math]::Round($t.Size / 1GB, 2)
    $usableGB += $capGB
    Info ("{0,-36} {1,7} GB" -f $t.Model, $capGB)
}
if ($targets.Count -gt 0) {
    Info ('-' * 46)
    Info ("removable capacity {0} GB   vs   your data {1} GB" -f `
          [math]::Round($usableGB, 2), $dataGB)
}
Write-Host ""

if ($targets.Count -eq 0) {
    Bad 'No external drive attached - you have nowhere to put a copy.'
    $blockers++
} elseif ($usableGB -lt $dataGB) {
    Bad ("Not enough room: {0} GB of removable storage, but {1} GB of data." -f `
         [math]::Round($usableGB, 2), $dataGB)
    Info 'Counting a small stick as "a backup exists" is how people get caught'
    Info 'out. It cannot hold a full copy.'
    $blockers++
} else {
    Ok ("Enough removable space for a full copy ({0} GB available)" -f `
        [math]::Round($usableGB, 2))
}

if ($blockers -gt 0) {
    Info ''
    Info '  Shrinking a partition does not touch your files - it only moves the'
    Info '  boundary into free space. But "normally safe" is not a backup, and'
    Info '  one wrong click in the installer has no undo.'
    Info ''
    Info '  Before you partition, do ONE of these:'
    Info '    a) copy the folders above to an external drive big enough, or'
    Info '    b) upload the important ones to cloud storage, or'
    Info '    c) run Ubuntu with "Try Ubuntu" and do not partition at all.'
}

# --- summary -----------------------------------------------------------------
Head 'Summary'
if ($blockers -eq 0 -and $warnings -eq 0) {
    Write-Host '  READY - no blockers found.' -ForegroundColor Green
} else {
    Write-Host "  $blockers blocker(s), $warnings warning(s)" -ForegroundColor Yellow
    if ($blockers -gt 0) {
        Write-Host '  Resolve the RISK lines above before touching the disk.' -ForegroundColor Red
    }
    if ($warnings -gt 0) {
        Write-Host '  The WARN lines will not stop you but will cause trouble.' -ForegroundColor Yellow
    }
}
Write-Host ''
exit ([int]($blockers -gt 0))

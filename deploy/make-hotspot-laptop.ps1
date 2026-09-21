# =============================================================================
#  HEZIIS NET - turn this laptop into the hotspot           *** AS ADMIN ***
# =============================================================================
#  One right-click does every step that needs administrator rights:
#
#    1. turns off Fast Startup and hibernation  (required for dual boot, and
#       hibernation is a real data-corruption risk if Linux mounts NTFS)
#    2. checks BitLocker and refuses to resize if it is on
#    3. writes the Ubuntu installer to your USB stick
#    4. reports how much space Windows can give Ubuntu, and optionally frees it
#
#      DOUBLE-CLICK THIS INSTEAD:   RUN-AS-ADMIN.cmd   (in the same folder)
#
#      or, from an Administrator PowerShell:
#          .\deploy\make-hotspot-laptop.ps1
#          .\deploy\make-hotspot-laptop.ps1 -FreeGB 25
#
#  DO NOT right-click this file and pick "Run with PowerShell". On Windows 10/11
#  that does NOT elevate: the script starts, finds it has no administrator
#  rights, refuses, and the window closes before you can read why. It looks
#  like a crash but it is not. Use RUN-AS-ADMIN.cmd, which asks Windows for the
#  rights properly.
#
#  WHY A SINGLE SCRIPT
#  -------------------
#  Every step here needs elevation, and each one is dangerous on its own. Doing
#  them in one place means one confirmation, one report, and no chance of the
#  USB stick being written while the disk is in a state that cannot boot.
#
#  THE DATA CHECK IS NOT OPTIONAL
#  ------------------------------
#  Before the stick is touched, this script compares its contents against
#  C:\Users\f\USB-STICK-BACKUP and REFUSES if anything is not accounted for.
#  A stick holding a 1170 MB wedding video was very nearly wiped by an earlier
#  version of this tooling because nothing checked.
# =============================================================================

[CmdletBinding()]
param(
    # Free this much space for Ubuntu. Omit to only report.
    [int]$FreeGB = 0,

    # Where the USB stick's contents were backed up.
    # NOTE: this must NOT be "$env:USERPROFILE\..\USB-STICK-BACKUP" - that
    # resolves to C:\USB-STICK-BACKUP, one level ABOVE the user profile. The
    # backup is inside it.
    [string]$BackupDir = "$env:USERPROFILE\USB-STICK-BACKUP",

    # Skip the backup verification before wiping (dangerous)
    [switch]$SkipBackupCheck,

    # Skip confirmation prompts (unattended)
    [switch]$Force,

    # Rehearse the whole flow WITHOUT administrator rights and WITHOUT writing
    # anything. Use this to see exactly what the real run would do.
    [switch]$DryRun,

    # Write a full log here. Lets a failed run be diagnosed afterwards without
    # guessing what was on screen.
    [string]$LogFile = ''
)

$ErrorActionPreference = 'Continue'
$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$IsoPath = Join-Path $env:USERPROFILE 'Downloads\ubuntu-24.04.3-live-server-amd64.iso'

# Every message is mirrored to the log file when -LogFile is set, so a run that
# fails or confuses someone can be diagnosed afterwards instead of guessed at.
function Write-Log($t) {
    if ($script:LogFile) {
        try { Add-Content -LiteralPath $script:LogFile -Value $t -Encoding UTF8 -ErrorAction Stop } catch { }
    }
}
function Head($t) {
    Write-Host ""; Write-Host $t -ForegroundColor Cyan; Write-Host ('-' * 74)
    Write-Log ''; Write-Log $t; Write-Log ('-' * 74)
}
function Ok($t)   { Write-Host "  OK    $t" -ForegroundColor Green;  Write-Log "  OK    $t" }
function Warn($t) { Write-Host "  WARN  $t" -ForegroundColor Yellow; Write-Log "  WARN  $t" }
function Bad($t)  { Write-Host "  RISK  $t" -ForegroundColor Red;    Write-Log "  RISK  $t" }
function Info($t) { Write-Host "  $t";                               Write-Log "  $t" }

if ($LogFile) {
    try {
        Add-Content -LiteralPath $LogFile -Value ("=" * 74) -Encoding UTF8
        Add-Content -LiteralPath $LogFile -Value ("HEZIIS run started " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + "  host=" + $env:COMPUTERNAME + "  dryrun=" + $DryRun) -Encoding UTF8
    } catch { Write-Host "  WARN  could not write log: $($_.Exception.Message)" }
}

Write-Host ""
Write-Host "HEZIIS NET - make this laptop the hotspot" -ForegroundColor Cyan
Write-Host "host: $env:COMPUTERNAME"

# --- 0. elevation ------------------------------------------------------------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    Ok 'running elevated'
} elseif ($DryRun) {
    Info 'This run only shows you what the real run would do.'
} else {
    Write-Host ""
    Bad 'This needs Administrator rights.'
    Write-Host ""
    Info 'DOUBLE-CLICK this file instead (it is in the same folder):'
    Write-Host '      RUN-AS-ADMIN.cmd' -ForegroundColor Yellow
    Write-Host ""
    Info 'That asks Windows for administrator rights and keeps the window open.'
    Info 'Click YES on the Windows permission prompt.'
    Write-Host ""
    Info 'Note: right-clicking a .ps1 and choosing "Run with PowerShell" does NOT'
    Info 'grant administrator rights, so this script will always refuse that way.'
    Write-Host ""
    Info 'Or, from an already-elevated PowerShell window:'
    Write-Host "      cd $RepoDir" -ForegroundColor Yellow
    Write-Host '      .\deploy\make-hotspot-laptop.ps1' -ForegroundColor Yellow
    Write-Host ""
    Info 'To open an elevated PowerShell: Start menu, type Terminal,'
    Info 'right-click it, choose "Run as administrator".'
    Write-Host ""
    exit 1
}

# --- plan --------------------------------------------------------------------
Head 'What this will do'
Info '1. Turn off Fast Startup and hibernation'
Info '2. Check BitLocker (and refuse to resize if it is on)'
Info '3. Write the Ubuntu installer to your USB stick'
if ($FreeGB -gt 0) { Info "4. Free $FreeGB GB on C: for Ubuntu" }
else               { Info '4. Report how much space can be freed (change nothing)' }
Write-Host ""
Info 'It will NOT delete any files. Your stick is checked against its backup'
Info 'before it is touched.'
Write-Host ""
if (-not $Force) {
    $go = Read-Host "  Type GO to continue, or anything else to stop"
    if ($go.Trim() -ne 'GO') { Write-Host ""; Warn 'Stopped. Nothing changed.'; Write-Host ""; exit 0 }
}

# --- 1. Fast Startup + hibernation -------------------------------------------
Head '1. Fast Startup and hibernation'
$hb = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
       -Name HiberbootEnabled -ErrorAction SilentlyContinue).HiberbootEnabled
Info "Fast Startup is currently $(if ($hb -eq 1) { 'ON' } else { 'off' })"

$hibSize = 0
$hf = Get-Item 'C:\hiberfil.sys' -Force -ErrorAction SilentlyContinue
if ($hf) { $hibSize = $hf.Length }

& powercfg.exe /hibernate off 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) { Ok 'hibernation disabled' } else { Warn "powercfg returned $LASTEXITCODE" }

try {
    Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
        -Name HiberbootEnabled -Value 0 -Type DWord -ErrorAction Stop
} catch { Warn "could not set HiberbootEnabled: $($_.Exception.Message)" }

$hb2 = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
        -Name HiberbootEnabled -ErrorAction SilentlyContinue).HiberbootEnabled
if ($hb2 -eq 0) { Ok 'Fast Startup is OFF' } else { Bad 'Fast Startup still reads as ON' }

if ($hibSize -gt 0) {
    if (Test-Path 'C:\hiberfil.sys') { Info "hiberfil.sys still present" }
    else { Ok ("freed {0} GB" -f [math]::Round($hibSize / 1GB, 2)) }
}

# --- 2. BitLocker ------------------------------------------------------------
Head '2. BitLocker'
$bdeOn = $false
try {
    $out = & manage-bde.exe -status C: 2>&1 | Out-String
    $prot = [regex]::Match($out, 'Protection Status:\s*(.+)').Groups[1].Value.Trim()
    Info "protection status: $prot"
    if ($prot -match 'Protection On') {
        Bad 'BitLocker is ACTIVE on C:'
        Info 'Resizing an encrypted partition can lock you out of your own data.'
        Info 'Suspend it first:   manage-bde -protectors -disable C: -rebootcount 2'
        $bdeOn = $true
    } else {
        Ok 'C: is not encrypted'
    }
} catch { Warn "could not read BitLocker: $($_.Exception.Message)" }

# --- 3. the USB stick --------------------------------------------------------
Head '3. USB stick'

if (-not (Test-Path $IsoPath)) {
    Bad "Ubuntu ISO not found at $IsoPath"
    Info 'Download it first:  .\deploy\make-usb.ps1 -Download'
    exit 1
}
$isoItem = Get-Item $IsoPath
Ok ("ISO ready: {0} MB" -f [math]::Round($isoItem.Length / 1MB, 0))

$usb = @(Get-CimInstance Win32_DiskDrive | Where-Object {
    $_.InterfaceType -eq 'USB' -or $_.MediaType -like '*Removable*'
})
if ($usb.Count -eq 0) {
    Bad 'No USB stick plugged in.'
    Info 'Plug one in and run this again. The ISO needs 3.15 GB.'
    exit 1
}

Write-Host ""
foreach ($d in $usb) {
    Write-Host ("   Disk {0}   {1,-30} {2,6} GB" -f $d.Index, $d.Model, [math]::Round($d.Size / 1GB, 1)) -ForegroundColor White
}
Write-Host ""

function Get-VolumesOn([int]$n) {
    $o = @()
    $disk = Get-CimInstance Win32_DiskDrive -Filter "Index=$n" -ErrorAction SilentlyContinue
    if (-not $disk) { return $o }
    foreach ($p in @(Get-CimAssociatedInstance -InputObject $disk -Association Win32_DiskDriveToDiskPartition -ErrorAction SilentlyContinue)) {
        foreach ($v in @(Get-CimAssociatedInstance -InputObject $p -Association Win32_LogicalDiskToPartition -ErrorAction SilentlyContinue)) {
            if ($v.DeviceID) { $o += $v }
        }
    }
    return $o
}

$stick = if ($usb.Count -eq 1) { $usb[0] } else { $null }
if (-not $stick) {
    $pick = Read-Host "  Which disk number is the stick?"
    $stick = $usb | Where-Object Index -eq ([int]$pick) | Select-Object -First 1
}
if (-not $stick) { Bad 'No valid stick selected.'; exit 1 }

Write-Host ("  chosen: disk {0}  {1}  {2} GB" -f $stick.Index, $stick.Model, [math]::Round($stick.Size / 1GB, 1)) -ForegroundColor White

if ($stick.Size -lt $isoItem.Length) {
    Bad ("Stick is {0} GB but the ISO is {1} GB - too small." -f `
        [math]::Round($stick.Size / 1GB, 1), [math]::Round($isoItem.Length / 1GB, 2))
    exit 1
}

# --- show what is on it ------------------------------------------------------
$vols = @(Get-VolumesOn $stick.Index)
Head 'What is on that stick'
if ($vols.Count -eq 0) {
    Warn 'No readable volume. It may be unformatted - or hold data Windows cannot read.'
} else {
    foreach ($v in $vols) {
        $used = [math]::Round(($v.Size - $v.FreeSpace) / 1GB, 2)
        Write-Host ("  {0}\  {1}, {2} GB used of {3} GB" -f $v.DeviceID, $v.FileSystem, $used, [math]::Round($v.Size / 1GB, 2)) -ForegroundColor White
        Get-ChildItem "$($v.DeviceID)\" -Force -ErrorAction SilentlyContinue |
            Select-Object -First 12 |
            ForEach-Object { Write-Host ("      {0}{1}" -f $(if ($_.PSIsContainer) { '[dir]  ' } else { '       ' }), $_.Name) -ForegroundColor DarkGray }
    }
}

# --- THE BACKUP CHECK --------------------------------------------------------
Head 'Checking that this stick is safe to erase'
if ($SkipBackupCheck) {
    Warn 'Backup check SKIPPED by request - you are erasing unverified data.'
} else {
    $bk = $null
    foreach ($candidate in @($BackupDir, 'C:\Users\f\USB-STICK-BACKUP')) {
        if ($candidate -and (Test-Path $candidate)) { $bk = $candidate; break }
    }
    if (-not $bk) {
        Bad 'No backup folder found.'
        Info "Expected something at: $BackupDir"
        Info ''
        Info 'Copy the stick off first, then run this again. If you are certain the'
        Info 'stick holds nothing you want, re-run with -SkipBackupCheck.'
        exit 1
    }
    Info "backup folder: $bk"

    $srcFiles = @()
    foreach ($v in $vols) {
        $srcFiles += @(Get-ChildItem "$($v.DeviceID)\" -Recurse -File -Force -ErrorAction SilentlyContinue |
                        Where-Object { $_.FullName -notlike '*System Volume Information*' })
    }
    $bkFiles = @(Get-ChildItem -LiteralPath $bk -Recurse -File -Force -ErrorAction SilentlyContinue)
    $srcBytes = ($srcFiles | Measure-Object Length -Sum).Sum
    $bkBytes = ($bkFiles | Measure-Object Length -Sum).Sum
    if (-not $srcBytes) { $srcBytes = 0 }
    if (-not $bkBytes) { $bkBytes = 0 }

    Info ("stick  : {0,5} files   {1,9} MB" -f $srcFiles.Count, [math]::Round($srcBytes / 1MB, 1))
    Info ("backup : {0,5} files   {1,9} MB" -f $bkFiles.Count, [math]::Round($bkBytes / 1MB, 1))

    $missingBytes = $srcBytes - $bkBytes
    Write-Host ""
    if ($srcFiles.Count -eq $bkFiles.Count -and $missingBytes -eq 0) {
        Ok 'Every file on the stick is in the backup.'
    } elseif ($bkBytes -eq 0) {
        Bad 'The backup folder is EMPTY. Do not erase this stick.'
        exit 1
    } else {
        Warn ("{0} file(s), {1} MB are NOT accounted for in the backup." -f `
              ($srcFiles.Count - $bkFiles.Count), [math]::Round($missingBytes / 1MB, 1))
        Info ''
        Info 'These are the files that could not be read off the stick at all'
        Info '(corrupt directory entries). They cannot be copied by any tool, so'
        Info 'erasing the stick loses them permanently.'
        Info ''
        if ($DryRun) {
            Info 'DRY RUN - would ask you to type ERASE here.'
        } elseif (-not $Force) {
            $a = Read-Host "  Type ERASE anyway, or anything else to stop"
            Write-Log ("  [you typed] " + $a)
            if ($a.Trim().ToUpper() -ne 'ERASE') { Write-Host ""; Warn 'Stopped. Nothing erased.'; Write-Host ""; exit 0 }
        }
    }
}

# --- confirm -----------------------------------------------------------------
Write-Host ""
Write-Host "  +--------------------------------------------------------------+" -ForegroundColor Red
Write-Host ("  |  DISK {0} WILL BE COMPLETELY ERASED.                        |" -f $stick.Index) -ForegroundColor Red
Write-Host "  +--------------------------------------------------------------+" -ForegroundColor Red
Write-Host ""
Info 'Close any Explorer window showing this stick.'

if ($DryRun) {
    Head 'DRY RUN COMPLETE'
    Ok ("Would erase disk {0} and write {1} MB of Ubuntu to it." -f `
        $stick.Index, [math]::Round($isoItem.Length / 1MB, 0))
    Info 'Nothing was written. Your stick is untouched.'
    Write-Host ''
    Info 'To really do it, double-click RUN-AS-ADMIN.cmd in this folder.'
    Write-Host ''
    exit 0
}

if (-not $Force) {
    $a = Read-Host ("  Type the disk number ({0}) to write the installer" -f $stick.Index)
    Write-Log ("  [you typed] " + $a)
    if ($a.Trim() -ne "$($stick.Index)") { Write-Host ""; Warn 'Stopped. Nothing written.'; Write-Host ""; exit 0 }
}

# --- write -------------------------------------------------------------------
Head 'Clearing the partition table'
$tmp = Join-Path $env:TEMP "heziis-go-$PID.txt"
("select disk $($stick.Index)`r`nclean`r`n") | Set-Content -Path $tmp -Encoding ASCII
try {
    $out = & diskpart.exe /s $tmp 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { Bad 'diskpart failed'; Write-Host $out -ForegroundColor DarkGray; exit 1 }
    Ok 'cleared'
} finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Head 'Writing the ISO (takes several minutes)'
$drivePath = "\\.\PhysicalDrive$($stick.Index)"
$src = $null; $dst = $null
try {
    $src = [System.IO.File]::OpenRead($IsoPath)
    $dst = New-Object System.IO.FileStream($drivePath, [System.IO.FileMode]::Open,
                                            [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $buf = New-Object byte[] (4MB)
    $written = 0L; $lastPct = -1; $isoLen = $isoItem.Length
    while (($read = $src.Read($buf, 0, $buf.Length)) -gt 0) {
        $writeLen = $read
        if ($writeLen % 512 -ne 0) {
            $writeLen = [int]([math]::Ceiling($writeLen / 512) * 512)
            for ($i = $read; $i -lt $writeLen; $i++) { $buf[$i] = 0 }
        }
        $dst.Write($buf, 0, $writeLen)
        $written += $writeLen
        $pct = [int](($written * 100) / $isoLen)
        if ($pct -ne $lastPct -and $pct % 20 -eq 0) {
            $lastPct = $pct
            Write-Host ("    {0,3}%   {1} MB" -f $pct, [math]::Round($written / 1MB, 0))
        }
    }
    $dst.Flush($true)
    Ok ("wrote {0} MB" -f [math]::Round($written / 1MB, 0))
} catch {
    Bad "raw write failed: $($_.Exception.Message)"
    Info 'Close Explorer/antivirus holding the stick, unplug, replug, retry.'
    exit 1
} finally {
    if ($dst) { $dst.Dispose() }
    if ($src) { $src.Dispose() }
}

# verify the boot code landed
Start-Sleep -Seconds 2
try {
    $rb = New-Object System.IO.FileStream($drivePath, [System.IO.FileMode]::Open,
                                          [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    $check = New-Object byte[] (1MB)
    $null = $rb.Read($check, 0, $check.Length)
    $rb.Dispose()
    $head = New-Object byte[] (1MB)
    $fs = [System.IO.File]::OpenRead($IsoPath)
    $null = $fs.Read($head, 0, $head.Length)
    $fs.Dispose()
    $same = $true
    for ($i = 0; $i -lt $check.Length; $i++) { if ($check[$i] -ne $head[$i]) { $same = $false; break } }
    if ($same) { Ok 'verified: boot code is on the stick (legacy BIOS + UEFI)' }
    else { Bad 'verification FAILED - do not trust this stick'; exit 1 }
} catch { Warn "could not verify: $($_.Exception.Message)" }

# --- 4. space for Ubuntu -----------------------------------------------------
Head '4. Space for Ubuntu'
try {
    $part = Get-Partition -DriveLetter C -ErrorAction Stop
    $sup  = Get-PartitionSupportedSize -DriveLetter C -ErrorAction Stop
    $curGB = [math]::Round($part.Size / 1GB, 2)
    $maxGB = [math]::Round(($part.Size - $sup.SizeMin) / 1GB, 2)
    Info "C: is $curGB GB; the most Windows can free is $maxGB GB"
    if ($maxGB -lt 26) {
        Warn "That is less than the 26 GB Ubuntu wants."
        Info 'Defragment C: (defrag C: /X) and run this again for more.'
    } else {
        Ok "Enough for Ubuntu (needs about 25 GB)"
    }

    if ($FreeGB -gt 0) {
        if ($bdeOn) {
            Bad 'Refusing to resize: BitLocker is on.'
        } elseif ($FreeGB -gt [math]::Floor($maxGB)) {
            Bad "Cannot free $FreeGB GB. Maximum is $([math]::Floor($maxGB)) GB."
            Info "Re-run with:  -FreeGB $([math]::Floor($maxGB))"
        } else {
            $want = $part.Size - ($FreeGB * 1GB)
            Write-Host ""
            Info ("Freeing {0} GB: C: goes to {1} GB, leaving {2} GB unallocated" -f `
                  $FreeGB, [math]::Round($want / 1GB, 2), $FreeGB)
            Info 'Your files are not touched - only the partition boundary moves.'
            $a = if ($Force) { 'SHRINK' } else { Read-Host "  Type SHRINK to confirm" }
            if ($a.Trim() -eq 'SHRINK') {
                try {
                    Resize-Partition -DriveLetter C -Size $want -ErrorAction Stop
                    Ok 'partition resized'
                    Info ("C: is now {0} GB" -f [math]::Round((Get-Partition -DriveLetter C).Size / 1GB, 2))
                    Info 'Leave the unallocated space EMPTY - do not format it.'
                } catch { Bad "resize failed: $($_.Exception.Message)" }
            } else { Warn 'Shrink skipped.' }
        }
    }
} catch { Warn "could not query partition: $($_.Exception.Message)" }

# --- done --------------------------------------------------------------------
Head 'Done - reboot to continue'
Write-Host ""
Write-Host '  On the laptop:' -ForegroundColor White
Write-Host '   1. Shut down FULLY (not Restart).' -ForegroundColor White
Write-Host '   2. Power on, tap F12 (or F2/Del) until the boot menu appears.' -ForegroundColor White
Write-Host '   3. This machine is LEGACY BIOS - pick the entry WITHOUT "UEFI:"' -ForegroundColor Yellow
Write-Host '   4. Choose "Try or Install Ubuntu Server".' -ForegroundColor White
Write-Host '   5. Pick "Try Ubuntu" first if you want to test with zero risk.' -ForegroundColor White
Write-Host ''
Write-Host '  Then, in Linux:' -ForegroundColor White
Write-Host '      sudo ./deploy/setup-hotspot.sh' -ForegroundColor Yellow
Write-Host '      sudo ./deploy/verify-hotspot.sh' -ForegroundColor Yellow
Write-Host ''
Write-Host '  If you only ran "Try Ubuntu", get the project from the Windows' -ForegroundColor DarkGray
Write-Host '  partition - NTFS is readable from Linux - or git clone it.' -ForegroundColor DarkGray
Write-Host ''
if ($LogFile) { Info "Full log written to: $LogFile" }
Write-Host ''

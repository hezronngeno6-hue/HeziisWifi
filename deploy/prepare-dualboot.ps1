# =============================================================================
#  HEZIIS NET - prepare Windows for dual boot      *** RUN AS ADMINISTRATOR ***
# =============================================================================
#  Does everything that needs elevation, in one go:
#
#    1. checks BitLocker (resizing an encrypted drive can lock you out)
#    2. turns OFF Fast Startup and hibernation
#    3. reports the true maximum shrink Windows will allow
#    4. optionally frees space for Ubuntu
#
#  USAGE - right-click this file -> "Run with PowerShell", or in an
#  ADMINISTRATOR PowerShell:
#
#      .\deploy\prepare-dualboot.ps1                 # safe prep + report only
#      .\deploy\prepare-dualboot.ps1 -FreeGB 25      # also free 25 GB for Ubuntu
#
#  WHY HIBERNATION HAS TO GO, NOT JUST FAST STARTUP
#  ------------------------------------------------
#  Fast Startup alone stops the USB boot menu appearing. But hibernation is the
#  bigger hazard: if Windows is hibernated, its NTFS partition is left in an
#  unclean state, and if Linux then mounts that partition read-write you can
#  corrupt the whole thing. Turning hibernation off removes the risk and frees
#  5-15 GB of hiberfil.sys into the bargain.
#
#  THIS SCRIPT DOES NOT TOUCH YOUR FILES. Shrinking only moves the partition
#  boundary into space that is already free. It still has no undo, so it asks
#  before doing it.
# =============================================================================

[CmdletBinding()]
param(
    # How much space to free for Ubuntu, in GB. Omit to only report.
    [int]$FreeGB = 0,

    # Do not ask before shrinking
    [switch]$Force
)

$ErrorActionPreference = 'Continue'

function Head($t) { Write-Host ""; Write-Host $t -ForegroundColor Cyan; Write-Host ('-' * 74) }
function Ok($t)   { Write-Host "  OK    $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  WARN  $t" -ForegroundColor Yellow }
function Bad($t)  { Write-Host "  RISK  $t" -ForegroundColor Red }
function Info($t) { Write-Host "  $t" }

$blockers = 0

Write-Host ""
Write-Host "HEZIIS NET - prepare Windows for dual boot" -ForegroundColor Cyan
Write-Host "host: $env:COMPUTERNAME"

# --- elevation ---------------------------------------------------------------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ""
    Bad 'This must run as Administrator - it changes boot settings and resizes a partition.'
    Write-Host ""
    Info 'DOUBLE-CLICK the launcher in the same folder instead:'
    Write-Host '      RUN-AS-ADMIN.cmd prepare-dualboot.ps1' -ForegroundColor Yellow
    Write-Host ""
    Info 'Click YES on the Windows permission prompt.'
    Info 'Right-clicking a .ps1 and choosing "Run with PowerShell" does NOT elevate.'
    Write-Host ""
    Info 'Or, from an already-elevated PowerShell window:'
    Write-Host "      cd $((Resolve-Path (Join-Path $PSScriptRoot '..')).Path)" -ForegroundColor Yellow
    Write-Host '      .\deploy\prepare-dualboot.ps1 -FreeGB 25' -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
Ok 'running elevated'

# --- 1. BitLocker ------------------------------------------------------------
Head '1. BitLocker - must be off before resizing'
$bdeProtected = $false
try {
    $out = & manage-bde.exe -status C: 2>&1 | Out-String
    $conv = [regex]::Match($out, 'Conversion Status:\s*(.+)').Groups[1].Value.Trim()
    $prot = [regex]::Match($out, 'Protection Status:\s*(.+)').Groups[1].Value.Trim()
    Info "conversion status: $conv"
    Info "protection status: $prot"
    if ($prot -match 'Protection On' -or $conv -match 'Encrypted') {
        Bad 'BitLocker is ACTIVE on C:'
        Info 'Resizing can lock you out of your own data. Either:'
        Info '   a) suspend it:   manage-bde -protectors -disable C: -rebootcount 2'
        Info '   b) turn it off:  manage-bde -off C:      (takes a while)'
        Info 'Make sure you have the recovery key written down first.'
        $bdeProtected = $true
        $blockers++
    } else {
        Ok 'C: is not encrypted - safe to resize'
    }
} catch {
    Warn "could not read BitLocker status: $($_.Exception.Message)"
    Info 'continuing - but confirm manually with:  manage-bde -status C:'
}

# --- 2. Fast Startup + hibernation -------------------------------------------
Head '2. Fast Startup and hibernation'
$hb = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
       -Name HiberbootEnabled -ErrorAction SilentlyContinue).HiberbootEnabled
if ($hb -eq 1) { Info 'Fast Startup is currently ON' } elseif ($hb -eq 0) { Info 'Fast Startup is already off' }

$hibFile = 'C:\hiberfil.sys'
$hibSize = 0
try {
    $f = Get-Item $hibFile -Force -ErrorAction SilentlyContinue
    if ($f) { $hibSize = $f.Length }
} catch { }

Info 'Turning off hibernation (this also disables Fast Startup)...'
& powercfg.exe /hibernate off 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Ok 'hibernation is off'
} else {
    Warn "powercfg returned $LASTEXITCODE"
}

# belt and braces: set the flag directly too
try {
    Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
        -Name HiberbootEnabled -Value 0 -Type DWord -ErrorAction Stop
    Ok 'Fast Startup disabled in the registry'
} catch {
    Warn "could not write HiberbootEnabled: $($_.Exception.Message)"
}

$hb2 = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
        -Name HiberbootEnabled -ErrorAction SilentlyContinue).HiberbootEnabled
if ($hb2 -eq 0) { Ok 'verified: Fast Startup is OFF' }
else { Bad 'Fast Startup still reads as ON' ; $blockers++ }

if ($hibSize -gt 0) {
    $f2 = Get-Item $hibFile -Force -ErrorAction SilentlyContinue
    if ($f2) { Info "hiberfil.sys still present ($([math]::Round($f2.Length/1GB,2)) GB)" }
    else { Ok "freed $([math]::Round($hibSize/1GB,2)) GB (hiberfil.sys removed)" }
}

# --- 3. what shrink is actually possible -------------------------------------
Head '3. Maximum shrink Windows will allow'
$supported = $null
try {
    $part = Get-Partition -DriveLetter C -ErrorAction Stop
    $supported = Get-PartitionSupportedSize -DriveLetter C -ErrorAction Stop
    $curGB  = [math]::Round($part.Size / 1GB, 2)
    $minGB  = [math]::Round($supported.SizeMin / 1GB, 2)
    $maxFreeGB = [math]::Round(($part.Size - $supported.SizeMin) / 1GB, 2)
    Info "C: is now          : $curGB GB"
    Info "smallest allowed   : $minGB GB"
    Info "largest shrink     : $maxFreeGB GB"
    Write-Host ""
    if ($maxFreeGB -ge 26) {
        Ok "You can free up to $maxFreeGB GB. Ubuntu needs about 25 GB."
    } elseif ($maxFreeGB -ge 12) {
        Warn "Only $maxFreeGB GB can be freed. That is enough for a minimal Ubuntu,"
        Info "but you will be short of room. Defragment C: and try again for more."
    } else {
        Bad "Only $maxFreeGB GB can be freed - that is not enough for Ubuntu."
        Info "Windows is blocked by unmovable files. Try: defrag C: /X  then rerun."
        $blockers++
    }
} catch {
    Bad "could not query shrink limits: $($_.Exception.Message)"
    $blockers++
}

# --- 4. the shrink -----------------------------------------------------------
if ($FreeGB -le 0) {
    Head '4. Shrink - NOT requested'
    Info 'This run only reported. No partition was changed.'
    Info ''
    Info "To actually free space for Ubuntu, run again with -FreeGB, e.g.:"
    Write-Host "      .\deploy\prepare-dualboot.ps1 -FreeGB 25" -ForegroundColor Yellow
} elseif ($blockers -gt 0) {
    Head '4. Shrink - REFUSED'
    Bad "There are $blockers blocker(s) above. Fix those first."
    Info 'Nothing was changed.'
} elseif ($bdeProtected) {
    Head '4. Shrink - REFUSED (BitLocker is on)'
} else {
    Head "4. Free $FreeGB GB for Ubuntu"
    $part = Get-Partition -DriveLetter C
    $supported = Get-PartitionSupportedSize -DriveLetter C

    $wantBytes = $part.Size - ($FreeGB * 1GB)
    if ($wantBytes -lt $supported.SizeMin) {
        $maxGB = [math]::Floor(($part.Size - $supported.SizeMin) / 1GB)
        Bad "Cannot free $FreeGB GB. The most Windows allows is $maxGB GB."
        Info "Run again with  -FreeGB $maxGB"
        exit 1
    }

    Info ("C: will go from {0} GB to {1} GB, leaving {2} GB unallocated for Ubuntu" -f `
        [math]::Round($part.Size / 1GB, 2),
        [math]::Round($wantBytes / 1GB, 2),
        $FreeGB)
    Write-Host ""
    Info 'Your files are NOT touched - this only moves the partition boundary.'
    Info 'There is still no undo, so make sure anything irreplaceable is copied off.'
    Write-Host ""

    $go = $true
    if (-not $Force) {
        $a = Read-Host "  Type SHRINK to continue, or anything else to abort"
        if ($a.Trim() -ne 'SHRINK') { $go = $false }
    }

    if (-not $go) {
        Write-Host ""
        Warn 'Aborted. Nothing was changed.'
    } else {
        try {
            Resize-Partition -DriveLetter C -Size $wantBytes -ErrorAction Stop
            Ok 'partition resized'
            Start-Sleep -Seconds 1
            $after = Get-Partition -DriveLetter C
            Info ("C: is now {0} GB" -f [math]::Round($after.Size / 1GB, 2))
            $free = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
            Info ("free space on C: {0} GB" -f [math]::Round($free.FreeSpace / 1GB, 2))
            Write-Host ""
            Ok 'Space is ready. Leave the unallocated space EMPTY - do not format it.'
        } catch {
            Bad "resize failed: $($_.Exception.Message)"
            $blockers++
        }
    }
}

# --- summary -----------------------------------------------------------------
Head 'Summary'
if ($blockers -eq 0) {
    Write-Host '  No blockers. Windows is ready for the Ubuntu installer.' -ForegroundColor Green
} else {
    Write-Host "  $blockers blocker(s) - resolve them before installing." -ForegroundColor Red
}
Write-Host ""
Write-Host '  Next:' -ForegroundColor White
Write-Host '    1. Plug in a SPARE usb stick (never the one holding your files)' -ForegroundColor White
Write-Host '    2. .\deploy\make-usb.ps1 -List      then   -DiskNumber <n>' -ForegroundColor White
Write-Host '    3. Reboot, F12, pick the entry WITHOUT "UEFI:"' -ForegroundColor White
Write-Host '    4. In the installer choose Custom storage layout, NOT "entire disk"' -ForegroundColor White
Write-Host ''
exit ([int]($blockers -gt 0))

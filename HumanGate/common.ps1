# Shared helper for HumanGate executable script packets. Dot-source only.
# Local values may be used for execution, but MUST NOT be printed to public stdout.
function Get-HgRoot([string]$scriptDir) {
    $root = ([string](& git -C $scriptDir rev-parse --show-toplevel)).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($root)) { throw "WORKTREE_UNRESOLVED" }
    if (-not (Test-Path -LiteralPath (Join-Path $root ".git"))) { throw "NOT_A_WORKTREE_ROOT" }
    return $root
}

function Initialize-HgEnv([string]$root) {
    $destination = Join-Path $root ".env"
    if (Test-Path -LiteralPath $destination) { return $false }
    $template = Join-Path $root "HumanGate\.env.example"
    $bytes = [IO.File]::ReadAllBytes($template)
    $stream = $null
    try {
        $stream = [IO.File]::Open($destination, [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
    return $true
}

function Import-HgEnv([string]$root) {
    $allowed = @("HG_EXPECTED_HEAD","HG_PYTHON","HG_SERIAL_PORT",
        "HG_DEVICE_MAC","HG_PNP_INSTANCE_ID","HG_NVS_OFFSET","HG_NVS_SIZE",
        "HG_NVS_PLAN_SHA256","HG_NVS_PARTITION_SHA256")
    # Invalidate every earlier worktree/shell value BEFORE trying to parse.
    foreach ($key in $allowed) {
        [Environment]::SetEnvironmentVariable($key,$null,"Process")
    }
    $seen=@{}; $values=@{}
    foreach ($line in [IO.File]::ReadAllLines((Join-Path $root ".env"))) {
        $trim=$line.Trim()
        if (-not $trim -or $trim.StartsWith("#")) { continue }
        $index=$trim.IndexOf("=")
        if ($index -lt 1) { throw "INVALID_ENV_LINE" }
        $key=$trim.Substring(0,$index).Trim()
        if ($key -cnotin $allowed -or $seen.ContainsKey($key)) {
            throw "UNKNOWN_OR_DUPLICATE_ENV_KEY"
        }
        $seen[$key]=$true
        $value=$trim.Substring($index+1).Trim()
        if ($value.Length -ge 2 -and
            (($value[0] -eq '"' -and $value[$value.Length-1] -eq '"') -or
             ($value[0] -eq "'" -and $value[$value.Length-1] -eq "'"))) {
            $value=$value.Substring(1,$value.Length-2)
        }
        if ($value.IndexOf([char]0) -ge 0) { throw "INVALID_ENV_VALUE" }
        $values[$key]=$value
    }
    foreach ($key in $allowed) {
        if (-not $seen.ContainsKey($key)) { throw "MISSING_ENV_KEY" }
    }
    foreach ($key in $allowed) {
        $value=[string]$values[$key]
        if ([string]::IsNullOrEmpty($value)) { $value=$null }
        [Environment]::SetEnvironmentVariable($key,$value,"Process")
    }
}

function Assert-HgHead([string]$root) {
    if ($env:HG_EXPECTED_HEAD -cnotmatch '^[a-fA-F0-9]{40}$') { throw "EXPECTED_HEAD_MISSING_OR_INVALID" }
    $head = ([string](& git -C $root rev-parse HEAD)).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -cne $env:HG_EXPECTED_HEAD) { throw "HEAD_MISMATCH" }
    $dirty = @(& git -C $root status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0 -or $dirty.Count -gt 0) { throw "TRACKED_WORKTREE_DIRTY" }
    return $head
}

function Assert-HgPort {
    if ($env:HG_SERIAL_PORT -cnotmatch '^COM[1-9][0-9]*$' -or
        [string]::IsNullOrWhiteSpace($env:HG_PNP_INSTANCE_ID)) { throw "PORT_IDENTITY_UNQUALIFIED" }
    $ports = @(Get-PnpDevice -Class Ports -PresentOnly -ErrorAction Stop |
        Where-Object { ([string]$_.FriendlyName) -match ('\(' + [regex]::Escape($env:HG_SERIAL_PORT) + '\)$') })
    if ($ports.Count -ne 1 -or
        -not ([string]$ports[0].InstanceId).Equals($env:HG_PNP_INSTANCE_ID,
            [StringComparison]::OrdinalIgnoreCase)) { throw "PORT_IDENTITY_MISMATCH" }
}

# Only the exact-HEAD HG-BUILD qualification receipt may supply these hashes.
# This checks local build evidence; runtime/Device identity remains a separate gate.
function Assert-HgNvsPartition([string]$root,[long]$offset,[long]$size) {
    $expectedPlan=[string]$env:HG_NVS_PLAN_SHA256
    $expectedTable=[string]$env:HG_NVS_PARTITION_SHA256
    if ($expectedPlan -cnotmatch '^[a-fA-F0-9]{64}$' -or
        $expectedTable -cnotmatch '^[a-fA-F0-9]{64}$') {
        throw "QUALIFIED_PARTITION_EVIDENCE_REQUIRED"
    }
    $build=Join-Path $root "firmware\build\hg-qualified"
    $planPath=Join-Path $build "flasher_args.json"
    $tablePath=Join-Path $build "partition_table\partition-table.bin"
    if (-not [IO.File]::Exists($planPath) -or
        -not [IO.File]::Exists($tablePath)) {
        throw "QUALIFIED_PARTITION_FILES_MISSING"
    }
    $planHash=(Get-FileHash -LiteralPath $planPath -Algorithm SHA256 -ErrorAction Stop).Hash
    $tableHash=(Get-FileHash -LiteralPath $tablePath -Algorithm SHA256 -ErrorAction Stop).Hash
    if ($planHash -cne $expectedPlan.ToUpperInvariant() -or
        $tableHash -cne $expectedTable.ToUpperInvariant()) {
        throw "QUALIFIED_PARTITION_HASH_MISMATCH"
    }
    $plan=[IO.File]::ReadAllText($planPath) | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $plan.flash_files) { throw "PARTITION_FLASH_PLAN_INVALID" }
    $members=@($plan.flash_files.PSObject.Properties | Where-Object {
        ([string]$_.Value).Replace('\','/') -ceq "partition_table/partition-table.bin"
    })
    if ($members.Count -ne 1 -or
        ([string]$members[0].Name) -cnotmatch '^0x[0-9a-fA-F]+$') {
        throw "PARTITION_FLASH_PLAN_INVALID"
    }
    $planOffset=[Convert]::ToInt64(([string]$members[0].Name).Substring(2),16)
    if ($planOffset -lt 0 -or $planOffset -ge 8388608 -or
        $planOffset % 4096 -ne 0) { throw "PARTITION_FLASH_PLAN_INVALID" }
    $bytes=[IO.File]::ReadAllBytes($tablePath)
    if ($bytes.Length -lt 32 -or $bytes.Length -gt 4096 -or
        $bytes.Length % 32 -ne 0) { throw "PARTITION_BINARY_INVALID" }
    $found=@()
    for ($pos=0; $pos -le $bytes.Length-32; $pos+=32) {
        $magic=[BitConverter]::ToUInt16($bytes,$pos)
        if ($magic -eq 0xFFFF) { break }
        if ($magic -eq 0xEBEB) { continue }
        if ($magic -ne 0x50AA) { throw "PARTITION_BINARY_INVALID" }
        $label=[Text.Encoding]::ASCII.GetString($bytes,$pos+12,16).Split([char]0)[0]
        if ($label -ceq "nvs") {
            if ($bytes[$pos+2] -ne 1 -or $bytes[$pos+3] -ne 2) {
                throw "NVS_PARTITION_TYPE_MISMATCH"
            }
            $found += [pscustomobject]@{
                Offset=[long][BitConverter]::ToUInt32($bytes,$pos+4)
                Size=[long][BitConverter]::ToUInt32($bytes,$pos+8)
            }
        }
    }
    if ($found.Count -ne 1) { throw "NVS_PARTITION_NOT_UNIQUE" }
    if ($found[0].Offset -ne $offset -or $found[0].Size -ne $size) {
        throw "NVS_PARTITION_OFFSET_SIZE_MISMATCH"
    }
    if ($size -le 0 -or $offset -lt 0 -or ($offset+$size) -gt 8388608) {
        throw "NVS_PARTITION_BOUNDS_INVALID"
    }
    return [pscustomobject]@{PlanMatched=$true;TableMatched=$true}
}

function Quote-HgArg([string]$value) {
    if ($value -match '^[^\s"]+$') { return $value }
    $text = '"'; $slashes = 0
    foreach ($ch in $value.ToCharArray()) {
        if ($ch -eq '\') { $slashes++; continue }
        if ($ch -eq '"') { $text += ('\' * (2*$slashes+1)) + '"'; $slashes=0; continue }
        $text += ('\' * $slashes) + $ch; $slashes=0
    }
    return $text + ('\' * (2*$slashes)) + '"'
}

function Invoke-HgNative([string]$exe,[string[]]$argv,[string]$workingDir,
                         [int]$deadlineSeconds,[string]$phase,[string]$cleanupExecutable="") {
    $result=[pscustomobject]@{
        Started=$false; Pid="NOT_RUN"; ExitCode="NOT_RUN"; Status="NOT_RUN"
        TimedOut=$false; DeadlineSeconds=$deadlineSeconds; RootExited="NOT_RUN"
        TreeStopped="NOT_RUN"; Cleanup="NOT_REQUIRED"
        Stdout=""; Stderr=""
    }
    $proc=New-Object System.Diagnostics.Process
    $psi=New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName=$exe
    $psi.Arguments=(@($argv | ForEach-Object { Quote-HgArg ([string]$_) }) -join " ")
    $psi.WorkingDirectory=$workingDir
    $psi.UseShellExecute=$false
    $psi.CreateNoWindow=$true
    $psi.RedirectStandardOutput=$true
    $psi.RedirectStandardError=$true
    $proc.StartInfo=$psi
    $stdoutTask=$null; $stderrTask=$null; $needsCleanup=$false
    $watch=[Diagnostics.Stopwatch]::StartNew()
    $nextProgress=15
    try {
        if (-not $proc.Start()) { throw "PROCESS_START_REJECTED" }
        $result.Started=$true
        $result.Pid=$proc.Id
        $result.ExitCode="UNKNOWN"
        $result.Status="RUNNING"
        $stdoutTask=$proc.StandardOutput.ReadToEndAsync()
        $stderrTask=$proc.StandardError.ReadToEndAsync()
        Write-Host ("PROGRESS phase={0} elapsed_seconds=0 status=RUNNING" -f $phase)
        while ($true) {
            if ($proc.WaitForExit(1000)) { $result.RootExited=$true; break }
            $elapsed=[int]$watch.Elapsed.TotalSeconds
            if ($elapsed -ge $nextProgress) {
                Write-Host ("PROGRESS phase={0} elapsed_seconds={1} status=RUNNING" -f $phase,$elapsed)
                $nextProgress=$elapsed+15
            }
            if ($watch.Elapsed.TotalSeconds -ge $deadlineSeconds) {
                $result.TimedOut=$true
                $result.Status="TIMEOUT"
                $needsCleanup=$true
                break
            }
        }
        if (-not $needsCleanup) {
            if ($stdoutTask.Wait(10000) -and $stderrTask.Wait(10000)) {
                $result.ExitCode=$proc.ExitCode
                $result.Status="EXITED"
                $result.TreeStopped="NOT_REQUIRED"
            } else {
                $result.Status="OUTPUT_DRAIN_TIMEOUT"
                $needsCleanup=$true
            }
        }
    } catch {
        if ($result.Started) { $result.Status="POST_START_EXCEPTION" }
        else { $result.Status="START_EXCEPTION" }
        $needsCleanup=$result.Started
    } finally {
        if ($needsCleanup -and $result.Started) {
            $result.Cleanup="TREE_STOP_UNVERIFIED"
            $killer=$null; $killSuccess=$false
            try {
                $killInfo=New-Object System.Diagnostics.ProcessStartInfo
                $killInfo.FileName=if ([string]::IsNullOrEmpty($cleanupExecutable)) {
                    Join-Path $env:SystemRoot 'System32\taskkill.exe'
                } else { $cleanupExecutable }
                $killInfo.Arguments="/PID $($result.Pid) /T /F"
                $killInfo.UseShellExecute=$false
                $killInfo.CreateNoWindow=$true
                $killer=New-Object System.Diagnostics.Process
                $killer.StartInfo=$killInfo
                if ($killer.Start()) {
                    if ($killer.WaitForExit(15000)) {
                        $killSuccess=$killer.ExitCode -eq 0
                        if (-not $killSuccess) { $result.Cleanup="TASKKILL_NONZERO" }
                    } else {
                        $result.Cleanup="TASKKILL_TIMEOUT"
                        try { $killer.Kill() } catch {}
                        try { [void]$killer.WaitForExit(5000) } catch {}
                    }
                } else { $result.Cleanup="TASKKILL_START_REJECTED" }
            } catch { $result.Cleanup="TASKKILL_EXCEPTION" }
            finally { if ($null -ne $killer) { try { $killer.Dispose() } catch {} } }
            $rootStopped=$false
            try { $rootStopped=$proc.WaitForExit(15000); $result.RootExited=$rootStopped }
            catch { $result.RootExited="UNKNOWN" }
            $result.TreeStopped=($killSuccess -and $rootStopped)
            if ($result.TreeStopped) { $result.Cleanup="TREE_STOP_REPORTED" }
        }
        if ($result.Started -and [string]$result.RootExited -ceq "NOT_RUN") {
            try { $result.RootExited=[bool]$proc.HasExited }
            catch { $result.RootExited="UNKNOWN" }
        }
        if ($null -ne $stdoutTask) {
            try { if ($stdoutTask.Wait(5000)) { $result.Stdout=[string]$stdoutTask.Result } } catch {}
        }
        if ($null -ne $stderrTask) {
            try { if ($stderrTask.Wait(5000)) { $result.Stderr=[string]$stderrTask.Result } } catch {}
        }
        try { $proc.Dispose() } catch {}
        $watch.Stop()
    }
    return $result
}

function Save-HgPrivate([string]$root,[string]$phase,[object]$result) {
    $dir=Join-Path $root ".local\HumanGate"
    [void][IO.Directory]::CreateDirectory($dir)
    $name=("{0}-{1}" -f $phase,([guid]::NewGuid().ToString("N")))
    # Write process identity first; output capture may itself fail.
    [IO.File]::WriteAllText((Join-Path $dir ($name+".status.txt")),
        ("started={0};pid={1};deadline_seconds={2};root_exited={3};status={4};exit={5};timeout={6};tree_stopped={7};cleanup={8}" -f
        $result.Started,$result.Pid,$result.DeadlineSeconds,$result.RootExited,
        $result.Status,$result.ExitCode,$result.TimedOut,$result.TreeStopped,
        $result.Cleanup),[Text.Encoding]::UTF8)
    [IO.File]::WriteAllText((Join-Path $dir ($name+".stdout.txt")),
        [string]$result.Stdout,[Text.Encoding]::UTF8)
    [IO.File]::WriteAllText((Join-Path $dir ($name+".stderr.txt")),
        [string]$result.Stderr,[Text.Encoding]::UTF8)
}

function Write-HgResult([string]$phase,[System.Collections.IDictionary]$fields,
                        [string]$diagnosis,[string]$disposition) {
    Write-Host ""
    Write-Host "==== $phase ===="
    foreach ($key in $fields.Keys) { Write-Host ("{0}={1}" -f $key,$fields[$key]) }
    Write-Host ("diagnosis={0}" -f $diagnosis)
    Write-Host ("disposition={0}" -f $disposition)
    Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase,$diagnosis,$disposition)
    Write-Host ""
}

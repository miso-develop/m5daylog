# M5Daylog Device Human Gate Operator Runbook

status: operational supplement
scope: Windows host + M5Capsule / ESP32-S3 Device Human Gate
authority: approved Spec / Decision / Acceptance Criteria remain normative

## Purpose

This runbook records the repeatable operator path that proved effective during the Strategy 2 USB lifecycle Human Gate. It exists to reduce setup churn, prevent invalid cycle counting, and make recovery from host/tooling failures deterministic.

This file does not replace Acceptance Criteria. When it conflicts with the current Issue / Spec / Decision or the repository Human Gate rules, the current approved requirement wins.

## 1. Golden rules

1. Test one exact revision. If HEAD changes, stop and start a new evidence session.
2. Use the ESP-IDF managed Python and the canonical esp-idf tools/idf.py script explicitly. Do not rely on a bare idf.py command resolving correctly.
3. Establish a qualified Device baseline before counting any cycle.
4. Every PowerShell packet uses one outer & { ... } block and reaches one common SUMMARY footer.
5. RELEASE_STORAGE is sent at most once per release attempt. After request transmission or accepted response, never retry it merely because cleanup or later observation fails.
6. Separate harness/tooling failures from product failures.
7. Low-level flash/NVS reads may change runtime state even when persistent data is read-only. Re-establish the application baseline afterward.
8. Keep physical instructions explicit and one action at a time. For this project, operator-facing ACTION= and WAIT= text should be Japanese.
9. Do not publish machine-local paths, usernames, serial numbers, or raw captured audio in durable public evidence.

## 2. Known-good Windows / ESP-IDF preparation

The source-backed, reusable non-mutating operator packets are now versioned under `HumanGate/`. Start with `& { & .\HumanGate\init.ps1 }` in the current worktree to create an ignored worktree-root `.env` from `HumanGate/.env.example`, then fill its allowlisted `HG_*` fields locally and run `HumanGate/check-head.ps1`. Executable script packets are candidates until individually proven on Windows/the intended device; historical #87/#50 evidence is not automatically portable to their current source HEAD. For #51 interrupted-sync evidence, the PC domain remains authoritative over its sync implementation; the device runbook records the observation/recovery boundary only. See `HumanGate/KNOWHOW.md` for NVS, D-031, CDC, and incremental sync lessons.

Use placeholders in durable documentation and resolve concrete paths only in the operator's local shell. The **preflight, build, and flash packets below are independent**: each resolves its own paths, Git revision, managed Python, and canonical idf.py rather than relying on PowerShell variables left by an earlier & { ... } block. The firmware requires **ESP-IDF v5.5.5**.

Recommended PowerShell preflight:

~~~powershell
& {
    $phase = "HG-PREFLIGHT"
    $expectedHead = "<revision>"
    $repoRoot = "<repo-root>"
    $idfRoot = "<esp-idf-root>"
    $idfTools = "<idf-tools-root>"
    $python = "<idf-managed-python>"
    $idfPy = Join-Path $idfRoot "tools\idf.py"

    $head = "UNRESOLVED"
    $gitExit = "NOT_RUN"
    $idfExit = "NOT_RUN"
    $idfVersion = "NOT_RUN"
    $diagnosis = "UNEXPECTED_PREFLIGHT_ERROR"
    $disposition = "BLOCKED"

    try {
        foreach ($required in @($repoRoot, $idfRoot, $idfTools, $python, $idfPy)) {
            if (-not (Test-Path -LiteralPath $required)) {
                $diagnosis = "REQUIRED_PATH_MISSING"
                throw $diagnosis
            }
        }
        $head = ([string](& git -C $repoRoot rev-parse HEAD)).Trim()
        $gitExit = $LASTEXITCODE
        if ($gitExit -ne 0 -or $head -ne $expectedHead) {
            $diagnosis = "HEAD_MISMATCH_OR_GIT_FAILURE"
            throw $diagnosis
        }
        $tracked = @(& git -C $repoRoot status --porcelain --untracked-files=no)
        $gitExit = $LASTEXITCODE
        if ($gitExit -ne 0 -or $tracked.Count -ne 0) {
            $diagnosis = "TRACKED_WORKTREE_NOT_CLEAN"
            throw $diagnosis
        }

        $env:IDF_PATH = $idfRoot
        $env:IDF_TOOLS_PATH = $idfTools
        $env:PATH = "$(Split-Path -Parent $python);$env:PATH"
        . (Join-Path $idfRoot "export.ps1")

        $versionLines = @(& $python $idfPy --version)
        $idfExit = $LASTEXITCODE
        $idfVersion = ($versionLines -join " ").Trim()
        if ($idfExit -ne 0 -or $idfVersion -ne "ESP-IDF v5.5.5") {
            $diagnosis = "UNSUPPORTED_OR_UNRESOLVED_ESP_IDF"
            throw $diagnosis
        }

        $diagnosis = "OK"
        $disposition = "PASS"
    }
    catch {
        # Preserve the specific safe diagnosis above; do not print local paths.
    }
    finally {
        Write-Host ""
        Write-Host "==== $phase ===="
        Write-Host ("expected_head={0}" -f $expectedHead)
        Write-Host ("head={0}" -f $head)
        Write-Host ("git_exit={0}" -f $gitExit)
        Write-Host ("idf_exit={0}" -f $idfExit)
        Write-Host ("idf_version={0}" -f $idfVersion)
        Write-Host ("diagnosis={0}" -f $diagnosis)
        Write-Host ("disposition={0}" -f $disposition)
        Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase, $diagnosis, $disposition)
        Write-Host ""
    }
}
~~~

Important lessons:

- A Windows application alias named python can resolve ahead of the ESP-IDF managed interpreter.
- An idf.py.exe helper/shim can report its own wrapper version rather than the actual ESP-IDF version.
- For acceptance-relevant commands, invoke the managed Python plus the canonical tools/idf.py path directly.
- Source export.ps1 only after IDF_PATH, IDF_TOOLS_PATH, and the managed Python directory are set.
- A nonzero native exit or a version other than ESP-IDF v5.5.5 is BLOCKED, never qualified PASS.
- A host reboot normally requires environment reactivation and a fresh Device baseline; it does not by itself require reflashing unchanged exact-head firmware.

## 3. Exact build identity

For a clean exact-head firmware qualification, use a standalone packet that generates the effective SDKCONFIG from the tracked sdkconfig.defaults in a fresh isolated build directory. Git ignores firmware/sdkconfig and fullclean preserves it; neither a clean tracked worktree nor fullclean is sufficient to qualify that ambient file. Existing project configuration, output and Device state must never be silently deleted or reset. If unmanaged components, untracked firmware inputs, ambient build overrides or old isolated output exist, stop and use an explicitly provisioned fresh worktree rather than deleting them:

~~~powershell
& {
    $phase = "HG-BUILD"
    $expectedHead = "<revision>"
    $repoRoot = "<repo-root>"
    $idfRoot = "<esp-idf-root>"
    $idfTools = "<idf-tools-root>"
    $python = "<idf-managed-python>"
    $privateLogDir = "<local-private-evidence-dir>"
    $idfPy = Join-Path $idfRoot "tools\idf.py"
    $firmwareRoot = Join-Path $repoRoot "firmware"
    $defaults = Join-Path $firmwareRoot "sdkconfig.defaults"
    $qualifiedBuildDir = Join-Path $firmwareRoot "build\hg-qualified"
    $qualifiedSdkconfig = Join-Path $qualifiedBuildDir "sdkconfig"
    $app = Join-Path $qualifiedBuildDir "m5daylog.bin"

    $head = "UNRESOLVED"
    $gitExit = "NOT_RUN"
    $idfExit = "NOT_RUN"
    $idfVersion = "NOT_RUN"
    $defaultsHash = "NOT_RUN"
    $configHash = "NOT_RUN"
    $buildExit = "NOT_RUN"
    $buildDeadlineSeconds=3600
    $buildTimedOut=$false
    $buildTreeStopped="NOT_RUN"
    $buildStarted="NOT_RUN"
    $buildPid="NOT_RUN"
    $buildStatus="NOT_RUN"
    $buildRootStopped="NOT_RUN"
    $buildCleanupStatus="NOT_RUN"
    $appHash = "NOT_RUN"
    $planHash = "NOT_RUN"
    $imageSetHash = "NOT_RUN"
    $imageCount = "NOT_RUN"
    $enteredFirmware = $false
    $diagnosis = "UNEXPECTED_BUILD_ERROR"
    $disposition = "BLOCKED"

    # Exact argv quoting for Windows PowerShell 5.1 / .NET Framework.
    function Quote-NativeArgument([string]$value) {
        if ($value -match '^[^\s"]+$') { return $value }
        $q = '"'; $slashes = 0
        foreach ($ch in $value.ToCharArray()) {
            if ($ch -eq '\') { $slashes++; continue }
            if ($ch -eq '"') { $q += ('\' * (2*$slashes+1)) + '"'; $slashes=0; continue }
            $q += ('\' * $slashes) + $ch; $slashes=0
        }
        return $q + ('\' * (2*$slashes)) + '"'
    }
    # Bounded native child. Separate asynchronous output drains avoid pipe deadlock.
    function Invoke-BoundedPython([string[]]$argv,[string]$workDir,[int]$seconds) {
        $result=[pscustomobject]@{
            Started=$false; Pid="NOT_RUN"; ExitCode="NOT_RUN"
            TimedOut=$false; RootStopped="NOT_RUN"; TreeStopped="NOT_RUN"
            Status="NOT_RUN"; CleanupStatus="NOT_REQUIRED"
            Stdout=""; Stderr=""
        }
        $psi=New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName=$python
        $psi.Arguments=(@($argv | ForEach-Object { Quote-NativeArgument ([string]$_) }) -join ' ')
        $psi.WorkingDirectory=$workDir
        $psi.UseShellExecute=$false
        $psi.CreateNoWindow=$true
        $psi.RedirectStandardOutput=$true
        $psi.RedirectStandardError=$true
        $proc=New-Object System.Diagnostics.Process
        $proc.StartInfo=$psi
        $outTask=$null
        $errTask=$null
        $needsCleanup=$false
        try {
            if (-not $proc.Start()) {
                $result.Status="START_REJECTED"
            } else {
                $result.Started=$true
                $result.Pid=$proc.Id
                $result.ExitCode="UNKNOWN"
                $result.Status="RUNNING"
                $outTask=$proc.StandardOutput.ReadToEndAsync()
                $errTask=$proc.StandardError.ReadToEndAsync()
                if (-not $proc.WaitForExit($seconds*1000)) {
                    $result.TimedOut=$true
                    $result.Status="TIMEOUT"
                    $needsCleanup=$true
                } elseif (-not $outTask.Wait(10000) -or -not $errTask.Wait(10000)) {
                    $result.Status="OUTPUT_DRAIN_TIMEOUT"
                    $needsCleanup=$true
                } else {
                    $result.RootStopped=$true
                    $result.ExitCode=$proc.ExitCode
                    $result.Status="EXITED"
                    $result.TreeStopped="NOT_REQUIRED"
                }
            }
        }
        catch {
            # Do not let a post-start exception escape without PID/child state.
            if ($result.Started) { $result.Status="POST_START_EXCEPTION" }
            else { $result.Status="START_EXCEPTION" }
            $needsCleanup=[bool]$result.Started
        }
        finally {
            if ($result.Started -and $needsCleanup) {
                # Best effort only; taskkill failure never proves that a child stopped.
                $result.CleanupStatus="TREE_STOP_UNVERIFIED"
                $killSucceeded=$false
                $kill=$null
                try {
                    $kpsi=New-Object System.Diagnostics.ProcessStartInfo
                    $kpsi.FileName=Join-Path $env:SystemRoot 'System32\taskkill.exe'
                    $kpsi.Arguments="/PID $($result.Pid) /T /F"
                    $kpsi.UseShellExecute=$false
                    $kpsi.CreateNoWindow=$true
                    $kill=New-Object System.Diagnostics.Process
                    $kill.StartInfo=$kpsi
                    if ($kill.Start()) {
                        if ($kill.WaitForExit(15000)) {
                            $killSucceeded=($kill.ExitCode -eq 0)
                            if (-not $killSucceeded) { $result.CleanupStatus="TASKKILL_NONZERO_EXIT" }
                        } else {
                            $result.CleanupStatus="TASKKILL_TIMEOUT"
                            try { $kill.Kill() } catch {}
                            try { [void]$kill.WaitForExit(5000) } catch {}
                        }
                    } else { $result.CleanupStatus="TASKKILL_NOT_STARTED" }
                }
                catch { $result.CleanupStatus="TASKKILL_EXCEPTION" }
                finally { if ($null -ne $kill) { try { $kill.Dispose() } catch {} } }

                $rootExited=$false
                try { $rootExited=$proc.WaitForExit(15000) }
                catch { $result.CleanupStatus="ROOT_WAIT_EXCEPTION" }
                $result.RootStopped=$rootExited
                $result.TreeStopped=($killSucceeded -and $rootExited)
                if ($result.TreeStopped) { $result.CleanupStatus="TREE_STOP_REPORTED" }
            }
            if ($result.Started -and [string]$result.RootStopped -ceq "NOT_RUN") {
                try { $result.RootStopped=$proc.HasExited }
                catch { $result.RootStopped="UNKNOWN" }
            }
            # Capture may fail independently of process completion. Never call it PASS.
            if ($null -ne $outTask) {
                try {
                    if ($outTask.Wait(5000)) { $result.Stdout=[string]$outTask.Result }
                    elseif ($result.Status -eq "EXITED") { $result.Status="OUTPUT_CAPTURE_TIMEOUT" }
                }
                catch { if ($result.Status -eq "EXITED") { $result.Status="OUTPUT_CAPTURE_EXCEPTION" } }
            }
            if ($null -ne $errTask) {
                try {
                    if ($errTask.Wait(5000)) { $result.Stderr=[string]$errTask.Result }
                    elseif ($result.Status -eq "EXITED") { $result.Status="OUTPUT_CAPTURE_TIMEOUT" }
                }
                catch { if ($result.Status -eq "EXITED") { $result.Status="OUTPUT_CAPTURE_EXCEPTION" } }
            }
            try { $proc.Dispose() } catch {}
        }
        return $result
    }

    # Every process's raw stdout/stderr and exit metadata stay on the operator host.
    function Save-PrivateProcessEvidence([string]$stage,[object]$result) {
        if (-not (Test-Path -LiteralPath $privateLogDir -PathType Container)) {
            throw "PRIVATE_EVIDENCE_DIRECTORY_MISSING"
        }
        $base=("{0}-{1}-{2}" -f $stage,
            (Get-Date).ToString("yyyyMMdd-HHmmssfff"),[guid]::NewGuid().ToString("N"))
        [IO.File]::WriteAllText((Join-Path $privateLogDir "$base.stdout.txt"),
            [string]$result.Stdout,[Text.Encoding]::UTF8)
        [IO.File]::WriteAllText((Join-Path $privateLogDir "$base.stderr.txt"),
            [string]$result.Stderr,[Text.Encoding]::UTF8)
        [IO.File]::WriteAllText((Join-Path $privateLogDir "$base.status.txt"),
            ("started={0};pid={1};status={2};exit={3};timeout={4};root_stopped={5};tree_stopped={6};cleanup={7}" -f
                $result.Started,$result.Pid,$result.Status,$result.ExitCode,
                $result.TimedOut,$result.RootStopped,$result.TreeStopped,$result.CleanupStatus),[Text.Encoding]::UTF8)
    }

    # Derive the entire immutable qualification identity from the generated
    # flasher plan. Do not rely on the application binary hash alone.
    function Get-QualifiedFlashPlan([string]$root) {
        $jsonPath = Join-Path $root "flasher_args.json"
        $p = [IO.File]::ReadAllText($jsonPath) | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $p.flash_files -or $null -eq $p.write_flash_args -or
            $null -eq $p.extra_esptool_args -or $null -eq $p.flash_settings -or
            [string]$p.extra_esptool_args.chip -cne "esp32s3" -or
            [string]$p.flash_settings.flash_size -cne "8MB" -or
            $p.extra_esptool_args.stub -isnot [bool]) {
            throw "INVALID_FLASH_PLAN"
        }
        $flags = @($p.write_flash_args | ForEach-Object { [string]$_ })
        if ($flags.Count -ne 6) { throw "INVALID_FLASH_FLAGS" }
        $opts = @{}
        for ($i = 0; $i -lt $flags.Count; $i += 2) {
            if ($flags[$i] -cnotin @("--flash_mode","--flash_size","--flash_freq") -or
                $opts.ContainsKey($flags[$i])) { throw "INVALID_FLASH_FLAGS" }
            $opts[$flags[$i]] = $flags[$i+1]
        }
        if ($opts.Count -ne 3 -or $opts["--flash_size"] -cne "8MB" -or
            $opts["--flash_mode"] -cnotin @("dio","dout","qio","qout") -or
            $opts["--flash_freq"] -cnotmatch '^[0-9]{1,3}m$' -or
            $opts["--flash_mode"] -cne [string]$p.flash_settings.flash_mode -or
            $opts["--flash_freq"] -cne [string]$p.flash_settings.flash_freq -or
            [string]$p.extra_esptool_args.before -cnotmatch '^[a-z_]+$' -or
            [string]$p.extra_esptool_args.after -cnotmatch '^[a-z_]+$') {
            throw "INVALID_FLASH_FLAGS"
        }
        $images = @()
        foreach ($entry in $p.flash_files.PSObject.Properties) {
            $offset = [string]$entry.Name
            $relative = [string]$entry.Value
            if ($offset -cnotmatch '^0x[0-9a-fA-F]+$' -or
                $relative -cnotmatch '^(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+[.]bin$') {
                throw "INVALID_FLASH_ENTRY"
            }
            $number = [Convert]::ToInt64($offset.Substring(2),16)
            $full = Join-Path $root $relative.Replace('/', '\')
            $file = Get-Item -LiteralPath $full -ErrorAction Stop
            if ($file.PSIsContainer -or $file.Length -le 0 -or
                ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "INVALID_FLASH_IMAGE"
            }
            $images += [pscustomobject]@{
                Offset=$number; OffsetText=("0x{0:x}" -f $number)
                Relative=$relative; Path=$full; Size=$file.Length
                Sha=(Get-FileHash -LiteralPath $full -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
            }
        }
        $images = @($images | Sort-Object Offset)
        if ($images.Count -lt 3 -or
            @($images | Where-Object Relative -eq "bootloader/bootloader.bin").Count -ne 1 -or
            @($images | Where-Object Relative -eq "partition_table/partition-table.bin").Count -ne 1 -or
            @($images | Where-Object Relative -eq "m5daylog.bin").Count -ne 1) {
            throw "REQUIRED_FLASH_IMAGE_MISSING"
        }
        $end = [int64]0
        $rows = @()
        $filePairs = @()
        foreach ($image in $images) {
            if ($image.Offset -lt $end -or
                ($image.Offset + $image.Size) -gt (8*1024*1024)) {
                throw "FLASH_OFFSETS_INVALID"
            }
            $end = $image.Offset + $image.Size
            $rows += ("{0}|{1}|{2}" -f $image.OffsetText,$image.Relative,$image.Sha)
            $filePairs += $image.OffsetText
            $filePairs += $image.Path
        }
        # Canonical identity: sorted offset|relative-file|sha256, LF terminated.
        $canonical = ($rows -join [char]10) + [char]10
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            $bytes = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical))
            $imageSetDigest = ([BitConverter]::ToString($bytes)).Replace("-","").ToLowerInvariant()
        }
        finally { $sha256.Dispose() }
        [pscustomobject]@{
            PlanHash=(Get-FileHash -LiteralPath $jsonPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
            ImageHash=$imageSetDigest; Count=$images.Count
            Flags=$flags; Pairs=$filePairs
            ImagePaths=@($images | ForEach-Object { $_.Path })
            Before=[string]$p.extra_esptool_args.before
            After=[string]$p.extra_esptool_args.after
            Stub=[bool]$p.extra_esptool_args.stub
        }
    }

    try {
        foreach ($required in @($repoRoot, $idfRoot, $idfTools, $python, $idfPy, $privateLogDir, $firmwareRoot,
                                (Join-Path $firmwareRoot "CMakeLists.txt"), $defaults)) {
            if (-not (Test-Path -LiteralPath $required)) {
                $diagnosis = "REQUIRED_PATH_MISSING"
                throw $diagnosis
            }
        }
        $head = ([string](& git -C $repoRoot rev-parse HEAD)).Trim()
        $gitExit = $LASTEXITCODE
        if ($gitExit -ne 0 -or $head -ne $expectedHead) {
            $diagnosis = "HEAD_MISMATCH_OR_GIT_FAILURE"
            throw $diagnosis
        }
        $tracked = @(& git -C $repoRoot status --porcelain --untracked-files=no)
        $gitExit = $LASTEXITCODE
        if ($gitExit -ne 0 -or $tracked.Count -ne 0) {
            $diagnosis = "TRACKED_WORKTREE_NOT_CLEAN"
            throw $diagnosis
        }

        $untrackedFirmware = @(& git -C $firmwareRoot ls-files --others --exclude-standard)
        $gitExit = $LASTEXITCODE
        if ($gitExit -ne 0 -or $untrackedFirmware.Count -ne 0) {
            $diagnosis = "UNTRACKED_FIRMWARE_INPUT"
            throw $diagnosis
        }
        if (Test-Path -LiteralPath (Join-Path $firmwareRoot "managed_components")) {
            $diagnosis = "UNQUALIFIED_LOCAL_MANAGED_COMPONENTS"
            throw $diagnosis
        }
        if (Test-Path -LiteralPath $qualifiedBuildDir) {
            $diagnosis = "QUALIFICATION_OUTPUT_ALREADY_EXISTS"
            throw $diagnosis
        }
        foreach ($name in @("SDKCONFIG", "SDKCONFIG_DEFAULTS", "IDF_TARGET", "IDF_PRESET", "EXTRA_COMPONENT_DIRS")) {
            if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name))) {
                $diagnosis = "AMBIENT_BUILD_OVERRIDE"
                throw $diagnosis
            }
        }
        $defaultsHash = (Get-FileHash -LiteralPath $defaults -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()

        $env:IDF_PATH = $idfRoot
        $env:IDF_TOOLS_PATH = $idfTools
        $env:PATH = "$(Split-Path -Parent $python);$env:PATH"
        . (Join-Path $idfRoot "export.ps1")
        $versionLines = @(& $python $idfPy --version)
        $idfExit = $LASTEXITCODE
        $idfVersion = ($versionLines -join " ").Trim()
        if ($idfExit -ne 0 -or $idfVersion -ne "ESP-IDF v5.5.5") {
            $diagnosis = "UNSUPPORTED_OR_UNRESOLVED_ESP_IDF"
            throw $diagnosis
        }

        Push-Location -LiteralPath $firmwareRoot -ErrorAction Stop
        $enteredFirmware = $true
        $buildStarted="UNKNOWN"
        $buildPid="UNKNOWN"
        $buildStatus="UNKNOWN"
        $buildRootStopped="UNKNOWN"
        $buildCleanupStatus="UNKNOWN"
        $diagnosis="BUILD_WRAPPER_EXCEPTION_CHILD_STATE_UNKNOWN"
        $buildResult=Invoke-BoundedPython @($idfPy,"-B",$qualifiedBuildDir,
            "-DSDKCONFIG=$qualifiedSdkconfig","-DSDKCONFIG_DEFAULTS=$defaults",
            "-DIDF_TARGET=esp32s3","build") $firmwareRoot $buildDeadlineSeconds
        $buildStarted=$buildResult.Started
        $buildPid=$buildResult.Pid
        $buildStatus=$buildResult.Status
        $buildRootStopped=$buildResult.RootStopped
        $buildCleanupStatus=$buildResult.CleanupStatus
        $buildExit=$buildResult.ExitCode
        $buildTimedOut=$buildResult.TimedOut
        $buildTreeStopped=$buildResult.TreeStopped
        $diagnosis="BUILD_PRIVATE_EVIDENCE_SAVE_FAILED"
        Save-PrivateProcessEvidence "build" $buildResult
        if ($buildTimedOut) { $diagnosis="BUILD_TIMEOUT_NO_AUTOMATIC_RETRY"; throw $diagnosis }
        if ($buildStatus -ne "EXITED") {
            $diagnosis="BUILD_CHILD_INDETERMINATE_NO_AUTOMATIC_RETRY"
            throw $diagnosis
        }
        if ($buildExit -ne 0) {
            $diagnosis="BUILD_FAILED"
            throw $diagnosis
        }
        if (-not (Test-Path -LiteralPath $app -PathType Leaf)) {
            $diagnosis = "APP_BINARY_MISSING"
            throw $diagnosis
        }
        if (-not (Test-Path -LiteralPath $qualifiedSdkconfig -PathType Leaf)) {
            $diagnosis = "EFFECTIVE_SDKCONFIG_MISSING"
            throw $diagnosis
        }
        $effectiveLines = @([System.IO.File]::ReadAllLines($qualifiedSdkconfig))
        $defaultLines = @([System.IO.File]::ReadAllLines($defaults) |
            Where-Object { $_ -match '^CONFIG_[A-Za-z0-9_]+=' })
        if ($defaultLines.Count -eq 0) {
            $diagnosis = "EMPTY_APPROVED_DEFAULTS"
            throw $diagnosis
        }
        foreach ($line in $defaultLines) {
            if ($effectiveLines -cnotcontains $line) {
                $diagnosis = "EFFECTIVE_CONFIG_DEFAULT_MISMATCH"
                throw $diagnosis
            }
        }
        if ((Get-FileHash -LiteralPath $defaults -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -ne $defaultsHash) {
            $diagnosis = "DEFAULTS_CHANGED_DURING_BUILD"
            throw $diagnosis
        }
        $configHash = (Get-FileHash -LiteralPath $qualifiedSdkconfig -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        $appHash = (Get-FileHash -LiteralPath $app -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        $diagnosis = "FLASH_PLAN_INVALID_AFTER_BUILD"
        $qualifiedPlan = Get-QualifiedFlashPlan $qualifiedBuildDir
        $planHash = $qualifiedPlan.PlanHash
        $imageSetHash = $qualifiedPlan.ImageHash
        $imageCount = $qualifiedPlan.Count
        $diagnosis = "OK"
        $disposition = "PASS"
    }
    catch {
        # Failure is captured as BLOCKED; no acceptance cycle is counted.
    }
    finally {
        if ($enteredFirmware) {
            try { Pop-Location -ErrorAction Stop }
            catch { $diagnosis = "LOCATION_RESTORE_FAILED"; $disposition = "BLOCKED" }
        }
        Write-Host ""
        Write-Host "==== $phase ===="
        Write-Host ("expected_head={0}" -f $expectedHead)
        Write-Host ("head={0}" -f $head)
        Write-Host ("git_exit={0}" -f $gitExit)
        Write-Host ("idf_exit={0}" -f $idfExit)
        Write-Host ("idf_version={0}" -f $idfVersion)
        Write-Host ("defaults_sha256={0}" -f $defaultsHash)
        Write-Host ("effective_sdkconfig_sha256={0}" -f $configHash)
        Write-Host ("build_exit={0}" -f $buildExit)
        Write-Host ("build_deadline_seconds={0}" -f $buildDeadlineSeconds)
        Write-Host ("build_timed_out={0}" -f $buildTimedOut)
        Write-Host ("build_tree_stopped={0}" -f $buildTreeStopped)
        Write-Host ("build_child_started={0}" -f $buildStarted)
        Write-Host ("build_child_pid={0}" -f $buildPid)
        Write-Host ("build_child_status={0}" -f $buildStatus)
        Write-Host ("build_root_stopped={0}" -f $buildRootStopped)
        Write-Host ("build_cleanup_status={0}" -f $buildCleanupStatus)
        Write-Host ("app_sha256={0}" -f $appHash)
        Write-Host ("flasher_args_sha256={0}" -f $planHash)
        Write-Host ("flash_image_set_sha256={0}" -f $imageSetHash)
        Write-Host ("flash_image_count={0}" -f $imageCount)
        Write-Host ("diagnosis={0}" -f $diagnosis)
        Write-Host ("disposition={0}" -f $disposition)
        Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase, $diagnosis, $disposition)
        Write-Host ""
    }
}
~~~

Retain the passing exact HEAD plus defaults_sha256, effective_sdkconfig_sha256, app_sha256, flasher_args_sha256 and flash_image_set_sha256 as the build-to-flash identity. The image-set SHA-256 covers every generated flash image as sorted LF-terminated UTF-8 `0xoffset|build-relative-image.bin|sha256` records. Together with the generated JSON plan digest, this binds offsets, tool flags, bootloader, partition table, application, and any additional flash image. Keep the generated plan and original per-image digests in local evidence. Do not build again or change this qualified output between qualification and flashing. Never reuse them across a revision/toolchain change. A partial isolated build is not requalified by fullclean; preserve evidence and use a newly controlled checkout/output path. The hash is not itself proof of what is running on the physical target; qualification still requires an observed flash result and a fresh Device baseline. Use the build-generated flash plan. Do not guess partition offsets. Do not erase NVS unless the approved setup explicitly requires a fixture reset. Fixture repair/reset is setup and counts as zero acceptance cycles.

## 4. Flash and runtime baseline

Before a write, bind the selected COM port to an independently operator-qualified physical board. Privately record the trusted factory/eFuse MAC and the Ports-class Windows PnP InstanceId. Neither value may be learned solely from the candidate COM in this same attempt. Do not publish either value. A COM number, ESP32-S3 chip family, or VID/PID alone cannot identify the intended unit.

Place the intended board in approved ROM Download Mode, then establish its re-enumerated COM and PnP mapping. The read-only esptool read_mac operation does not program flash but may change runtime state or USB enumeration. Recheck the port after MAC probing and immediately before the writer; any absent, changed, duplicate or unexpected identity is BLOCKED with no write. No cable swap is permitted between check and write: independent esptool processes cannot cryptographically prevent a last-moment physical substitution.

Process deadlines: build 3600 seconds, read_mac 120 seconds, flash 900 seconds. Timeout and post-start invocation/output exceptions trigger bounded best-effort taskkill /T /F process-tree stop. Each child operation records whether it actually started, its local PID, concrete exit code or UNKNOWN, timeout, root-exit observation, process-tree cleanup result, and status separately; taskkill launch failure is never a successful stop. A process exited by itself does not prove that its descendants exited. Before any retry an operator must independently confirm that no child remains; a reported process-tree cleanup success alone is not permission to retry. After flash launch, exit failure, timeout, wrapper or evidence-save error means WRITE_STATE_UNKNOWN regardless of process cleanup: no automatic retry, erase, acceptance cycle, or assumed firmware success. Re-establish Device runtime state and keep raw process logs/identifiers local; public evidence must omit PIDs and machine-local details.

For normal exact-head flashing, copy **all five** passing HG-BUILD hashes (defaults, effective sdkconfig, app, generated plan, complete image set) into this standalone packet. It revalidates every image and the complete flash plan **before** touching the Device. It then invokes the managed Python's `esptool write_flash` directly, using only verified plan entries. Do **not** substitute `idf.py flash`: that action may automatically rebuild before writing. No concurrent editor, build or flashing process may mutate the qualification output. The flash packet holds Windows read-only sharing locks on the verified plan/configuration/images and repeats all five hash checks under those locks before invoking esptool; lock acquisition or drift is BLOCKED without attempting to write. A successful flash still requires an independently verified runtime baseline. Hash agreement alone does not prove the target was programmed correctly; a fresh Device baseline remains mandatory.

~~~powershell
& {
    $phase = "HG-FLASH"
    $expectedHead = "<revision>"
    $expectedDefaultsSha256 = "<defaults-sha256-from-HG-BUILD>"
    $expectedConfigSha256 = "<effective-sdkconfig-sha256-from-HG-BUILD>"
    $expectedAppSha256 = "<app-sha256-from-HG-BUILD>"
    $expectedPlanSha256 = "<flasher-args-sha256-from-HG-BUILD>"
    $expectedImageSetSha256 = "<flash-image-set-sha256-from-HG-BUILD>"
    $repoRoot = "<repo-root>"
    $idfRoot = "<esp-idf-root>"
    $idfTools = "<idf-tools-root>"
    $python = "<idf-managed-python>"
    $privateLogDir = "<local-private-evidence-dir>"
    $idfPy = Join-Path $idfRoot "tools\idf.py"
    $serialPort = "<serial-port>"
    $expectedDeviceMac = "<locally-qualified-device-mac>"
    $expectedPortInstanceId = "<locally-qualified-pnp-instance-id>"
    $firmwareRoot = Join-Path $repoRoot "firmware"
    $defaults = Join-Path $firmwareRoot "sdkconfig.defaults"
    $qualifiedBuildDir = Join-Path $firmwareRoot "build\hg-qualified"
    $qualifiedSdkconfig = Join-Path $qualifiedBuildDir "sdkconfig"
    $app = Join-Path $qualifiedBuildDir "m5daylog.bin"

    $head = "UNRESOLVED"
    $gitExit = "NOT_RUN"
    $idfExit = "NOT_RUN"
    $idfVersion = "NOT_RUN"
    $flashExit = "NOT_RUN"
    $identityExit="NOT_RUN"
    $pnpMatch=$false
    $macMatch=$false
    $flashAttempted=$false
    $flashWriteState="NOT_STARTED"
    $identityDeadlineSeconds=120
    $flashDeadlineSeconds=900
    $flashTimedOut=$false
    $flashTreeStopped="NOT_RUN"
    $flashStarted="NOT_RUN"
    $flashPid="NOT_RUN"
    $flashStatus="NOT_RUN"
    $flashRootStopped="NOT_RUN"
    $flashCleanupStatus="NOT_RUN"
    $identityStarted="NOT_RUN"
    $identityPid="NOT_RUN"
    $identityStatus="NOT_RUN"
    $identityRootStopped="NOT_RUN"
    $identityCleanupStatus="NOT_RUN"
    $beforeHash = "NOT_RUN"
    $afterHash = "NOT_RUN"
    $beforeConfigHash = "NOT_RUN"
    $afterConfigHash = "NOT_RUN"
    $beforeDefaultsHash = "NOT_RUN"
    $afterDefaultsHash = "NOT_RUN"
    $beforePlanHash = "NOT_RUN"
    $afterPlanHash = "NOT_RUN"
    $beforeImageHash = "NOT_RUN"
    $afterImageHash = "NOT_RUN"
    $imageCount = "NOT_RUN"
    $readLocks = @()
    $enteredFirmware = $false
    $diagnosis = "UNEXPECTED_FLASH_ERROR"
    $disposition = "BLOCKED"

    # Exact argv quoting for Windows PowerShell 5.1 / .NET Framework.
    function Quote-NativeArgument([string]$value) {
        if ($value -match '^[^\s"]+$') { return $value }
        $q = '"'; $slashes = 0
        foreach ($ch in $value.ToCharArray()) {
            if ($ch -eq '\') { $slashes++; continue }
            if ($ch -eq '"') { $q += ('\' * (2*$slashes+1)) + '"'; $slashes=0; continue }
            $q += ('\' * $slashes) + $ch; $slashes=0
        }
        return $q + ('\' * (2*$slashes)) + '"'
    }
    # Bounded native child. Separate asynchronous output drains avoid pipe deadlock.
    function Invoke-BoundedPython([string[]]$argv,[string]$workDir,[int]$seconds) {
        $result=[pscustomobject]@{
            Started=$false; Pid="NOT_RUN"; ExitCode="NOT_RUN"
            TimedOut=$false; RootStopped="NOT_RUN"; TreeStopped="NOT_RUN"
            Status="NOT_RUN"; CleanupStatus="NOT_REQUIRED"
            Stdout=""; Stderr=""
        }
        $psi=New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName=$python
        $psi.Arguments=(@($argv | ForEach-Object { Quote-NativeArgument ([string]$_) }) -join ' ')
        $psi.WorkingDirectory=$workDir
        $psi.UseShellExecute=$false
        $psi.CreateNoWindow=$true
        $psi.RedirectStandardOutput=$true
        $psi.RedirectStandardError=$true
        $proc=New-Object System.Diagnostics.Process
        $proc.StartInfo=$psi
        $outTask=$null
        $errTask=$null
        $needsCleanup=$false
        try {
            if (-not $proc.Start()) {
                $result.Status="START_REJECTED"
            } else {
                $result.Started=$true
                $result.Pid=$proc.Id
                $result.ExitCode="UNKNOWN"
                $result.Status="RUNNING"
                $outTask=$proc.StandardOutput.ReadToEndAsync()
                $errTask=$proc.StandardError.ReadToEndAsync()
                if (-not $proc.WaitForExit($seconds*1000)) {
                    $result.TimedOut=$true
                    $result.Status="TIMEOUT"
                    $needsCleanup=$true
                } elseif (-not $outTask.Wait(10000) -or -not $errTask.Wait(10000)) {
                    $result.Status="OUTPUT_DRAIN_TIMEOUT"
                    $needsCleanup=$true
                } else {
                    $result.RootStopped=$true
                    $result.ExitCode=$proc.ExitCode
                    $result.Status="EXITED"
                    $result.TreeStopped="NOT_REQUIRED"
                }
            }
        }
        catch {
            # Do not let a post-start exception escape without PID/child state.
            if ($result.Started) { $result.Status="POST_START_EXCEPTION" }
            else { $result.Status="START_EXCEPTION" }
            $needsCleanup=[bool]$result.Started
        }
        finally {
            if ($result.Started -and $needsCleanup) {
                # Best effort only; taskkill failure never proves that a child stopped.
                $result.CleanupStatus="TREE_STOP_UNVERIFIED"
                $killSucceeded=$false
                $kill=$null
                try {
                    $kpsi=New-Object System.Diagnostics.ProcessStartInfo
                    $kpsi.FileName=Join-Path $env:SystemRoot 'System32\taskkill.exe'
                    $kpsi.Arguments="/PID $($result.Pid) /T /F"
                    $kpsi.UseShellExecute=$false
                    $kpsi.CreateNoWindow=$true
                    $kill=New-Object System.Diagnostics.Process
                    $kill.StartInfo=$kpsi
                    if ($kill.Start()) {
                        if ($kill.WaitForExit(15000)) {
                            $killSucceeded=($kill.ExitCode -eq 0)
                            if (-not $killSucceeded) { $result.CleanupStatus="TASKKILL_NONZERO_EXIT" }
                        } else {
                            $result.CleanupStatus="TASKKILL_TIMEOUT"
                            try { $kill.Kill() } catch {}
                            try { [void]$kill.WaitForExit(5000) } catch {}
                        }
                    } else { $result.CleanupStatus="TASKKILL_NOT_STARTED" }
                }
                catch { $result.CleanupStatus="TASKKILL_EXCEPTION" }
                finally { if ($null -ne $kill) { try { $kill.Dispose() } catch {} } }

                $rootExited=$false
                try { $rootExited=$proc.WaitForExit(15000) }
                catch { $result.CleanupStatus="ROOT_WAIT_EXCEPTION" }
                $result.RootStopped=$rootExited
                $result.TreeStopped=($killSucceeded -and $rootExited)
                if ($result.TreeStopped) { $result.CleanupStatus="TREE_STOP_REPORTED" }
            }
            if ($result.Started -and [string]$result.RootStopped -ceq "NOT_RUN") {
                try { $result.RootStopped=$proc.HasExited }
                catch { $result.RootStopped="UNKNOWN" }
            }
            # Capture may fail independently of process completion. Never call it PASS.
            if ($null -ne $outTask) {
                try {
                    if ($outTask.Wait(5000)) { $result.Stdout=[string]$outTask.Result }
                    elseif ($result.Status -eq "EXITED") { $result.Status="OUTPUT_CAPTURE_TIMEOUT" }
                }
                catch { if ($result.Status -eq "EXITED") { $result.Status="OUTPUT_CAPTURE_EXCEPTION" } }
            }
            if ($null -ne $errTask) {
                try {
                    if ($errTask.Wait(5000)) { $result.Stderr=[string]$errTask.Result }
                    elseif ($result.Status -eq "EXITED") { $result.Status="OUTPUT_CAPTURE_TIMEOUT" }
                }
                catch { if ($result.Status -eq "EXITED") { $result.Status="OUTPUT_CAPTURE_EXCEPTION" } }
            }
            try { $proc.Dispose() } catch {}
        }
        return $result
    }

    # Every process's raw stdout/stderr and exit metadata stay on the operator host.
    function Save-PrivateProcessEvidence([string]$stage,[object]$result) {
        if (-not (Test-Path -LiteralPath $privateLogDir -PathType Container)) {
            throw "PRIVATE_EVIDENCE_DIRECTORY_MISSING"
        }
        $base=("{0}-{1}-{2}" -f $stage,
            (Get-Date).ToString("yyyyMMdd-HHmmssfff"),[guid]::NewGuid().ToString("N"))
        [IO.File]::WriteAllText((Join-Path $privateLogDir "$base.stdout.txt"),
            [string]$result.Stdout,[Text.Encoding]::UTF8)
        [IO.File]::WriteAllText((Join-Path $privateLogDir "$base.stderr.txt"),
            [string]$result.Stderr,[Text.Encoding]::UTF8)
        [IO.File]::WriteAllText((Join-Path $privateLogDir "$base.status.txt"),
            ("started={0};pid={1};status={2};exit={3};timeout={4};root_stopped={5};tree_stopped={6};cleanup={7}" -f
                $result.Started,$result.Pid,$result.Status,$result.ExitCode,
                $result.TimedOut,$result.RootStopped,$result.TreeStopped,$result.CleanupStatus),[Text.Encoding]::UTF8)
    }

    # Derive the entire immutable qualification identity from the generated
    # flasher plan. Do not rely on the application binary hash alone.
    function Get-QualifiedFlashPlan([string]$root) {
        $jsonPath = Join-Path $root "flasher_args.json"
        $p = [IO.File]::ReadAllText($jsonPath) | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $p.flash_files -or $null -eq $p.write_flash_args -or
            $null -eq $p.extra_esptool_args -or $null -eq $p.flash_settings -or
            [string]$p.extra_esptool_args.chip -cne "esp32s3" -or
            [string]$p.flash_settings.flash_size -cne "8MB" -or
            $p.extra_esptool_args.stub -isnot [bool]) {
            throw "INVALID_FLASH_PLAN"
        }
        $flags = @($p.write_flash_args | ForEach-Object { [string]$_ })
        if ($flags.Count -ne 6) { throw "INVALID_FLASH_FLAGS" }
        $opts = @{}
        for ($i = 0; $i -lt $flags.Count; $i += 2) {
            if ($flags[$i] -cnotin @("--flash_mode","--flash_size","--flash_freq") -or
                $opts.ContainsKey($flags[$i])) { throw "INVALID_FLASH_FLAGS" }
            $opts[$flags[$i]] = $flags[$i+1]
        }
        if ($opts.Count -ne 3 -or $opts["--flash_size"] -cne "8MB" -or
            $opts["--flash_mode"] -cnotin @("dio","dout","qio","qout") -or
            $opts["--flash_freq"] -cnotmatch '^[0-9]{1,3}m$' -or
            $opts["--flash_mode"] -cne [string]$p.flash_settings.flash_mode -or
            $opts["--flash_freq"] -cne [string]$p.flash_settings.flash_freq -or
            [string]$p.extra_esptool_args.before -cnotmatch '^[a-z_]+$' -or
            [string]$p.extra_esptool_args.after -cnotmatch '^[a-z_]+$') {
            throw "INVALID_FLASH_FLAGS"
        }
        $images = @()
        foreach ($entry in $p.flash_files.PSObject.Properties) {
            $offset = [string]$entry.Name
            $relative = [string]$entry.Value
            if ($offset -cnotmatch '^0x[0-9a-fA-F]+$' -or
                $relative -cnotmatch '^(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+[.]bin$') {
                throw "INVALID_FLASH_ENTRY"
            }
            $number = [Convert]::ToInt64($offset.Substring(2),16)
            $full = Join-Path $root $relative.Replace('/', '\')
            $file = Get-Item -LiteralPath $full -ErrorAction Stop
            if ($file.PSIsContainer -or $file.Length -le 0 -or
                ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "INVALID_FLASH_IMAGE"
            }
            $images += [pscustomobject]@{
                Offset=$number; OffsetText=("0x{0:x}" -f $number)
                Relative=$relative; Path=$full; Size=$file.Length
                Sha=(Get-FileHash -LiteralPath $full -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
            }
        }
        $images = @($images | Sort-Object Offset)
        if ($images.Count -lt 3 -or
            @($images | Where-Object Relative -eq "bootloader/bootloader.bin").Count -ne 1 -or
            @($images | Where-Object Relative -eq "partition_table/partition-table.bin").Count -ne 1 -or
            @($images | Where-Object Relative -eq "m5daylog.bin").Count -ne 1) {
            throw "REQUIRED_FLASH_IMAGE_MISSING"
        }
        $end = [int64]0
        $rows = @()
        $filePairs = @()
        foreach ($image in $images) {
            if ($image.Offset -lt $end -or
                ($image.Offset + $image.Size) -gt (8*1024*1024)) {
                throw "FLASH_OFFSETS_INVALID"
            }
            $end = $image.Offset + $image.Size
            $rows += ("{0}|{1}|{2}" -f $image.OffsetText,$image.Relative,$image.Sha)
            $filePairs += $image.OffsetText
            $filePairs += $image.Path
        }
        # Canonical identity: sorted offset|relative-file|sha256, LF terminated.
        $canonical = ($rows -join [char]10) + [char]10
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            $bytes = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical))
            $imageSetDigest = ([BitConverter]::ToString($bytes)).Replace("-","").ToLowerInvariant()
        }
        finally { $sha256.Dispose() }
        [pscustomobject]@{
            PlanHash=(Get-FileHash -LiteralPath $jsonPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
            ImageHash=$imageSetDigest; Count=$images.Count
            Flags=$flags; Pairs=$filePairs
            ImagePaths=@($images | ForEach-Object { $_.Path })
            Before=[string]$p.extra_esptool_args.before
            After=[string]$p.extra_esptool_args.after
            Stub=[bool]$p.extra_esptool_args.stub
        }
    }

    function Assert-QualifiedPort {
        $ports=@(Get-PnpDevice -Class Ports -PresentOnly -ErrorAction Stop |
            Where-Object { [string]$_.FriendlyName -match ('\(' + [regex]::Escape($serialPort) + '\)$') })
        if ($ports.Count -ne 1 -or
            -not ([string]$ports[0].InstanceId).Equals($expectedPortInstanceId,
                [StringComparison]::OrdinalIgnoreCase)) {
            throw "PORT_IDENTITY_MISSING_MISMATCH_OR_AMBIGUOUS"
        }
    }

    try {
        foreach ($required in @($repoRoot, $idfRoot, $idfTools, $python, $idfPy, $privateLogDir, $firmwareRoot,
                                (Join-Path $firmwareRoot "CMakeLists.txt"), $defaults, $qualifiedSdkconfig, $app)) {
            if (-not (Test-Path -LiteralPath $required)) {
                $diagnosis = "REQUIRED_PATH_MISSING"
                throw $diagnosis
            }
        }
        $invalidHash = $false
        foreach ($digest in @($expectedDefaultsSha256, $expectedConfigSha256, $expectedAppSha256, $expectedPlanSha256, $expectedImageSetSha256)) {
            if ($digest -notmatch '^[a-fA-F0-9]{64}$') { $invalidHash = $true }
        }
        if ($invalidHash -or $serialPort -notmatch '^COM[1-9][0-9]*$' -or
            $expectedDeviceMac -notmatch '^(?:[a-fA-F0-9]{2}:){5}[a-fA-F0-9]{2}$' -or
            [string]::IsNullOrWhiteSpace($expectedPortInstanceId) -or
            $expectedPortInstanceId.StartsWith("<")) {
            $diagnosis = "FLASH_IDENTITY_INPUT_MISSING"
            throw $diagnosis
        }
        $head = ([string](& git -C $repoRoot rev-parse HEAD)).Trim()
        $gitExit = $LASTEXITCODE
        if ($gitExit -ne 0 -or $head -ne $expectedHead) {
            $diagnosis = "HEAD_MISMATCH_OR_GIT_FAILURE"
            throw $diagnosis
        }
        $tracked = @(& git -C $repoRoot status --porcelain --untracked-files=no)
        $gitExit = $LASTEXITCODE
        if ($gitExit -ne 0 -or $tracked.Count -ne 0) {
            $diagnosis = "TRACKED_WORKTREE_NOT_CLEAN"
            throw $diagnosis
        }
        foreach ($name in @("SDKCONFIG", "SDKCONFIG_DEFAULTS", "IDF_TARGET", "IDF_PRESET", "EXTRA_COMPONENT_DIRS")) {
            if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name))) {
                $diagnosis = "AMBIENT_BUILD_OVERRIDE"
                throw $diagnosis
            }
        }
        $beforeDefaultsHash = (Get-FileHash -LiteralPath $defaults -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        $beforeConfigHash = (Get-FileHash -LiteralPath $qualifiedSdkconfig -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        if ($beforeDefaultsHash -ne $expectedDefaultsSha256.ToLowerInvariant()) {
            $diagnosis = "DEFAULTS_HASH_MISMATCH_BEFORE_FLASH"
            throw $diagnosis
        }
        if ($beforeConfigHash -ne $expectedConfigSha256.ToLowerInvariant()) {
            $diagnosis = "CONFIG_HASH_MISMATCH_BEFORE_FLASH"
            throw $diagnosis
        }
        $beforeHash = (Get-FileHash -LiteralPath $app -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        if ($beforeHash -ne $expectedAppSha256.ToLowerInvariant()) {
            $diagnosis = "APP_HASH_MISMATCH_BEFORE_FLASH"
            throw $diagnosis
        }
        $diagnosis = "FLASH_PLAN_INVALID_BEFORE_FLASH"
        $qualifiedPlan = Get-QualifiedFlashPlan $qualifiedBuildDir
        $beforePlanHash = $qualifiedPlan.PlanHash
        $beforeImageHash = $qualifiedPlan.ImageHash
        $imageCount = $qualifiedPlan.Count
        if ($beforePlanHash -ne $expectedPlanSha256.ToLowerInvariant() -or
            $beforeImageHash -ne $expectedImageSetSha256.ToLowerInvariant()) {
            $diagnosis = "FLASH_PLAN_OR_IMAGE_MISMATCH_BEFORE_FLASH"
            throw $diagnosis
        }
        # On Windows, Read sharing without Write/Delete sharing prevents
        # concurrent modifications while esptool opens these same files.
        # Re-hash after acquiring every lock, before ANY Device mutation.
        $diagnosis = "FLASH_SNAPSHOT_LOCK_FAILED"
        $lockCandidates = @($defaults, $qualifiedSdkconfig, $app,
                            (Join-Path $qualifiedBuildDir "flasher_args.json")) +
                          @($qualifiedPlan.ImagePaths)
        foreach ($filePath in @($lockCandidates | Select-Object -Unique)) {
            $readLocks += [IO.File]::Open($filePath, [IO.FileMode]::Open,
                                         [IO.FileAccess]::Read, [IO.FileShare]::Read)
        }
        $diagnosis = "FLASH_SNAPSHOT_DRIFT_BEFORE_WRITE"
        $lockedPlan = Get-QualifiedFlashPlan $qualifiedBuildDir
        if ($lockedPlan.PlanHash -ne $expectedPlanSha256.ToLowerInvariant() -or
            $lockedPlan.ImageHash -ne $expectedImageSetSha256.ToLowerInvariant() -or
            (Get-FileHash -LiteralPath $defaults -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -ne $expectedDefaultsSha256.ToLowerInvariant() -or
            (Get-FileHash -LiteralPath $qualifiedSdkconfig -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -ne $expectedConfigSha256.ToLowerInvariant() -or
            (Get-FileHash -LiteralPath $app -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant() -ne $expectedAppSha256.ToLowerInvariant()) {
            throw $diagnosis
        }
        $qualifiedPlan = $lockedPlan

        $env:IDF_PATH = $idfRoot
        $env:IDF_TOOLS_PATH = $idfTools
        $env:PATH = "$(Split-Path -Parent $python);$env:PATH"
        . (Join-Path $idfRoot "export.ps1")
        $versionLines = @(& $python $idfPy --version)
        $idfExit = $LASTEXITCODE
        $idfVersion = ($versionLines -join " ").Trim()
        if ($idfExit -ne 0 -or $idfVersion -ne "ESP-IDF v5.5.5") {
            $diagnosis = "UNSUPPORTED_OR_UNRESOLVED_ESP_IDF"
            throw $diagnosis
        }

        # Match independent operator-local identity before any mutating write.
        $diagnosis="PORT_IDENTITY_MISSING_MISMATCH_OR_AMBIGUOUS"
        Assert-QualifiedPort
        $pnpMatch=$true
        $diagnosis="DEVICE_MAC_READ_FAILED"
        $identityStarted="UNKNOWN"
        $identityPid="UNKNOWN"
        $identityStatus="UNKNOWN"
        $diagnosis="DEVICE_IDENTITY_WRAPPER_EXCEPTION_NO_FLASH"
        $idResult=Invoke-BoundedPython @("-m","esptool","--chip","esp32s3",
            "-p",$serialPort,"--before","no_reset","--after","no_reset","read_mac") $qualifiedBuildDir $identityDeadlineSeconds
        $identityStarted=$idResult.Started
        $identityPid=$idResult.Pid
        $identityStatus=$idResult.Status
        $identityRootStopped=$idResult.RootStopped
        $identityCleanupStatus=$idResult.CleanupStatus
        $identityExit=$idResult.ExitCode
        $diagnosis="DEVICE_IDENTITY_EVIDENCE_SAVE_FAILED_NO_FLASH"
        Save-PrivateProcessEvidence "identity" $idResult
        if ($idResult.TimedOut) { $diagnosis="DEVICE_IDENTITY_TIMEOUT_NO_FLASH";throw $diagnosis }
        if ($identityStatus -ne "EXITED") {
            $diagnosis="DEVICE_IDENTITY_PROCESS_INDETERMINATE_NO_FLASH"
            throw $diagnosis
        }
        if ($identityExit -ne 0) { $diagnosis="DEVICE_MAC_READ_FAILED";throw $diagnosis }
        # esptool may print one MAC both during connect and read_mac.
        $observed=@([regex]::Matches($idResult.Stdout,
            '(?im)^\s*MAC:\s*((?:[a-f0-9]{2}:){5}[a-f0-9]{2})\s*$') |
            ForEach-Object { $_.Groups[1].Value.ToLowerInvariant() } | Select-Object -Unique)
        if ($observed.Count -ne 1 -or
            $observed[0] -ne $expectedDeviceMac.ToLowerInvariant()) {
            $diagnosis="DEVICE_MAC_MISSING_MISMATCH_OR_AMBIGUOUS";throw $diagnosis
        }
        $macMatch=$true
        $diagnosis="PORT_IDENTITY_CHANGED_AFTER_READ"
        Assert-QualifiedPort
        $diagnosis="PORT_IDENTITY_CHANGED_BEFORE_WRITE"
        Assert-QualifiedPort

        Push-Location -LiteralPath $firmwareRoot -ErrorAction Stop
        $enteredFirmware = $true
        # No implicit build, and no unchecked @flash_args file.
        $flashArgs = @("--chip","esp32s3","-p",$serialPort,
                  "--before",$qualifiedPlan.Before,"--after",$qualifiedPlan.After)
        if (-not $qualifiedPlan.Stub) { $flashArgs += "--no-stub" }
        $flashArgs += "write_flash"
        $flashArgs += @($qualifiedPlan.Flags)
        $flashArgs += @($qualifiedPlan.Pairs)
        Push-Location -LiteralPath $qualifiedBuildDir -ErrorAction Stop
        try {
            $flashAttempted=$true
            $flashWriteState="WRITE_STATE_UNKNOWN"
            $flashStarted="UNKNOWN"
            $flashPid="UNKNOWN"
            $flashStatus="UNKNOWN"
            $flashRootStopped="UNKNOWN"
            $flashCleanupStatus="UNKNOWN"
            $diagnosis="FLASH_WRAPPER_EXCEPTION_WRITE_STATE_UNKNOWN_NO_RETRY"
            $flashResult=Invoke-BoundedPython (@("-m","esptool") + $flashArgs) $qualifiedBuildDir $flashDeadlineSeconds
            $flashStarted=$flashResult.Started
            $flashPid=$flashResult.Pid
            $flashStatus=$flashResult.Status
            $flashRootStopped=$flashResult.RootStopped
            $flashCleanupStatus=$flashResult.CleanupStatus
            $flashExit=$flashResult.ExitCode
            $flashTimedOut=$flashResult.TimedOut
            $flashTreeStopped=$flashResult.TreeStopped
            $diagnosis="FLASH_PRIVATE_EVIDENCE_SAVE_FAILED_WRITE_STATE_UNKNOWN_NO_RETRY"
            Save-PrivateProcessEvidence "flash" $flashResult
        } finally { Pop-Location -ErrorAction Stop }
        if (-not $flashStarted) {
            $flashWriteState="NOT_STARTED"
            $diagnosis="FLASH_PROCESS_NOT_STARTED_NO_WRITE"
            throw $diagnosis
        }
        if ($flashTimedOut) {
            $diagnosis="FLASH_TIMEOUT_WRITE_STATE_UNKNOWN_NO_RETRY";throw $diagnosis
        }
        if ($flashStatus -ne "EXITED") {
            $diagnosis="FLASH_CHILD_INDETERMINATE_WRITE_STATE_UNKNOWN_NO_RETRY"
            throw $diagnosis
        }
        if ($flashExit -ne 0) {
            $diagnosis="FLASH_FAILED_WRITE_STATE_UNKNOWN_NO_RETRY";throw $diagnosis
        }
        $flashWriteState="WRITE_COMMAND_COMPLETED"
        $afterHash = (Get-FileHash -LiteralPath $app -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        $afterConfigHash = (Get-FileHash -LiteralPath $qualifiedSdkconfig -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        $afterDefaultsHash = (Get-FileHash -LiteralPath $defaults -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        $diagnosis = "FLASH_PLAN_INVALID_AFTER_FLASH"
        $postPlan = Get-QualifiedFlashPlan $qualifiedBuildDir
        $afterPlanHash = $postPlan.PlanHash
        $afterImageHash = $postPlan.ImageHash
        if ($afterHash -ne $beforeHash -or $afterConfigHash -ne $beforeConfigHash -or
            $afterDefaultsHash -ne $beforeDefaultsHash -or
            $afterPlanHash -ne $beforePlanHash -or
            $afterImageHash -ne $beforeImageHash) {
            $diagnosis = "QUALIFIED_ARTIFACT_CHANGED_DURING_FLASH"
            throw $diagnosis
        }

        $diagnosis = "OK"
        $disposition = "PASS"
    }
    catch {
        # A failed or ambiguous flash is BLOCKED; requalify Device before any cycle.
    }
    finally {
        foreach ($stream in $readLocks) {
            try { $stream.Dispose() }
            catch { $diagnosis = "FLASH_LOCK_CLEANUP_FAILED"; $disposition = "BLOCKED" }
        }
        if ($enteredFirmware) {
            try { Pop-Location -ErrorAction Stop }
            catch { $diagnosis = "LOCATION_RESTORE_FAILED"; $disposition = "BLOCKED" }
        }
        Write-Host ""
        Write-Host "==== $phase ===="
        Write-Host ("expected_head={0}" -f $expectedHead)
        Write-Host ("head={0}" -f $head)
        Write-Host ("git_exit={0}" -f $gitExit)
        Write-Host ("idf_exit={0}" -f $idfExit)
        Write-Host ("idf_version={0}" -f $idfVersion)
        Write-Host ("flash_exit={0}" -f $flashExit)
        Write-Host ("identity_exit={0}" -f $identityExit)
        Write-Host ("identity_child_started={0}" -f $identityStarted)
        Write-Host ("identity_child_pid={0}" -f $identityPid)
        Write-Host ("identity_child_status={0}" -f $identityStatus)
        Write-Host ("identity_root_stopped={0}" -f $identityRootStopped)
        Write-Host ("identity_cleanup_status={0}" -f $identityCleanupStatus)
        Write-Host ("qualified_pnp_match={0}" -f $pnpMatch)
        Write-Host ("qualified_mac_match={0}" -f $macMatch)
        Write-Host ("identity_deadline_seconds={0}" -f $identityDeadlineSeconds)
        Write-Host ("flash_deadline_seconds={0}" -f $flashDeadlineSeconds)
        Write-Host ("flash_attempted={0}" -f $flashAttempted)
        Write-Host ("flash_timed_out={0}" -f $flashTimedOut)
        Write-Host ("flash_tree_stopped={0}" -f $flashTreeStopped)
        Write-Host ("flash_child_started={0}" -f $flashStarted)
        Write-Host ("flash_child_pid={0}" -f $flashPid)
        Write-Host ("flash_child_status={0}" -f $flashStatus)
        Write-Host ("flash_root_stopped={0}" -f $flashRootStopped)
        Write-Host ("flash_cleanup_status={0}" -f $flashCleanupStatus)
        Write-Host ("flash_write_state={0}" -f $flashWriteState)
        Write-Host ("expected_defaults_sha256={0}" -f $expectedDefaultsSha256)
        Write-Host ("expected_effective_sdkconfig_sha256={0}" -f $expectedConfigSha256)
        Write-Host ("expected_app_sha256={0}" -f $expectedAppSha256)
        Write-Host ("expected_flasher_args_sha256={0}" -f $expectedPlanSha256)
        Write-Host ("expected_flash_image_set_sha256={0}" -f $expectedImageSetSha256)
        Write-Host ("before_defaults_sha256={0}" -f $beforeDefaultsHash)
        Write-Host ("before_effective_sdkconfig_sha256={0}" -f $beforeConfigHash)
        Write-Host ("after_defaults_sha256={0}" -f $afterDefaultsHash)
        Write-Host ("after_effective_sdkconfig_sha256={0}" -f $afterConfigHash)
        Write-Host ("before_app_sha256={0}" -f $beforeHash)
        Write-Host ("after_app_sha256={0}" -f $afterHash)
        Write-Host ("before_flasher_args_sha256={0}" -f $beforePlanHash)
        Write-Host ("after_flasher_args_sha256={0}" -f $afterPlanHash)
        Write-Host ("before_flash_image_set_sha256={0}" -f $beforeImageHash)
        Write-Host ("after_flash_image_set_sha256={0}" -f $afterImageHash)
        Write-Host ("flash_image_count={0}" -f $imageCount)
        Write-Host ("diagnosis={0}" -f $diagnosis)
        Write-Host ("disposition={0}" -f $disposition)
        Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase, $diagnosis, $disposition)
        Write-Host ""
    }
}
~~~

Negative verification (host-only disposable worktree, no connected Device):

I-05 host-only parser tests: feed simulated absent, duplicate, mismatched PnP records and missing/ambiguous/mismatched MAC lines. Verify every case stops before launching the writer. Never claim a real physical MAC check was performed by this test. I-06/I-06R host-only mock-child tests (no connected Device): use a non-mutating child with a shortened deadline and separately redirected high-volume stdout/stderr. Assert STARTED/PID, TIMEOUT, UNKNOWN exit, bounded cleanup, BLOCKED and zero writer retries. Inject failure to start taskkill after a child has started and timed out; require TIMEOUT, TASKKILL_EXCEPTION (or equivalent), tree_stopped=false, retained PID and explicit independent-absence verification before another attempt. Inject failure in ReadToEndAsync / WaitForExit or output-drain completion after a successful child start; require POST_START_EXCEPTION / OUTPUT_DRAIN_TIMEOUT, UNKNOWN exit unless reliably observed, and a truthful BLOCKED diagnosis. Finally, deliberately reject local evidence-file saving after a completed mock child; no PASS may be claimed for the resulting flash-equivalent phase. These are negative-test instructions, not executed test results.



1. Place an intentionally stale ignored firmware/sdkconfig containing CONFIG_TINYUSB_MSC_ENABLED=n. HG-BUILD must use only the isolated build/hg-qualified/sdkconfig generated from tracked defaults, prove CONFIG_TINYUSB_MSC_ENABLED=y and leave the stale root sdkconfig untouched. A default-directory fullclean/build is not qualified.
2. Re-run HG-BUILD without retiring the isolated output. Expected: QUALIFICATION_OUTPUT_ALREADY_EXISTS; build_exit=NOT_RUN; disposition=BLOCKED; no overwrite of existing output.
3. Alter the isolated effective sdkconfig after a passing HG-BUILD; evaluate HG-FLASH prerequisites without connecting the target. Expected: CONFIG_HASH_MISMATCH_BEFORE_FLASH; flash_exit=NOT_RUN; disposition=BLOCKED. Do not force a flash to complete a negative test.
4. In a disposable qualified build, modify `partition_table/partition-table.bin` or `bootloader/bootloader.bin` but leave the app/default/configuration hashes unchanged. HG-FLASH preflight must report `FLASH_PLAN_OR_IMAGE_MISMATCH_BEFORE_FLASH`, `flash_exit=NOT_RUN`, and `disposition=BLOCKED`. Never attach the target for this negative check.
5. Modify `flasher_args.json` offset/options with all binaries intact: the plan digest must fail before any write. Independently verify by inspection that the flash packet invokes only `python -m esptool ... write_flash` and **never** calls the implicit-build `idf.py flash` action. These are specified host-only tests, not claims of executed physical acceptance.

Do not use erase-flash as a convenience step in a Human Gate. A flash failure, rebuild/hash mismatch, or missing accepted flash result is **not** evidence that the intended firmware is running. Do not count an acceptance cycle until the exact runtime identity and baseline are re-established.

After flash, fixture restore, ROM-mode diagnostic, NVS read, host reboot, or any operation that can reset/re-enumerate the Device, establish a fresh runtime baseline before counting a cycle.

A qualified USB publication baseline should prove all applicable items:

- exactly one intended USB mass-storage target;
- FAT32 filesystem;
- expected M5DAYLOG directory structure and manifest are readable;
- exactly one intended CDC interface;
- manifest state is internally consistent;
- no stale .part file relevant to the cycle;
- current exact revision/build identity is already established.

Do not identify the target only by a reused drive letter or by the appearance of a new PnP identity.

## 5. PowerShell packet pattern

For every emitted PowerShell Human Gate instruction, use one outer `& { ... }` block. Gather all decision-relevant output in one final section **after the work**, preceded and followed by an empty line. The first result line must be exactly `==== $phase ====`. Long-running operations should emit bounded `PROGRESS phase=...` lines while the operation is active, without confusing progress with acceptance. The final result must contain meaningful keyed observations plus a single `SUMMARY`. Never print the worktree `.env` contents, raw NVS, private paths, PIDs, serial or Device identifiers into public evidence.

Every acceptance phase should follow this structure:

~~~powershell
& {
    $phase = "<phase>"
    $expectedHead = "<revision>"
    $continue = $true
    $diagnosis = "UNKNOWN"
    $disposition = "BLOCKED"

    try {
        # prerequisite observation
        # one intended mutation / physical action
        # bounded observation
        # acceptance-specific validation
    }
    catch {
        # classify tooling/harness vs product result
    }
    finally {
        # guarded cleanup only
    }

    Write-Host ""
    Write-Host "==== $phase ===="
    Write-Host ("expected_head={0}" -f $expectedHead)
    Write-Host ("diagnosis={0}" -f $diagnosis)
    Write-Host ("disposition={0}" -f $disposition)
    Write-Host (
        "SUMMARY phase={0} diagnosis={1} disposition={2}" -f
        $phase, $diagnosis, $disposition
    )
    Write-Host ""
}
~~~

Avoid early return paths that skip the common summary.

## 6. D-031 canonical release sequence

The successful Windows-side order is:

1. Revalidate the exact intended Device/volume.
2. Stop new host-side Device I/O and close application-owned volume handles.
3. Open the exact volume for control.
4. FSCTL_LOCK_VOLUME succeeds.
5. While the lock is held, FSCTL_DISMOUNT_VOLUME succeeds.
6. Only then open/use the dedicated CDC release lease.
7. Send exactly one RELEASE_STORAGE request with a fresh request id and releaseAttemptId.
8. Require the matching accepted=true response.
9. Do not send another RELEASE_STORAGE in that session.
10. Observe bounded complete teardown.
11. Continue to physical removal / manual WAKE only after the required release/teardown proof is present.

Windows control values used by the successful harness:

- desired access: 0xC0000000
- share mode: 3
- FSCTL_LOCK_VOLUME: 0x00090018
- FSCTL_DISMOUNT_VOLUME: 0x00090020

Canonical request shape:

~~~json
{"id":"<request-id>","cmd":"RELEASE_STORAGE","args":{"releaseAttemptId":"<attempt-id>"}}
~~~

Canonical success fields:

~~~text
ok=true
result.releaseAttemptId=<attempt-id>
result.accepted=true
~~~

The release request is non-repeatable within the same attempt. If response delivery is accepted and later serial close/dispose reports an error, preserve the accepted response and evaluate teardown independently.

## 7. Efficient orderly lifecycle cycle

Start from one qualified MSC + CDC baseline.

Recommended cycle:

1. Snapshot baseline manifest count / identity and relevant recording state.
2. Perform exact volume lock + dismount.
3. Send RELEASE_STORAGE exactly once.
4. Validate matching accepted response.
5. Observe complete teardown with a bounded deadline.
6. Physically remove USB.
7. With USB still removed, hold WAKE for 3 seconds, then release it.
8. Allow a recording-evidence dwell before reconnect. A 180-second dwell was a practical stable value during the corrective Human Gate; this is harness timing, not a product requirement.
9. Reconnect USB without pressing WAKE/RESET/BOOT.
10. Wait for qualified MSC + CDC publication.
11. Verify exactly one new finalized manifest entry.
12. Verify WAV exists, matching .part is absent, size matches, SHA-256 matches, duration matches, and recording is non-empty.
13. Count the cycle only after the full required observation is valid.

A reconnect that occurs too soon after WAKE can finalize a 44-byte / 0-ms WAV before useful PCM is captured. Treat that as insufficient fresh-recording evidence, not automatically as a release/teardown defect.

## 8. Efficient accidental pre-release removal cycle

Start from a qualified baseline and capture:

- manifest entry count;
- manifest SHA-256;
- recording-file count;
- deterministic recording-file snapshot hash based on relative filename + size;
- .part count.

Recommended sequence:

1. Do not safe-remove and do not send RELEASE_STORAGE.
2. Physically unplug USB.
3. Keep the Device detached for the bounded matrix interval.
4. Reconnect without WAKE/RESET/BOOT.
5. Wait for qualified re-publication.
6. Prove the manifest hash/count and recording-file snapshot are unchanged.
7. Prove no new .part and no automatic recording/remount occurred.
8. Only after that fail-closed proof, execute the canonical lock + dismount + RELEASE_STORAGE sequence.
9. Observe complete teardown.
10. Physically remove USB.
11. Hold WAKE for 3 seconds while USB remains removed.
12. Use the recording-evidence dwell.
13. Reconnect and obtain the fresh-recording integrity proof.

If volume lock fails before RELEASE_STORAGE is sent, preserve the already-valid accidental-pull snapshot. Close host consumers such as file-browser windows and retry only the lock/dismount/release recovery phase when the snapshot is still unchanged. Do not repeat the earlier physical pull solely to obtain a cleaner wrapper result.

## 9. Failure and recovery decision table

| Observation | Classification / action |
|---|---|
| HEAD mismatch | STOP; no acceptance action |
| Qualified baseline missing | BLOCKED; repair baseline first |
| Volume lock fails and request_sent=false | Host/harness BLOCKED; close host handles and bounded-retry the release phase only |
| Dismount fails and request_sent=false | BLOCKED; do not send RELEASE_STORAGE |
| RELEASE_STORAGE may have been sent | Never resend in that attempt |
| Matching accepted response + complete teardown, but serial close fails | Serial cleanup issue; do not overturn independently valid product evidence |
| Accepted response but teardown does not complete during a valid full observation window | Product failure for the required teardown path; no release retry |
| Manual WAKE was omitted or operator is unsure it occurred | Required sequence not proven: counted=false; disposition=BLOCKED if attempted without valid preconditions, or NOT_RUN if the required phase was never attempted; not product FAIL |
| Fresh WAV is 44 bytes / 0 ms after otherwise valid recovery | Fresh-recording evidence insufficient; do not infer release failure; use a longer approved evidence dwell |
| Read-only NVS/flash diagnostic was run | Runtime continuity is broken; establish a fresh application baseline before continuing |
| Wrapper/parser verdict contradicts immutable raw observations | Correct the harness verdict from preserved evidence; do not repeat physical actions solely for formatting |

## 10. Efficiency rules

- Perform environment/toolchain qualification once per host session, not once per physical cycle.
- Perform exact-head build/flash once per exact revision unless the Device firmware or test fixture must actually change.
- After a PC reboot, first re-establish Git/IDF environment and observe the Device baseline. Do not reflexively rebuild/reflash.
- Keep file-browser windows and other consumers closed against the Device volume before lock/dismount phases.
- Use one complete PowerShell packet per cycle with Japanese ACTION= / WAIT= prompts and Enter as the synchronization boundary.
- Keep the cycle number, baseline manifest count, and rolling PASS count in the summary.
- Increase the recording-evidence dwell rather than repeatedly generating invalid 44-byte/0-ms attempts.
- Snapshot before interruption tests so a later host-side BLOCKED recovery can resume without repeating already-valid physical evidence.
- Never convert fixture repair, diagnostic reboot, flash, or baseline recovery into an acceptance cycle.
- Prefer observation-only follow-up after any accepted release.

## 11. Durable evidence fields

For each counted lifecycle cycle, retain at minimum:

~~~text
exact_head
cycle_id
baseline_manifest_count
release_lock_succeeded
release_dismount_succeeded
request_sent
response_accepted
release_retry_count
teardown_observed
physical_usb_removal_confirmed
manual_wake_confirmed
publication_observed
final_manifest_delta
last_state
wav_present
wav_part_absent
size_match
sha256_match
duration_match
nonempty_recording
filesystem_corruption_observed
dual_ownership_observed
silent_success_observed
diagnosis
disposition
~~~

For accidental pre-release cycles additionally retain:

~~~text
baseline_manifest_sha256
baseline_file_snapshot_sha256
post_pull_manifest_sha256
post_pull_file_snapshot_sha256
manifest_unchanged_after_pull
files_unchanged_after_pull
no_part_after_pull
no_automatic_recording_after_pull
~~~

Publish only sanitized structured evidence. Rich local logs stay local unless separately reduced to public-safe evidence.

## 12. Historical qualification from #87

The corrective #87 Human Gate demonstrated that this operating model can produce stable evidence, including:

- a 10-cycle orderly lifecycle corrective matrix;
- a 5-cycle accidental pre-release fail-closed matrix;
- exactly-once canonical release attempts;
- safe recovery from a pre-release Windows volume-lock BLOCKED condition without repeating preserved physical evidence;
- correction of an invalid operator sequence where manual WAKE was uncertain;
- separation of post-response serial cleanup behavior from independently observed release/teardown success.

Those historical results apply only to their exact tested revision. The operational lessons in this runbook are reusable; the acceptance evidence itself is not automatically reusable after a revision change.

## 13. Before the next Human Gate

Before giving the operator any command:

1. read the current Issue / Spec / Decision and determine the exact matrix row;
2. read all required files under .agent/HUMAN_GATE;
3. confirm the current PR HEAD and prerequisite Review/Security/CI state;
4. identify whether the host session needs only environment activation or also build/flash;
5. choose the smallest phase that can produce one unambiguous observation;
6. decide in advance which failures permit observation-only continuation and which require a fresh baseline;
7. serialize only public-safe durable evidence and validate it before publication.

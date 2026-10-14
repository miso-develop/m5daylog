& {
    $phase="HG-HOST-SAFETY-TEST";$diagnosis="HOST_FIXTURE_EXCEPTION";$disposition="BLOCKED"
    $passed=0;$total=3;$temp=$null;$saved=@{};$mock=$null
    $fixtureStage="SETUP"
    $keys=@("HG_EXPECTED_HEAD","HG_PYTHON","HG_SERIAL_PORT","HG_DEVICE_MAC",
        "HG_PNP_INSTANCE_ID","HG_NVS_OFFSET","HG_NVS_SIZE",
        "HG_NVS_PLAN_SHA256","HG_NVS_PARTITION_SHA256")
    try {
        . (Join-Path $PSScriptRoot "common.ps1")
        foreach($key in $keys) { $saved[$key]=[Environment]::GetEnvironmentVariable($key,"Process") }
        $temp=Join-Path ([IO.Path]::GetTempPath()) ("hg-safety-"+[guid]::NewGuid().ToString("N"))
        [void][IO.Directory]::CreateDirectory($temp)
        $fixtureStage="ENV"
        $envFile=Join-Path $temp ".env"
        $lines=@($keys | ForEach-Object { "$_=" })
        [IO.File]::WriteAllLines($envFile,@($lines | Where-Object { $_ -cnotmatch '^HG_NVS_OFFSET=' }))
        $env:HG_NVS_OFFSET="0x12345"
        $blocked=$false
        try { Import-HgEnv $temp } catch { $blocked=$_.Exception.Message -eq "MISSING_ENV_KEY" }
        if (-not $blocked -or $env:HG_NVS_OFFSET) { throw "ENV_STALE_REUSE" }
        [IO.File]::WriteAllLines($envFile,$lines)
        Import-HgEnv $temp
        if ($env:HG_NVS_OFFSET) { throw "ENV_EMPTY_VALUE_REUSE" }
        $passed++
        $fixtureStage="NVS"
        $build=Join-Path $temp "firmware\build\hg-qualified"
        $partDir=Join-Path $build "partition_table"
        [void][IO.Directory]::CreateDirectory($partDir)
        $plan=Join-Path $build "flasher_args.json"
        $table=Join-Path $partDir "partition-table.bin"
        $bytes=New-Object byte[] 4096
        for($i=0;$i -lt 4096;$i++) { $bytes[$i]=255 }
        $bytes[0]=0xAA;$bytes[1]=0x50;$bytes[2]=1;$bytes[3]=2
        [BitConverter]::GetBytes([uint32]0x9000).CopyTo($bytes,4)
        [BitConverter]::GetBytes([uint32]0x6000).CopyTo($bytes,8)
        # A partition entry has a 16-byte NUL-padded label, not 0xFF padding.
        for($i=12;$i -lt 28;$i++) { $bytes[$i]=0 }
        [Text.Encoding]::ASCII.GetBytes("nvs").CopyTo($bytes,12)
        [IO.File]::WriteAllBytes($table,$bytes)
        [IO.File]::WriteAllText($plan,'{"flash_files":{"0x8000":"partition_table/partition-table.bin"}}')
        $env:HG_NVS_PLAN_SHA256=(Get-FileHash -LiteralPath $plan -Algorithm SHA256).Hash
        $env:HG_NVS_PARTITION_SHA256=(Get-FileHash -LiteralPath $table -Algorithm SHA256).Hash
        $good=Assert-HgNvsPartition $temp 0x9000 0x6000
        if (-not $good.PlanMatched) { throw "PARTITION_VALID_CASE" }
        $blocked=$false
        try { $null=Assert-HgNvsPartition $temp 0xA000 0x6000 } catch {
            $blocked=$_.Exception.Message -eq "NVS_PARTITION_OFFSET_SIZE_MISMATCH"
        }
        if (-not $blocked) { throw "PARTITION_WRONG_IN_BOUNDS_REGION" }
        $env:HG_NVS_PLAN_SHA256=$null
        $blocked=$false
        try { $null=Assert-HgNvsPartition $temp 0x9000 0x6000 } catch {
            $blocked=$_.Exception.Message -eq "QUALIFIED_PARTITION_EVIDENCE_REQUIRED"
        }
        if (-not $blocked) { throw "PARTITION_MISSING_EVIDENCE" }
        $passed++
        $fixtureStage="MOCK_CHILD"
        $ping=Join-Path $env:SystemRoot "System32\PING.EXE"
        if (-not [IO.File]::Exists($ping)) { throw "MOCK_PING_UNAVAILABLE" }
        try {
            $mock=Invoke-HgNative $ping @("-n","10","127.0.0.1") $temp 1 $phase (Join-Path $temp "missing-taskkill.exe")
            if (-not $mock.Started -or -not $mock.TimedOut -or
                $mock.Status -ne "TIMEOUT" -or [string]$mock.ExitCode -ne "UNKNOWN" -or
                $mock.TreeStopped -eq $true -or $mock.Cleanup -ne "TASKKILL_EXCEPTION") {
                throw "MOCK_CHILD_RESULT_INVALID"
            }
            Save-HgPrivate $temp $phase $mock
            $receipt=Get-ChildItem -LiteralPath (Join-Path $temp ".local\HumanGate") -Filter "*.status.txt" -File | Select-Object -First 1
            if ($null -eq $receipt) { throw "MOCK_RECEIPT_MISSING" }
            $text=[IO.File]::ReadAllText($receipt.FullName)
            if (-not ($text.Contains("pid=") -and $text.Contains("root_exited=") -and
                $text.Contains("deadline_seconds=1") -and $text.Contains("cleanup=TASKKILL_EXCEPTION"))) {
                throw "MOCK_RECEIPT_INCOMPLETE"
            }
            $passed++
        } finally {
            # The bounded read-only ping mock ends on its own. Never blindly
            # kill a PID here: a completed child ID could already be reused.
            if ($null -ne $mock -and $mock.Started -and [string]$mock.Pid -match '^[0-9]+$') {
                try {
                    $candidate=Get-Process -Id ([int]$mock.Pid) -ErrorAction SilentlyContinue
                    if ($null -ne $candidate -and $candidate.ProcessName -ieq "PING") {
                        Wait-Process -Id ([int]$mock.Pid) -Timeout 30 -ErrorAction SilentlyContinue
                    }
                } catch {}
            }
        }
        $fixtureStage="COMPLETE"
        $diagnosis="HOST_NEGATIVE_FIXTURES_COMPLETE";$disposition="PASS"
    } catch { $diagnosis="HOST_FIXTURE_FAILED" }
    finally {
        foreach ($key in $keys) { [Environment]::SetEnvironmentVariable($key,$saved[$key],"Process") }
        if ($null -ne $temp -and [IO.Directory]::Exists($temp) -and
            [IO.Path]::GetFileName($temp) -match '^hg-safety-[a-f0-9]{32}$') {
            try { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction Stop }
            catch { $diagnosis="HOST_FIXTURE_CLEANUP_FAILED";$disposition="BLOCKED" }
        }
        Write-Host ""
        Write-Host "==== $phase ===="
        Write-Host ("fixtures_passed={0}" -f $passed)
        Write-Host ("fixtures_total={0}" -f $total)
        Write-Host ("fixture_stage={0}" -f $fixtureStage)
        Write-Host "device_access=False"
        Write-Host "cycles_counted=0"
        Write-Host ("diagnosis={0}" -f $diagnosis)
        Write-Host ("disposition={0}" -f $disposition)
        Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase,$diagnosis,$disposition)
        Write-Host ""
    }
}

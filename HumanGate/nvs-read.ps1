& {
    $phase="HG-NVS-READONLY-CAPTURE";$diagnosis="NVS_CAPTURE_EXCEPTION";$disposition="BLOCKED"
    $commonLoaded=$false
    $fields=[ordered]@{head="UNRESOLVED";port_identity_match=$false;
        nvs_offset_qualified=$false;nvs_size_qualified=$false;child_started="NOT_RUN";
        child_status="NOT_RUN";native_exit="NOT_RUN";timed_out="NOT_RUN";
        tree_stopped="NOT_RUN";root_exited="NOT_RUN";partition_plan_matched=$false;
        partition_image_matched=$false;local_hash_recorded=$false;bytes_read=0;
        runtime_baseline_must_be_requalified=$false;flash_write_attempted=$false;cycles_counted=0}
    try {
        . (Join-Path $PSScriptRoot "common.ps1");$commonLoaded=$true
        $root=Get-HgRoot $PSScriptRoot
        if (Initialize-HgEnv $root) {$diagnosis="ENV_CREATED_NEEDS_LOCAL_CONFIGURATION";throw $diagnosis}
        Import-HgEnv $root
        $fields.head=Assert-HgHead $root
        if (-not (Test-Path -LiteralPath $env:HG_PYTHON -PathType Leaf)) {
            $diagnosis="MANAGED_PYTHON_MISSING";throw $diagnosis
        }
        Assert-HgPort;$fields.port_identity_match=$true
        if ($env:HG_NVS_OFFSET -notmatch '^0x[0-9a-fA-F]+$' -or
            $env:HG_NVS_SIZE -notmatch '^0x[0-9a-fA-F]+$') {
            $diagnosis="QUALIFIED_NVS_PARTITION_REQUIRED";throw $diagnosis
        }
        $offset=[Convert]::ToInt64($env:HG_NVS_OFFSET.Substring(2),16)
        $size=[Convert]::ToInt64($env:HG_NVS_SIZE.Substring(2),16)
        if ($offset -lt 0 -or $size -le 0 -or ($offset+$size) -gt (8*1024*1024)) {
            $diagnosis="NVS_PARTITION_BOUNDS_INVALID";throw $diagnosis
        }
        $diagnosis="NVS_PARTITION_NOT_QUALIFIED"
        $partition=Assert-HgNvsPartition $root $offset $size
        $fields.partition_plan_matched=$partition.PlanMatched
        $fields.partition_image_matched=$partition.TableMatched
        $fields.nvs_offset_qualified=$true;$fields.nvs_size_qualified=$true
        $dir=Join-Path $root ".local\HumanGate"
        [void][IO.Directory]::CreateDirectory($dir)
        $name="nvs-"+[guid]::NewGuid().ToString("N")
        $dump=Join-Path $dir ($name+".bin")
        $fields.runtime_baseline_must_be_requalified=$true
        $diagnosis="NVS_NATIVE_WRAPPER_UNKNOWN"
        $run=Invoke-HgNative $env:HG_PYTHON @("-m","esptool","--chip","esp32s3",
            "--port",$env:HG_SERIAL_PORT,"--before","no_reset","--after","no_reset",
            "read_flash",$env:HG_NVS_OFFSET,$env:HG_NVS_SIZE,$dump) $root 240 $phase
        $fields.child_started=$run.Started;$fields.child_status=$run.Status
        $fields.native_exit=$run.ExitCode;$fields.timed_out=$run.TimedOut;$fields.tree_stopped=$run.TreeStopped
        $fields.root_exited=$run.RootExited
        $diagnosis="NVS_PRIVATE_EVIDENCE_SAVE_FAILED"
        Save-HgPrivate $root $phase $run
        if ($run.Status -ne "EXITED" -or $run.ExitCode -ne 0) {
            $diagnosis="NVS_READ_FAILED_OR_INDETERMINATE";throw $diagnosis
        }
        $file=Get-Item -LiteralPath $dump -ErrorAction Stop
        if ($file.Length -ne $size) {$diagnosis="NVS_SIZE_MISMATCH";throw $diagnosis}
        $hash=(Get-FileHash -LiteralPath $dump -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        [IO.File]::WriteAllText((Join-Path $dir ($name+".sha256.local.txt")),$hash,[Text.Encoding]::UTF8)
        $fields.local_hash_recorded=$true;$fields.bytes_read=$file.Length
        $diagnosis="QUALIFIED_READ_ONLY_CAPTURE_COMPLETE";$disposition="PASS"
    } catch {if ($_.Exception.Message -match '^[A-Z_]+$') {$diagnosis=$_.Exception.Message}}
    finally {
        if($commonLoaded) {Write-HgResult $phase $fields $diagnosis $disposition}
        else {
            Write-Host "";Write-Host "==== $phase ===="
            Write-Host ("diagnosis={0}" -f $diagnosis)
            Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase,$diagnosis,$disposition)
            Write-Host ""
        }
    }
}

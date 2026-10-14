& {
    $phase="HG-ROM-FLASH-ID";$diagnosis="ROM_LINK_EXCEPTION";$disposition="BLOCKED"
    $commonLoaded=$false
    $fields=[ordered]@{head="UNRESOLVED";port_identity_match=$false;
        child_started="NOT_RUN";child_status="NOT_RUN";native_exit="NOT_RUN";
        timed_out="NOT_RUN";tree_stopped="NOT_RUN";local_evidence_saved=$false;
        flash_write_attempted=$false;runtime_baseline_must_be_requalified=$false;cycles_counted=0}
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
        $fields.runtime_baseline_must_be_requalified=$true
        $diagnosis="ROM_NATIVE_WRAPPER_UNKNOWN"
        $run=Invoke-HgNative $env:HG_PYTHON @("-m","esptool","--chip","esp32s3",
            "--port",$env:HG_SERIAL_PORT,"--before","no_reset","--after","no_reset",
            "flash_id") $root 120 $phase
        $fields.child_started=$run.Started
        $fields.child_status=$run.Status
        $fields.native_exit=$run.ExitCode
        $fields.timed_out=$run.TimedOut
        $fields.tree_stopped=$run.TreeStopped
        $diagnosis="ROM_PRIVATE_EVIDENCE_SAVE_FAILED"
        Save-HgPrivate $root $phase $run
        $fields.local_evidence_saved=$true
        if ($run.Status -ne "EXITED" -or $run.ExitCode -ne 0) {
            $diagnosis="ROM_LINK_FAILED_OR_INDETERMINATE";throw $diagnosis
        }
        $diagnosis="ROM_FLASH_ID_COMMAND_COMPLETE";$disposition="PASS"
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

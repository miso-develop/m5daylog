& {
    $phase="HG-INIT"; $diagnosis="INIT_EXCEPTION"; $disposition="BLOCKED"
    $commonLoaded=$false
    $fields=[ordered]@{env_created="NOT_RUN";env_exists="NOT_RUN";device_access=$false}
    try {
        . (Join-Path $PSScriptRoot "common.ps1"); $commonLoaded=$true
        $root=Get-HgRoot $PSScriptRoot
        $created=Initialize-HgEnv $root
        $fields.env_created=$created
        $fields.env_exists=(Test-Path -LiteralPath (Join-Path $root ".env"))
        $diagnosis=if ($created) {"ENV_CREATED_NEEDS_LOCAL_CONFIGURATION"} else {"ENV_ALREADY_EXISTS_PRESERVED"}
        $disposition="PASS"
    } catch { if ($_.Exception.Message -match '^[A-Z_]+$') {$diagnosis=$_.Exception.Message} }
    finally {
        if ($commonLoaded) {Write-HgResult $phase $fields $diagnosis $disposition}
        else {
            Write-Host ""; Write-Host "==== $phase ===="
            Write-Host ("diagnosis={0}" -f $diagnosis)
            Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase,$diagnosis,$disposition)
            Write-Host ""
        }
    }
}

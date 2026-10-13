& {
    $phase="HG-HEAD-CHECK"; $diagnosis="PREFLIGHT_EXCEPTION"; $disposition="BLOCKED"
    $commonLoaded=$false
    $fields=[ordered]@{head="UNRESOLVED";head_match=$false;tracked_clean="NOT_RUN";managed_python_present="NOT_RUN";device_access=$false}
    try {
        . (Join-Path $PSScriptRoot "common.ps1"); $commonLoaded=$true
        $root=Get-HgRoot $PSScriptRoot
        if (Initialize-HgEnv $root) {$diagnosis="ENV_CREATED_NEEDS_LOCAL_CONFIGURATION";throw $diagnosis}
        Import-HgEnv $root
        $fields.head=Assert-HgHead $root
        $fields.head_match=$true; $fields.tracked_clean=$true
        $fields.managed_python_present=(Test-Path -LiteralPath $env:HG_PYTHON -PathType Leaf)
        if (-not $fields.managed_python_present) {$diagnosis="MANAGED_PYTHON_MISSING";throw $diagnosis}
        $diagnosis="HEAD_AND_INTERPRETER_PRESENT"; $disposition="PASS"
    } catch {if ($_.Exception.Message -match '^[A-Z_]+$') {$diagnosis=$_.Exception.Message}}
    finally {
        if ($commonLoaded) {Write-HgResult $phase $fields $diagnosis $disposition}
        else {
            Write-Host "";Write-Host "==== $phase ===="
            Write-Host ("diagnosis={0}" -f $diagnosis)
            Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase,$diagnosis,$disposition)
            Write-Host ""
        }
    }
}

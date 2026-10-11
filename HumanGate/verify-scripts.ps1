& {
    $phase="HG-SCRIPT-STATIC-VERIFY"
    $diagnosis="SCRIPT_PARSER_EXCEPTION"
    $disposition="BLOCKED"
    $fileCount=0;$parsed=0;$errorCount=0
    $invalidFiles=@()
    try {
        $files=@(Get-ChildItem -LiteralPath $PSScriptRoot -Filter "*.ps1" -File -ErrorAction Stop)
        $fileCount=$files.Count
        foreach($file in $files) {
            $tokens=$null;$parseErrors=$null
            [void][System.Management.Automation.Language.Parser]::ParseFile(
                $file.FullName,[ref]$tokens,[ref]$parseErrors)
            if (@($parseErrors).Count -gt 0) {
                $errorCount+=@($parseErrors).Count
                $invalidFiles+=([string]$file.Name)
            } else { $parsed++ }
        }
        if ($errorCount -gt 0) {$diagnosis="POWERSHELL_PARSE_FAILURE"}
        else {$diagnosis="ALL_POWERSHELL_FILES_PARSE";$disposition="PASS"}
    } catch {
        $diagnosis="SCRIPT_PARSER_EXCEPTION"
        $disposition="BLOCKED"
    } finally {
        Write-Host ""
        Write-Host "==== $phase ===="
        Write-Host ("files_checked={0}" -f $fileCount)
        Write-Host ("parse_passed={0}" -f $parsed)
        Write-Host ("parse_error_count={0}" -f $errorCount)
        Write-Host ("parse_failed_basenames={0}" -f (($invalidFiles | Sort-Object) -join ","))
        Write-Host ("diagnosis={0}" -f $diagnosis)
        Write-Host ("disposition={0}" -f $disposition)
        Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase,$diagnosis,$disposition)
        Write-Host ""
    }
}

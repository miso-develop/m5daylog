& {
    $phase="HG-CDC-READONLY"; $diagnosis="CDC_OBSERVATION_EXCEPTION"; $disposition="BLOCKED"
    $commonLoaded=$false; $port=$null
    $fields=[ordered]@{head="UNRESOLVED";port_identity_match=$false;cdc_opened=$false;
        get_status_before_pass=$false;ping_pass=$false;get_info_pass=$false;
        get_status_after_pass=$false;cdc_cleanup="NOT_RUN";mutation_attempted=$false;cycles_counted=0}
    try {
        . (Join-Path $PSScriptRoot "common.ps1");$commonLoaded=$true
        $root=Get-HgRoot $PSScriptRoot
        if (Initialize-HgEnv $root) {$diagnosis="ENV_CREATED_NEEDS_LOCAL_CONFIGURATION";throw $diagnosis}
        Import-HgEnv $root
        $fields.head=Assert-HgHead $root
        Assert-HgPort;$fields.port_identity_match=$true
        $port=New-Object System.IO.Ports.SerialPort($env:HG_SERIAL_PORT,115200)
        $port.NewLine=[string][char]10
        $port.ReadTimeout=5000;$port.WriteTimeout=5000
        $port.Open();$fields.cdc_opened=$true
        $commands=@("GET_STATUS","PING","GET_INFO","GET_STATUS")
        for($i=0;$i -lt $commands.Count;$i++) {
            $command=$commands[$i]
            $requestId="hg-readonly-"+[guid]::NewGuid().ToString("N")
            $request=@{id=$requestId;cmd=$command;args=@{}} | ConvertTo-Json -Compress -Depth 4
            Write-Host ("PROGRESS phase={0} step={1}/4 command={2}" -f $phase,($i+1),$command)
            $port.WriteLine([string]$request)
            $obj=([string]$port.ReadLine()) | ConvertFrom-Json -ErrorAction Stop
            if ([string]$obj.id -cne $requestId -or $obj.ok -ne $true) {
                $diagnosis="CDC_RESPONSE_ID_OR_RESULT_MISMATCH";throw $diagnosis
            }
            if($command -eq "PING" -and $obj.result.pong -ne $true) {
                $diagnosis="PING_RESULT_INVALID";throw $diagnosis
            }
            if($command -eq "GET_STATUS" -and [string]$obj.result.state -cne "USB_SYNC") {
                $diagnosis="CDC_UNEXPECTED_LIFECYCLE";throw $diagnosis
            }
            switch($i) {
                0 {$fields.get_status_before_pass=$true}
                1 {$fields.ping_pass=$true}
                2 {$fields.get_info_pass=$true}
                3 {$fields.get_status_after_pass=$true}
            }
        }
        $diagnosis="FOUR_READONLY_OBSERVATIONS_COMPLETE";$disposition="PASS"
    } catch {if ($_.Exception.Message -match '^[A-Z_]+$') {$diagnosis=$_.Exception.Message}}
    finally {
        if($null -ne $port) {
            try {$port.Close();$fields.cdc_cleanup="CLOSED"} catch {$fields.cdc_cleanup="CLOSE_EXCEPTION"}
            try {$port.Dispose()} catch {}
        }
        if($commonLoaded) {Write-HgResult $phase $fields $diagnosis $disposition}
        else {
            Write-Host "";Write-Host "==== $phase ===="
            Write-Host ("diagnosis={0}" -f $diagnosis)
            Write-Host ("SUMMARY phase={0} diagnosis={1} disposition={2}" -f $phase,$diagnosis,$disposition)
            Write-Host ""
        }
    }
}

param([string]$EvidenceRoot)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backend = Join-Path $root 'im-service/backend'
if (-not $EvidenceRoot) {
    $EvidenceRoot = [IO.Path]::GetTempPath()
}
$artifacts = Join-Path (Resolve-Path -LiteralPath $EvidenceRoot).Path ('tickdesk-quant-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $artifacts | Out-Null
$testData = Join-Path $artifacts 'kernel-data'
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
try {
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
} finally {
    $listener.Stop()
}

$previousData = $env:DATA_DIR
$previousUtf8 = $env:PYTHONUTF8
$previousAddress = $env:TICKDESK_QUANT_VERIFY_ADDRESS
$fixture = $null
try {
    $env:DATA_DIR = $testData
    $env:PYTHONUTF8 = '1'
    $env:TICKDESK_QUANT_VERIFY_ADDRESS = "127.0.0.1:$port"
    $fixture = Start-Process -FilePath (Join-Path $backend '.venv/Scripts/python.exe') `
        -ArgumentList @('tests/tickdesk_quant_fixture.py', '--port', $port, '--data-dir', ('"{0}"' -f $testData)) `
        -WorkingDirectory $backend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $artifacts 'kernel.log') `
        -RedirectStandardError (Join-Path $artifacts 'kernel-errors.log')
    $ready = $false
    for ($attempt = 0; $attempt -lt 40 -and -not $fixture.HasExited; $attempt++) {
        try {
            $contract = Invoke-RestMethod -Uri "http://127.0.0.1:$port/openapi.json" -TimeoutSec 1
            if ($contract.paths.'/api/tickdesk/prepare') {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if (-not $ready) {
        throw "Synthetic kernel did not start. See $artifacts"
    }
    # This fixture deliberately replays a completed daily bar after the market window.
    cargo test --manifest-path (Join-Path $root 'local-service/Cargo.toml') --locked -j 1 --features integration-fixtures `
        --test market quant_kernel_http_to_durable_scan -- --ignored
    if ($LASTEXITCODE -ne 0) {
        throw "Quant integration test failed. See $artifacts"
    }
    Write-Output "Synthetic kernel artifacts: $artifacts"
} finally {
    if ($fixture -and -not $fixture.HasExited) {
        # Stop the verified process tree since the Windows venv launcher spawns Python.
        & taskkill.exe /PID $fixture.Id /T /F | Out-Null
        $fixture.WaitForExit()
    }
    $env:DATA_DIR = $previousData
    $env:PYTHONUTF8 = $previousUtf8
    $env:TICKDESK_QUANT_VERIFY_ADDRESS = $previousAddress
}

# Start tick-stock-panel backend + frontend as detached processes.
# Logs: data/logs/{backend,frontend}.{out,err}.log
# PIDs: data/{backend,frontend}.pid
$ErrorActionPreference = 'Stop'

$ToolsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ToolsDir
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$LogDir = Join-Path $Root 'data\logs'
$PythonExe = Join-Path $BackendDir '.venv\Scripts\python.exe'
$ViteJs = Join-Path $FrontendDir 'node_modules\vite\bin\vite.js'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-Port($port) {
    $conns = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    return [bool]$conns
}

function Wait-Port($port, $seconds) {
    for ($i = 0; $i -lt ($seconds * 2); $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Port $port) { return $true }
    }
    return $false
}

# ---- backend ----
if (Test-Port 3018) {
    Write-Host "[start] backend already listening on 3018"
} else {
    Write-Host "[start] launching backend on 127.0.0.1:3018"
    $env:PYTHONUNBUFFERED = '1'
    $bp = Start-Process -FilePath $PythonExe `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '3018') `
        -WorkingDirectory $BackendDir `
        -RedirectStandardOutput (Join-Path $LogDir 'backend.out.log') `
        -RedirectStandardError (Join-Path $LogDir 'backend.err.log') `
        -WindowStyle Hidden -PassThru
    $bp.Id | Set-Content (Join-Path $Root 'data\backend.pid')
    if (-not (Wait-Port 3018 60)) {
        Write-Host "[start] backend failed to bind port 3018; see data\logs\backend.err.log"
    } else {
        Write-Host "[start] backend up (pid $($bp.Id))"
    }
}

# ---- frontend ----
if (Test-Port 3011) {
    Write-Host "[start] frontend already listening on 3011"
} else {
    Write-Host "[start] launching frontend (vite) on 127.0.0.1:3011"
    if (-not (Test-Path $ViteJs)) {
        throw "vite not installed at $ViteJs; run: cd frontend; npm install"
    }
    $env:BACKEND_HOST = '127.0.0.1'
    $env:BACKEND_PORT = '3018'
    $fp = Start-Process -FilePath 'node' `
        -ArgumentList @($ViteJs, '--host', '127.0.0.1', '--port', '3011') `
        -WorkingDirectory $FrontendDir `
        -RedirectStandardOutput (Join-Path $LogDir 'frontend.out.log') `
        -RedirectStandardError (Join-Path $LogDir 'frontend.err.log') `
        -WindowStyle Hidden -PassThru
    $fp.Id | Set-Content (Join-Path $Root 'data\frontend.pid')
    if (-not (Wait-Port 3011 45)) {
        Write-Host "[start] frontend failed to bind port 3011; see data\logs\frontend.err.log"
    } else {
        Write-Host "[start] frontend up (pid $($fp.Id))"
    }
}

# ---- notification relay (面板告警 -> 微信推送) ----
$RelayDir = Join-Path $ToolsDir 'notify-relay'
if (Test-Port 8899) {
    Write-Host "[start] notify relay already listening on 8899"
} else {
    if (Test-Path (Join-Path $RelayDir 'relay.py')) {
        Write-Host "[start] launching notification relay on 127.0.0.1:8899"
        $rp = Start-Process -FilePath 'python' `
            -ArgumentList @((Join-Path $RelayDir 'relay.py')) `
            -WorkingDirectory $RelayDir `
            -RedirectStandardOutput (Join-Path $LogDir 'relay.out.log') `
            -RedirectStandardError (Join-Path $LogDir 'relay.err.log') `
            -WindowStyle Hidden -PassThru
        $rp.Id | Set-Content (Join-Path $Root 'data\relay.pid')
        if (-not (Wait-Port 8899 30)) {
            Write-Host "[start] relay failed to bind 8899; see data\logs\relay.err.log"
        } else {
            Write-Host "[start] relay up (pid $($rp.Id))"
        }
    }
}

# ---- market scanner (每5分钟全市场扫描 -> 推送买卖点) ----
$ScannerDir = Join-Path $ToolsDir 'market-scanner'
$ScannerPidFile = Join-Path $Root 'data\scanner.pid'
$scannerAlive = $false
if (Test-Path $ScannerPidFile) {
    $scannerAlive = [bool](Get-Process -Id (Get-Content $ScannerPidFile) -ErrorAction SilentlyContinue)
}
if ($scannerAlive) {
    Write-Host "[start] market scanner already running (pid $(Get-Content $ScannerPidFile))"
} elseif (Test-Path (Join-Path $ScannerDir 'scanner.py')) {
    Write-Host "[start] launching market scanner (every 5 min during trading hours)"
    $sp = Start-Process -FilePath 'python' `
        -ArgumentList @((Join-Path $ScannerDir 'scanner.py')) `
        -WorkingDirectory $ScannerDir `
        -RedirectStandardOutput (Join-Path $LogDir 'scanner.out.log') `
        -RedirectStandardError (Join-Path $LogDir 'scanner.err.log') `
        -WindowStyle Hidden -PassThru
    $sp.Id | Set-Content $ScannerPidFile
    Start-Sleep -Seconds 2
    if ([bool](Get-Process -Id $sp.Id -ErrorAction SilentlyContinue)) {
        Write-Host "[start] market scanner up (pid $($sp.Id))"
    } else {
        Write-Host "[start] market scanner exited; see tools\market-scanner\scanner.log"
    }
}

Write-Host "[start] backend 3018 listening: $(Test-Port 3018)"
Write-Host "[start] frontend 3011 listening: $(Test-Port 3011)"
Write-Host "[start] relay 8899 listening: $(Test-Port 8899)"

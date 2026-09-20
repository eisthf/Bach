# Bach 개발 서버 시작 스크립트 (Windows PowerShell)
#
#   .\dev.ps1          .env 설정 그대로 (실거래 계좌 포함 가능)
#   .\dev.ps1 --demo   합성 mock 계좌 1개만. 장 시작/종료를 수동 토글 가능

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptDir = $PSScriptRoot
Set-Location -LiteralPath $scriptDir
$frontendPort = if ($env:BACH_FRONTEND_PORT) { [int]$env:BACH_FRONTEND_PORT } else { 5273 }

$demo = $false
foreach ($arg in $args) {
    switch ($arg) {
        '--demo' { $demo = $true }
        { $_ -in '-h', '--help' } {
            Write-Host '.\dev.ps1          .env 설정 그대로 (실거래 계좌 포함 가능)'
            Write-Host '.\dev.ps1 --demo   합성 mock 계좌 1개만. 장 시작/종료를 수동 토글 가능'
            exit 0
        }
        default {
            Write-Error "알 수 없는 옵션: $arg (사용법: .\dev.ps1 [--demo])"
        }
    }
}

function Assert-Command {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$InstallHint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "'$Name' 명령을 찾을 수 없습니다. $InstallHint"
    }
}

function Assert-PortsAvailable {
    $busy = @()
    foreach ($port in 8000, $frontendPort) {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        foreach ($listener in $listeners) {
            $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
            $busy += [PSCustomObject]@{
                Port = $port
                Pid = $listener.OwningProcess
                Name = if ($process) { $process.ProcessName } else { '?' }
                Path = if ($process) { $process.Path } else { '' }
            }
        }
    }

    if ($busy.Count -eq 0) { return }

    Write-Host '⚠️  개발 서버 포트가 이미 사용 중입니다:' -ForegroundColor Yellow
    $busy | Sort-Object Port, Pid -Unique | Format-Table Port, Pid, Name, Path -AutoSize
    throw '기존 프로세스를 확인해 종료한 뒤 다시 실행하세요. 실행 중인 실거래 서버일 수 있어 자동 종료하지 않았습니다.'
}

function Get-ProcessTreeIds {
    param([Parameter(Mandatory)][int]$RootId)

    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $pending = [System.Collections.Generic.Queue[int]]::new()
    $result = [System.Collections.Generic.List[int]]::new()
    $pending.Enqueue($RootId)
    while ($pending.Count -gt 0) {
        $parentId = $pending.Dequeue()
        foreach ($child in $all | Where-Object ParentProcessId -eq $parentId) {
            $childId = [int]$child.ProcessId
            if (-not $result.Contains($childId)) {
                $result.Add($childId)
                $pending.Enqueue($childId)
            }
        }
    }
    return @($result)
}

function Stop-DevProcess {
    param([Parameter(Mandatory)][System.Diagnostics.Process]$Process)

    if ($Process.HasExited) { return }
    $ids = @(Get-ProcessTreeIds -RootId $Process.Id)
    [array]::Reverse($ids)
    foreach ($id in $ids + @($Process.Id)) {
        Stop-Process -Id $id -ErrorAction SilentlyContinue
    }
}

Assert-Command -Name 'uv' -InstallHint 'https://docs.astral.sh/uv/getting-started/installation/ 에서 uv를 설치하세요.'
Assert-Command -Name 'npm.cmd' -InstallHint 'https://nodejs.org/ 에서 Node.js LTS를 설치하세요.'
Assert-PortsAvailable

if (-not (Test-Path -LiteralPath "$scriptDir\backend\.venv")) {
    Write-Host '📦 백엔드 초기 셋업...'
    Push-Location -LiteralPath "$scriptDir\backend"
    try {
        & uv venv --python 3.11
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 가상환경 생성에 실패했습니다.' }
        & uv pip install -e .
        if ($LASTEXITCODE -ne 0) { throw '백엔드 패키지 설치에 실패했습니다.' }
        if (-not (Test-Path -LiteralPath '.env')) {
            Copy-Item -LiteralPath '.env.example' -Destination '.env'
        }
    }
    finally {
        Pop-Location
    }
    Write-Host ''
}

if (-not (Test-Path -LiteralPath "$scriptDir\frontend\node_modules")) {
    Write-Host '📦 프론트 초기 셋업...'
    Push-Location -LiteralPath "$scriptDir\frontend"
    try {
        & npm.cmd install
        if ($LASTEXITCODE -ne 0) { throw '프론트 패키지 설치에 실패했습니다.' }
    }
    finally {
        Pop-Location
    }
    Write-Host ''
}

Write-Host '========================================='
if ($demo) {
    Write-Host '🧪 Bach 개발 서버 시작 — 데모 모드'
    Write-Host '   합성 mock 계좌 1개. 실주문 없음. 장 토글 수동.'
}
else {
    Write-Host '🚀 Bach 개발 서버 시작'
}
Write-Host '========================================='

$processes = [System.Collections.Generic.List[System.Diagnostics.Process]]::new()
$savedEnvironment = @{}
$demoEnvironment = @{
    ACCOUNTS = 'mock'
    MOCK_PROVIDER = 'mock'
    # Windows는 빈 환경변수를 삭제하므로 공백을 넘긴다. 백엔드는 strip() 후
    # 빈 토큰으로 해석해 .env의 실사용 토큰보다 우선시한다.
    API_TOKEN = ' '
    BACH_STATE_MOCK = "$scriptDir\backend\state.demo.json"
}

try {
    if ($demo) {
        foreach ($name in $demoEnvironment.Keys) {
            $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, $demoEnvironment[$name], 'Process')
        }
    }

    # Windows에서 uv가 만든 console-script 실행 파일을 다시 spawn하면 일부
    # 보안 환경에서 Access denied가 날 수 있어 Python 모듈로 직접 실행한다.
    $backendPython = "$scriptDir\backend\.venv\Scripts\python.exe"
    $backend = Start-Process -FilePath $backendPython `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--reload') `
        -WorkingDirectory "$scriptDir\backend" -NoNewWindow -PassThru
    $processes.Add($backend)

    if ($demo) {
        foreach ($name in $demoEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
        }
    }

    $frontend = Start-Process -FilePath 'npm.cmd' -ArgumentList @('run', 'dev') `
        -WorkingDirectory "$scriptDir\frontend" -NoNewWindow -PassThru
    $processes.Add($frontend)

    Write-Host "✅ 백엔드 실행 중 (포트 8000, PID: $($backend.Id))"
    Write-Host "✅ 프론트 실행 중 (포트 $frontendPort, PID: $($frontend.Id))"
    Write-Host ''
    Write-Host "📱 접속 주소: http://localhost:$frontendPort"
    if ($demo) {
        Write-Host ''
        Write-Host "   데모 흐름: 종목 추가 → 칩 PUSH로 '모니터' → 우상단 '장 시작'"
        Write-Host '   → 자동매매 진입. 상태는 backend/state.demo.json 에만 저장됨.'
    }
    Write-Host ''
    Write-Host '⏹️  종료: Ctrl+C 입력'
    Write-Host '========================================='
    Write-Host ''

    while (-not ($backend.HasExited -or $frontend.HasExited)) {
        Start-Sleep -Milliseconds 500
        $backend.Refresh()
        $frontend.Refresh()
    }

    if ($backend.HasExited) { Write-Warning "백엔드가 종료되었습니다 (종료 코드: $($backend.ExitCode))." }
    if ($frontend.HasExited) { Write-Warning "프론트가 종료되었습니다 (종료 코드: $($frontend.ExitCode))." }
}
finally {
    if ($demo) {
        foreach ($name in $demoEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
        }
    }
    if ($processes.Count -gt 0) {
        Write-Host ''
        Write-Host '🛑 서버 종료 중...'
        foreach ($process in $processes) { Stop-DevProcess -Process $process }
        Write-Host '✅ 종료됨'
    }
}

# 설교 실시간 번역 - 서버 + 셀폰 외부접속 통합 실행 스크립트
$ErrorActionPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Set-Location "C:\Claude"

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "   설교 실시간 번역 시작" -ForegroundColor Cyan
Write-Host "====================================================`n"

# 1) 기존 프로세스 정리
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force

# 1-2) 포트 5000을 점유한 이전 서버 프로세스 정리 (옛 코드 중복 실행 방지)
$conns = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    $conns.OwningProcess | Sort-Object -Unique | ForEach-Object {
        Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

# 2) 번역 서버 시작 (별도 창)
Write-Host "[1/4] 번역 서버 시작 중..." -ForegroundColor Yellow
Start-Process -FilePath "python" -ArgumentList "server.py" -WorkingDirectory "C:\Claude"

# 3) 서버 포트(5000)가 열릴 때까지 대기 (최대 30초)
Write-Host "[2/4] 서버 준비 대기 중..." -ForegroundColor Yellow
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", 5000)
        if ($tcp.Connected) { $tcp.Close(); $ready = $true; break }
    } catch {}
}

if (-not $ready) {
    Write-Host "      서버 감지에 실패했지만 계속 진행합니다." -ForegroundColor Yellow
} else {
    Write-Host "      서버 준비 완료!" -ForegroundColor Green
}

# 4) 운영자 대시보드 열기 (Chrome 우선)
Write-Host "[3/4] 운영자 대시보드 여는 중..." -ForegroundColor Yellow
$chrome = "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe"
$chrome86 = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
$dashUrl = "http://localhost:5000/operator"
if (Test-Path $chrome) {
    Start-Process $chrome $dashUrl
} elseif (Test-Path $chrome86) {
    Start-Process $chrome86 $dashUrl
} else {
    Start-Process $dashUrl
}

# 5) 셀폰 외부접속 — ngrok 고정 도메인이 설정돼 있으면 사용, 없으면 cloudflare 임시주소
Write-Host "[4/4] 셀폰 외부접속 주소 준비 중..." -ForegroundColor Yellow

$domainFile = "C:\Claude\ngrok-domain.txt"
$ngrokDomain = $null
if (Test-Path $domainFile) {
    $ngrokDomain = (Get-Content $domainFile -Raw).Trim()
}

if ($ngrokDomain) {
    # ----- ngrok 고정 주소 방식 -----
    $ngrok = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter ngrok.exe -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
    if (-not $ngrok) { $ngrok = "ngrok" }
    Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Process -FilePath $ngrok -ArgumentList 'http','5000','--domain',$ngrokDomain -WindowStyle Minimized

    $url = "https://$ngrokDomain"
    Write-Host "`n====================================================" -ForegroundColor Green
    Write-Host "  고정 셀폰 접속 주소 (매주 동일):" -ForegroundColor Green
    Write-Host "  $url/mobile" -ForegroundColor White
    Write-Host "  QR은 한 번만 만들어 두면 계속 재사용됩니다." -ForegroundColor Gray
    Write-Host "====================================================`n" -ForegroundColor Green
    $url | Set-Clipboard
    $url | Out-File "C:\Claude\현재-셀폰주소.txt" -Encoding utf8
} else {
    # ----- cloudflare 임시 주소 방식(고정 도메인 미설정 시) -----
    $cf = @(
        "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
        "${env:ProgramFiles}\cloudflared\cloudflared.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $cf) {
        Write-Host "cloudflared를 찾을 수 없습니다. 셀폰 외부접속은 건너뜁니다." -ForegroundColor Red
    } else {
        Remove-Item "C:\Claude\tunnel.log" -ErrorAction SilentlyContinue
        Start-Process -FilePath $cf -ArgumentList 'tunnel','--url','http://localhost:5000','--logfile','C:\Claude\tunnel.log' -WindowStyle Minimized

        $url = $null
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 1
            if (Test-Path "C:\Claude\tunnel.log") {
                $m = Select-String -Path "C:\Claude\tunnel.log" -Pattern "https://[a-zA-Z0-9-]+\.trycloudflare\.com" -AllMatches
                if ($m) { $url = $m.Matches[0].Value; break }
            }
        }

        if ($url) {
            Write-Host "`n셀폰 접속 주소: $url/mobile" -ForegroundColor Green
            $url | Set-Clipboard
            $url | Out-File "C:\Claude\현재-셀폰주소.txt" -Encoding utf8
        } else {
            Write-Host "터널 주소를 찾지 못했습니다. 인터넷 연결을 확인하세요." -ForegroundColor Red
        }
    }
}

Write-Host "`n이 창을 닫으면 셀폰 접속이 끊깁니다. 예배 중 닫지 마세요." -ForegroundColor Yellow
Read-Host "종료하려면 엔터를 누르세요"

# 종료 시 정리
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force

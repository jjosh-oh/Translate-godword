# 설교 번역 시스템 백업 스크립트
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$date = Get-Date -Format 'yyyy-MM-dd_HHmm'
$dest = 'C:\Claude-백업\' + $date
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item 'C:\Claude\*' -Destination $dest -Recurse -Force -Exclude 'tunnel.log','tunnel_stdout.log','__pycache__'

Write-Host ''
Write-Host ('백업 완료: ' + $dest) -ForegroundColor Green
$count = (Get-ChildItem $dest -Recurse -File | Measure-Object).Count
Write-Host ('백업된 파일 수: ' + $count) -ForegroundColor Green
Write-Host ''
Read-Host '엔터를 누르면 닫힙니다'

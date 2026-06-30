@echo off
set NGROK="C:\Users\okyep\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"

echo ====================================================
echo   ngrok fixed-address setup (one time only)
echo ====================================================
echo.
echo [Step 1] Paste your ngrok Authtoken, then press Enter
echo   (right-click inside this window to paste)
echo.
set /p TOKEN="Authtoken: "
%NGROK% config add-authtoken %TOKEN%
echo.
echo [Step 2] Type your fixed domain, then press Enter
echo   linguist-copartner-overflow.ngrok-free.dev
echo.
set /p DOMAIN="Domain: "
echo %DOMAIN%> "C:\Claude\ngrok-domain.txt"
echo.
echo ====================================================
echo   DONE.  Phone URL: https://%DOMAIN%/mobile
echo ====================================================
echo.
pause

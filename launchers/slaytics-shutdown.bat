@echo off
REM Tutup semua jendela Chrome yang dibuka oleh slaytics-command-center.bat
REM
REM Caranya: kita filter berdasarkan command-line (--app=*command-center*)
REM Tidak akan menutup Chrome biasa yang lagi browsing.

echo Menutup semua jendela Slaytics Command Center...

REM Tutup berdasarkan title window (Chrome --app menyetel title sesuai HTML <title>)
taskkill /F /FI "WINDOWTITLE eq Slaytics M1*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Slaytics M2*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Slaytics M3*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Slaytics M4*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Slaytics M5*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Slaytics M6*" >nul 2>&1

echo Done.
timeout /t 2 >nul

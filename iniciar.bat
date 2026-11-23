@echo off
REM Arranca la base de datos web RFID con el PN532 en COM9.
REM Para probar sin lector:  iniciar.bat --simulado
cd /d "%~dp0"
set PY=python
if exist "..\.venv\Scripts\python.exe" set PY="..\.venv\Scripts\python.exe"
REM La exportacion a Excel necesita openpyxl: se instala la primera vez si falta
%PY% -c "import openpyxl" 2>nul || (
    echo Instalando openpyxl para la exportacion a Excel...
    %PY% -m pip install openpyxl
)
%PY% servidor.py %*
pause

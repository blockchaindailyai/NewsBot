@echo off
cd %~dp0
echo ============================================
echo   Blockchain Daily Bot - Running
echo ============================================

REM ---- Load environment variables from .env.txt ----
if exist ".env.txt" (
    echo Loading environment variables from .env.txt...
    for /f "usebackq tokens=1,* delims==" %%A in (".env.txt") do (
        if NOT "%%A"=="" (
            if NOT "%%A"=="#" (
                set "%%A=%%B"
            )
        )
    )
) else (
    echo WARNING: .env.txt file not found, continuing without env vars.
)

echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Resolving certifi CA bundle path...
for /f "delims=" %%A in ('python -c "import certifi; print(certifi.where())"') do (
    set "SSL_CERT_FILE=%%A"
    set "REQUESTS_CA_BUNDLE=%%A"
)
echo Using CA bundle: %SSL_CERT_FILE%

echo Starting bot with venv Python...
python main.py

echo Bot stopped.
pause

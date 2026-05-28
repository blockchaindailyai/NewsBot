@echo off
echo ============================================
echo   Blockchain Daily Bot - Setup (Python 3.10)
echo ============================================

REM ---- Check that py -3.10 exists ----
py -3.10 --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python 3.10 not found.
    echo Install Python 3.10 and make sure "py -3.10" works.
    pause
    exit /b
)

echo Creating virtual environment with Python 3.10...
py -3.10 -m venv venv
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to create venv with Python 3.10.
    pause
    exit /b
)

echo Activating virtual environment...
call venv\Scripts\activate.bat

echo Upgrading pip...
venv\Scripts\python.exe -m pip install --upgrade pip

echo Installing required packages...
venv\Scripts\python.exe -m pip install selenium undetected-chromedriver openai regex

echo Creating profile folders if needed...
mkdir x_profile >nul 2>&1
mkdir x_poster_profile >nul 2>&1

echo Setup complete!
echo.
pause

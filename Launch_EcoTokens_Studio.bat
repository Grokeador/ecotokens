@echo off
title EcoTokens 2.0 Studio - Zero-Bloat MCP & -76%% AI Gateway
echo ========================================================================
echo   EcoTokens 2.0 Studio - Starting Web Hub (http://127.0.0.1:8794)
echo   Works 100%% Out of the Box (Zero External Pip Dependencies Needed)
echo ========================================================================
cd /d "%~dp0"
python "%~dp0ecotokens\studio_ui.py" %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Python 3.11+ required. Verify with: python --version
    pause
)

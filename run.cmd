@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run setup.cmd first. >&2
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0run.py" %*
exit /b %errorlevel%

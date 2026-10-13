@echo off
setlocal
if defined VISIBILITY_PYTHON (
  "%VISIBILITY_PYTHON%" "%~dp0setup.py" %*
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    python "%~dp0setup.py" %*
  ) else (
    py -3 "%~dp0setup.py" %*
  )
)
exit /b %errorlevel%

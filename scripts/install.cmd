@echo off
rem CATIA MCP server installer wrapper: runs install.ps1 with a relaxed
rem execution policy for this one process (no admin rights, no policy change).
rem   install.cmd                  install / upgrade from the main branch
rem   install.cmd -Branch NAME     install another branch
rem   install.cmd -Uninstall       remove the service, client entries and tool
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo Installer failed with exit code %RC%.
rem Keep the window open when started by double-click from Explorer.
echo %CMDCMDLINE% | find /i "%~nx0" >NUL 2>NUL && pause
endlocal & exit /b %RC%

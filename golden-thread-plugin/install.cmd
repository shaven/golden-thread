@echo off
rem install.cmd -- run install.sh from cmd.exe, PowerShell or Explorer on Windows (0.19.2).
rem
rem A LAUNCHER, NOT A SECOND INSTALLER. It finds Git Bash and hands it install.sh with every
rem argument unchanged; all the work, every check and every refusal stays in install.sh.
rem Git for Windows is already required: Claude Code on Windows runs its tools and hooks
rem through Git Bash.
rem
rem Which bash, first hit wins:
rem   1. `where bash`, skipping C:\Windows\System32\bash.exe (WSL -- a Linux, not this
rem      Windows; an install there lands in the Linux home) and the WindowsApps alias;
rem   2. %ProgramFiles%\Git\bin\bash.exe        (Git for Windows, all users);
rem   3. %LocalAppData%\Programs\Git\bin\bash.exe (Git for Windows, this user only).
rem
rem CRLF LINE ENDINGS ON PURPOSE: cmd.exe misreads labels and blocks in an LF batch file.
rem .gitattributes pins *.cmd to CRLF so a checkout on any platform keeps them.
setlocal EnableExtensions
set "GT_BASH="
for /f "delims=" %%B in ('where bash 2^>nul') do call :consider "%%~fB"
if not defined GT_BASH if exist "%ProgramFiles%\Git\bin\bash.exe" set "GT_BASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined GT_BASH if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "GT_BASH=%LocalAppData%\Programs\Git\bin\bash.exe"
if not defined GT_BASH (
  echo X Git Bash was not found. Golden Thread installs through Git Bash, which Claude Code
  echo   on Windows needs as well. Install Git for Windows from https://git-scm.com/download/win
  echo   then open a new terminal and run install.cmd again.
  exit /b 1
)
rem install.sh finds its own directory from $0, so hand it a "/" path: `dirname` in bash
rem does not split on "\".
set "GT_SH=%~dp0install.sh"
set "GT_SH=%GT_SH:\=/%"
"%GT_BASH%" "%GT_SH%" %*
exit /b %ERRORLEVEL%

:consider
if defined GT_BASH exit /b 0
if /i "%~1"=="%SystemRoot%\System32\bash.exe" exit /b 0
echo %~1| findstr /i "\\WindowsApps\\" >nul && exit /b 0
set "GT_BASH=%~1"
exit /b 0

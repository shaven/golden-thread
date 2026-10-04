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
rem Windows-style switches (0.20.1): /uninstall, /check, /yes and /? are passed on as
rem --uninstall, --check, --yes and --help, so `install.cmd /uninstall /check` works as a
rem Windows user would type it. Every other argument is passed on unchanged.
rem
rem UTF-8 (0.20.1): install.sh prints UTF-8 (arrows, dashes); in a console still on the OEM
rem code page that was mojibake. The code page is switched to 65001 for the run and put back.
rem GT_LAUNCHER tells install.sh its hints are read in cmd/PowerShell, not in Git Bash.
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
set "GT_ARGS="
:args
if "%~1"=="" goto run
set "GT_A=%~1"
if /i "%GT_A%"=="/uninstall" set "GT_A=--uninstall"
if /i "%GT_A%"=="/check" set "GT_A=--check"
if /i "%GT_A%"=="/yes" set "GT_A=--yes"
if /i "%GT_A%"=="/?" set "GT_A=--help"
set GT_ARGS=%GT_ARGS% "%GT_A%"
shift
goto args
:run
set "GT_CP="
for /f "tokens=2 delims=:" %%C in ('chcp') do set "GT_CP=%%C"
if defined GT_CP set "GT_CP=%GT_CP: =%"
if defined GT_CP set "GT_CP=%GT_CP:.=%"
chcp 65001 >nul
set "GT_LAUNCHER=install.cmd"
set "PYTHONIOENCODING=utf-8"
"%GT_BASH%" "%GT_SH%" %GT_ARGS%
set "GT_RC=%ERRORLEVEL%"
if defined GT_CP chcp %GT_CP% >nul
exit /b %GT_RC%

:consider
if defined GT_BASH exit /b 0
if /i "%~1"=="%SystemRoot%\System32\bash.exe" exit /b 0
echo %~1| findstr /i "\\WindowsApps\\" >nul && exit /b 0
set "GT_BASH=%~1"
exit /b 0

@echo off
rem Builds uplink_hook.exe (the fast Claude Code hook) with MSVC; the companion build bundles it.
setlocal
cd /d %~dp0
set VCVARS=
for /f "usebackq delims=" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -property installationPath 2^>nul`) do set VCVARS=%%i\VC\Auxiliary\Build\vcvars64.bat
if not defined VCVARS set VCVARS=%ProgramFiles%\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat
call "%VCVARS%" >nul 2>nul || exit /b 1
cl /nologo /O1 /MT /W4 /D_CRT_SECURE_NO_WARNINGS uplink_hook.c /Fe:uplink_hook.exe /Fo:uplink_hook.obj ^
   /link /SUBSYSTEM:WINDOWS /ENTRY:mainCRTStartup kernel32.lib >build.log || (type build.log & exit /b 1)
del uplink_hook.obj build.log 2>nul
echo built %~dp0uplink_hook.exe

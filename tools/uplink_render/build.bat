@echo off
rem Builds render.exe with MSVC (Visual Studio 2022 Build Tools) and renders every screen into out\.
setlocal
cd /d %~dp0
if not exist u8g2\u8g2.h python fetch_u8g2.py || exit /b 1
set VCVARS=
for /f "usebackq delims=" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -property installationPath 2^>nul`) do set VCVARS=%%i\VC\Auxiliary\Build\vcvars64.bat
if not defined VCVARS set VCVARS=%ProgramFiles%\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat
call "%VCVARS%" >nul 2>nul || exit /b 1
if not exist obj mkdir obj
if not exist out mkdir out
cl /nologo /std:c11 /utf-8 /O1 /W3 /wd4244 /wd4267 /wd4018 /wd4146 /wd4996 /D_CRT_SECURE_NO_WARNINGS ^
   /I fake /I u8g2 /I ..\..\apps\dedsec_uplink harness.c fake\fake_sdk.c host_fonts.c u8g2\*.c ^
   /Fe:render.exe /Foobj\ >obj\build.log || (type obj\build.log & exit /b 1)
"%~dp0render.exe" "%~dp0out" || exit /b 1
python sheet.py

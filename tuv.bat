@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "TUV_HOME=%~dp0"
if "%TUV_HOME:~-1%"=="\" set "TUV_HOME=%TUV_HOME:~0,-1%"
set "RUNNER=%TUV_HOME%\.tuv-venv"
set "REQ=%TUV_HOME%\requirements.txt"
set "APP=%TUV_HOME%\tuv.py"

if not exist "%APP%" (
  echo tuv.py was not found in %TUV_HOME% 1>&2
  exit /b 1
)

if not exist "%REQ%" (
  echo requirements.txt was not found in %TUV_HOME% 1>&2
  exit /b 1
)

set "FIND_PS=%TEMP%\tuv-find-python-%RANDOM%-%RANDOM%.ps1"
> "%FIND_PS%" echo $ErrorActionPreference = 'SilentlyContinue'
>> "%FIND_PS%" echo $c = @()
>> "%FIND_PS%" echo try { ^& py -0p 2^>^&1 ^| ForEach-Object { if ($_ -match '([A-Za-z]:\\.*?python(?:w)?\.exe)') { $c += $Matches[1] } } } catch {}
>> "%FIND_PS%" echo $roots = @('HKCU:\Software\Python', 'HKLM:\Software\Python', 'HKLM:\Software\WOW6432Node\Python')
>> "%FIND_PS%" echo foreach ($root in $roots) { if (Test-Path $root) { Get-ChildItem $root -Recurse ^| ForEach-Object { $props = Get-ItemProperty $_.PSPath; foreach ($prop in $props.PSObject.Properties) { if ($prop.Name -eq 'ExecutablePath') { $c += [string]$prop.Value }; if ($prop.Name -eq 'InstallPath') { $c += (Join-Path ([string]$prop.Value) 'python.exe') }; if ($prop.Name -eq '(default)' -and $_.PSChildName -eq 'InstallPath') { $c += (Join-Path ([string]$prop.Value) 'python.exe') } } } } }
>> "%FIND_PS%" echo foreach ($name in @('python.exe','python3.exe','python3.15.exe','python3.14.exe','python3.13.exe','python3.12.exe','python3.11.exe','python3.10.exe','python3.9.exe','python3.8.exe','python3.7.exe')) { $cmd = Get-Command $name; if ($cmd) { $c += $cmd.Source } }
>> "%FIND_PS%" echo foreach ($base in @($env:LOCALAPPDATA + '\Programs\Python', $env:ProgramFiles, ${env:ProgramFiles(x86)})) { if ($base -and (Test-Path $base)) { Get-ChildItem $base -Filter python.exe -Recurse -Depth 2 ^| ForEach-Object { $c += $_.FullName } } }
>> "%FIND_PS%" echo $infos = @()
>> "%FIND_PS%" echo foreach ($p in ($c ^| Where-Object { $_ } ^| Select-Object -Unique)) { if (Test-Path $p) { $out = ^& $p --version 2^>^&1 ^| Out-String; if ($out -match 'Python\s+(\d+)\.(\d+)\.(\d+)') { $infos += [pscustomobject]@{Major=[int]$Matches[1]; Minor=[int]$Matches[2]; Patch=[int]$Matches[3]; Path=(Resolve-Path $p).Path} } } }
>> "%FIND_PS%" echo $infos ^| Sort-Object Major, Minor, Patch -Descending ^| Select-Object -First 1 -ExpandProperty Path

for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%FIND_PS%"`) do set "NEWEST_PYTHON=%%P"
del "%FIND_PS%" >nul 2>nul

if not defined NEWEST_PYTHON (
  echo No usable Python interpreter was found. 1>&2
  exit /b 1
)

"%NEWEST_PYTHON%" -m uv --version >nul 2>nul
if errorlevel 1 (
  choice /M "uv is missing from %NEWEST_PYTHON%. Install uv into this Python"
  if errorlevel 2 (
    echo uv is required to run Tuv. 1>&2
    exit /b 1
  )
  "%NEWEST_PYTHON%" -m pip --version >nul 2>nul
  if errorlevel 1 "%NEWEST_PYTHON%" -m ensurepip --upgrade
  "%NEWEST_PYTHON%" -m pip install uv
  if errorlevel 1 exit /b 1
)

if not exist "%RUNNER%\Scripts\python.exe" (
  "%NEWEST_PYTHON%" -m uv venv --allow-existing --python "%NEWEST_PYTHON%" "%RUNNER%"
  if errorlevel 1 exit /b 1
)

set "REQ_HASH="
for /f "skip=1 tokens=1" %%H in ('certutil -hashfile "%REQ%" SHA256 ^| findstr /R "^[0-9A-Fa-f][0-9A-Fa-f]"') do if not defined REQ_HASH set "REQ_HASH=%%H"
set "STATE=%RUNNER%\.tuv-requirements-state"
set "STATE_HASH="
if exist "%STATE%" set /p STATE_HASH=<"%STATE%"

if not "%REQ_HASH%"=="%STATE_HASH%" (
  "%NEWEST_PYTHON%" -m uv pip install --python "%RUNNER%" -r "%REQ%"
  if errorlevel 1 exit /b 1
  > "%STATE%" echo %REQ_HASH%
)

set "TUV_HOME=%TUV_HOME%"
"%RUNNER%\Scripts\python.exe" "%APP%" %*

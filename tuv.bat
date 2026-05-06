@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "TUV_HOME=%~dp0"
if "%TUV_HOME:~-1%"=="\" set "TUV_HOME=%TUV_HOME:~0,-1%"
set "REQ=%TUV_HOME%\requirements.txt"
set "APP=%TUV_HOME%\tuv.py"
set "LAUNCHER_MODE=default"
set "FORWARD_ARGS="

if "%~1"=="." (
  set "LAUNCHER_MODE=cwd"
  shift
)

:collect_args
if "%~1"=="" goto args_done
set "FORWARD_ARGS=%FORWARD_ARGS% "%~1""
shift
goto collect_args

:args_done
if not exist "%APP%" (
  echo tuv.py was not found in %TUV_HOME% 1>&2
  exit /b 1
)

if not exist "%REQ%" (
  echo requirements.txt was not found in %TUV_HOME% 1>&2
  exit /b 1
)

for /f "delims=" %%T in ('powershell -NoProfile -Command "[IO.Path]::Combine($env:TEMP, 'tuv-bootstrap-' + [IO.Path]::GetRandomFileName() + '.ps1')"') do set "FIND_PS=%%T"
> "%FIND_PS%" echo $ErrorActionPreference = 'SilentlyContinue'
>> "%FIND_PS%" echo $c = @()
>> "%FIND_PS%" echo $cwd = (Get-Location).Path
>> "%FIND_PS%" echo foreach ($rel in @('python.exe','python3.exe','Scripts\python.exe','bin\python.exe')) { $p = Join-Path $cwd $rel; if (Test-Path $p) { $c += $p } }
>> "%FIND_PS%" echo try { ^& py -0p 2^>^&1 ^| ForEach-Object { if ($_ -match '([A-Za-z]:\\.*?python(?:w)?\.exe)') { $c += $Matches[1] } } } catch {}
>> "%FIND_PS%" echo $roots = @('HKCU:\Software\Python', 'HKLM:\Software\Python', 'HKLM:\Software\WOW6432Node\Python')
>> "%FIND_PS%" echo foreach ($root in $roots) { if (Test-Path $root) { Get-ChildItem $root -Recurse ^| ForEach-Object { $props = Get-ItemProperty $_.PSPath; foreach ($prop in $props.PSObject.Properties) { if ($prop.Name -eq 'ExecutablePath') { $c += [string]$prop.Value }; if ($prop.Name -eq 'InstallPath') { $c += (Join-Path ([string]$prop.Value) 'python.exe') }; if ($prop.Name -eq '(default)' -and $_.PSChildName -eq 'InstallPath') { $c += (Join-Path ([string]$prop.Value) 'python.exe') } } } } }
>> "%FIND_PS%" echo foreach ($name in @('python.exe','python3.exe','python3.15.exe','python3.14.exe','python3.13.exe','python3.12.exe','python3.11.exe','python3.10.exe','python3.9.exe','python3.8.exe','python3.7.exe')) { $cmd = Get-Command $name; if ($cmd) { $c += $cmd.Source } }
>> "%FIND_PS%" echo foreach ($base in @($env:LOCALAPPDATA + '\Programs\Python', $env:ProgramFiles, ${env:ProgramFiles(x86)})) { if ($base -and (Test-Path $base)) { Get-ChildItem $base -Filter python.exe -Recurse -Depth 2 ^| ForEach-Object { $c += $_.FullName } } }
>> "%FIND_PS%" echo $infos = @()
>> "%FIND_PS%" echo foreach ($p in ($c ^| Where-Object { $_ } ^| Select-Object -Unique)) { if (Test-Path $p) { $out = ^& $p -c "import sys; print(str(sys.version_info[0]) + ' ' + str(sys.version_info[1]) + ' ' + str(sys.version_info[2]) + ' ' + sys.executable)" 2^>^&1 ^| Out-String; if ($LASTEXITCODE -eq 0 -and $out -match '^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+?)\s*$') { $infos += [pscustomobject]@{Major=[int]$Matches[1]; Minor=[int]$Matches[2]; Patch=[int]$Matches[3]; Path=(Resolve-Path $Matches[4]).Path} } } }
>> "%FIND_PS%" echo $infos ^| Sort-Object Major, Minor, Patch -Descending ^| Select-Object -First 1 -ExpandProperty Path

for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%FIND_PS%"`) do set "BOOTSTRAP_PYTHON=%%P"
del "%FIND_PS%" >nul 2>nul

if not defined BOOTSTRAP_PYTHON (
  echo No usable Python interpreter was found. 1>&2
  exit /b 1
)

set "TUV_HOME=%TUV_HOME%"
for /f "delims=" %%T in ('powershell -NoProfile -Command "[IO.Path]::Combine($env:TEMP, 'tuv-runner-' + [IO.Path]::GetRandomFileName() + '.txt')"') do set "PREP_OUT=%%T"
"%BOOTSTRAP_PYTHON%" "%APP%" --prepare-runner --launcher-mode "%LAUNCHER_MODE%" > "%PREP_OUT%"
if errorlevel 1 (
  type "%PREP_OUT%" 1>&2
  del "%PREP_OUT%" >nul 2>nul
  exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in ("%PREP_OUT%") do (
  if "%%A"=="TUV_NEWEST_PYTHON" set "TUV_NEWEST_PYTHON=%%B"
  if "%%A"=="TUV_RUNNER_VENV" set "TUV_RUNNER_VENV=%%B"
  if "%%A"=="TUV_RUNNER_PYTHON" set "TUV_RUNNER_PYTHON=%%B"
)
del "%PREP_OUT%" >nul 2>nul

if not defined TUV_RUNNER_VENV (
  echo Tuv runner preparation did not return a runner venv. 1>&2
  exit /b 1
)

if not defined TUV_RUNNER_PYTHON (
  echo Tuv runner preparation did not return a runner Python. 1>&2
  exit /b 1
)

"%TUV_RUNNER_PYTHON%" -m pip --version >nul 2>nul
if errorlevel 1 "%TUV_RUNNER_PYTHON%" -m ensurepip --upgrade
if errorlevel 1 exit /b 1

"%TUV_RUNNER_PYTHON%" -m uv --version >nul 2>nul
if errorlevel 1 "%TUV_RUNNER_PYTHON%" -m pip install uv
if errorlevel 1 exit /b 1

set "REQ_HASH="
for /f "tokens=1" %%H in ('certutil -hashfile "%REQ%" SHA256 ^| findstr /R "^[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]"') do if not defined REQ_HASH set "REQ_HASH=%%H"
set "STATE=%TUV_RUNNER_VENV%\.tuv-requirements-state"
set "STATE_HASH="
if exist "%STATE%" set /p STATE_HASH=<"%STATE%"

if not "%REQ_HASH%"=="%STATE_HASH%" (
  "%TUV_RUNNER_PYTHON%" -m pip install -r "%REQ%"
  if errorlevel 1 exit /b 1
  > "%STATE%" echo(%REQ_HASH%
)

"%TUV_RUNNER_PYTHON%" -c "import packaging" >nul 2>nul
if errorlevel 1 (
  "%TUV_RUNNER_PYTHON%" -m pip install -r "%REQ%"
  if errorlevel 1 exit /b 1
  > "%STATE%" echo(%REQ_HASH%
)

set "TUV_SYSTEM_UV_EXE="
for /f "delims=" %%U in ('where uv 2^>nul') do if not defined TUV_SYSTEM_UV_EXE set "TUV_SYSTEM_UV_EXE=%%U"
if defined TUV_SYSTEM_UV_EXE (
  "%TUV_SYSTEM_UV_EXE%" --version >nul 2>nul
  if errorlevel 1 set "TUV_SYSTEM_UV_EXE="
)

"%TUV_RUNNER_PYTHON%" "%APP%" %FORWARD_ARGS%
exit /b %ERRORLEVEL%

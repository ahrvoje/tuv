# Tuv Specification

Tuv is an alternate-screen terminal UI Python package manager backed by `uv`. It gives users a fast, keyboard-driven way to inspect installed packages for a selected Python context, choose target versions, and run package installations without leaving the terminal.

## Goals

- Provide a full-screen TUI that runs in the terminal alternate screen and restores the original terminal contents on exit.
- Implement the TUI with native Python terminal control, not a third-party TUI framework.
- Use `uv` as the backend for Python environment inspection and package installation, resolving the uv executable per selected context from the most local usable installation to the most global fallback.
- Start from a small platform launcher that discovers the newest available Python interpreter, prepares or repairs the Tuv runner venv with that interpreter, ensures runner pip and uv, installs requirements, and runs `tuv.py`.
- Keep all launcher references script-relative.
- Let the user select a discovered Python interpreter, a current-working-directory Python distribution, a current-directory virtual environment, the active virtual environment, or the Tuv runner venv.
- Show a tabular overview of all installed packages in the selected context.
- Allow target version selection from the table and run installation for the focused package.
- Treat perceived snappiness as a deliberate design choice: show the first useful TUI frame as soon as runner bootstrap permits, avoid blocking it on package metadata, and keep package metadata and operations asynchronous.
- Be skilled at discovering the newest available Python interpreter on each supported platform.

## Non-Goals

- Replacing `uv` project management, lockfile management, or `pyproject.toml` editing.
- Creating, deleting, or repairing user project virtual environments.
- Supporting package search and first-time installation of packages that are not already installed.
- Running concurrent package mutations.
- Silently modifying a system Python installation without an explicit confirmation.
- Overriding externally managed Python protections such as `--break-system-packages`.

## Deployment Artifacts

The complete deployment consists of exactly these files:

- `tuv.bat`: Windows launcher.
- `tuv.sh`: Linux and macOS launcher.
- `tuv.py`: cross-platform Python application containing the Tuv implementation.
- `requirements.txt`: Python runtime requirements for the Tuv runner venv.

Generated files and directories, such as the runner virtual environment and install-state markers, are runtime artifacts and are not part of the deployment.

## User Entry Point

Users start Tuv through the platform launcher:

- Windows: `tuv.bat`
- Linux/macOS: `tuv.sh`

The launchers are expected to be placed on `PATH`, but all file references are resolved relative to the launcher script directory. There is no separate repository-root lookup.

Launcher responsibilities:

1. Print an immediate concise startup line before any potentially slow discovery, venv, or dependency work.
2. Determine the launcher directory and use it as `TUV_HOME`.
3. Locate `tuv.py` and `requirements.txt` in `TUV_HOME`.
4. Parse launcher-only runner selection arguments before creating the runner venv. The literal dot argument `.` is consumed by the launcher and is not forwarded to `tuv.py`.
5. If Tuv is started as `tuv .`, `tuv.sh .`, or `tuv.bat .`, explicitly select the current-working-directory Python as the runner Python.
6. Without the dot argument, discover platform runner Python candidates first and select the newest usable platform interpreter; use a current-working-directory Python only as a fallback when no usable platform interpreter exists.
7. After selecting the runner Python, find a compatible Tuv runner venv under `TUV_HOME`; if none exists, create a new script-relative runner venv with a hash suffix such as `TUV_HOME/tuv-venv-1a3b8e4f`.
8. Verify that the selected runner venv Python exists and can execute the standard JSON probe. If it is missing, removed, broken, or points at a removed base interpreter, mark that runner venv incompatible and select or create another compatible runner venv.
9. Record the interpreter used to build the runner venv. If a different runner Python is later selected, use a compatible runner venv for that interpreter instead of mutating an incompatible runner venv in place.
10. Ensure the selected Tuv runner venv can be put into functional mode. Functional mode means runner Python works, `pip` works or can be restored with `ensurepip`, and `uv` works or can be installed through runner `pip`.
11. Ensure `pip` in the selected Tuv runner venv. If `<tuv-venv-python> -m pip --version` fails, run `<tuv-venv-python> -m ensurepip --upgrade`, then check runner `pip` again.
12. Ensure `uv` in the selected Tuv runner venv. If `<tuv-venv-python> -m uv --version` fails, install it with `<tuv-venv-python> -m pip install uv`, after ensuring runner pip.
13. If runner `pip`, runner `uv`, and runner `ensurepip` are all unavailable or cannot make the runner venv functional, exit gracefully with an informative message instead of starting a broken TUI.
14. Install `requirements.txt` into the selected runner venv using the runner venv Python.
15. Detect whether a standalone system `uv` executable is available by running `uv --version`.
16. Execute `TUV_HOME/tuv.py` with the selected runner venv Python.
17. Forward remaining CLI arguments to `tuv.py`.

Startup shell feedback:

- The launcher must write human progress lines to `stderr`, not `stdout`, so helper output and machine-readable preparation values remain separate.
- The first progress line must be emitted immediately after launch argument parsing starts or completes, before runner Python discovery.
- Progress lines should be short, stable, and prefixed consistently, for example `tuv: starting`, `tuv: discovering runner Python`, `tuv: preparing runner environment`, `tuv: ensuring runner pip`, `tuv: ensuring runner uv`, `tuv: checking runner requirements`, `tuv: checking system uv provider`, and `tuv: entering terminal UI`.
- When a meaningful path is known, show it on a follow-up progress line, especially the selected runner Python and selected runner venv path.
- The launcher should print extra lines only when slow repair work is actually performed, such as restoring runner `pip` with `ensurepip`, installing runner `uv`, or reinstalling `requirements.txt`.
- These progress lines are launcher-time shell output only. Once `tuv.py` enters alternate-screen mode, normal TUI status rendering owns further feedback.

The launcher should pass discovery anchors to `tuv.py` through environment variables:

- `TUV_NEWEST_PYTHON=<absolute path to selected runner base Python>`.
- `TUV_RUNNER_VENV=<absolute path to selected Tuv runner venv>`.
- `TUV_RUNNER_PYTHON=<absolute path to Tuv runner venv Python>`.
- `TUV_SYSTEM_UV_EXE=<absolute path to standalone system uv>`, only when standalone system uv is available.

`TUV_NEWEST_PYTHON` is a historical environment variable name. In default mode it contains the newest usable platform Python. In explicit cwd-runner mode it contains the selected current-working-directory Python.

The launcher must continue to discover the most recent usable platform Python interpreter for default launches and use it for the Tuv runner environment. This preserves predictable startup and venv management even when package operations later choose a more local uv provider for a selected context. The explicit dot argument overrides this default and intentionally uses the current-working-directory Python as the runner Python.

The launcher must not require `pip` or `uv` in the base interpreter used to create the Tuv runner venv. Base Python only needs to be able to execute the probe and create or repair the Tuv runner venv. All Tuv-owned dependency bootstrapping after venv creation happens inside the Tuv runner venv. However, the selected runner path must ultimately produce a functional runner venv; if the runner venv cannot obtain working `pip` through existing runner `pip` or runner `ensurepip`, and therefore cannot install or run runner-local `uv`, the launcher must report the problem clearly.

When bootstrapping requires network access and dependencies cannot be installed because the network or package index is unavailable, Tuv must exit gracefully with a message that names the unavailable runner dependency and suggests retrying with network access or pre-populating the runner venv. It must not leave the terminal in alternate-screen mode for launcher-time failures.

When a standalone system provider or Tuv runner venv provider is available, Tuv must not require `uv` or `pip` to be installed in any selected target context. This allows Tuv to inspect and manage Python distributions that contain a Python interpreter but lack both `uv` and `pip`.

Runner Python discovery sequence:

1. Define the discovery current working directory as the directory from which the user launched Tuv, not `TUV_HOME`.
2. If the first non-launcher argument is the literal dot argument `.`, enter explicit cwd-runner mode:
   - Discover only current-working-directory Python candidates.
   - Select the newest usable current-working-directory candidate as `NEWEST_PYTHON`.
   - If no usable current-working-directory Python exists, exit with a clear message.
   - Do not fall back to platform discovery in this mode, because the user explicitly requested cwd Python as the runner.
3. Without the dot argument, add platform discovery candidates first:
   - Windows: Python Launcher output from `py -0p`, PEP 514 registry locations, `PATH` executables, and common install directories.
   - POSIX: common `PATH` names such as `python3.13`, `python3.12`, `python3`, and `python`, plus common install directories.
4. Probe each platform candidate by executing it. A runner-usable interpreter must report its version and executable path, must not be a virtual environment interpreter, must be able to import the standard-library `venv` module, and must be capable of creating a runner venv that can be made functional.
5. Ignore platform candidates that cannot execute, cannot report a version, are unsupported, are virtual environment interpreters, or cannot create or maintain the Tuv runner venv.
6. Deduplicate usable platform candidates by resolved executable path.
7. Sort usable platform candidates by semantic Python version, newest first.
8. If at least one usable platform candidate remains, select the first candidate as `NEWEST_PYTHON`.
9. Only when no usable platform candidate exists, add current-working-directory Python fallback candidates, unless the current working directory contains `pyvenv.cfg`. A directory containing `pyvenv.cfg` is a virtual environment context, not a default base runner Python candidate.
10. Current-working-directory candidate paths:
   - Windows: `python.exe`, `python3.exe`, `Scripts\python.exe`, and `bin\python.exe`.
   - POSIX: `python`, `python3`, `bin/python`, and `bin/python3`.
11. In explicit cwd-runner mode, a current working directory containing `pyvenv.cfg` may use its venv Python as the explicit cwd Python when that Python is runner-usable. In default mode, a current working directory containing `pyvenv.cfg` remains a virtual environment context and is not used as the runner fallback.
12. Probe, filter, deduplicate, and sort current-working-directory candidates with the same rules used for platform candidates, except that explicit cwd-runner mode may intentionally accept a current-directory virtual environment interpreter when the current directory contains `pyvenv.cfg`.
13. If no usable platform candidate exists but a usable current-working-directory fallback candidate exists, select the newest fallback candidate as `NEWEST_PYTHON`.
14. If neither platform nor current-working-directory fallback discovery yields a usable interpreter, exit with a clear message that no usable Python interpreter was found.
15. Use `NEWEST_PYTHON` only as the base interpreter for selecting, creating, or repairing a Tuv runner venv and for Tuv-owned venv management. `NEWEST_PYTHON` does not need `pip` or `uv`.

Runner venv compatibility and naming:

1. A Tuv runner venv is script-relative and must live directly under `TUV_HOME`.
2. New runner venvs must be named `tuv-venv-<hash>`, where `<hash>` is a short lowercase hexadecimal suffix, for example `tuv-venv-1a3b8e4f`.
3. The hash suffix should be derived from a stable runner compatibility key containing at least the selected runner Python resolved executable path, Python version, implementation, architecture, operating system, and launcher mode (`default` or explicit cwd-runner).
4. Before creating a runner venv, scan existing script-relative Tuv runner venv directories, including `tuv-venv-*` and any legacy `.tuv-venv`.
5. A runner venv is compatible when its state marker matches the selected runner compatibility key, its runner Python executable exists and runs, its recorded base interpreter still exists, and it can execute the standard JSON probe.
6. If one compatible runner venv exists, reuse it. If multiple compatible runner venvs exist, choose the newest valid state marker timestamp, breaking ties by path name.
7. If no compatible runner venv exists, create a new runner venv under `TUV_HOME/tuv-venv-<hash>`. Do not delete or overwrite incompatible Tuv runner venvs during selection.
8. If the deterministic hash path already exists but is incompatible, create a novel unused hash-suffixed path by adding collision input such as a timestamp or random nonce to the hash seed.
9. Record the selected runner base interpreter path, version, compatibility key, compatibility hash, launcher mode, and creation or update timestamp in `<runner-venv>/.tuv-runner-state`.
10. The selected runner venv path is passed to `tuv.py` as `TUV_RUNNER_VENV`; the selected runner venv Python is passed as `TUV_RUNNER_PYTHON`.
11. On later launches, select a different compatible runner venv or create a new one before starting `tuv.py` when the current runner venv is missing, broken, incompatible with the selected runner Python, or built from a removed base interpreter.

Recommended POSIX launcher flow:

```sh
TUV_HOME="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LAUNCHER_MODE="default"
if [ "${1:-}" = "." ]; then
  LAUNCHER_MODE="cwd"
  shift
fi
NEWEST_PYTHON="$(select_runner_python "$LAUNCHER_MODE")"
RUNNER="$(find_or_create_compatible_runner "$NEWEST_PYTHON" "$LAUNCHER_MODE")"
RUNNER_PYTHON="$RUNNER/bin/python"
RUNNER_STATE="$RUNNER/.tuv-runner-state"
if runner_python_missing_or_broken "$RUNNER_PYTHON" || ! runner_state_is_compatible "$RUNNER_STATE" "$NEWEST_PYTHON" "$LAUNCHER_MODE"; then
  mark_runner_incompatible "$RUNNER"
  RUNNER="$(create_new_hashed_runner "$NEWEST_PYTHON" "$LAUNCHER_MODE")"
  RUNNER_PYTHON="$RUNNER/bin/python"
  RUNNER_STATE="$RUNNER/.tuv-runner-state"
  "$NEWEST_PYTHON" -m venv "$RUNNER"
  write_runner_state "$RUNNER_STATE" "$NEWEST_PYTHON"
fi
"$RUNNER_PYTHON" -m pip --version >/dev/null 2>&1 || "$RUNNER_PYTHON" -m ensurepip --upgrade
"$RUNNER_PYTHON" -m pip --version >/dev/null 2>&1 || fail_runner_bootstrap "runner pip is unavailable and ensurepip could not restore it"
"$RUNNER_PYTHON" -m uv --version >/dev/null 2>&1 || "$RUNNER_PYTHON" -m pip install uv
"$RUNNER_PYTHON" -m uv --version >/dev/null 2>&1 || fail_runner_bootstrap "runner uv is unavailable and could not be installed"
"$RUNNER_PYTHON" -m pip install -r "$TUV_HOME/requirements.txt"
if command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; then
  export TUV_SYSTEM_UV_EXE="$(command -v uv)"
fi
export TUV_NEWEST_PYTHON="$NEWEST_PYTHON"
export TUV_RUNNER_VENV="$RUNNER"
export TUV_RUNNER_PYTHON="$RUNNER_PYTHON"
"$RUNNER_PYTHON" "$TUV_HOME/tuv.py" "$@"
```

Recommended Windows launcher flow:

```bat
set "TUV_HOME=%~dp0"
set "LAUNCHER_MODE=default"
if "%~1"=="." (
  set "LAUNCHER_MODE=cwd"
  shift
)
set "NEWEST_PYTHON=<selected-runner-python>"
set "RUNNER=<compatible-or-new-hashed-runner-venv>"
set "RUNNER_PYTHON=%RUNNER%\Scripts\python.exe"
set "RUNNER_STATE=%RUNNER%\.tuv-runner-state"
call :runner_python_missing_or_broken "%RUNNER_PYTHON%"
if errorlevel 1 goto repair_runner
call :runner_state_is_compatible "%RUNNER_STATE%" "%NEWEST_PYTHON%" "%LAUNCHER_MODE%"
if errorlevel 1 goto repair_runner
goto runner_ready
:repair_runner
call :mark_runner_incompatible "%RUNNER%"
set "RUNNER=<new-unused-hashed-runner-venv>"
set "RUNNER_PYTHON=%RUNNER%\Scripts\python.exe"
set "RUNNER_STATE=%RUNNER%\.tuv-runner-state"
"%NEWEST_PYTHON%" -m venv "%RUNNER%"
call :write_runner_state "%RUNNER_STATE%" "%NEWEST_PYTHON%"
:runner_ready
"%RUNNER_PYTHON%" -m pip --version >nul 2>nul || "%RUNNER_PYTHON%" -m ensurepip --upgrade
"%RUNNER_PYTHON%" -m pip --version >nul 2>nul || goto runner_bootstrap_failed
"%RUNNER_PYTHON%" -m uv --version >nul 2>nul || "%RUNNER_PYTHON%" -m pip install uv
"%RUNNER_PYTHON%" -m uv --version >nul 2>nul || goto runner_bootstrap_failed
"%RUNNER_PYTHON%" -m pip install -r "%TUV_HOME%\requirements.txt"
for /f "delims=" %%I in ('where uv 2^>nul') do if not defined TUV_SYSTEM_UV_EXE set "TUV_SYSTEM_UV_EXE=%%I"
if defined TUV_SYSTEM_UV_EXE "%TUV_SYSTEM_UV_EXE%" --version >nul 2>nul || set "TUV_SYSTEM_UV_EXE="
set "TUV_NEWEST_PYTHON=%NEWEST_PYTHON%"
set "TUV_RUNNER_VENV=%RUNNER%"
set "TUV_RUNNER_PYTHON=%RUNNER_PYTHON%"
"%RUNNER_PYTHON%" "%TUV_HOME%\tuv.py" %*
```

The launcher should avoid reinstalling requirements on every run. It should store a hash or timestamp marker for `requirements.txt` under the selected runner venv, at `<runner-venv>/.tuv-requirements-state`, and reinstall only when the file changes.

Runner repair must be careful but automatic. A launcher may mark incompatible only a script-relative Tuv-owned runner directory whose name is `tuv-venv-<hash>` or the legacy `.tuv-venv`, after resolving the path and verifying it is directly inside `TUV_HOME`. It must not touch arbitrary virtual environments. A broken or stale runner venv is a Tuv-owned runtime artifact, not user project state.

If runner bootstrap fails because `pip`, `ensurepip`, or `uv` cannot be made available, the launcher must print a concise diagnostic that includes the selected runner Python path, runner venv path, and the failing step. Network or index failures during runner dependency installation should be reported as bootstrap failures, not as TUI package-operation failures.

## Python and uv Discovery

Tuv resolves a uv package-operation provider for the selected Python context. Resolution is per operation and must prefer the most local usable uv installation:

1. Context venv uv: when the selected context is a non-Tuv virtual environment and that venv Python can run `<venv-python> -m uv --version`, use `<venv-python> -m uv`.
2. Reference Python interpreter uv: use the reference Python for the selected context when it can run `<reference-python> -m uv --version`. For interpreter contexts, the reference Python is the context interpreter. For virtual environment contexts, the reference Python is the base interpreter reported by the venv probe when it can be resolved; otherwise this provider is skipped.
3. Standalone system uv: use the standalone executable reported by `TUV_SYSTEM_UV_EXE` or found by `uv --version`.
4. Tuv runner venv uv: use `<TUV_RUNNER_PYTHON> -m uv` as the final fallback.

The reference Python for a virtual environment should be resolved from reliable interpreter metadata such as `pyvenv.cfg` `home`, the probe's `sys.base_prefix` or `sys._base_executable` when available, or an already-discovered interpreter with the same base prefix. If no reference interpreter can be resolved, skip the reference provider and continue down the hierarchy.

Provider invocation:

```sh
# venv, reference interpreter, or Tuv runner venv provider
<provider-python> -m uv ...

# standalone system provider
<uv-executable> ...
```

Tuv must not require standalone `uv` on `PATH`; it only uses it after more local Python-module uv providers are unavailable. Tuv must also not require `uv` or `pip` inside the selected target context when a less local provider can manage that context. All package inspection and package mutation commands should use the resolved uv provider with `--python <context>`, where `<context>` is either a target interpreter executable or a target virtual environment root.

Interpreter discovery for the TUI:

- Tuv should reuse the runner Python candidate sources for interpreter contexts, including current-working-directory Python distributions and normal installed interpreters.
- The launcher-provided `TUV_NEWEST_PYTHON` should be included as a selected runner Python anchor when it is not otherwise discovered by `tuv.py`.
- Probe each candidate by executing it and reading JSON from:

```sh
<python> -c "import json, sys; print(json.dumps({'version': sys.version_info[:3], 'executable': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'base_executable': getattr(sys, '_base_executable', None)}))"
```

- Ignore candidates that cannot execute, cannot report a version, or are unsupported.
- For interpreter contexts, ignore candidates whose probe reports a virtual environment (`sys.prefix` differs from `sys.base_prefix`) or whose executable is inside a directory containing `pyvenv.cfg`; those candidates must be represented only as virtual environment contexts.
- Deduplicate by resolved executable path.
- Sort by semantic Python version, preferring higher version numbers.
- Current-working-directory interpreters are deduplicated by executable path like all other candidates, but their context metadata should preserve that they were found from `cwd` so the selector can label them clearly.

uv provider bootstrap:

- Probe uv providers in the defined hierarchy for each selected context.
- Runner startup must ensure both `pip` and `uv` are available inside the Tuv runner venv before launching `tuv.py`.
- Runner startup must verify the runner venv reached functional mode: runner Python executes, runner `pip` works, and runner `uv` works.
- Never offer to install `uv` into a selected context virtual environment.
- Never offer to install `uv` into a selected interpreter context.
- If no usable provider can be resolved after startup, repair or re-bootstrap `uv` in the Tuv runner venv.
- Install or repair Tuv runner venv `uv` with `<tuv-venv-python> -m pip install uv`.
- If `pip` is unavailable in the Tuv runner venv, run `<tuv-venv-python> -m ensurepip --upgrade` before installing `uv`, then verify `pip` again.
- If runner `pip` is still unavailable and runner `ensurepip` cannot restore it, Tuv cannot ensure a compatible runner environment; exit gracefully with an informative bootstrap error.
- If runner `pip` works but runner `uv` cannot be installed because the network or configured index is unavailable, exit gracefully with an informative bootstrap error and leave user project environments untouched.
- If a target context lacks both `uv` and `pip`, still offer it when any resolved uv provider can run `uv pip ... --python <context>`.
- Never install `uv` into a non-Tuv environment silently. Tuv-owned runner venv pip and uv bootstrap may be automatic because the selected hash-suffixed runner venv is a Tuv runtime artifact.

This bootstrap restriction applies only to Tuv's own dependencies. In normal package-management flows, `pip` and `uv` are regular package rows when they are installed in the selected context. Tuv may update them in the current context when the user explicitly selects them, presses `Enter`, or confirms `F2` bulk update. Tuv must not install `pip` or `uv` into a selected context merely to make Tuv itself work.

## Native TUI Proposal

Tuv should use a small native terminal UI layer implemented directly in `tuv.py`.

Conclusion:

- A Tuv-sized table UI is simple enough to implement without a TUI framework.
- Native Python can enter the alternate screen, clear/redraw the viewport, read keyboard input, and run background install jobs.
- This keeps the deployment smaller and makes the UI behavior explicit.

Required native terminal behavior:

- Enter alternate screen on startup with ANSI control sequence `ESC [?1049h`.
- Leave alternate screen on all exits with `ESC [?1049l`.
- Hide the cursor while drawing and restore it on exit.
- Use `try`/`finally` so terminal state is restored after errors.
- Use `os.get_terminal_size()` for responsive table sizing.
- Redraw the screen after input, package refresh, installation status changes, spinner ticks, and resize events.
- Decode common keyboard sequences for arrows, `PageUp`, `PageDown`, `Left`, `Right`, `Enter`, `F3`, `F4`, `F9`, `F10`, refresh, and quit.
- Decode `F2` for updating all ready packages.
- Decode `F4` for opening a package version selector overlay.
- Decode `Esc` and `q` for closing every modal overlay or dialog.
- Decode `F10` and main-screen `q` for quitting the application when no modal overlay or dialog is active.

Input handling:

- POSIX: use `termios`, `tty`, and `select` for raw, non-blocking input.
- Windows: use `msvcrt.getwch()` for keyboard input.
- Windows ANSI rendering should enable virtual terminal processing through `ctypes` when needed.
- Because function-key sequences vary between terminals, `F9` should support common sequences and may have a fallback context-selector key if a terminal cannot report `F9` reliably.

Native UI responsibilities:

- Maintain table viewport state, focused row, and scroll offset.
- Render the context selector as a lightweight combo overlay.
- Render package version choices as a lightweight combo overlay.
- When any modal dialog or selector is active, render the background content dimmed: it should lose color intensity and appear slightly darker while the modal remains visually dominant.
- When a modal overlay or dialog has a title, embed the title elegantly into the top-left portion of the dialog border instead of placing it as a separate body line.
- Use Unicode box-drawing characters for internal separators so table lines render as continuous terminal lines.
- Do not draw far-left, far-right, top, or bottom boundary lines around the main table; preserving space is preferred.
- Use Unicode arrow characters in the bottom key legend.
- Use a Unicode enter symbol in the bottom key legend.
- Render failed rows in bold red.
- Render current packages in light green.
- Render the installation spinner in the fourth column.
- Start the TUI shell immediately after launch; package tables may appear with installed versions first while latest target versions continue loading asynchronously.
- Keep install subprocesses off the input/render loop with `threading` or `asyncio`.
- Keep package refresh, outdated-version lookup, candidate-version lookup, and install subprocesses off the input/render loop.
- Run installation jobs asynchronously so the TUI remains responsive for navigation, context viewing, status updates, and information panels during installation activity.
- Do not run concurrent installations because dependency resolution and environment mutation can clash.
- Run bulk updates sequentially and re-check package state after each install.
- Restore terminal modes, colors, cursor visibility, and alternate-screen state after normal exits, exceptions, and interrupted runs.

## Runtime Dependencies

Tuv should keep runtime dependencies small.

Recommended dependencies:

- `packaging`: package name normalization and version ordering.
- `uv`: backend package manager supplied by the resolved uv provider. It may be installed in a selected venv, installed in the selected context's reference interpreter, available as standalone system `uv`, or installed in the Tuv runner venv.

`uv` is ensured inside the Tuv runner venv at startup as the guaranteed fallback provider, while more local context providers and standalone system `uv` still take precedence for package operations. No TUI framework dependency is required. The launchers should rely only on shell or batch features, OS Python discovery commands, the selected newest Python interpreter, and any discovered uv providers. Python dependencies are required only after the runner environment exists, except for the confirmed Tuv-runner-venv `uv` bootstrap.

## Python Contexts

A Python context is the environment Tuv inspects and mutates.

Context types:

- `interpreter`: an installed or directly offered Python interpreter discovered by Tuv, including a current-working-directory Python distribution.
- `venv`: a PEP 405 virtual environment found from the current working directory or from `VIRTUAL_ENV`.
- `tuv`: the selected Tuv runner venv from `TUV_RUNNER_VENV`; this is always listed last.

The active virtual environment is not a separate context type in the state model. It is represented as `type=venv` with `source=active`, so it cannot be confused with an interpreter context.

Context discovery order:

1. Interpreter contexts first, newest first, including any current-working-directory Python distribution.
2. Virtual environment contexts next, including the active virtual environment from `VIRTUAL_ENV` and virtual environments under the current working directory.
3. Tuv runner venv last, labeled `tuv venv`; this context is always available.

Current-working-directory interpreter detection:

- `cwd` means the directory from which the user launched Tuv, not `TUV_HOME`.
- If the current working directory contains a runnable Python interpreter, offer it as an `interpreter` context even when it is not installed system-wide.
- If the current working directory contains `pyvenv.cfg`, classify it as a virtual environment instead of a cwd interpreter.
- If a discovered Python executable reports `sys.prefix != sys.base_prefix`, classify it as a virtual environment executable and do not include it in the interpreter section.
- On POSIX, check executable names such as `python`, `python3`, `bin/python`, and `bin/python3` under the current working directory.
- On Windows, check executable names such as `python.exe`, `python3.exe`, `Scripts/python.exe`, and `bin/python.exe` under the current working directory.
- Probe a current-working-directory interpreter with the same JSON probe used for installed interpreter discovery.
- Label this entry clearly, for example `cwd interpreter`, and include its Python version and path in the selector.
- Deduplicate it by resolved executable path, but preserve the `cwd` source label if the same interpreter is also found by another method.

Virtual environment detection:

- A directory is considered a virtual environment when it contains `pyvenv.cfg`.
- The executable must exist at `bin/python` on POSIX or `Scripts/python.exe` on Windows.
- Tuv scans the current working directory and direct child directories.
- Deduplicate virtual environment contexts by resolved root path, and secondarily by resolved Python executable path.
- If the same virtual environment is found through `VIRTUAL_ENV`, `.venv`, the current directory, and direct-child scanning, show one selector entry and preserve the most useful source labels, preferring `active` over `cwd` over `scanned`.
- The active virtual environment must never appear in the interpreter section merely because its `python` executable is found on `PATH`.

Default selected context:

1. Active virtual environment, when present.
2. `.venv` in the current working directory, when present.
3. Current-working-directory interpreter, when present.
4. Newest discovered interpreter.
5. `tuv venv`.

Interpreter contexts may refer to system Python installations. Installing into those contexts is potentially risky, so the first mutation in an interpreter context must show a confirmation dialog.

The selected context does not need to contain `uv` or `pip` when a less local provider can manage it. Tuv should resolve a uv provider for the selected context using the provider hierarchy, then inspect and mutate the context through `uv pip ... --python <context>`. A context should be hidden or marked unavailable only when no target interpreter executable can be found or no resolved uv provider can operate on it.

## Main Screen

The application opens directly into the package manager view in the terminal alternate screen.

Layout:

```text
[ .venv - Python 3.12.4 - C:\repo\.venv ]
────────────────────────────────────────────────────────────────────────────────
Package                         Installed             Target                Act
* pytest                        8.3.5                 8.3.5                 curr
* requests                      2.31.0                2.32.5                ready
  rich                          13.9.4                14.0.0                ready
────────────────────────────────────────────────────────────────────────────────
↑/↓ Row | PgUp/PgDn Jump | ←/→ Version | ↵ Install | F2 All | F3 Info
```

Top menu:

- Shows the selected context directly, without a leading `Context:` label.
- Omits idle/status text such as `Status: idle`; space is reserved for package data and controls.
- Contains a context selector.
- The selector is a combo control opened with `F9`.
- `F9` focuses the context selector and opens the context combo.
- The combo lists interpreter contexts first, virtual environment contexts second, and `tuv venv` last.
- Interpreter contexts include installed interpreters and any current-working-directory Python distribution.
- Virtual environment contexts include the active virtual environment and current-directory virtual environments.
- Each entry shows enough metadata to distinguish it: type, Python version, and path.
- Changing the selected context reloads the package table.

Table:

- First column: package name.
- Second column: current installed version.
- Third column: target version to be installed.
- Fourth column: action/status indicator.
- Package names may be prefixed with `* ` when the package can be uninstalled without breaking dependency requirements for other installed packages.
- Installed and target version columns should be wide enough for long PEP 440 versions; each should use a wider minimum than the action column, with a recommended minimum of 20 characters when terminal width allows it.
- Rows list all packages in alphabetical order by normalized package name.
- The focused row is visually distinct.
- Current packages are colored light green.
- Outdated packages should be visually distinguishable from current packages.
- Packages installed or updated during the current Tuv session are colored white.
- During a package-table refresh, rows that temporarily return to `loading` while status and target-version data is being recomputed should retain their previous stable row color when the package still exists. This retained color is only a rendering hint; it must not make the row actionable or preserve stale status semantics.
- Loading rows without a previous stable color should use a subdued neutral style rather than bright/default white.
- White must not be used as a default or transient refresh color for unchanged rows. Only packages whose installed version changed or newly appeared during the current Tuv session should receive the white updated-session styling.
- Failed installations are shown in bold red.

Visual priority:

1. Focused row indication.
2. Failed row: bold red.
3. Installed or updated during current session: white.
4. Current package, or a loading row whose retained refresh color was current: light green.
5. Outdated package, or a loading row whose retained refresh color was outdated: outdated styling.
6. Loading row without a retained refresh color: subdued neutral styling.

Bottom indicator:

- Shows currently available key binds.
- Must include:
  - `Up/Down`: move package selection by one row.
  - `PageUp/PageDown`: jump quickly through table rows.
  - `Left/Right`: change target version for the focused package.
  - `Enter`: start installation for the focused package.
  - `F2`: update all ready packages.
  - `F3`: open information for the focused row, especially install failure details.
  - `F4`: open a version selector for the focused package.
  - `F9`: focus and open the context selector.
  - `F10`: quit the application when no modal dialog or selector is active.
- May also include:
  - `R`: refresh package list.
  - `Q`: quit when no modal dialog or selector is active.

The bottom key legend must use Unicode arrows for arrow-key hints, for example `↑/↓ Row` and `←/→ Version`.
The bottom key legend must use a Unicode enter symbol for the install key, for example `↵ Install`.
Function-key hints in the bottom legend must be sorted by function key number, for example `F2 All | F3 Info | F4 Versions | F9 Context | F10 Quit`.
The bottom key legend does not need to include last-action or status-message text; status may be shown elsewhere in the UI.

## Package Data

Installed package list:

- Use `<resolved-uv-provider> pip list --python <context> --format json` for a selected context.
- For virtual environments, `<context>` may be the venv root path.
- For interpreter contexts, `<context>` should be the interpreter executable path.
- Resolve `<resolved-uv-provider>` through the uv provider hierarchy before each package operation.
- The selected context is the target of `--python`; it does not need its own `uv` or `pip` installation.
- Load the installed package list first so the table can appear as soon as possible.

Outdated package list:

- Use `<resolved-uv-provider> pip list --python <context> --outdated --format json`.
- Merge outdated data into the installed package table.
- Load and merge outdated data asynchronously after the initial installed package table is visible.
- While outdated data and package index resolution are still loading, target versions may temporarily equal installed versions, but rows must remain non-installable loading/current placeholders rather than actionable `ready` rows.
- Default target version:
  - Latest available version for outdated packages.
  - Installed version for current packages.
- A row may become `ready` only after Tuv has completed successful version resolution for that package against the effective package index configuration and has confirmed the selected target is a real installable candidate.

Version candidates:

- `Left` chooses the next older known version.
- `Right` chooses the next newer known version.
- `Left` and `Right` must consider the complete available install-version list for the focused package, not only the installed version and latest version.
- Pressing `Left` or `Right` before the complete version list has loaded must trigger candidate-version loading and apply the requested movement only after the list is available.
- `F4` opens an overlay combo selector containing all known installable versions for the focused package.
- In the version selector, `Enter` selects the highlighted version and starts installation for that package only after the complete candidate-version list has loaded successfully.
- While candidate-version lookup is loading or has failed, `Enter` in the version selector must not start installation.
- In the version selector, `Esc` closes the selector without changing or installing.
- Candidate versions should load lazily for the focused row.
- Candidate version lookup is core package-manager functionality and must be robust, testable, and treated as part of the package operation contract rather than a best-effort embellishment.
- Candidate version lookup must enumerate available versions through the effective configured package index API, preferably the Python Simple Repository API, including PEP 503 HTML responses and PEP 691 JSON responses where supported by the index.
- Candidate version lookup must use the same configured package server or index choices that uv will use for installation whenever those settings are discoverable, including index URL, extra index URL, credentials from supported environment variables, and local uv configuration files.
- Tuv must not silently fall back to a different public index when the selected context or project has an explicit configured index that cannot be queried. In that case, keep installed package data visible, mark version data unavailable, and keep affected rows non-installable until the lookup succeeds.
- Version lookup should use structured parsers for configuration and index responses where available; ad hoc regex parsing is acceptable only as a narrow fallback for formats that cannot otherwise be parsed.
- Version lookup should preserve enough diagnostic detail to explain which index/config source failed.
- If no project or environment-specific index configuration is discoverable, use the default Python package index.
- Filter index results to installable distribution versions for the selected package name, normalize versions with `packaging`, and sort with PEP 440 ordering.
- `uv` remains the authoritative installer and resolver. If metadata lookup offers a target that `uv` cannot install, surface the uv error and keep the row unchanged.

The initial implementation must implement the multi-version candidate provider before enabling `Left`, `Right`, or `F4` version navigation. A two-choice installed/latest-only implementation is not sufficient for these controls.

Uninstall-safe marker:

- Tuv should compute a reverse dependency view of installed packages for the selected context only from successful metadata collection that corresponds to the same installed package list currently displayed.
- Metadata correspondence means the metadata result identifies the same selected context and refresh generation, and its package-name set matches the displayed installed rows closely enough to be trusted.
- A package is marked with `* ` only when metadata collection succeeded, corresponds to the current table, and no other installed package declares a dependency requirement satisfied by that package.
- If dependency metadata collection fails, is partial, stale, or does not correspond to the current installed package list, do not show uninstall-safe markers for that refresh.
- The marker is informational; it does not add uninstall behavior.

Package metadata and dependency relationships:

- Tuv should combine multiple available techniques to identify package dependencies and usage relationships instead of relying on a single fragile source.
- Preferred sources include installed distribution metadata read through the selected context's interpreter, `importlib.metadata` fields such as `Requires-Dist` and summary, metadata files under installed `.dist-info` directories, and uv-provided inspection output when it is available.
- Dependency evaluation should normalize package names and evaluate non-extra environment markers for the selected context where possible.
- Because standard installed distribution metadata does not reliably record which extras were requested when a package was installed, Tuv must be conservative for uninstall-safety. If an installed package declares a `Requires-Dist` dependency that applies for the current Python/platform environment under any declared `Provides-Extra`, and the dependency package is installed, treat that dependency as used by the declaring package.
- Extra-gated dependencies that are incompatible with the current Python/platform environment must not be counted merely because the extra exists.
- The information panel should use the package summary or description metadata when available, preferring a short one-line summary over long project descriptions.
- Failed or untrusted metadata must be surfaced as unavailable metadata, not converted into empty dependency and usage relationships.

## Installation Flow

When the user presses `Enter` on a package row:

1. If the selected context is an interpreter context and has not been confirmed yet, show a confirmation dialog.
2. Resolve the uv provider for the selected context using the provider hierarchy.
3. If no uv provider is available or the resolved provider cannot operate on the selected context, repair or re-bootstrap `uv` in the Tuv runner venv. Do not offer to install `uv` into the selected context venv or interpreter.
4. If the row is still loading installed data, outdated data, or candidate versions, do not install; keep the row non-installable and show a short status message that version resolution is still in progress.
5. If the row's target version was not produced by successful candidate-version resolution for the effective index configuration, do not install.
6. If the target version equals the installed version, do nothing and show a short status message.
7. Mark the row as `installing`.
8. Start an asynchronous background worker that runs `uv pip install`.
9. Animate the fourth column while the process is running.
10. Capture stdout, stderr, exit code, and elapsed time.
11. After the uv process exits, refresh the entire package table for the selected context, because uv may update dependencies as part of the install.
12. On success, show the refreshed package versions and clear the completed row status.
13. On failure, mark the row as `failed`, render it bold red, and keep failure details available through `F3`.

Install command:

```sh
<resolved-uv-provider> pip install --python <context> "<package-name>==<target-version>"
```

`pip` and `uv` are not special-cased as package rows. If either package is installed in the selected context and appears in the table, Tuv may update it through the same explicit single-package or confirmed bulk-update flow as any other package. This permission does not allow Tuv to install `pip` or `uv` into a selected context as a hidden prerequisite for Tuv operation.

For normal system interpreter contexts, pass `uv pip` flags needed to explicitly opt into system mutation after user confirmation, such as `--system` when required by uv for the selected target. Tuv does not support externally managed system Python mutation overrides such as `--break-system-packages`; if uv refuses because the interpreter is externally managed, surface that uv error and leave the context unchanged.

Only one installation may run at a time. If the user presses `Enter` on another row while an installation is running, Tuv must not start a concurrent uv process. Instead, mark that requested row with the displayed status `Wait`. If the user presses `Enter` repeatedly on the same waiting row, keep the single existing wait request and keep displaying `Wait`; repeated key presses must not enqueue duplicate installs. When the active installation finishes and the full package table refresh completes, Tuv may start a waiting installation if its package row and target version are still valid.

## Update All Ready Packages

`F2` updates all packages currently in `ready` state after user confirmation.

Bulk update rules:

- Before starting any install, show a permission dialog summarizing how many ready packages will be installed.
- The permission dialog must close with `Esc` or a negative answer without starting installs.
- Bulk update starts only after explicit positive confirmation.
- If the selected context is an interpreter context that has not yet been confirmed for mutation, the bulk permission dialog must also serve as the interpreter installation confirmation.
- In that case, the dialog must clearly state that the bulk update will install into the interpreter context; accepting it marks the interpreter context as confirmed for mutation.
- `F2` must not begin a bulk update while initial refresh, outdated lookup, or required target-version resolution is still in progress.
- Build the initial work list only from rows whose normalized package name is unique, whose status is `ready`, and whose target version came from successful candidate-version resolution for the effective index configuration. Each queued item records the package name and the latest target version known at the moment the bulk run starts.
- Run installs sequentially with the same asynchronous worker used for single-package installs.
- Never start more than one uv install process at a time.
- After each package install exits, refresh the full package table before choosing the next package.
- Before starting each next package, re-check the refreshed row state.
- Skip a package when it failed earlier in the current bulk run, was already processed in the current bulk run, is no longer present, or is already installed at the queued latest target version.
- If a queued package was updated to its queued latest target version earlier in the same bulk run as a dependency-side effect of another install, treat it as complete and skip its own install step.
- If a queued package is still waiting, has not failed in the current bulk run, and is not installed at its queued latest target version, run its planned update when its turn arrives even if dependency-side modifications changed its currently installed version.
- If a package installation fails during a bulk update, keep that package in `failed` status, record its failure details, and do not retry it during the same bulk update run.
- Mark the active row as `installing`; mark pending bulk rows as `wait` if they are visible.
- Keep the TUI responsive throughout the bulk update.

## Row Status Values

The fourth table column displays one of these states:

- `current`: installed version equals target version.
- `ready`: target version differs from installed version, was produced by successful version resolution for the effective index configuration, and can be installed.
- `loading`: target versions are being fetched.
- `wait`: displayed as `Wait`; installation was requested while another installation is already running.
- `installing`: install worker is running; show an animated spinner.
- `skipped`: package was skipped by a bulk update because it failed earlier in the current bulk run, was already processed, disappeared after refresh, or reached its queued latest target version before its own install step.
- `done`: install completed successfully; short-lived before refresh.
- `failed`: install failed; row is rendered bold red and remains selectable.

Recommended spinner frames:

```text
- \ | /
```

## Information Panel

`F3` opens an information panel for the focused row.

For failed rows, the panel must show:

- Package name.
- Version installed at the time of the attempted install, labeled `Version`.
- Exit code.
- Last relevant stdout and stderr lines.
- Elapsed time.

For non-failed rows, the panel may show package metadata that is directly about the focused package.

The information panel is package-focused:

- It does not need to include the full known versions list.
- It does not need to include selected context details such as environment, interpreter, or Python path.
- It does not need to include target version.
- It does not need to include uninstall-safe marker state.
- It does not need to include row status.
- The installed package version should be labeled `Version`, not `Installed`.

For every package row, the information panel must list:

- Short package description: a concise summary from installed package metadata when available, or an explicit empty value when unavailable.
- Dependency packages: installed packages required by the focused package.
- Usage packages: installed packages that depend on the focused package.

These lists should use normalized dependency metadata from the selected context. Empty descriptions and empty lists should be shown explicitly as empty rather than omitted.

## Modal Behavior

Every modal overlay or dialog must close with `Esc` or `q`.

Modal titles:

- Framed modal overlays and dialogs should render their title inside the top border near the top-left corner, for example a border shaped like `┌ Information ─────┐`.
- The title should be separated from the left corner by a small amount of horizontal border, typically one border segment or one space, so it reads as part of the frame rather than as floating text.
- The remaining top border should continue after the title so the frame still looks continuous.
- The title must not consume a separate content row inside the modal body.
- If the modal is too narrow to fit the full title, truncate the title within the top border rather than wrapping it or breaking the frame.

Modal examples:

- Context selector.
- Version selector.
- Information dialog.
- Permission and confirmation dialogs, including install-all permission.
- Error detail dialogs.

When `Esc` or `q` closes a permission or confirmation dialog, the associated action is cancelled.

When no modal overlay or dialog is active, `q` quits the main application. `F10` also quits the main application. `q` must prefer closing the active modal over quitting the app whenever a modal is present.

## Error Handling

Missing `uv`:

- Resolve uv through the provider hierarchy: selected non-Tuv venv uv, selected context reference interpreter uv, standalone system uv, then Tuv runner venv uv.
- Do not require standalone `uv` on `PATH`.
- If no provider can be resolved, repair or install `uv` in the Tuv runner venv.
- Never ask to install `uv` into the selected context venv.
- Never ask to install `uv` into the selected interpreter context.
- If Tuv runner uv repair succeeds, continue.
- If Tuv runner uv repair fails after the TUI has started, show a clear failed state row that represents the Tuv runner uv repair failure, not a fake package row from the selected context.
- The failed repair row should be visibly distinct as a Tuv operational error, include the failed runner path and repair command details through `F3`, and disable package mutation until repair succeeds on a later refresh or restart.
- If the selected target context lacks both `uv` and `pip`, do not report missing `uv` as long as a resolved uv provider can manage that context with `--python <context>`.

Broken Tuv runner venv:

- Treat the runner venv as broken when the runner Python executable is missing, cannot execute the JSON probe, cannot import required runner dependencies after installation, reports a base executable/prefix that no longer exists, or cannot be put into functional mode with working runner `pip` and runner `uv`.
- When broken, mark that script-relative Tuv runner venv incompatible and select another compatible runner venv for the selected runner Python, or create a new hash-suffixed runner venv.
- If the runner state marker records a compatibility key that does not match the selected runner Python and launcher mode, do not mutate that runner venv in place; select or create a compatible runner venv.
- After selecting or creating a compatible runner venv, ensure runner pip, ensure runner uv, reinstall requirements when needed, rewrite the runner state marker, and launch `tuv.py`.
- If this launcher-time repair fails before `tuv.py` starts, exit with a clear message that includes the runner path and the selected newest interpreter path. In-app uv provider repair failures after alternate-screen startup follow the `Missing uv` behavior above and render a real Tuv operational failure row.

Runner bootstrap failure:

- If no compatible runner venv can be ensured because runner `pip`, runner `ensurepip`, runner `uv`, or required dependency installation fails, exit before entering the TUI and print a clear diagnostic.
- If dependency installation fails because the network or configured package index is unavailable, explain that Tuv could not finish preparing its runner environment and that no selected target context was modified.
- Do not report runner bootstrap network failures as missing selected-context `uv` or `pip`.

No Python interpreter:

- The launcher exits with a clear message.
- Do not silently install Python.
- Mention that a Python interpreter must be installed before Tuv can run.

Invalid or broken virtual environment:

- Exclude it from the selector if it is obviously invalid.
- If it becomes invalid after selection, show an error state and keep the app open.

Network/index errors:

- Keep installed package data visible.
- Mark target version data as unavailable.
- Allow refresh.
- Distinguish TUI package metadata/index failures from launcher-time runner bootstrap failures; the former keep the TUI open, while the latter exit gracefully before startup because Tuv cannot run without its own dependencies.

Installation errors:

- Preserve the current table.
- Render the failed row in bold red.
- Make details available through `F3`.
- Never crash the TUI for an expected uv failure.

## Architecture

Tuv is implemented as one Python file, `tuv.py`, plus the two platform launchers and `requirements.txt`.

Suggested internal organization inside `tuv.py`:

- Models: dataclasses for contexts, package rows, install jobs, and status values.
- Discovery: newest Python interpreter discovery and virtual environment discovery.
- uv backend: per-context uv provider resolution, subprocess wrapper for Python-module or standalone uv commands, and JSON parsing.
- Versions: index API candidate target version lookup and version ordering.
- Native terminal UI: alternate-screen lifecycle, raw input handling, key decoding, redraw scheduling, widgets, key bindings, and rendering.
- Installer: background install queue and result handling.

Architecture hygiene:

- Keep implementation helpers mapped to active behavior described by this specification.
- Remove stale functions, dead state fields, old context types, and unused compatibility shims when their behavior is replaced.
- Avoid retaining duplicate helper paths that compute the same concept differently, especially for dependency metadata, uninstall-safe markers, candidate versions, and runner repair.
- Tests should cover active paths rather than stale helpers.

Subprocess rules:

- Always call subprocesses with argument lists, not shell-interpolated command strings.
- Invoke uv through the resolved provider from the hierarchy: `<provider-python> -m uv` for venv, reference interpreter, or Tuv runner providers, and `<uv-executable>` for standalone system uv.
- Capture stdout and stderr.
- Parse machine-readable JSON output whenever uv provides it.
- Keep raw command output available for error details.

Terminal rules:

- Enter and leave alternate screen explicitly.
- Use raw terminal mode only while Tuv is active.
- Restore terminal mode in `finally` blocks and signal/interrupt handlers where practical.
- Keep ANSI output centralized in a renderer helper so drawing logic stays predictable.
- Draw main table separators with Unicode box-drawing character `─`.
- Do not draw main table outer boundary characters such as `┌`, `┐`, `└`, `┘`, or far-left/far-right `│`.
- Modal overlays may still use boxed Unicode borders when a framed dialog improves clarity.
- Modal overlays should dim the background content behind them so the inactive UI is visually subdued.
- Use Unicode arrow glyphs in key legends: `↑`, `↓`, `←`, and `→`.

## State Model

Core objects:

```text
UvProvider
  type: context_venv | reference_python | standalone | tuv
  executable: absolute path or null
  python_path: absolute path or null
  priority: integer, lower is more local
  version: uv version string

PythonContext
  id: stable string
  type: tuv | venv | interpreter
  source: tuv | active | cwd | installed | scanned
  label: display label
  python_path: absolute path
  reference_python_path: absolute path or null
  root_path: absolute path or null
  version: Python version string
  resolved_uv_provider: UvProvider or null
  confirmed_for_mutation: bool

PackageRow
  name: normalized distribution name
  display_name: package display name
  description: short package description string or null
  uninstall_safe: bool
  metadata_trusted: bool, true only when metadata corresponds to current refresh
  versions_resolved: bool, true only when candidate versions came from effective index
  installed_version: version string
  target_version: version string
  candidate_versions: list of version strings
  status: current | ready | loading | wait | installing | skipped | done | failed
  updated_in_session: bool, true when installed or updated during current Tuv session
  color_hint: optional current | outdated | updated rendering hint for loading rows during refresh
  last_error: string or null
  last_error_detail: string or null
  last_install_result: InstallResult or null

InstallJob
  context_id: string
  package_name: string
  target_version: string
  started_at: timestamp

InstallResult
  package_name: string
  requested_version: string
  installed_version_at_attempt: version string
  exit_code: integer or null when process did not start
  stdout_tail: list of strings
  stderr_tail: list of strings
  elapsed_seconds: float
  failed_in_bulk_run_id: string or null
```

## Acceptance Criteria

- Running `tuv.sh` on Linux/macOS or `tuv.bat` on Windows starts an alternate-screen TUI when Python is installed and the Tuv runner environment can be ensured.
- The launcher prints an immediate `tuv:` startup line and concise bootstrap progress lines before the alternate-screen TUI starts.
- The launcher uses script-relative paths for `tuv.py`, `requirements.txt`, and the runner venv.
- New runner venvs are created directly under `TUV_HOME` with names like `tuv-venv-1a3b8e4f`; legacy `.tuv-venv` may be reused only when compatible.
- The launcher discovers the newest usable platform Python interpreter for default launches.
- The launcher does not classify an active or project virtual environment Python from `PATH` as a platform interpreter.
- When started with the literal dot argument `.` as in `tuv .`, the launcher uses current-working-directory Python as the runner Python and does not fall back to platform discovery.
- After selecting runner Python, the launcher reuses a compatible Tuv runner venv or creates a novel hash-suffixed runner venv when none is compatible.
- The launcher uses the selected runner Python for the Tuv runner venv and for Tuv-owned venv management.
- The launcher starts by ensuring the Tuv runner venv reaches functional mode with working runner `pip` and runner `uv`; neither dependency is required in the base interpreter.
- If runner `pip`, runner `ensurepip`, and runner `uv` cannot make the runner venv functional, the launcher exits gracefully with an informative bootstrap message.
- If a compatible runner environment cannot be ensured because network or index access is unavailable, Tuv exits gracefully before entering alternate-screen mode and explains that runner dependency bootstrap failed.
- If the runner Python is missing, broken, points at a removed base interpreter, or is incompatible with the selected runner Python and launcher mode, the launcher selects or creates a compatible runner venv before launching Tuv.
- Tuv resolves uv providers in this priority order for package operations: selected context venv uv, selected context reference interpreter uv, standalone system uv, Tuv runner venv uv.
- Tuv detects standalone system `uv` when available, but uses it only after more local context providers are unavailable.
- The launcher does not require standalone `uv` on `PATH`.
- If no uv provider is available, Tuv repairs or installs `uv` into the Tuv runner venv and continues when repair succeeds.
- If Tuv runner uv repair fails after the TUI has started, Tuv shows a real Tuv operational failure row, not a fake selected-context package row.
- Tuv never offers to install `uv` into a selected context venv or selected interpreter context.
- Tuv may update `pip` and `uv` in the selected context only when they are normal package rows selected by the user or included in a confirmed bulk update.
- Tuv never installs `pip` or `uv` into a selected context as a hidden prerequisite for Tuv operation.
- If standalone system `uv` or Tuv runner venv `uv` is available, Tuv can inspect and manage a target Python distribution that lacks both `uv` and `pip`.
- Tuv treats snappy startup as a design requirement: after launcher bootstrap completes, it enters the alternate-screen UI without blocking on latest-version lookup or candidate-version enumeration.
- The context selector always includes `tuv venv`.
- The context selector lists interpreter contexts first, virtual environment contexts second, and `tuv venv` last.
- A current working directory that contains a runnable Python interpreter is offered as an interpreter context even when that interpreter is not installed or discoverable by usual system methods.
- `F9` focuses and opens the context selector combo.
- Selecting a context loads packages for that context.
- Installed packages are rendered first, and latest target versions are updated asynchronously after the table is visible.
- Rows are not actionable for `Enter`, version-selector `Enter`, or `F2` bulk update until required version resolution for those rows has completed successfully.
- Active virtual environments are listed as virtual environment contexts and are not duplicated as interpreter contexts.
- The table contains package name, installed version, target version, and action/status columns.
- The top status bar starts directly with selected context data, without a leading `Context:` label or idle/status text.
- The table uses Unicode separator lines and does not render broken ASCII-style lines.
- The main table does not draw far-left, far-right, top, or bottom boundary lines.
- The bottom key legend uses Unicode arrow glyphs for arrow-key hints.
- The bottom key legend uses `↵` for Enter/install.
- The bottom key legend displays function-key actions sorted by function key number.
- The bottom key legend may omit last-action/status-message text.
- Installed and target version columns use wider sizing suitable for long PEP 440 versions.
- The table lists all packages in alphabetical order.
- `Up/Down` changes the focused package row by one row.
- `PageUp/PageDown` jumps quickly through table rows.
- `Left/Right` changes the focused row target version.
- `Left/Right` uses the complete available install-version list for the focused package, not only installed and latest.
- Candidate versions are robustly enumerated through the effective configured package index API and sorted with PEP 440 semantics.
- `F4` opens an overlay combo listing all known installable versions for the focused package.
- `Enter` in the version selector starts installation of the highlighted version only after the complete candidate-version list has loaded successfully.
- `Esc` closes the version selector.
- `q` closes modal dialogs and selectors the same way as `Esc`; when no modal is active, `q` quits the main app.
- `F10` quits the main app when no modal is active.
- Active modal dialogs and selectors dim the background content behind them.
- Modal dialog and selector titles are embedded into the top-left portion of their top border, not rendered as a separate body line.
- `Enter` installs the selected target version through the resolved uv provider.
- `Enter` on a loading, unresolved, or version-lookup-failed row does not start installation.
- System interpreter installs use normal explicit system mutation only after confirmation and do not use externally managed override flags.
- `F2` asks for permission, then updates all ready packages sequentially only after confirmation.
- Bulk update skips packages that failed earlier in the current bulk run, packages already processed in the current bulk run, and queued packages that reached their queued latest target version as a dependency-side effect.
- The fourth column shows an animated installation indicator while uv is running.
- Installations run asynchronously and do not block table navigation, context selector access, or `F3` information panels.
- If `Enter` requests another installation while one is running, the requested row displays `Wait` and no concurrent uv install starts.
- A failed installation row is rendered bold red.
- `F3` opens package-focused information for the focused row and shows failure details for failed rows.
- `F3` labels the installed package version as `Version`.
- `F3` shows a short package description plus dependency and usage packages for every row.
- `F3` omits known versions, target version, uninstall marker, row status, and context/interpreter details.
- The TUI remains responsive during installation.
- After any completed installation, the full package table refreshes so dependency updates made by uv are reflected.
- Packages installed or updated during the current Tuv session are colored white after refresh.
- During the post-install refresh, unchanged rows do not briefly flash white while their refreshed status is still loading; they keep their previous stable color until fresh current/outdated status is known.
- Current packages are colored light green unless a higher-priority row style applies.
- Package rows show `* ` before the package name when that package can be uninstalled without breaking dependency requirements.
- Package rows show `* ` only after trusted metadata matching the current displayed package table is available.
- Dependency and usage relationships are derived from multiple available metadata sources and normalized for the selected context.
- Optional-extra dependency metadata is interpreted conservatively for uninstall-safety so installed packages are not marked unused merely because the active extra cannot be proven from installed metadata.
- Failures are visible, recoverable, and do not terminate the application.
- Every modal overlay or dialog closes with `Esc` or `q`.

## Implementation Milestones

1. Create `tuv.sh`, `tuv.bat`, `tuv.py`, and `requirements.txt`.
2. Implement newest Python discovery in both launchers, including current-working-directory interpreter detection and exclusion of virtual environment interpreters from default platform discovery.
3. Implement uv provider resolution with the context venv, reference interpreter, standalone system, and Tuv runner venv hierarchy.
4. Create and repair script-relative hash-suffixed runner venvs under `TUV_HOME`, including compatible-runner selection, explicit `tuv .` cwd-runner mode, missing/broken runner Python, and incompatible-runner replacement.
5. Add native alternate-screen app shell with static header, table, and footer key hints.
6. Implement context discovery, including interpreter-first ordering, current-working-directory interpreter contexts, virtual environment deduplication, and always-last `tuv venv`.
7. Implement `F9` context selector combo behavior.
8. Implement uv-backed package listing and outdated merge.
9. Implement row navigation and `Left/Right` target version state backed by complete index-enumerated candidate versions.
10. Implement version selector overlay with `F4`, `Enter`, and `Esc`.
11. Implement install worker and row status loop.
12. Implement `F2` sequential update-all-ready flow.
13. Add uninstall-safe package marker.
14. Add bold-red failed row rendering and `F3` information panel.
15. Add tests for discovery, explicit `tuv .` cwd-runner mode, runner compatibility and hash-suffixed venv selection, uv provider hierarchy resolution, Tuv-runner pip and uv bootstrap, runner repair, uv JSON parsing, context ordering, active-venv classification, virtual environment deduplication, current-working-directory interpreter detection, robust effective-index-backed version selection, action gating before version resolution, metadata trust gating for uninstall-safe markers, bulk update sequencing, failure detail state, and install state transitions.

## References

- uv overview: https://docs.astral.sh/uv/
- uv installation: https://docs.astral.sh/uv/getting-started/installation/
- uv pip interface: https://docs.astral.sh/uv/pip/
- uv environment behavior: https://docs.astral.sh/uv/pip/environments/
- uv package inspection: https://docs.astral.sh/uv/pip/inspection/
- uv CLI reference: https://docs.astral.sh/uv/reference/cli/
- Python Simple Repository API: https://packaging.python.org/en/latest/specifications/simple-repository-api/

## Bug Counterexamples

These examples describe behavior that must be treated as bugs and covered by regression checks:

- `Esc` key does not close the version selector.
- `Esc` key does not close the information dialog.
- `Esc` key does not close the context selector.
- `q` key does not close any modal dialog or selector in the same way as `Esc`.
- Pressing `q` with no modal open does not quit the main application.
- Pressing `F10` with no modal open does not quit the main application.
- The launcher appears silent for several seconds before the alternate-screen TUI starts.
- Large information dialog content is not scrollable as expected.
- In the version selector, moving selection down scrolls the entire content upward while the selection marker stays at the top; the marker should move down until it reaches the visible selector boundary.
- A modal dialog title is rendered as a separate first body line instead of being embedded into the top-left border.
- After launcher bootstrap completes, Tuv blocks the first TUI frame on latest-version or candidate-version lookup instead of showing installed package data promptly.
- Tuv emits sounds, such as terminal beeps; this should never happen.
- Pressing `F2` starts installation of one package but then does not proceed with the rest of the packages that were previously waiting.
- Pressing `Enter`, version-selector `Enter`, or `F2` starts an install while version lookup is still loading, unresolved, failed, or based on stale index data.
- Pressing `Enter` repeatedly on a waiting row queues duplicate installs for that same row.
- A selected venv contains a working `uv`, but Tuv uses standalone system `uv` instead.
- A selected interpreter can run `python -m uv`, but Tuv uses standalone system `uv` instead.
- Tuv prompts to install `uv` into a selected project venv or interpreter context.
- Tuv runner uv repair fails after the TUI has started, and Tuv renders a fake selected-context package row instead of a clear Tuv operational failure row.
- Standalone `uv` is available, but Tuv prompts to install `uv` into the newest Python interpreter.
- The base interpreter lacks `pip` or `uv`, and Tuv fails instead of creating the runner venv and bootstrapping runner-local `pip` and `uv`.
- The selected runner venv lacks working `pip`, `uv`, and usable `ensurepip`, but Tuv starts anyway instead of reporting that no functional runner environment can be ensured.
- Runner dependency bootstrap fails because the package index or network is unavailable, and Tuv enters alternate-screen mode or reports a misleading selected-context error.
- The Tuv runner venv Python executable exists but cannot run, and the launcher still tries to use it instead of repairing the runner venv.
- A newer usable Python interpreter is available, but the Tuv runner venv remains pinned to an older interpreter after startup.
- An active virtual environment Python found on `PATH` is selected as the default platform runner Python.
- `tuv .` is launched from a directory containing a usable Python, but the launcher chooses a platform Python instead.
- `tuv .` is launched from a directory without usable Python, but the launcher silently falls back to platform Python instead of failing clearly.
- The selected runner Python changes, but Tuv mutates an incompatible existing runner venv in place instead of selecting or creating a compatible hash-suffixed runner venv.
- No compatible runner venv exists, but Tuv reuses an incompatible runner venv instead of creating a new directory such as `tuv-venv-1a3b8e4f`.
- Tuv installs `pip` or `uv` into a selected context as a hidden prerequisite for package listing or installation.
- A target Python distribution without `uv` or `pip` is rejected even though standalone system uv or Tuv runner venv uv can manage it through `--python`.
- The current working directory contains a runnable Python interpreter, but no corresponding interpreter context appears in the context selector.
- `tuv venv` appears before interpreter or project virtual environment contexts in the context selector.
- The same virtual environment appears more than once in the context selector through `VIRTUAL_ENV`, `.venv`, and scanned child discovery.
- `Left` or `Right` only toggles between installed and latest instead of traversing all index-enumerated installable versions.
- `F3` omits failure exit code, stdout/stderr tails, elapsed time, short description, or dependency/usage relationships required by the information panel.
- Dependency metadata collection fails, but all packages are marked with `* ` as though metadata proved they are uninstall-safe.
- A package required only through a standard `Requires-Dist: <name>; extra == ...` relationship is marked uninstall-safe while the declaring package and dependency are both installed.
- Version lookup silently falls back to a different public package index when the selected context has an explicit configured index that failed.
- Dead helper functions or stale state fields remain after behavior is replaced, causing future changes to target inactive code paths.

# Tuv Specification

Status: Draft v0.2

Tuv is an alternate-screen terminal UI Python package manager backed by `uv`. It gives users a fast, keyboard-driven way to inspect installed packages for a selected Python context, choose target versions, and run package installations without leaving the terminal.

## Goals

- Provide a full-screen TUI that runs in the terminal alternate screen and restores the original terminal contents on exit.
- Implement the TUI with native Python terminal control, not a third-party TUI framework.
- Use `uv` as the backend for Python environment inspection and package installation.
- Start from a small platform launcher that discovers the newest available Python interpreter, prepares the Tuv runner venv, installs requirements, and runs `tuv.py`.
- Keep all launcher references script-relative.
- Let the user select a discovered Python interpreter, a current-directory virtual environment, the active virtual environment, or the Tuv runner venv.
- Show a tabular overview of all installed packages in the selected context.
- Allow target version selection from the table and run installation for the focused package.
- Keep the UI responsive while package operations run.
- Be skilled at discovering the newest available Python interpreter on each supported platform.

## Non-Goals for v0.1

- Replacing `uv` project management, lockfile management, or `pyproject.toml` editing.
- Creating, deleting, or repairing user project virtual environments.
- Supporting package search and first-time installation of packages that are not already installed.
- Running concurrent package mutations.
- Silently modifying a system Python installation without an explicit confirmation.

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

1. Determine the launcher directory and use it as `TUV_HOME`.
2. Locate `tuv.py` and `requirements.txt` in `TUV_HOME`.
3. Discover installed Python interpreters and select the newest usable interpreter.
4. Check whether the newest interpreter can run `uv` with `<python> -m uv --version`.
5. If `uv` is missing from the newest Python distribution, ask the user whether Tuv should install `uv` into that interpreter.
6. If the user confirms, install `uv` for that interpreter and continue; if the user declines or installation fails, exit with a clear message.
7. Create or reuse the Tuv runner venv at exactly `TUV_HOME/.tuv-venv`.
8. Install `requirements.txt` into the runner venv.
9. Execute `TUV_HOME/tuv.py` with the runner venv Python.
10. Forward CLI arguments to `tuv.py`.

Recommended POSIX launcher flow:

```sh
TUV_HOME="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
NEWEST_PYTHON="<newest-discovered-python>"
"$NEWEST_PYTHON" -m uv --version || offer_uv_install
"$NEWEST_PYTHON" -m uv venv --allow-existing --python "$NEWEST_PYTHON" "$TUV_HOME/.tuv-venv"
"$NEWEST_PYTHON" -m uv pip install --python "$TUV_HOME/.tuv-venv" -r "$TUV_HOME/requirements.txt"
"$TUV_HOME/.tuv-venv/bin/python" "$TUV_HOME/tuv.py" "$@"
```

Recommended Windows launcher flow:

```bat
set "TUV_HOME=%~dp0"
set "NEWEST_PYTHON=<newest-discovered-python>"
"%NEWEST_PYTHON%" -m uv --version || call :offer_uv_install
"%NEWEST_PYTHON%" -m uv venv --allow-existing --python "%NEWEST_PYTHON%" "%TUV_HOME%\.tuv-venv"
"%NEWEST_PYTHON%" -m uv pip install --python "%TUV_HOME%\.tuv-venv" -r "%TUV_HOME%\requirements.txt"
"%TUV_HOME%\.tuv-venv\Scripts\python.exe" "%TUV_HOME%\tuv.py" %*
```

The launcher should avoid reinstalling requirements on every run. It should store a hash or timestamp marker for `requirements.txt` under `TUV_HOME/.tuv-venv/.tuv-requirements-state` and reinstall only when the file changes.

## Python and uv Discovery

Tuv must not require a bare `uv` executable on `PATH`. All uv calls should be made through a Python interpreter as:

```sh
<python> -m uv ...
```

Each Python interpreter context should either already supply `uv` or be offered an installation path when `uv` is missing.

Newest interpreter discovery:

- On Windows, discover candidates in this order:
  1. Python Launcher output from `py -0p`.
  2. PEP 514 registry locations: `HKCU\Software\Python`, `HKLM\Software\Python`, and `HKLM\Software\WOW6432Node\Python`.
  3. `PATH` executables such as `python.exe`, `python3.exe`, `python3.13.exe`, and `python3.12.exe`.
  4. Common install directories such as `%LocalAppData%\Programs\Python\Python*`, `%ProgramFiles%\Python*`, and `%ProgramFiles(x86)%\Python*`.
- On Linux and macOS, inspect common executable names on `PATH`, such as `python3`, `python3.13`, `python3.12`, and `python`.
- Probe each candidate by executing it and reading JSON from:

```sh
<python> -c "import json, sys; print(json.dumps({'version': sys.version_info[:3], 'executable': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix}))"
```

- Ignore candidates that cannot execute, cannot report a version, or are unsupported.
- Deduplicate by resolved executable path.
- Sort by semantic Python version, preferring higher version numbers.
- Use the newest usable interpreter for the Tuv runner venv.

uv bootstrap:

- First check `<python> -m uv --version`.
- If missing, ask the user whether to install `uv` for that interpreter.
- If confirmed, install with `<python> -m pip install uv`.
- If `pip` is unavailable, try `<python> -m ensurepip --upgrade` before installing `uv`.
- Never install `uv` silently.

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
- Decode common keyboard sequences for arrows, `PageUp`, `PageDown`, `Left`, `Right`, `Enter`, `F3`, `F4`, `F9`, refresh, and quit.
- Decode `F2` for updating all ready packages.
- Decode `F4` for opening a package version selector overlay.
- Decode `Esc` and `q` for closing every modal overlay or dialog.

Input handling:

- POSIX: use `termios`, `tty`, and `select` for raw, non-blocking input.
- Windows: use `msvcrt.getwch()` for keyboard input.
- Windows ANSI rendering should enable virtual terminal processing through `ctypes` when needed.
- Because function-key sequences vary between terminals, `F9` should support common sequences and may have a fallback context-selector key if a terminal cannot report `F9` reliably.

Native UI responsibilities:

- Maintain table viewport state, focused row, and scroll offset.
- Render the context selector as a lightweight combo overlay.
- Render package version choices as a lightweight combo overlay.
- Use Unicode box-drawing characters for internal separators so table lines render as continuous terminal lines.
- Do not draw far-left, far-right, top, or bottom boundary lines around the main table; preserving space is preferred.
- Use Unicode arrow characters in the bottom key legend.
- Use a Unicode enter symbol in the bottom key legend.
- Render failed rows in bold red.
- Render current packages in light green.
- Render the installation spinner in the fourth column.
- Keep install subprocesses off the input/render loop with `threading` or `asyncio`.
- Run installation jobs asynchronously so the TUI remains responsive for navigation, context viewing, status updates, and information panels during installation activity.
- Do not run concurrent installations because dependency resolution and environment mutation can clash.
- Run bulk updates sequentially and re-check package state after each install.
- Restore terminal modes, colors, cursor visibility, and alternate-screen state after normal exits, exceptions, and interrupted runs.

## Runtime Dependencies

Tuv should keep runtime dependencies small.

Recommended dependencies:

- `packaging`: package name normalization and version ordering.
- `uv`: backend package manager invoked with `python -m uv`.

No TUI framework dependency is required. The launchers should rely only on shell or batch features, OS Python discovery commands, and the selected newest Python interpreter. Python dependencies are required only after the runner environment exists.

## Python Contexts

A Python context is the environment Tuv inspects and mutates.

Context types:

- `tuv`: the Tuv runner venv at `TUV_HOME/.tuv-venv`.
- `venv`: a PEP 405 virtual environment found from the current working directory.
- `interpreter`: an installed Python interpreter discovered by Tuv.
- `active`: the currently activated virtual environment from `VIRTUAL_ENV`, if present.

Context discovery order:

1. Active virtual environment from `VIRTUAL_ENV`.
2. Tuv runner venv, labeled `tuv venv`; this context is always available.
3. Virtual environments under the current working directory.
4. Installed Python interpreters, newest first.

Virtual environment detection:

- A directory is considered a virtual environment when it contains `pyvenv.cfg`.
- The executable must exist at `bin/python` on POSIX or `Scripts/python.exe` on Windows.
- v0.1 scans the current working directory and direct child directories.
- A later version may add configurable recursive scanning depth.

Default selected context:

1. Active virtual environment, when present.
2. `.venv` in the current working directory, when present.
3. `tuv venv`.
4. Newest discovered interpreter.

Interpreter contexts may refer to system Python installations. Installing into those contexts is potentially risky, so the first mutation in an interpreter context must show a confirmation dialog.

When a selected context cannot run `uv`, Tuv should prompt the user to install `uv` into that context before loading package data or running an install.

## Main Screen

The application opens directly into the package manager view in the terminal alternate screen.

Layout:

```text
Context: [ .venv - Python 3.12.4 - C:\repo\.venv ]              Refreshing: idle
────────────────────────────────────────────────────────────────────────────────
Package                         Installed             Target                Act
* pytest                        8.3.5                 8.3.5                 curr
* requests                      2.31.0                2.32.5                ready
  rich                          13.9.4                14.0.0                ready
────────────────────────────────────────────────────────────────────────────────
↑/↓ Row | PgUp/PgDn Jump | ←/→ Version | ↵ Install | F2 All | F3 Info
```

Top menu:

- Contains a context selector.
- The selector is a combo control opened with `F9`.
- `F9` focuses the context selector and opens the context combo.
- The combo lists interpreters, `tuv venv`, the active virtual environment, and current-directory virtual environments.
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
- Failed installations are shown in bold red.

Visual priority:

1. Focused row indication.
2. Failed row: bold red.
3. Installed or updated during current session: white.
4. Current package: light green.
5. Outdated package: outdated styling.

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
- May also include:
  - `R`: refresh package list.
  - `Q`: quit.

The bottom key legend must use Unicode arrows for arrow-key hints, for example `↑/↓ Row` and `←/→ Version`.
The bottom key legend must use a Unicode enter symbol for the install key, for example `↵ Install`.
Function-key hints in the bottom legend must be sorted by function key number, for example `F2 All | F3 Info | F4 Versions | F9 Context | F10 Quit`.
The bottom key legend does not need to include last-action or status-message text; status may be shown elsewhere in the UI.

## Package Data

Installed package list:

- Use `<context-python> -m uv pip list --python <context> --format json` for a selected context.
- For virtual environments, `<context>` may be the venv root path.
- For interpreter contexts, `<context>` should be the interpreter executable path.

Outdated package list:

- Use `<context-python> -m uv pip list --python <context> --outdated --format json`.
- Merge outdated data into the installed package table.
- Default target version:
  - Latest available version for outdated packages.
  - Installed version for current packages.

Version candidates:

- `Left` chooses the next older known version.
- `Right` chooses the next newer known version.
- `Left` and `Right` must consider all available install versions for the focused package, not only the installed and latest versions.
- If the full version list has not been loaded when `Left` or `Right` is pressed, Tuv should load it before applying the version change.
- `F4` opens an overlay combo selector containing all known installable versions for the focused package.
- In the version selector, `Enter` selects the highlighted version and starts installation for that package.
- In the version selector, `Esc` closes the selector without changing or installing.
- Candidate versions should load lazily for the focused row.
- Candidate version lookup should list all available installation versions from the configured package server or index where practical.
- Candidate version lookup should respect uv index configuration where practical.
- `uv` remains the authoritative installer and resolver. If metadata lookup offers a target that `uv` cannot install, surface the uv error and keep the row unchanged.

The initial implementation may support only the installed version and latest version while the candidate-version provider is built out, but the UI and state model should already allow multiple target candidates.

Uninstall-safe marker:

- Tuv should compute a reverse dependency view of installed packages for the selected context.
- A package is marked with `* ` when no other installed package declares a dependency requirement satisfied by that package.
- The marker is informational in v0.1; it does not add uninstall behavior.

## Installation Flow

When the user presses `Enter` on a package row:

1. If the selected context is an interpreter context and has not been confirmed yet, show a confirmation dialog.
2. If the selected context cannot run `uv`, ask whether to install `uv` into that context before continuing.
3. If the target version equals the installed version, do nothing and show a short status message.
4. Mark the row as `installing`.
5. Start an asynchronous background worker that runs `uv pip install`.
6. Animate the fourth column while the process is running.
7. Capture stdout, stderr, exit code, and elapsed time.
8. After the uv process exits, refresh the entire package table for the selected context, because uv may update dependencies as part of the install.
9. On success, show the refreshed package versions and clear the completed row status.
10. On failure, mark the row as `failed`, render it bold red, and keep failure details available through `F3`.

Install command:

```sh
<context-python> -m uv pip install --python <context> "<package-name>==<target-version>"
```

For system interpreter contexts, pass the uv flags needed to explicitly opt into system mutation after user confirmation.

Only one installation may run at a time. If the user presses `Enter` on another row while an installation is running, Tuv must not start a concurrent uv process. Instead, mark that requested row with the displayed status `Wait`. When the active installation finishes and the full package table refresh completes, Tuv may start the waiting installation if its package row and target version are still valid.

## Update All Ready Packages

`F2` updates all packages currently in `ready` state after user confirmation.

Bulk update rules:

- Before starting any install, show a permission dialog summarizing how many ready packages will be installed.
- The permission dialog must close with `Esc` or a negative answer without starting installs.
- Bulk update starts only after explicit positive confirmation.
- Build the initial work list from rows whose normalized package name is unique, whose status is `ready`, and whose `updated_in_session` flag is false.
- Run installs sequentially with the same asynchronous worker used for single-package installs.
- Never start more than one uv install process at a time.
- After each package install exits, refresh the full package table before choosing the next package.
- Before starting each next package, re-check the refreshed row state.
- Skip a package when it is already current, no longer ready, already installed or updated during the current session, or already processed in this bulk update run.
- Mark the active row as `installing`; mark pending bulk rows as `wait` if they are visible.
- Keep the TUI responsive throughout the bulk update.

## Row Status Values

The fourth table column displays one of these states:

- `current`: installed version equals target version.
- `ready`: target version differs from installed version and can be installed.
- `loading`: target versions are being fetched.
- `wait`: displayed as `Wait`; installation was requested while another installation is already running.
- `installing`: install worker is running; show an animated spinner.
- `skipped`: package was skipped by a bulk update because it was already current, already processed, or already installed or updated during the session.
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
- Installed version at the time of the attempted install.
- Target version.
- Exit code.
- Last relevant stdout and stderr lines.
- Elapsed time.

For non-failed rows, the panel may show package metadata that is directly about the focused package.

The information panel is package-focused:

- It does not need to include the full known versions list.
- It does not need to include selected context details such as environment, interpreter, or Python path.

For every package row, the information panel must list:

- Dependency packages: installed packages required by the focused package.
- Usage packages: installed packages that depend on the focused package.

These lists should use normalized dependency metadata from the selected context. Empty lists should be shown explicitly as empty rather than omitted.

## Modal Behavior

Every modal overlay or dialog must close with `Esc` or `q`.

Modal examples:

- Context selector.
- Version selector.
- Information dialog.
- Permission and confirmation dialogs, including install-all permission.
- Error detail dialogs.

When `Esc` or `q` closes a permission or confirmation dialog, the associated action is cancelled.

## Error Handling

Missing `uv`:

- Do not require `uv` on `PATH`.
- Detect uv with `<python> -m uv --version`.
- If uv is missing from the newest Python distribution, ask the user whether to install it.
- If confirmed, install uv and continue.
- If declined or installation fails, exit with a clear message.

No Python interpreter:

- The launcher exits with a clear message.
- Do not silently install Python in v0.1.
- Mention that a Python interpreter must be installed before Tuv can run.

Invalid or broken virtual environment:

- Exclude it from the selector if it is obviously invalid.
- If it becomes invalid after selection, show an error state and keep the app open.

Network/index errors:

- Keep installed package data visible.
- Mark target version data as unavailable.
- Allow refresh.

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
- uv backend: subprocess wrapper for `python -m uv` commands and JSON parsing.
- Versions: candidate target version lookup and version ordering.
- Native terminal UI: alternate-screen lifecycle, raw input handling, key decoding, redraw scheduling, widgets, key bindings, and rendering.
- Installer: background install queue and result handling.

Subprocess rules:

- Always call subprocesses with argument lists, not shell-interpolated command strings.
- Invoke uv as `<python> -m uv`, not as a bare `uv` command.
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
- Use Unicode arrow glyphs in key legends: `↑`, `↓`, `←`, and `→`.

## State Model

Core objects:

```text
PythonContext
  id: stable string
  type: tuv | active | venv | interpreter
  label: display label
  python_path: absolute path
  root_path: absolute path or null
  version: Python version string
  uv_available: bool
  confirmed_for_mutation: bool

PackageRow
  name: normalized distribution name
  display_name: package display name
  uninstall_safe: bool
  installed_version: version string
  target_version: version string
  candidate_versions: list of version strings
  status: current | ready | loading | wait | installing | skipped | done | failed
  updated_in_session: bool, true when installed or updated during current Tuv session
  last_error: string or null
  last_error_detail: string or null

InstallJob
  context_id: string
  package_name: string
  target_version: string
  started_at: timestamp
```

## Acceptance Criteria

- Running `tuv.sh` on Linux/macOS or `tuv.bat` on Windows starts an alternate-screen TUI when Python is installed.
- The launcher uses script-relative paths for `tuv.py`, `requirements.txt`, and the runner venv.
- The runner venv is created at `TUV_HOME/.tuv-venv`, where `TUV_HOME` is the exact folder containing `tuv.py`.
- The launcher discovers the newest usable Python interpreter.
- The launcher does not require bare `uv` on `PATH`.
- If `uv` is missing from the newest Python distribution, the user is offered installation and Tuv continues after confirmation.
- The context selector always includes `tuv venv`.
- `F9` focuses and opens the context selector combo.
- Selecting a context loads packages for that context.
- The table contains package name, installed version, target version, and action/status columns.
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
- `Left/Right` uses all available install versions for the focused package.
- `F4` opens an overlay combo listing all known installable versions for the focused package.
- `Enter` in the version selector starts installation of the highlighted version.
- `Esc` closes the version selector.
- `q` closes modal dialogs and selectors the same way as `Esc`.
- `Enter` installs the selected target version through `python -m uv`.
- `F2` asks for permission, then updates all ready packages sequentially only after confirmation.
- Bulk update skips packages already installed or updated during the current session and does not install any package twice.
- The fourth column shows an animated installation indicator while uv is running.
- Installations run asynchronously and do not block table navigation, context selector access, or `F3` information panels.
- If `Enter` requests another installation while one is running, the requested row displays `Wait` and no concurrent uv install starts.
- A failed installation row is rendered bold red.
- `F3` opens information for the focused row and shows failure details for failed rows.
- The TUI remains responsive during installation.
- After any completed installation, the full package table refreshes so dependency updates made by uv are reflected.
- Packages installed or updated during the current Tuv session are colored white after refresh.
- Current packages are colored light green unless a higher-priority row style applies.
- Package rows show `* ` before the package name when that package can be uninstalled without breaking dependency requirements.
- Failures are visible, recoverable, and do not terminate the application.
- Every modal overlay or dialog closes with `Esc` or `q`.

## Implementation Milestones

1. Create `tuv.sh`, `tuv.bat`, `tuv.py`, and `requirements.txt`.
2. Implement newest Python discovery in both launchers.
3. Implement uv detection and confirmed uv bootstrap.
4. Create the script-relative runner venv at `TUV_HOME/.tuv-venv`.
5. Add native alternate-screen app shell with static header, table, and footer key hints.
6. Implement context discovery, including always-present `tuv venv`.
7. Implement `F9` context selector combo behavior.
8. Implement uv-backed package listing and outdated merge.
9. Implement row navigation and `Left/Right` target version state.
10. Implement version selector overlay with `F4`, `Enter`, and `Esc`.
11. Implement install worker and row status loop.
12. Implement `F2` sequential update-all-ready flow.
13. Add uninstall-safe package marker.
14. Add bold-red failed row rendering and `F3` information panel.
15. Add tests for discovery, uv JSON parsing, version selection, bulk update sequencing, and install state transitions.

## References

- uv overview: https://docs.astral.sh/uv/
- uv installation: https://docs.astral.sh/uv/getting-started/installation/
- uv pip interface: https://docs.astral.sh/uv/pip/
- uv environment behavior: https://docs.astral.sh/uv/pip/environments/
- uv package inspection: https://docs.astral.sh/uv/pip/inspection/
- uv CLI reference: https://docs.astral.sh/uv/reference/cli/

## Bug Counterexamples

These examples describe behavior that must be treated as bugs and covered by regression checks:

- `Esc` key does not close the version selector.
- `Esc` key does not close the information dialog.
- `Esc` key does not close the context selector.
- `q` key does not close any modal dialog or selector in the same way as `Esc`.
- Large information dialog content is not scrollable as expected.
- In the version selector, moving selection down scrolls the entire content upward while the selection marker stays at the top; the marker should move down until it reaches the visible selector boundary.

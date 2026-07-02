# Tuv

Tuv is a small alternate-screen terminal UI for managing installed Python packages with `uv`. It discovers Python interpreters and local virtual environments, shows packages in a compact table, lets you choose target versions, and runs installs without leaving the terminal.

## Demo

Main package table:

![Tuv main package table](tuv_demo_1.png)

Package information dialog:

![Tuv package information dialog](tuv_demo_3.png)

## Highlights

- Native terminal UI in one Python file, with no TUI framework dependency.
- `uv` backend invoked as `python -m uv`; no bare `uv` executable is required on `PATH`.
- Context selector for the Tuv runner venv, active venvs, local venvs, and discovered interpreters, with venv creation and rescan built in.
- Alphabetical package table with installed version, target version, and status, plus a live status line for progress and errors.
- Async installs and uninstalls with a responsive UI, a wait queue, and cancellable sequential update-all with a summary report.
- Incremental filtering, multi-select, per-context pins that exclude packages from update-all, and new-package installs.
- Package info panel with description, dependency, and usage-package lists.
- Yanked releases are marked in the version selector and require an extra confirmation.

## Run

Windows:

```bat
tuv.bat
```

Linux and macOS:

```sh
./tuv.sh
```

Run Tuv from a project directory to make its local Python and virtual environments easy to pick in the context selector:

```bat
cd C:\projects\my-app
tuv.bat
```

```sh
cd ~/projects/my-app
./tuv.sh
```

Use the dot argument when you explicitly want the current directory's Python to be used as Tuv's runner Python:

```bat
cd C:\tools\python-3.13
tuv.bat .
```

```sh
cd ~/tools/python-3.13
./tuv.sh .
```

The launcher discovers a usable Python, creates or reuses a script-relative runner environment, installs `requirements.txt`, ensures runner-local `uv`, and starts `tuv.py`.

## Keys

| Key | Action |
| --- | --- |
| Up / Down | Move package selection |
| PageUp / PageDown | Jump through rows |
| Left / Right | Select older or newer target version (fetches the full version list on first use) |
| Enter | Install selected target version (queues if an install is running) |
| Space | Toggle package selection for a selective update-all |
| / | Filter the package table incrementally (Enter keeps the filter, Esc clears it) |
| i | Install a new package by name |
| Delete | Uninstall the focused package after a safety preview |
| p | Pin or unpin the focused package (pinned packages are excluded from update-all) |
| F2 | Update all ready packages (or the selected/filtered subset) after a preview confirmation |
| F3 | Show package information |
| F4 | Open version selector |
| F5 | Rescan Python contexts without restarting |
| F9 | Open context selector (`n` inside creates a venv in the current directory) |
| Esc | Close dialogs; on the main screen: clear selection, then filter, then cancel a running operation |
| F10 / q | Quit from the main screen (asks before abandoning a running install) |

Row markers: `*` before a name means no other installed package requires it (safe to uninstall); `+` marks packages selected with Space. Pinned packages show `pinned` in the Action column.

## Project Files

- `tuv.py`: application implementation.
- `tuv.bat`: Windows launcher.
- `tuv.sh`: Linux/macOS launcher.
- `requirements.txt`: runner dependencies.
- `spec.md`: detailed behavior specification.

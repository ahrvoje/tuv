#!/usr/bin/env python3
"""Tuv: a native alternate-screen terminal UI for uv-managed packages."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

try:
    from packaging.utils import canonicalize_name
    from packaging.version import InvalidVersion, Version
except Exception:  # pragma: no cover - runner requirements should provide packaging.
    canonicalize_name = lambda value: re.sub(r"[-_.]+", "-", value).lower()  # type: ignore
    Version = None  # type: ignore
    InvalidVersion = Exception  # type: ignore


IS_WINDOWS = os.name == "nt"
TUV_HOME = Path(os.environ.get("TUV_HOME", Path(__file__).resolve().parent)).resolve()
RUNNER_VENV = TUV_HOME / ".tuv-venv"

ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
CLEAR = "\x1b[2J"
HOME = "\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
RESET = "\x1b[0m"
BOLD_RED = "\x1b[1;31m"
WHITE = "\x1b[97m"
LIGHT_GREEN = "\x1b[92m"
YELLOW = "\x1b[33m"
REVERSE = "\x1b[7m"
DIM = "\x1b[2m"
MODAL_BACKDROP = "\x1b[2;90m"
BOLD = "\x1b[1m"
SPINNER = "-\\|/"


@dataclass
class PythonInfo:
    executable: Path
    version: tuple[int, int, int]
    prefix: str = ""
    base_prefix: str = ""

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)


@dataclass
class PythonContext:
    id: str
    type: str
    label: str
    python_path: Path
    root_path: Path | None
    version: str
    uv_available: bool
    confirmed_for_mutation: bool = False

    @property
    def uv_target(self) -> str:
        if self.type in {"tuv", "active", "venv"} and self.root_path is not None:
            return str(self.root_path)
        return str(self.python_path)

    @property
    def is_virtual(self) -> bool:
        return self.type in {"tuv", "active", "venv"}


@dataclass
class PackageRow:
    name: str
    display_name: str
    uninstall_safe: bool
    installed_version: str
    target_version: str
    candidate_versions: list[str]
    status: str
    versions_loaded: bool = False
    dependency_packages: list[str] = field(default_factory=list)
    usage_packages: list[str] = field(default_factory=list)
    updated_in_session: bool = False
    last_error: str | None = None
    last_error_detail: str | None = None

    @property
    def is_outdated(self) -> bool:
        return self.target_version != self.installed_version and self.status != "failed"


@dataclass
class InstallResult:
    context_id: str
    package_name: str
    target_version: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed: float
    before_versions: dict[str, str]

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class Prompt:
    title: str
    message: str
    on_yes: Callable[[], None]
    on_no: Callable[[], None] | None = None


def version_key(value: str) -> tuple[int, object]:
    if Version is not None:
        try:
            return (0, Version(value))
        except InvalidVersion:
            pass
    parts: list[object] = []
    for chunk in re.split(r"([0-9]+)", value):
        if chunk.isdigit():
            parts.append(int(chunk))
        elif chunk:
            parts.append(chunk.lower())
    return (1, tuple(parts))


def stable_context_id(context_type: str, path: Path) -> str:
    return f"{context_type}:{str(path.resolve()).lower() if IS_WINDOWS else str(path.resolve())}"


def venv_python(root: Path) -> Path:
    if IS_WINDOWS:
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def is_venv(root: Path) -> bool:
    return (root / "pyvenv.cfg").is_file() and venv_python(root).is_file()


def run_command(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        shell=False,
    )


def probe_python(executable: str | Path, timeout: float = 3.0) -> PythonInfo | None:
    path = Path(str(executable).strip().strip('"'))
    if not path:
        return None
    if not path.is_file():
        resolved = shutil.which(str(path))
        if not resolved:
            return None
        path = Path(resolved)
    code = (
        "import json, sys; "
        "print(json.dumps({'version': sys.version_info[:3], "
        "'executable': sys.executable, 'prefix': sys.prefix, "
        "'base_prefix': sys.base_prefix}))"
    )
    try:
        proc = run_command([str(path), "-c", code], timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        version = tuple(int(part) for part in data["version"])
        if len(version) != 3:
            return None
        return PythonInfo(
            executable=Path(data["executable"]).resolve(),
            version=version,  # type: ignore[arg-type]
            prefix=str(data.get("prefix", "")),
            base_prefix=str(data.get("base_prefix", "")),
        )
    except Exception:
        return None


def parse_py_launcher_output(output: str) -> list[Path]:
    paths: list[Path] = []
    for line in output.splitlines():
        match = re.search(r"([A-Za-z]:\\.*?python(?:w)?\.exe)", line, re.IGNORECASE)
        if match:
            paths.append(Path(match.group(1)))
    return paths


def registry_python_candidates() -> list[Path]:
    if not IS_WINDOWS:
        return []
    try:
        import winreg
    except Exception:
        return []

    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Python"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Python"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Python"),
    ]
    found: list[Path] = []

    def walk(root_key: object, subkey: str, depth: int = 0) -> None:
        if depth > 4:
            return
        try:
            with winreg.OpenKey(root_key, subkey) as key:
                try:
                    executable, _ = winreg.QueryValueEx(key, "ExecutablePath")
                    found.append(Path(executable))
                except OSError:
                    pass
                try:
                    install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                    found.append(Path(install_path) / "python.exe")
                except OSError:
                    pass
                try:
                    with winreg.OpenKey(key, "InstallPath") as install_key:
                        try:
                            executable, _ = winreg.QueryValueEx(install_key, "ExecutablePath")
                            found.append(Path(executable))
                        except OSError:
                            pass
                        try:
                            default_path, _ = winreg.QueryValueEx(install_key, "")
                            found.append(Path(default_path) / "python.exe")
                        except OSError:
                            pass
                except OSError:
                    pass
                index = 0
                while True:
                    try:
                        child = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    walk(root_key, f"{subkey}\\{child}", depth + 1)
                    index += 1
        except OSError:
            return

    for root_key, subkey in roots:
        walk(root_key, subkey)
    return found


def windows_python_candidates() -> list[Path]:
    candidates: list[Path] = []
    try:
        proc = run_command(["py", "-0p"], timeout=3)
        candidates.extend(parse_py_launcher_output(proc.stdout + "\n" + proc.stderr))
    except Exception:
        pass

    candidates.extend(registry_python_candidates())

    names = ["python.exe", "python3.exe"] + [f"python3.{minor}.exe" for minor in range(15, 6, -1)]
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            candidates.append(Path(resolved))

    for env_name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if not base:
            continue
        root = Path(base)
        if env_name == "LOCALAPPDATA":
            candidates.extend((root / "Programs" / "Python").glob("Python*/python.exe"))
        candidates.extend(root.glob("Python*/python.exe"))
    return candidates


def posix_python_candidates() -> list[Path]:
    names = [f"python3.{minor}" for minor in range(15, 6, -1)] + ["python3", "python"]
    paths: list[Path] = []
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            paths.append(Path(resolved))
    for root in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin"):
        root_path = Path(root)
        if root_path.is_dir():
            for candidate in root_path.glob("python3*"):
                if candidate.is_file():
                    paths.append(candidate)
    return paths


def discover_python_infos() -> list[PythonInfo]:
    raw_candidates = windows_python_candidates() if IS_WINDOWS else posix_python_candidates()
    seen: set[str] = set()
    infos: list[PythonInfo] = []
    for candidate in raw_candidates:
        info = probe_python(candidate)
        if info is None:
            continue
        key = str(info.executable).lower() if IS_WINDOWS else str(info.executable)
        if key in seen:
            continue
        seen.add(key)
        infos.append(info)
    infos.sort(key=lambda item: item.version, reverse=True)
    return infos


def has_uv(python_path: Path, timeout: float = 5.0) -> bool:
    try:
        proc = run_command([str(python_path), "-m", "uv", "--version"], timeout=timeout)
        return proc.returncode == 0
    except Exception:
        return False


def install_uv_command(python_path: Path) -> tuple[list[str], list[str]]:
    ensurepip = [str(python_path), "-m", "ensurepip", "--upgrade"]
    install = [str(python_path), "-m", "pip", "install", "uv"]
    return ensurepip, install


def context_from_venv(context_type: str, root: Path, label_prefix: str) -> PythonContext | None:
    if not is_venv(root):
        return None
    info = probe_python(venv_python(root))
    if info is None:
        return None
    label = f"{label_prefix} - Python {info.version_text} - {root}"
    return PythonContext(
        id=stable_context_id(context_type, root),
        type=context_type,
        label=label,
        python_path=info.executable,
        root_path=root.resolve(),
        version=info.version_text,
        uv_available=has_uv(info.executable),
    )


def discover_contexts() -> list[PythonContext]:
    contexts: list[PythonContext] = []
    seen: set[str] = set()

    def add(context: PythonContext | None) -> None:
        if context is None:
            return
        if context.id in seen:
            return
        seen.add(context.id)
        contexts.append(context)

    active = os.environ.get("VIRTUAL_ENV")
    if active:
        add(context_from_venv("active", Path(active), "active venv"))

    add(context_from_venv("tuv", RUNNER_VENV, "tuv venv"))

    cwd = Path.cwd()
    if is_venv(cwd):
        add(context_from_venv("venv", cwd, cwd.name or str(cwd)))
    for child in sorted((item for item in cwd.iterdir() if item.is_dir()), key=lambda p: p.name.lower()):
        add(context_from_venv("venv", child, child.name))

    for info in discover_python_infos():
        label = f"interpreter - Python {info.version_text} - {info.executable}"
        add(
            PythonContext(
                id=stable_context_id("interpreter", info.executable),
                type="interpreter",
                label=label,
                python_path=info.executable,
                root_path=None,
                version=info.version_text,
                uv_available=has_uv(info.executable),
            )
        )

    return contexts


def run_uv_json(context: PythonContext, args: list[str], timeout: float | None = 90.0) -> tuple[object, str]:
    cmd = [str(context.python_path), "-m", "uv", *args]
    proc = run_command(cmd, timeout=timeout)
    if proc.returncode != 0:
        detail = command_detail(cmd, proc.returncode, proc.stdout, proc.stderr, 0)
        raise RuntimeError(detail)
    try:
        return json.loads(proc.stdout or "[]"), proc.stderr
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse uv JSON output: {exc}\n\n{proc.stdout}") from exc


def dependency_map(context: PythonContext) -> dict[str, set[str]]:
    code = r"""
import importlib.metadata as metadata
import json
import re

def norm(value):
    return re.sub(r"[-_.]+", "-", value).lower()

def req_name(value):
    if not value:
        return None
    head = re.split(r"[<>=!~;\[\s(]", value, 1)[0].strip()
    return norm(head) if head else None

result = {}
for dist in metadata.distributions():
    name = dist.metadata.get("Name")
    if not name:
        continue
    deps = []
    for req in dist.requires or []:
        dep = req_name(req)
        if dep:
            deps.append(dep)
    result[norm(name)] = sorted(set(deps))
print(json.dumps(result))
"""
    try:
        proc = run_command([str(context.python_path), "-c", code], timeout=30)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        raw = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, set[str]] = {}
    for package_name, deps in raw.items():
        if isinstance(package_name, str) and isinstance(deps, list):
            result[canonicalize_name(package_name)] = {
                canonicalize_name(str(dep)) for dep in deps if isinstance(dep, str)
            }
    return result


def uninstall_safe_names(context: PythonContext) -> set[str]:
    deps_by_package = dependency_map(context)
    required: set[str] = set()
    for deps in deps_by_package.values():
        required.update(deps)
    return set(deps_by_package) - required


def fetch_available_versions(package_name: str, timeout: float = 12.0) -> list[str]:
    quoted = urllib.parse.quote(canonicalize_name(package_name), safe="")
    url = f"https://pypi.org/pypi/{quoted}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "tuv/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    releases = data.get("releases", {})
    if not isinstance(releases, dict):
        return []
    versions = [
        str(version)
        for version, files in releases.items()
        if isinstance(version, str) and (not isinstance(files, list) or files)
    ]
    return sorted(set(versions), key=version_key)


def build_package_rows(context: PythonContext, updated_names: set[str]) -> tuple[list[PackageRow], str | None]:
    installed_data, _ = run_uv_json(
        context,
        ["pip", "list", "--python", context.uv_target, "--format", "json"],
    )
    if not isinstance(installed_data, list):
        raise RuntimeError("uv returned unexpected installed package data")

    rows: list[PackageRow] = []
    for item in installed_data:
        if not isinstance(item, dict) or "name" not in item or "version" not in item:
            continue
        display_name = str(item["name"])
        normalized = canonicalize_name(display_name)
        installed = str(item["version"])
        rows.append(
            PackageRow(
                name=normalized,
                display_name=display_name,
                uninstall_safe=False,
                installed_version=installed,
                target_version=installed,
                candidate_versions=[installed],
                status="current",
                updated_in_session=normalized in updated_names,
            )
        )
    rows.sort(key=lambda row: row.name)
    return rows, None


def latest_from_outdated_item(item: dict[str, object], installed: str | None = None) -> str | None:
    latest = item.get("latest_version") or item.get("latest") or item.get("latest-version")
    if latest is None:
        return installed
    return str(latest)


def load_outdated_targets(context: PythonContext) -> tuple[dict[str, str], str | None]:
    try:
        outdated_data, _ = run_uv_json(
            context,
            ["pip", "list", "--python", context.uv_target, "--outdated", "--format", "json"],
        )
    except Exception as exc:
        return {}, f"Outdated data unavailable: {last_lines(str(exc), 2)}"
    if not isinstance(outdated_data, list):
        return {}, "Outdated data unavailable: uv returned unexpected data"
    targets: dict[str, str] = {}
    for item in outdated_data:
        if not isinstance(item, dict) or "name" not in item:
            continue
        latest = latest_from_outdated_item(item)
        if latest:
            targets[canonicalize_name(str(item["name"]))] = latest
    return targets, None


def load_dependency_info(
    context: PythonContext,
    display_by_name: dict[str, str],
) -> tuple[set[str], dict[str, list[str]], dict[str, list[str]]]:
    installed_names = set(display_by_name)
    deps_by_package = dependency_map(context)
    installed_deps_by_package = {
        package_name: {dep for dep in deps if dep in installed_names}
        for package_name, deps in deps_by_package.items()
        if package_name in installed_names
    }
    required_names: set[str] = set()
    usage_by_package: dict[str, set[str]] = {name: set() for name in installed_names}
    for package_name, deps in installed_deps_by_package.items():
        required_names.update(deps)
        for dep in deps:
            usage_by_package.setdefault(dep, set()).add(package_name)
    safe_names = installed_names - required_names
    dependency_packages = {
        name: [display_by_name.get(dep, dep) for dep in sorted(deps)]
        for name, deps in installed_deps_by_package.items()
    }
    usage_packages = {
        name: [display_by_name.get(user, user) for user in sorted(users)]
        for name, users in usage_by_package.items()
    }
    return safe_names, dependency_packages, usage_packages


def last_lines(text: str, count: int = 8) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:]) if lines else ""


def command_detail(
    command: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
    elapsed: float,
    include_command: bool = True,
) -> str:
    lines = []
    if include_command:
        lines.append(f"Command: {format_command(command)}")
    lines.extend(
        [
            f"Exit code: {returncode}",
            f"Elapsed: {elapsed:.1f}s",
            "",
            "stdout:",
            last_lines(stdout, 12) or "(empty)",
            "",
            "stderr:",
            last_lines(stderr, 12) or "(empty)",
        ]
    )
    return "\n".join(lines)


def format_command(command: Iterable[str]) -> str:
    return " ".join(quote_arg(part) for part in command)


def quote_arg(value: str) -> str:
    if not value:
        return '""'
    if re.search(r"\s", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


class Terminal:
    def __init__(self) -> None:
        self.fd: int | None = None
        self.old_termios: object | None = None

    def __enter__(self) -> "Terminal":
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        if IS_WINDOWS:
            self._enable_windows_vt()
        else:
            import termios
            import tty

            self.fd = sys.stdin.fileno()
            self.old_termios = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        self.write(ALT_ON + HIDE_CURSOR + CLEAR + HOME)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.write(RESET + SHOW_CURSOR + CLEAR + HOME + ALT_OFF)
        if not IS_WINDOWS and self.fd is not None and self.old_termios is not None:
            import termios

            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_termios)

    def _enable_windows_vt(self) -> None:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)

    def write(self, text: str) -> None:
        text = text.replace("\a", "")
        sys.stdout.write(text)
        sys.stdout.flush()

    def size(self) -> tuple[int, int]:
        size = os.get_terminal_size()
        return size.columns, size.lines

    def read_key(self, timeout: float) -> str | None:
        if IS_WINDOWS:
            return self._read_windows_key(timeout)
        return self._read_posix_key(timeout)

    def _read_posix_key(self, timeout: float) -> str | None:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        data = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
        if data == "\x1b":
            sequence = data
            end = time.time() + 0.03
            while time.time() < end:
                ready, _, _ = select.select([sys.stdin], [], [], 0)
                if not ready:
                    time.sleep(0.002)
                    continue
                sequence += os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
            return normalize_key(sequence)
        return normalize_key(data)

    def _read_windows_key(self, timeout: float) -> str | None:
        import msvcrt

        end = time.time() + timeout
        while time.time() < end:
            if not msvcrt.kbhit():
                time.sleep(0.01)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                code = msvcrt.getwch()
                return WINDOWS_SPECIAL_KEYS.get(code)
            return normalize_key(ch)
        return None


WINDOWS_SPECIAL_KEYS = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
    "I": "pageup",
    "Q": "pagedown",
    "G": "home",
    "O": "end",
    "<": "f2",
    "=": "f3",
    ">": "f4",
    "C": "f9",
    "D": "f10",
}

ESCAPE_KEYS = {
    "\x1b[A": "up",
    "\x1b[B": "down",
    "\x1b[C": "right",
    "\x1b[D": "left",
    "\x1b[5~": "pageup",
    "\x1b[6~": "pagedown",
    "\x1b[H": "home",
    "\x1b[F": "end",
    "\x1b[1~": "home",
    "\x1b[4~": "end",
    "\x1b[12~": "f2",
    "\x1bOQ": "f2",
    "\x1b[13~": "f3",
    "\x1bOR": "f3",
    "\x1b[14~": "f4",
    "\x1bOS": "f4",
    "\x1b[20~": "f9",
    "\x1b[21~": "f10",
    "\x1b[24~": "f12",
}


def normalize_key(data: str) -> str | None:
    if data in ESCAPE_KEYS:
        return ESCAPE_KEYS[data]
    if data in ("\r", "\n"):
        return "enter"
    if data == "\x1b":
        return "esc"
    if data in ("\x03", "\x04"):
        return "quit"
    if len(data) == 1:
        lowered = data.lower()
        if lowered in {"q", "r", "y", "n", "c"}:
            return lowered
    return None


class TuvApp:
    def __init__(self) -> None:
        self.terminal = Terminal()
        self.contexts: list[PythonContext] = []
        self.context_index = 0
        self.context_overlay = False
        self.context_overlay_index = 0
        self.version_overlay = False
        self.version_overlay_row: str | None = None
        self.version_overlay_index = 0
        self.version_overlay_scroll = 0
        self.version_options: list[str] = []
        self.version_loading = False
        self.version_error: str | None = None
        self.pending_version_direction: int | None = None
        self.rows: list[PackageRow] = []
        self.focus_index = 0
        self.scroll = 0
        self.message = "Starting..."
        self.discovering_contexts = False
        self.discovery_error: str | None = None
        self.refreshing = False
        self.refresh_context_id: str | None = None
        self.refresh_generation = 0
        self.outdated_loading = False
        self.dependency_loading = False
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.updated_by_context: dict[str, set[str]] = {}
        self.installing = False
        self.active_install_context_id: str | None = None
        self.wait_request: tuple[str, str, str] | None = None
        self.bulk_active = False
        self.bulk_queue: list[tuple[str, str]] = []
        self.bulk_processed: set[str] = set()
        self.bulk_failed_results: dict[str, InstallResult] = {}
        self.prompt: Prompt | None = None
        self.info_open = False
        self.info_scroll = 0
        self.spinner_index = 0
        self.should_quit = False
        self.last_render = ""
        self._install_signal_handlers()

    @property
    def context(self) -> PythonContext | None:
        if not self.contexts:
            return None
        self.context_index = max(0, min(self.context_index, len(self.contexts) - 1))
        return self.contexts[self.context_index]

    def _install_signal_handlers(self) -> None:
        try:
            signal.signal(signal.SIGINT, lambda _sig, _frame: self.request_quit())
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, lambda _sig, _frame: self.request_quit())
        except Exception:
            pass

    def run(self) -> int:
        with self.terminal:
            self.start_context_discovery()
            while not self.should_quit:
                self.process_events()
                self.ensure_scroll_visible()
                self.spinner_index = (self.spinner_index + 1) % len(SPINNER)
                screen = self.render()
                if screen != self.last_render:
                    self.terminal.write(HOME + screen)
                    self.last_render = screen
                key = self.terminal.read_key(0.08)
                if key:
                    self.handle_key(key)
            return 0

    def start_context_discovery(self) -> None:
        if self.discovering_contexts:
            return
        self.discovering_contexts = True
        self.discovery_error = None
        self.message = "Discovering Python contexts"

        def worker() -> None:
            try:
                contexts = discover_contexts()
                self.event_queue.put(("contexts_done", contexts))
            except Exception as exc:
                self.event_queue.put(("contexts_failed", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def on_contexts_done(self, contexts: list[PythonContext]) -> None:
        self.discovering_contexts = False
        self.contexts = contexts
        if not self.contexts:
            self.discovery_error = "Tuv could not find any usable Python context."
            self.message = self.discovery_error
            return
        self.context_index = self.default_context_index()
        self.context_overlay_index = self.context_index
        self.load_current_context()

    def on_contexts_failed(self, error: str) -> None:
        self.discovering_contexts = False
        self.contexts = []
        self.discovery_error = f"Context discovery failed: {last_lines(error, 2)}"
        self.message = self.discovery_error

    def request_quit(self) -> None:
        self.should_quit = True

    def default_context_index(self) -> int:
        for preferred in ("active", "venv", "tuv", "interpreter"):
            for index, context in enumerate(self.contexts):
                if preferred == "venv" and context.root_path and context.root_path.name == ".venv":
                    return index
                if context.type == preferred and preferred != "venv":
                    return index
        return 0

    def load_current_context(self) -> None:
        context = self.context
        if context is None:
            return
        self.rows = []
        self.focus_index = 0
        self.scroll = 0
        self.version_overlay = False
        self.version_overlay_row = None
        self.version_overlay_scroll = 0
        self.version_options = []
        self.version_error = None
        self.pending_version_direction = None
        self.bulk_active = False
        self.bulk_queue = []
        self.bulk_processed = set()
        self.bulk_failed_results = {}
        self.outdated_loading = False
        self.dependency_loading = False
        self.info_open = False
        self.info_scroll = 0
        self.message = f"Loading {context.label}"
        self.ensure_uv(context, lambda: self.start_refresh(context, "Loading packages"))

    def ensure_uv(self, context: PythonContext, on_ready: Callable[[], None]) -> None:
        if context.uv_available:
            on_ready()
            return

        def yes() -> None:
            self.prompt = None
            self.bootstrap_uv(context, on_ready)

        def no() -> None:
            self.prompt = None
            self.message = f"uv is required for {context.label}"

        self.prompt = Prompt(
            title="Install uv?",
            message=f"uv is missing from {context.python_path}. Install uv into this Python? y/N",
            on_yes=yes,
            on_no=no,
        )

    def bootstrap_uv(self, context: PythonContext, on_ready: Callable[[], None]) -> None:
        self.message = f"Installing uv into {context.python_path}"

        def worker() -> None:
            ensurepip, install = install_uv_command(context.python_path)
            try:
                pip_check = run_command([str(context.python_path), "-m", "pip", "--version"], timeout=15)
                if pip_check.returncode != 0:
                    run_command(ensurepip, timeout=120)
                start = time.time()
                proc = run_command(install, timeout=300)
                elapsed = time.time() - start
                detail = command_detail(install, proc.returncode, proc.stdout, proc.stderr, elapsed)
                self.event_queue.put(("uv_bootstrap_done", (context.id, proc.returncode, detail, on_ready)))
            except Exception as exc:
                self.event_queue.put(("uv_bootstrap_done", (context.id, 1, str(exc), on_ready)))

        threading.Thread(target=worker, daemon=True).start()

    def start_refresh(
        self,
        context: PythonContext,
        message: str,
        install_result: InstallResult | None = None,
    ) -> None:
        self.refreshing = True
        self.refresh_context_id = context.id
        self.refresh_generation += 1
        generation = self.refresh_generation
        self.outdated_loading = False
        self.dependency_loading = False
        self.message = message
        updated_names = set(self.updated_by_context.get(context.id, set()))

        def worker() -> None:
            try:
                rows, warning = build_package_rows(context, updated_names)
                payload = (context.id, generation, rows, warning, install_result)
                self.event_queue.put(("refresh_done", payload))
            except Exception as exc:
                self.event_queue.put(("refresh_failed", (context.id, generation, str(exc), install_result)))

        threading.Thread(target=worker, daemon=True).start()

    def start_outdated_refresh(self, context: PythonContext, generation: int) -> None:
        self.outdated_loading = True

        def worker() -> None:
            targets, warning = load_outdated_targets(context)
            self.event_queue.put(("outdated_done", (context.id, generation, targets, warning)))

        threading.Thread(target=worker, daemon=True).start()

    def start_dependency_refresh(self, context: PythonContext, generation: int) -> None:
        display_by_name = {row.name: row.display_name for row in self.rows}
        self.dependency_loading = True

        def worker() -> None:
            try:
                payload = load_dependency_info(context, display_by_name)
                self.event_queue.put(("dependency_done", (context.id, generation, payload, None)))
            except Exception as exc:
                self.event_queue.put(("dependency_done", (context.id, generation, None, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def process_events(self) -> None:
        while True:
            try:
                event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                return
            if event == "refresh_done":
                context_id, generation, rows, warning, install_result = payload  # type: ignore[misc]
                self.on_refresh_done(context_id, generation, rows, warning, install_result)
            elif event == "refresh_failed":
                context_id, generation, error, install_result = payload  # type: ignore[misc]
                self.on_refresh_failed(context_id, generation, error, install_result)
            elif event == "contexts_done":
                self.on_contexts_done(payload)  # type: ignore[arg-type]
            elif event == "contexts_failed":
                self.on_contexts_failed(str(payload))
            elif event == "outdated_done":
                context_id, generation, targets, warning = payload  # type: ignore[misc]
                self.on_outdated_done(context_id, generation, targets, warning)
            elif event == "dependency_done":
                context_id, generation, dependency_payload, error = payload  # type: ignore[misc]
                self.on_dependency_done(context_id, generation, dependency_payload, error)
            elif event == "install_done":
                self.on_install_done(payload)  # type: ignore[arg-type]
            elif event == "uv_bootstrap_done":
                context_id, returncode, detail, on_ready = payload  # type: ignore[misc]
                self.on_uv_bootstrap_done(context_id, returncode, detail, on_ready)
            elif event == "versions_done":
                row_name, versions, error = payload  # type: ignore[misc]
                self.on_versions_done(row_name, versions, error)

    def on_uv_bootstrap_done(
        self,
        context_id: str,
        returncode: int,
        detail: str,
        on_ready: Callable[[], None],
    ) -> None:
        context = self.find_context(context_id)
        if context is None:
            return
        if returncode == 0:
            context.uv_available = True
            self.message = "uv installed"
            on_ready()
        else:
            self.message = "uv installation failed"
            self.info_open = True
            self.rows = [
                PackageRow(
                    name="uv",
                    display_name="uv bootstrap",
                    uninstall_safe=False,
                    installed_version="-",
                    target_version="-",
                    candidate_versions=["-"],
                    status="failed",
                    last_error="uv installation failed",
                    last_error_detail=detail,
                )
            ]

    def on_refresh_done(
        self,
        context_id: str,
        generation: int,
        rows: list[PackageRow],
        warning: str | None,
        install_result: InstallResult | None,
    ) -> None:
        if self.context is None or self.context.id != context_id or generation != self.refresh_generation:
            return
        self.refreshing = False
        self.refresh_context_id = None

        if install_result is not None:
            self.note_updated_packages(context_id, install_result.before_versions, rows)
            updated_names = self.updated_by_context.get(context_id, set())
            for row in rows:
                row.updated_in_session = row.name in updated_names

        self.rows = rows
        self.focus_index = min(self.focus_index, max(0, len(self.rows) - 1))
        self.scroll = min(self.scroll, max(0, len(self.rows) - 1))
        self.restore_bulk_failed_rows()

        if install_result is not None:
            self.installing = False
            self.active_install_context_id = None
            if install_result.ok:
                self.message = f"Installed {install_result.package_name}=={install_result.target_version}"
            else:
                self.mark_failed_row(install_result)
                self.message = f"Install failed: {install_result.package_name}"
        elif warning:
            self.message = warning
        else:
            self.message = f"Loaded {len(self.rows)} packages"

        if install_result is not None:
            if self.bulk_active:
                self.continue_bulk_update()
            else:
                self.maybe_start_waiting_install()

        context = self.context
        if context is not None and context.id == context_id and not self.installing:
            self.start_dependency_refresh(context, generation)
            self.start_outdated_refresh(context, generation)

    def on_refresh_failed(
        self,
        context_id: str,
        generation: int,
        error: str,
        install_result: InstallResult | None,
    ) -> None:
        if generation != self.refresh_generation:
            return
        self.refreshing = False
        self.refresh_context_id = None
        if install_result is not None:
            self.installing = False
            self.active_install_context_id = None
        if self.context is not None and self.context.id == context_id:
            self.message = f"Refresh failed: {last_lines(error, 2)}"
        if install_result is not None:
            if self.bulk_active:
                self.continue_bulk_update()
            else:
                self.maybe_start_waiting_install()

    def on_outdated_done(
        self,
        context_id: str,
        generation: int,
        targets: dict[str, str],
        warning: str | None,
    ) -> None:
        if self.context is None or self.context.id != context_id or generation != self.refresh_generation:
            return
        self.outdated_loading = False
        for row in self.rows:
            latest = targets.get(row.name)
            if not latest:
                continue
            if latest not in row.candidate_versions:
                row.candidate_versions = sorted(set(row.candidate_versions + [latest]), key=version_key)
            if (
                row.target_version == row.installed_version
                and row.status not in {"failed", "installing", "wait", "loading"}
            ):
                row.target_version = latest
                row.status = "ready" if latest != row.installed_version else "current"
        if warning:
            self.message = warning
        elif targets:
            self.message = "Latest target versions loaded"

    def on_dependency_done(
        self,
        context_id: str,
        generation: int,
        dependency_payload: tuple[set[str], dict[str, list[str]], dict[str, list[str]]] | None,
        error: str | None,
    ) -> None:
        if self.context is None or self.context.id != context_id or generation != self.refresh_generation:
            return
        self.dependency_loading = False
        if dependency_payload is None:
            if error:
                self.message = f"Dependency data unavailable: {last_lines(error, 2)}"
            return
        safe_names, dependency_packages, usage_packages = dependency_payload
        for row in self.rows:
            row.uninstall_safe = row.name in safe_names
            row.dependency_packages = dependency_packages.get(row.name, [])
            row.usage_packages = usage_packages.get(row.name, [])

    def restore_bulk_failed_rows(self) -> None:
        if not self.bulk_failed_results:
            return
        for result in self.bulk_failed_results.values():
            self.apply_failed_result_to_row(result)

    def note_updated_packages(
        self,
        context_id: str,
        before_versions: dict[str, str],
        rows: list[PackageRow],
    ) -> None:
        changed = self.updated_by_context.setdefault(context_id, set())
        for row in rows:
            previous = before_versions.get(row.name)
            if previous is None or previous != row.installed_version:
                changed.add(row.name)

    def mark_failed_row(self, result: InstallResult) -> None:
        if self.bulk_active:
            self.bulk_failed_results[result.package_name] = result
            self.bulk_processed.add(result.package_name)
        self.apply_failed_result_to_row(result)

    def apply_failed_result_to_row(self, result: InstallResult) -> None:
        detail = command_detail(
            result.command,
            result.returncode,
            result.stdout,
            result.stderr,
            result.elapsed,
            include_command=False,
        )
        row = self.find_row(result.package_name)
        if row is None:
            return
        if result.target_version not in row.candidate_versions:
            row.candidate_versions = sorted(set(row.candidate_versions + [result.target_version]), key=version_key)
        row.target_version = result.target_version
        row.status = "failed"
        row.last_error = f"Install failed with exit code {result.returncode}"
        row.last_error_detail = detail

    def on_install_done(self, result: InstallResult) -> None:
        context = self.find_context(result.context_id)
        if context is None:
            self.installing = False
            self.active_install_context_id = None
            return
        self.start_refresh(context, "Refreshing packages after install", result)

    def maybe_start_waiting_install(self) -> None:
        if self.installing or self.wait_request is None:
            return
        context_id, package_name, target_version = self.wait_request
        self.wait_request = None
        if self.context is None or self.context.id != context_id:
            return
        row = self.find_row(package_name)
        if row is None:
            self.message = f"Waiting package vanished: {package_name}"
            return
        if row.installed_version == target_version:
            row.status = "current"
            self.message = f"Waiting package already current: {row.display_name}"
            return
        if target_version not in row.candidate_versions:
            row.candidate_versions = sorted(set(row.candidate_versions + [target_version]), key=version_key)
        row.target_version = target_version
        self.begin_install(row)

    def start_bulk_update(self) -> None:
        if self.installing or self.refreshing:
            self.message = "Update all waits until the current activity finishes"
            return
        context = self.context
        if context is None:
            return
        if not context.uv_available:
            self.ensure_uv(context, self.start_bulk_update)
            return
        seen: set[str] = set()
        queue_items: list[tuple[str, str]] = []
        for row in self.rows:
            if row.name in seen:
                continue
            seen.add(row.name)
            if row.status == "ready" and not row.updated_in_session:
                queue_items.append((row.name, row.target_version))
        if not queue_items:
            self.message = "No ready packages to update"
            return
        title = "Confirm update all"
        message = f"Install updates for {len(queue_items)} ready package(s)? y/N"
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            title = "Confirm interpreter update all"
            message = (
                f"Install updates for {len(queue_items)} ready package(s) into interpreter "
                f"{context.python_path}? y/N"
            )
        self.prompt = Prompt(
            title=title,
            message=message,
            on_yes=lambda items=queue_items, selected_context=context: self.confirm_bulk_update(items, selected_context),
            on_no=lambda: setattr(self, "message", "Update all cancelled"),
        )

    def confirm_bulk_update(
        self,
        queue_items: list[tuple[str, str]],
        confirmed_context: PythonContext | None = None,
    ) -> None:
        self.prompt = None
        context = self.context
        if (
            confirmed_context is not None
            and context is not None
            and context.id == confirmed_context.id
            and context.type == "interpreter"
        ):
            context.confirmed_for_mutation = True
        self.bulk_active = True
        self.bulk_queue = list(queue_items)
        self.bulk_processed = set()
        self.bulk_failed_results = {}
        self.mark_bulk_pending_waits()
        self.message = f"Updating {len(queue_items)} ready packages"
        self.continue_bulk_update()

    def mark_bulk_pending_waits(self) -> None:
        pending = {name: target for name, target in self.bulk_queue}
        for row in self.rows:
            target = pending.get(row.name)
            if (
                target is not None
                and row.installed_version != target
                and row.status not in {"installing", "failed"}
            ):
                row.status = "wait"

    def continue_bulk_update(self) -> None:
        if self.installing or not self.bulk_active:
            return
        while self.bulk_queue:
            package_name, target_version = self.bulk_queue.pop(0)
            normalized = canonicalize_name(package_name)
            row = self.find_row(normalized)
            if row is None:
                self.bulk_processed.add(normalized)
                continue
            if normalized in self.bulk_processed:
                if row.status != "failed":
                    row.status = "skipped"
                continue
            if row.installed_version == target_version or row.status in {"failed", "installing"}:
                row.status = "skipped" if row.status not in {"current", "failed"} else row.status
                self.bulk_processed.add(normalized)
                continue
            if target_version not in row.candidate_versions:
                row.candidate_versions = sorted(set(row.candidate_versions + [target_version]), key=version_key)
            row.target_version = target_version
            self.bulk_processed.add(normalized)
            self.mark_bulk_pending_waits()
            self.begin_install(row)
            return
        self.bulk_active = False
        self.bulk_processed = set()
        self.bulk_failed_results = {}
        self.message = "Update all complete"
        self.maybe_start_waiting_install()

    def find_context(self, context_id: str) -> PythonContext | None:
        return next((context for context in self.contexts if context.id == context_id), None)

    def find_row(self, package_name: str) -> PackageRow | None:
        normalized = canonicalize_name(package_name)
        return next((row for row in self.rows if row.name == normalized), None)

    def refresh_version_options(self, row: PackageRow) -> None:
        self.version_options = sorted(set(row.candidate_versions + [row.target_version]), key=version_key, reverse=True)
        try:
            self.version_overlay_index = self.version_options.index(row.target_version)
        except ValueError:
            self.version_overlay_index = 0
        self.ensure_version_overlay_visible()

    def start_version_lookup(self, row: PackageRow, pending_direction: int | None = None) -> bool:
        if row.versions_loaded:
            return False
        if self.version_loading:
            if self.version_overlay_row == row.name and pending_direction is not None:
                self.pending_version_direction = pending_direction
                self.message = f"Version change waits for {row.display_name} versions"
            else:
                self.message = "Version lookup already in progress"
            return False
        self.version_overlay_row = row.name
        self.version_error = None
        self.version_loading = True
        self.pending_version_direction = pending_direction
        if row.status not in {"installing", "wait", "failed"}:
            row.status = "loading"
        self.message = f"Loading versions for {row.display_name}"

        def worker(package_name: str) -> None:
            try:
                versions = fetch_available_versions(package_name)
                self.event_queue.put(("versions_done", (canonicalize_name(package_name), versions, None)))
            except Exception as exc:
                self.event_queue.put(("versions_done", (canonicalize_name(package_name), [], str(exc))))

        threading.Thread(target=worker, args=(row.display_name,), daemon=True).start()
        return True

    def apply_version_direction(self, row: PackageRow, direction: int) -> None:
        if not row.candidate_versions:
            return
        candidates = sorted(set(row.candidate_versions + [row.target_version]), key=version_key)
        try:
            index = candidates.index(row.target_version)
        except ValueError:
            index = 0
        index = max(0, min(len(candidates) - 1, index + direction))
        row.candidate_versions = candidates
        row.target_version = candidates[index]
        if row.status not in {"installing", "wait", "failed"}:
            row.status = "ready" if row.target_version != row.installed_version else "current"
        if self.version_overlay and self.version_overlay_row == row.name:
            self.refresh_version_options(row)

    def open_version_selector(self) -> None:
        row = self.focused_row()
        if row is None:
            return
        self.version_overlay = True
        self.version_overlay_row = row.name
        self.version_overlay_scroll = 0
        self.refresh_version_options(row)
        self.version_error = None
        self.pending_version_direction = None
        self.start_version_lookup(row)

    def on_versions_done(self, row_name: str, versions: list[str], error: str | None) -> None:
        row = self.find_row(row_name)
        if row is None:
            return
        self.version_loading = False
        pending_direction = self.pending_version_direction if self.version_overlay_row == row.name else None
        self.pending_version_direction = None
        if versions:
            row.candidate_versions = sorted(set(versions + row.candidate_versions), key=version_key)
            row.versions_loaded = True
            self.refresh_version_options(row)
            self.version_error = None
            self.message = f"Loaded {len(self.version_options)} versions for {row.display_name}"
        else:
            self.refresh_version_options(row)
            self.version_error = f"Version lookup failed: {last_lines(error or 'no versions found', 2)}"
            self.message = self.version_error
        if row.status == "loading":
            row.status = "ready" if row.target_version != row.installed_version else "current"
        if pending_direction is not None and versions:
            self.apply_version_direction(row, pending_direction)

    def handle_key(self, key: str) -> None:
        if self.prompt:
            self.handle_prompt_key(key)
            return
        if self.info_open:
            if key in {"esc", "enter", "f3", "q"}:
                self.info_open = False
                self.info_scroll = 0
            elif key == "up":
                self.info_scroll = max(0, self.info_scroll - 1)
            elif key == "down":
                self.info_scroll += 1
            elif key == "pageup":
                self.info_scroll = max(0, self.info_scroll - self.page_size())
            elif key == "pagedown":
                self.info_scroll += self.page_size()
            elif key == "home":
                self.info_scroll = 0
            elif key == "end":
                self.info_scroll = 10**9
            return
        if self.version_overlay:
            self.handle_version_overlay_key(key)
            return
        if self.context_overlay:
            self.handle_context_overlay_key(key)
            return

        if key in {"quit", "q", "f10"}:
            self.request_quit()
        elif key == "up":
            self.move_focus(-1)
        elif key == "down":
            self.move_focus(1)
        elif key == "pageup":
            self.move_focus(-self.page_size())
        elif key == "pagedown":
            self.move_focus(self.page_size())
        elif key == "home":
            self.focus_index = 0
        elif key == "end":
            self.focus_index = max(0, len(self.rows) - 1)
        elif key == "left":
            self.change_target_version(-1)
        elif key == "right":
            self.change_target_version(1)
        elif key == "enter":
            self.request_install()
        elif key == "f2":
            self.start_bulk_update()
        elif key == "f4":
            self.open_version_selector()
        elif key == "f9" or key == "c":
            self.context_overlay = True
            self.context_overlay_index = self.context_index
        elif key == "f3":
            self.info_open = True
            self.info_scroll = 0
        elif key == "r":
            context = self.context
            if context:
                if self.installing:
                    self.message = "Refresh waits until the current install finishes"
                    return
                self.ensure_uv(context, lambda: self.start_refresh(context, "Refreshing packages"))

    def handle_version_overlay_key(self, key: str) -> None:
        if key in {"esc", "q", "f4"}:
            self.version_overlay = False
            return
        if not self.version_options:
            return
        if key == "up":
            self.version_overlay_index = max(0, self.version_overlay_index - 1)
            self.ensure_version_overlay_visible()
        elif key == "down":
            self.version_overlay_index = min(len(self.version_options) - 1, self.version_overlay_index + 1)
            self.ensure_version_overlay_visible()
        elif key == "pageup":
            self.version_overlay_index = max(0, self.version_overlay_index - self.page_size())
            self.ensure_version_overlay_visible()
        elif key == "pagedown":
            self.version_overlay_index = min(
                len(self.version_options) - 1,
                self.version_overlay_index + self.page_size(),
            )
            self.ensure_version_overlay_visible()
        elif key == "home":
            self.version_overlay_index = 0
            self.ensure_version_overlay_visible()
        elif key == "end":
            self.version_overlay_index = len(self.version_options) - 1
            self.ensure_version_overlay_visible()
        elif key == "enter":
            row = self.find_row(self.version_overlay_row or "")
            if row is None:
                self.version_overlay = False
                return
            selected = self.version_options[self.version_overlay_index]
            if selected not in row.candidate_versions:
                row.candidate_versions = sorted(set(row.candidate_versions + [selected]), key=version_key)
            row.target_version = selected
            if row.status not in {"installing", "wait", "failed"}:
                row.status = "ready" if row.target_version != row.installed_version else "current"
            self.version_overlay = False
            self.focus_index = self.rows.index(row)
            self.request_install()

    def handle_prompt_key(self, key: str) -> None:
        prompt = self.prompt
        if prompt is None:
            return
        if key in {"y", "enter"}:
            prompt.on_yes()
        elif key in {"n", "esc", "q"}:
            self.prompt = None
            if prompt.on_no:
                prompt.on_no()
            elif key == "esc":
                self.message = "Cancelled"

    def handle_context_overlay_key(self, key: str) -> None:
        if key in {"esc", "q", "f9"}:
            self.context_overlay = False
            return
        if not self.contexts:
            return
        if key == "up":
            self.context_overlay_index = max(0, self.context_overlay_index - 1)
        elif key == "down":
            self.context_overlay_index = min(len(self.contexts) - 1, self.context_overlay_index + 1)
        elif key == "pageup":
            self.context_overlay_index = max(0, self.context_overlay_index - self.page_size())
        elif key == "pagedown":
            self.context_overlay_index = min(len(self.contexts) - 1, self.context_overlay_index + self.page_size())
        elif key == "enter":
            if self.installing:
                self.message = "Context switch waits until the current install finishes"
                self.context_overlay = False
                return
            if self.context_overlay_index != self.context_index:
                self.context_index = self.context_overlay_index
                self.context_overlay = False
                self.load_current_context()
            else:
                self.context_overlay = False

    def move_focus(self, amount: int) -> None:
        if not self.rows:
            return
        self.focus_index = max(0, min(len(self.rows) - 1, self.focus_index + amount))

    def page_size(self) -> int:
        _, height = self.terminal.size()
        return max(1, height - 5)

    def ensure_scroll_visible(self) -> None:
        visible = self.page_size()
        if self.focus_index < self.scroll:
            self.scroll = self.focus_index
        elif self.focus_index >= self.scroll + visible:
            self.scroll = self.focus_index - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, len(self.rows) - visible)))

    def version_visible_items(self, height: int | None = None) -> int:
        if height is None:
            _, height = self.terminal.size()
        height = max(height, 10)
        overlay_h = min(height - 4, max(8, min(len(self.version_options) + 5, height - 4)))
        return max(1, overlay_h - 4)

    def ensure_version_overlay_visible(self, visible: int | None = None) -> None:
        if not self.version_options:
            self.version_overlay_scroll = 0
            self.version_overlay_index = 0
            return
        visible = visible or self.version_visible_items()
        self.version_overlay_index = max(0, min(self.version_overlay_index, len(self.version_options) - 1))
        if self.version_overlay_index < self.version_overlay_scroll:
            self.version_overlay_scroll = self.version_overlay_index
        elif self.version_overlay_index >= self.version_overlay_scroll + visible:
            self.version_overlay_scroll = self.version_overlay_index - visible + 1
        self.version_overlay_scroll = max(0, min(self.version_overlay_scroll, max(0, len(self.version_options) - visible)))

    def change_target_version(self, direction: int) -> None:
        row = self.focused_row()
        if row is None:
            return
        if not row.versions_loaded:
            self.start_version_lookup(row, pending_direction=direction)
            return
        self.apply_version_direction(row, direction)

    def focused_row(self) -> PackageRow | None:
        if not self.rows:
            return None
        self.focus_index = max(0, min(self.focus_index, len(self.rows) - 1))
        return self.rows[self.focus_index]

    def request_install(self) -> None:
        context = self.context
        row = self.focused_row()
        if context is None or row is None:
            return
        if self.installing:
            self.mark_wait(row, context)
            return
        if row.target_version == row.installed_version:
            self.message = f"{row.display_name} is already at {row.installed_version}"
            row.status = "current"
            return
        if not context.uv_available:
            self.ensure_uv(context, lambda: self.request_install())
            return
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            self.prompt = Prompt(
                title="Confirm interpreter install",
                message=f"Install into interpreter {context.python_path}? y/N",
                on_yes=lambda: self.confirm_interpreter_and_install(context),
                on_no=lambda: setattr(self, "message", "Install cancelled"),
            )
            return
        self.begin_install(row)

    def confirm_interpreter_and_install(self, context: PythonContext) -> None:
        self.prompt = None
        context.confirmed_for_mutation = True
        self.request_install()

    def mark_wait(self, row: PackageRow, context: PythonContext) -> None:
        if self.wait_request:
            _, old_name, _ = self.wait_request
            old_row = self.find_row(old_name)
            if old_row and old_row.status == "wait":
                old_row.status = "ready" if old_row.target_version != old_row.installed_version else "current"
        row.status = "wait"
        self.wait_request = (context.id, row.name, row.target_version)
        self.message = f"Queued after current install: {row.display_name}"

    def begin_install(self, row: PackageRow) -> None:
        context = self.context
        if context is None:
            return
        self.installing = True
        self.active_install_context_id = context.id
        row.status = "installing"
        row.last_error = None
        row.last_error_detail = None
        before_versions = {item.name: item.installed_version for item in self.rows}
        package_name = row.name
        package_display_name = row.display_name
        target_version = row.target_version
        package_spec = f"{package_display_name}=={target_version}"
        command = [
            str(context.python_path),
            "-m",
            "uv",
            "pip",
            "install",
            "--python",
            context.uv_target,
        ]
        if context.type == "interpreter":
            command.append("--system")
        command.append(package_spec)
        self.message = f"Installing {package_spec}"

        def worker() -> None:
            start = time.time()
            try:
                proc = run_command(command, timeout=None)
                elapsed = time.time() - start
                result = InstallResult(
                    context_id=context.id,
                    package_name=package_name,
                    target_version=target_version,
                    command=command,
                    returncode=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    elapsed=elapsed,
                    before_versions=before_versions,
                )
            except Exception as exc:
                elapsed = time.time() - start
                result = InstallResult(
                    context_id=context.id,
                    package_name=package_name,
                    target_version=target_version,
                    command=command,
                    returncode=1,
                    stdout="",
                    stderr=str(exc),
                    elapsed=elapsed,
                    before_versions=before_versions,
                )
            self.event_queue.put(("install_done", result))

        threading.Thread(target=worker, daemon=True).start()

    def render(self) -> str:
        width, height = self.terminal.size()
        width = max(width, 40)
        height = max(height, 10)
        lines: list[str] = []
        lines.append(self.header_line(width))
        lines.append(self.separator(width))
        lines.append(self.table_header(width))

        visible_rows = max(1, height - 5)
        for absolute_index in range(self.scroll, self.scroll + visible_rows):
            if absolute_index < len(self.rows):
                lines.append(self.render_row(width, absolute_index, self.rows[absolute_index]))
            else:
                lines.append(" " * width)

        lines.append(self.separator(width))
        lines.append(self.footer_line(width))

        if self.context_overlay or self.version_overlay or self.info_open or self.prompt:
            lines = self.dim_background(lines, width)
        if self.context_overlay:
            lines = self.overlay_contexts(lines, width, height)
        if self.version_overlay:
            lines = self.overlay_versions(lines, width, height)
        if self.info_open:
            lines = self.overlay_info(lines, width, height)
        if self.prompt:
            lines = self.overlay_prompt(lines, width, height)

        return "\n".join(lines[:height]).ljust(width)

    def separator(self, width: int) -> str:
        return "─" * width

    def header_line(self, width: int) -> str:
        context = self.context
        if context is not None:
            label = context.label
        elif self.discovery_error:
            label = self.discovery_error
        elif self.discovering_contexts:
            label = "Discovering Python contexts..."
        else:
            label = "Starting..."
        return truncate(f"[ {label} ]", width).ljust(width)

    def table_header(self, width: int) -> str:
        name_w, installed_w, target_w, action_w = self.columns(width)
        text = (
            f"{'Package':<{name_w}}"
            f"{'Installed':<{installed_w}}"
            f"{'Target':<{target_w}}"
            f"{'Action':<{action_w}}"
        )
        return text[:width].ljust(width)

    def render_row(self, width: int, index: int, row: PackageRow) -> str:
        name_w, installed_w, target_w, action_w = self.columns(width)
        action = self.display_status(row)
        name = ("* " if row.uninstall_safe else "  ") + row.display_name
        text = (
            f"{truncate(name, name_w):<{name_w}}"
            f"{truncate(row.installed_version, installed_w):<{installed_w}}"
            f"{truncate(row.target_version, target_w):<{target_w}}"
            f"{truncate(action, action_w):<{action_w}}"
        )
        line = text[:width].ljust(width)
        style = ""
        if index == self.focus_index:
            style += REVERSE
        if row.status == "failed":
            style += BOLD_RED
        elif row.updated_in_session:
            style += WHITE
        elif row.status == "current":
            style += LIGHT_GREEN
        elif row.is_outdated:
            style += YELLOW
        if style:
            return style + line + RESET
        return line

    def columns(self, width: int) -> tuple[int, int, int, int]:
        inner = width
        action_w = 10
        installed_w = 20 if inner >= 72 else 16
        target_w = 20 if inner >= 72 else 16
        name_w = max(12, inner - installed_w - target_w - action_w)
        if name_w < 18 and inner > 50:
            installed_w = 16
            target_w = 16
            name_w = max(12, inner - installed_w - target_w - action_w)
        return name_w, installed_w, target_w, action_w

    def display_status(self, row: PackageRow) -> str:
        if row.status == "installing":
            return SPINNER[self.spinner_index]
        if row.status == "wait":
            return "Wait"
        return row.status

    def footer_line(self, width: int) -> str:
        if width >= 96:
            keys = "↑/↓ Row | PgUp/PgDn | ←/→ Ver | ↵ Install | F2 All | F3 Info | F4 Ver | F9 Ctx | F10 Quit"
        elif width >= 72:
            keys = "↑/↓ | Pg | ←/→ | ↵ | F2 All | F3 Info | F4 Ver | F9 Ctx | F10 Quit"
        else:
            keys = "↑/↓ | Pg | ←/→ | ↵ | F2 | F3 | F4 | F9 | F10"
        return truncate(keys, width).ljust(width)

    def dim_background(self, lines: list[str], width: int) -> list[str]:
        return [MODAL_BACKDROP + strip_ansi(line).ljust(width)[:width] + RESET for line in lines]

    def overlay_contexts(self, lines: list[str], width: int, height: int) -> list[str]:
        overlay_w = min(width - 4, max(50, width * 3 // 4))
        overlay_h = min(height - 4, max(6, len(self.contexts) + 4))
        top = max(1, (height - overlay_h) // 2)
        left = max(1, (width - overlay_w) // 2)
        items_visible = overlay_h - 3
        start = max(0, min(self.context_overlay_index, max(0, len(self.contexts) - items_visible)))
        box = [self.box_border(overlay_w, "top"), self.box_line("Context selector", overlay_w)]
        for idx in range(start, start + items_visible):
            if idx < len(self.contexts):
                marker = ">" if idx == self.context_overlay_index else " "
                uv = "uv" if self.contexts[idx].uv_available else "no uv"
                label = f"{marker} {self.contexts[idx].label} [{uv}]"
            else:
                label = ""
            content = self.box_line(label, overlay_w)
            if idx == self.context_overlay_index:
                content = REVERSE + content + RESET
            box.append(content)
        box.append(self.box_border(overlay_w, "bottom"))
        return paste_box(lines, box, top, left, width)

    def overlay_versions(self, lines: list[str], width: int, height: int) -> list[str]:
        row = self.find_row(self.version_overlay_row or "")
        title = f"Versions: {row.display_name if row else ''}".strip()
        overlay_w = min(width - 4, max(42, width // 2))
        overlay_h = min(height - 4, max(8, min(len(self.version_options) + 5, height - 4)))
        top = max(1, (height - overlay_h) // 2)
        left = max(1, (width - overlay_w) // 2)
        items_visible = overlay_h - 4
        self.ensure_version_overlay_visible(items_visible)
        start = self.version_overlay_scroll
        box = [self.box_border(overlay_w, "top"), self.box_line(title, overlay_w)]
        hint = "Enter install | Esc/q close"
        if self.version_loading:
            hint = "Loading versions... | Esc/q close"
        elif self.version_error:
            hint = f"{self.version_error} | Esc/q close"
        box.append(self.box_line(hint, overlay_w))
        for idx in range(start, start + items_visible):
            if idx < len(self.version_options):
                version = self.version_options[idx]
                marker = ">" if idx == self.version_overlay_index else " "
                current = " installed" if row and version == row.installed_version else ""
                target = " target" if row and version == row.target_version else ""
                label = f"{marker} {version}{current}{target}"
            else:
                label = ""
            content = self.box_line(label, overlay_w)
            if idx == self.version_overlay_index:
                content = REVERSE + content + RESET
            box.append(content)
        box.append(self.box_border(overlay_w, "bottom"))
        return paste_box(lines, box, top, left, width)

    def package_relation_lines(self, row: PackageRow) -> list[str]:
        lines = ["", "Dependency packages:"]
        if row.dependency_packages:
            lines.extend(f"  {name}" for name in row.dependency_packages)
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("Usage packages:")
        if row.usage_packages:
            lines.extend(f"  {name}" for name in row.usage_packages)
        else:
            lines.append("  (none)")
        return lines

    def overlay_info(self, lines: list[str], width: int, height: int) -> list[str]:
        row = self.focused_row()
        if row is None:
            body = ["No package selected."]
        elif row.status == "failed" and row.last_error_detail:
            body = [
                f"Package: {row.display_name}",
                f"Version: {row.installed_version}",
                *self.package_relation_lines(row),
                "",
                row.last_error or "Install failed",
                "",
                *row.last_error_detail.splitlines(),
            ]
        else:
            body = [
                f"Package: {row.display_name}",
                f"Version: {row.installed_version}",
                *self.package_relation_lines(row),
            ]
        return self.overlay_text(lines, width, height, "Information", body, scroll_attr="info_scroll")

    def overlay_prompt(self, lines: list[str], width: int, height: int) -> list[str]:
        prompt = self.prompt
        if prompt is None:
            return lines
        body = [prompt.message, "", "Y/Enter: yes    N/Esc/q: no"]
        return self.overlay_text(lines, width, height, prompt.title, body)

    def overlay_text(
        self,
        lines: list[str],
        width: int,
        height: int,
        title: str,
        body: list[str],
        scroll_attr: str | None = None,
    ) -> list[str]:
        overlay_w = min(width - 4, max(50, width * 4 // 5))
        wrapped: list[str] = []
        for line in body:
            if not line:
                wrapped.append("")
            else:
                wrapped.extend(textwrap.wrap(line, width=max(10, overlay_w - 4)) or [""])
        overlay_h = min(height - 4, max(6, min(len(wrapped) + 4, height - 4)))
        top = max(1, (height - overlay_h) // 2)
        left = max(1, (width - overlay_w) // 2)
        visible = overlay_h - 3
        scroll = 0
        if scroll_attr is not None:
            scroll = max(0, int(getattr(self, scroll_attr, 0)))
            scroll = min(scroll, max(0, len(wrapped) - visible))
            setattr(self, scroll_attr, scroll)
            if len(wrapped) > visible:
                title = f"{title} ({scroll + 1}-{min(scroll + visible, len(wrapped))}/{len(wrapped)})"
        box = [self.box_border(overlay_w, "top"), self.box_line(title, overlay_w)]
        for line in wrapped[scroll : scroll + visible]:
            box.append(self.box_line(line, overlay_w))
        while len(box) < overlay_h - 1:
            box.append(self.box_line("", overlay_w))
        box.append(self.box_border(overlay_w, "bottom"))
        return paste_box(lines, box, top, left, width)

    def box_border(self, width: int, kind: str = "top") -> str:
        if kind == "bottom":
            return "└" + "─" * (width - 2) + "┘"
        return "┌" + "─" * (width - 2) + "┐"

    def box_line(self, text: str, width: int) -> str:
        return "│" + truncate(" " + text, width - 2).ljust(width - 2) + "│"


def truncate(value: object, width: int) -> str:
    text = str(value)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "~"


def framed_join(left: str, right: str, width: int) -> str:
    inner = width - 2
    if len(left) + len(right) > inner:
        left = truncate(left, inner - len(right))
    gap = max(0, inner - len(left) - len(right))
    return "│" + left + " " * gap + right + "│"


def unframed_join(left: str, right: str, width: int) -> str:
    if len(left) + len(right) > width:
        left = truncate(left, max(0, width - len(right) - 1))
    gap = max(1, width - len(left) - len(right))
    return (left + " " * gap + right)[:width].ljust(width)


def paste_box(lines: list[str], box: list[str], top: int, left: int, width: int) -> list[str]:
    result = list(lines)
    for offset, box_line in enumerate(box):
        row_index = top + offset
        if row_index >= len(result):
            break
        dimmed = result[row_index].startswith(MODAL_BACKDROP)
        raw = strip_ansi(result[row_index]).ljust(width)
        box_width = len(strip_ansi(box_line))
        if dimmed:
            result[row_index] = (
                MODAL_BACKDROP
                + raw[:left]
                + RESET
                + box_line
                + MODAL_BACKDROP
                + raw[left + box_width :]
                + RESET
            )
        else:
            result[row_index] = raw[:left] + box_line + raw[left + box_width :]
    return result


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def main() -> int:
    if "--version" in sys.argv:
        print("tuv 0.1.0")
        return 0
    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: tuv.py [--version]\n\nStart the Tuv alternate-screen package manager.")
        return 0
    try:
        return TuvApp().run()
    except Exception:
        print("Tuv crashed before the terminal UI could recover:", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

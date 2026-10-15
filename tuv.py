#!/usr/bin/env python3
"""Tuv: a native alternate-screen terminal UI for uv-managed packages."""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import json
import netrc
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
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from functools import partial
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar

try:
    import tomllib
except ImportError:  # Python < 3.11 cannot parse uv configuration files.
    tomllib = None

# `--prepare-runner` runs under the bootstrap interpreter before the runner
# requirements exist. The terminal UI refuses to start without them.
try:
    from packaging.markers import Marker, default_environment
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.utils import (
        InvalidSdistFilename,
        InvalidWheelFilename,
        canonicalize_name,
        parse_sdist_filename,
        parse_wheel_filename,
    )
    from packaging.version import InvalidVersion, Version
    from wcwidth import wcwidth
except ImportError as exc:
    RUNNER_IMPORT_ERROR: ImportError | None = exc
else:
    RUNNER_IMPORT_ERROR = None

T = TypeVar("T")

TUV_VERSION = "0.3.0"
IS_WINDOWS = os.name == "nt"
TUV_HOME = Path(os.environ.get("TUV_HOME", Path(__file__).resolve().parent)).resolve()
RUNNER_VENV = Path(os.environ.get("TUV_RUNNER_VENV", TUV_HOME / ".tuv-venv")).resolve()
RUNNER_VENV_KEEP = 4

ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
CLEAR = "\x1b[2J"
HOME = "\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
RESET = "\x1b[0m"
BOLD_RED = "\x1b[1;31m"
BRIGHT_CYAN = "\x1b[96m"
LIGHT_GREEN = "\x1b[92m"
YELLOW = "\x1b[33m"
REVERSE = "\x1b[7m"
DIM = "\x1b[2m"
MODAL_BACKDROP = "\x1b[2;90m"
SPINNER = "-\\|/"
MIN_WIDTH = 40
MIN_HEIGHT = 8


# --------------------------------------------------------------------------- models


@dataclass
class PythonInfo:
    executable: Path
    version: tuple[int, int, int]
    prefix: str = ""
    base_prefix: str = ""
    base_executable: str = ""
    implementation: str = ""
    architecture: str = ""
    os_name: str = ""
    has_venv: bool = False
    source: str = "installed"

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)

    @property
    def is_venv(self) -> bool:
        if self.prefix and self.base_prefix and path_key(Path(self.prefix)) != path_key(Path(self.base_prefix)):
            return True
        return venv_root_of(self.executable) is not None


@dataclass
class UvProvider:
    type: str  # context_venv | reference_python | standalone | tuv
    executable: Path | None
    python_path: Path | None
    priority: int
    version: str

    @property
    def command_prefix(self) -> list[str]:
        if self.type == "standalone":
            if self.executable is None:
                raise RuntimeError("Standalone uv provider has no executable path")
            return [str(self.executable)]
        if self.python_path is None:
            raise RuntimeError("Module uv provider has no Python path")
        return [str(self.python_path), "-m", "uv"]


@dataclass
class PythonContext:
    id: str
    type: str  # tuv | venv | interpreter
    source: str  # tuv | active | cwd | installed | scanned
    label: str
    python_path: Path
    reference_python_path: Path | None
    root_path: Path | None
    version: str
    resolved_uv_provider: UvProvider | None = None
    confirmed_for_mutation: bool = False

    @property
    def is_virtual(self) -> bool:
        return self.type in {"tuv", "venv"}

    @property
    def uv_target(self) -> str:
        if self.is_virtual and self.root_path is not None:
            return str(self.root_path)
        return str(self.python_path)


@dataclass
class InstallResult:
    context_id: str
    package_name: str
    display_name: str
    operation: str  # install | uninstall
    target_version: str
    command: list[str]
    exit_code: int | None  # None when the process did not start
    stdout: str
    stderr: str
    elapsed: float
    before_versions: dict[str, str]
    installed_version_at_attempt: str = ""
    candidate_versions_at_attempt: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def label(self) -> str:
        return self.display_name or self.package_name

    @property
    def exit_text(self) -> str:
        return "process did not start" if self.exit_code is None else str(self.exit_code)


@dataclass
class PackageRow:
    name: str
    display_name: str
    installed_version: str
    target_version: str = ""
    candidate_versions: list[str] = field(default_factory=list)
    status: str = "loading"  # current | ready | loading | nodata | wait | installing | skipped | failed
    uninstall_safe: bool = False
    metadata_trusted: bool = False
    versions_resolved: bool = False
    full_versions_loaded: bool = False
    yanked_versions: set[str] = field(default_factory=set)
    color_hint: str | None = None
    dependency_packages: list[str] = field(default_factory=list)
    usage_packages: list[str] = field(default_factory=list)
    description: str | None = None
    updated_in_session: bool = False
    last_error: str | None = None
    last_error_detail: str | None = None
    last_install_result: InstallResult | None = None
    operational_error: bool = False

    def __post_init__(self) -> None:
        self.target_version = self.target_version or self.installed_version
        self.candidate_versions = self.candidate_versions or [self.installed_version]

    @property
    def is_outdated(self) -> bool:
        return self.target_version != self.installed_version and self.status != "failed"


@dataclass
class Prompt:
    title: str
    message: str
    on_yes: Callable[[], None]
    on_no: Callable[[], None]
    quit: bool = False


@dataclass
class PackageMetadata:
    description: str | None = None
    dependencies: set[str] = field(default_factory=set)
    extras: set[str] = field(default_factory=set)


@dataclass
class DependencyInfo:
    safe_names: set[str]
    dependencies: dict[str, list[str]]
    usage: dict[str, list[str]]
    descriptions: dict[str, str | None]


@dataclass(frozen=True)
class IndexEntry:
    name: str | None
    url: str
    explicit: bool = False
    default: bool = False
    authenticate: str = "auto"
    ignore_error_codes: tuple[int, ...] = ()


@dataclass(frozen=True)
class EffectiveIndexConfig:
    entries: tuple[IndexEntry, ...]
    strategy: str = "first-index"


# --------------------------------------------------------------------------- generic helpers


def version_key(value: str) -> tuple[int, object]:
    try:
        return (0, Version(value))
    except InvalidVersion:
        # Homogeneous (tag, value) chunks keep non-PEP 440 strings comparable.
        chunks = [(0, int(c)) if c.isdigit() else (1, c.lower()) for c in re.split(r"([0-9]+)", value) if c]
        return (1, tuple(chunks))


def path_key(path: Path) -> str:
    resolved = str(path.resolve())
    return resolved.lower() if IS_WINDOWS else resolved


def stable_context_id(context_type: str, path: Path) -> str:
    return f"{context_type}:{path_key(path)}"


def venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def is_venv(root: Path) -> bool:
    return (root / "pyvenv.cfg").is_file() and venv_python(root).is_file()


def venv_root_of(executable: Path) -> Path | None:
    parent = executable.parent
    if parent.name.lower() in {"bin", "scripts"} and (parent.parent / "pyvenv.cfg").is_file():
        return parent.parent.resolve()
    if (parent / "pyvenv.cfg").is_file():
        return parent.resolve()
    return None


def resolve_file_path(value: str | None) -> Path | None:
    text = (value or "").strip().strip('"')
    if not text:
        return None
    path = Path(text)
    if not path.is_file():
        found = shutil.which(text)
        if not found:
            return None
        path = Path(found)
    try:
        return path.resolve()
    except OSError:
        return None


def environment_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def run_command(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, shell=False)


def terminate_process_tree(proc: subprocess.Popen[str], grace: float = 0.5) -> None:
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=grace + 1.0,
                shell=False,
            )
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            with contextlib.suppress(Exception):
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=grace)
            return


class SingleFlight:
    """Caches successful results per key; concurrent callers share one in-flight computation."""

    def __init__(self, ttl: float | None = None) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._done: dict[object, tuple[float, object]] = {}
        self._inflight: dict[object, threading.Event] = {}

    def _fresh(self, key: object) -> object | None:
        hit = self._done.get(key)
        if hit is not None and (self.ttl is None or time.monotonic() - hit[0] < self.ttl):
            return hit[1]
        return None

    def get(self, key: object, compute: Callable[[], T | None], wait: float, retry: bool = False) -> T | None:
        """Return the cached value or compute it once; concurrent callers wait for that result.

        When the computation they waited for failed, waiters return None, or run it
        themselves when `retry` is set.
        """
        while True:
            with self._lock:
                hit = self._fresh(key)
                if hit is not None:
                    return hit  # type: ignore[return-value]
                event = self._inflight.get(key)
                if event is None:
                    event = self._inflight[key] = threading.Event()
                    break
            event.wait(wait)
            if not retry:
                with self._lock:
                    return self._fresh(key)  # type: ignore[return-value]
        try:
            value = compute()
            if value is not None:
                with self._lock:
                    self._done[key] = (time.monotonic(), value)
            return value
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()

    def clear(self) -> None:
        with self._lock:
            self._done.clear()


@contextlib.contextmanager
def file_lock(path: Path, timeout: float, stale_after: float, poll: float = 0.05) -> Iterator[bool]:
    """Exclusive inter-process lock file. Yields False when the lock could not be taken."""
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", "replace"))
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - path.stat().st_mtime > stale_after:
                    path.unlink()
                    continue
            if time.monotonic() >= deadline:
                break
            time.sleep(poll)
        except OSError:
            break
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                path.unlink()


def last_lines(text: str, count: int = 8) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def quote_arg(value: str) -> str:
    if not value:
        return '""'
    if re.search(r"\s", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def format_command(command: Iterable[str]) -> str:
    return " ".join(quote_arg(part) for part in command)


def command_detail(
    command: list[str],
    exit_code: int | None,
    stdout: str,
    stderr: str,
    elapsed: float = 0.0,
    include_command: bool = True,
) -> str:
    lines = [f"Command: {format_command(command)}"] if include_command else []
    exit_text = "process did not start" if exit_code is None else str(exit_code)
    lines += [
        f"Exit code: {exit_text}",
        f"Elapsed: {elapsed:.1f}s",
        "",
        "stdout:",
        last_lines(stdout, 12) or "(empty)",
        "",
        "stderr:",
        last_lines(stderr, 12) or "(empty)",
    ]
    return "\n".join(lines)


def failed_command_detail(command: list[str], proc: subprocess.CompletedProcess[str]) -> str:
    return command_detail(command, proc.returncode, proc.stdout, proc.stderr)


# --------------------------------------------------------------------------- python discovery

PROBE_CODE = (
    "import importlib.util, json, platform, sys; "
    "print(json.dumps({'version': sys.version_info[:3], 'executable': sys.executable, "
    "'prefix': sys.prefix, 'base_prefix': sys.base_prefix, "
    "'base_executable': getattr(sys, '_base_executable', None), "
    "'implementation': sys.implementation.name, 'architecture': platform.machine(), "
    "'os_name': sys.platform, 'has_venv': importlib.util.find_spec('venv') is not None}))"
)

_PROBE_CACHE: SingleFlight = SingleFlight()


def probe_python(executable: str | Path, source: str = "installed", timeout: float = 5.0) -> PythonInfo | None:
    """Run the JSON probe once per interpreter; results are cached for the process lifetime."""
    raw = str(executable).strip().strip('"')
    if not raw:
        return None
    resolved = raw if Path(raw).is_file() else shutil.which(raw)
    if not resolved:
        return None
    try:
        key = os.path.normcase(str(Path(resolved).resolve()))
    except OSError:
        key = os.path.normcase(resolved)

    def compute() -> PythonInfo | None:
        try:
            proc = run_command([resolved, "-c", PROBE_CODE], timeout=timeout)
            if proc.returncode != 0:
                return None
            data = json.loads(proc.stdout.strip().splitlines()[-1])
            version = tuple(int(part) for part in data["version"])
            if len(version) != 3:
                return None
            return PythonInfo(
                executable=Path(data["executable"]).resolve(),
                version=version,  # type: ignore[arg-type]
                prefix=str(data.get("prefix") or ""),
                base_prefix=str(data.get("base_prefix") or ""),
                base_executable=str(data.get("base_executable") or ""),
                implementation=str(data.get("implementation") or ""),
                architecture=str(data.get("architecture") or ""),
                os_name=str(data.get("os_name") or ""),
                has_venv=bool(data.get("has_venv")),
            )
        except Exception:
            return None

    info = _PROBE_CACHE.get(key, compute, timeout + 1.0)
    return replace(info, source=source) if info is not None else None


def parse_py_launcher_output(output: str) -> list[Path]:
    return [Path(m.group(1)) for m in re.finditer(r"([A-Za-z]:\\.*?python(?:w)?\.exe)", output, re.IGNORECASE)]


def registry_python_candidates() -> list[Path]:
    """PEP 514 registry entries: ExecutablePath and InstallPath values under Software\\Python."""
    if not IS_WINDOWS:
        return []
    import winreg

    found: list[Path] = []

    def read(key: object, name: str) -> str | None:
        try:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
        except OSError:
            return None

    def walk(root: object, subkey: str, depth: int = 0) -> None:
        if depth > 4:
            return
        try:
            with winreg.OpenKey(root, subkey) as key:
                if executable := read(key, "ExecutablePath"):
                    found.append(Path(executable))
                if install_path := read(key, "InstallPath"):
                    found.append(Path(install_path) / "python.exe")
                with contextlib.suppress(OSError):
                    with winreg.OpenKey(key, "InstallPath") as install_key:
                        if executable := read(install_key, "ExecutablePath"):
                            found.append(Path(executable))
                        if default_path := read(install_key, ""):
                            found.append(Path(default_path) / "python.exe")
                index = 0
                while True:
                    try:
                        child = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    walk(root, f"{subkey}\\{child}", depth + 1)
                    index += 1
        except OSError:
            return

    for root, subkey in (
        (winreg.HKEY_CURRENT_USER, r"Software\Python"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Python"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Python"),
    ):
        walk(root, subkey)
    return found


def windows_python_candidates() -> list[Path]:
    candidates: list[Path] = []
    with contextlib.suppress(Exception):
        proc = run_command(["py", "-0p"], timeout=3)
        candidates += parse_py_launcher_output(proc.stdout + "\n" + proc.stderr)
    candidates += registry_python_candidates()
    names = ["python.exe", "python3.exe"] + [f"python3.{minor}.exe" for minor in range(15, 6, -1)]
    candidates += [Path(found) for name in names if (found := shutil.which(name))]
    for env_name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_name)
        if not base:
            continue
        if env_name == "LOCALAPPDATA":
            candidates += (Path(base) / "Programs" / "Python").glob("Python*/python.exe")
        candidates += Path(base).glob("Python*/python.exe")
    return candidates


def posix_python_candidates() -> list[Path]:
    names = [f"python3.{minor}" for minor in range(15, 6, -1)] + ["python3", "python"]
    candidates = [Path(found) for name in names if (found := shutil.which(name))]
    for root in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin"):
        candidates += (path for path in Path(root).glob("python3*") if path.is_file())
    return candidates


def platform_python_candidates() -> list[Path]:
    return windows_python_candidates() if IS_WINDOWS else posix_python_candidates()


def cwd_python_candidates(cwd: Path | None = None, allow_venv: bool = False) -> list[Path]:
    cwd = (cwd or Path.cwd()).resolve()
    if (cwd / "pyvenv.cfg").is_file():
        return [venv_python(cwd)] if allow_venv and venv_python(cwd).is_file() else []
    if IS_WINDOWS:
        names = ["python.exe", "python3.exe", "Scripts/python.exe", "bin/python.exe"]
    else:
        names = ["python", "python3", "bin/python", "bin/python3"]
    return [cwd / name for name in names if (cwd / name).is_file()]


def launcher_python_candidates() -> list[Path]:
    newest = resolve_file_path(os.environ.get("TUV_NEWEST_PYTHON"))
    return [newest] if newest is not None else []


def dedupe_candidates(candidates: Iterable[tuple[Path, str]]) -> list[tuple[Path, str]]:
    seen: set[str] = set()
    unique: list[tuple[Path, str]] = []
    for candidate, source in candidates:
        key = str(candidate).lower() if IS_WINDOWS else str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append((candidate, source))
    return unique


def probe_all(
    candidates: Iterable[tuple[Path, str]],
    allow_venv: bool = True,
    runner_usable: bool = False,
) -> list[PythonInfo]:
    """Probe candidates concurrently, dedupe by resolved executable, and sort newest first."""
    unique = dedupe_candidates(candidates)
    if not unique:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(unique))) as pool:
        probed = list(pool.map(lambda item: probe_python(item[0], source=item[1]), unique))
    infos: dict[str, PythonInfo] = {}
    for info in probed:
        if info is None or (runner_usable and not info.has_venv) or (not allow_venv and info.is_venv):
            continue
        key = path_key(info.executable)
        if key in infos:
            if info.source == "cwd":  # the same interpreter found via cwd keeps the cwd label
                infos[key].source = "cwd"
            continue
        infos[key] = info
    return sorted(infos.values(), key=lambda item: item.version, reverse=True)


def discover_python_infos() -> list[PythonInfo]:
    candidates = [(path, "cwd") for path in cwd_python_candidates()]
    candidates += [(path, "installed") for path in launcher_python_candidates() + platform_python_candidates()]
    return probe_all(candidates, allow_venv=False)


def select_runner_python(mode: str) -> PythonInfo:
    cwd = Path.cwd()
    if mode == "cwd":
        infos = probe_all([(p, "cwd") for p in cwd_python_candidates(cwd, allow_venv=True)], runner_usable=True)
        if not infos:
            raise RuntimeError(f"No usable current-working-directory Python was found in {cwd}")
        return infos[0]
    candidates = [(p, "installed") for p in platform_python_candidates()] + [(Path(sys.executable), "installed")]
    infos = probe_all(candidates, allow_venv=False, runner_usable=True)
    if not infos:
        infos = probe_all([(p, "cwd") for p in cwd_python_candidates(cwd)], allow_venv=False, runner_usable=True)
    if not infos:
        raise RuntimeError("No usable Python interpreter was found.")
    return infos[0]


# --------------------------------------------------------------------------- uv providers

_UV_VERSION_CACHE: SingleFlight = SingleFlight()


def validate_uv_command(command: list[str], timeout: float = 5.0) -> str | None:
    """Return the uv version string when `<command> --version` works."""

    def compute() -> str | None:
        try:
            proc = run_command([*command, "--version"], timeout=timeout)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        first = next((line.strip() for line in (proc.stdout or proc.stderr).splitlines() if line.strip()), "")
        return first or "unknown"

    return _UV_VERSION_CACHE.get(tuple(command), compute, timeout + 1.0)


def runner_python_path() -> Path:
    return resolve_file_path(os.environ.get("TUV_RUNNER_PYTHON")) or venv_python(RUNNER_VENV).resolve()


def python_uv_provider(provider_type: str, python_path: Path | None, priority: int) -> UvProvider | None:
    if python_path is None or not python_path.is_file():
        return None
    try:
        python_path = python_path.resolve()
    except OSError:
        return None
    version = validate_uv_command([str(python_path), "-m", "uv"])
    return UvProvider(provider_type, None, python_path, priority, version) if version else None


def standalone_uv_provider(priority: int = 3) -> UvProvider | None:
    seen: set[str] = set()
    for candidate in (os.environ.get("TUV_SYSTEM_UV_EXE"), shutil.which("uv")):
        path = resolve_file_path(candidate)
        if path is None or path_key(path) in seen:
            continue
        seen.add(path_key(path))
        version = validate_uv_command([str(path)])
        if version:
            return UvProvider("standalone", path, None, priority, version)
    return None


def runner_uv_provider(priority: int = 4) -> UvProvider | None:
    return python_uv_provider("tuv", runner_python_path(), priority)


PROVIDER_LABELS = {"context_venv": "venv uv", "reference_python": "ref uv", "standalone": "system uv", "tuv": "tuv uv"}


def provider_label(provider: UvProvider | None) -> str:
    return "unavailable" if provider is None else PROVIDER_LABELS.get(provider.type, provider.type)


def resolve_uv_provider(context: PythonContext) -> UvProvider | None:
    """Most local usable uv first: context venv, reference interpreter, standalone, Tuv runner."""
    if context.type == "tuv":
        if provider := runner_uv_provider(priority=1):
            return provider
    elif context.is_virtual:
        if provider := python_uv_provider("context_venv", context.python_path, 1):
            return provider
    reference = context.python_path if context.type == "interpreter" else context.reference_python_path
    if reference is not None and (context.type == "interpreter" or path_key(reference) != path_key(context.python_path)):
        if provider := python_uv_provider("reference_python", reference, 2):
            return provider
    return standalone_uv_provider() or runner_uv_provider()


def refresh_context_uv_provider(context: PythonContext) -> UvProvider | None:
    context.resolved_uv_provider = resolve_uv_provider(context)
    return context.resolved_uv_provider


def uv_command(context: PythonContext, args: list[str]) -> list[str]:
    # The provider resolved at discovery time is trusted; a provider that breaks
    # mid-session surfaces as a command failure, which re-resolves it.
    provider = context.resolved_uv_provider or refresh_context_uv_provider(context)
    if provider is None:
        raise RuntimeError(
            "No uv provider is available. Tuv can install uv into the Tuv runner venv, "
            "but it will not install uv into the selected context."
        )
    return [*provider.command_prefix, *args]


# --------------------------------------------------------------------------- runner venv management

RUNNER_PROBE_CODE = """
import importlib.util, json, os, sys
def has(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False
uv_ok = False
try:
    import uv
    uv_ok = os.path.isfile(uv.find_uv_bin())
except Exception:
    pass
base = getattr(sys, "_base_executable", "") or ""
print(json.dumps({
    "base_ok": not base or os.path.exists(base),
    "pip": has("pip"), "ensurepip": has("ensurepip"), "uv": uv_ok,
    "requirements": has("packaging") and has("wcwidth"),
}))
"""


def runner_status(python_path: Path) -> dict[str, bool] | None:
    """One probe of a runner venv Python: which runner-mode ingredients are present."""
    if not python_path.is_file():
        return None
    try:
        proc = run_command([str(python_path), "-c", RUNNER_PROBE_CODE], timeout=15)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        return {key: bool(value) for key, value in data.items()} if isinstance(data, dict) else None
    except Exception:
        return None


def runner_compatibility_key(info: PythonInfo, mode: str) -> str:
    return "|".join([path_key(info.executable), info.version_text, info.implementation, info.architecture, info.os_name, mode])


def runner_hash(key: str, length: int = 8) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:length]


def read_runner_state(root: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    with contextlib.suppress(OSError):
        for line in (root / ".tuv-runner-state").read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                data[key.strip()] = value.strip()
    return data


def write_runner_state(root: Path, info: PythonInfo, mode: str, key: str, hash_value: str) -> None:
    lines = [
        f"base_python={info.executable}",
        f"base_version={info.version_text}",
        f"compat_key={key}",
        f"compat_hash={hash_value}",
        f"launcher_mode={mode}",
        f"timestamp={time.strftime('%Y%m%d%H%M%S', time.gmtime())}",
    ]
    (root / ".tuv-runner-state").write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_managed_runner_dir(root: Path) -> bool:
    try:
        resolved = root.resolve()
    except OSError:
        return False
    return resolved.parent == TUV_HOME and (resolved.name.startswith("tuv-venv-") or resolved.name == ".tuv-venv")


def compatible_runner_status(root: Path, key: str) -> dict[str, bool] | None:
    """Return the runner status when the venv matches `key` and can reach functional mode."""
    if not is_managed_runner_dir(root):
        return None
    state = read_runner_state(root)
    if state.get("compat_key") != key or not Path(state.get("base_python", "")).is_file():
        return None
    status = runner_status(venv_python(root))
    if status and status.get("base_ok") and (status.get("pip") or status.get("ensurepip")):
        return status
    return None


def runner_venv_candidates() -> list[Path]:
    candidates = [*TUV_HOME.glob("tuv-venv-*"), TUV_HOME / ".tuv-venv"]
    return [path for path in candidates if path.is_dir()]


def find_compatible_runner_venv(key: str) -> tuple[Path, dict[str, bool]] | None:
    compatible: list[tuple[str, str, Path, dict[str, bool]]] = []
    for root in runner_venv_candidates():
        status = compatible_runner_status(root, key)
        if status is not None:
            compatible.append((read_runner_state(root).get("timestamp", ""), root.name, root.resolve(), status))
    if not compatible:
        return None
    _timestamp, _name, root, status = max(compatible, key=lambda item: (item[0], item[1]))
    return root, status


def new_runner_venv_path(key: str) -> tuple[Path, str]:
    hash_value = runner_hash(key)
    if not (TUV_HOME / f"tuv-venv-{hash_value}").exists():
        return TUV_HOME / f"tuv-venv-{hash_value}", hash_value
    for counter in range(100):
        hash_value = runner_hash(f"{key}|{time.time_ns()}|{counter}")
        if not (TUV_HOME / f"tuv-venv-{hash_value}").exists():
            return TUV_HOME / f"tuv-venv-{hash_value}", hash_value
    raise RuntimeError("Could not allocate a unique Tuv runner venv path")


def gc_runner_venvs(keep_root: Path, keep_recent: int = RUNNER_VENV_KEEP) -> None:
    """Delete stale managed runner venvs, keeping the active one plus the most recent few."""
    entries: list[tuple[str, str, Path]] = []
    for root in TUV_HOME.glob("tuv-venv-*"):
        if not root.is_dir():
            continue
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved == keep_root or not read_runner_state(root).get("compat_key") or not (root / "pyvenv.cfg").is_file():
            continue
        entries.append((read_runner_state(root).get("timestamp", ""), root.name, resolved))
    entries.sort(reverse=True)
    for _timestamp, _name, root in entries[max(0, keep_recent - 1):]:
        shutil.rmtree(root, ignore_errors=True)


def requirements_hash() -> str | None:
    try:
        return hashlib.sha256((TUV_HOME / "requirements.txt").read_bytes()).hexdigest()
    except OSError:
        return None


def runner_is_ready(root: Path, status: dict[str, bool]) -> bool:
    """True when the launcher can skip its pip, uv, and requirements repair steps."""
    try:
        marker = (root / ".tuv-requirements-state").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return False
    required = status.get("pip") and status.get("uv") and status.get("requirements")
    return bool(required) and marker != "" and marker == requirements_hash()


def prepare_runner_environment(mode: str) -> tuple[PythonInfo, Path, bool]:
    if mode not in {"default", "cwd"}:
        raise RuntimeError(f"Unsupported launcher mode: {mode}")
    info = select_runner_python(mode)
    key = runner_compatibility_key(info, mode)
    with file_lock(TUV_HOME / ".tuv-runner.lock", timeout=30.0, stale_after=600.0, poll=0.2) as locked:
        found = find_compatible_runner_venv(key)
        if found is not None:
            runner, status = found
            hash_value = read_runner_state(runner).get("compat_hash", runner_hash(key))
            with contextlib.suppress(OSError):
                write_runner_state(runner, info, mode, key, hash_value)  # keeps GC from collecting it
            ready = runner_is_ready(runner, status)
        else:
            runner, hash_value = new_runner_venv_path(key)
            command = [str(info.executable), "-m", "venv", str(runner)]
            proc = run_command(command, timeout=None)
            if proc.returncode != 0:
                raise RuntimeError(failed_command_detail(command, proc))
            write_runner_state(runner, info, mode, key, hash_value)
            ready = False
        if locked:
            gc_runner_venvs(runner.resolve())
    return info, runner, ready


def print_prepare_runner(mode: str) -> int:
    try:
        info, runner, ready = prepare_runner_environment(mode)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"TUV_NEWEST_PYTHON={info.executable}")
    print(f"TUV_RUNNER_VENV={runner}")
    print(f"TUV_RUNNER_PYTHON={venv_python(runner)}")
    print(f"TUV_RUNNER_READY={int(ready)}")
    return 0


# --------------------------------------------------------------------------- contexts


def read_pyvenv_home(root: Path) -> str | None:
    with contextlib.suppress(OSError):
        for line in (root / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().lower() == "home":
                return value.strip() or None
    return None


def python_paths_from_location(location: str | Path) -> list[Path]:
    path = Path(str(location).strip().strip('"'))
    if path.is_file():
        return [path]
    names = ["python.exe", "python3.exe", "Scripts/python.exe"] if IS_WINDOWS else ["python", "python3", "bin/python", "bin/python3"]
    return [path / name for name in names]


def venv_reference_python(root: Path, info: PythonInfo) -> Path | None:
    """The base interpreter of a venv, from the probe, pyvenv.cfg home, or base prefix."""
    candidates: list[Path] = []
    if info.base_executable:
        candidates.append(Path(info.base_executable))
    if home := read_pyvenv_home(root):
        candidates += python_paths_from_location(home)
    if info.base_prefix:
        candidates += python_paths_from_location(info.base_prefix)
    own = path_key(info.executable)
    for candidate in candidates:
        probed = probe_python(candidate)
        if probed is not None and path_key(probed.executable) != own:
            return probed.executable
    return None


def interpreter_context(info: PythonInfo) -> PythonContext:
    prefix = "cwd interpreter" if info.source == "cwd" else "interpreter"
    return PythonContext(
        id=stable_context_id("interpreter", info.executable),
        type="interpreter",
        source=info.source,
        label=f"{prefix} - Python {info.version_text} - {info.executable}",
        python_path=info.executable,
        reference_python_path=info.executable,
        root_path=None,
        version=info.version_text,
    )


def venv_context(context_type: str, root: Path, label_prefix: str, source: str) -> PythonContext | None:
    if not is_venv(root):
        return None
    info = probe_python(venv_python(root))
    if info is None:
        return None
    return PythonContext(
        id=stable_context_id(context_type, root),
        type=context_type,
        source=source,
        label=f"{label_prefix} - Python {info.version_text} - {root}",
        python_path=info.executable,
        reference_python_path=venv_reference_python(root, info),
        root_path=root.resolve(),
        version=info.version_text,
    )


def unique_contexts(contexts: Iterable[PythonContext | None]) -> list[PythonContext]:
    seen: set[str] = set()
    unique: list[PythonContext] = []
    for context in contexts:
        if context is not None and context.id not in seen:
            seen.add(context.id)
            unique.append(context)
    return unique


def active_venv_spec() -> tuple[str, Path, str, str] | None:
    active = os.environ.get("VIRTUAL_ENV")
    return ("venv", Path(active), "active venv", "active") if active else None


def venv_specs(cwd: Path) -> list[tuple[str, Path, str, str]]:
    """Active venv, the cwd itself, direct child venvs, then the Tuv runner venv."""
    specs = [spec for spec in [active_venv_spec()] if spec is not None]
    if is_venv(cwd):
        specs.append(("venv", cwd, cwd.name or str(cwd), "cwd"))
    try:
        children = sorted((item for item in cwd.iterdir() if item.is_dir()), key=lambda p: p.name.lower())
    except OSError:
        children = []
    for child in children:
        try:
            managed = child.resolve() == RUNNER_VENV or (is_managed_runner_dir(child) and (child / ".tuv-runner-state").is_file())
        except OSError:
            continue
        if not managed and is_venv(child):
            specs.append(("venv", child, child.name, "scanned"))
    specs.append(("tuv", RUNNER_VENV, "tuv venv", "tuv"))
    return specs


def resolve_providers(contexts: Iterable[PythonContext]) -> None:
    unresolved = [context for context in contexts if context.resolved_uv_provider is None]
    if unresolved:
        with ThreadPoolExecutor(max_workers=min(8, len(unresolved))) as pool:
            list(pool.map(refresh_context_uv_provider, unresolved))


def discover_contexts() -> list[PythonContext]:
    """Interpreters newest first, then virtual environments, then the Tuv runner venv."""
    interpreters = [interpreter_context(info) for info in discover_python_infos()]
    specs = venv_specs(Path.cwd())
    with ThreadPoolExecutor(max_workers=min(8, len(specs))) as pool:
        venvs = list(pool.map(lambda spec: venv_context(*spec), specs))
    contexts = unique_contexts([*interpreters, *venvs])
    resolve_providers(contexts)
    return contexts


def discover_priority_contexts() -> list[PythonContext]:
    """Only the contexts that can plausibly be selected by default, so the first frame is fast."""
    cwd = Path.cwd()
    specs = [spec for spec in [active_venv_spec()] if spec is not None]
    if is_venv(cwd):
        specs.append(("venv", cwd, cwd.name or str(cwd), "cwd"))
    project = cwd / ".venv"
    if is_venv(project) and project.resolve() != RUNNER_VENV:
        specs.append(("venv", project, ".venv", "scanned"))
    contexts: list[PythonContext | None] = [venv_context(*spec) for spec in specs]
    contexts += [interpreter_context(info) for info in probe_all([(p, "cwd") for p in cwd_python_candidates(cwd)], allow_venv=False)]
    for candidate in launcher_python_candidates():
        info = probe_python(candidate)
        if info is not None and not info.is_venv:
            contexts.append(interpreter_context(info))
    contexts.append(venv_context("tuv", RUNNER_VENV, "tuv venv", "tuv"))
    unique = unique_contexts(contexts)
    if unique:
        preferred = next(
            (c for c in unique if c.type == "venv" and (c.source == "active" or (c.root_path and c.root_path.name == ".venv"))),
            unique[0],
        )
        refresh_context_uv_provider(preferred)
    return unique


# --------------------------------------------------------------------------- uv package data


def run_uv_json(context: PythonContext, args: list[str], timeout: float | None = 90.0) -> object:
    command = uv_command(context, args)
    try:
        proc = run_command(command, timeout=timeout)
    except FileNotFoundError:
        # The provider executable vanished; drop caches so the next attempt re-resolves.
        context.resolved_uv_provider = None
        _UV_VERSION_CACHE.clear()
        raise
    if proc.returncode != 0:
        raise RuntimeError(failed_command_detail(command, proc))
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse uv JSON output: {exc}\n\n{proc.stdout}") from exc


def build_package_rows(context: PythonContext, updated_names: set[str]) -> list[PackageRow]:
    data = run_uv_json(context, ["pip", "list", "--python", context.uv_target, "--format", "json"])
    if not isinstance(data, list):
        raise RuntimeError("uv returned unexpected installed package data")
    rows = [
        PackageRow(
            name=canonicalize_name(str(item["name"])),
            display_name=str(item["name"]),
            installed_version=str(item["version"]),
            updated_in_session=canonicalize_name(str(item["name"])) in updated_names,
        )
        for item in data
        if isinstance(item, dict) and "name" in item and "version" in item
    ]
    rows.sort(key=lambda row: row.name)
    return rows


def load_outdated_targets(context: PythonContext) -> tuple[dict[str, str], str | None]:
    """Latest versions reported by `uv pip list --outdated`, plus a warning when unavailable."""
    try:
        data = run_uv_json(context, ["pip", "list", "--python", context.uv_target, "--outdated", "--format", "json"])
    except Exception as exc:
        return {}, f"Outdated data unavailable: {last_lines(str(exc), 2)}"
    if not isinstance(data, list):
        return {}, "Outdated data unavailable: uv returned unexpected data"
    targets: dict[str, str] = {}
    for item in data:
        if isinstance(item, dict) and "name" in item:
            latest = item.get("latest_version") or item.get("latest") or item.get("latest-version")
            if latest:
                targets[canonicalize_name(str(item["name"]))] = str(latest)
    return targets, None


METADATA_PROBE_CODE = r"""
import email.parser, importlib.metadata as metadata, json, os, pathlib, platform, re, sys

def norm(value):
    return re.sub(r"[-_.]+", "-", value).lower()

result = {}

def put(name, summary, requires, extras):
    if not name:
        return
    current = result.setdefault(norm(str(name)), {"name": name, "summary": "", "requires": [], "extras": []})
    if summary and not current["summary"]:
        current["summary"] = str(summary)
    current["requires"].extend(str(req) for req in (requires or []) if req)
    current["extras"].extend(str(extra) for extra in (extras or []) if extra)

try:
    for dist in metadata.distributions():
        meta = dist.metadata
        put(meta.get("Name"), meta.get("Summary"), list(dist.requires or meta.get_all("Requires-Dist") or []), meta.get_all("Provides-Extra") or [])
except Exception:
    pass

if not result:
    for base in list(sys.path):
        try:
            for meta_path in pathlib.Path(base).glob("*.dist-info/METADATA"):
                try:
                    msg = email.parser.Parser().parsestr(meta_path.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                put(msg.get("Name"), msg.get("Summary"), msg.get_all("Requires-Dist") or [], msg.get_all("Provides-Extra") or [])
        except Exception:
            continue

for value in result.values():
    value["requires"] = sorted(set(value["requires"]))
    value["extras"] = sorted(set(value["extras"]))

impl = sys.implementation.version
suffix = "" if impl.releaselevel == "final" else impl.releaselevel[0] + str(impl.serial)
env = {
    "implementation_name": sys.implementation.name,
    "implementation_version": f"{impl.major}.{impl.minor}.{impl.micro}{suffix}",
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_python_implementation": platform.python_implementation(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "python_version": f"{sys.version_info[0]}.{sys.version_info[1]}",
    "sys_platform": sys.platform,
    "extra": "",
}
print(json.dumps({"packages": result, "environment": env}))
"""


def installed_package_metadata(context: PythonContext) -> dict[str, PackageMetadata]:
    """Summary, dependencies, and extras of every distribution installed in the context."""
    command = [str(context.python_path), "-c", METADATA_PROBE_CODE]
    try:
        proc = run_command(command, timeout=30)
    except Exception as exc:
        raise RuntimeError(f"Could not collect package metadata: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(failed_command_detail(command, proc))
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"Could not parse package metadata JSON: {exc}") from exc
    packages = payload.get("packages") if isinstance(payload, dict) else None
    if not isinstance(packages, dict):
        raise RuntimeError("Package metadata probe returned unexpected package data")
    marker_env = payload.get("environment")
    marker_env = marker_env if isinstance(marker_env, dict) else {}
    result: dict[str, PackageMetadata] = {}
    for package_name, item in packages.items():
        if not isinstance(package_name, str) or not isinstance(item, dict):
            continue
        requires = [req for req in item.get("requires", []) if isinstance(req, str)]
        extras = {str(extra).strip() for extra in item.get("extras", []) if str(extra).strip()}
        for req in requires:
            extras |= extra_names_from_requirement(req)
        deps = {dep for req in requires if (dep := dependency_name_from_requirement(req, marker_env, extras))}
        result[canonicalize_name(package_name)] = PackageMetadata(
            description=short_description(item.get("summary")),
            dependencies=deps,
            extras={canonicalize_name(extra) for extra in extras},
        )
    return result


def dependency_name_from_requirement(requirement: str, marker_env: dict[str, object], extras: set[str] | None = None) -> str | None:
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement:
        head = re.split(r"[<>=!~;\[\s(]", requirement, maxsplit=1)[0].strip()
        return canonicalize_name(head) if head else None
    if parsed.marker is not None and not requirement_marker_applies(parsed.marker, marker_env, extras or set()):
        return None
    return canonicalize_name(parsed.name)


def requirement_marker_applies(marker: Marker, marker_env: dict[str, object], extras: set[str]) -> bool:
    """Installed metadata does not record requested extras, so any declared extra counts."""
    env = dict(default_environment())
    env.update({str(key): str(value) for key, value in marker_env.items()})
    try:
        for extra in ["", *extra_marker_values(extras)]:
            env["extra"] = extra
            if marker.evaluate(environment=env):
                return True
    except Exception:
        return True
    return False


def extra_marker_values(extras: set[str]) -> list[str]:
    values: set[str] = set()
    for extra in extras:
        raw = str(extra).strip()
        if raw:
            normalized = canonicalize_name(raw)
            values |= {raw, normalized, normalized.replace("-", "_")}
    return sorted(values)


def extra_names_from_requirement(requirement: str) -> set[str]:
    with contextlib.suppress(InvalidRequirement):
        parsed = Requirement(requirement)
        if parsed.marker is not None:
            requirement = str(parsed.marker)
    values: set[str] = set()
    for match in re.finditer(r"\bextra\s*(?:==|!=|in|not\s+in)\s*(['\"])(.*?)\1", requirement, re.IGNORECASE):
        values |= {value.strip() for value in re.split(r"[,\s]+", match.group(2)) if value.strip()}
    return values


def short_description(value: object) -> str | None:
    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        return None
    return text if len(text) <= 180 else text[:177].rstrip() + "..."


def load_dependency_info(context: PythonContext, display_by_name: dict[str, str]) -> DependencyInfo:
    """Relationships among the displayed packages; raises when metadata does not cover them all."""
    installed = set(display_by_name)
    metadata = installed_package_metadata(context)
    if installed - set(metadata):
        missing = sorted(installed - set(metadata))
        raise RuntimeError(f"Package metadata did not match the displayed package list; missing metadata for {', '.join(missing[:8])}")
    deps = {name: {dep for dep in metadata[name].dependencies if dep in installed} for name in installed}
    usage: dict[str, set[str]] = {name: set() for name in installed}
    for name, names in deps.items():
        for dep in names:
            usage[dep].add(name)
    display = lambda name: display_by_name.get(name, name)  # noqa: E731
    return DependencyInfo(
        safe_names={name for name, users in usage.items() if not users},
        dependencies={name: [display(dep) for dep in sorted(names)] for name, names in deps.items()},
        usage={name: [display(user) for user in sorted(users)] for name, users in usage.items()},
        descriptions={name: metadata[name].description for name in installed},
    )


# --------------------------------------------------------------------------- effective uv index


class SimpleRepositoryLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, bool, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attributes = {name.lower(): value for name, value in attrs}
        href = attributes.get("href")
        if href:
            self.links.append((href, "data-yanked" in attributes, attributes.get("data-requires-python") or None))


def read_uv_config_table(path: Path) -> dict[str, object]:
    if tomllib is None:
        raise RuntimeError(f"Cannot parse uv configuration on this Python version: {path}")
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception as exc:
        raise RuntimeError(f"Could not parse uv configuration {path}: {exc}") from exc
    if path.name == "pyproject.toml":
        tool = data.get("tool") if isinstance(data, dict) else None
        table = tool.get("uv") if isinstance(tool, dict) else None
        return dict(table) if isinstance(table, dict) else {}
    return dict(data) if isinstance(data, dict) else {}


def discover_project_uv_config() -> tuple[Path, dict[str, object]] | None:
    for directory in [Path.cwd(), *Path.cwd().parents]:
        if (directory / "uv.toml").is_file():
            return directory / "uv.toml", read_uv_config_table(directory / "uv.toml")
        if (directory / "pyproject.toml").is_file():
            table = read_uv_config_table(directory / "pyproject.toml")
            if table:
                return directory / "pyproject.toml", table
    return None


def user_uv_config_path() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "uv" / "uv.toml"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "uv" / "uv.toml"


def system_uv_config_paths() -> list[Path]:
    if IS_WINDOWS:
        base = os.environ.get("PROGRAMDATA")
        return [Path(base) / "uv" / "uv.toml"] if base else []
    xdg_dirs = [Path(value) for value in os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(os.pathsep) if value]
    return [*(path / "uv" / "uv.toml" for path in xdg_dirs), Path("/etc/uv/uv.toml")]


def uv_config_tables() -> list[tuple[Path, dict[str, object]]]:
    """Configuration tables in uv precedence order: project, user, then the first system file."""
    if environment_flag("UV_NO_CONFIG"):
        return []
    if explicit := os.environ.get("UV_CONFIG_FILE"):
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"UV_CONFIG_FILE does not exist: {path}")
        return [(path, read_uv_config_table(path))]
    tables = [project] if (project := discover_project_uv_config()) else []
    if (user := user_uv_config_path()).is_file():
        tables.append((user, read_uv_config_table(user)))
    system = next((path for path in system_uv_config_paths() if path.is_file()), None)
    if system is not None:
        tables.append((system, read_uv_config_table(system)))
    return tables


def index_entry_from_value(value: object) -> IndexEntry | None:
    if not isinstance(value, dict) or not isinstance(value.get("url"), str) or not value["url"].strip():
        return None
    raw_codes = value.get("ignore-error-codes", [])
    codes = tuple(int(code) for code in raw_codes if str(code).isdigit()) if isinstance(raw_codes, list) else ()
    return IndexEntry(
        name=str(value["name"]).strip() if value.get("name") else None,
        url=value["url"].strip(),
        explicit=value.get("explicit") is True,
        default=value.get("default") is True,
        authenticate=str(value.get("authenticate", "auto")),
        ignore_error_codes=codes,
    )


def split_index_values(value: object) -> list[IndexEntry]:
    """Parse `UV_INDEX`-style values: whitespace-separated `[name=]url` items, or a list of them."""
    if isinstance(value, list):
        return [entry for item in value if isinstance(item, str) for entry in split_index_values(item)]
    entries: list[IndexEntry] = []
    for part in (value or "").split() if isinstance(value, str) else []:
        name, sep, url = part.partition("=")
        if sep and re.fullmatch(r"[A-Za-z0-9._-]+", name) and "://" in url:
            entries.append(IndexEntry(name, url))
        else:
            entries.append(IndexEntry(None, part))
    return entries


def table_setting(tables: list[tuple[Path, dict[str, object]]], key: str, include_pip: bool = True) -> object | None:
    if include_pip:
        for _path, table in tables:
            pip_table = table.get("pip")
            if isinstance(pip_table, dict) and key in pip_table:
                return pip_table[key]
    return next((table[key] for _path, table in tables if key in table), None)


def marker_matches_context(marker_text: str, context: PythonContext) -> bool:
    environment = dict(default_environment())
    environment["python_full_version"] = context.version
    environment["python_version"] = ".".join(context.version.split(".")[:2])
    try:
        return bool(Marker(marker_text).evaluate(environment=environment))
    except Exception:
        return False


def source_index_name(source: object, context: PythonContext) -> str | None:
    for candidate in source if isinstance(source, list) else [source]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("index"), str):
            continue
        marker = candidate.get("marker")
        if marker is None or (isinstance(marker, str) and marker_matches_context(marker, context)):
            return str(candidate["index"])
    return None


def effective_index_config(package_name: str, context: PythonContext) -> EffectiveIndexConfig:
    """The indexes uv would consult for `package_name`, in order, with the configured strategy."""
    if environment_flag("UV_NO_INDEX") or environment_flag("PIP_NO_INDEX"):
        raise RuntimeError("uv index access is disabled by UV_NO_INDEX")
    if os.environ.get("UV_FIND_LINKS") or os.environ.get("PIP_FIND_LINKS"):
        raise RuntimeError("Full version selection is unavailable when UV_FIND_LINKS is configured")
    tables = uv_config_tables()
    if table_setting(tables, "no-index") is True:
        raise RuntimeError("uv index access is disabled by configuration")
    if table_setting(tables, "find-links") not in (None, False, ""):
        raise RuntimeError("Full version selection is unavailable with uv find-links configuration")
    if table_setting(tables, "exclude-newer", include_pip=False) not in (None, False):
        raise RuntimeError("Full version selection is unavailable with uv exclude-newer configuration")

    configured = split_index_values(os.environ.get("UV_INDEX"))
    configured += split_index_values(os.environ.get("UV_EXTRA_INDEX_URL") or os.environ.get("PIP_EXTRA_INDEX_URL"))
    for _path, table in tables:
        raw_indexes = table.get("index", [])
        configured += [entry for item in (raw_indexes if isinstance(raw_indexes, list) else []) if (entry := index_entry_from_value(item))]
        pip_table = table.get("pip")
        if isinstance(pip_table, dict):
            configured += split_index_values(pip_table.get("extra-index-url"))

    project_table = tables[0][1] if tables and tables[0][0].name == "pyproject.toml" else {}
    raw_project_indexes = project_table.get("index", [])
    project_indexes = [e for item in (raw_project_indexes if isinstance(raw_project_indexes, list) else []) if (e := index_entry_from_value(item))]
    sources = project_table.get("sources")
    source = None
    if isinstance(sources, dict):
        wanted = canonicalize_name(package_name)
        source = next((value for name, value in sources.items() if canonicalize_name(str(name)) == wanted), None)
    pinned_index = source_index_name(source, context) if source is not None else None
    if source is not None and pinned_index is None:
        raise RuntimeError(f"{package_name} uses a non-index or unsupported marker-based uv source")

    default_entry = next((entry for entry in configured if entry.default), None)
    if default_url := os.environ.get("UV_DEFAULT_INDEX") or os.environ.get("UV_INDEX_URL") or os.environ.get("PIP_INDEX_URL"):
        default_entry = IndexEntry(None, default_url, default=True)
    if default_entry is None:
        pip_tables = [table["pip"] for _path, table in tables if isinstance(table.get("pip"), dict)]
        candidate = (
            next((pip["index-url"] for pip in pip_tables if isinstance(pip.get("index-url"), str)), None)
            or table_setting(tables, "default-index", include_pip=False)
            or table_setting(tables, "index-url", include_pip=False)
        )
        if isinstance(candidate, str) and candidate.strip():
            default_entry = IndexEntry(None, candidate.strip(), default=True)
    if default_entry is None:
        default_entry = IndexEntry("pypi", "https://pypi.org/simple/", default=True)

    if pinned_index is not None:
        # uv source mappings may reference only indexes declared in the same
        # project pyproject.toml, never a same-named environment/user index.
        pinned = next((entry for entry in project_indexes if entry.name == pinned_index), None)
        if pinned is None:
            raise RuntimeError(f"Package source references unknown uv index: {pinned_index}")
        entries = [pinned]
    else:
        entries = [entry for entry in configured if not entry.default and not entry.explicit]
        if not default_entry.explicit:
            entries.append(default_entry)
    deduped: dict[str, IndexEntry] = {}
    for entry in entries:
        deduped.setdefault(redact_url(entry.url), entry)
    if not deduped:
        raise RuntimeError(f"No configured uv index is eligible for {package_name}")

    configured_strategy = table_setting(tables, "index-strategy")
    strategy = os.environ.get("UV_INDEX_STRATEGY") or (configured_strategy if isinstance(configured_strategy, str) else "") or "first-index"
    if strategy not in {"first-index", "unsafe-first-match", "unsafe-best-match"}:
        raise RuntimeError(f"Unsupported uv index strategy: {strategy}")
    return EffectiveIndexConfig(tuple(deduped.values()), strategy)


def simple_project_url(index_url: str, package_name: str) -> str:
    parts = urllib.parse.urlsplit(index_url)
    path = parts.path.rstrip("/") + "/" + urllib.parse.quote(canonicalize_name(package_name), safe="") + "/"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def redact_url(url: str) -> str:
    """Drop credentials and the query string; both commonly carry secrets."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "") + (f":{parsed.port}" if parsed.port else "")
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def redact_secrets(text: str, url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    secrets = [parsed.username, parsed.password, *(value for _name, value in urllib.parse.parse_qsl(parsed.query))]
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***").replace(urllib.parse.unquote(secret), "***")
    return text


_INDEX_AUTH_CACHE: dict[tuple[str, str | None], str | None] = {}
_INDEX_AUTH_LOCK = threading.Lock()


def uv_store_token(context: PythonContext, host: str, username: str | None) -> str | None:
    """Ask `uv auth token` once per host/user; failures are cached as None."""
    cache_key = (host, username)
    with _INDEX_AUTH_LOCK:
        if cache_key in _INDEX_AUTH_CACHE:
            return _INDEX_AUTH_CACHE[cache_key]
    token: str | None = None
    try:
        args = ["auth", "token", *(["--username", username] if username else []), host]
        proc = run_command(uv_command(context, args), timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            token = proc.stdout.strip().splitlines()[-1]
    except Exception:
        token = None
    with _INDEX_AUTH_LOCK:
        _INDEX_AUTH_CACHE[cache_key] = token
    return token


def index_authorization(entry: IndexEntry, context: PythonContext, search_uv_store: bool = False) -> tuple[str, str | None]:
    """Return the credential-free URL and an Authorization header value, if any."""
    parsed = urllib.parse.urlsplit(entry.url)
    host = parsed.hostname or ""
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    clean_url = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    username = urllib.parse.unquote(parsed.username or "") or None
    password = urllib.parse.unquote(parsed.password or "") or None
    if env_prefix := re.sub(r"[^A-Za-z0-9]", "_", (entry.name or "").upper()):
        username = os.environ.get(f"UV_INDEX_{env_prefix}_USERNAME") or username
        password = os.environ.get(f"UV_INDEX_{env_prefix}_PASSWORD") or password
    if entry.authenticate == "never":
        if username or password:
            raise RuntimeError(f"Credentials are forbidden for index {entry.name or redact_url(entry.url)}")
        return clean_url, None
    if not username and not password and host:
        try:
            auth = netrc.netrc(os.environ.get("NETRC")).authenticators(host)
        except (OSError, netrc.NetrcParseError):
            auth = None
        if auth:
            username, _account, password = auth
    if not password and host and (username is not None or entry.authenticate == "always" or search_uv_store):
        password = uv_store_token(context, host, username)
    if password:
        encoded = base64.b64encode(f"{username or '__token__'}:{password}".encode("utf-8")).decode("ascii")
        return clean_url, f"Basic {encoded}"
    if entry.authenticate == "always":
        raise RuntimeError(f"Authentication is required for index {entry.name or redact_url(entry.url)}")
    return clean_url, None


def version_from_distribution_filename(filename: str, package_name: str) -> str | None:
    base = Path(urllib.parse.unquote(urllib.parse.urlparse(filename).path)).name
    if not base or base.endswith(".metadata"):
        return None
    wanted = canonicalize_name(package_name)
    for parser in (parse_wheel_filename, parse_sdist_filename):
        try:
            name, version = parser(base)[:2]
        except (InvalidWheelFilename, InvalidSdistFilename, ValueError):
            continue
        if canonicalize_name(str(name)) == wanted:
            return str(version)
    return None


def requires_python_allows(specifier: object, python_version: str) -> bool:
    if specifier is None or not str(specifier).strip():
        return True
    try:
        return bool(SpecifierSet(str(specifier)).contains(python_version, prereleases=True))
    except (InvalidSpecifier, ValueError):
        return False


def collect_versions(files: Iterable[tuple[str, bool, object]], package_name: str, python_version: str) -> tuple[set[str], set[str]]:
    """(versions, versions whose every file is yanked) from (filename, yanked, requires-python) items."""
    versions: set[str] = set()
    yanked: set[str] = set()
    available: set[str] = set()
    for filename, is_yanked, requires_python in files:
        version = version_from_distribution_filename(filename, package_name)
        if not version or (python_version and not requires_python_allows(requires_python, python_version)):
            continue
        versions.add(version)
        (yanked if is_yanked else available).add(version)
    return versions, yanked - available


def versions_from_simple_json(data: object, package_name: str, python_version: str = "") -> tuple[set[str], set[str]]:
    if not isinstance(data, dict):
        return set(), set()
    files = data.get("files")
    if not isinstance(files, list):
        raw_versions = data.get("versions")
        versions = {str(v) for v in raw_versions if isinstance(v, str)} if isinstance(raw_versions, list) else set()
        return versions, set()
    items = (
        (item["filename"] if isinstance(item.get("filename"), str) else item.get("url"), bool(item.get("yanked")), item.get("requires-python"))
        for item in files
        if isinstance(item, dict)
    )
    return collect_versions(((f, y, r) for f, y, r in items if isinstance(f, str)), package_name, python_version)


def versions_from_simple_html(text: str, package_name: str, python_version: str = "") -> tuple[set[str], set[str]]:
    parser = SimpleRepositoryLinkParser()
    parser.feed(text)
    return collect_versions(parser.links, package_name, python_version)


MAX_INDEX_RESPONSE = 20 * 1024 * 1024


def fetch_versions_from_index(entry: IndexEntry, package_name: str, context: PythonContext, timeout: float) -> tuple[bool, set[str], set[str]]:
    """(project found, versions, yanked-only versions) from one Simple Repository API index."""
    url = ""
    for attempt in range(2):
        index_url, authorization = index_authorization(entry, context, search_uv_store=attempt > 0)
        url = simple_project_url(index_url, package_name)
        headers = {
            "Accept": "application/vnd.pypi.simple.v1+json, text/html;q=0.2",
            "Accept-Encoding": "identity",
            "User-Agent": f"tuv/{TUV_VERSION}",
        }
        if authorization:
            headers["Authorization"] = authorization
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                body_bytes = response.read(MAX_INDEX_RESPONSE + 1)
            if len(body_bytes) > MAX_INDEX_RESPONSE:
                raise RuntimeError("Simple-index response exceeded 20 MiB")
            body = body_bytes.decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or exc.code in entry.ignore_error_codes:
                return False, set(), set()
            if attempt == 0 and exc.code in {401, 403} and entry.authenticate != "never":
                continue
            raise RuntimeError(f"{redact_url(url)} returned HTTP {exc.code}") from exc
        except Exception as exc:
            raise RuntimeError(f"{redact_url(url)}: {redact_secrets(sanitize_terminal_text(exc), entry.url)}") from exc
    if "json" in content_type or body.lstrip().startswith("{"):
        found, yanked = versions_from_simple_json(json.loads(body), package_name, context.version)
    else:
        found, yanked = versions_from_simple_html(body, package_name, context.version)
    return True, found, yanked


_VERSION_LOOKUP_CACHE: SingleFlight = SingleFlight(ttl=600.0)


def fetch_available_versions(package_name: str, context: PythonContext, timeout: float = 12.0) -> tuple[list[str], set[str]]:
    """All installable versions (PEP 440 order) and yanked-only versions, per uv's index strategy."""
    config = effective_index_config(package_name, context)
    cache_key = (
        canonicalize_name(package_name),
        context.version,
        config.strategy,
        tuple((entry.name, redact_url(entry.url), entry.explicit, entry.default) for entry in config.entries),
    )

    def compute() -> tuple[list[str], set[str]]:
        versions: set[str] = set()
        yanked: set[str] = set()
        available: set[str] = set()
        project_found = False
        for entry in config.entries:
            try:
                found_project, found, found_yanked = fetch_versions_from_index(entry, package_name, context, timeout)
            except Exception as exc:
                raise RuntimeError(f"Version lookup failed: {sanitize_terminal_text(exc)}") from exc
            if not found_project:
                continue
            project_found = True
            versions |= found
            yanked |= found_yanked
            available |= found - found_yanked
            if config.strategy == "first-index" or (config.strategy == "unsafe-first-match" and found):
                break
        if not versions and not project_found:
            raise RuntimeError(f"Package {package_name} was not found on the configured uv indexes")
        return sorted(versions, key=version_key), yanked - available

    result = _VERSION_LOOKUP_CACHE.get(cache_key, compute, timeout * max(1, len(config.entries)) + 2.0, retry=True)
    assert result is not None
    return list(result[0]), set(result[1])


# --------------------------------------------------------------------------- pins


def pins_file_path() -> Path:
    return Path.home() / ".tuv" / "pins.json"


def parse_pins_data(data: object) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    if isinstance(data, dict):
        for key, values in data.items():
            if isinstance(key, str) and isinstance(values, list):
                names = {canonicalize_name(str(value)) for value in values if str(value).strip()}
                if names:
                    result[key] = names
    return result


def read_pins_file(quarantine_malformed: bool = False) -> dict[str, set[str]]:
    path = pins_file_path()
    try:
        return parse_pins_data(json.loads(path.read_text(encoding="utf-8")))
    except ValueError:  # malformed JSON or undecodable text
        if quarantine_malformed:
            corrupt = path.with_name(f"pins.corrupt-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}.json")
            with contextlib.suppress(OSError):
                os.replace(path, corrupt)
        return {}
    except OSError:
        return {}


def pins_lock(timeout: float) -> contextlib.AbstractContextManager[bool]:
    return file_lock(pins_file_path().with_suffix(".lock"), timeout=timeout, stale_after=30.0, poll=0.02)


def load_pins() -> dict[str, set[str]]:
    with pins_lock(timeout=0.5) as locked:
        return read_pins_file(quarantine_malformed=locked)


def update_pinned_package(context_id: str, package_name: str, pinned: bool) -> tuple[dict[str, set[str]], str | None]:
    """Merge one pin change into the on-disk state under the lock; write atomically."""
    with pins_lock(timeout=3.0) as locked:
        if not locked:
            return read_pins_file(), "Could not lock the pin state file; another Tuv process may be updating it"
        path = pins_file_path()
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            pins = read_pins_file(quarantine_malformed=True)
            names = pins.setdefault(context_id, set())
            (names.add if pinned else names.discard)(canonicalize_name(package_name))
            if not names:
                pins.pop(context_id, None)
            with open(temp_path, "x", encoding="utf-8", newline="\n") as handle:
                json.dump({key: sorted(values) for key, values in pins.items()}, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            return pins, None
        except OSError as exc:
            return read_pins_file(), f"Could not save pins: {exc}"
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()


# --------------------------------------------------------------------------- text layout

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# CSI, OSC (BEL or ST terminated), DCS/SOS/PM/APC (ST terminated), then any other ESC+char.
ESCAPE_SEQUENCE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[PX^_].*?(?:\x1b\\|$)|.?)",
    re.DOTALL,
)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def sanitize_terminal_text(value: object) -> str:
    """Strip escape sequences, bidi overrides, and control characters from untrusted text."""
    text = ESCAPE_SEQUENCE_RE.sub("", str(value))
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return CONTROL_RE.sub("", text)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def char_display_width(ch: str) -> int:
    return max(0, wcwidth(ch))


def display_width(text: str) -> int:
    if text.isascii():
        return len(text)
    return sum(char_display_width(ch) for ch in text)


def truncate(value: object, width: int) -> str:
    """Fit text in `width` cells; a trailing `~` marks truncation."""
    text = sanitize_terminal_text(value)
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return text[:1] if text and char_display_width(text[0]) <= 1 else "~"
    out: list[str] = []
    used = 0
    for ch in text:
        ch_width = char_display_width(ch)
        if used + ch_width > width - 1:
            break
        out.append(ch)
        used += ch_width
    return "".join(out) + "~"


def pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def fit(text: object, width: int) -> str:
    return pad_display(truncate(text, width), width)


def slice_display(text: str, start: int, width: int) -> str:
    """Cells `start` to `start + width` of `text`, never splitting a wide character."""
    if width <= 0:
        return ""
    end = start + width
    position = 0
    output: list[str] = []
    for ch in text:
        ch_width = char_display_width(ch)
        if ch_width == 0:
            if output and start <= position <= end:
                output.append(ch)
            continue
        if position + ch_width > end:
            break
        if position >= start:
            output.append(ch)
        position += ch_width
    return "".join(output)


def paste_box(lines: list[str], box: list[str], top: int, left: int, width: int) -> list[str]:
    """Overlay `box` rows onto `lines`, keeping the dimmed backdrop outside the box."""
    result = list(lines)
    for offset, box_line in enumerate(box):
        row_index = top + offset
        if row_index >= len(result):
            break
        dimmed = result[row_index].startswith(MODAL_BACKDROP)
        raw = fit(strip_ansi(result[row_index]), width)
        prefix = slice_display(raw, 0, left)
        suffix_start = left + display_width(strip_ansi(box_line))
        suffix = slice_display(raw, suffix_start, max(0, width - suffix_start))
        if dimmed:
            result[row_index] = MODAL_BACKDROP + prefix + RESET + box_line + MODAL_BACKDROP + suffix + RESET
        else:
            result[row_index] = prefix + box_line + suffix
    return result


def box_border(width: int, kind: str = "top", title: str | None = None) -> str:
    if kind == "bottom":
        return "└" + "─" * (width - 2) + "┘"
    inner = max(0, width - 2)
    if not title or inner <= 0:
        return "┌" + "─" * inner + "┐"
    title_text = truncate(title, max(0, inner - 3))
    label = truncate(f" {title_text} " if title_text else " ", inner)
    return "┌" + label + "─" * (inner - display_width(label)) + "┐"


def box_line(text: str, width: int) -> str:
    return "│" + fit(" " + text, width - 2) + "│"


# --------------------------------------------------------------------------- terminal and keys

WINDOWS_SPECIAL_KEYS = {
    "H": "up", "P": "down", "K": "left", "M": "right", "I": "pageup", "Q": "pagedown",
    "G": "home", "O": "end", "S": "delete",
    "<": "f2", "=": "f3", ">": "f4", "?": "f5", "@": "f6", "C": "f9", "D": "f10",
}

ESCAPE_KEYS = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
    "\x1b[5~": "pageup", "\x1b[6~": "pagedown",
    "\x1b[H": "home", "\x1b[F": "end", "\x1b[1~": "home", "\x1b[4~": "end", "\x1b[3~": "delete",
    "\x1b[12~": "f2", "\x1bOQ": "f2", "\x1b[13~": "f3", "\x1bOR": "f3", "\x1b[14~": "f4", "\x1bOS": "f4",
    "\x1b[15~": "f5", "\x1b[17~": "f6", "\x1b[20~": "f9", "\x1b[21~": "f10", "\x1b[24~": "f12",
}
ESCAPE_SEQUENCES = {sequence.encode("ascii"): key for sequence, key in ESCAPE_KEYS.items()}
ESC_AMBIGUITY_WINDOW = 0.03


def normalize_key(data: str) -> str | None:
    if data in ESCAPE_KEYS:
        return ESCAPE_KEYS[data]
    if data in ("\r", "\n"):
        return "enter"
    if data == "\x1b":
        return "esc"
    if data in ("\x03", "\x04"):
        return "quit"
    if data in ("\x7f", "\x08"):
        return "backspace"
    if len(data) == 1 and data.isprintable():
        # Printable characters pass through unchanged so text-input modes can
        # consume them; command dispatch matches them case-insensitively.
        return data
    return None


def decode_key(buffer: bytes, more_may_follow: bool) -> tuple[str | None, int]:
    """Decode one key from raw input bytes: (key, bytes consumed).

    (None, 0) asks the caller to wait for more bytes; (None, n) drops n bytes.
    """
    if not buffer:
        return None, 0
    if buffer[0] == 0x1B:
        for sequence, key in ESCAPE_SEQUENCES.items():
            if buffer.startswith(sequence):
                return key, len(sequence)
        if more_may_follow and any(sequence.startswith(buffer) for sequence in ESCAPE_SEQUENCES):
            return None, 0
        if buffer[1:2] in (b"[", b"O"):
            # Unknown CSI/SS3 sequence: drop it whole (final byte 0x40-0x7E).
            for index in range(2, len(buffer)):
                if 0x40 <= buffer[index] <= 0x7E:
                    return None, index + 1
            return (None, 0) if more_may_follow else (None, len(buffer))
        return "esc", 1
    lead = buffer[0]
    length = 4 if lead >= 0xF0 else 3 if lead >= 0xE0 else 2 if lead >= 0xC0 else 1
    if len(buffer) < length:
        return (None, 0) if more_may_follow else (None, len(buffer))
    try:
        return normalize_key(buffer[:length].decode("utf-8")), length
    except UnicodeDecodeError:
        return None, 1


class Terminal:
    """Alternate-screen lifecycle, raw input, and a wake-up channel for worker threads."""

    def __init__(self) -> None:
        self.fd: int | None = None
        self.old_termios: object | None = None
        self._buffer = b""
        self._eof = False
        self._wake_r = self._wake_w = -1
        self._stdin_handle = self._wake_event = self._ctrl_handler = None

    def __enter__(self) -> "Terminal":
        if hasattr(sys.stdout, "reconfigure"):
            with contextlib.suppress(Exception):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if IS_WINDOWS:
            kernel32 = ctypes.windll.kernel32
            for name in ("GetStdHandle", "CreateEventW"):
                getattr(kernel32, name).restype = ctypes.c_void_p
            kernel32.SetEvent.argtypes = kernel32.CloseHandle.argtypes = kernel32.FlushConsoleInputBuffer.argtypes = [ctypes.c_void_p]
            kernel32.WaitForMultipleObjects.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
                kernel32.SetConsoleMode(ctypes.c_void_p(handle), mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            self._stdin_handle = kernel32.GetStdHandle(-10)
            self._wake_event = kernel32.CreateEventW(None, False, False, None)
            # Ctrl+C arrives on another thread while the main thread sits in the console
            # wait; waking it lets Python's own SIGINT handler run right away.
            self._ctrl_handler = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint32)(lambda _event: self.wake() or 0)
            kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True)
        else:
            import termios
            import tty

            self.fd = sys.stdin.fileno()
            self.old_termios = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self._wake_r, self._wake_w = os.pipe()
            os.set_blocking(self._wake_r, False)
            os.set_blocking(self._wake_w, False)
        self.write(ALT_ON + HIDE_CURSOR + CLEAR + HOME)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.write(RESET + SHOW_CURSOR + CLEAR + HOME + ALT_OFF)
        if not IS_WINDOWS and self.fd is not None and self.old_termios is not None:
            import termios

            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_termios)
            os.close(self._wake_r)
            os.close(self._wake_w)
        elif self._wake_event is not None:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_handler, False)
            ctypes.windll.kernel32.CloseHandle(self._wake_event)

    def write(self, text: str) -> None:
        sys.stdout.write(text.replace("\a", ""))
        sys.stdout.flush()

    def size(self) -> tuple[int, int]:
        size = os.get_terminal_size()
        return size.columns, size.lines

    def wake(self) -> None:
        """Interrupt a pending read_key; safe to call from any thread."""
        if IS_WINDOWS:
            if self._wake_event is not None:
                ctypes.windll.kernel32.SetEvent(self._wake_event)
        elif self._wake_w >= 0:
            with contextlib.suppress(OSError):
                os.write(self._wake_w, b"\0")

    def read_key(self, timeout: float) -> str | None:
        return self._read_windows_key(timeout) if IS_WINDOWS else self._read_posix_key(timeout)

    # POSIX: bytes are buffered so bursts of keys and multi-byte characters survive.

    def _fill(self, timeout: float, ignore_wake: bool = False) -> bool:
        """Wait up to `timeout` for stdin bytes. A worker wake-up ends the wait unless `ignore_wake`."""
        import select

        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self.fd, self._wake_r], [], [], remaining)
            if self._wake_r in ready:
                with contextlib.suppress(OSError):
                    os.read(self._wake_r, 4096)
            if self.fd in ready:
                data = os.read(self.fd, 4096)
                if not data:
                    self._eof = True  # stdin hung up; read_key reports quit once the buffer drains
                    return False
                self._buffer += data
                return True
            if not ready or remaining <= 0 or not ignore_wake:
                return False

    def _read_posix_key(self, timeout: float) -> str | None:
        if not self._buffer and not self._fill(timeout):
            return "quit" if self._eof else None
        while self._buffer:
            key, consumed = decode_key(self._buffer, more_may_follow=True)
            if consumed == 0:
                # A bare Esc (or a partial sequence): wait briefly for the rest.
                if self._fill(ESC_AMBIGUITY_WINDOW, ignore_wake=True):
                    continue
                key, consumed = decode_key(self._buffer, more_may_follow=False)
            self._buffer = self._buffer[consumed:]
            if key is not None:
                return key
        return None

    def _read_windows_key(self, timeout: float) -> str | None:
        import msvcrt

        if not msvcrt.kbhit() and timeout > 0:
            kernel32 = ctypes.windll.kernel32
            handles = (ctypes.c_void_p * 2)(self._stdin_handle, self._wake_event)
            result = kernel32.WaitForMultipleObjects(2, handles, False, max(1, int(timeout * 1000)))
            if result == 0 and not msvcrt.kbhit():
                # Non-key console events (mouse, focus, resize) keep stdin signaled; discard them.
                kernel32.FlushConsoleInputBuffer(self._stdin_handle)
        if not msvcrt.kbhit():
            return None
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return WINDOWS_SPECIAL_KEYS.get(msvcrt.getwch())
        return normalize_key(ch)


# --------------------------------------------------------------------------- application

INFO_TABS = ["Details", "Dependencies", "Required by"]
MODAL_CLOSE_HINT = "Esc/q: close"
VIEWS_HINT = "Views: 1 Details | 2 Dependencies | 3 Required by | Left/Right switch"


@dataclass
class NewPackage:
    display_name: str
    versions: list[str]
    yanked: set[str]


def table_columns(width: int) -> tuple[int, int, int, int]:
    action_w = 10
    installed_w = target_w = 20 if width >= 72 else 16
    name_w = max(12, width - installed_w - target_w - action_w)
    if name_w < 18 and width > 50:
        installed_w = target_w = 16
        name_w = max(12, width - installed_w - target_w - action_w)
    return name_w, installed_w, target_w, action_w


def step_index(index: int, key: str, count: int, page: int) -> int:
    """Move a list cursor for a navigation key, clamped to the list."""
    if count <= 0:
        return 0
    delta = {"up": -1, "down": 1, "pageup": -page, "pagedown": page}.get(key)
    if delta is not None:
        index += delta
    elif key == "home":
        index = 0
    elif key == "end":
        index = count - 1
    return max(0, min(count - 1, index))


class TuvApp:
    def __init__(self) -> None:
        self.terminal = Terminal()
        self.event_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        # Contexts.
        self.contexts: list[PythonContext] = []
        self.context_index = 0
        self.discovering_contexts = False
        self.rediscover_preserve = False
        self.quick_context_loaded = False
        self.pending_select_root: str | None = None
        self.discovery_error: str | None = None
        self.context_overlay = False
        self.context_overlay_pending = False
        self.context_overlay_index = 0
        self.context_overlay_scroll = 0
        # Package table.
        self.rows: list[PackageRow] = []
        self.view: list[PackageRow] = []
        self.focus_index = 0
        self.scroll = 0
        self.filter_text = ""
        self.selected_names: set[str] = set()
        self.pinned_by_context: dict[str, set[str]] = load_pins()
        self.target_overrides: dict[str, str] = {}
        self.updated_by_context: dict[str, set[str]] = {}
        self.refreshing = False
        self.refresh_generation = 0
        self.outdated_loading = False
        self.dependency_loading = False
        self.pending_after_refresh_action = False
        # Version selection.
        self.version_overlay = False
        self.version_overlay_row: str | None = None
        self.version_overlay_index = 0
        self.version_overlay_scroll = 0
        self.version_options: list[str] = []
        self.version_loading = False
        self.version_lookup_token = 0
        self.version_error: str | None = None
        self.pending_version_direction: int | None = None
        self.new_package: NewPackage | None = None
        # Package operations.
        self.installing = False
        self.install_proc: subprocess.Popen[str] | None = None
        self.install_proc_lock = threading.Lock()
        self.install_cancel_event = threading.Event()
        self.wait_queue: list[tuple[str, str]] = []
        self.bulk_active = False
        self.bulk_queue: list[tuple[str, str]] = []
        self.bulk_total = 0
        self.bulk_processed: set[str] = set()
        self.bulk_failed_results: dict[str, InstallResult] = {}
        self.bulk_summary: list[tuple[str, str, str]] = []
        self.mutation_blocked_reason: str | None = None
        self.creating_venv = False
        # Environment health.
        self.health_status = "unknown"
        self.health_issue_count: int | None = None
        self.health_lines: list[str] = []
        self.health_loading = False
        # Dialogs.
        self.prompt: Prompt | None = None
        self.info_open = False
        self.info_scroll = 0
        self.info_tab = 0
        self.report_open = False
        self.report_title = ""
        self.report_lines: list[str] = []
        self.report_scroll = 0
        self.input_mode: str | None = None  # filter | new_package | new_venv
        self.input_buffer = ""
        # Shell.
        self._message = "Starting..."
        self._message_kind = "info"
        self.spinner_index = 0
        self.should_quit = False
        self.last_render = ""
        self.last_size: tuple[int, int] = (0, 0)
        self._install_signal_handlers()

    # ----------------------------------------------------------------- plumbing

    @property
    def message(self) -> str:
        return self._message

    @message.setter
    def message(self, value: str) -> None:
        self.set_message(value)

    def set_message(self, value: str, kind: str = "info") -> None:
        self._message = value
        self._message_kind = kind

    @property
    def context(self) -> PythonContext | None:
        if not self.contexts:
            return None
        self.context_index = max(0, min(self.context_index, len(self.contexts) - 1))
        return self.contexts[self.context_index]

    def find_context(self, context_id: str) -> PythonContext | None:
        return next((context for context in self.contexts if context.id == context_id), None)

    def find_row(self, package_name: str) -> PackageRow | None:
        normalized = canonicalize_name(package_name)
        return next((row for row in self.rows if row.name == normalized), None)

    def is_current(self, context_id: str, generation: int | None = None) -> bool:
        """True when an event belongs to the selected context and the latest refresh."""
        context = self.context
        if context is None or context.id != context_id:
            return False
        return generation is None or generation == self.refresh_generation

    def pinned_names(self) -> set[str]:
        context = self.context
        return self.pinned_by_context.setdefault(context.id, set()) if context else set()

    def post(self, callback: Callable[[], None]) -> None:
        """Hand a callback from a worker thread to the main loop."""
        self.event_queue.put(callback)
        self.terminal.wake()

    @staticmethod
    def spawn(worker: Callable[[], None]) -> None:
        threading.Thread(target=worker, daemon=True).start()

    def process_events(self) -> None:
        while True:
            try:
                callback = self.event_queue.get_nowait()
            except queue.Empty:
                return
            callback()

    def _install_signal_handlers(self) -> None:
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGINT, lambda *_: self.interrupt())
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, lambda *_: self.interrupt(force=True))
            if hasattr(signal, "SIGWINCH"):
                signal.signal(signal.SIGWINCH, lambda *_: self.terminal.wake())

    def interrupt(self, force: bool = False) -> None:
        """Signal-handler entry: request a quit and wake the input wait so it is seen at once."""
        self.request_quit(force=force)
        self.terminal.wake()

    def run(self) -> int:
        with self.terminal:
            self.start_context_discovery()
            while not self.should_quit:
                self.process_events()
                self.update_view()
                self.ensure_scroll_visible()
                self.spinner_index = (self.spinner_index + 1) % len(SPINNER)
                size = self.terminal.size()
                if size != self.last_size:
                    self.last_size = size
                    self.last_render = ""
                    self.terminal.write(CLEAR + HOME)
                screen = self.render()
                if screen != self.last_render:
                    self.write_render_diff(screen)
                # Worker events wake the read early; the timeout only paces the spinner.
                key = self.terminal.read_key(0.08 if self.busy() else 0.5)
                handled = 0
                while key is not None and handled < 40 and not self.should_quit:
                    self.handle_key(key)
                    handled += 1
                    key = self.terminal.read_key(0.0)
            self.cancel_active_install()
        return 0

    def write_render_diff(self, screen: str) -> None:
        previous = self.last_render.splitlines()
        current = screen.splitlines()
        if not self.last_render or len(previous) != len(current):
            self.terminal.write(HOME + screen)
        else:
            changes = [f"\x1b[{index + 1};1H{line}" for index, line in enumerate(current) if line != previous[index]]
            if changes:
                self.terminal.write("".join(changes))
        self.last_render = screen

    def busy(self) -> bool:
        return (
            self.installing
            or self.refreshing
            or self.outdated_loading
            or self.version_loading
            or self.dependency_loading
            or self.discovering_contexts
            or self.creating_venv
            or self.health_loading
        )

    # ----------------------------------------------------------------- contexts

    def start_context_discovery(self, preserve_current: bool = False) -> None:
        if self.discovering_contexts:
            return
        self.discovering_contexts = True
        self.rediscover_preserve = preserve_current
        self.discovery_error = None
        self.message = "Rescanning Python contexts" if preserve_current else "Discovering Python contexts"

        def worker() -> None:
            try:
                if not preserve_current:
                    quick = discover_priority_contexts()
                    if quick:
                        self.post(partial(self.on_contexts_quick, quick))
                contexts = discover_contexts()
                self.post(partial(self.on_contexts_done, contexts))
            except Exception as exc:
                self.post(partial(self.on_contexts_failed, str(exc)))

        self.spawn(worker)

    def on_contexts_quick(self, contexts: list[PythonContext]) -> None:
        if not self.discovering_contexts or self.contexts or not contexts:
            return
        self.contexts = contexts
        self.context_index = self.context_overlay_index = self.default_context_index()
        self.quick_context_loaded = True
        self.load_current_context()

    def on_contexts_done(self, contexts: list[PythonContext]) -> None:
        self.discovering_contexts = False
        preserve, self.rediscover_preserve = self.rediscover_preserve, False
        quick_loaded, self.quick_context_loaded = self.quick_context_loaded, False
        pending_root, self.pending_select_root = self.pending_select_root, None
        previous = self.context
        confirmed = {context.id for context in self.contexts if context.confirmed_for_mutation}
        self.contexts = contexts
        for context in contexts:
            context.confirmed_for_mutation = context.confirmed_for_mutation or context.id in confirmed
        if not contexts:
            self.discovery_error = "Tuv could not find any usable Python context."
            self.set_message(self.discovery_error, "error")
            self.context_overlay_pending = False
            return
        ids = [context.id for context in contexts]
        kept = ids.index(previous.id) if previous is not None and previous.id in ids else None
        wanted = stable_context_id("venv", Path(pending_root)) if pending_root else None
        if quick_loaded and kept is not None:
            self.context_index = self.context_overlay_index = kept  # its refresh is already running
        elif wanted in ids and not self.installing:
            self.context_index = self.context_overlay_index = ids.index(wanted)
            self.load_current_context()
        elif preserve and kept is not None:
            self.context_index = kept
            self.context_overlay_index = min(self.context_overlay_index, len(contexts) - 1)
            self.message = f"Contexts rescanned: {len(contexts)} found"
            if wanted in ids:  # a venv created mid-install must not switch away from the install's context
                self.set_message("Created venv is listed; select it with F9 after the current install", "warn")
        else:
            self.context_index = self.context_overlay_index = self.default_context_index()
            self.load_current_context()
        self.open_pending_context_selector()

    def on_contexts_failed(self, error: str) -> None:
        self.discovering_contexts = False
        self.rediscover_preserve = False
        if self.quick_context_loaded and self.contexts:
            self.quick_context_loaded = False
            self.discovery_error = f"Full context discovery failed: {last_lines(error, 2)}"
            self.set_message(self.discovery_error, "warn")
            self.open_pending_context_selector()
            return
        self.context_overlay_pending = False
        self.contexts = []
        self.discovery_error = f"Context discovery failed: {last_lines(error, 2)}"
        self.set_message(self.discovery_error, "error")

    def default_context_index(self) -> int:
        preferences: list[Callable[[PythonContext], bool]] = [
            lambda c: c.type == "venv" and c.source == "active",
            lambda c: c.type == "venv" and c.root_path is not None and c.root_path.name == ".venv",
            lambda c: c.type == "interpreter" and c.source == "cwd",
            lambda c: c.type == "interpreter",
            lambda c: c.type == "tuv",
        ]
        for prefer in preferences:
            index = next((i for i, context in enumerate(self.contexts) if prefer(context)), None)
            if index is not None:
                return index
        return 0

    def request_context_selector(self) -> None:
        if self.discovering_contexts:
            self.context_overlay = False
            self.context_overlay_pending = True
            self.set_message("Finishing context discovery before opening selector", "warn")
            return
        self.open_context_selector()

    def open_pending_context_selector(self) -> None:
        if self.context_overlay_pending and self.contexts:
            self.open_context_selector()

    def open_context_selector(self) -> None:
        self.context_overlay_pending = False
        self.context_overlay = True
        self.context_overlay_index = self.context_index
        self.context_overlay_scroll = 0

    def rescan_contexts(self) -> None:
        if self.installing:
            self.set_message("Context rescan waits until the current install finishes", "warn")
            return
        self.start_context_discovery(preserve_current=True)

    def load_current_context(self) -> None:
        context = self.context
        if context is None:
            return
        self.rows = []
        self.view = []
        self.focus_index = self.scroll = 0
        self.filter_text = ""
        self.selected_names = set()
        self.target_overrides = {}
        self.wait_queue = []
        self.version_overlay = False
        self.version_overlay_row = None
        self.version_overlay_scroll = 0
        self.version_options = []
        self.version_loading = False
        self.version_lookup_token += 1
        self.version_error = None
        self.pending_version_direction = None
        self.new_package = None
        self.bulk_active = False
        self.bulk_queue = []
        self.bulk_total = 0
        self.bulk_processed = set()
        self.bulk_failed_results = {}
        self.bulk_summary = []
        self.outdated_loading = self.dependency_loading = self.pending_after_refresh_action = False
        self.reset_health()
        self.info_open = self.report_open = False
        self.info_scroll = self.info_tab = self.report_scroll = 0
        self.report_lines = []
        self.input_mode = None
        self.input_buffer = ""
        self.mutation_blocked_reason = None
        self.message = f"Loading {context.label}"
        self.load_packages(context, "Loading packages")

    def load_packages(self, context: PythonContext, message: str) -> None:
        if self.ensure_uv_provider(context, partial(self.load_packages, context, message)):
            self.start_refresh(context, message)

    def ensure_uv_provider(self, context: PythonContext, on_ready: Callable[[], None]) -> bool:
        """True when a uv provider is available now; otherwise repair runner uv and call `on_ready` later."""
        if (context.resolved_uv_provider or refresh_context_uv_provider(context)) is not None:
            self.mutation_blocked_reason = None
            return True
        self.bootstrap_tuv_runner_uv(context, on_ready)
        return False

    def bootstrap_tuv_runner_uv(self, context: PythonContext, on_ready: Callable[[], None]) -> None:
        runner = runner_python_path()
        self.message = f"Installing uv into Tuv runner venv: {runner}"

        def step(command: list[str], timeout: float) -> str:
            """Run one repair command; return its detail text, raising it on failure."""
            start = time.monotonic()
            proc = run_command(command, timeout=timeout)
            detail = command_detail(command, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start)
            if proc.returncode != 0:
                raise RuntimeError(detail)
            return detail

        def worker() -> None:
            pip = [str(runner), "-m", "pip"]
            try:
                if not runner.is_file():
                    raise RuntimeError(f"Tuv runner Python was not found: {runner}")
                if run_command([*pip, "--version"], timeout=20).returncode != 0:
                    step([str(runner), "-m", "ensurepip", "--upgrade"], 120)
                    step([*pip, "--version"], 20)
                detail = step([*pip, "install", "uv"], 300)
                step([str(runner), "-m", "uv", "--version"], 20)
            except Exception as exc:
                self.post(partial(self.on_runner_uv_done, context.id, False, str(exc), on_ready))
                return
            self.post(partial(self.on_runner_uv_done, context.id, True, detail, on_ready))

        self.spawn(worker)

    def on_runner_uv_done(self, context_id: str, ok: bool, detail: str, on_ready: Callable[[], None]) -> None:
        context = self.find_context(context_id)
        if context is None:
            return
        if ok and refresh_context_uv_provider(context) is not None:
            self.message = "uv installed into Tuv runner venv"
            self.mutation_blocked_reason = None
            on_ready()
            return
        self.message = "Tuv runner uv installation failed"
        self.mutation_blocked_reason = self.message
        self.info_open = True
        self.rows = [
            PackageRow(
                name="__tuv_runner_uv_repair__",
                display_name="Tuv runner uv repair failed",
                installed_version="-",
                status="failed",
                last_error="Tuv runner uv installation failed",
                last_error_detail=detail,
                description="Tuv operational error: runner uv repair failed.",
                operational_error=True,
            )
        ]

    # ----------------------------------------------------------------- package refresh

    def reset_health(self) -> None:
        self.health_status = "unknown"
        self.health_issue_count = None
        self.health_lines = []
        self.health_loading = False

    def start_refresh(self, context: PythonContext, message: str, install_result: InstallResult | None = None) -> None:
        self.refreshing = True
        self.refresh_generation += 1
        generation = self.refresh_generation
        self.reset_health()
        self.outdated_loading = self.dependency_loading = self.pending_after_refresh_action = False
        self.message = message
        updated_names = set(self.updated_by_context.get(context.id, set()))

        def worker() -> None:
            try:
                rows = build_package_rows(context, updated_names)
            except Exception as exc:
                self.post(partial(self.on_refresh_failed, context.id, generation, str(exc), install_result))
                return
            self.post(partial(self.on_refresh_done, context.id, generation, rows, install_result))

        self.spawn(worker)

    def start_outdated_refresh(self, context: PythonContext, generation: int) -> None:
        self.outdated_loading = True

        def worker() -> None:
            targets, warning = load_outdated_targets(context)
            self.post(partial(self.on_outdated_done, context.id, generation, targets, warning))

        self.spawn(worker)

    def start_dependency_refresh(self, context: PythonContext, generation: int) -> None:
        display_by_name = {row.name: row.display_name for row in self.rows}
        self.dependency_loading = True

        def worker() -> None:
            try:
                info = load_dependency_info(context, display_by_name)
            except Exception as exc:
                self.post(partial(self.on_dependency_done, context.id, generation, None, str(exc)))
                return
            self.post(partial(self.on_dependency_done, context.id, generation, info, None))

        self.spawn(worker)

    def start_health_check(self, context: PythonContext, generation: int) -> None:
        if self.health_loading:
            return
        self.health_status = "checking"
        self.health_issue_count = None
        self.health_loading = True

        def worker() -> None:
            try:
                proc = run_command(uv_command(context, ["pip", "check", "--python", context.uv_target]), timeout=120)
                outcome: tuple[int | None, str, str, str | None] = (proc.returncode, proc.stdout, proc.stderr, None)
            except Exception as exc:
                outcome = (None, "", "", str(exc))
            self.post(partial(self.on_health_done, context.id, generation, *outcome))

        self.spawn(worker)

    def on_refresh_done(self, context_id: str, generation: int, rows: list[PackageRow], install_result: InstallResult | None) -> None:
        if not self.is_current(context_id, generation):
            return
        self.refreshing = False
        if install_result is not None:
            self.note_updated_packages(context_id, install_result.before_versions, rows)
        focused = self.focused_row()
        self.carry_refresh_state(rows)
        self.rows = rows
        self.update_view()
        if focused is not None:
            self.focus_on_package(focused.name)
        self.scroll = min(self.scroll, max(0, len(self.view) - 1))
        for result in self.bulk_failed_results.values():
            self.apply_failed_result_to_row(result)
        self.prune_target_overrides()
        if install_result is None:
            self.message = f"Loaded {len(self.rows)} packages"
        else:
            self.installing = False
            self.pending_after_refresh_action = True
            self.record_bulk_outcome(install_result)
            self.report_install_result(install_result)
        context = self.context
        assert context is not None
        if self.bulk_active and self.bulk_queue and install_result is not None:
            # Between bulk items only the installed list is needed to detect
            # dependency-side version changes. Defer network outdated lookup,
            # metadata traversal, and health checks until the final item.
            self.finish_full_refresh()
            return
        self.start_dependency_refresh(context, generation)
        self.start_outdated_refresh(context, generation)
        if not self.bulk_active:
            self.start_health_check(context, generation)

    def on_refresh_failed(self, context_id: str, generation: int, error: str, install_result: InstallResult | None) -> None:
        if generation != self.refresh_generation:
            return
        self.refreshing = False
        if self.is_current(context_id):
            self.message = f"Refresh failed: {last_lines(error, 2)}"
        if install_result is not None:
            self.installing = False
            if self.bulk_active:
                self.continue_bulk_update()
            else:
                self.maybe_start_waiting_install()

    def on_outdated_done(self, context_id: str, generation: int, targets: dict[str, str], warning: str | None) -> None:
        if not self.is_current(context_id, generation):
            return
        self.outdated_loading = False
        outdated = 0
        for row in self.rows:
            if row.status == "failed" and row.last_install_result is not None:
                continue
            latest = targets.get(row.name)
            if latest:
                # uv's latest is a trusted installable target; the full candidate
                # list is fetched lazily when the user first browses versions.
                row.candidate_versions = sorted({*row.candidate_versions, row.installed_version, latest}, key=version_key)
                row.versions_resolved = True
            override = self.target_overrides.get(row.name)
            if override and override != row.installed_version and row.versions_resolved and override in row.candidate_versions:
                row.target_version = override
            else:
                row.target_version = latest or row.installed_version
            if row.status not in {"failed", "installing", "wait"}:
                row.status = "ready" if row.target_version != row.installed_version else "current"
            outdated += row.is_outdated
        if warning:
            self.set_message(warning, "warn")
        elif outdated:
            self.message = f"{outdated} package(s) have updates available"
        else:
            self.message = "All packages are up to date"
        self.finish_full_refresh()

    def finish_full_refresh(self) -> None:
        if self.refreshing or self.outdated_loading or not self.pending_after_refresh_action:
            return
        self.pending_after_refresh_action = False
        if self.bulk_active:
            self.continue_bulk_update()
        else:
            self.maybe_start_waiting_install()

    def on_dependency_done(self, context_id: str, generation: int, info: DependencyInfo | None, error: str | None) -> None:
        if not self.is_current(context_id, generation):
            return
        self.dependency_loading = False
        if info is None:
            if error:
                self.message = f"Dependency data unavailable: {last_lines(error, 2)}"
            return
        for row in self.rows:
            row.metadata_trusted = True
            row.uninstall_safe = row.name in info.safe_names
            row.dependency_packages = info.dependencies.get(row.name, [])
            row.usage_packages = info.usage.get(row.name, [])
            row.description = info.descriptions.get(row.name)

    def on_health_done(self, context_id: str, generation: int, returncode: int | None, stdout: str, stderr: str, error: str | None) -> None:
        if not self.is_current(context_id, generation):
            return
        self.health_loading = False
        combined = [line.rstrip() for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
        if error is not None or returncode is None:
            self.health_status = "error"
            self.health_issue_count = None
            self.health_lines = ["Status: check failed", "", error or "Unknown health-check failure"]
        elif returncode == 0:
            self.health_status = "healthy"
            self.health_issue_count = 0
            self.health_lines = ["Status: healthy", "", *(combined or ["All installed packages are compatible"])]
        else:
            match = re.search(r"\bFound\s+(\d+)\s+(?:incompatibilit|issue|problem)", "\n".join(combined), re.IGNORECASE)
            diagnostics = [l for l in stdout.splitlines() if l.strip() and not l.lower().startswith(("checked ", "using python "))]
            count = int(match.group(1)) if match else (len(diagnostics) or None)
            self.health_status = "issues"
            self.health_issue_count = count
            count_text = str(count) if count is not None else "one or more"
            self.health_lines = [f"Status: {count_text} compatibility issue(s)", "", *(combined or ["uv pip check failed"])]

    def note_updated_packages(self, context_id: str, before_versions: dict[str, str], rows: list[PackageRow]) -> None:
        if not before_versions:
            return
        changed = self.updated_by_context.setdefault(context_id, set())
        changed.update(row.name for row in rows if before_versions.get(row.name) != row.installed_version)
        for row in rows:
            row.updated_in_session = row.name in changed

    def carry_refresh_state(self, rows: list[PackageRow]) -> None:
        """Carry color hints and already-fetched candidate lists across a refresh."""
        previous = {row.name: row for row in self.rows}
        for row in rows:
            old = previous.get(row.name)
            if old is None:
                continue
            if row.status == "loading":
                row.color_hint = self.row_color_hint(old)
            if old.versions_resolved:
                row.candidate_versions = sorted({*old.candidate_versions, *row.candidate_versions}, key=version_key)
                row.versions_resolved = True
                row.full_versions_loaded = old.full_versions_loaded
                row.yanked_versions = set(old.yanked_versions)

    def row_color_hint(self, row: PackageRow) -> str | None:
        if row.updated_in_session:
            return "updated"
        if row.status == "current":
            return "current"
        if row.is_outdated:
            return "outdated"
        return row.color_hint if row.status == "loading" else None

    def prune_target_overrides(self) -> None:
        for name, target in list(self.target_overrides.items()):
            row = self.find_row(name)
            if row is None or row.installed_version == target:
                self.target_overrides.pop(name, None)

    # ----------------------------------------------------------------- table view

    def update_view(self) -> None:
        needle = self.filter_text.lower()
        view = [row for row in self.rows if not needle or needle in row.display_name.lower() or needle in row.name]
        if len(view) != len(self.view) or any(a is not b for a, b in zip(view, self.view)):
            focused = self.focused_row()
            self.view = view
            if focused is not None:
                self.focus_on_package(focused.name)
        self.focus_index = max(0, min(self.focus_index, max(0, len(self.view) - 1)))

    def focus_on_package(self, package_name: str) -> None:
        index = next((i for i, row in enumerate(self.view) if row.name == package_name), None)
        self.focus_index = index if index is not None else max(0, min(self.focus_index, max(0, len(self.view) - 1)))

    def focused_row(self) -> PackageRow | None:
        if not self.view:
            return None
        self.focus_index = max(0, min(self.focus_index, len(self.view) - 1))
        return self.view[self.focus_index]

    def move_focus(self, amount: int) -> None:
        if self.view:
            self.focus_index = max(0, min(len(self.view) - 1, self.focus_index + amount))

    def page_size(self) -> int:
        _, height = self.terminal.size()
        return max(1, height - 6)

    def ensure_scroll_visible(self) -> None:
        visible = self.page_size()
        if self.focus_index < self.scroll:
            self.scroll = self.focus_index
        elif self.focus_index >= self.scroll + visible:
            self.scroll = self.focus_index - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, len(self.view) - visible)))

    def natural_row_status(self, row: PackageRow) -> str:
        if row.target_version == row.installed_version:
            return "current"
        if row.versions_resolved and row.target_version in row.candidate_versions:
            return "ready"
        return "loading"

    def version_resolution_busy(self) -> bool:
        return self.refreshing or self.outdated_loading or self.version_loading

    def row_target_installable(self, row: PackageRow) -> bool:
        return (
            row.target_version != row.installed_version
            and row.versions_resolved
            and row.target_version in row.candidate_versions
            and row.status not in {"loading", "installing"}
        )

    def block_unresolved_action(self, row: PackageRow | None = None) -> bool:
        if self.version_resolution_busy():
            self.set_message("Version resolution is still in progress", "warn")
            return True
        if row is not None and row.target_version != row.installed_version and not self.row_target_installable(row):
            self.set_message(f"Version data unavailable for {row.display_name}", "warn")
            return True
        return False

    # ----------------------------------------------------------------- key handling

    def handle_key(self, key: str) -> None:
        if key in {"\x1b", "escape", "Escape", "ESC"}:
            key = "esc"
        modal = self.prompt or self.report_open or self.info_open or self.version_overlay or self.context_overlay
        if key == "q" and (modal or self.context_overlay_pending):
            key = "esc"
        if self.context_overlay_pending and key == "esc":
            self.context_overlay_pending = False
            self.set_message("Context selector cancelled", "warn")
        elif self.input_mode:
            self.handle_input_key(key)
        elif self.prompt:
            self.handle_prompt_key(key)
        elif self.report_open:
            self.handle_report_key(key)
        elif self.info_open:
            self.handle_info_key(key)
        elif self.version_overlay:
            self.handle_version_overlay_key(key)
        elif self.context_overlay:
            self.handle_context_overlay_key(key)
        else:
            self.handle_main_key(key)

    def handle_main_key(self, key: str) -> None:
        lowered = key.lower() if len(key) == 1 else key
        if key in {"quit", "f10"} or lowered == "q":
            self.request_quit()
        elif key == "esc":
            self.handle_main_escape()
        elif key in {"up", "down", "pageup", "pagedown", "home", "end"}:
            self.focus_index = step_index(self.focus_index, key, len(self.view), self.page_size())
        elif key == "left":
            self.change_target_version(-1)
        elif key == "right":
            self.change_target_version(1)
        elif key == "enter":
            self.request_install()
        elif key == " ":
            self.toggle_selection()
        elif key == "/":
            self.input_mode = "filter"
            self.input_buffer = self.filter_text
        elif lowered == "n":
            self.request_new_package()
        elif lowered == "i" or key == "f3":
            self.open_information()
        elif lowered == "p":
            self.toggle_pin()
        elif key == "delete":
            self.request_uninstall()
        elif key == "f2":
            self.start_bulk_update()
        elif key == "f4":
            self.open_version_selector()
        elif key == "f5":
            self.rescan_contexts()
        elif key == "f6":
            self.open_health_report()
        elif key == "f9" or lowered == "c":
            self.request_context_selector()
        elif lowered == "r":
            context = self.context
            if context is not None:
                if self.installing:
                    self.set_message("Refresh waits until the current install finishes", "warn")
                else:
                    self.load_packages(context, "Refreshing packages")

    def handle_main_escape(self) -> None:
        if self.selected_names:
            self.selected_names = set()
            self.message = "Selection cleared"
        elif self.filter_text:
            self.filter_text = ""
            self.message = "Filter cleared"
        elif self.bulk_active and not self.installing:
            self.bulk_queue = []
            self.set_message("Update all cancelled; finishing current step", "warn")
        elif self.installing:
            operation = "bulk update" if self.bulk_active else "operation"
            self.prompt = Prompt(
                title="Cancel running operation",
                message=f"Cancel the running {operation}? y/N",
                on_yes=self.confirm_cancel_install,
                on_no=partial(self.set_message, "Cancel dismissed"),
            )

    def confirm_cancel_install(self) -> None:
        self.cancel_active_install()
        self.set_message("Cancelling the running operation...", "warn")

    def handle_input_key(self, key: str) -> None:
        mode = self.input_mode
        if key == "esc":
            self.input_mode = None
            self.input_buffer = ""
            if mode == "filter":
                self.filter_text = ""
                self.message = "Filter cleared"
            else:
                self.message = "Cancelled"
        elif key == "enter":
            if mode == "filter":
                self.input_mode = None
                self.input_buffer = ""
                self.message = f"Filter: {self.filter_text}" if self.filter_text else "Filter cleared"
            elif mode == "new_package":
                self.submit_new_package_name()
            elif mode == "new_venv":
                self.submit_create_venv()
            else:
                self.input_mode = None
        elif key == "backspace":
            self.input_buffer = self.input_buffer[:-1]
            if mode == "filter":
                self.filter_text = self.input_buffer
        elif mode == "filter" and key in {"up", "down", "pageup", "pagedown"}:
            # Allow navigating results while the filter is being typed.
            self.focus_index = step_index(self.focus_index, key, len(self.view), self.page_size())
        elif len(key) == 1 and key.isprintable() and len(self.input_buffer) < 80:
            self.input_buffer += key
            if mode == "filter":
                self.filter_text = self.input_buffer

    def scroll_text(self, attr: str, key: str) -> None:
        current: int = getattr(self, attr)
        page = self.page_size()
        target = {"up": current - 1, "down": current + 1, "pageup": current - page, "pagedown": current + page, "home": 0, "end": 10**9}
        if key in target:
            setattr(self, attr, max(0, target[key]))

    def handle_report_key(self, key: str) -> None:
        if key in {"esc", "enter"}:
            self.report_open = False
            self.report_scroll = 0
        else:
            self.scroll_text("report_scroll", key)

    def handle_info_key(self, key: str) -> None:
        if key in {"esc", "enter", "f3"}:
            self.info_open = False
            self.info_scroll = self.info_tab = 0
        elif key == "left":
            self.set_info_tab(self.info_tab - 1)
        elif key == "right":
            self.set_info_tab(self.info_tab + 1)
        elif key in {"1", "2", "3"}:
            self.set_info_tab(int(key) - 1)
        else:
            self.scroll_text("info_scroll", key)

    def open_information(self) -> None:
        self.info_open = True
        self.info_scroll = self.info_tab = 0

    def set_info_tab(self, index: int) -> None:
        self.info_tab = index % len(INFO_TABS)
        self.info_scroll = 0

    def open_report(self, title: str, lines: list[str]) -> None:
        self.report_open = True
        self.report_title = title
        self.report_lines = lines
        self.report_scroll = 0

    def open_health_report(self) -> None:
        context = self.context
        if context is None:
            return
        if self.health_loading:
            self.set_message("Environment health check is still running", "warn")
        elif self.health_status == "unknown":
            self.start_health_check(context, self.refresh_generation)
            self.message = "Checking environment health"
        else:
            self.open_report("Environment health", self.health_lines or ["Status: not checked"])

    def handle_prompt_key(self, key: str) -> None:
        """Destructive confirmations are fail-closed: only `y` accepts."""
        prompt = self.prompt
        if prompt is None:
            return
        lowered = key.lower() if len(key) == 1 else key
        if lowered == "y":
            self.prompt = None
            prompt.on_yes()
        elif lowered == "n" or key in {"esc", "enter"}:
            self.prompt = None
            prompt.on_no()

    def handle_context_overlay_key(self, key: str) -> None:
        lowered = key.lower() if len(key) == 1 else key
        if key in {"esc", "f9"}:
            self.context_overlay = False
        elif lowered == "n":
            self.request_create_venv()
        elif key == "f5":
            self.rescan_contexts()
        elif not self.contexts:
            return
        elif key == "enter":
            self.context_overlay = False
            if self.installing:
                self.set_message("Context switch waits until the current install finishes", "warn")
            elif self.context_overlay_index != self.context_index:
                self.context_index = self.context_overlay_index
                self.load_current_context()
        else:
            self.context_overlay_index = step_index(self.context_overlay_index, key, len(self.contexts), self.page_size())

    def request_quit(self, force: bool = False) -> None:
        quit_prompt_pending = self.prompt is not None and self.prompt.quit
        if force or not self.installing or quit_prompt_pending:
            # A second quit request while the quit prompt is pending forces out.
            self.should_quit = True
            return
        operation = "bulk update" if self.bulk_active else "install"
        self.prompt = Prompt(
            title="Quit while installing",
            message=f"A {operation} is running. Quit and cancel it? y/N",
            on_yes=self.confirm_quit,
            on_no=partial(self.set_message, "Quit cancelled; the operation continues", "warn"),
            quit=True,
        )

    def confirm_quit(self) -> None:
        self.cancel_active_install()
        self.should_quit = True

    def cancel_active_install(self) -> None:
        self.install_cancel_event.set()
        with self.install_proc_lock:
            proc = self.install_proc
        if proc is not None:
            terminate_process_tree(proc)
        if self.bulk_active:
            self.bulk_queue = []

    # ----------------------------------------------------------------- versions

    def refresh_version_options(self, row: PackageRow) -> None:
        self.version_options = sorted({*row.candidate_versions, row.target_version}, key=version_key, reverse=True)
        self.version_overlay_index = self.version_options.index(row.target_version)
        self.ensure_version_overlay_visible()

    def version_visible_items(self, height: int | None = None) -> int:
        if height is None:
            _, height = self.terminal.size()
        height = max(height, 10)
        overlay_h = min(height - 4, max(8, min(len(self.version_options) + 5, height - 4)))
        return max(1, overlay_h - 4)

    def ensure_version_overlay_visible(self, visible: int | None = None) -> None:
        count = len(self.version_options)
        if not count:
            self.version_overlay_scroll = self.version_overlay_index = 0
            return
        visible = visible or self.version_visible_items()
        self.version_overlay_index = max(0, min(self.version_overlay_index, count - 1))
        if self.version_overlay_index < self.version_overlay_scroll:
            self.version_overlay_scroll = self.version_overlay_index
        elif self.version_overlay_index >= self.version_overlay_scroll + visible:
            self.version_overlay_scroll = self.version_overlay_index - visible + 1
        self.version_overlay_scroll = max(0, min(self.version_overlay_scroll, max(0, count - visible)))

    def start_version_lookup(self, row: PackageRow, pending_direction: int | None = None) -> bool:
        context = self.context
        if context is None or row.full_versions_loaded:
            return False
        if self.version_loading:
            if self.version_overlay_row == row.name and pending_direction is not None:
                self.pending_version_direction = pending_direction
                self.set_message(f"Version change waits for {row.display_name} versions", "warn")
            else:
                self.set_message("Version lookup already in progress", "warn")
            return False
        self.version_overlay_row = row.name
        self.version_error = None
        self.version_loading = True
        self.version_lookup_token += 1
        token = self.version_lookup_token
        self.pending_version_direction = pending_direction
        if not row.versions_resolved and row.status not in {"installing", "wait"} and not (row.status == "failed" and row.last_install_result):
            row.status = "loading"
            row.last_error = row.last_error_detail = None
        self.message = f"Loading versions for {row.display_name}"
        self.spawn(partial(self.version_lookup_worker, token, row.display_name, context, self.on_versions_done))
        return True

    def version_lookup_worker(self, token: int, display_name: str, context: PythonContext, done: Callable[..., None]) -> None:
        try:
            versions, yanked = fetch_available_versions(display_name, context)
        except Exception as exc:
            self.post(partial(done, token, display_name, [], set(), str(exc)))
            return
        self.post(partial(done, token, display_name, versions, yanked, None))

    def selectable_versions(self, row: PackageRow) -> list[str]:
        candidates = sorted({*row.candidate_versions, row.target_version}, key=version_key)
        # Arrow stepping skips yanked versions; they stay selectable in the
        # F4 overlay, where selection asks for confirmation.
        allowed = [v for v in candidates if v not in row.yanked_versions or v in {row.installed_version, row.target_version}]
        return allowed or candidates

    def change_target_version(self, direction: int) -> None:
        row = self.focused_row()
        if row is None:
            return
        if not row.full_versions_loaded:
            # Fetch the full candidate list lazily; the step is applied when the lookup completes.
            self.start_version_lookup(row, pending_direction=direction)
            return
        self.apply_version_direction(row, direction)

    def apply_version_direction(self, row: PackageRow, direction: int) -> None:
        if not row.versions_resolved:
            self.set_message(f"Version lookup is not ready for {row.display_name}", "warn")
            return
        candidates = self.selectable_versions(row)
        index = candidates.index(row.target_version) if row.target_version in candidates else 0
        self.set_row_target(row, candidates[max(0, min(len(candidates) - 1, index + direction))])

    def set_row_target(self, row: PackageRow, target: str) -> None:
        row.target_version = target
        if target != row.installed_version:
            self.target_overrides[row.name] = target
        else:
            self.target_overrides.pop(row.name, None)
        if row.status not in {"installing", "wait", "failed"}:
            row.status = "ready" if row.target_version != row.installed_version else "current"
        if self.version_overlay and self.version_overlay_row == row.name:
            self.refresh_version_options(row)

    def open_version_selector(self) -> None:
        row = self.focused_row()
        if row is None:
            return
        self.new_package = None
        self.version_overlay = True
        self.version_overlay_row = row.name
        self.version_overlay_scroll = 0
        self.refresh_version_options(row)
        self.version_error = None
        self.pending_version_direction = None
        self.start_version_lookup(row)

    def on_versions_done(self, token: int, display_name: str, versions: list[str], yanked: set[str], error: str | None) -> None:
        if token != self.version_lookup_token:
            return  # A newer lookup owns the loading flag; drop this stale result.
        self.version_loading = False
        row = self.find_row(display_name)
        if row is None:
            return
        pending_direction = self.pending_version_direction if self.version_overlay_row == row.name else None
        self.pending_version_direction = None
        if versions:
            row.candidate_versions = sorted({*versions, row.installed_version}, key=version_key)
            row.versions_resolved = row.full_versions_loaded = True
            row.yanked_versions = set(yanked)
            row.last_error = row.last_error_detail = None
            self.version_error = None
            self.refresh_version_options(row)
            self.message = f"Loaded {len(self.version_options)} versions for {row.display_name}"
            if row.status == "loading":
                row.status = "ready" if row.target_version != row.installed_version else "current"
            if pending_direction is not None:
                self.apply_version_direction(row, pending_direction)
            return
        detail = last_lines(error or "no versions found", 2)
        if row.versions_resolved:
            # uv already supplied an installable target; degrade gracefully
            # instead of blocking installs (private indexes, offline, ...).
            self.version_error = f"Full version list unavailable: {detail}"
            self.set_message(f"Full version list unavailable for {row.display_name}; using uv-reported versions", "warn")
        else:
            self.version_error = f"Version lookup failed: {detail}"
            self.set_message(self.version_error, "error")
            if row.status == "loading":
                row.status = "nodata"
                row.last_error = "Version lookup failed"
                row.last_error_detail = error or "No installable versions found"
        self.refresh_version_options(row)

    def handle_version_overlay_key(self, key: str) -> None:
        if key in {"esc", "f4"}:
            self.version_overlay = False
            self.new_package = None
        elif not self.version_options:
            return
        elif key != "enter":
            self.version_overlay_index = step_index(self.version_overlay_index, key, len(self.version_options), self.page_size())
            self.ensure_version_overlay_visible()
        elif self.new_package is not None:
            self.handle_new_package_version_enter()
        else:
            row = self.find_row(self.version_overlay_row or "")
            if row is None:
                self.version_overlay = False
                return
            if self.version_loading or not row.versions_resolved:
                self.set_message(f"Version lookup is not ready for {row.display_name}", "warn")
                return
            selected = self.version_options[self.version_overlay_index]
            if selected not in row.candidate_versions and selected != row.installed_version:
                self.set_message(f"Version data unavailable for {row.display_name}", "warn")
                return
            self.set_row_target(row, selected)
            self.version_overlay = False
            self.focus_on_package(row.name)
            self.request_install(row.name)

    # ----------------------------------------------------------------- install, uninstall, new package

    def request_install(self, package_name: str | None = None) -> None:
        context = self.context
        row = self.find_row(package_name) if package_name else self.focused_row()
        if context is None or row is None or row.operational_error:
            return
        if self.mutation_blocked_reason:
            self.set_message(self.mutation_blocked_reason, "error")
            return
        if self.block_unresolved_action(row):
            return
        if self.installing:
            self.mark_wait(row)
            return
        if row.target_version == row.installed_version:
            self.message = f"{row.display_name} is already at {row.installed_version}"
            if row.status not in {"failed", "nodata"}:
                row.status = "current"
            return
        # Capture the package name so async continuations retry the same row even if focus moved.
        if not self.ensure_uv_provider(context, partial(self.request_install, row.name)):
            return
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            self.prompt = Prompt(
                title="Confirm interpreter install",
                message=f"Install into interpreter {context.python_path}? y/N",
                on_yes=partial(self.confirm_interpreter_and_install, context, row.name),
                on_no=partial(self.set_message, "Install cancelled"),
            )
            return
        if row.target_version in row.yanked_versions:
            self.prompt = Prompt(
                title="Confirm yanked version",
                message=self.yanked_warning(row.display_name, row.target_version),
                on_yes=partial(self.confirm_yanked_and_install, row.name),
                on_no=partial(self.set_message, "Install cancelled"),
            )
            return
        self.begin_install(row)

    @staticmethod
    def yanked_warning(display_name: str, version: str) -> str:
        return f"{display_name}=={version} is yanked on the index (usually due to a serious defect). Install anyway? y/N"

    def confirm_interpreter_and_install(self, context: PythonContext, package_name: str) -> None:
        context.confirmed_for_mutation = True
        self.request_install(package_name)

    def confirm_yanked_and_install(self, package_name: str) -> None:
        row = self.find_row(package_name)
        if row is not None:
            self.begin_install(row)  # Bypasses the yanked re-check; every other guard still applies.

    def mark_wait(self, row: PackageRow) -> None:
        """Queue a row for installation after the running operation; repeated requests keep one entry."""
        row.status = "wait"
        for index, (name, _target) in enumerate(self.wait_queue):
            if name == row.name:
                self.wait_queue[index] = (row.name, row.target_version)
                self.set_message(f"Already queued: {row.display_name}", "warn")
                return
        self.wait_queue.append((row.name, row.target_version))
        self.message = f"Queued ({len(self.wait_queue)} waiting): {row.display_name}"

    def maybe_start_waiting_install(self) -> None:
        while self.wait_queue and not self.installing:
            package_name, target_version = self.wait_queue.pop(0)
            row = self.find_row(package_name)
            if row is None:
                self.set_message(f"Waiting package vanished: {package_name}", "warn")
            elif row.installed_version == target_version:
                row.status = "current"
                self.message = f"Waiting package already current: {row.display_name}"
            elif not row.versions_resolved or target_version not in row.candidate_versions:
                self.set_message(f"Waiting package no longer has a valid target: {row.display_name}", "warn")
                if row.status == "wait":
                    row.status = self.natural_row_status(row)
            else:
                row.target_version = target_version
                self.begin_install(row)

    def begin_install(self, row: PackageRow) -> bool:
        context = self.context
        if context is None:
            return False
        if not self.row_target_installable(row):
            self.set_message(f"Version data unavailable for {row.display_name}", "warn")
            return False
        try:
            command = self.mutation_command(context, "install")
        except Exception as exc:
            row.status = "failed"
            row.last_error = "uv provider unavailable"
            row.last_error_detail = str(exc)
            self.set_message("uv provider unavailable", "error")
            return False
        package_spec = f"{row.display_name}=={row.target_version}"
        row.status = "installing"
        row.last_error = row.last_error_detail = None
        row.last_install_result = None
        self.message = f"Installing {package_spec}"
        self.run_package_operation(
            context,
            [*command, package_spec],
            operation="install",
            package_name=row.name,
            display_name=row.display_name,
            target_version=row.target_version,
            installed_version_at_attempt=row.installed_version,
            candidate_versions_at_attempt=list(row.candidate_versions),
        )
        return True

    @staticmethod
    def mutation_command(context: PythonContext, verb: str) -> list[str]:
        command = uv_command(context, ["pip", verb, "--python", context.uv_target])
        if context.type == "interpreter":
            command.append("--system")
        return command

    def run_package_operation(
        self,
        context: PythonContext,
        command: list[str],
        operation: str,
        package_name: str,
        display_name: str,
        target_version: str,
        installed_version_at_attempt: str = "",
        candidate_versions_at_attempt: list[str] | None = None,
    ) -> None:
        """Run `command` in a worker; installs are validated with `--dry-run` first."""
        self.installing = True
        self.install_cancel_event.clear()
        before_versions = {row.name: row.installed_version for row in self.rows}
        steps = [command]
        if operation == "install":
            steps.insert(0, [*command[:-1], "--dry-run", command[-1]])
        popen_options: dict[str, object] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
        )

        def worker() -> None:
            start = time.monotonic()
            step = command
            exit_code: int | None = None
            stdout = stderr = ""
            try:
                for step in steps:
                    if self.install_cancel_event.is_set():
                        raise RuntimeError("Operation cancelled before process start")
                    proc = subprocess.Popen(step, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **popen_options)
                    with self.install_proc_lock:
                        self.install_proc = proc
                    if self.install_cancel_event.is_set():
                        terminate_process_tree(proc)
                    stdout, stderr = proc.communicate()
                    with self.install_proc_lock:
                        self.install_proc = None
                    exit_code = proc.returncode
                    if exit_code != 0 or self.install_cancel_event.is_set():
                        break
            except Exception as exc:
                exit_code, stdout, stderr = None, "", str(exc)
            finally:
                with self.install_proc_lock:
                    self.install_proc = None
            result = InstallResult(
                context_id=context.id,
                package_name=package_name,
                display_name=display_name,
                operation=operation,
                target_version=target_version,
                command=step,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                elapsed=time.monotonic() - start,
                before_versions=before_versions,
                installed_version_at_attempt=installed_version_at_attempt,
                candidate_versions_at_attempt=list(candidate_versions_at_attempt or []),
                cancelled=self.install_cancel_event.is_set(),
            )
            self.post(partial(self.on_install_done, result))

        self.spawn(worker)

    def on_install_done(self, result: InstallResult) -> None:
        context = self.find_context(result.context_id)
        if context is None or not self.is_current(result.context_id):
            # The install's context is gone or no longer selected, so its refresh
            # events would be dropped; release the install slot directly.
            self.installing = False
            if self.bulk_active:
                self.finalize_bulk_update()
            return
        self.start_refresh(context, f"Refreshing packages after {result.operation}", result)

    def report_install_result(self, result: InstallResult) -> None:
        if result.cancelled:
            self.set_message(f"{result.operation.capitalize()} cancelled: {result.label}", "warn")
        elif not result.ok:
            self.mark_failed_row(result)
            self.set_message(f"{result.operation.capitalize()} failed: {result.label}", "error")
        elif result.operation == "uninstall":
            self.message = f"Uninstalled {result.label}"
        else:
            self.message = f"Installed {result.label}=={result.target_version}"

    def mark_failed_row(self, result: InstallResult) -> None:
        if self.bulk_active:
            self.bulk_failed_results[result.package_name] = result
            self.bulk_processed.add(result.package_name)
        self.apply_failed_result_to_row(result)
        if self.find_row(result.package_name) is None and not result.cancelled:
            # No row can hold the detail (e.g. a failed new-package install); use a report overlay.
            detail = command_detail(result.command, result.exit_code, result.stdout, result.stderr, result.elapsed)
            self.open_report(f"{result.operation.capitalize()} failed: {result.label}", detail.splitlines())

    def apply_failed_result_to_row(self, result: InstallResult) -> None:
        row = self.find_row(result.package_name)
        if row is None:
            return
        if result.operation == "install":
            if result.candidate_versions_at_attempt:
                row.candidate_versions = sorted(set(result.candidate_versions_at_attempt), key=version_key)
            row.target_version = result.target_version
            row.versions_resolved = result.target_version in row.candidate_versions
        row.status = "failed"
        row.last_error = f"{result.operation.capitalize()} failed with exit code {result.exit_text}"
        row.last_error_detail = command_detail(result.command, result.exit_code, result.stdout, result.stderr, result.elapsed, include_command=False)
        row.last_install_result = result

    def request_uninstall(self, package_name: str | None = None) -> None:
        context = self.context
        row = self.find_row(package_name) if package_name else self.focused_row()
        if context is None or row is None or row.operational_error:
            return
        if self.mutation_blocked_reason:
            self.set_message(self.mutation_blocked_reason, "error")
            return
        if self.installing or self.bulk_active:
            self.set_message("Uninstall waits until the current activity finishes", "warn")
            return
        if not self.ensure_uv_provider(context, partial(self.request_uninstall, row.name)):
            return
        lines = [f"Uninstall {row.display_name} {row.installed_version} from:", context.label, ""]
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            lines += [f"This modifies interpreter {context.python_path}.", ""]
        if not row.metadata_trusted:
            lines.append("Dependency data is unavailable - safety cannot be verified.")
        elif row.usage_packages:
            suffix = ", ..." if len(row.usage_packages) > 8 else ""
            lines += [f"WARNING - required by: {', '.join(row.usage_packages[:8])}{suffix}", "Uninstalling may break those packages."]
        else:
            lines.append("Not required by any other installed package.")
        lines += ["", "Proceed? y/N"]
        self.prompt = Prompt(
            title="Confirm uninstall",
            message="\n".join(lines),
            on_yes=partial(self.confirm_uninstall, context, row.name),
            on_no=partial(self.set_message, "Uninstall cancelled"),
        )

    def confirm_uninstall(self, context: PythonContext, package_name: str) -> None:
        row = self.find_row(package_name)
        if not self.is_current(context.id) or row is None or self.installing:
            return
        if context.type == "interpreter":
            context.confirmed_for_mutation = True
        try:
            command = self.mutation_command(context, "uninstall")
        except Exception as exc:
            self.set_message(f"uv provider unavailable: {last_lines(str(exc), 1)}", "error")
            return
        row.status = "installing"
        row.last_error = row.last_error_detail = None
        self.message = f"Uninstalling {row.display_name}"
        self.run_package_operation(
            context,
            [*command, row.display_name],
            operation="uninstall",
            package_name=row.name,
            display_name=row.display_name,
            target_version=row.installed_version,
            installed_version_at_attempt=row.installed_version,
        )

    def request_new_package(self) -> None:
        if self.context is None:
            return
        if self.mutation_blocked_reason:
            self.set_message(self.mutation_blocked_reason, "error")
            return
        if self.installing or self.bulk_active:
            self.set_message("Install new waits until the current activity finishes", "warn")
            return
        self.input_mode = "new_package"
        self.input_buffer = ""
        self.message = "Install new package"

    def submit_new_package_name(self) -> None:
        raw = self.input_buffer.strip()
        self.input_mode = None
        self.input_buffer = ""
        if not raw:
            self.message = "Install new cancelled"
            return
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", raw):
            self.set_message(f"Invalid package name: {raw}", "error")
            return
        existing = self.find_row(raw)
        if existing is not None:
            self.filter_text = ""
            self.update_view()
            self.focus_on_package(existing.name)
            self.set_message(f"{existing.display_name} is already installed; use arrows or F4 to change versions", "warn")
            return
        context = self.context
        if context is None:
            return
        if self.version_loading:
            self.set_message("Version lookup already in progress", "warn")
            return
        self.version_loading = True
        self.version_lookup_token += 1
        self.message = f"Loading versions for {raw}"
        self.spawn(partial(self.version_lookup_worker, self.version_lookup_token, raw, context, self.on_new_versions_done))

    def on_new_versions_done(self, token: int, display_name: str, versions: list[str], yanked: set[str], error: str | None) -> None:
        if token != self.version_lookup_token:
            return
        self.version_loading = False
        if not versions:
            self.set_message(f"No installable versions for {display_name}: {last_lines(error or 'no versions found', 1)}", "error")
            return
        ordered = sorted(set(versions), key=version_key, reverse=True)
        self.new_package = NewPackage(display_name, ordered, set(yanked))
        self.version_overlay = True
        self.version_overlay_row = None
        self.version_overlay_scroll = 0
        self.version_options = ordered
        self.version_overlay_index = next((i for i, v in enumerate(ordered) if v not in yanked), 0)
        self.version_error = None
        self.pending_version_direction = None
        self.ensure_version_overlay_visible()
        self.message = f"Select a version for {display_name}"

    def handle_new_package_version_enter(self) -> None:
        info = self.new_package
        if info is None:
            self.version_overlay = False
            return
        selected = self.version_options[self.version_overlay_index]
        if selected in info.yanked:
            self.prompt = Prompt(
                title="Confirm yanked version",
                message=self.yanked_warning(info.display_name, selected),
                on_yes=partial(self.install_new_selected, selected),
                on_no=partial(self.set_message, "Install cancelled"),
            )
            return
        self.install_new_selected(selected)

    def install_new_selected(self, version: str) -> None:
        context = self.context
        info = self.new_package
        self.version_overlay = False
        self.new_package = None
        if context is None or info is None or not info.display_name:
            return
        if not self.ensure_uv_provider(context, partial(self.begin_install_new, info.display_name, version)):
            return
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            self.prompt = Prompt(
                title="Confirm interpreter install",
                message=f"Install {info.display_name}=={version} into interpreter {context.python_path}? y/N",
                on_yes=partial(self.confirm_interpreter_and_install_new, context, info.display_name, version),
                on_no=partial(self.set_message, "Install cancelled"),
            )
            return
        self.begin_install_new(info.display_name, version)

    def confirm_interpreter_and_install_new(self, context: PythonContext, display_name: str, version: str) -> None:
        context.confirmed_for_mutation = True
        self.begin_install_new(display_name, version)

    def begin_install_new(self, display_name: str, version: str) -> None:
        context = self.context
        if context is None:
            return
        if self.installing:
            self.set_message("Install new waits until the current activity finishes", "warn")
            return
        try:
            command = self.mutation_command(context, "install")
        except Exception as exc:
            self.set_message(f"uv provider unavailable: {last_lines(str(exc), 1)}", "error")
            return
        self.message = f"Installing {display_name}=={version}"
        self.run_package_operation(
            context,
            [*command, f"{display_name}=={version}"],
            operation="install",
            package_name=canonicalize_name(display_name),
            display_name=display_name,
            target_version=version,
        )

    # ----------------------------------------------------------------- bulk update

    def start_bulk_update(self) -> None:
        if self.mutation_blocked_reason:
            self.set_message(self.mutation_blocked_reason, "error")
            return
        if self.installing:
            self.set_message("Update all waits until the current activity finishes", "warn")
            return
        if self.version_resolution_busy():
            self.set_message("Update all waits for version resolution", "warn")
            return
        context = self.context
        if context is None or not self.ensure_uv_provider(context, self.start_bulk_update):
            return
        if self.selected_names:
            source_rows = [row for row in self.rows if row.name in self.selected_names]
            scope = "selected"
        else:
            source_rows = list(self.view)
            scope = "filtered" if self.filter_text else "ready"
        pinned = self.pinned_names()
        seen: set[str] = set()
        queue_rows: list[PackageRow] = []
        skipped_pinned = 0
        for row in source_rows:
            if row.name in seen or row.status != "ready" or not self.row_target_installable(row):
                continue
            seen.add(row.name)
            if row.name in pinned:
                skipped_pinned += 1
            else:
                queue_rows.append(row)
        pinned_note = f"({skipped_pinned} pinned package(s) excluded)" if skipped_pinned else ""
        if not queue_rows:
            note = f" ({skipped_pinned} pinned excluded)" if skipped_pinned else ""
            self.set_message(f"No {scope} packages to update{note}", "warn")
            return
        lines = [f"{row.display_name}  {row.installed_version} -> {row.target_version}" for row in queue_rows[:12]]
        if len(queue_rows) > 12:
            lines.append(f"...and {len(queue_rows) - 12} more")
        if pinned_note:
            lines.append(pinned_note)
        title = "Confirm update all"
        header = f"Install updates for {len(queue_rows)} {scope} package(s)? y/N"
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            title = "Confirm interpreter update all"
            header = f"Install updates for {len(queue_rows)} {scope} package(s) into interpreter {context.python_path}? y/N"
        items = [(row.name, row.target_version) for row in queue_rows]
        self.prompt = Prompt(
            title=title,
            message="\n".join([header, "", *lines]),
            on_yes=partial(self.confirm_bulk_update, items, context),
            on_no=partial(self.set_message, "Update all cancelled"),
        )

    def confirm_bulk_update(self, queue_items: list[tuple[str, str]], confirmed_context: PythonContext) -> None:
        context = self.context
        if context is not None and context.id == confirmed_context.id and context.type == "interpreter":
            context.confirmed_for_mutation = True
        self.bulk_active = True
        self.bulk_queue = list(queue_items)
        self.bulk_total = len(queue_items)
        self.bulk_processed = set()
        self.bulk_failed_results = {}
        self.bulk_summary = []
        self.selected_names = set()
        self.mark_bulk_pending_waits()
        self.message = f"Updating {len(queue_items)} packages"
        self.continue_bulk_update()

    def mark_bulk_pending_waits(self) -> None:
        pending = dict(self.bulk_queue)
        for row in self.rows:
            target = pending.get(row.name)
            if target is not None and row.installed_version != target and row.status not in {"installing", "failed"}:
                row.status = "wait"

    def continue_bulk_update(self) -> None:
        if self.installing or not self.bulk_active:
            return
        while self.bulk_queue:
            package_name, target_version = self.bulk_queue.pop(0)
            row = self.find_row(package_name)
            if row is None:
                self.bulk_processed.add(package_name)
                self.bulk_summary.append((package_name, "skipped", "package vanished"))
                continue
            if package_name in self.bulk_processed:
                if row.status != "failed":
                    row.status = "skipped"
                continue
            self.bulk_processed.add(package_name)
            if row.installed_version == target_version:
                if row.status != "failed":
                    row.status = "skipped"
                self.bulk_summary.append((row.display_name, "skipped", "already current"))
                continue
            if row.status in {"failed", "installing"}:
                self.bulk_summary.append((row.display_name, "skipped", f"status: {row.status}"))
                continue
            if not row.versions_resolved or target_version not in row.candidate_versions:
                row.status = "skipped"
                self.bulk_summary.append((row.display_name, "skipped", "target no longer available"))
                continue
            row.target_version = target_version
            # The between-items refresh rebuilds rows as "loading", which begin_install
            # rejects; every gate it checks has passed above, so restate the queued state.
            row.status = "wait"
            self.mark_bulk_pending_waits()
            self.message = f"Updating {self.bulk_total - len(self.bulk_queue)}/{self.bulk_total}: {row.display_name}=={target_version}"
            if self.begin_install(row):
                return
            row.status = "skipped"
            self.bulk_summary.append((row.display_name, "skipped", "could not start install"))
        self.finalize_bulk_update()

    def record_bulk_outcome(self, result: InstallResult) -> None:
        if not self.bulk_active or result.operation != "install":
            return
        if result.cancelled:
            self.bulk_summary.append((result.label, "cancelled", "cancelled by user"))
        elif result.ok:
            self.bulk_summary.append((result.label, "updated", f"-> {result.target_version}"))
        else:
            self.bulk_summary.append((result.label, "failed", f"exit {result.exit_text if result.exit_code is not None else 'did not start'}"))

    def finalize_bulk_update(self) -> None:
        summary = self.bulk_summary
        self.bulk_active = False
        self.bulk_processed = set()
        self.bulk_failed_results = {}
        self.bulk_total = 0
        self.bulk_summary = []
        for row in self.rows:
            if row.status == "skipped":
                row.status = self.natural_row_status(row)
        counts = {outcome: sum(1 for _n, o, _d in summary if o == outcome) for outcome in ("updated", "failed", "skipped", "cancelled")}
        if counts["failed"] or counts["skipped"] or counts["cancelled"]:
            lines = [
                f"Updated: {counts['updated']}   Failed: {counts['failed']}   Skipped: {counts['skipped']}   Cancelled: {counts['cancelled']}",
                "",
                *(f"{outcome:<10} {name}  {note}" for name, outcome, note in summary),
                "",
                "Failed rows keep their error details; press F3 on a row for specifics.",
            ]
            self.open_report("Update all summary", lines)
            self.set_message(
                f"Update all finished: {counts['updated']} updated, {counts['failed']} failed, {counts['skipped']} skipped",
                "error" if counts["failed"] else "warn",
            )
        else:
            self.message = f"Update all complete: {counts['updated']} updated"
        context = self.context
        if context is not None:
            self.start_health_check(context, self.refresh_generation)
        self.maybe_start_waiting_install()

    # ----------------------------------------------------------------- venv creation, pins, selection

    def request_create_venv(self) -> None:
        if self.creating_venv:
            self.set_message("Venv creation is already in progress", "warn")
            return
        self.context_overlay = False
        self.input_mode = "new_venv"
        self.input_buffer = ".venv"
        self.message = "Create venv in current directory"

    def submit_create_venv(self) -> None:
        raw = self.input_buffer.strip().strip('"')
        self.input_mode = None
        self.input_buffer = ""
        if not raw:
            self.message = "Create venv cancelled"
            return
        if re.search(r'[<>:"|?*/\\]', raw) or raw in {".", ".."}:
            self.set_message(f"Invalid venv directory name: {raw}", "error")
            return
        root = Path.cwd() / raw
        if root.exists():
            self.set_message(f"Directory already exists: {root}", "error")
            return
        base = next((context for context in self.contexts if context.type == "interpreter"), None)
        python_path = base.python_path if base is not None else runner_python_path()
        if not Path(python_path).is_file():
            self.set_message("No interpreter is available to create a venv", "error")
            return
        self.creating_venv = True
        self.message = f"Creating venv {root} (Python {base.version if base else 'runner'})"
        command = [str(python_path), "-m", "venv", str(root)]

        def worker() -> None:
            try:
                proc = run_command(command, timeout=600)
                error = None if proc.returncode == 0 else failed_command_detail(command, proc)
            except Exception as exc:
                error = str(exc)
            self.post(partial(self.on_venv_created, str(root), error))

        self.spawn(worker)

    def on_venv_created(self, root: str, error: str | None) -> None:
        self.creating_venv = False
        if error:
            self.set_message(f"Venv creation failed: {last_lines(error, 2)}", "error")
            self.open_report("Venv creation failed", [root, "", *error.splitlines()])
            return
        self.message = f"Created venv: {root}"
        self.pending_select_root = root
        self.start_context_discovery(preserve_current=True)

    def toggle_pin(self) -> None:
        row = self.focused_row()
        context = self.context
        if row is None or context is None or row.operational_error:
            return
        should_pin = row.name not in self.pinned_names()
        updated, error = update_pinned_package(context.id, row.name, should_pin)
        if error:
            self.set_message(error, "error")
            return
        self.pinned_by_context = updated
        self.message = f"Pinned {row.display_name} (excluded from update all)" if should_pin else f"Unpinned {row.display_name}"

    def toggle_selection(self) -> None:
        row = self.focused_row()
        if row is None or row.operational_error:
            return
        self.selected_names ^= {row.name}
        count = len(self.selected_names)
        self.message = f"Selected {count} package(s)" if count else "Selection cleared"
        self.move_focus(1)

    # ----------------------------------------------------------------- rendering

    def render(self) -> str:
        width, height = self.terminal.size()
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            message = f"Terminal too small ({width}x{height}); Tuv needs at least {MIN_WIDTH}x{MIN_HEIGHT}."
            return "\n".join([truncate(message, max(1, width))] + [" " * max(1, width)] * max(0, height - 1))
        visible_rows = max(1, height - 6)
        lines = [self.header_line(width), "─" * width, self.table_header(width)]
        for index in range(self.scroll, self.scroll + visible_rows):
            lines.append(self.render_row(width, index, self.view[index]) if index < len(self.view) else " " * width)
        lines += ["─" * width, self.status_line(width), self.footer_line(width)]
        text_input_open = self.input_mode in {"new_package", "new_venv"}
        if self.context_overlay or self.version_overlay or self.info_open or self.report_open or self.prompt or text_input_open:
            lines = [MODAL_BACKDROP + fit(strip_ansi(line), width) + RESET for line in lines]
        if self.context_overlay:
            lines = self.overlay_contexts(lines, width, height)
        if self.version_overlay:
            lines = self.overlay_versions(lines, width, height)
        if self.info_open:
            lines = self.overlay_info(lines, width, height)
        if self.report_open:
            body = [MODAL_CLOSE_HINT, "", *(self.report_lines or ["(empty)"])]
            lines = self.overlay_text(lines, width, height, self.report_title or "Report", body, scroll_attr="report_scroll")
        if text_input_open:
            lines = self.overlay_input(lines, width, height)
        if self.prompt:
            body = [*self.prompt.message.splitlines(), "", "Y: yes    Enter/N/Esc/q: no"]
            lines = self.overlay_text(lines, width, height, self.prompt.title, body)
        return "\n".join(lines[:height])

    def header_line(self, width: int) -> str:
        context = self.context
        if context is None:
            label = self.discovery_error or ("Discovering Python contexts..." if self.discovering_contexts else "Starting...")
            return fit(f"[ {label} ]", width)
        badge = f"[{self.health_badge()}]"
        label_width = max(1, width - display_width(badge) - 1)
        return fit(f"[ {context.label} [{provider_label(context.resolved_uv_provider)}] ]", label_width) + " " + badge

    def health_badge(self) -> str:
        if self.health_status == "issues":
            return f"health: {self.health_issue_count} issue(s)" if self.health_issue_count is not None else "health: issues"
        return {"checking": "health: checking", "healthy": "health: ok", "error": "health: check failed"}.get(self.health_status, "health: unknown")

    def status_line(self, width: int) -> str:
        if self.input_mode == "filter":
            return REVERSE + fit(f"Filter: {self.input_buffer}_  (Enter keep, Esc clear)", width) + RESET
        extras: list[str] = []
        if self.bulk_active and self.bulk_total:
            extras.append(f"update {self.bulk_total - len(self.bulk_queue)}/{self.bulk_total}")
        if self.wait_queue:
            extras.append(f"{len(self.wait_queue)} queued")
        if self.selected_names:
            extras.append(f"{len(self.selected_names)} selected")
        if self.filter_text:
            extras.append(f"filter: {self.filter_text}")
        text = (f"{SPINNER[self.spinner_index]} " if self.busy() else "  ") + self._message
        if extras:
            text = f"{text}  [{' | '.join(extras)}]"
        line = fit(text, width)
        style = {"error": BOLD_RED, "warn": YELLOW}.get(self._message_kind, "")
        return style + line + RESET if style else line

    def table_header(self, width: int) -> str:
        name_w, installed_w, target_w, action_w = table_columns(width)
        text = pad_display("Package", name_w) + pad_display("Installed", installed_w) + pad_display("Target", target_w) + pad_display("Action", action_w)
        return fit(text, width)

    def render_row(self, width: int, index: int, row: PackageRow) -> str:
        name_w, installed_w, target_w, action_w = table_columns(width)
        selected_marker = "+" if row.name in self.selected_names else " "
        safe_marker = "*" if row.metadata_trusted and row.uninstall_safe else " "
        text = (
            fit(f"{selected_marker}{safe_marker} {row.display_name}", name_w)
            + fit(row.installed_version, installed_w)
            + fit(row.target_version, target_w)
            + fit(self.display_status(row), action_w)
        )
        line = fit(text, width)
        hint = row.color_hint if row.status == "loading" else None
        style = REVERSE if index == self.focus_index else ""
        if row.status == "failed":
            style += BOLD_RED
        elif row.updated_in_session or hint == "updated":
            style += BRIGHT_CYAN
        elif row.status == "current" or hint == "current":
            style += LIGHT_GREEN
        elif row.is_outdated or hint == "outdated":
            style += YELLOW
        elif row.status in {"loading", "nodata"}:
            style += DIM
        return style + line + RESET if style else line

    def display_status(self, row: PackageRow) -> str:
        if row.status == "installing":
            return SPINNER[self.spinner_index]
        if row.status == "nodata":
            return "no data"
        if row.name in self.pinned_names() and row.status in {"ready", "current", "loading", "nodata"}:
            return "pinned"
        return row.status

    def footer_line(self, width: int) -> str:
        if width >= 130:
            keys = (
                "↑/↓ | ←/→ Ver | ↵ Install | Space Sel | / Filter | i Info | n New | Del Unins | p Pin"
                " | F2 Update | F3 Info | F4 Ver | F5 Rescan | F6 Health | F9 Ctx | F10 Quit"
            )
        elif width >= 100:
            keys = "↑/↓ | ←/→ | ↵ Inst | Spc Sel | / Filt | i Info | n New | Del | p Pin | F2 All | F3 | F4 | F5 | F6 | F9 | F10"
        elif width >= 72:
            keys = "↑/↓ | ←/→ | ↵ | Spc | / | i | n | Del | p | F2 | F3 | F4 | F5 | F6 | F9 | F10"
        else:
            keys = "↑/↓ ←/→ ↵ Spc / i n Del p F2-F10"
        return fit(keys, width)

    def overlay_contexts(self, lines: list[str], width: int, height: int) -> list[str]:
        overlay_w = min(width - 4, max(50, width * 3 // 4))
        # Use the full terminal height when necessary so every context that fits is shown.
        overlay_h = min(height, max(7, len(self.contexts) + 3))
        items_visible = overlay_h - 3
        count = len(self.contexts)
        if self.context_overlay_index < self.context_overlay_scroll:
            self.context_overlay_scroll = self.context_overlay_index
        elif self.context_overlay_index >= self.context_overlay_scroll + items_visible:
            self.context_overlay_scroll = self.context_overlay_index - items_visible + 1
        self.context_overlay_scroll = max(0, min(self.context_overlay_scroll, max(0, count - items_visible)))
        start = self.context_overlay_scroll
        title = "Context selector"
        if count > items_visible:
            title += f" ({start + 1}-{min(start + items_visible, count)}/{count})"
        elif self.discovering_contexts:
            title += " (discovering...)"
        box = [box_border(overlay_w, "top", title), box_line("Enter select | n new venv | F5 rescan | Esc/q close", overlay_w)]
        for index in range(start, start + items_visible):
            label = ""
            if index < count:
                context = self.contexts[index]
                marker = ">" if index == self.context_overlay_index else " "
                label = f"{marker} {context.label} [{provider_label(context.resolved_uv_provider)}]"
            content = box_line(label, overlay_w)
            box.append(REVERSE + content + RESET if index == self.context_overlay_index else content)
        box.append(box_border(overlay_w, "bottom"))
        return paste_box(lines, box, max(0, (height - overlay_h) // 2), max(1, (width - overlay_w) // 2), width)

    def overlay_versions(self, lines: list[str], width: int, height: int) -> list[str]:
        if self.new_package is not None:
            row = None
            title = f"Versions: {self.new_package.display_name} (new install)"
            yanked = self.new_package.yanked
        else:
            row = self.find_row(self.version_overlay_row or "")
            title = f"Versions: {row.display_name if row else ''}".strip()
            yanked = row.yanked_versions if row else set()
        overlay_w = min(width - 4, max(46, width // 2))
        overlay_h = min(height - 4, max(8, min(len(self.version_options) + 5, height - 4)))
        items_visible = overlay_h - 3
        self.ensure_version_overlay_visible(items_visible)
        start = self.version_overlay_scroll
        if self.version_loading:
            hint = "Loading versions... | Esc/q close"
        elif self.version_error:
            hint = f"{self.version_error} | Esc/q close"
        else:
            hint = "Enter install | Esc/q close"
        box = [box_border(overlay_w, "top", title), box_line(hint, overlay_w)]
        for index in range(start, start + items_visible):
            label = ""
            if index < len(self.version_options):
                version = self.version_options[index]
                marker = ">" if index == self.version_overlay_index else " "
                tags = (" (yanked)" if version in yanked else "") + (" installed" if row and version == row.installed_version else "")
                tags += " target" if row and version == row.target_version else ""
                label = f"{marker} {version}{tags}"
            content = box_line(label, overlay_w)
            box.append(REVERSE + content + RESET if index == self.version_overlay_index else content)
        box.append(box_border(overlay_w, "bottom"))
        return paste_box(lines, box, max(1, (height - overlay_h) // 2), max(1, (width - overlay_w) // 2), width)

    @staticmethod
    def package_relation_lines(row: PackageRow) -> list[str]:
        if not row.metadata_trusted:
            unavailable = "  (metadata unavailable)"
            return ["", "Description:", unavailable, "", "Dependency packages:", unavailable, "", "Usage packages:", unavailable]
        return [
            "",
            "Description:",
            f"  {row.description or '(empty)'}",
            "",
            "Dependency packages:",
            *([f"  {name}" for name in row.dependency_packages] or ["  (none)"]),
            "",
            "Usage packages:",
            *([f"  {name}" for name in row.usage_packages] or ["  (none)"]),
        ]

    def package_tree_lines(self, root: PackageRow, invert: bool) -> list[str]:
        if not root.metadata_trusted:
            return ["Loading installed package metadata..."] if self.dependency_loading else ["Dependency metadata is unavailable for this context."]
        rows_by_name = {row.name: row for row in self.rows}
        expanded = {root.name}
        output = [self.package_tree_label(root)]
        repeated = False

        def append_children(row: PackageRow, prefix: str, ancestry: set[str]) -> None:
            nonlocal repeated
            names = row.usage_packages if invert else row.dependency_packages
            children: dict[str, str] = {}
            for name in names:
                children.setdefault(canonicalize_name(name), name)
            for index, (normalized, display_name) in enumerate(children.items()):
                is_last = index == len(children) - 1
                connector = "└── " if is_last else "├── "
                child = rows_by_name.get(normalized)
                if child is None:
                    output.append(prefix + connector + display_name + " (installed metadata missing)")
                    continue
                label = self.package_tree_label(child)
                if child.name in ancestry:
                    output.append(prefix + connector + label + " (cycle)")
                elif child.name in expanded:
                    output.append(prefix + connector + label + " (*)")
                    repeated = True
                else:
                    output.append(prefix + connector + label)
                    expanded.add(child.name)
                    append_children(child, prefix + ("    " if is_last else "│   "), {*ancestry, child.name})

        append_children(root, "", {root.name})
        if repeated:
            output += ["", "(*) Package tree already displayed"]
        return output

    @staticmethod
    def package_tree_label(row: PackageRow) -> str:
        label = f"{row.display_name} v{row.installed_version}"
        if row.target_version != row.installed_version and row.versions_resolved:
            label += f" (latest: v{row.target_version})"
        return label

    def overlay_info(self, lines: list[str], width: int, height: int) -> list[str]:
        row = self.focused_row()
        title = f"Information - {INFO_TABS[self.info_tab]}"
        if row is None:
            body = ["No package selected."]
        elif row.operational_error:
            body = ["Tuv operational error", "", row.last_error or row.display_name, "", f"Runner: {runner_python_path()}", "", *(row.last_error_detail or "").splitlines()]
        elif self.info_tab in {1, 2}:
            invert = self.info_tab == 2
            heading = "Reverse dependency tree" if invert else "Dependency tree"
            body = [f"Package: {row.display_name}", f"Version: {row.installed_version}", VIEWS_HINT, "", f"{heading}:", *self.package_tree_lines(row, invert)]
        elif row.status == "failed" and row.last_error_detail:
            result = row.last_install_result
            body = [
                VIEWS_HINT,
                "",
                f"Package: {row.display_name}",
                f"Version: {result.installed_version_at_attempt if result else row.installed_version}",
                f"Exit code: {result.exit_text if result else 'process did not start'}",
                f"Elapsed: {f'{result.elapsed:.1f}s' if result else 'unknown'}",
                *self.package_relation_lines(row),
                "",
                row.last_error or "Install failed",
                "",
                *row.last_error_detail.splitlines(),
            ]
        else:
            pinned = "yes (excluded from update all)" if row.name in self.pinned_names() else "no"
            body = [VIEWS_HINT, "", f"Package: {row.display_name}", f"Version: {row.installed_version}", f"Pinned: {pinned}", *self.package_relation_lines(row)]
        return self.overlay_text(lines, width, height, title, [MODAL_CLOSE_HINT, "", *body], scroll_attr="info_scroll")

    def overlay_input(self, lines: list[str], width: int, height: int) -> list[str]:
        if self.input_mode == "new_package":
            title, prompt_text = "Install new package", "Package name:"
        else:
            title, prompt_text = "Create venv in current directory", f"Directory name (created in {Path.cwd()}):"
        body = [prompt_text, f"> {self.input_buffer}_", "", "Enter: confirm    Esc: cancel"]
        return self.overlay_text(lines, width, height, title, body)

    def overlay_text(self, lines: list[str], width: int, height: int, title: str, body: list[str], scroll_attr: str | None = None) -> list[str]:
        overlay_w = min(width - 4, max(50, width * 4 // 5))
        wrapped: list[str] = []
        for line in body:
            line = sanitize_terminal_text(line)
            wrapped += textwrap.wrap(line, width=max(10, overlay_w - 4)) or [""]
        overlay_h = min(height - 4, max(6, min(len(wrapped) + 3, height - 4)))
        visible = overlay_h - 2
        scroll = 0
        if scroll_attr is not None:
            scroll = min(max(0, int(getattr(self, scroll_attr, 0))), max(0, len(wrapped) - visible))
            setattr(self, scroll_attr, scroll)
            if len(wrapped) > visible:
                title = f"{title} ({scroll + 1}-{min(scroll + visible, len(wrapped))}/{len(wrapped)})"
        box = [box_border(overlay_w, "top", title)]
        box += [box_line(line, overlay_w) for line in wrapped[scroll : scroll + visible]]
        box += [box_line("", overlay_w)] * (overlay_h - 1 - len(box))
        box.append(box_border(overlay_w, "bottom"))
        return paste_box(lines, box, max(1, (height - overlay_h) // 2), max(1, (width - overlay_w) // 2), width)


# --------------------------------------------------------------------------- entry point


def main() -> int:
    if "--prepare-runner" in sys.argv:
        mode = "default"
        if "--launcher-mode" in sys.argv:
            try:
                mode = sys.argv[sys.argv.index("--launcher-mode") + 1]
            except IndexError:
                print("--launcher-mode requires a value", file=sys.stderr)
                return 1
        return print_prepare_runner(mode)
    if "--version" in sys.argv:
        print(f"tuv {TUV_VERSION}")
        return 0
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            "Usage: tuv.py [--version]\n\n"
            "Start the Tuv alternate-screen package manager.\n"
            "Run through tuv.bat / tuv.sh to bootstrap the runner environment;\n"
            "pass '.' to the launcher to use the current directory's Python as runner."
        )
        return 0
    if RUNNER_IMPORT_ERROR is not None:
        print(f"Tuv runner requirements are missing ({RUNNER_IMPORT_ERROR}); start Tuv through tuv.bat or tuv.sh.", file=sys.stderr)
        return 1
    try:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        interactive = False
    if not interactive:
        print("Tuv needs an interactive terminal (stdin and stdout must be a TTY).", file=sys.stderr)
        return 1
    try:
        return TuvApp().run()
    except Exception:
        import traceback

        print("Tuv crashed before the terminal UI could recover:", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

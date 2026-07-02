#!/usr/bin/env python3
"""Tuv: a native alternate-screen terminal UI for uv-managed packages."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

try:
    import tomllib
except Exception:  # pragma: no cover - Python < 3.11 falls back to regex scanning.
    tomllib = None  # type: ignore

try:
    from packaging.markers import default_environment
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import (
        InvalidSdistFilename,
        InvalidWheelFilename,
        canonicalize_name,
        parse_sdist_filename,
        parse_wheel_filename,
    )
    from packaging.version import InvalidVersion, Version
except Exception:  # pragma: no cover - runner requirements should provide packaging.
    canonicalize_name = lambda value: re.sub(r"[-_.]+", "-", value).lower()  # type: ignore
    Requirement = None  # type: ignore
    InvalidRequirement = Exception  # type: ignore
    InvalidSdistFilename = Exception  # type: ignore
    InvalidWheelFilename = Exception  # type: ignore
    parse_sdist_filename = None  # type: ignore
    parse_wheel_filename = None  # type: ignore
    default_environment = None  # type: ignore
    Version = None  # type: ignore
    InvalidVersion = Exception  # type: ignore


IS_WINDOWS = os.name == "nt"
TUV_HOME = Path(os.environ.get("TUV_HOME", Path(__file__).resolve().parent)).resolve()
RUNNER_VENV = Path(os.environ.get("TUV_RUNNER_VENV", TUV_HOME / ".tuv-venv")).resolve()

ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
CLEAR = "\x1b[2J"
HOME = "\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
RESET = "\x1b[0m"
BOLD_RED = "\x1b[1;31m"
WHITE = "\x1b[97m"
BRIGHT_CYAN = "\x1b[96m"
LIGHT_GREEN = "\x1b[92m"
YELLOW = "\x1b[33m"
REVERSE = "\x1b[7m"
DIM = "\x1b[2m"
MODAL_BACKDROP = "\x1b[2;90m"
BOLD = "\x1b[1m"
SPINNER = "-\\|/"
MIN_WIDTH = 40
MIN_HEIGHT = 8
RUNNER_VENV_KEEP = 4


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
    source: str = "installed"

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)


@dataclass
class UvProvider:
    type: str
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
    type: str
    source: str
    label: str
    python_path: Path
    reference_python_path: Path | None
    root_path: Path | None
    version: str
    resolved_uv_provider: UvProvider | None = None
    confirmed_for_mutation: bool = False

    @property
    def uv_target(self) -> str:
        if self.type in {"tuv", "venv"} and self.root_path is not None:
            return str(self.root_path)
        return str(self.python_path)

    @property
    def is_virtual(self) -> bool:
        return self.type in {"tuv", "venv"}

    @property
    def uv_manageable(self) -> bool:
        return self.resolved_uv_provider is not None


@dataclass
class PackageRow:
    name: str
    display_name: str
    uninstall_safe: bool
    installed_version: str
    target_version: str
    candidate_versions: list[str]
    status: str
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
    last_install_result: "InstallResult | None" = None
    operational_error: bool = False

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
    installed_version_at_attempt: str = ""
    exit_code: int | None = None
    stdout_tail: list[str] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    failed_in_bulk_run_id: str | None = None
    candidate_versions_at_attempt: list[str] = field(default_factory=list)
    operation: str = "install"
    display_name: str = ""
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def requested_version(self) -> str:
        return self.target_version


@dataclass
class Prompt:
    title: str
    message: str
    on_yes: Callable[[], None]
    on_no: Callable[[], None] | None = None


@dataclass
class PackageMetadata:
    description: str | None = None
    dependencies: set[str] = field(default_factory=set)
    extras: set[str] = field(default_factory=set)


def version_key(value: str) -> tuple[int, object]:
    if Version is not None:
        try:
            return (0, Version(value))
        except InvalidVersion:
            pass
    # Homogeneous (tag, value) chunks keep the fallback tuples comparable even
    # when digit and letter chunks are misaligned between two version strings.
    parts: list[tuple[int, object]] = []
    for chunk in re.split(r"([0-9]+)", value):
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        elif chunk:
            parts.append((1, chunk.lower()))
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


def uv_version_text(output: str) -> str:
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    return first_line or "unknown"


_UV_VALIDATION_CACHE: dict[tuple[str, ...], str] = {}
_UV_VALIDATION_LOCK = threading.Lock()


def validate_uv_command(command: list[str], timeout: float = 5.0) -> str | None:
    key = tuple(command)
    with _UV_VALIDATION_LOCK:
        cached = _UV_VALIDATION_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        proc = run_command([*command, "--version"], timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    version = uv_version_text(proc.stdout or proc.stderr)
    with _UV_VALIDATION_LOCK:
        _UV_VALIDATION_CACHE[key] = version
    return version


def invalidate_uv_validation_cache() -> None:
    with _UV_VALIDATION_LOCK:
        _UV_VALIDATION_CACHE.clear()


def path_key(path: Path) -> str:
    resolved = path.resolve()
    return str(resolved).lower() if IS_WINDOWS else str(resolved)


def resolve_file_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    text = str(value).strip().strip('"')
    if not text:
        return None
    path = Path(text)
    if not path.is_file():
        resolved = shutil.which(text)
        if not resolved:
            return None
        path = Path(resolved)
    try:
        return path.resolve()
    except OSError:
        return None


def possible_venv_root_for_python(executable: Path) -> Path | None:
    parent = executable.parent
    if parent.name.lower() in {"bin", "scripts"}:
        root = parent.parent
        if (root / "pyvenv.cfg").is_file():
            return root.resolve()
    if (parent / "pyvenv.cfg").is_file():
        return parent.resolve()
    return None


def python_info_is_venv(info: PythonInfo) -> bool:
    if info.prefix and info.base_prefix:
        try:
            if path_key(Path(info.prefix)) != path_key(Path(info.base_prefix)):
                return True
        except Exception:
            if info.prefix != info.base_prefix:
                return True
    return possible_venv_root_for_python(info.executable) is not None


def runner_python_path() -> Path:
    env_path = resolve_file_path(os.environ.get("TUV_RUNNER_PYTHON"))
    if env_path is not None:
        return env_path
    return venv_python(RUNNER_VENV).resolve()


def python_uv_provider(provider_type: str, python_path: Path | None, priority: int) -> UvProvider | None:
    if python_path is None or not python_path.is_file():
        return None
    try:
        python_path = python_path.resolve()
    except OSError:
        return None
    version = validate_uv_command([str(python_path), "-m", "uv"])
    if not version:
        return None
    return UvProvider(provider_type, None, python_path, priority, version)


def standalone_uv_provider(priority: int = 3) -> UvProvider | None:
    candidates: list[str | Path] = []
    env_value = os.environ.get("TUV_SYSTEM_UV_EXE")
    if env_value:
        candidates.append(env_value)
    resolved = shutil.which("uv")
    if resolved:
        candidates.append(resolved)
    seen: set[str] = set()
    for candidate in candidates:
        path = resolve_file_path(candidate)
        if path is None:
            continue
        key = path_key(path)
        if key in seen:
            continue
        seen.add(key)
        version = validate_uv_command([str(path)])
        if version:
            return UvProvider("standalone", path, None, priority, version)
    return None


def runner_uv_provider(priority: int = 4) -> UvProvider | None:
    return python_uv_provider("tuv", runner_python_path(), priority)


def provider_label(provider: UvProvider | None) -> str:
    if provider is None:
        return "unavailable"
    labels = {
        "context_venv": "venv uv",
        "reference_python": "ref uv",
        "standalone": "system uv",
        "tuv": "tuv uv",
    }
    return labels.get(provider.type, provider.type)


def resolve_uv_provider(context: PythonContext) -> UvProvider | None:
    checked_python: set[str] = set()

    if context.is_virtual and context.type != "tuv":
        checked_python.add(path_key(context.python_path))
        provider = python_uv_provider("context_venv", context.python_path, 1)
        if provider is not None:
            return provider

    reference = context.reference_python_path
    if context.type == "interpreter":
        reference = context.python_path
    if reference is not None:
        key = path_key(reference)
        if key not in checked_python:
            checked_python.add(key)
            provider = python_uv_provider("reference_python", reference, 2)
            if provider is not None:
                return provider

    provider = standalone_uv_provider()
    if provider is not None:
        return provider

    return runner_uv_provider()


def refresh_context_uv_provider(context: PythonContext) -> UvProvider | None:
    context.resolved_uv_provider = resolve_uv_provider(context)
    return context.resolved_uv_provider


def uv_command(context: PythonContext, args: list[str]) -> list[str]:
    # Trust the provider resolved at discovery time; validating it would spawn
    # an extra `uv --version` subprocess before every command. A provider that
    # broke mid-session surfaces as a command failure, which re-resolves.
    provider = context.resolved_uv_provider or refresh_context_uv_provider(context)
    if provider is None:
        raise RuntimeError(
            "No uv provider is available. Tuv can install uv into the Tuv runner venv, "
            "but it will not install uv into the selected context."
        )
    return [*provider.command_prefix, *args]


def probe_python(executable: str | Path, timeout: float = 3.0, source: str = "installed") -> PythonInfo | None:
    path = Path(str(executable).strip().strip('"'))
    if not path:
        return None
    if not path.is_file():
        resolved = shutil.which(str(path))
        if not resolved:
            return None
        path = Path(resolved)
    code = (
        "import json, platform, sys; "
        "print(json.dumps({'version': sys.version_info[:3], "
        "'executable': sys.executable, 'prefix': sys.prefix, "
        "'base_prefix': sys.base_prefix, "
        "'base_executable': getattr(sys, '_base_executable', None), "
        "'implementation': sys.implementation.name, "
        "'architecture': platform.machine(), "
        "'os_name': sys.platform}))"
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
            base_executable=str(data.get("base_executable") or ""),
            implementation=str(data.get("implementation", "")),
            architecture=str(data.get("architecture", "")),
            os_name=str(data.get("os_name", "")),
            source=source,
        )
    except Exception:
        return None


def probe_runner_python(executable: str | Path, timeout: float = 5.0, source: str = "installed") -> PythonInfo | None:
    path = Path(str(executable).strip().strip('"'))
    if not path.is_file():
        resolved = shutil.which(str(path))
        if not resolved:
            return None
        path = Path(resolved)
    code = (
        "import json, platform, sys, venv; "
        "print(json.dumps({'version': sys.version_info[:3], "
        "'executable': sys.executable, 'prefix': sys.prefix, "
        "'base_prefix': sys.base_prefix, "
        "'base_executable': getattr(sys, '_base_executable', None), "
        "'implementation': sys.implementation.name, "
        "'architecture': platform.machine(), "
        "'os_name': sys.platform}))"
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
            base_executable=str(data.get("base_executable") or ""),
            implementation=str(data.get("implementation", "")),
            architecture=str(data.get("architecture", "")),
            os_name=str(data.get("os_name", "")),
            source=source,
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


def cwd_python_candidates(cwd: Path | None = None, allow_venv: bool = False) -> list[Path]:
    cwd = (cwd or Path.cwd()).resolve()
    if (cwd / "pyvenv.cfg").is_file():
        return [venv_python(cwd)] if allow_venv and venv_python(cwd).is_file() else []
    if IS_WINDOWS:
        relative_candidates = ["python.exe", "python3.exe", "Scripts/python.exe", "bin/python.exe"]
    else:
        relative_candidates = ["python", "python3", "bin/python", "bin/python3"]
    return [cwd / relative for relative in relative_candidates if (cwd / relative).is_file()]


def launcher_python_candidates() -> list[Path]:
    newest = resolve_file_path(os.environ.get("TUV_NEWEST_PYTHON"))
    return [newest] if newest is not None else []


def dedupe_candidates(candidates: Iterable[tuple[Path, str]]) -> list[tuple[Path, str]]:
    seen: set[str] = set()
    unique: list[tuple[Path, str]] = []
    for candidate, source in candidates:
        key = str(candidate).lower() if IS_WINDOWS else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append((candidate, source))
    return unique


def probe_candidates_parallel(
    candidates: list[tuple[Path, str]],
    probe: Callable[..., PythonInfo | None],
    max_workers: int = 8,
) -> list[PythonInfo | None]:
    if not candidates:
        return []
    workers = min(max_workers, len(candidates))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda item: probe(item[0], source=item[1]), candidates))


def discover_python_infos() -> list[PythonInfo]:
    installed_candidates = windows_python_candidates() if IS_WINDOWS else posix_python_candidates()
    raw_candidates = dedupe_candidates(
        [(candidate, "cwd") for candidate in cwd_python_candidates()]
        + [(candidate, "installed") for candidate in launcher_python_candidates()]
        + [(candidate, "installed") for candidate in installed_candidates]
    )
    probed = probe_candidates_parallel(raw_candidates, probe_python)
    seen: set[str] = set()
    infos: list[PythonInfo] = []
    by_key: dict[str, PythonInfo] = {}
    for (_candidate, source), info in zip(raw_candidates, probed):
        if info is None:
            continue
        if python_info_is_venv(info):
            continue
        key = str(info.executable).lower() if IS_WINDOWS else str(info.executable)
        if key in seen:
            if source == "cwd":
                by_key[key].source = "cwd"
            continue
        seen.add(key)
        by_key[key] = info
        infos.append(info)
    infos.sort(key=lambda item: item.version, reverse=True)
    return infos


def sorted_runner_infos(candidates: Iterable[Path], source: str, allow_venv: bool = False) -> list[PythonInfo]:
    unique = dedupe_candidates((candidate, source) for candidate in candidates)
    probed = probe_candidates_parallel(unique, probe_runner_python)
    seen: set[str] = set()
    infos: list[PythonInfo] = []
    for info in probed:
        if info is None:
            continue
        if not allow_venv and python_info_is_venv(info):
            continue
        key = path_key(info.executable)
        if key in seen:
            continue
        seen.add(key)
        infos.append(info)
    infos.sort(key=lambda item: item.version, reverse=True)
    return infos


def select_runner_python(mode: str) -> PythonInfo:
    cwd = Path.cwd()
    if mode == "cwd":
        infos = sorted_runner_infos(cwd_python_candidates(cwd, allow_venv=True), "cwd", allow_venv=True)
        if not infos:
            raise RuntimeError(f"No usable current-working-directory Python was found in {cwd}")
        return infos[0]

    platform_candidates = windows_python_candidates() if IS_WINDOWS else posix_python_candidates()
    platform_infos = sorted_runner_infos(platform_candidates, "installed")
    if platform_infos:
        return platform_infos[0]

    cwd_infos = sorted_runner_infos(cwd_python_candidates(cwd, allow_venv=False), "cwd")
    if cwd_infos:
        return cwd_infos[0]
    raise RuntimeError("No usable Python interpreter was found.")


def runner_compatibility_key(info: PythonInfo, mode: str) -> str:
    parts = [
        path_key(info.executable),
        info.version_text,
        info.implementation,
        info.architecture,
        info.os_name,
        mode,
    ]
    return "|".join(parts)


def runner_hash(key: str, length: int = 8) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:length]


def read_runner_state(root: Path) -> dict[str, str]:
    state = root / ".tuv-runner-state"
    data: dict[str, str] = {}
    try:
        for line in state.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                data[key.strip()] = value.strip()
    except OSError:
        pass
    return data


def write_runner_state(root: Path, info: PythonInfo, mode: str, key: str, hash_value: str) -> None:
    state = root / ".tuv-runner-state"
    lines = [
        f"base_python={info.executable}",
        f"base_version={info.version_text}",
        f"compat_key={key}",
        f"compat_hash={hash_value}",
        f"launcher_mode={mode}",
        f"timestamp={time.strftime('%Y%m%d%H%M%S', time.gmtime())}",
    ]
    state.write_text("\n".join(lines) + "\n", encoding="utf-8")


def runner_python_ok(python_path: Path) -> bool:
    if not python_path.is_file():
        return False
    code = (
        "import os, sys; "
        "base = getattr(sys, '_base_executable', '') or ''; "
        "raise SystemExit(0 if (not base or os.path.exists(base)) else 1)"
    )
    try:
        proc = run_command([str(python_path), "-c", code], timeout=10)
    except Exception:
        return False
    return proc.returncode == 0 and probe_python(python_path, timeout=5, source="tuv") is not None


def runner_pip_functional_or_repairable(python_path: Path) -> bool:
    if not python_path.is_file():
        return False
    try:
        pip_check = run_command([str(python_path), "-m", "pip", "--version"], timeout=15)
        if pip_check.returncode == 0:
            return True
    except Exception:
        pass
    try:
        ensure_check = run_command([str(python_path), "-c", "import ensurepip"], timeout=15)
    except Exception:
        return False
    return ensure_check.returncode == 0


def runner_venv_compatible(root: Path, key: str) -> bool:
    try:
        root = root.resolve()
    except OSError:
        return False
    if root.parent != TUV_HOME:
        return False
    if not (root.name.startswith("tuv-venv-") or root.name == ".tuv-venv"):
        return False
    state = read_runner_state(root)
    if state.get("compat_key") != key:
        return False
    base_python = state.get("base_python", "")
    if not base_python or not Path(base_python).is_file():
        return False
    python_path = venv_python(root)
    return runner_python_ok(python_path) and runner_pip_functional_or_repairable(python_path)


def runner_venv_candidates() -> list[Path]:
    candidates = list(TUV_HOME.glob("tuv-venv-*"))
    legacy = TUV_HOME / ".tuv-venv"
    if legacy.is_dir():
        candidates.append(legacy)
    return [path for path in candidates if path.is_dir()]


def find_compatible_runner_venv(key: str) -> Path | None:
    compatible: list[tuple[str, str, Path]] = []
    for root in runner_venv_candidates():
        if not runner_venv_compatible(root, key):
            continue
        state = read_runner_state(root)
        compatible.append((state.get("timestamp", ""), root.name, root.resolve()))
    if not compatible:
        return None
    compatible.sort(reverse=True)
    return compatible[0][2]


def new_runner_venv_path(key: str) -> tuple[Path, str]:
    hash_value = runner_hash(key)
    candidate = TUV_HOME / f"tuv-venv-{hash_value}"
    if not candidate.exists():
        return candidate, hash_value
    for counter in range(100):
        seed = f"{key}|{time.time_ns()}|{counter}"
        hash_value = runner_hash(seed)
        candidate = TUV_HOME / f"tuv-venv-{hash_value}"
        if not candidate.exists():
            return candidate, hash_value
    raise RuntimeError("Could not allocate a unique Tuv runner venv path")


def acquire_runner_lock(timeout: float = 30.0, stale_after: float = 600.0) -> Path | None:
    lock_path = TUV_HOME / ".tuv-runner.lock"
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii", "replace"))
            finally:
                os.close(fd)
            return lock_path
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_after:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.2)
        except OSError:
            return None


def release_runner_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


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
        if resolved == keep_root:
            continue
        state = read_runner_state(root)
        if not state.get("compat_key") or not (root / "pyvenv.cfg").is_file():
            continue
        entries.append((state.get("timestamp", ""), root.name, resolved))
    entries.sort(reverse=True)
    for _timestamp, _name, root in entries[max(0, keep_recent - 1):]:
        shutil.rmtree(root, ignore_errors=True)


def prepare_runner_environment(mode: str) -> tuple[PythonInfo, Path]:
    if mode not in {"default", "cwd"}:
        raise RuntimeError(f"Unsupported launcher mode: {mode}")
    info = select_runner_python(mode)
    key = runner_compatibility_key(info, mode)
    lock = acquire_runner_lock()
    try:
        runner = find_compatible_runner_venv(key)
        if runner is not None:
            hash_value = read_runner_state(runner).get("compat_hash", runner_hash(key))
            try:
                # Touch the state timestamp so GC keeps recently used runners alive.
                write_runner_state(runner, info, mode, key, hash_value)
            except OSError:
                pass
        else:
            runner, hash_value = new_runner_venv_path(key)
            proc = run_command([str(info.executable), "-m", "venv", str(runner)], timeout=None)
            if proc.returncode != 0:
                detail = command_detail(
                    [str(info.executable), "-m", "venv", str(runner)], proc.returncode, proc.stdout, proc.stderr, 0
                )
                raise RuntimeError(detail)
            write_runner_state(runner, info, mode, key, hash_value)
        if lock is not None:
            gc_runner_venvs(runner.resolve())
        return info, runner
    finally:
        release_runner_lock(lock)


def print_prepare_runner(mode: str) -> int:
    try:
        info, runner = prepare_runner_environment(mode)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"TUV_NEWEST_PYTHON={info.executable}")
    print(f"TUV_RUNNER_VENV={runner}")
    print(f"TUV_RUNNER_PYTHON={venv_python(runner)}")
    return 0


def read_pyvenv_home(root: Path) -> str | None:
    cfg = root / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().lower() == "home":
                home = value.strip()
                return home or None
    except OSError:
        return None
    return None


def python_paths_from_location(location: str | Path) -> list[Path]:
    path = Path(str(location).strip().strip('"'))
    if path.is_file():
        return [path]
    if IS_WINDOWS:
        names = ["python.exe", "python3.exe", "Scripts/python.exe"]
    else:
        names = ["python", "python3", "bin/python", "bin/python3"]
    return [path / name for name in names]


def resolve_venv_reference_python(root: Path, info: PythonInfo) -> Path | None:
    candidates: list[Path] = []
    if info.base_executable:
        candidates.append(Path(info.base_executable))
    home = read_pyvenv_home(root)
    if home:
        candidates.extend(python_paths_from_location(home))
    if info.base_prefix:
        candidates.extend(python_paths_from_location(info.base_prefix))

    seen: set[str] = {path_key(info.executable)}
    for candidate in candidates:
        probed = probe_python(candidate)
        if probed is None:
            continue
        key = path_key(probed.executable)
        if key in seen:
            continue
        return probed.executable
    return None


def context_from_venv(context_type: str, root: Path, label_prefix: str, source: str) -> PythonContext | None:
    if not is_venv(root):
        return None
    info = probe_python(venv_python(root))
    if info is None:
        return None
    label = f"{label_prefix} - Python {info.version_text} - {root}"
    context = PythonContext(
        id=stable_context_id(context_type, root),
        type=context_type,
        source=source,
        label=label,
        python_path=info.executable,
        reference_python_path=resolve_venv_reference_python(root, info),
        root_path=root.resolve(),
        version=info.version_text,
    )
    return context


def discover_contexts() -> list[PythonContext]:
    interpreter_contexts: list[PythonContext] = []
    venv_contexts: list[PythonContext] = []
    tuv_contexts: list[PythonContext] = []
    seen: set[str] = set()

    def add(target: list[PythonContext], context: PythonContext | None) -> None:
        if context is None:
            return
        if context.id in seen:
            return
        seen.add(context.id)
        target.append(context)

    for info in discover_python_infos():
        prefix = "cwd interpreter" if info.source == "cwd" else "interpreter"
        label = f"{prefix} - Python {info.version_text} - {info.executable}"
        context = PythonContext(
            id=stable_context_id("interpreter", info.executable),
            type="interpreter",
            source=info.source,
            label=label,
            python_path=info.executable,
            reference_python_path=info.executable,
            root_path=None,
            version=info.version_text,
        )
        add(
            interpreter_contexts,
            context,
        )

    venv_specs: list[tuple[str, Path, str, str]] = []
    active = os.environ.get("VIRTUAL_ENV")
    if active:
        venv_specs.append(("venv", Path(active), "active venv", "active"))

    cwd = Path.cwd()
    if is_venv(cwd):
        venv_specs.append(("venv", cwd, cwd.name or str(cwd), "cwd"))
    try:
        children = sorted((item for item in cwd.iterdir() if item.is_dir()), key=lambda p: p.name.lower())
    except OSError:
        children = []
    for child in children:
        try:
            if child.resolve() == RUNNER_VENV:
                continue
        except OSError:
            continue
        if not is_venv(child):
            continue
        venv_specs.append(("venv", child, child.name, "scanned"))
    venv_specs.append(("tuv", RUNNER_VENV, "tuv venv", "tuv"))

    venv_results: list[PythonContext | None] = []
    if venv_specs:
        with ThreadPoolExecutor(max_workers=min(8, len(venv_specs))) as pool:
            venv_results = list(pool.map(lambda spec: context_from_venv(*spec), venv_specs))
    for spec, context in zip(venv_specs, venv_results):
        add(tuv_contexts if spec[0] == "tuv" else venv_contexts, context)

    contexts = interpreter_contexts + venv_contexts + tuv_contexts
    unresolved = [context for context in contexts if context.resolved_uv_provider is None]
    if unresolved:
        with ThreadPoolExecutor(max_workers=min(8, len(unresolved))) as pool:
            list(pool.map(refresh_context_uv_provider, unresolved))
    return contexts


def run_uv_json(context: PythonContext, args: list[str], timeout: float | None = 90.0) -> tuple[object, str]:
    cmd = uv_command(context, args)
    try:
        proc = run_command(cmd, timeout=timeout)
    except FileNotFoundError:
        # The provider executable vanished; drop caches so the next attempt re-resolves.
        context.resolved_uv_provider = None
        invalidate_uv_validation_cache()
        raise
    if proc.returncode != 0:
        detail = command_detail(cmd, proc.returncode, proc.stdout, proc.stderr, 0)
        raise RuntimeError(detail)
    try:
        return json.loads(proc.stdout or "[]"), proc.stderr
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse uv JSON output: {exc}\n\n{proc.stdout}") from exc


def installed_package_metadata(context: PythonContext) -> dict[str, PackageMetadata]:
    code = r"""
import email.parser
import importlib.metadata as metadata
import json
import os
import pathlib
import platform
import re
import sys

def norm(value):
    return re.sub(r"[-_.]+", "-", value).lower()

result = {}

def implementation_version():
    version = sys.implementation.version
    suffix = "" if version.releaselevel == "final" else version.releaselevel[0] + str(version.serial)
    return f"{version.major}.{version.minor}.{version.micro}{suffix}"

def put(name, summary, requires, extras):
    if not name:
        return
    key = norm(str(name))
    current = result.setdefault(key, {"name": name, "summary": "", "requires": [], "extras": []})
    if summary and not current["summary"]:
        current["summary"] = str(summary)
    current["requires"].extend(str(req) for req in (requires or []) if req)
    current["extras"].extend(str(extra) for extra in (extras or []) if extra)

try:
    for dist in metadata.distributions():
        meta = dist.metadata
        put(
            meta.get("Name"),
            meta.get("Summary"),
            list(dist.requires or meta.get_all("Requires-Dist") or []),
            meta.get_all("Provides-Extra") or [],
        )
except Exception:
    pass

if not result:
    for base in list(sys.path):
        try:
            root = pathlib.Path(base)
            if not root.is_dir():
                continue
            for meta_path in root.glob("*.dist-info/METADATA"):
                try:
                    msg = email.parser.Parser().parsestr(meta_path.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                put(
                    msg.get("Name"),
                    msg.get("Summary"),
                    msg.get_all("Requires-Dist") or [],
                    msg.get_all("Provides-Extra") or [],
                )
        except Exception:
            continue

env = {
    "implementation_name": sys.implementation.name,
    "implementation_version": implementation_version(),
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

for value in result.values():
    value["requires"] = sorted(set(value["requires"]))
    value["extras"] = sorted(set(value["extras"]))

print(json.dumps({"packages": result, "environment": env}))
"""
    command = [str(context.python_path), "-c", code]
    try:
        proc = run_command(command, timeout=30)
    except Exception as exc:
        raise RuntimeError(f"Could not collect package metadata: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(command_detail(command, proc.returncode, proc.stdout, proc.stderr, 0))
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"Could not parse package metadata JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Package metadata probe returned unexpected data")
    raw_packages = payload.get("packages", {})
    marker_env = payload.get("environment", {})
    if not isinstance(raw_packages, dict):
        raise RuntimeError("Package metadata probe returned unexpected package data")
    if not isinstance(marker_env, dict):
        marker_env = {}
    result: dict[str, PackageMetadata] = {}
    for package_name, item in raw_packages.items():
        if not isinstance(package_name, str) or not isinstance(item, dict):
            continue
        summary = item.get("summary")
        requires = item.get("requires", [])
        if not isinstance(requires, list):
            requires = []
        raw_extras = item.get("extras", [])
        extras = (
            {str(extra).strip() for extra in raw_extras if str(extra).strip()}
            if isinstance(raw_extras, list)
            else set()
        )
        for req in requires:
            if isinstance(req, str):
                extras.update(extra_names_from_requirement(req))
        deps = {
            dep
            for req in requires
            if isinstance(req, str)
            for dep in [dependency_name_from_requirement(req, marker_env, extras)]
            if dep
        }
        result[canonicalize_name(package_name)] = PackageMetadata(
            description=short_description(summary),
            dependencies=deps,
            extras={canonicalize_name(extra) for extra in extras},
        )
    return result


def dependency_name_from_requirement(
    requirement: str,
    marker_env: dict[str, object],
    extras: set[str] | None = None,
) -> str | None:
    if Requirement is not None:
        try:
            parsed = Requirement(requirement)
            if parsed.marker is not None:
                if not requirement_marker_applies(parsed.marker, marker_env, extras or set()):
                    return None
            return canonicalize_name(parsed.name)
        except InvalidRequirement:
            pass
    head = re.split(r"[<>=!~;\[\s(]", requirement, 1)[0].strip()
    return canonicalize_name(head) if head else None


def requirement_marker_applies(
    marker: object,
    marker_env: dict[str, object],
    extras: set[str],
) -> bool:
    env = dict(default_environment() if default_environment is not None else {})
    env.update({str(key): str(value) for key, value in marker_env.items()})
    extra_values = ["", *extra_marker_values(extras)]
    try:
        for extra in extra_values:
            env["extra"] = extra
            if marker.evaluate(environment=env):  # type: ignore[attr-defined]
                return True
    except Exception:
        return True
    return False


def extra_marker_values(extras: set[str]) -> list[str]:
    values: set[str] = set()
    for extra in extras:
        raw = str(extra).strip()
        if not raw:
            continue
        normalized = canonicalize_name(raw)
        values.add(raw)
        values.add(normalized)
        values.add(normalized.replace("-", "_"))
    return sorted(values)


def extra_names_from_requirement(requirement: str) -> set[str]:
    values: set[str] = set()
    if Requirement is not None:
        try:
            parsed = Requirement(requirement)
            if parsed.marker is not None:
                requirement = str(parsed.marker)
        except InvalidRequirement:
            pass
    for match in re.finditer(r"\bextra\s*(?:==|!=|in|not\s+in)\s*(['\"])(.*?)\1", requirement, re.IGNORECASE):
        for value in re.split(r"[,\s]+", match.group(2)):
            cleaned = value.strip()
            if cleaned:
                values.add(cleaned)
    return values


def short_description(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= 180 else text[:177].rstrip() + "..."


class SimpleRepositoryLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href: str | None = None
        yanked = False
        for name, value in attrs:
            lowered = name.lower()
            if lowered == "href" and value:
                href = value
            elif lowered == "data-yanked":
                yanked = True
        if href:
            self.links.append((href, yanked))


def configured_index_urls() -> list[str]:
    urls: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        for part in re.split(r"\s+", value.strip()):
            if part and part not in urls:
                urls.append(part)

    primary = os.environ.get("UV_INDEX_URL") or os.environ.get("UV_DEFAULT_INDEX") or os.environ.get("PIP_INDEX_URL")
    if primary:
        add(primary)
    else:
        local = discover_local_index_url()
        add(local or "https://pypi.org/simple/")
    add(os.environ.get("UV_EXTRA_INDEX_URL") or os.environ.get("PIP_EXTRA_INDEX_URL"))
    return urls


def index_url_from_uv_table(table: object) -> str | None:
    if not isinstance(table, dict):
        return None
    indexes = table.get("index")
    if isinstance(indexes, list):
        for entry in indexes:
            if isinstance(entry, dict) and entry.get("default") is True:
                url = entry.get("url")
                if isinstance(url, str) and url.strip():
                    return url.strip()
    for key in ("default-index", "index-url"):
        value = table.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def index_url_from_config_file(path: Path) -> str | None:
    if tomllib is not None:
        try:
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
        except Exception:
            return None
        if path.name == "uv.toml":
            return index_url_from_uv_table(data)
        tool = data.get("tool")
        return index_url_from_uv_table(tool.get("uv")) if isinstance(tool, dict) else None
    # Regex fallback (Python < 3.11): only uv.toml is scanned; a bare `url =`
    # in pyproject.toml usually belongs to unrelated tables such as project.urls.
    if path.name != "uv.toml":
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for key in ("default-index", "index-url"):
        match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            return match.group(1)
    return None


def discover_local_index_url() -> str | None:
    for directory in [Path.cwd(), *Path.cwd().parents]:
        for filename in ("uv.toml", "pyproject.toml"):
            path = directory / filename
            if not path.is_file():
                continue
            url = index_url_from_config_file(path)
            if url:
                return url
        # Stop at the repository root; configs above it belong to other projects.
        if (directory / ".git").exists():
            break
    return None


def simple_project_url(index_url: str, package_name: str) -> str:
    base = index_url.rstrip("/") + "/"
    quoted = urllib.parse.quote(canonicalize_name(package_name), safe="")
    return urllib.parse.urljoin(base, f"{quoted}/")


def version_from_distribution_filename(filename: str, package_name: str) -> str | None:
    base = Path(urllib.parse.unquote(urllib.parse.urlparse(filename).path)).name
    if not base or base.endswith(".metadata"):
        return None
    normalized = canonicalize_name(package_name)
    if parse_wheel_filename is not None:
        try:
            name, version, _build, _tags = parse_wheel_filename(base)
            if canonicalize_name(str(name)) == normalized:
                return str(version)
        except (InvalidWheelFilename, ValueError):
            pass
    if parse_sdist_filename is not None:
        try:
            name, version = parse_sdist_filename(base)
            if canonicalize_name(str(name)) == normalized:
                return str(version)
        except (InvalidSdistFilename, ValueError):
            pass
    return None


def versions_from_simple_json(data: object, package_name: str) -> tuple[set[str], set[str]]:
    versions: set[str] = set()
    yanked_only: set[str] = set()
    available: set[str] = set()
    if not isinstance(data, dict):
        return versions, set()
    raw_versions = data.get("versions")
    if isinstance(raw_versions, list):
        versions.update(str(version) for version in raw_versions if isinstance(version, str))
    files = data.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename") or item.get("url")
            if not isinstance(filename, str):
                continue
            version = version_from_distribution_filename(filename, package_name)
            if not version:
                continue
            versions.add(version)
            if item.get("yanked"):
                yanked_only.add(version)
            else:
                available.add(version)
    return versions, yanked_only - available


def versions_from_simple_html(text: str, package_name: str) -> tuple[set[str], set[str]]:
    parser = SimpleRepositoryLinkParser()
    parser.feed(text)
    versions: set[str] = set()
    yanked_only: set[str] = set()
    available: set[str] = set()
    for href, yanked in parser.links:
        version = version_from_distribution_filename(href, package_name)
        if not version:
            continue
        versions.add(version)
        if yanked:
            yanked_only.add(version)
        else:
            available.add(version)
    return versions, yanked_only - available


def fetch_available_versions(package_name: str, timeout: float = 12.0) -> tuple[list[str], set[str]]:
    """Return (sorted versions, versions whose files are all yanked) across configured indexes."""
    versions: set[str] = set()
    yanked: set[str] = set()
    not_yanked: set[str] = set()
    errors: list[str] = []
    for index_url in configured_index_urls():
        url = simple_project_url(index_url, package_name)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.pypi.simple.v1+json, text/html;q=0.2",
                "User-Agent": "tuv/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        try:
            if "json" in content_type or body.lstrip().startswith("{"):
                found, found_yanked = versions_from_simple_json(json.loads(body), package_name)
            else:
                found, found_yanked = versions_from_simple_html(body, package_name)
            versions.update(found)
            yanked.update(found_yanked)
            not_yanked.update(found - found_yanked)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    yanked -= not_yanked
    if versions:
        return sorted(versions, key=version_key), yanked
    if errors:
        raise RuntimeError("Version lookup failed: " + "; ".join(errors[-3:]))
    return [], set()


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
                status="loading",
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
) -> tuple[bool, set[str], dict[str, list[str]], dict[str, list[str]], dict[str, str | None]]:
    installed_names = set(display_by_name)
    metadata_by_package = installed_package_metadata(context)
    if installed_names and not installed_names.issubset(set(metadata_by_package)):
        missing = sorted(installed_names - set(metadata_by_package))
        raise RuntimeError(
            "Package metadata did not match the displayed package list; "
            f"missing metadata for {', '.join(missing[:8])}"
        )
    deps_by_package = {name: meta.dependencies for name, meta in metadata_by_package.items()}
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
    descriptions = {
        name: metadata_by_package.get(name, PackageMetadata()).description
        for name in installed_names
    }
    return True, safe_names, dependency_packages, usage_packages, descriptions


def pins_file_path() -> Path:
    return Path.home() / ".tuv" / "pins.json"


def load_pins() -> dict[str, set[str]]:
    try:
        data = json.loads(pins_file_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    result: dict[str, set[str]] = {}
    if isinstance(data, dict):
        for key, values in data.items():
            if isinstance(key, str) and isinstance(values, list):
                names = {canonicalize_name(str(value)) for value in values if str(value).strip()}
                if names:
                    result[key] = names
    return result


def save_pins(pins: dict[str, set[str]]) -> None:
    try:
        path = pins_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: sorted(values) for key, values in pins.items() if values}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def last_lines(text: str, count: int = 8) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-count:]) if lines else ""


def tail_lines(text: str, count: int = 12) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()][-count:]


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


def result_label(result: InstallResult) -> str:
    return result.display_name or result.package_name


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

        deadline = time.time() + timeout
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    code = msvcrt.getwch()
                    return WINDOWS_SPECIAL_KEYS.get(code)
                return normalize_key(ch)
            if time.time() >= deadline:
                return None
            time.sleep(0.01)


WINDOWS_SPECIAL_KEYS = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
    "I": "pageup",
    "Q": "pagedown",
    "G": "home",
    "O": "end",
    "S": "delete",
    "<": "f2",
    "=": "f3",
    ">": "f4",
    "?": "f5",
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
    "\x1b[3~": "delete",
    "\x1b[12~": "f2",
    "\x1bOQ": "f2",
    "\x1b[13~": "f3",
    "\x1bOR": "f3",
    "\x1b[14~": "f4",
    "\x1bOS": "f4",
    "\x1b[15~": "f5",
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
    if data in ("\x7f", "\x08"):
        return "backspace"
    if len(data) == 1 and data.isprintable():
        # Printable characters pass through unchanged so text-input modes can
        # consume them; command dispatch matches them case-insensitively.
        return data
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
        self.version_lookup_token = 0
        self.version_error: str | None = None
        self.pending_version_direction: int | None = None
        self.new_package: dict[str, object] | None = None
        self.rows: list[PackageRow] = []
        self.view: list[PackageRow] = []
        self.focus_index = 0
        self.scroll = 0
        self._message = "Starting..."
        self._message_kind = "info"
        self.filter_text = ""
        self.selected_names: set[str] = set()
        self.pinned_by_context: dict[str, set[str]] = load_pins()
        self.target_overrides: dict[str, str] = {}
        self.discovering_contexts = False
        self.rediscover_preserve = False
        self.pending_select_root: str | None = None
        self.discovery_error: str | None = None
        self.refreshing = False
        self.refresh_context_id: str | None = None
        self.refresh_generation = 0
        self.outdated_loading = False
        self.dependency_loading = False
        self.pending_after_refresh_action = False
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.updated_by_context: dict[str, set[str]] = {}
        self.installing = False
        self.active_install_context_id: str | None = None
        self.install_proc: subprocess.Popen[str] | None = None
        self.install_proc_lock = threading.Lock()
        self.install_cancelled = False
        self.wait_queue: list[tuple[str, str, str]] = []
        self.bulk_active = False
        self.bulk_queue: list[tuple[str, str]] = []
        self.bulk_total = 0
        self.bulk_processed: set[str] = set()
        self.bulk_failed_results: dict[str, InstallResult] = {}
        self.bulk_summary: list[tuple[str, str, str]] = []
        self.bulk_run_id: str | None = None
        self.bulk_run_counter = 0
        self.prompt: Prompt | None = None
        self.info_open = False
        self.info_scroll = 0
        self.report_open = False
        self.report_title = ""
        self.report_lines: list[str] = []
        self.report_scroll = 0
        self.input_mode: str | None = None
        self.input_buffer = ""
        self.creating_venv = False
        self.mutation_blocked_reason: str | None = None
        self.spinner_index = 0
        self.should_quit = False
        self.quit_after_prompt = False
        self.last_render = ""
        self.last_size: tuple[int, int] = (0, 0)
        self._install_signal_handlers()

    @property
    def message(self) -> str:
        return self._message

    @message.setter
    def message(self, value: str) -> None:
        self._message = value
        self._message_kind = "info"

    def set_message(self, value: str, kind: str = "info") -> None:
        self._message = value
        self._message_kind = kind

    @property
    def context(self) -> PythonContext | None:
        if not self.contexts:
            return None
        self.context_index = max(0, min(self.context_index, len(self.contexts) - 1))
        return self.contexts[self.context_index]

    def pinned_names(self) -> set[str]:
        context = self.context
        if context is None:
            return set()
        return self.pinned_by_context.setdefault(context.id, set())

    def _install_signal_handlers(self) -> None:
        try:
            signal.signal(signal.SIGINT, lambda _sig, _frame: self.request_quit())
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, lambda _sig, _frame: self.request_quit(force=True))
        except Exception:
            pass

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
                    self.terminal.write(HOME + screen)
                    self.last_render = screen
                # Drain buffered keys so held-down navigation stays responsive.
                key = self.terminal.read_key(0.08)
                handled = 0
                while key is not None and handled < 40 and not self.should_quit:
                    self.handle_key(key)
                    handled += 1
                    key = self.terminal.read_key(0.0)
            self.terminate_install_process()
            return 0

    def terminate_install_process(self) -> None:
        with self.install_proc_lock:
            proc = self.install_proc
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass

    def start_context_discovery(self, preserve_current: bool = False) -> None:
        if self.discovering_contexts:
            return
        self.discovering_contexts = True
        self.rediscover_preserve = preserve_current
        self.discovery_error = None
        self.message = "Discovering Python contexts" if not preserve_current else "Rescanning Python contexts"

        def worker() -> None:
            try:
                contexts = discover_contexts()
                self.event_queue.put(("contexts_done", contexts))
            except Exception as exc:
                self.event_queue.put(("contexts_failed", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def on_contexts_done(self, contexts: list[PythonContext]) -> None:
        self.discovering_contexts = False
        preserve = self.rediscover_preserve
        self.rediscover_preserve = False
        previous = self.context
        previous_confirmed = {context.id for context in self.contexts if context.confirmed_for_mutation}
        self.contexts = contexts
        for context in self.contexts:
            if context.id in previous_confirmed:
                context.confirmed_for_mutation = True
        if not self.contexts:
            self.discovery_error = "Tuv could not find any usable Python context."
            self.set_message(self.discovery_error, "error")
            return
        if self.pending_select_root:
            wanted = stable_context_id("venv", Path(self.pending_select_root))
            self.pending_select_root = None
            for index, context in enumerate(self.contexts):
                if context.id == wanted:
                    self.context_index = index
                    self.context_overlay_index = index
                    self.load_current_context()
                    return
        if preserve and previous is not None:
            for index, context in enumerate(self.contexts):
                if context.id == previous.id:
                    self.context_index = index
                    self.context_overlay_index = min(self.context_overlay_index, len(self.contexts) - 1)
                    self.message = f"Contexts rescanned: {len(self.contexts)} found"
                    return
        self.context_index = self.default_context_index()
        self.context_overlay_index = self.context_index
        self.load_current_context()

    def on_contexts_failed(self, error: str) -> None:
        self.discovering_contexts = False
        self.rediscover_preserve = False
        self.contexts = []
        self.discovery_error = f"Context discovery failed: {last_lines(error, 2)}"
        self.set_message(self.discovery_error, "error")

    def request_quit(self, force: bool = False) -> None:
        if force or not self.installing:
            self.should_quit = True
            return
        if self.quit_after_prompt:
            # Second quit request while the prompt is pending: force out.
            self.should_quit = True
            return
        self.quit_after_prompt = True
        operation = "bulk update" if self.bulk_active else "install"
        self.prompt = Prompt(
            title="Quit while installing",
            message=f"A {operation} is running. Quit and cancel it? y/N",
            on_yes=self.confirm_quit_cancel_install,
            on_no=self.decline_quit_cancel_install,
        )

    def confirm_quit_cancel_install(self) -> None:
        self.prompt = None
        self.quit_after_prompt = False
        self.cancel_active_install()
        self.should_quit = True

    def decline_quit_cancel_install(self) -> None:
        self.quit_after_prompt = False
        self.set_message("Quit cancelled; the operation continues", "warn")

    def cancel_active_install(self) -> None:
        with self.install_proc_lock:
            proc = self.install_proc
            if proc is not None:
                self.install_cancelled = True
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        if self.bulk_active:
            self.bulk_queue = []

    def update_view(self) -> None:
        if not self.filter_text:
            view = list(self.rows)
        else:
            needle = self.filter_text.lower()
            view = [
                row
                for row in self.rows
                if needle in row.display_name.lower() or needle in row.name.lower()
            ]
        same = len(view) == len(self.view) and all(a is b for a, b in zip(view, self.view))
        if not same:
            focused = self.focused_row()
            self.view = view
            if focused is not None:
                self.focus_on_package(focused.name)
        self.focus_index = max(0, min(self.focus_index, max(0, len(self.view) - 1)))

    def focus_on_package(self, package_name: str) -> None:
        for index, row in enumerate(self.view):
            if row.name == package_name:
                self.focus_index = index
                return
        self.focus_index = max(0, min(self.focus_index, max(0, len(self.view) - 1)))

    def default_context_index(self) -> int:
        for index, context in enumerate(self.contexts):
            if context.type == "venv" and context.source == "active":
                return index
        for index, context in enumerate(self.contexts):
            if context.type == "venv" and context.root_path and context.root_path.name == ".venv":
                return index
        for index, context in enumerate(self.contexts):
            if context.type == "interpreter" and context.source == "cwd":
                return index
        for index, context in enumerate(self.contexts):
            if context.type == "interpreter":
                return index
        for index, context in enumerate(self.contexts):
            if context.type == "tuv":
                return index
        return 0

    def load_current_context(self) -> None:
        context = self.context
        if context is None:
            return
        self.rows = []
        self.view = []
        self.focus_index = 0
        self.scroll = 0
        self.version_overlay = False
        self.version_overlay_row = None
        self.version_overlay_scroll = 0
        self.version_options = []
        self.version_loading = False
        self.version_lookup_token += 1
        self.version_error = None
        self.pending_version_direction = None
        self.new_package = None
        self.filter_text = ""
        self.selected_names = set()
        self.target_overrides = {}
        self.wait_queue = []
        self.bulk_active = False
        self.bulk_queue = []
        self.bulk_total = 0
        self.bulk_processed = set()
        self.bulk_failed_results = {}
        self.bulk_summary = []
        self.bulk_run_id = None
        self.outdated_loading = False
        self.dependency_loading = False
        self.pending_after_refresh_action = False
        self.info_open = False
        self.info_scroll = 0
        self.report_open = False
        self.report_lines = []
        self.report_scroll = 0
        self.input_mode = None
        self.input_buffer = ""
        self.mutation_blocked_reason = None
        self.message = f"Loading {context.label}"
        self.ensure_uv_provider(context, lambda: self.start_refresh(context, "Loading packages"))

    def ensure_uv_provider(self, context: PythonContext, on_ready: Callable[[], None]) -> None:
        if (context.resolved_uv_provider or refresh_context_uv_provider(context)) is not None:
            self.mutation_blocked_reason = None
            on_ready()
            return
        self.bootstrap_tuv_runner_uv(context, on_ready)

    def bootstrap_tuv_runner_uv(self, context: PythonContext, on_ready: Callable[[], None]) -> None:
        runner = runner_python_path()
        self.message = f"Installing uv into Tuv runner venv: {runner}"

        def worker() -> None:
            try:
                if not runner.is_file():
                    raise RuntimeError(f"Tuv runner Python was not found: {runner}")
                pip_check = run_command([str(runner), "-m", "pip", "--version"], timeout=20)
                if pip_check.returncode != 0:
                    ensurepip = [str(runner), "-m", "ensurepip", "--upgrade"]
                    ensure = run_command(ensurepip, timeout=120)
                    if ensure.returncode != 0:
                        detail = command_detail(ensurepip, ensure.returncode, ensure.stdout, ensure.stderr, 0)
                        self.event_queue.put(("runner_uv_done", (context.id, 1, detail, on_ready)))
                        return
                    pip_recheck = run_command([str(runner), "-m", "pip", "--version"], timeout=20)
                    if pip_recheck.returncode != 0:
                        detail = command_detail(
                            [str(runner), "-m", "pip", "--version"],
                            pip_recheck.returncode,
                            pip_recheck.stdout,
                            pip_recheck.stderr,
                            0,
                        )
                        self.event_queue.put(("runner_uv_done", (context.id, 1, detail, on_ready)))
                        return
                install = [str(runner), "-m", "pip", "install", "uv"]
                start = time.time()
                proc = run_command(install, timeout=300)
                elapsed = time.time() - start
                detail = command_detail(install, proc.returncode, proc.stdout, proc.stderr, elapsed)
                if proc.returncode == 0:
                    uv_check = run_command([str(runner), "-m", "uv", "--version"], timeout=20)
                    if uv_check.returncode != 0:
                        detail = command_detail(
                            [str(runner), "-m", "uv", "--version"],
                            uv_check.returncode,
                            uv_check.stdout,
                            uv_check.stderr,
                            0,
                        )
                        self.event_queue.put(("runner_uv_done", (context.id, 1, detail, on_ready)))
                        return
                self.event_queue.put(("runner_uv_done", (context.id, proc.returncode, detail, on_ready)))
            except Exception as exc:
                self.event_queue.put(("runner_uv_done", (context.id, 1, str(exc), on_ready)))

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
        self.pending_after_refresh_action = False
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
            elif event == "runner_uv_done":
                context_id, returncode, detail, on_ready = payload  # type: ignore[misc]
                self.on_runner_uv_done(context_id, returncode, detail, on_ready)
            elif event == "versions_done":
                token, row_name, versions, yanked, error = payload  # type: ignore[misc]
                self.on_versions_done(token, row_name, versions, yanked, error)
            elif event == "new_versions_done":
                token, display_name, versions, yanked, error = payload  # type: ignore[misc]
                self.on_new_versions_done(token, display_name, versions, yanked, error)
            elif event == "venv_created":
                root, error = payload  # type: ignore[misc]
                self.on_venv_created(root, error)

    def on_runner_uv_done(
        self,
        context_id: str,
        returncode: int,
        detail: str,
        on_ready: Callable[[], None],
    ) -> None:
        context = self.find_context(context_id)
        if context is None:
            return
        if returncode == 0 and refresh_context_uv_provider(context) is not None:
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
                uninstall_safe=False,
                installed_version="-",
                target_version="-",
                candidate_versions=["-"],
                status="failed",
                last_error="Tuv runner uv installation failed",
                last_error_detail=detail,
                description="Tuv operational error: runner uv repair failed.",
                operational_error=True,
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

        focused = self.focused_row()
        self.carry_refresh_state(rows)
        self.rows = rows
        self.update_view()
        if focused is not None:
            self.focus_on_package(focused.name)
        self.scroll = min(self.scroll, max(0, len(self.view) - 1))
        self.restore_bulk_failed_rows()
        self.prune_target_overrides()

        if install_result is not None:
            self.installing = False
            self.active_install_context_id = None
            self.pending_after_refresh_action = True
            self.record_bulk_outcome(install_result)
            if install_result.cancelled:
                self.set_message(f"{install_result.operation.capitalize()} cancelled: {result_label(install_result)}", "warn")
            elif install_result.ok:
                if install_result.operation == "uninstall":
                    self.message = f"Uninstalled {result_label(install_result)}"
                else:
                    self.message = f"Installed {result_label(install_result)}=={install_result.target_version}"
            else:
                self.mark_failed_row(install_result)
                self.set_message(f"{install_result.operation.capitalize()} failed: {result_label(install_result)}", "error")
        elif warning:
            self.set_message(warning, "warn")
        else:
            self.message = f"Loaded {len(self.rows)} packages"

        context = self.context
        if context is not None and context.id == context_id and not self.installing:
            self.start_dependency_refresh(context, generation)
            self.start_outdated_refresh(context, generation)
        else:
            self.finish_full_refresh()

    def prune_target_overrides(self) -> None:
        for name, target in list(self.target_overrides.items()):
            row = self.find_row(name)
            if row is None or row.installed_version == target:
                self.target_overrides.pop(name, None)

    def record_bulk_outcome(self, result: InstallResult) -> None:
        if not self.bulk_active or result.operation != "install":
            return
        label = result_label(result)
        if result.cancelled:
            self.bulk_summary.append((label, "cancelled", "cancelled by user"))
        elif result.ok:
            self.bulk_summary.append((label, "updated", f"-> {result.target_version}"))
        else:
            exit_text = str(result.exit_code) if result.exit_code is not None else "did not start"
            self.bulk_summary.append((label, "failed", f"exit {exit_text}"))

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
        outdated = 0
        for row in self.rows:
            if row.status == "failed" and row.last_install_result is not None:
                continue
            latest = targets.get(row.name)
            if latest:
                # Trust uv's latest as an installable target; the full candidate
                # list is fetched lazily when the user first browses versions.
                merged = set(row.candidate_versions)
                merged.add(row.installed_version)
                merged.add(latest)
                row.candidate_versions = sorted(merged, key=version_key)
                row.versions_resolved = True
            override = self.target_overrides.get(row.name)
            if (
                override
                and override != row.installed_version
                and row.versions_resolved
                and override in row.candidate_versions
            ):
                row.target_version = override
            elif latest:
                row.target_version = latest
            else:
                row.target_version = row.installed_version
            if row.status not in {"failed", "installing", "wait"}:
                row.status = "ready" if row.target_version != row.installed_version else "current"
            if row.is_outdated:
                outdated += 1
        if warning:
            self.set_message(warning, "warn")
        elif outdated:
            self.message = f"{outdated} package(s) have updates available"
        else:
            self.message = "All packages are up to date"
        self.finish_full_refresh()

    def finish_full_refresh(self) -> None:
        if self.refreshing or self.outdated_loading:
            return
        if not self.pending_after_refresh_action:
            return
        self.pending_after_refresh_action = False
        if self.bulk_active:
            self.continue_bulk_update()
        else:
            self.maybe_start_waiting_install()

    def on_dependency_done(
        self,
        context_id: str,
        generation: int,
        dependency_payload: tuple[
            bool,
            set[str],
            dict[str, list[str]],
            dict[str, list[str]],
            dict[str, str | None],
        ] | None,
        error: str | None,
    ) -> None:
        if self.context is None or self.context.id != context_id or generation != self.refresh_generation:
            return
        self.dependency_loading = False
        if dependency_payload is None:
            if error:
                self.message = f"Dependency data unavailable: {last_lines(error, 2)}"
            return
        metadata_trusted, safe_names, dependency_packages, usage_packages, descriptions = dependency_payload
        if not metadata_trusted:
            return
        for row in self.rows:
            row.metadata_trusted = True
            row.uninstall_safe = row.name in safe_names
            row.dependency_packages = dependency_packages.get(row.name, [])
            row.usage_packages = usage_packages.get(row.name, [])
            row.description = descriptions.get(row.name)

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
        if not before_versions:
            return
        changed = self.updated_by_context.setdefault(context_id, set())
        for row in rows:
            previous = before_versions.get(row.name)
            if previous is None or previous != row.installed_version:
                changed.add(row.name)

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
                merged = set(old.candidate_versions)
                merged.update(row.candidate_versions)
                row.candidate_versions = sorted(merged, key=version_key)
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
        if row.status == "loading":
            return row.color_hint
        return None

    def mark_failed_row(self, result: InstallResult) -> None:
        if self.bulk_active:
            result.failed_in_bulk_run_id = self.bulk_run_id
            self.bulk_failed_results[result.package_name] = result
            self.bulk_processed.add(result.package_name)
        self.apply_failed_result_to_row(result)
        if self.find_row(result.package_name) is None and not result.cancelled:
            # No row can hold the detail (e.g. a failed new-package install);
            # surface it in a report overlay instead.
            detail = command_detail(result.command, result.returncode, result.stdout, result.stderr, result.elapsed)
            self.open_report(
                f"{result.operation.capitalize()} failed: {result_label(result)}",
                detail.splitlines(),
            )

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
        if result.operation == "install":
            if result.candidate_versions_at_attempt:
                row.candidate_versions = sorted(set(result.candidate_versions_at_attempt), key=version_key)
            row.target_version = result.target_version
            row.versions_resolved = result.target_version in row.candidate_versions
        row.status = "failed"
        exit_text = str(result.exit_code) if result.exit_code is not None else "process did not start"
        row.last_error = f"{result.operation.capitalize()} failed with exit code {exit_text}"
        row.last_error_detail = detail
        row.last_install_result = result

    def on_install_done(self, result: InstallResult) -> None:
        context = self.find_context(result.context_id)
        if context is None:
            self.installing = False
            self.active_install_context_id = None
            if self.bulk_active:
                self.finalize_bulk_update()
            return
        self.start_refresh(context, f"Refreshing packages after {result.operation}", result)

    def maybe_start_waiting_install(self) -> None:
        if self.installing:
            return
        while self.wait_queue:
            context_id, package_name, target_version = self.wait_queue.pop(0)
            if self.context is None or self.context.id != context_id:
                continue
            row = self.find_row(package_name)
            if row is None:
                self.set_message(f"Waiting package vanished: {package_name}", "warn")
                continue
            if row.installed_version == target_version:
                row.status = "current"
                self.message = f"Waiting package already current: {row.display_name}"
                continue
            if not row.versions_resolved or target_version not in row.candidate_versions:
                self.set_message(f"Waiting package no longer has a valid target: {row.display_name}", "warn")
                if row.status == "wait":
                    row.status = self.natural_row_status(row)
                continue
            row.target_version = target_version
            if self.begin_install(row):
                return

    def natural_row_status(self, row: PackageRow) -> str:
        if row.target_version == row.installed_version:
            return "current"
        if row.versions_resolved and row.target_version in row.candidate_versions:
            return "ready"
        return "loading"

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
        if context is None:
            return
        if (context.resolved_uv_provider or refresh_context_uv_provider(context)) is None:
            self.ensure_uv_provider(context, self.start_bulk_update)
            return
        pinned = self.pinned_names()
        if self.selected_names:
            source_rows = [row for row in self.rows if row.name in self.selected_names]
            scope = "selected"
        else:
            source_rows = list(self.view)
            scope = "filtered" if self.filter_text else "ready"
        seen: set[str] = set()
        queue_items: list[tuple[str, str]] = []
        preview_rows: list[PackageRow] = []
        skipped_pinned = 0
        for row in source_rows:
            if row.name in seen:
                continue
            seen.add(row.name)
            if row.status != "ready" or not self.row_target_installable(row):
                continue
            if row.name in pinned:
                skipped_pinned += 1
                continue
            queue_items.append((row.name, row.target_version))
            preview_rows.append(row)
        if not queue_items:
            note = f" ({skipped_pinned} pinned excluded)" if skipped_pinned else ""
            self.set_message(f"No {scope} packages to update{note}", "warn")
            return
        preview_limit = 12
        lines = [
            f"{row.display_name}  {row.installed_version} -> {row.target_version}"
            for row in preview_rows[:preview_limit]
        ]
        if len(preview_rows) > preview_limit:
            lines.append(f"...and {len(preview_rows) - preview_limit} more")
        if skipped_pinned:
            lines.append(f"({skipped_pinned} pinned package(s) excluded)")
        title = "Confirm update all"
        header = f"Install updates for {len(queue_items)} {scope} package(s)? y/N"
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            title = "Confirm interpreter update all"
            header = (
                f"Install updates for {len(queue_items)} {scope} package(s) into interpreter "
                f"{context.python_path}? y/N"
            )
        message = "\n".join([header, "", *lines])
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
        self.bulk_run_counter += 1
        self.bulk_run_id = f"bulk-{self.bulk_run_counter}"
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
                self.bulk_summary.append((package_name, "skipped", "package vanished"))
                continue
            if normalized in self.bulk_processed:
                if row.status != "failed":
                    row.status = "skipped"
                continue
            if row.installed_version == target_version:
                if row.status != "failed":
                    row.status = "skipped"
                self.bulk_processed.add(normalized)
                self.bulk_summary.append((row.display_name, "skipped", "already current"))
                continue
            if row.status in {"failed", "installing"}:
                self.bulk_processed.add(normalized)
                self.bulk_summary.append((row.display_name, "skipped", f"status: {row.status}"))
                continue
            if not row.versions_resolved or target_version not in row.candidate_versions:
                row.status = "skipped"
                self.bulk_processed.add(normalized)
                self.bulk_summary.append((row.display_name, "skipped", "target no longer available"))
                continue
            row.target_version = target_version
            self.bulk_processed.add(normalized)
            self.mark_bulk_pending_waits()
            done = self.bulk_total - len(self.bulk_queue)
            self.message = f"Updating {done}/{self.bulk_total}: {row.display_name}=={target_version}"
            if self.begin_install(row):
                return
            row.status = "skipped"
            self.bulk_summary.append((row.display_name, "skipped", "could not start install"))
        self.finalize_bulk_update()

    def finalize_bulk_update(self) -> None:
        summary = list(self.bulk_summary)
        self.bulk_active = False
        self.bulk_processed = set()
        self.bulk_failed_results = {}
        self.bulk_run_id = None
        self.bulk_total = 0
        self.bulk_summary = []
        for row in self.rows:
            if row.status == "skipped":
                row.status = self.natural_row_status(row)
        updated = sum(1 for _name, outcome, _note in summary if outcome == "updated")
        failed = sum(1 for _name, outcome, _note in summary if outcome == "failed")
        skipped = sum(1 for _name, outcome, _note in summary if outcome == "skipped")
        cancelled = sum(1 for _name, outcome, _note in summary if outcome == "cancelled")
        if failed or skipped or cancelled:
            lines = [f"Updated: {updated}   Failed: {failed}   Skipped: {skipped}   Cancelled: {cancelled}", ""]
            for name, outcome, note in summary:
                lines.append(f"{outcome:<10} {name}  {note}")
            lines.append("")
            lines.append("Failed rows keep their error details; press F3 on a row for specifics.")
            self.open_report("Update all summary", lines)
            self.set_message(
                f"Update all finished: {updated} updated, {failed} failed, {skipped} skipped",
                "error" if failed else "warn",
            )
        else:
            self.message = f"Update all complete: {updated} updated"
        self.maybe_start_waiting_install()

    def open_report(self, title: str, lines: list[str]) -> None:
        self.report_open = True
        self.report_title = title
        self.report_lines = lines
        self.report_scroll = 0

    def find_context(self, context_id: str) -> PythonContext | None:
        return next((context for context in self.contexts if context.id == context_id), None)

    def find_row(self, package_name: str) -> PackageRow | None:
        normalized = canonicalize_name(package_name)
        return next((row for row in self.rows if row.name == normalized), None)

    def new_package_yanked(self) -> set[str]:
        if self.new_package is None:
            return set()
        yanked = self.new_package.get("yanked")
        return yanked if isinstance(yanked, set) else set()

    def refresh_version_options(self, row: PackageRow) -> None:
        self.version_options = sorted(set(row.candidate_versions + [row.target_version]), key=version_key, reverse=True)
        try:
            self.version_overlay_index = self.version_options.index(row.target_version)
        except ValueError:
            self.version_overlay_index = 0
        self.ensure_version_overlay_visible()

    def start_version_lookup(self, row: PackageRow, pending_direction: int | None = None) -> bool:
        if row.full_versions_loaded:
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
        if (
            not row.versions_resolved
            and row.status not in {"installing", "wait"}
            and not (row.status == "failed" and row.last_install_result is not None)
        ):
            row.status = "loading"
            row.last_error = None
            row.last_error_detail = None
        self.message = f"Loading versions for {row.display_name}"

        def worker(package_name: str) -> None:
            try:
                versions, yanked = fetch_available_versions(package_name)
                self.event_queue.put(("versions_done", (token, canonicalize_name(package_name), versions, yanked, None)))
            except Exception as exc:
                self.event_queue.put(("versions_done", (token, canonicalize_name(package_name), [], set(), str(exc))))

        threading.Thread(target=worker, args=(row.display_name,), daemon=True).start()
        return True

    def selectable_versions(self, row: PackageRow) -> list[str]:
        candidates = sorted(set(row.candidate_versions + [row.target_version]), key=version_key)
        # Arrow stepping skips yanked versions; they stay selectable in the
        # F4 overlay, where selection asks for confirmation.
        allowed = [
            version
            for version in candidates
            if version not in row.yanked_versions
            or version in {row.installed_version, row.target_version}
        ]
        return allowed or candidates

    def apply_version_direction(self, row: PackageRow, direction: int) -> None:
        if not row.versions_resolved:
            self.set_message(f"Version lookup is not ready for {row.display_name}", "warn")
            return
        if not row.candidate_versions:
            return
        candidates = self.selectable_versions(row)
        try:
            index = candidates.index(row.target_version)
        except ValueError:
            index = 0
        index = max(0, min(len(candidates) - 1, index + direction))
        self.set_row_target(row, candidates[index])

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

    def on_versions_done(
        self,
        token: int,
        row_name: str,
        versions: list[str],
        yanked: set[str],
        error: str | None,
    ) -> None:
        if token != self.version_lookup_token:
            # A newer lookup owns the loading flag; drop this stale result.
            return
        self.version_loading = False
        row = self.find_row(row_name)
        if row is None:
            return
        pending_direction = self.pending_version_direction if self.version_overlay_row == row.name else None
        self.pending_version_direction = None
        if versions:
            merged = set(versions)
            merged.add(row.installed_version)
            row.candidate_versions = sorted(merged, key=version_key)
            row.versions_resolved = True
            row.full_versions_loaded = True
            row.yanked_versions = set(yanked)
            row.last_error = None
            row.last_error_detail = None
            self.refresh_version_options(row)
            self.version_error = None
            self.message = f"Loaded {len(self.version_options)} versions for {row.display_name}"
        else:
            detail = last_lines(error or "no versions found", 2)
            if row.versions_resolved:
                # uv already supplied an installable target; degrade gracefully
                # instead of blocking installs (private indexes, offline, ...).
                self.version_error = f"Full version list unavailable: {detail}"
                self.set_message(
                    f"Full version list unavailable for {row.display_name}; using uv-reported versions",
                    "warn",
                )
            else:
                self.version_error = f"Version lookup failed: {detail}"
                self.set_message(self.version_error, "error")
                if row.status == "loading":
                    row.status = "nodata"
                    row.last_error = "Version lookup failed"
                    row.last_error_detail = error or "No installable versions found"
            self.refresh_version_options(row)
        if row.status == "loading" and versions:
            row.status = "ready" if row.target_version != row.installed_version else "current"
        if pending_direction is not None and versions:
            self.apply_version_direction(row, pending_direction)

    def handle_key(self, key: str) -> None:
        if self.input_mode:
            self.handle_input_key(key)
            return
        if self.prompt:
            self.handle_prompt_key(key)
            return
        if self.report_open:
            self.handle_report_key(key)
            return
        if self.info_open:
            if key in {"esc", "enter", "f3"}:
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

        lowered = key.lower() if len(key) == 1 else key
        if key == "quit" or lowered == "q" or key == "f10":
            self.request_quit()
        elif key == "esc":
            self.handle_main_escape()
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
            self.focus_index = max(0, len(self.view) - 1)
        elif key == "left":
            self.change_target_version(-1)
        elif key == "right":
            self.change_target_version(1)
        elif key == "enter":
            self.request_install()
        elif key == " ":
            self.toggle_selection()
        elif lowered == "/":
            self.input_mode = "filter"
            self.input_buffer = self.filter_text
        elif lowered == "i":
            self.request_new_package()
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
        elif key == "f9" or lowered == "c":
            self.context_overlay = True
            self.context_overlay_index = self.context_index
        elif key == "f3":
            self.info_open = True
            self.info_scroll = 0
        elif lowered == "r":
            context = self.context
            if context:
                if self.installing:
                    self.set_message("Refresh waits until the current install finishes", "warn")
                    return
                self.ensure_uv_provider(context, lambda: self.start_refresh(context, "Refreshing packages"))

    def handle_main_escape(self) -> None:
        if self.selected_names:
            self.selected_names = set()
            self.message = "Selection cleared"
            return
        if self.filter_text:
            self.filter_text = ""
            self.message = "Filter cleared"
            return
        if self.bulk_active and not self.installing:
            self.bulk_queue = []
            self.set_message("Update all cancelled; finishing current step", "warn")
            return
        if self.installing:
            operation = "bulk update" if self.bulk_active else "operation"
            self.prompt = Prompt(
                title="Cancel running operation",
                message=f"Cancel the running {operation}? y/N",
                on_yes=self.confirm_cancel_install,
                on_no=lambda: setattr(self, "message", "Cancel dismissed"),
            )

    def confirm_cancel_install(self) -> None:
        self.prompt = None
        self.cancel_active_install()
        self.set_message("Cancelling the running operation...", "warn")

    def rescan_contexts(self) -> None:
        if self.installing:
            self.set_message("Context rescan waits until the current install finishes", "warn")
            return
        self.start_context_discovery(preserve_current=True)

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
            return
        if key == "enter":
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
            return
        if key == "backspace":
            self.input_buffer = self.input_buffer[:-1]
            if mode == "filter":
                self.filter_text = self.input_buffer
            return
        if mode == "filter" and key in {"up", "down", "pageup", "pagedown"}:
            # Allow navigating results while the filter is being typed.
            amount = {"up": -1, "down": 1, "pageup": -self.page_size(), "pagedown": self.page_size()}[key]
            self.move_focus(amount)
            return
        if len(key) == 1 and key.isprintable():
            if len(self.input_buffer) < 80:
                self.input_buffer += key
                if mode == "filter":
                    self.filter_text = self.input_buffer
            return

    def handle_report_key(self, key: str) -> None:
        if key in {"esc", "enter"}:
            self.report_open = False
            self.report_scroll = 0
        elif key == "up":
            self.report_scroll = max(0, self.report_scroll - 1)
        elif key == "down":
            self.report_scroll += 1
        elif key == "pageup":
            self.report_scroll = max(0, self.report_scroll - self.page_size())
        elif key == "pagedown":
            self.report_scroll += self.page_size()
        elif key == "home":
            self.report_scroll = 0
        elif key == "end":
            self.report_scroll = 10**9

    def handle_version_overlay_key(self, key: str) -> None:
        if key in {"esc", "f4"}:
            self.version_overlay = False
            self.new_package = None
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
            if self.new_package is not None:
                self.handle_new_package_version_enter()
                return
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

    def handle_new_package_version_enter(self) -> None:
        info = self.new_package
        if info is None:
            self.version_overlay = False
            return
        selected = self.version_options[self.version_overlay_index]
        display_name = str(info.get("display", ""))
        if selected in self.new_package_yanked():
            self.prompt = Prompt(
                title="Confirm yanked version",
                message=(
                    f"{display_name}=={selected} is yanked on the index "
                    "(usually due to a serious defect). Install anyway? y/N"
                ),
                on_yes=lambda: self.confirm_yanked_new_install(selected),
                on_no=lambda: setattr(self, "message", "Install cancelled"),
            )
            return
        self.install_new_selected(selected)

    def confirm_yanked_new_install(self, version: str) -> None:
        self.prompt = None
        self.install_new_selected(version)

    def handle_prompt_key(self, key: str) -> None:
        prompt = self.prompt
        if prompt is None:
            return
        lowered = key.lower() if len(key) == 1 else key
        if lowered == "y" or key == "enter":
            prompt.on_yes()
        elif lowered == "n" or key == "esc":
            self.prompt = None
            self.quit_after_prompt = False
            if prompt.on_no:
                prompt.on_no()
            elif key == "esc":
                self.message = "Cancelled"

    def handle_context_overlay_key(self, key: str) -> None:
        lowered = key.lower() if len(key) == 1 else key
        if key in {"esc", "f9"}:
            self.context_overlay = False
            return
        if lowered == "n":
            self.request_create_venv()
            return
        if key == "f5":
            self.rescan_contexts()
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
                self.set_message("Context switch waits until the current install finishes", "warn")
                self.context_overlay = False
                return
            if self.context_overlay_index != self.context_index:
                self.context_index = self.context_overlay_index
                self.context_overlay = False
                self.load_current_context()
            else:
                self.context_overlay = False

    def move_focus(self, amount: int) -> None:
        if not self.view:
            return
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
        if not row.full_versions_loaded:
            # Fetch the full candidate list lazily; the step is applied when
            # the lookup completes.
            self.start_version_lookup(row, pending_direction=direction)
            return
        self.apply_version_direction(row, direction)

    def focused_row(self) -> PackageRow | None:
        if not self.view:
            return None
        self.focus_index = max(0, min(self.focus_index, len(self.view) - 1))
        return self.view[self.focus_index]

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

    def request_install(self, package_name: str | None = None) -> None:
        context = self.context
        row = self.find_row(package_name) if package_name else self.focused_row()
        if context is None or row is None:
            return
        if row.operational_error:
            return
        if self.mutation_blocked_reason:
            self.set_message(self.mutation_blocked_reason, "error")
            return
        if self.block_unresolved_action(row):
            return
        if self.installing:
            self.mark_wait(row, context)
            return
        if row.target_version == row.installed_version:
            self.message = f"{row.display_name} is already at {row.installed_version}"
            if row.status not in {"failed", "nodata"}:
                row.status = "current"
            return
        if (context.resolved_uv_provider or refresh_context_uv_provider(context)) is None:
            # Capture the package so the async bootstrap retries the same row
            # even if focus moved meanwhile.
            captured = row.name
            self.ensure_uv_provider(context, lambda: self.request_install(captured))
            return
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            captured = row.name
            self.prompt = Prompt(
                title="Confirm interpreter install",
                message=f"Install into interpreter {context.python_path}? y/N",
                on_yes=lambda: self.confirm_interpreter_and_install(context, captured),
                on_no=lambda: setattr(self, "message", "Install cancelled"),
            )
            return
        if row.target_version in row.yanked_versions:
            captured = row.name
            self.prompt = Prompt(
                title="Confirm yanked version",
                message=(
                    f"{row.display_name}=={row.target_version} is yanked on the index "
                    "(usually due to a serious defect). Install anyway? y/N"
                ),
                on_yes=lambda: self.confirm_yanked_and_install(captured),
                on_no=lambda: setattr(self, "message", "Install cancelled"),
            )
            return
        self.begin_install(row)

    def confirm_interpreter_and_install(self, context: PythonContext, package_name: str) -> None:
        self.prompt = None
        context.confirmed_for_mutation = True
        self.request_install(package_name)

    def confirm_yanked_and_install(self, package_name: str) -> None:
        self.prompt = None
        row = self.find_row(package_name)
        if row is None:
            return
        # Bypass the yanked re-check; every other guard still applies.
        self.begin_install(row)

    def mark_wait(self, row: PackageRow, context: PythonContext) -> None:
        for index, (context_id, name, _target) in enumerate(self.wait_queue):
            if context_id == context.id and name == row.name:
                self.wait_queue[index] = (context.id, row.name, row.target_version)
                row.status = "wait"
                self.set_message(f"Already queued: {row.display_name}", "warn")
                return
        self.wait_queue.append((context.id, row.name, row.target_version))
        row.status = "wait"
        self.message = f"Queued ({len(self.wait_queue)} waiting): {row.display_name}"

    def begin_install(self, row: PackageRow) -> bool:
        context = self.context
        if context is None:
            return False
        if not self.row_target_installable(row):
            self.set_message(f"Version data unavailable for {row.display_name}", "warn")
            return False
        package_spec = f"{row.display_name}=={row.target_version}"
        try:
            command = uv_command(context, ["pip", "install", "--python", context.uv_target])
        except Exception as exc:
            row.status = "failed"
            row.last_error = "uv provider unavailable"
            row.last_error_detail = str(exc)
            self.set_message("uv provider unavailable", "error")
            return False
        if context.type == "interpreter":
            command.append("--system")
        command.append(package_spec)
        row.status = "installing"
        row.last_error = None
        row.last_error_detail = None
        row.last_install_result = None
        self.message = f"Installing {package_spec}"
        self.run_package_operation(
            context,
            command,
            operation="install",
            package_name=row.name,
            display_name=row.display_name,
            target_version=row.target_version,
            installed_version_at_attempt=row.installed_version,
            candidate_versions_at_attempt=list(row.candidate_versions),
        )
        return True

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
        self.installing = True
        self.active_install_context_id = context.id
        self.install_cancelled = False
        before_versions = {item.name: item.installed_version for item in self.rows}
        candidates = candidate_versions_at_attempt or []

        def worker() -> None:
            start = time.time()
            proc: subprocess.Popen[str] | None = None
            try:
                proc = subprocess.Popen(
                    command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                )
                with self.install_proc_lock:
                    self.install_proc = proc
                stdout, stderr = proc.communicate()
                elapsed = time.time() - start
                result = InstallResult(
                    context_id=context.id,
                    package_name=package_name,
                    target_version=target_version,
                    command=command,
                    returncode=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    elapsed=elapsed,
                    before_versions=before_versions,
                    installed_version_at_attempt=installed_version_at_attempt,
                    exit_code=proc.returncode,
                    stdout_tail=tail_lines(stdout),
                    stderr_tail=tail_lines(stderr),
                    candidate_versions_at_attempt=candidates,
                    operation=operation,
                    display_name=display_name,
                    cancelled=self.install_cancelled,
                )
            except Exception as exc:
                elapsed = time.time() - start
                stderr_text = str(exc)
                result = InstallResult(
                    context_id=context.id,
                    package_name=package_name,
                    target_version=target_version,
                    command=command,
                    returncode=1,
                    stdout="",
                    stderr=stderr_text,
                    elapsed=elapsed,
                    before_versions=before_versions,
                    installed_version_at_attempt=installed_version_at_attempt,
                    exit_code=None,
                    stdout_tail=[],
                    stderr_tail=tail_lines(stderr_text),
                    candidate_versions_at_attempt=candidates,
                    operation=operation,
                    display_name=display_name,
                    cancelled=self.install_cancelled,
                )
            finally:
                with self.install_proc_lock:
                    self.install_proc = None
            self.event_queue.put(("install_done", result))

        threading.Thread(target=worker, daemon=True).start()

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
        if (context.resolved_uv_provider or refresh_context_uv_provider(context)) is None:
            captured = row.name
            self.ensure_uv_provider(context, lambda: self.request_uninstall(captured))
            return
        lines = [f"Uninstall {row.display_name} {row.installed_version} from:", context.label, ""]
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            lines.append(f"This modifies interpreter {context.python_path}.")
            lines.append("")
        if not row.metadata_trusted:
            lines.append("Dependency data is unavailable - safety cannot be verified.")
        elif row.usage_packages:
            users = ", ".join(row.usage_packages[:8])
            suffix = ", ..." if len(row.usage_packages) > 8 else ""
            lines.append(f"WARNING - required by: {users}{suffix}")
            lines.append("Uninstalling may break those packages.")
        else:
            lines.append("Not required by any other installed package.")
        lines.append("")
        lines.append("Proceed? y/N")
        captured = row.name
        self.prompt = Prompt(
            title="Confirm uninstall",
            message="\n".join(lines),
            on_yes=lambda: self.confirm_uninstall(context, captured),
            on_no=lambda: setattr(self, "message", "Uninstall cancelled"),
        )

    def confirm_uninstall(self, context: PythonContext, package_name: str) -> None:
        self.prompt = None
        if self.context is None or self.context.id != context.id:
            return
        row = self.find_row(package_name)
        if row is None or self.installing:
            return
        if context.type == "interpreter":
            context.confirmed_for_mutation = True
        try:
            command = uv_command(context, ["pip", "uninstall", "--python", context.uv_target])
        except Exception as exc:
            self.set_message(f"uv provider unavailable: {last_lines(str(exc), 1)}", "error")
            return
        if context.type == "interpreter":
            command.append("--system")
        command.append(row.display_name)
        row.status = "installing"
        row.last_error = None
        row.last_error_detail = None
        self.message = f"Uninstalling {row.display_name}"
        self.run_package_operation(
            context,
            command,
            operation="uninstall",
            package_name=row.name,
            display_name=row.display_name,
            target_version=row.installed_version,
            installed_version_at_attempt=row.installed_version,
        )

    def request_new_package(self) -> None:
        context = self.context
        if context is None:
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
        normalized = canonicalize_name(raw)
        existing = self.find_row(normalized)
        if existing is not None:
            self.filter_text = ""
            self.update_view()
            self.focus_on_package(normalized)
            self.set_message(f"{existing.display_name} is already installed; use arrows or F4 to change versions", "warn")
            return
        self.start_new_package_lookup(raw)

    def start_new_package_lookup(self, display_name: str) -> None:
        if self.version_loading:
            self.set_message("Version lookup already in progress", "warn")
            return
        self.version_loading = True
        self.version_lookup_token += 1
        token = self.version_lookup_token
        self.message = f"Loading versions for {display_name}"

        def worker() -> None:
            try:
                versions, yanked = fetch_available_versions(display_name)
                self.event_queue.put(("new_versions_done", (token, display_name, versions, yanked, None)))
            except Exception as exc:
                self.event_queue.put(("new_versions_done", (token, display_name, [], set(), str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def on_new_versions_done(
        self,
        token: int,
        display_name: str,
        versions: list[str],
        yanked: set[str],
        error: str | None,
    ) -> None:
        if token != self.version_lookup_token:
            return
        self.version_loading = False
        if not versions:
            detail = last_lines(error or "no versions found", 1)
            self.set_message(f"No installable versions for {display_name}: {detail}", "error")
            return
        ordered = sorted(set(versions), key=version_key, reverse=True)
        self.new_package = {"display": display_name, "versions": ordered, "yanked": set(yanked)}
        self.version_overlay = True
        self.version_overlay_row = None
        self.version_overlay_scroll = 0
        self.version_options = ordered
        self.version_overlay_index = next((i for i, v in enumerate(ordered) if v not in yanked), 0)
        self.version_error = None
        self.pending_version_direction = None
        self.ensure_version_overlay_visible()
        self.message = f"Select a version for {display_name}"

    def install_new_selected(self, version: str) -> None:
        context = self.context
        info = self.new_package
        if context is None or info is None:
            return
        display_name = str(info.get("display", ""))
        self.version_overlay = False
        self.new_package = None
        if not display_name:
            return
        if (context.resolved_uv_provider or refresh_context_uv_provider(context)) is None:
            self.ensure_uv_provider(context, lambda: self.begin_install_new(display_name, version))
            return
        if context.type == "interpreter" and not context.confirmed_for_mutation:
            self.prompt = Prompt(
                title="Confirm interpreter install",
                message=f"Install {display_name}=={version} into interpreter {context.python_path}? y/N",
                on_yes=lambda: self.confirm_interpreter_and_install_new(context, display_name, version),
                on_no=lambda: setattr(self, "message", "Install cancelled"),
            )
            return
        self.begin_install_new(display_name, version)

    def confirm_interpreter_and_install_new(self, context: PythonContext, display_name: str, version: str) -> None:
        self.prompt = None
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
            command = uv_command(context, ["pip", "install", "--python", context.uv_target])
        except Exception as exc:
            self.set_message(f"uv provider unavailable: {last_lines(str(exc), 1)}", "error")
            return
        if context.type == "interpreter":
            command.append("--system")
        command.append(f"{display_name}=={version}")
        self.message = f"Installing {display_name}=={version}"
        self.run_package_operation(
            context,
            command,
            operation="install",
            package_name=canonicalize_name(display_name),
            display_name=display_name,
            target_version=version,
        )

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
        if re.search(r'[<>:"|?*]', raw) or "/" in raw or "\\" in raw or raw in {".", ".."}:
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
                if proc.returncode != 0:
                    detail = command_detail(command, proc.returncode, proc.stdout, proc.stderr, 0)
                    self.event_queue.put(("venv_created", (str(root), detail)))
                else:
                    self.event_queue.put(("venv_created", (str(root), None)))
            except Exception as exc:
                self.event_queue.put(("venv_created", (str(root), str(exc))))

        threading.Thread(target=worker, daemon=True).start()

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
        pins = self.pinned_by_context.setdefault(context.id, set())
        if row.name in pins:
            pins.discard(row.name)
            self.message = f"Unpinned {row.display_name}"
        else:
            pins.add(row.name)
            self.message = f"Pinned {row.display_name} (excluded from update all)"
        save_pins(self.pinned_by_context)

    def toggle_selection(self) -> None:
        row = self.focused_row()
        if row is None or row.operational_error:
            return
        if row.name in self.selected_names:
            self.selected_names.discard(row.name)
        else:
            self.selected_names.add(row.name)
        count = len(self.selected_names)
        self.message = f"Selected {count} package(s)" if count else "Selection cleared"
        self.move_focus(1)

    def render(self) -> str:
        width, height = self.terminal.size()
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            message = f"Terminal too small ({width}x{height}); Tuv needs at least {MIN_WIDTH}x{MIN_HEIGHT}."
            filler = max(0, height - 1)
            return "\n".join([truncate(message, max(1, width))] + [" " * max(1, width)] * filler)
        lines: list[str] = []
        lines.append(self.header_line(width))
        lines.append(self.separator(width))
        lines.append(self.table_header(width))

        visible_rows = max(1, height - 6)
        for absolute_index in range(self.scroll, self.scroll + visible_rows):
            if absolute_index < len(self.view):
                lines.append(self.render_row(width, absolute_index, self.view[absolute_index]))
            else:
                lines.append(" " * width)

        lines.append(self.separator(width))
        lines.append(self.status_line(width))
        lines.append(self.footer_line(width))

        text_input_open = self.input_mode in {"new_package", "new_venv"}
        if self.context_overlay or self.version_overlay or self.info_open or self.report_open or self.prompt or text_input_open:
            lines = self.dim_background(lines, width)
        if self.context_overlay:
            lines = self.overlay_contexts(lines, width, height)
        if self.version_overlay:
            lines = self.overlay_versions(lines, width, height)
        if self.info_open:
            lines = self.overlay_info(lines, width, height)
        if self.report_open:
            lines = self.overlay_report(lines, width, height)
        if text_input_open:
            lines = self.overlay_input(lines, width, height)
        if self.prompt:
            lines = self.overlay_prompt(lines, width, height)

        return "\n".join(lines[:height])

    def separator(self, width: int) -> str:
        return "─" * width

    def header_line(self, width: int) -> str:
        context = self.context
        if context is not None:
            label = f"{context.label} [{provider_label(context.resolved_uv_provider)}]"
        elif self.discovery_error:
            label = self.discovery_error
        elif self.discovering_contexts:
            label = "Discovering Python contexts..."
        else:
            label = "Starting..."
        return pad_display(truncate(f"[ {label} ]", width), width)

    def busy(self) -> bool:
        return (
            self.installing
            or self.refreshing
            or self.outdated_loading
            or self.version_loading
            or self.dependency_loading
            or self.discovering_contexts
            or self.creating_venv
        )

    def status_line(self, width: int) -> str:
        if self.input_mode == "filter":
            text = f"Filter: {self.input_buffer}_  (Enter keep, Esc clear)"
            return REVERSE + pad_display(truncate(text, width), width) + RESET
        prefix = f"{SPINNER[self.spinner_index]} " if self.busy() else "  "
        extras: list[str] = []
        if self.bulk_active and self.bulk_total:
            extras.append(f"update {self.bulk_total - len(self.bulk_queue)}/{self.bulk_total}")
        if self.wait_queue:
            extras.append(f"{len(self.wait_queue)} queued")
        if self.selected_names:
            extras.append(f"{len(self.selected_names)} selected")
        if self.filter_text:
            extras.append(f"filter: {self.filter_text}")
        text = prefix + self._message
        if extras:
            text = f"{text}  [{' | '.join(extras)}]"
        line = pad_display(truncate(text, width), width)
        if self._message_kind == "error":
            return BOLD_RED + line + RESET
        if self._message_kind == "warn":
            return YELLOW + line + RESET
        return line

    def table_header(self, width: int) -> str:
        name_w, installed_w, target_w, action_w = self.columns(width)
        text = (
            pad_display("Package", name_w)
            + pad_display("Installed", installed_w)
            + pad_display("Target", target_w)
            + pad_display("Action", action_w)
        )
        return pad_display(truncate(text, width), width)

    def render_row(self, width: int, index: int, row: PackageRow) -> str:
        name_w, installed_w, target_w, action_w = self.columns(width)
        action = self.display_status(row)
        selected_marker = "+" if row.name in self.selected_names else " "
        safe_marker = "*" if row.metadata_trusted and row.uninstall_safe else " "
        name = f"{selected_marker}{safe_marker} {row.display_name}"
        text = (
            pad_display(truncate(name, name_w), name_w)
            + pad_display(truncate(row.installed_version, installed_w), installed_w)
            + pad_display(truncate(row.target_version, target_w), target_w)
            + pad_display(truncate(action, action_w), action_w)
        )
        line = pad_display(truncate(text, width), width)
        style = ""
        loading_color_hint = row.color_hint if row.status == "loading" else None
        if index == self.focus_index:
            style += REVERSE
        if row.status == "failed":
            style += BOLD_RED
        elif row.updated_in_session or loading_color_hint == "updated":
            style += BRIGHT_CYAN
        elif row.status == "current" or loading_color_hint == "current":
            style += LIGHT_GREEN
        elif row.is_outdated or loading_color_hint == "outdated":
            style += YELLOW
        elif row.status in {"loading", "nodata"}:
            style += DIM
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
        if row.status == "nodata":
            return "no data"
        if row.name in self.pinned_names() and row.status in {"ready", "current", "loading", "nodata"}:
            return "pinned"
        return row.status

    def footer_line(self, width: int) -> str:
        if width >= 130:
            keys = (
                "↑/↓ | ←/→ Ver | ↵ Install | Space Sel | / Filter | i New | Del Unins | p Pin"
                " | F2 Update | F3 Info | F4 Ver | F5 Rescan | F9 Ctx | F10 Quit"
            )
        elif width >= 100:
            keys = "↑/↓ | ←/→ | ↵ Inst | Spc Sel | / Filt | i New | Del | p Pin | F2 All | F3 | F4 | F5 | F9 | F10"
        elif width >= 72:
            keys = "↑/↓ | ←/→ | ↵ | Spc | / | i | Del | p | F2 | F3 | F4 | F5 | F9 | F10"
        else:
            keys = "↑/↓ ←/→ ↵ Spc / i Del p F2-F10"
        return pad_display(truncate(keys, width), width)

    def dim_background(self, lines: list[str], width: int) -> list[str]:
        return [MODAL_BACKDROP + strip_ansi(line).ljust(width)[:width] + RESET for line in lines]

    def overlay_contexts(self, lines: list[str], width: int, height: int) -> list[str]:
        overlay_w = min(width - 4, max(50, width * 3 // 4))
        overlay_h = min(height - 4, max(7, len(self.contexts) + 5))
        top = max(1, (height - overlay_h) // 2)
        left = max(1, (width - overlay_w) // 2)
        items_visible = overlay_h - 3
        start = max(0, min(self.context_overlay_index, max(0, len(self.contexts) - items_visible)))
        box = [self.box_border(overlay_w, "top", "Context selector")]
        box.append(self.box_line("Enter select | n new venv | F5 rescan | Esc close", overlay_w))
        for idx in range(start, start + items_visible):
            if idx < len(self.contexts):
                marker = ">" if idx == self.context_overlay_index else " "
                status = provider_label(self.contexts[idx].resolved_uv_provider)
                label = f"{marker} {self.contexts[idx].label} [{status}]"
            else:
                label = ""
            content = self.box_line(label, overlay_w)
            if idx == self.context_overlay_index:
                content = REVERSE + content + RESET
            box.append(content)
        box.append(self.box_border(overlay_w, "bottom"))
        return paste_box(lines, box, top, left, width)

    def overlay_versions(self, lines: list[str], width: int, height: int) -> list[str]:
        row = self.find_row(self.version_overlay_row or "") if self.new_package is None else None
        if self.new_package is not None:
            title = f"Versions: {self.new_package.get('display', '')} (new install)"
        else:
            title = f"Versions: {row.display_name if row else ''}".strip()
        yanked = row.yanked_versions if row is not None else self.new_package_yanked()
        overlay_w = min(width - 4, max(46, width // 2))
        overlay_h = min(height - 4, max(8, min(len(self.version_options) + 5, height - 4)))
        top = max(1, (height - overlay_h) // 2)
        left = max(1, (width - overlay_w) // 2)
        items_visible = overlay_h - 3
        self.ensure_version_overlay_visible(items_visible)
        start = self.version_overlay_scroll
        box = [self.box_border(overlay_w, "top", title)]
        hint = "Enter install | Esc close"
        if self.version_loading:
            hint = "Loading versions... | Esc close"
        elif self.version_error:
            hint = f"{self.version_error} | Esc close"
        box.append(self.box_line(hint, overlay_w))
        for idx in range(start, start + items_visible):
            if idx < len(self.version_options):
                version = self.version_options[idx]
                marker = ">" if idx == self.version_overlay_index else " "
                current = " installed" if row and version == row.installed_version else ""
                target = " target" if row and version == row.target_version else ""
                yanked_tag = " (yanked)" if version in yanked else ""
                label = f"{marker} {version}{yanked_tag}{current}{target}"
            else:
                label = ""
            content = self.box_line(label, overlay_w)
            if idx == self.version_overlay_index:
                content = REVERSE + content + RESET
            box.append(content)
        box.append(self.box_border(overlay_w, "bottom"))
        return paste_box(lines, box, top, left, width)

    def package_relation_lines(self, row: PackageRow) -> list[str]:
        if not row.metadata_trusted:
            return [
                "",
                "Description:",
                "  (metadata unavailable)",
                "",
                "Dependency packages:",
                "  (metadata unavailable)",
                "",
                "Usage packages:",
                "  (metadata unavailable)",
            ]
        lines = [
            "",
            "Description:",
            f"  {row.description or '(empty)'}",
            "",
            "Dependency packages:",
        ]
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
        elif row.operational_error:
            body = [
                "Tuv operational error",
                "",
                row.last_error or row.display_name,
                "",
                f"Runner: {runner_python_path()}",
                "",
                *(row.last_error_detail or "").splitlines(),
            ]
        elif row.status == "failed" and row.last_error_detail:
            result = row.last_install_result
            exit_code = (
                str(result.exit_code)
                if result is not None and result.exit_code is not None
                else "process did not start"
            )
            elapsed = f"{result.elapsed:.1f}s" if result is not None else "unknown"
            body = [
                f"Package: {row.display_name}",
                f"Version: {result.installed_version_at_attempt if result else row.installed_version}",
                f"Exit code: {exit_code}",
                f"Elapsed: {elapsed}",
                *self.package_relation_lines(row),
                "",
                row.last_error or "Install failed",
                "",
                *row.last_error_detail.splitlines(),
            ]
        else:
            pinned = "yes (excluded from update all)" if row.name in self.pinned_names() else "no"
            body = [
                f"Package: {row.display_name}",
                f"Version: {row.installed_version}",
                f"Pinned: {pinned}",
                *self.package_relation_lines(row),
            ]
        return self.overlay_text(lines, width, height, "Information", body, scroll_attr="info_scroll")

    def overlay_report(self, lines: list[str], width: int, height: int) -> list[str]:
        body = self.report_lines or ["(empty)"]
        return self.overlay_text(lines, width, height, self.report_title or "Report", body, scroll_attr="report_scroll")

    def overlay_input(self, lines: list[str], width: int, height: int) -> list[str]:
        if self.input_mode == "new_package":
            title = "Install new package"
            prompt_text = "Package name:"
        else:
            title = "Create venv in current directory"
            prompt_text = f"Directory name (created in {Path.cwd()}):"
        body = [prompt_text, f"> {self.input_buffer}_", "", "Enter: confirm    Esc: cancel"]
        return self.overlay_text(lines, width, height, title, body)

    def overlay_prompt(self, lines: list[str], width: int, height: int) -> list[str]:
        prompt = self.prompt
        if prompt is None:
            return lines
        body = [*prompt.message.splitlines(), "", "Y/Enter: yes    N/Esc: no"]
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
        overlay_h = min(height - 4, max(6, min(len(wrapped) + 3, height - 4)))
        top = max(1, (height - overlay_h) // 2)
        left = max(1, (width - overlay_w) // 2)
        visible = overlay_h - 2
        scroll = 0
        if scroll_attr is not None:
            scroll = max(0, int(getattr(self, scroll_attr, 0)))
            scroll = min(scroll, max(0, len(wrapped) - visible))
            setattr(self, scroll_attr, scroll)
            if len(wrapped) > visible:
                title = f"{title} ({scroll + 1}-{min(scroll + visible, len(wrapped))}/{len(wrapped)})"
        box = [self.box_border(overlay_w, "top", title)]
        for line in wrapped[scroll : scroll + visible]:
            box.append(self.box_line(line, overlay_w))
        while len(box) < overlay_h - 1:
            box.append(self.box_line("", overlay_w))
        box.append(self.box_border(overlay_w, "bottom"))
        return paste_box(lines, box, top, left, width)

    def box_border(self, width: int, kind: str = "top", title: str | None = None) -> str:
        if kind == "bottom":
            return "└" + "─" * (width - 2) + "┘"
        if title:
            inner = max(0, width - 2)
            if inner <= 0:
                return "┌┐"
            max_title_width = max(0, inner - 3)
            title_text = truncate(title, max_title_width)
            label = f" {title_text} " if title_text else " "
            if len(label) >= inner:
                label = truncate(label, inner)
            fill = "─" * max(0, inner - len(label))
            return "┌" + label + fill + "┐"
        return "┌" + "─" * (width - 2) + "┐"

    def box_line(self, text: str, width: int) -> str:
        return "│" + pad_display(truncate(" " + text, width - 2), width - 2) + "│"


def char_display_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_display_width(ch) for ch in text)


def truncate(value: object, width: int) -> str:
    text = str(value)
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
        print("tuv 0.2.0")
        return 0
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            "Usage: tuv.py [--version]\n\n"
            "Start the Tuv alternate-screen package manager.\n"
            "Run through tuv.bat / tuv.sh to bootstrap the runner environment;\n"
            "pass '.' to the launcher to use the current directory's Python as runner."
        )
        return 0
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("Tuv needs an interactive terminal (stdin and stdout must be a TTY).", file=sys.stderr)
            return 1
    except Exception:
        print("Tuv needs an interactive terminal (stdin and stdout must be a TTY).", file=sys.stderr)
        return 1
    try:
        return TuvApp().run()
    except Exception:
        print("Tuv crashed before the terminal UI could recover:", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

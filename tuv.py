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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

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


def uv_version_text(output: str) -> str:
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    return first_line or "unknown"


def validate_uv_command(command: list[str], timeout: float = 5.0) -> str | None:
    try:
        proc = run_command([*command, "--version"], timeout=timeout)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return uv_version_text(proc.stdout or proc.stderr)


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
    provider = refresh_context_uv_provider(context)
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


def discover_python_infos() -> list[PythonInfo]:
    installed_candidates = windows_python_candidates() if IS_WINDOWS else posix_python_candidates()
    raw_candidates = (
        [(candidate, "cwd") for candidate in cwd_python_candidates()]
        + [(candidate, "installed") for candidate in launcher_python_candidates()]
        + [(candidate, "installed") for candidate in installed_candidates]
    )
    seen: set[str] = set()
    infos: list[PythonInfo] = []
    by_key: dict[str, PythonInfo] = {}
    for candidate, source in raw_candidates:
        info = probe_python(candidate, source=source)
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
    seen: set[str] = set()
    infos: list[PythonInfo] = []
    for candidate in candidates:
        info = probe_runner_python(candidate, source=source)
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


def prepare_runner_environment(mode: str) -> tuple[PythonInfo, Path]:
    if mode not in {"default", "cwd"}:
        raise RuntimeError(f"Unsupported launcher mode: {mode}")
    info = select_runner_python(mode)
    key = runner_compatibility_key(info, mode)
    runner = find_compatible_runner_venv(key)
    if runner is not None:
        return info, runner

    runner, hash_value = new_runner_venv_path(key)
    proc = run_command([str(info.executable), "-m", "venv", str(runner)], timeout=None)
    if proc.returncode != 0:
        detail = command_detail([str(info.executable), "-m", "venv", str(runner)], proc.returncode, proc.stdout, proc.stderr, 0)
        raise RuntimeError(detail)
    write_runner_state(runner, info, mode, key, hash_value)
    return info, runner


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
    refresh_context_uv_provider(context)
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
        refresh_context_uv_provider(context)
        add(
            interpreter_contexts,
            context,
        )

    active = os.environ.get("VIRTUAL_ENV")
    if active:
        add(venv_contexts, context_from_venv("venv", Path(active), "active venv", "active"))

    cwd = Path.cwd()
    if is_venv(cwd):
        add(venv_contexts, context_from_venv("venv", cwd, cwd.name or str(cwd), "cwd"))
    for child in sorted((item for item in cwd.iterdir() if item.is_dir()), key=lambda p: p.name.lower()):
        if child.resolve() == RUNNER_VENV:
            continue
        add(venv_contexts, context_from_venv("venv", child, child.name, "scanned"))

    add(tuv_contexts, context_from_venv("tuv", RUNNER_VENV, "tuv venv", "tuv"))
    return interpreter_contexts + venv_contexts + tuv_contexts


def run_uv_json(context: PythonContext, args: list[str], timeout: float | None = 90.0) -> tuple[object, str]:
    cmd = uv_command(context, args)
    proc = run_command(cmd, timeout=timeout)
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

def put(name, summary, requires):
    if not name:
        return
    key = norm(name)
    current = result.setdefault(key, {"name": name, "summary": "", "requires": []})
    if summary and not current["summary"]:
        current["summary"] = str(summary)
    current["requires"].extend(str(req) for req in (requires or []) if req)

try:
    for dist in metadata.distributions():
        meta = dist.metadata
        put(meta.get("Name"), meta.get("Summary"), list(dist.requires or meta.get_all("Requires-Dist") or []))
except Exception:
    pass

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
            put(msg.get("Name"), msg.get("Summary"), msg.get_all("Requires-Dist") or [])
    except Exception:
        continue

env = {
    "implementation_name": sys.implementation.name,
    "implementation_version": platform.python_version(),
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
        deps = {
            dep
            for req in requires
            if isinstance(req, str)
            for dep in [dependency_name_from_requirement(req, marker_env)]
            if dep
        }
        result[canonicalize_name(package_name)] = PackageMetadata(
            description=short_description(summary),
            dependencies=deps,
        )
    return result


def dependency_name_from_requirement(requirement: str, marker_env: dict[str, object]) -> str | None:
    if Requirement is not None:
        try:
            parsed = Requirement(requirement)
            if parsed.marker is not None:
                env = dict(default_environment() if default_environment is not None else {})
                env.update({str(key): str(value) for key, value in marker_env.items()})
                try:
                    if not parsed.marker.evaluate(environment=env):
                        return None
                except Exception:
                    pass
            return canonicalize_name(parsed.name)
        except InvalidRequirement:
            pass
    head = re.split(r"[<>=!~;\[\s(]", requirement, 1)[0].strip()
    return canonicalize_name(head) if head else None


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
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value)


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


def discover_local_index_url() -> str | None:
    for directory in [Path.cwd(), *Path.cwd().parents]:
        for filename in ("uv.toml", "pyproject.toml"):
            path = directory / filename
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for key in ("default-index", "index-url", "index_url", "url"):
                match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", text)
                if match:
                    return match.group(1)
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


def versions_from_simple_json(data: object, package_name: str) -> set[str]:
    versions: set[str] = set()
    if not isinstance(data, dict):
        return versions
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
            if version:
                versions.add(version)
    return versions


def versions_from_simple_html(text: str, package_name: str) -> set[str]:
    parser = SimpleRepositoryLinkParser()
    parser.feed(text)
    versions: set[str] = set()
    for href in parser.hrefs:
        version = version_from_distribution_filename(href, package_name)
        if version:
            versions.add(version)
    return versions


def fetch_available_versions(package_name: str, timeout: float = 12.0) -> list[str]:
    versions: set[str] = set()
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
                versions.update(versions_from_simple_json(json.loads(body), package_name))
            else:
                versions.update(versions_from_simple_html(body, package_name))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if versions:
        return sorted(versions, key=version_key)
    if errors:
        raise RuntimeError("Version lookup failed: " + "; ".join(errors[-3:]))
    return []


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
        self.target_resolution_loading = False
        self.dependency_loading = False
        self.pending_after_refresh_action = False
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.updated_by_context: dict[str, set[str]] = {}
        self.installing = False
        self.active_install_context_id: str | None = None
        self.wait_request: tuple[str, str, str] | None = None
        self.bulk_active = False
        self.bulk_queue: list[tuple[str, str]] = []
        self.bulk_processed: set[str] = set()
        self.bulk_failed_results: dict[str, InstallResult] = {}
        self.bulk_run_id: str | None = None
        self.bulk_run_counter = 0
        self.prompt: Prompt | None = None
        self.info_open = False
        self.info_scroll = 0
        self.mutation_blocked_reason: str | None = None
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
        self.bulk_run_id = None
        self.outdated_loading = False
        self.target_resolution_loading = False
        self.dependency_loading = False
        self.pending_after_refresh_action = False
        self.info_open = False
        self.info_scroll = 0
        self.mutation_blocked_reason = None
        self.message = f"Loading {context.label}"
        self.ensure_uv_provider(context, lambda: self.start_refresh(context, "Loading packages"))

    def ensure_uv_provider(self, context: PythonContext, on_ready: Callable[[], None]) -> None:
        if refresh_context_uv_provider(context) is not None:
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
        self.target_resolution_loading = False
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
                row_name, versions, error = payload  # type: ignore[misc]
                self.on_versions_done(row_name, versions, error)
            elif event == "target_versions_done":
                context_id, generation, results = payload  # type: ignore[misc]
                self.on_target_versions_done(context_id, generation, results)

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
        for candidate in self.contexts:
            refresh_context_uv_provider(candidate)
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

        self.rows = rows
        self.focus_index = min(self.focus_index, max(0, len(self.rows) - 1))
        self.scroll = min(self.scroll, max(0, len(self.rows) - 1))
        self.restore_bulk_failed_rows()

        if install_result is not None:
            self.installing = False
            self.active_install_context_id = None
            self.pending_after_refresh_action = True
            if install_result.ok:
                self.message = f"Installed {install_result.package_name}=={install_result.target_version}"
            else:
                self.mark_failed_row(install_result)
                self.message = f"Install failed: {install_result.package_name}"
        elif warning:
            self.message = warning
        else:
            self.message = f"Loaded {len(self.rows)} packages"

        context = self.context
        if context is not None and context.id == context_id and not self.installing:
            self.start_dependency_refresh(context, generation)
            self.start_outdated_refresh(context, generation)
        else:
            self.finish_full_refresh()

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
        resolution_items: list[tuple[str, str, str]] = []
        for row in self.rows:
            latest = targets.get(row.name)
            if row.status == "failed" and row.last_install_result is not None:
                continue
            if not latest:
                if row.status not in {"failed", "installing", "wait"}:
                    row.target_version = row.installed_version
                    row.status = "current"
                continue
            row.target_version = latest
            row.versions_resolved = False
            if row.status not in {"failed", "installing", "wait"}:
                row.status = "loading"
            if latest != row.installed_version:
                resolution_items.append((row.name, row.display_name, latest))
            elif row.status not in {"failed", "installing", "wait"}:
                row.status = "current"
        if resolution_items:
            self.start_target_version_resolution(context_id, generation, resolution_items)
        else:
            self.target_resolution_loading = False
            self.finish_full_refresh()
        if warning:
            self.message = warning
        elif resolution_items:
            self.message = f"Resolving target versions for {len(resolution_items)} package(s)"
        elif targets:
            self.message = "Latest target versions loaded"

    def start_target_version_resolution(
        self,
        context_id: str,
        generation: int,
        items: list[tuple[str, str, str]],
    ) -> None:
        self.target_resolution_loading = True

        def worker() -> None:
            results: dict[str, tuple[list[str], str | None]] = {}
            for normalized, display_name, target in items:
                try:
                    versions = fetch_available_versions(display_name)
                    if target not in versions:
                        results[normalized] = (
                            versions,
                            f"Resolved versions for {display_name} did not include target {target}",
                        )
                    else:
                        results[normalized] = (versions, None)
                except Exception as exc:
                    results[normalized] = ([], str(exc))
            self.event_queue.put(("target_versions_done", (context_id, generation, results)))

        threading.Thread(target=worker, daemon=True).start()

    def on_target_versions_done(
        self,
        context_id: str,
        generation: int,
        results: dict[str, tuple[list[str], str | None]],
    ) -> None:
        if self.context is None or self.context.id != context_id or generation != self.refresh_generation:
            return
        self.target_resolution_loading = False
        failed = 0
        resolved = 0
        for row in self.rows:
            result = results.get(row.name)
            if result is None:
                continue
            versions, error = result
            if versions and error is None and row.target_version in versions:
                row.candidate_versions = sorted(set(versions), key=version_key)
                row.versions_resolved = True
                row.last_error = None
                row.last_error_detail = None
                if row.status not in {"failed", "installing", "wait"}:
                    row.status = "ready" if row.target_version != row.installed_version else "current"
                resolved += 1
            else:
                failed += 1
                row.versions_resolved = False
                if versions:
                    row.candidate_versions = sorted(set(versions), key=version_key)
                row.status = "failed"
                row.last_error = "Version lookup failed"
                row.last_error_detail = error or f"No installable versions found for {row.display_name}"
        if failed:
            self.message = f"Version resolution failed for {failed} package(s)"
        elif resolved:
            self.message = f"Resolved target versions for {resolved} package(s)"
        self.finish_full_refresh()

    def finish_full_refresh(self) -> None:
        if self.refreshing or self.outdated_loading or self.target_resolution_loading:
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
        changed = self.updated_by_context.setdefault(context_id, set())
        for row in rows:
            previous = before_versions.get(row.name)
            if previous is None or previous != row.installed_version:
                changed.add(row.name)

    def mark_failed_row(self, result: InstallResult) -> None:
        if self.bulk_active:
            result.failed_in_bulk_run_id = self.bulk_run_id
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
        if result.candidate_versions_at_attempt:
            row.candidate_versions = sorted(set(result.candidate_versions_at_attempt), key=version_key)
        row.target_version = result.target_version
        row.versions_resolved = result.target_version in row.candidate_versions
        row.status = "failed"
        exit_text = str(result.exit_code) if result.exit_code is not None else "process did not start"
        row.last_error = f"Install failed with exit code {exit_text}"
        row.last_error_detail = detail
        row.last_install_result = result

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
        if not row.versions_resolved or target_version not in row.candidate_versions:
            self.message = f"Waiting package no longer has resolved target: {row.display_name}"
            return
        row.target_version = target_version
        self.begin_install(row)

    def start_bulk_update(self) -> None:
        if self.mutation_blocked_reason:
            self.message = self.mutation_blocked_reason
            return
        if self.installing:
            self.message = "Update all waits until the current activity finishes"
            return
        if self.version_resolution_busy():
            self.message = "Update all waits for version resolution"
            return
        context = self.context
        if context is None:
            return
        if refresh_context_uv_provider(context) is None:
            self.ensure_uv_provider(context, self.start_bulk_update)
            return
        seen: set[str] = set()
        queue_items: list[tuple[str, str]] = []
        for row in self.rows:
            if row.name in seen:
                continue
            seen.add(row.name)
            if row.status == "ready" and self.row_target_installable(row):
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
        self.bulk_run_counter += 1
        self.bulk_run_id = f"bulk-{self.bulk_run_counter}"
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
                if row.status != "failed":
                    row.status = "skipped"
                self.bulk_processed.add(normalized)
                continue
            if not row.versions_resolved or target_version not in row.candidate_versions:
                row.status = "skipped"
                self.bulk_processed.add(normalized)
                continue
            row.target_version = target_version
            self.bulk_processed.add(normalized)
            self.mark_bulk_pending_waits()
            self.begin_install(row)
            return
        self.bulk_active = False
        self.bulk_processed = set()
        self.bulk_failed_results = {}
        self.bulk_run_id = None
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
        if row.versions_resolved:
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
        if row.status not in {"installing", "wait"} and not (row.status == "failed" and row.last_install_result is not None):
            row.status = "loading"
            row.last_error = None
            row.last_error_detail = None
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
        if not row.versions_resolved:
            self.message = f"Version lookup is not ready for {row.display_name}"
            return
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
            row.candidate_versions = sorted(set(versions), key=version_key)
            row.versions_resolved = True
            row.last_error = None
            row.last_error_detail = None
            self.refresh_version_options(row)
            self.version_error = None
            self.message = f"Loaded {len(self.version_options)} versions for {row.display_name}"
        else:
            row.versions_resolved = False
            self.refresh_version_options(row)
            self.version_error = f"Version lookup failed: {last_lines(error or 'no versions found', 2)}"
            self.message = self.version_error
            if row.status == "loading":
                row.status = "failed"
                row.last_error = "Version lookup failed"
                row.last_error_detail = error or "No installable versions found"
        if row.status == "loading" and versions:
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
                self.ensure_uv_provider(context, lambda: self.start_refresh(context, "Refreshing packages"))

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
            if self.version_loading or self.version_error or not row.versions_resolved:
                self.message = f"Version lookup is not ready for {row.display_name}"
                return
            selected = self.version_options[self.version_overlay_index]
            if selected not in row.candidate_versions and selected != row.installed_version:
                self.message = f"Version data unavailable for {row.display_name}"
                return
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
        if not row.versions_resolved:
            self.start_version_lookup(row, pending_direction=direction)
            return
        self.apply_version_direction(row, direction)

    def focused_row(self) -> PackageRow | None:
        if not self.rows:
            return None
        self.focus_index = max(0, min(self.focus_index, len(self.rows) - 1))
        return self.rows[self.focus_index]

    def version_resolution_busy(self) -> bool:
        return self.refreshing or self.outdated_loading or self.target_resolution_loading or self.version_loading

    def row_target_installable(self, row: PackageRow) -> bool:
        return (
            row.target_version != row.installed_version
            and row.versions_resolved
            and row.target_version in row.candidate_versions
            and row.status not in {"loading", "installing"}
        )

    def block_unresolved_action(self, row: PackageRow | None = None) -> bool:
        if self.version_resolution_busy():
            self.message = "Version resolution is still in progress"
            return True
        if row is not None and row.target_version != row.installed_version and not self.row_target_installable(row):
            self.message = f"Version data unavailable for {row.display_name}"
            return True
        return False

    def request_install(self) -> None:
        context = self.context
        row = self.focused_row()
        if context is None or row is None:
            return
        if self.mutation_blocked_reason:
            self.message = self.mutation_blocked_reason
            return
        if self.block_unresolved_action(row):
            return
        if self.installing:
            self.mark_wait(row, context)
            return
        if row.target_version == row.installed_version:
            self.message = f"{row.display_name} is already at {row.installed_version}"
            row.status = "current"
            return
        if refresh_context_uv_provider(context) is None:
            self.ensure_uv_provider(context, lambda: self.request_install())
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
            old_context_id, old_name, old_target = self.wait_request
            if old_context_id == context.id and old_name == row.name and old_target == row.target_version:
                row.status = "wait"
                self.message = f"Queued after current install: {row.display_name}"
                return
            old_row = self.find_row(old_name)
            if old_row and old_row.status == "wait":
                if old_row.target_version == old_row.installed_version:
                    old_row.status = "current"
                elif old_row.versions_resolved and old_row.target_version in old_row.candidate_versions:
                    old_row.status = "ready"
                else:
                    old_row.status = "loading"
        row.status = "wait"
        self.wait_request = (context.id, row.name, row.target_version)
        self.message = f"Queued after current install: {row.display_name}"

    def begin_install(self, row: PackageRow) -> None:
        context = self.context
        if context is None:
            return
        if not self.row_target_installable(row):
            self.message = f"Version data unavailable for {row.display_name}"
            return
        self.installing = True
        self.active_install_context_id = context.id
        row.status = "installing"
        row.last_error = None
        row.last_error_detail = None
        row.last_install_result = None
        before_versions = {item.name: item.installed_version for item in self.rows}
        package_name = row.name
        package_display_name = row.display_name
        target_version = row.target_version
        installed_version_at_attempt = row.installed_version
        candidate_versions_at_attempt = list(row.candidate_versions)
        package_spec = f"{package_display_name}=={target_version}"
        try:
            command = uv_command(context, ["pip", "install", "--python", context.uv_target])
        except Exception as exc:
            self.installing = False
            self.active_install_context_id = None
            row.status = "failed"
            row.last_error = "uv provider unavailable"
            row.last_error_detail = str(exc)
            self.message = "uv provider unavailable"
            return
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
                    installed_version_at_attempt=installed_version_at_attempt,
                    exit_code=proc.returncode,
                    stdout_tail=tail_lines(proc.stdout),
                    stderr_tail=tail_lines(proc.stderr),
                    candidate_versions_at_attempt=candidate_versions_at_attempt,
                )
            except Exception as exc:
                elapsed = time.time() - start
                stderr = str(exc)
                result = InstallResult(
                    context_id=context.id,
                    package_name=package_name,
                    target_version=target_version,
                    command=command,
                    returncode=1,
                    stdout="",
                    stderr=stderr,
                    elapsed=elapsed,
                    before_versions=before_versions,
                    installed_version_at_attempt=installed_version_at_attempt,
                    exit_code=None,
                    stdout_tail=[],
                    stderr_tail=tail_lines(stderr),
                    candidate_versions_at_attempt=candidate_versions_at_attempt,
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
        name = ("* " if row.metadata_trusted and row.uninstall_safe else "  ") + row.display_name
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
        items_visible = overlay_h - 2
        start = max(0, min(self.context_overlay_index, max(0, len(self.contexts) - items_visible)))
        box = [self.box_border(overlay_w, "top", "Context selector")]
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
        row = self.find_row(self.version_overlay_row or "")
        title = f"Versions: {row.display_name if row else ''}".strip()
        overlay_w = min(width - 4, max(42, width // 2))
        overlay_h = min(height - 4, max(8, min(len(self.version_options) + 5, height - 4)))
        top = max(1, (height - overlay_h) // 2)
        left = max(1, (width - overlay_w) // 2)
        items_visible = overlay_h - 3
        self.ensure_version_overlay_visible(items_visible)
        start = self.version_overlay_scroll
        box = [self.box_border(overlay_w, "top", title)]
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

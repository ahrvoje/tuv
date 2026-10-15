"""Shared fixtures: deterministic threads, fake uv backends, and a headless TuvApp."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tuv  # noqa: E402

PROVIDER = tuv.UvProvider("tuv", None, Path(sys.executable), 4, "uv 0.0")


class ThreadRunner:
    """Replacement for threading.Thread that defers workers until the test runs them."""

    def __init__(self) -> None:
        self.pending: list[tuple] = []

    def Thread(self, target=None, args=(), kwargs=None, daemon=None, name=None):  # noqa: N802
        runner = self

        class Deferred:
            def start(self_inner) -> None:
                runner.pending.append((target, tuple(args), dict(kwargs or {})))

            def join(self_inner, timeout=None) -> None:
                pass

            def is_alive(self_inner) -> bool:
                return False

        return Deferred()

    def run_all(self) -> None:
        while self.pending:
            target, args, kwargs = self.pending.pop(0)
            target(*args, **kwargs)


@pytest.fixture
def threads(monkeypatch):
    runner = ThreadRunner()
    fake = SimpleNamespace(
        Thread=runner.Thread,
        Lock=threading.Lock,
        RLock=threading.RLock,
        Event=threading.Event,
        get_ident=threading.get_ident,
        current_thread=threading.current_thread,
        main_thread=threading.main_thread,
    )
    monkeypatch.setattr(tuv, "threading", fake)
    return runner


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    return tmp_path


def make_context(kind="venv", name="proj", source="scanned", version="3.12.1", provider=PROVIDER, confirmed=False):
    if kind == "interpreter":
        python = Path(sys.executable) if name == "sys" else Path(f"/pythons/{name}/python.exe")
        prefix = "cwd interpreter" if source == "cwd" else "interpreter"
        return tuv.PythonContext(
            id=tuv.stable_context_id("interpreter", python),
            type="interpreter",
            source=source,
            label=f"{prefix} - Python {version} - {python}",
            python_path=python,
            reference_python_path=python,
            root_path=None,
            version=version,
            resolved_uv_provider=provider,
            confirmed_for_mutation=confirmed,
        )
    root = Path(f"/envs/{name}")
    return tuv.PythonContext(
        id=tuv.stable_context_id(kind, root),
        type=kind,
        source=source,
        label=f"{'tuv venv' if kind == 'tuv' else name} - Python {version} - {root}",
        python_path=tuv.venv_python(root),
        reference_python_path=None,
        root_path=root.resolve(),
        version=version,
        resolved_uv_provider=provider,
        confirmed_for_mutation=confirmed,
    )


class FakeBackend:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.outdated: dict[str, str] = {}
        self.outdated_warning: str | None = None
        self.deps: object = None  # tuple payload, Exception, or None for "no deps"
        self.versions: dict[str, object] = {}
        self.contexts: list[tuv.PythonContext] = []
        self.quick_contexts: list[tuv.PythonContext] = []
        self.calls: list[tuple] = []
        self.popen_results: list[tuple[int, str, str]] = []
        self.popen_commands: list[list[str]] = []
        self.health: tuple[int, str, str] = (0, "Checked 3 packages\nAll installed packages are compatible", "")
        self.list_error: Exception | None = None
        self.provider: tuv.UvProvider | None = PROVIDER
        self.terminated: list[object] = []
        self.commands: list[list[str]] = []
        self.command_results: dict[str, tuple[int, str, str]] = {}

    def apply_install(self, command: list[str]) -> None:
        spec = command[-1]
        if "uninstall" in command:
            self.rows.pop(spec, None)
            self.rows = {k: v for k, v in self.rows.items() if tuv.canonicalize_name(k) != tuv.canonicalize_name(spec)}
            return
        name, _, version = spec.partition("==")
        key = next((k for k in self.rows if tuv.canonicalize_name(k) == tuv.canonicalize_name(name)), name)
        self.rows[key] = version


@pytest.fixture
def backend(monkeypatch, tmp_path):
    b = FakeBackend()

    def build_package_rows(context, updated_names):
        b.calls.append(("list", context.id))
        if b.list_error is not None:
            raise b.list_error
        rows = []
        for display, version in b.rows.items():
            normalized = tuv.canonicalize_name(display)
            rows.append(
                tuv.PackageRow(
                    name=normalized,
                    display_name=display,
                    uninstall_safe=False,
                    installed_version=version,
                    target_version=version,
                    candidate_versions=[version],
                    status="loading",
                    updated_in_session=normalized in updated_names,
                )
            )
        rows.sort(key=lambda row: row.name)
        return rows

    def load_outdated_targets(context):
        b.calls.append(("outdated", context.id))
        return dict(b.outdated), b.outdated_warning

    def load_dependency_info(context, display_by_name):
        b.calls.append(("deps", context.id))
        if isinstance(b.deps, Exception):
            raise b.deps
        if b.deps is None:
            names = set(display_by_name)
            return tuv.DependencyInfo(names, {n: [] for n in names}, {n: [] for n in names}, {n: None for n in names})
        return b.deps

    def fetch_available_versions(package_name, context, timeout=12.0):
        b.calls.append(("versions", package_name))
        result = b.versions.get(tuv.canonicalize_name(package_name))
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise RuntimeError(f"Package {package_name} was not found on the configured uv indexes")
        return list(result[0]), set(result[1])

    def run_command(args, timeout=None):
        b.commands.append(list(args))
        text = " ".join(str(a) for a in args)
        for marker, result in b.command_results.items():
            if marker in text:
                if isinstance(result, list):
                    result = result.pop(0) if len(result) > 1 else result[0]
                rc, out, err = result
                return subprocess.CompletedProcess(args, rc, out, err)
        if "check" in args:
            rc, out, err = b.health
            return subprocess.CompletedProcess(args, rc, out, err)
        if "venv" in args and "-m" in args:
            root = Path(args[-1])
            root.mkdir(parents=True)
            (root / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
            exe = tuv.venv_python(root)
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    class FakePopen:
        def __init__(self, args, **kwargs):
            b.popen_commands.append(list(args))
            self.args = list(args)
            self.pid = 4242
            self.returncode = None
            self._result = b.popen_results.pop(0) if b.popen_results else (0, "ok", "")

        def communicate(self, timeout=None):
            rc, out, err = self._result
            self.returncode = rc
            if rc == 0 and "--dry-run" not in self.args:
                b.apply_install(self.args)
            return out, err

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(tuv, "build_package_rows", build_package_rows)
    monkeypatch.setattr(tuv, "load_outdated_targets", load_outdated_targets)
    monkeypatch.setattr(tuv, "load_dependency_info", load_dependency_info)
    monkeypatch.setattr(tuv, "fetch_available_versions", fetch_available_versions)
    monkeypatch.setattr(tuv, "run_command", run_command)
    monkeypatch.setattr(tuv.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(tuv, "resolve_uv_provider", lambda context: b.provider)
    monkeypatch.setattr(tuv, "discover_contexts", lambda: list(b.contexts))
    monkeypatch.setattr(tuv, "discover_priority_contexts", lambda: list(b.quick_contexts))
    monkeypatch.setattr(tuv, "terminate_process_tree", lambda proc, grace=0.5: b.terminated.append(proc))
    monkeypatch.setattr(tuv, "runner_python_path", lambda: Path(sys.executable))
    return b


class FakeTerminal:
    def __init__(self) -> None:
        self.width = 120
        self.height = 30
        self.writes: list[str] = []
        self.keys: list[str] = []

    def size(self):
        return self.width, self.height

    def write(self, text: str) -> None:
        self.writes.append(text)

    def wake(self) -> None:
        pass

    def read_key(self, timeout: float):
        return self.keys.pop(0) if self.keys else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class RecordingApp(tuv.TuvApp):
    """TuvApp that records every status message it ever sets."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        super().__init__()

    def set_message(self, value: str, kind: str = "info") -> None:
        super().set_message(value, kind)
        self.messages.append(value)

    @property
    def message(self) -> str:
        return tuv.TuvApp.message.fget(self)

    @message.setter
    def message(self, value: str) -> None:
        tuv.TuvApp.message.fset(self, value)
        self.messages.append(value)


class Driver:
    """Drives a TuvApp the way its main loop would, without a terminal."""

    def __init__(self, app: RecordingApp, runner: ThreadRunner, backend: FakeBackend) -> None:
        self.app = app
        self.runner = runner
        self.backend = backend

    @property
    def messages(self) -> list[str]:
        return self.app.messages

    def _tick(self) -> None:
        self.app.process_events()
        self.app.update_view()
        self.app.ensure_scroll_visible()

    def pump(self) -> None:
        for _ in range(500):
            self.runner.run_all()
            self._tick()
            if not self.runner.pending and self.app.event_queue.empty():
                break
        else:
            raise AssertionError("event pump did not settle")

    def step(self) -> None:
        """Run exactly one pending worker, then process its events."""
        if self.runner.pending:
            target, args, kwargs = self.runner.pending.pop(0)
            target(*args, **kwargs)
        self._tick()

    def press(self, *keys: str, settle: bool = True) -> None:
        for key in keys:
            self.app.handle_key(key)
            self._tick()
            if settle:
                self.pump()

    def type_text(self, text: str) -> None:
        for ch in text:
            self.press(ch, settle=False)

    def start(self, contexts, quick=None) -> None:
        self.backend.contexts = list(contexts)
        self.backend.quick_contexts = list(quick or [])
        self.app.start_context_discovery()
        self.pump()

    def row(self, name: str) -> tuv.PackageRow:
        row = self.app.find_row(name)
        assert row is not None, f"row {name} missing"
        return row

    def focus(self, name: str) -> None:
        self.app.update_view()
        self.app.focus_on_package(tuv.canonicalize_name(name))
        assert self.app.focused_row() is not None and self.app.focused_row().name == tuv.canonicalize_name(name)

    def screen(self) -> list[str]:
        return [tuv.strip_ansi(line) for line in self.app.render().split("\n")]

    def installs(self) -> list[list[str]]:
        return [cmd for cmd in self.backend.popen_commands if "--dry-run" not in cmd]


@pytest.fixture
def driver(monkeypatch, threads, backend, home):
    monkeypatch.setattr(tuv.signal, "signal", lambda *args, **kwargs: None)
    tuv._VERSION_LOOKUP_CACHE.clear()
    tuv._INDEX_AUTH_CACHE.clear()
    app = RecordingApp()
    app.terminal = FakeTerminal()
    return Driver(app, threads, backend)

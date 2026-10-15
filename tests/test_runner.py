"""Interpreter probing, uv provider resolution, runner venv management, context discovery."""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

import pytest

import tuv
from conftest import make_context

HERE = Path(__file__).resolve().parent
READY_STATUS = {"base_ok": True, "pip": True, "ensurepip": True, "uv": True, "requirements": True}


def test_probe_python_reports_real_interpreter_and_caches():
    info = tuv.probe_python(sys.executable, source="cwd")
    assert info is not None
    assert info.version == tuple(sys.version_info[:3])
    assert info.source == "cwd"
    assert info.implementation == sys.implementation.name
    assert info.has_venv
    again = tuv.probe_python(sys.executable)
    assert again is not None and again.source == "installed"
    assert again.executable == info.executable
    assert tuv.probe_python(str(HERE / "no-such-python")) is None
    assert tuv.probe_python("") is None
    assert info.is_venv == (sys.prefix != sys.base_prefix)


def test_runner_status_reports_modules():
    status = tuv.runner_status(Path(sys.executable))
    assert status is not None
    assert status["base_ok"] and status["requirements"] and status["uv"] and status["ensurepip"]
    assert tuv.runner_status(HERE / "missing.exe") is None


def test_validate_uv_command_and_providers(monkeypatch):
    tuv._UV_VERSION_CACHE.clear()
    version = tuv.validate_uv_command([sys.executable, "-m", "uv"])
    assert version and version.startswith("uv ")
    assert tuv.validate_uv_command([str(HERE / "missing-uv")]) is None
    provider = tuv.python_uv_provider("tuv", Path(sys.executable), 4)
    assert provider is not None and provider.command_prefix == [str(Path(sys.executable).resolve()), "-m", "uv"]
    assert tuv.python_uv_provider("x", None, 1) is None
    assert tuv.provider_label(provider) == "tuv uv"
    assert tuv.provider_label(None) == "unavailable"
    standalone = tuv.UvProvider("standalone", Path("/x/uv"), None, 3, "uv 1")
    assert standalone.command_prefix == [str(Path("/x/uv"))]
    with pytest.raises(RuntimeError):
        tuv.UvProvider("standalone", None, None, 3, "uv 1").command_prefix


def test_resolve_uv_provider_hierarchy(monkeypatch, tmp_path):
    def validate(command, timeout=5.0):
        return "uv 9" if command[0] in ok else None

    monkeypatch.setattr(tuv, "validate_uv_command", validate)
    monkeypatch.setattr(tuv, "runner_python_path", lambda: tmp_path / "runner.exe")
    monkeypatch.setenv("TUV_SYSTEM_UV_EXE", str(tmp_path / "uv.exe"))
    for name in ["venv.exe", "ref.exe", "runner.exe", "uv.exe"]:
        (tmp_path / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(tuv.shutil, "which", lambda name: None)

    context = tuv.PythonContext("venv:x", "venv", "scanned", "x", tmp_path / "venv.exe", tmp_path / "ref.exe", tmp_path, "3.12")
    ok = {str((tmp_path / "venv.exe").resolve())}
    assert tuv.resolve_uv_provider(context).type == "context_venv"
    ok = {str((tmp_path / "ref.exe").resolve())}
    assert tuv.resolve_uv_provider(context).type == "reference_python"
    ok = {str((tmp_path / "uv.exe").resolve())}
    assert tuv.resolve_uv_provider(context).type == "standalone"
    ok = {str((tmp_path / "runner.exe").resolve())}
    provider = tuv.resolve_uv_provider(context)
    assert provider.type == "tuv" and provider.priority == 4
    ok = set()
    assert tuv.resolve_uv_provider(context) is None
    interpreter = tuv.PythonContext("i:x", "interpreter", "installed", "x", tmp_path / "ref.exe", tmp_path / "ref.exe", None, "3.12")
    ok = {str((tmp_path / "ref.exe").resolve())}
    assert tuv.resolve_uv_provider(interpreter).type == "reference_python"
    tuv_context = tuv.PythonContext("tuv:x", "tuv", "tuv", "x", tmp_path / "runner.exe", None, tmp_path, "3.12")
    ok = {str((tmp_path / "runner.exe").resolve())}
    assert tuv.resolve_uv_provider(tuv_context).priority == 1
    ok = set()
    with pytest.raises(RuntimeError):
        tuv.uv_command(tuv.PythonContext("v:y", "venv", "scanned", "y", tmp_path / "none.exe", None, tmp_path, "3.12"), ["pip"])


def test_uv_target_uses_root_for_venvs():
    venv_context = make_context("venv", "p")
    assert venv_context.uv_target == str(venv_context.root_path)
    interpreter = make_context("interpreter", "sys")
    assert interpreter.uv_target == str(interpreter.python_path)


def test_installed_package_metadata_and_dependency_info():
    context = tuv.PythonContext("venv:test", "venv", "scanned", "t", Path(sys.executable), None, Path(sys.prefix), "3.12")
    metadata = tuv.installed_package_metadata(context)
    assert "pytest" in metadata
    assert "packaging" in metadata["pytest"].dependencies
    assert metadata["pytest"].description
    display = {name: name for name in metadata}
    info = tuv.load_dependency_info(context, display)
    assert "packaging" not in info.safe_names and "pluggy" not in info.safe_names
    assert "packaging" in info.dependencies["pytest"]
    assert "pytest" in info.usage["packaging"]
    # pluggy declares `pytest; extra == "testing"`: extra-gated requirements count conservatively.
    assert "pluggy" in info.usage["pytest"] and "pytest" not in info.safe_names
    assert info.descriptions["pytest"]
    with pytest.raises(RuntimeError):
        tuv.load_dependency_info(context, {"definitely-not-installed-pkg": "x"})


def test_cwd_python_candidates(tmp_path):
    assert tuv.cwd_python_candidates(tmp_path) == []
    exe = tuv.venv_python(tmp_path)
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    expected = [tmp_path / ("Scripts/python.exe" if tuv.IS_WINDOWS else "bin/python")]
    assert tuv.cwd_python_candidates(tmp_path) == expected
    (tmp_path / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    assert tuv.cwd_python_candidates(tmp_path) == []
    assert tuv.cwd_python_candidates(tmp_path, allow_venv=True) == [exe]
    assert tuv.is_venv(tmp_path)
    assert tuv.venv_root_of(exe) == tmp_path.resolve()


def test_runner_compatibility_and_state(tmp_path, monkeypatch):
    monkeypatch.setattr(tuv, "TUV_HOME", tmp_path)
    info = tuv.probe_python(sys.executable)
    assert info is not None
    key = tuv.runner_compatibility_key(info, "default")
    assert key.endswith("|default") and key.split("|")[1] == info.version_text
    hash_value = tuv.runner_hash(key)
    assert len(hash_value) == 8
    root, hashed = tuv.new_runner_venv_path(key)
    assert root == tmp_path / f"tuv-venv-{hash_value}" and hashed == hash_value
    root.mkdir()
    other, other_hash = tuv.new_runner_venv_path(key)
    assert other != root and other.name.startswith("tuv-venv-") and len(other_hash) == 8
    tuv.write_runner_state(root, info, "default", key, hash_value)
    state = tuv.read_runner_state(root)
    assert state["compat_key"] == key and state["compat_hash"] == hash_value and state["launcher_mode"] == "default"
    assert state["base_python"] == str(info.executable)
    assert tuv.read_runner_state(tmp_path / "missing") == {}


def test_find_compatible_runner_prefers_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(tuv, "TUV_HOME", tmp_path)
    monkeypatch.setattr(tuv, "runner_status", lambda path: dict(READY_STATUS))
    info = tuv.probe_python(sys.executable)
    key = tuv.runner_compatibility_key(info, "default")
    old = tmp_path / "tuv-venv-aaaaaaaa"
    new = tmp_path / "tuv-venv-bbbbbbbb"
    legacy = tmp_path / ".tuv-venv"
    wrong = tmp_path / "tuv-venv-cccccccc"
    for root in (old, new, legacy, wrong):
        root.mkdir()
    tuv.write_runner_state(old, info, "default", key, "aaaaaaaa")
    (old / ".tuv-runner-state").write_text((old / ".tuv-runner-state").read_text().replace("timestamp=", "timestamp=1"), encoding="utf-8")
    tuv.write_runner_state(new, info, "default", key, "bbbbbbbb")
    tuv.write_runner_state(legacy, info, "default", key, "legacy")
    tuv.write_runner_state(wrong, info, "cwd", key + "|x", "cccccccc")
    found = tuv.find_compatible_runner_venv(key)
    assert found is not None and found[0] == new.resolve() and found[1] == READY_STATUS
    assert tuv.compatible_runner_status(wrong, key) is None
    assert tuv.compatible_runner_status(tmp_path / "elsewhere", key) is None
    assert sorted(p.name for p in tuv.runner_venv_candidates()) == [".tuv-venv", "tuv-venv-aaaaaaaa", "tuv-venv-bbbbbbbb", "tuv-venv-cccccccc"]
    monkeypatch.setattr(tuv, "runner_status", lambda path: {**READY_STATUS, "pip": False, "ensurepip": False})
    assert tuv.find_compatible_runner_venv(key) is None
    monkeypatch.setattr(tuv, "runner_status", lambda path: {**READY_STATUS, "pip": False})
    assert tuv.find_compatible_runner_venv(key) is not None


def test_runner_is_ready_requires_marker_and_modules(tmp_path, monkeypatch):
    monkeypatch.setattr(tuv, "TUV_HOME", tmp_path)
    (tmp_path / "requirements.txt").write_text("packaging\n", encoding="utf-8")
    root = tmp_path / "tuv-venv-x"
    root.mkdir()
    assert not tuv.runner_is_ready(root, READY_STATUS)
    (root / ".tuv-requirements-state").write_text(tuv.requirements_hash().upper() + "\n", encoding="utf-8")
    assert tuv.runner_is_ready(root, READY_STATUS)
    assert not tuv.runner_is_ready(root, {**READY_STATUS, "uv": False})
    assert not tuv.runner_is_ready(root, {**READY_STATUS, "pip": False})
    (tmp_path / "requirements.txt").write_text("packaging>=24\n", encoding="utf-8")
    assert not tuv.runner_is_ready(root, READY_STATUS)


def test_gc_runner_venvs_keeps_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(tuv, "TUV_HOME", tmp_path)
    info = tuv.probe_python(sys.executable)
    roots = []
    for index in range(6):
        root = tmp_path / f"tuv-venv-{index:08d}"
        root.mkdir()
        (root / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
        tuv.write_runner_state(root, info, "default", f"key{index}", f"{index:08d}")
        (root / ".tuv-runner-state").write_text(
            (root / ".tuv-runner-state").read_text().replace("timestamp=", f"timestamp={index}"), encoding="utf-8"
        )
        roots.append(root)
    unmanaged = tmp_path / "tuv-venv-unmanaged"
    unmanaged.mkdir()
    tuv.gc_runner_venvs(roots[0].resolve(), keep_recent=3)
    remaining = sorted(p.name for p in tmp_path.glob("tuv-venv-*"))
    assert remaining == ["tuv-venv-00000000", "tuv-venv-00000004", "tuv-venv-00000005", "tuv-venv-unmanaged"]


def test_file_lock(tmp_path, monkeypatch):
    lock = tmp_path / "sub" / "x.lock"
    monkeypatch.setattr(tuv.time, "sleep", lambda s: None)
    with tuv.file_lock(lock, timeout=1.0, stale_after=600.0) as locked:
        assert locked and lock.exists()
        with tuv.file_lock(lock, timeout=0.01, stale_after=600.0) as nested:
            assert not nested
        assert lock.exists()
    assert not lock.exists()
    lock.write_text("stale", encoding="utf-8")
    with tuv.file_lock(lock, timeout=0.01, stale_after=-1.0) as locked:
        assert locked
    assert not lock.exists()


def test_prepare_runner_environment_creates_and_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(tuv, "TUV_HOME", tmp_path)
    (tmp_path / "requirements.txt").write_text("packaging\nwcwidth\n", encoding="utf-8")
    info = tuv.probe_python(sys.executable)
    monkeypatch.setattr(tuv, "select_runner_python", lambda mode: info)
    created: list[list[str]] = []

    def run_command(args, timeout=None):
        created.append(list(args))
        venv.EnvBuilder(with_pip=False).create(Path(args[-1]))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tuv, "run_command", run_command)
    monkeypatch.setattr(tuv, "runner_status", lambda path: dict(READY_STATUS) if path.is_file() else None)
    selected, root, ready = tuv.prepare_runner_environment("default")
    assert selected == info and root.parent == tmp_path and root.name.startswith("tuv-venv-")
    assert len(created) == 1 and created[0][1:3] == ["-m", "venv"] and not ready
    again_info, again_root, ready = tuv.prepare_runner_environment("default")
    assert again_root == root.resolve() and len(created) == 1 and not ready
    (root / ".tuv-requirements-state").write_text(tuv.requirements_hash() + "\n", encoding="utf-8")
    assert tuv.prepare_runner_environment("default")[2]
    with pytest.raises(RuntimeError):
        tuv.prepare_runner_environment("bogus")


def test_print_prepare_runner_outputs_anchors(tmp_path, monkeypatch, capsys):
    info = tuv.probe_python(sys.executable)
    monkeypatch.setattr(tuv, "prepare_runner_environment", lambda mode: (info, tmp_path / "tuv-venv-x", True))
    assert tuv.print_prepare_runner("default") == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"TUV_NEWEST_PYTHON={info.executable}",
        f"TUV_RUNNER_VENV={tmp_path / 'tuv-venv-x'}",
        f"TUV_RUNNER_PYTHON={tuv.venv_python(tmp_path / 'tuv-venv-x')}",
        "TUV_RUNNER_READY=1",
    ]
    monkeypatch.setattr(tuv, "prepare_runner_environment", lambda mode: (_ for _ in ()).throw(RuntimeError("nope")))
    assert tuv.print_prepare_runner("default") == 1
    assert "nope" in capsys.readouterr().err


def test_select_runner_python_cwd_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError):
        tuv.select_runner_python("cwd")
    venv.EnvBuilder(with_pip=False).create(tmp_path)
    info = tuv.select_runner_python("cwd")
    assert info.source == "cwd" and info.is_venv


def test_select_runner_python_default_skips_venv_interpreters(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    monkeypatch.setattr(tuv, "platform_python_candidates", lambda: [base])
    info = tuv.select_runner_python("default")
    assert info.executable == base.resolve() and info.has_venv and not info.is_venv
    monkeypatch.setattr(tuv, "platform_python_candidates", lambda: [])
    if sys.prefix != sys.base_prefix:
        # sys.executable is a venv interpreter here, which default mode must not pick.
        with pytest.raises(RuntimeError):
            tuv.select_runner_python("default")


def test_discover_contexts_real_layout(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    venv.EnvBuilder(with_pip=False).create(project / ".venv")
    venv.EnvBuilder(with_pip=False).create(project / "other-env")
    (project / "plain").mkdir()
    runner = tmp_path / "home" / "tuv-venv-deadbeef"
    venv.EnvBuilder(with_pip=False).create(runner)
    monkeypatch.setattr(tuv, "TUV_HOME", runner.parent)
    monkeypatch.setattr(tuv, "RUNNER_VENV", runner.resolve())
    monkeypatch.setenv("VIRTUAL_ENV", str(project / "other-env"))
    monkeypatch.setenv("TUV_NEWEST_PYTHON", sys.executable)
    monkeypatch.chdir(project)
    monkeypatch.setattr(tuv, "refresh_context_uv_provider", lambda context: None)

    contexts = tuv.discover_contexts()
    types = [context.type for context in contexts]
    assert types == sorted(types, key=["interpreter", "venv", "tuv"].index)
    assert types[-1] == "tuv" and contexts[-1].label.startswith("tuv venv")
    venvs = [context for context in contexts if context.type == "venv"]
    assert [context.source for context in venvs] == ["active", "scanned"]
    assert venvs[0].root_path == (project / "other-env").resolve()
    assert venvs[1].root_path == (project / ".venv").resolve()
    assert len({context.id for context in contexts}) == len(contexts)
    assert all(context.reference_python_path is not None for context in venvs)
    for context in contexts:
        if context.type == "interpreter":
            assert tuv.venv_root_of(context.python_path) is None

    quick = tuv.discover_priority_contexts()
    quick_types = [(context.type, context.source) for context in quick]
    assert quick_types[0] == ("venv", "active")
    assert ("venv", "scanned") in quick_types
    assert quick_types[-1] == ("tuv", "tuv")


def test_discover_python_infos_marks_cwd_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tuv, "windows_python_candidates", lambda: [Path(sys.executable)])
    monkeypatch.setattr(tuv, "posix_python_candidates", lambda: [Path(sys.executable)])
    monkeypatch.setattr(tuv, "launcher_python_candidates", lambda: [])
    infos = tuv.discover_python_infos()
    if sys.prefix == sys.base_prefix:
        assert [info.source for info in infos] == ["installed"]
    else:
        assert infos == []
    monkeypatch.setattr(tuv, "cwd_python_candidates", lambda cwd=None, allow_venv=False: [Path(sys.executable)])
    infos = tuv.discover_python_infos()
    if sys.prefix == sys.base_prefix:
        assert [info.source for info in infos] == ["cwd"]

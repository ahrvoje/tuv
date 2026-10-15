"""TuvApp state machine driven through keys and worker events."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import tuv
from conftest import PROVIDER, make_context


def default_rows(backend):
    backend.rows = {"Requests": "2.31.0", "rich": "13.0.0", "urllib3": "2.2.0", "pytest": "8.0.0"}
    backend.outdated = {"requests": "2.32.0", "rich": "14.0.0"}
    backend.deps = tuv.DependencyInfo(
        safe_names={"requests", "rich", "pytest"},
        dependencies={"requests": ["urllib3"], "rich": [], "urllib3": [], "pytest": []},
        usage={"requests": [], "rich": [], "urllib3": ["Requests"], "pytest": []},
        descriptions={"requests": "HTTP for humans", "rich": None, "urllib3": "", "pytest": "testing"},
    )
    backend.versions = {
        "requests": (["2.30.0", "2.31.0", "2.32.0", "2.32.1"], {"2.32.1"}),
        "rich": (["13.0.0", "14.0.0"], set()),
    }


@pytest.fixture
def ready(driver):
    default_rows(driver.backend)
    venv_context = make_context("venv", ".venv", source="scanned")
    driver.start([venv_context, make_context("tuv", "runner", source="tuv")])
    assert driver.app.context.id == venv_context.id
    return driver


def statuses(app):
    return {row.name: row.status for row in app.rows}


def test_startup_quick_context_then_full_list_without_reload(driver):
    default_rows(driver.backend)
    active = make_context("venv", "active", source="active")
    interpreter = make_context("interpreter", "sys", version="3.14.0")
    tuv_context = make_context("tuv", "runner", source="tuv")
    driver.backend.contexts = [interpreter, active, tuv_context]
    driver.backend.quick_contexts = [active, tuv_context]
    driver.app.start_context_discovery()
    assert driver.app.discovering_contexts
    driver.press("f9", settle=False)
    assert driver.app.context_overlay_pending and not driver.app.context_overlay
    driver.pump()
    assert driver.app.context.id == active.id
    assert [c.id for c in driver.app.contexts] == [interpreter.id, active.id, tuv_context.id]
    assert driver.app.context_index == 1
    assert driver.app.refresh_generation == 1
    assert driver.backend.calls.count(("list", active.id)) == 1
    assert driver.app.context_overlay and not driver.app.context_overlay_pending
    assert statuses(driver.app) == {"requests": "ready", "rich": "ready", "urllib3": "current", "pytest": "current"}
    assert driver.app.message == "2 package(s) have updates available"
    assert driver.app.health_status == "healthy"


def test_default_context_priority(driver):
    interpreter = make_context("interpreter", "sys")
    cwd_interpreter = make_context("interpreter", "local", source="cwd")
    dotvenv = make_context("venv", ".venv", source="scanned")
    tuv_context = make_context("tuv", "runner", source="tuv")
    driver.start([interpreter, cwd_interpreter, dotvenv, tuv_context])
    assert driver.app.context.id == dotvenv.id
    driver.app.contexts = []
    driver.start([interpreter, cwd_interpreter, tuv_context])
    assert driver.app.context.id == cwd_interpreter.id
    driver.start([interpreter, tuv_context])
    assert driver.app.context.id == interpreter.id
    driver.start([tuv_context])
    assert driver.app.context.id == tuv_context.id


def test_discovery_failure_keeps_quick_contexts(driver, monkeypatch):
    default_rows(driver.backend)
    active = make_context("venv", "active", source="active")
    driver.backend.quick_contexts = [active]
    monkeypatch.setattr(tuv, "discover_contexts", lambda: (_ for _ in ()).throw(RuntimeError("scan broke")))
    driver.app.start_context_discovery()
    driver.press("f9", settle=False)
    driver.pump()
    assert driver.app.context.id == active.id
    assert "scan broke" in driver.app.discovery_error
    assert driver.app.context_overlay
    driver.app.contexts = []
    monkeypatch.setattr(tuv, "discover_priority_contexts", lambda: [])
    driver.app.start_context_discovery()
    driver.pump()
    assert driver.app.contexts == [] and driver.app.context is None
    assert "Context discovery failed" in driver.app.message
    assert "Context discovery failed" in driver.screen()[0]


def test_no_contexts_found(driver):
    driver.start([])
    assert driver.app.context is None
    assert "could not find" in driver.app.message


def test_refresh_sets_rows_colors_and_dependency_markers(ready):
    app = ready.app
    assert [row.display_name for row in app.rows] == ["pytest", "Requests", "rich", "urllib3"]
    requests = ready.row("requests")
    assert requests.target_version == "2.32.0" and requests.candidate_versions == ["2.31.0", "2.32.0"]
    assert requests.versions_resolved and not requests.full_versions_loaded
    assert requests.metadata_trusted and requests.uninstall_safe
    assert not ready.row("urllib3").uninstall_safe
    assert requests.dependency_packages == ["urllib3"] and ready.row("urllib3").usage_packages == ["Requests"]
    assert requests.description == "HTTP for humans"
    screen = ready.screen()
    assert screen[0].startswith("[ .venv - Python 3.12.1")
    assert screen[0].rstrip().endswith("[health: ok]")
    assert screen[2].startswith("Package")
    rows = [line for line in screen[3:7]]
    assert rows[0].startswith(" * pytest") and "current" in rows[0]
    assert rows[1].startswith(" * Requests") and "2.31.0" in rows[1] and "2.32.0" in rows[1] and "ready" in rows[1]
    assert rows[3].startswith("   urllib3")
    assert "F2" in screen[-1] and "F10" in screen[-1] and "↑/↓" in screen[-1] and "↵" in screen[-1]
    rendered = app.render().split("\n")
    assert tuv.LIGHT_GREEN in rendered[3] and tuv.YELLOW in rendered[4]
    assert tuv.REVERSE in rendered[3]


def test_dependency_failure_disables_markers_and_info_shows_unavailable(driver):
    default_rows(driver.backend)
    driver.backend.deps = RuntimeError("metadata broke")
    driver.start([make_context("venv", "proj")])
    assert not any(row.metadata_trusted for row in driver.app.rows)
    assert any(message.startswith("Dependency data unavailable") and "metadata broke" in message for message in driver.messages)
    assert not any("*" in line[:3] for line in driver.screen()[3:7])
    driver.press("f3")
    assert driver.app.info_open
    assert any("(metadata unavailable)" in line for line in driver.screen())
    driver.press("2")
    assert any("Dependency metadata is unavailable" in line for line in driver.screen())


def test_outdated_warning_keeps_rows_loading(driver):
    default_rows(driver.backend)
    driver.backend.outdated = {}
    driver.backend.outdated_warning = "Outdated data unavailable: offline"
    driver.start([make_context("venv", "proj")])
    assert statuses(driver.app) == {name: "current" for name in statuses(driver.app)}
    assert driver.app.message.startswith("Outdated data unavailable")
    assert driver.app._message_kind == "warn"


def test_refresh_failure_shows_message(driver):
    default_rows(driver.backend)
    driver.backend.list_error = RuntimeError("uv exploded\nbadly")
    driver.start([make_context("venv", "proj")])
    assert driver.app.rows == []
    assert driver.app.message.startswith("Refresh failed")
    assert "badly" in driver.app.message


def test_navigation_keys_and_bounds(ready):
    app = ready.app
    assert app.focus_index == 0
    ready.press("up")
    assert app.focus_index == 0
    ready.press("down", "down")
    assert app.focus_index == 2
    ready.press("end")
    assert app.focus_index == 3
    ready.press("down")
    assert app.focus_index == 3
    ready.press("home")
    assert app.focus_index == 0
    ready.press("pagedown")
    assert app.focus_index == 3
    ready.press("pageup")
    assert app.focus_index == 0


def test_scrolling_follows_focus(ready):
    ready.backend.rows = {f"pkg{index:03d}": "1.0" for index in range(60)}
    ready.backend.outdated = {}
    ready.backend.deps = None
    ready.press("r")
    app = ready.app
    assert len(app.rows) == 60
    ready.app.terminal.height = 16
    ready.press("end")
    assert app.focus_index == 59
    assert app.scroll == 60 - app.page_size()
    ready.press("home")
    assert app.scroll == 0
    ready.press("pagedown")
    assert app.focus_index == app.page_size() and app.scroll == 1
    screen = ready.screen()
    assert len(screen) == 16
    assert "pkg001" in screen[3] and "pkg000" not in "\n".join(screen)


def test_filter_and_selection(ready):
    app = ready.app
    ready.press("/")
    assert app.input_mode == "filter"
    ready.type_text("re")
    assert app.filter_text == "re" and [row.name for row in app.view] == ["requests"]
    ready.press("down")
    assert app.focus_index == 0
    ready.press("backspace")
    assert app.filter_text == "r" and len(app.view) == 3
    ready.press("q")
    assert app.filter_text == "rq" and app.view == []
    ready.press("backspace", "enter")
    assert app.input_mode is None and app.filter_text == "r"
    assert "filter: r" in ready.screen()[-2]
    ready.press("esc")
    assert app.filter_text == "" and len(app.view) == 4
    ready.press("/")
    ready.type_text("zzz")
    ready.press("esc")
    assert app.input_mode is None and app.filter_text == "" and app.message == "Filter cleared"
    ready.press(" ")
    assert app.selected_names == {"pytest"} and app.focus_index == 1
    ready.press(" ")
    assert app.selected_names == {"pytest", "requests"}
    assert ready.screen()[3].startswith("+*")
    assert "2 selected" in ready.screen()[-2]
    ready.press("esc")
    assert app.selected_names == set() and app.message == "Selection cleared"


def test_enter_runs_dry_run_then_install_and_refreshes(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("enter", settle=False)
    assert app.installing and ready.row("requests").status == "installing"
    assert tuv.SPINNER[app.spinner_index] in ready.screen()[4]
    ready.pump()
    commands = ready.backend.popen_commands
    assert len(commands) == 2
    assert commands[0][:6] == [str(Path(sys.executable)), "-m", "uv", "pip", "install", "--python"]
    assert commands[0][-2:] == ["--dry-run", "Requests==2.32.0"]
    assert commands[1][-1] == "Requests==2.32.0" and "--dry-run" not in commands[1]
    assert "--system" not in commands[1]
    assert not app.installing
    row = ready.row("requests")
    assert row.installed_version == "2.32.0" and row.status == "current" and row.updated_in_session
    assert app.message == "All packages are up to date" or "updates available" in app.message
    rendered = app.render().split("\n")
    assert tuv.BRIGHT_CYAN in rendered[4]
    assert ("outdated", app.context.id) in ready.backend.calls[-3:] or ready.backend.calls.count(("outdated", app.context.id)) == 2


def test_enter_blocked_while_loading_or_current(driver):
    default_rows(driver.backend)
    driver.backend.contexts = [make_context("venv", "proj")]
    driver.app.start_context_discovery()
    driver.step()
    driver.step()
    assert driver.app.rows and all(row.status == "loading" for row in driver.app.rows)
    driver.press("enter", settle=False)
    assert driver.app.message == "Version resolution is still in progress"
    assert driver.backend.popen_commands == []
    driver.pump()
    driver.focus("urllib3")
    driver.press("enter")
    assert driver.app.message == "urllib3 is already at 2.2.0"
    assert driver.backend.popen_commands == []
    driver.press("f2", settle=False)
    assert driver.app.prompt is not None


def test_install_failure_marks_row_and_info_shows_details(ready):
    app = ready.app
    ready.backend.popen_results = [(0, "", ""), (2, "partial out", "error: resolution failed\nmore")]
    ready.focus("requests")
    ready.press("enter")
    row = ready.row("requests")
    assert row.status == "failed" and row.installed_version == "2.31.0" and row.target_version == "2.32.0"
    assert "Install failed: Requests" in ready.messages
    assert tuv.BOLD_RED in app.render().split("\n")[4]
    app.terminal.height = 60
    ready.press("i")
    screen = ready.screen()
    text = "\n".join(screen)
    assert "Package: Requests" in text and "Version: 2.31.0" in text and "Exit code: 2" in text
    assert "resolution failed" in text and "Elapsed:" in text and "HTTP for humans" in text
    ready.press("q")
    assert not app.info_open
    ready.press("enter")
    assert ready.row("requests").status == "current" and ready.row("requests").installed_version == "2.32.0"


def test_dry_run_failure_skips_install(ready):
    ready.backend.popen_results = [(1, "", "no matching distribution")]
    ready.focus("requests")
    ready.press("enter")
    assert len(ready.backend.popen_commands) == 1
    row = ready.row("requests")
    assert row.status == "failed" and "no matching distribution" in row.last_error_detail


def test_interpreter_confirmation_is_fail_closed(driver):
    default_rows(driver.backend)
    interpreter = make_context("interpreter", "sys")
    driver.start([interpreter])
    driver.focus("requests")
    for decline in ("enter", "n", "esc", "q"):
        driver.press("enter", settle=False)
        assert driver.app.prompt is not None and driver.app.prompt.title == "Confirm interpreter install"
        driver.press(decline)
        assert driver.app.prompt is None and driver.app.message == "Install cancelled"
        assert driver.backend.popen_commands == []
    driver.press("enter", settle=False)
    driver.press("y")
    assert interpreter.confirmed_for_mutation
    assert all("--system" in cmd for cmd in driver.backend.popen_commands)
    assert driver.backend.popen_commands[0][-3:] == ["--system", "--dry-run", "Requests==2.32.0"]
    driver.focus("rich")
    driver.press("enter")
    assert driver.app.prompt is None and len(driver.installs()) == 2


def test_wait_queue_runs_after_install(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.focus("rich")
    ready.press("enter", settle=False)
    assert ready.row("rich").status == "wait" and len(app.wait_queue) == 1
    assert app.message.startswith("Queued (1 waiting)")
    ready.press("enter", settle=False)
    assert len(app.wait_queue) == 1 and app.message == "Already queued: rich"
    assert "1 queued" in ready.screen()[-2]
    assert "Wait" in ready.screen()[5] or "wait" in ready.screen()[5]
    ready.pump()
    installs = ready.installs()
    assert [cmd[-1] for cmd in installs] == ["Requests==2.32.0", "rich==14.0.0"]
    assert ready.row("rich").installed_version == "14.0.0" and app.wait_queue == []


def test_wait_queue_entry_dropped_when_target_changes(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.focus("rich")
    ready.press("enter", settle=False)
    ready.backend.rows["rich"] = "14.0.0"
    ready.backend.outdated = {}
    ready.pump()
    assert [cmd[-1] for cmd in ready.installs()] == ["Requests==2.32.0"]
    assert app.wait_queue == [] and ready.row("rich").status == "current"


def test_bulk_update_sequential_with_deferred_lookups(ready):
    app = ready.app
    ready.press("f2", settle=False)
    assert app.prompt is not None and app.prompt.title == "Confirm update all"
    assert app.prompt.message.startswith("Install updates for 2 ready package(s)? y/N")
    assert "Requests  2.31.0 -> 2.32.0" in app.prompt.message
    ready.press("esc")
    assert app.prompt is None and app.message == "Update all cancelled" and not app.bulk_active
    ready.press("f2", settle=False)
    ready.backend.calls.clear()
    ready.press("y", settle=False)
    assert app.bulk_active and ready.row("requests").status == "installing" and ready.row("rich").status == "wait"
    assert "update 1/2" in ready.screen()[-2]
    ready.step()
    ready.step()
    assert ready.row("requests").installed_version == "2.32.0"
    assert ("outdated", app.context.id) not in ready.backend.calls
    assert ready.row("rich").status == "installing"
    ready.pump()
    assert not app.bulk_active
    assert [cmd[-1] for cmd in ready.installs()] == ["Requests==2.32.0", "rich==14.0.0"]
    assert ready.backend.calls.count(("outdated", app.context.id)) == 1
    assert app.message == "Update all complete: 2 updated"
    assert not app.report_open
    assert app.health_status == "healthy"


def test_bulk_update_skips_failed_and_reports(ready):
    app = ready.app
    ready.backend.popen_results = [(0, "", ""), (1, "", "boom")]
    ready.press("f2", settle=False)
    ready.press("y")
    assert ready.row("requests").status == "failed"
    assert ready.row("rich").installed_version == "14.0.0"
    assert app.report_open and app.report_title == "Update all summary"
    text = "\n".join(app.report_lines)
    assert "Updated: 1   Failed: 1" in text and "failed     Requests" in text
    assert app.message.startswith("Update all finished: 1 updated, 1 failed")
    ready.press("q")
    assert not app.report_open


def test_bulk_update_skips_dependency_side_effects(ready):
    app = ready.app

    def apply(command):
        spec = command[-1]
        if spec.startswith("Requests"):
            ready.backend.rows["Requests"] = "2.32.0"
            ready.backend.rows["rich"] = "14.0.0"
        else:
            raise AssertionError("rich should not be installed separately")

    ready.backend.apply_install = apply
    ready.press("f2", settle=False)
    ready.press("y")
    assert [cmd[-1] for cmd in ready.installs()] == ["Requests==2.32.0"]
    assert app.report_open and "already current" in "\n".join(app.report_lines)
    assert ready.row("rich").status == "current"


def test_bulk_update_respects_selection_filter_and_pins(ready, home):
    app = ready.app
    ready.focus("rich")
    ready.press("p")
    assert app.message.startswith("Pinned rich")
    assert (home / ".tuv" / "pins.json").exists()
    assert "pinned" in ready.screen()[5]
    ready.press("f2", settle=False)
    assert app.prompt.message.startswith("Install updates for 1 ready package(s)")
    assert "(1 pinned package(s) excluded)" in app.prompt.message
    ready.press("esc")
    ready.press("p")
    assert app.message == "Unpinned rich"
    ready.press("/")
    ready.type_text("ri")
    ready.press("enter")
    ready.press("f2", settle=False)
    assert app.prompt.message.startswith("Install updates for 1 filtered package(s)")
    ready.press("esc", "esc")
    ready.focus("urllib3")
    ready.press(" ")
    ready.press("f2", settle=False)
    assert app.prompt is None and app.message == "No selected packages to update"
    ready.press("esc")
    ready.focus("rich")
    ready.press(" ")
    ready.press("f2", settle=False)
    assert app.prompt.message.startswith("Install updates for 1 selected package(s)")
    ready.press("y")
    assert [cmd[-1] for cmd in ready.installs()] == ["rich==14.0.0"]
    assert app.selected_names == set()


def test_bulk_update_blocked_while_busy_and_cancel_remaining(ready):
    app = ready.app
    ready.press("f2", settle=False)
    ready.press("y", settle=False)
    ready.press("f2", settle=False)
    assert app.message == "Update all waits until the current activity finishes"
    ready.step()
    ready.step()
    assert ready.row("rich").status == "installing"
    ready.press("esc", settle=False)
    assert app.prompt is not None and app.prompt.title == "Cancel running operation"
    ready.press("n", settle=False)
    assert app.prompt is None and app.message == "Cancel dismissed" and app.bulk_active
    ready.pump()
    assert not app.bulk_active and len(ready.installs()) == 2


def test_version_navigation_with_lazy_lookup(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("right", settle=False)
    assert app.version_loading and app.message == "Loading versions for Requests"
    ready.press("left", settle=False)
    assert app.message == "Version change waits for Requests versions"
    ready.pump()
    row = ready.row("requests")
    assert row.full_versions_loaded and row.yanked_versions == {"2.32.1"}
    assert row.candidate_versions == ["2.30.0", "2.31.0", "2.32.0", "2.32.1"]
    assert row.target_version == "2.31.0" and row.status == "current"
    ready.press("left")
    assert row.target_version == "2.30.0" and row.status == "ready"
    assert app.target_overrides == {"requests": "2.30.0"}
    ready.press("right", "right")
    assert row.target_version == "2.32.0"
    ready.press("right")
    assert row.target_version == "2.32.0"
    ready.press("left", "left")
    ready.press("r")
    row = ready.row("requests")
    assert row.target_version == "2.30.0" and row.status == "ready"
    assert row.full_versions_loaded and row.candidate_versions == ["2.30.0", "2.31.0", "2.32.0", "2.32.1"]
    ready.press("enter")
    assert ready.installs()[-1][-1] == "Requests==2.30.0"
    assert app.target_overrides == {}


def test_version_selector_overlay(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("f4", settle=False)
    assert app.version_overlay and app.version_options == ["2.32.0", "2.31.0"]
    ready.press("enter", settle=False)
    assert app.message == "Version lookup is not ready for Requests"
    assert "Loading versions" in "\n".join(ready.screen())
    ready.pump()
    assert app.version_options == ["2.32.1", "2.32.0", "2.31.0", "2.30.0"]
    assert app.version_overlay_index == 1
    screen = "\n".join(ready.screen())
    assert "Versions: Requests" in screen and "(yanked)" in screen and "installed" in screen and "target" in screen
    ready.press("down", "down", "down", "down")
    assert app.version_overlay_index == 3
    ready.press("up", "home")
    assert app.version_overlay_index == 0
    ready.press("enter", settle=False)
    assert app.prompt is not None and app.prompt.title == "Confirm yanked version"
    ready.press("n")
    assert app.prompt is None and not app.version_overlay
    assert ready.row("requests").target_version == "2.32.1" and ready.installs() == []
    ready.press("f4", settle=False)
    assert app.version_overlay_index == 0
    ready.press("end", "up", "enter")
    assert ready.installs() == [] and ready.row("requests").status == "current"
    assert app.message == "Requests is already at 2.31.0"
    ready.press("f4", settle=False)
    ready.press("end", "enter")
    assert ready.installs()[-1][-1] == "Requests==2.30.0"
    ready.press("f4", settle=False)
    ready.press("q")
    assert not app.version_overlay


def test_version_lookup_failure_paths(ready):
    app = ready.app
    ready.backend.versions["urllib3"] = RuntimeError("index unreachable")
    ready.focus("urllib3")
    ready.press("right")
    row = ready.row("urllib3")
    assert row.status == "nodata" and row.last_error == "Version lookup failed"
    assert app.message.startswith("Version lookup failed")
    ready.press("f4", settle=False)
    assert "Loading versions" in "\n".join(ready.screen())
    ready.pump()
    assert "Version lookup failed" in "\n".join(ready.screen())
    ready.press("esc")
    ready.press("enter")
    assert ready.installs() == [] and row.status == "nodata"
    ready.press("r")
    assert ready.row("urllib3").status == "current"
    ready.backend.versions["requests"] = RuntimeError("private index down")
    ready.focus("requests")
    ready.press("right")
    assert ready.row("requests").status == "ready"
    assert "using uv-reported versions" in app.message
    ready.press("enter")
    assert ready.installs()[-1][-1] == "Requests==2.32.0"


def test_version_lookup_failure_before_outdated_marks_nodata(driver):
    default_rows(driver.backend)
    driver.backend.outdated = {}
    driver.backend.outdated_warning = "Outdated data unavailable: offline"
    driver.backend.versions = {"rich": RuntimeError("offline")}
    driver.start([make_context("venv", "proj")])
    driver.focus("rich")
    driver.press("right")
    row = driver.row("rich")
    assert row.status == "nodata"
    assert "no data" in driver.screen()[5]
    driver.press("enter")
    assert driver.backend.popen_commands == []


def test_new_package_flow(ready):
    app = ready.app
    ready.backend.versions["httpx"] = (["0.26.0", "0.27.0", "0.27.1"], {"0.27.1"})
    ready.press("n")
    assert app.input_mode == "new_package"
    ready.type_text("bad name!")
    ready.press("enter")
    assert app.message == "Invalid package name: bad name!" and app.input_mode is None
    ready.press("n")
    ready.type_text("Requests")
    ready.press("enter")
    assert app.focused_row().name == "requests" and "already installed" in app.message
    ready.press("n")
    ready.type_text("httpq")
    ready.press("backspace")
    ready.type_text("x")
    assert app.input_buffer == "httpx"
    assert "Package name:" in "\n".join(ready.screen())
    ready.press("enter")
    assert app.version_overlay and app.new_package is not None
    assert app.version_options == ["0.27.1", "0.27.0", "0.26.0"] and app.version_overlay_index == 1
    ready.press("up", "enter", settle=False)
    assert app.prompt is not None and app.prompt.title == "Confirm yanked version"
    ready.press("y")
    assert ready.installs()[-1][-1] == "httpx==0.27.1"
    assert ready.row("httpx").installed_version == "0.27.1" and ready.row("httpx").updated_in_session
    assert "Installed httpx==0.27.1" in ready.messages


def test_new_package_failure_opens_report(ready):
    app = ready.app
    ready.backend.versions["httpx"] = (["0.27.0"], set())
    ready.backend.popen_results = [(0, "", ""), (1, "", "could not build")]
    ready.press("n")
    ready.type_text("httpx")
    ready.press("enter")
    ready.press("enter")
    assert app.report_open and "could not build" in "\n".join(app.report_lines)
    assert "Install failed: httpx" in ready.messages
    ready.press("esc")
    ready.press("n")
    ready.type_text("nothing")
    ready.press("enter")
    assert app.message.startswith("No installable versions for nothing")
    ready.press("n")
    ready.press("esc")
    assert app.input_mode is None and app.message == "Cancelled"


def test_uninstall_flow(ready):
    app = ready.app
    ready.focus("urllib3")
    ready.press("delete", settle=False)
    assert app.prompt is not None and app.prompt.title == "Confirm uninstall"
    assert "WARNING - required by: Requests" in app.prompt.message
    ready.press("enter")
    assert app.prompt is None and app.message == "Uninstall cancelled"
    ready.focus("pytest")
    ready.press("delete", settle=False)
    assert "Not required by any other installed package." in app.prompt.message
    ready.press("y")
    command = ready.backend.popen_commands[-1]
    assert command[3:5] == ["pip", "uninstall"] and command[-1] == "pytest" and len(ready.backend.popen_commands) == 1
    assert "Uninstalled pytest" in ready.messages
    assert app.find_row("pytest") is None
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.focus("rich")
    ready.press("delete", settle=False)
    assert app.message == "Uninstall waits until the current activity finishes"
    ready.pump()


def test_info_panel_tabs_and_trees(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("f3")
    assert app.info_open and app.info_tab == 0
    text = "\n".join(ready.screen())
    assert "Information - Details" in text and "Pinned: no" in text and "Dependency packages:" in text and "urllib3" in text
    ready.press("right")
    assert app.info_tab == 1
    text = "\n".join(ready.screen())
    assert "Information - Dependencies" in text and "└── urllib3 v2.2.0" in text and "Requests v2.31.0 (latest: v2.32.0)" in text
    ready.press("3")
    assert app.info_tab == 2
    assert "Reverse dependency tree" in "\n".join(ready.screen())
    ready.press("left", "left")
    assert app.info_tab == 0
    ready.press("left")
    assert app.info_tab == 2
    ready.press("enter")
    assert not app.info_open and app.info_tab == 0
    ready.focus("urllib3")
    ready.press("i", "3")
    assert "└── Requests v2.31.0 (latest: v2.32.0)" in "\n".join(ready.screen())
    ready.press("esc")


def test_info_tree_marks_cycles(ready):
    ready.backend.deps = tuv.DependencyInfo(
        safe_names=set(),
        dependencies={"requests": ["rich"], "rich": ["Requests"], "urllib3": [], "pytest": []},
        usage={"requests": ["rich"], "rich": ["Requests"], "urllib3": [], "pytest": []},
        descriptions={},
    )
    ready.press("r")
    ready.focus("requests")
    ready.press("f3", "2")
    text = "\n".join(ready.screen())
    assert "(cycle)" in text
    ready.press("esc")


def test_report_and_info_scrolling(ready):
    app = ready.app
    app.open_report("Long report", [f"line {index}" for index in range(100)])
    app.terminal.height = 20
    ready.press("down", "down", "pagedown")
    assert app.report_scroll > 2
    title = ready.screen()
    assert any("Long report (" in line for line in title)
    ready.press("end")
    assert "line 99" in "\n".join(ready.screen())
    ready.press("home")
    assert app.report_scroll == 0
    ready.press("pageup", "up")
    assert app.report_scroll == 0
    ready.press("enter")
    assert not app.report_open


def test_health_badge_states_and_report(ready):
    app = ready.app
    assert app.health_badge() == "health: ok"
    ready.backend.health = (1, "Checked 4 packages\nFound 2 incompatibilities\nrich requires x", "")
    ready.press("r")
    assert app.health_badge() == "health: 2 issue(s)"
    ready.press("f6")
    assert app.report_open and app.report_title == "Environment health"
    assert app.report_lines[0] == "Status: 2 compatibility issue(s)" and "rich requires x" in app.report_lines
    ready.press("esc")
    ready.backend.health = (1, "pkg a requires b", "")
    ready.press("r")
    assert app.health_badge() == "health: 1 issue(s)"
    ready.backend.command_results["check"] = (0, "", "")
    ready.backend.health = (0, "", "")
    ready.press("r")
    assert app.health_badge() == "health: ok"
    ready.press("f6")
    assert "All installed packages are compatible" in app.report_lines
    ready.press("esc")


def test_health_check_error(ready, monkeypatch):
    app = ready.app
    monkeypatch.setattr(tuv, "uv_command", lambda context, args: (_ for _ in ()).throw(RuntimeError("no uv")))
    ready.press("r")
    assert app.health_badge() == "health: check failed"
    ready.press("f6")
    assert "no uv" in "\n".join(app.report_lines)


def test_f6_before_check_starts_one(driver):
    default_rows(driver.backend)
    driver.backend.contexts = [make_context("venv", "proj")]
    driver.app.start_context_discovery()
    driver.step()
    driver.step()
    driver.app.health_status = "unknown"
    driver.app.health_loading = False
    driver.press("f6", settle=False)
    assert driver.app.message == "Checking environment health"
    driver.pump()
    assert driver.app.health_status == "healthy"


def test_quit_prompts_while_installing(ready):
    app = ready.app
    ready.press("q")
    assert app.should_quit
    app.should_quit = False
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.press("f10", settle=False)
    assert app.prompt is not None and app.prompt.title == "Quit while installing"
    ready.press("n", settle=False)
    assert app.prompt is None and not app.should_quit and app.message == "Quit cancelled; the operation continues"
    ready.press("q", settle=False)
    assert app.prompt is not None
    app.request_quit()
    assert app.should_quit
    ready.pump()


def test_quit_confirmation_cancels_install(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.press("q", settle=False)
    assert app.prompt is not None and app.prompt.title == "Quit while installing"
    ready.press("y", settle=False)
    assert app.should_quit and app.install_cancel_event.is_set() and app.prompt is None
    ready.pump()
    assert "Install cancelled: Requests" in ready.messages
    assert ready.installs() == []


def test_cancel_running_install(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.press("esc", settle=False)
    assert app.prompt.title == "Cancel running operation"
    ready.press("y", settle=False)
    assert app.message.startswith("Cancelling")
    ready.pump()
    assert "Install cancelled: Requests" in ready.messages and ready.installs() == []
    assert ready.row("requests").status == "ready"


def test_cancel_terminates_live_process(ready):
    app = ready.app
    proc = object()
    app.installing = True
    app.install_proc = proc
    app.cancel_active_install()
    assert ready.backend.terminated == [proc]
    app.installing = False
    app.install_proc = None


def test_context_selector_switch_and_blocked_while_installing(ready):
    app = ready.app
    assert len(app.contexts) == 2
    ready.press("f9")
    assert app.context_overlay and app.context_overlay_index == 0
    text = "\n".join(ready.screen())
    assert "Context selector" in text and "tuv venv" in text and "[tuv uv]" in text
    ready.press("down", "down")
    assert app.context_overlay_index == 1
    ready.press("enter")
    assert not app.context_overlay and app.context.type == "tuv" and app.refresh_generation == 2
    ready.press("f9", "up", "enter")
    assert app.context.type == "venv" and app.refresh_generation == 3
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.press("f9", "down", "enter", settle=False)
    assert not app.context_overlay and app.context.type == "venv"
    assert app.message == "Context switch waits until the current install finishes"
    ready.pump()
    ready.press("f9", "esc")
    assert not app.context_overlay
    ready.press("f9", "f9")
    assert not app.context_overlay


def test_context_selector_pending_cancelled_with_esc(driver):
    default_rows(driver.backend)
    driver.backend.contexts = [make_context("venv", "proj")]
    driver.app.start_context_discovery()
    driver.press("f9", settle=False)
    assert driver.app.context_overlay_pending
    driver.press("q", settle=False)
    assert not driver.app.context_overlay_pending and driver.app.message == "Context selector cancelled"
    driver.pump()
    assert not driver.app.context_overlay


def test_rescan_preserves_selection(ready):
    app = ready.app
    other = make_context("venv", "other", source="scanned")
    ready.backend.contexts = [other, app.context, make_context("tuv", "runner", source="tuv")]
    ready.press("f5")
    assert app.context.id == tuv.stable_context_id("venv", Path("/envs/.venv"))
    assert app.context_index == 1 and app.message == "Contexts rescanned: 3 found"
    assert app.refresh_generation == 1
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.press("f5", settle=False)
    assert app.message == "Context rescan waits until the current install finishes"
    ready.pump()


def test_create_venv_from_selector(ready, tmp_path, monkeypatch):
    app = ready.app
    monkeypatch.chdir(tmp_path)
    interpreter = make_context("interpreter", "sys")
    app.contexts.insert(0, interpreter)
    ready.press("f9", "n")
    assert app.input_mode == "new_venv" and app.input_buffer == ".venv"
    assert "Create venv" in "\n".join(ready.screen())
    ready.press("backspace", "backspace", "backspace", "backspace", "backspace")
    ready.type_text("bad/name")
    ready.press("enter")
    assert app.message.startswith("Invalid venv directory name")
    ready.press("f9", "n", "enter", settle=False)
    assert app.creating_venv
    new_root = (tmp_path / ".venv").resolve()
    created = make_context("venv", "created", source="scanned")
    created.root_path = new_root
    created.id = tuv.stable_context_id("venv", new_root)
    ready.backend.contexts = [interpreter, created, *ready.backend.contexts]
    ready.pump()
    assert not app.creating_venv
    venv_command = next(cmd for cmd in ready.backend.commands if cmd[1:3] == ["-m", "venv"])
    assert venv_command[0] == str(interpreter.python_path) and venv_command[-1] == str(tmp_path / ".venv")
    assert app.context.id == created.id
    assert (tmp_path / ".venv" / "pyvenv.cfg").exists()
    ready.press("f9", "n", "enter")
    assert app.message.startswith("Directory already exists")


def test_create_venv_during_install_keeps_context(ready, tmp_path, monkeypatch):
    app = ready.app
    monkeypatch.chdir(tmp_path)
    interpreter = make_context("interpreter", "sys")
    app.contexts.append(interpreter)
    current = app.context
    ready.focus("requests")
    ready.press("enter", settle=False)
    assert app.installing
    ready.press("f9", "n", "enter", settle=False)
    created = make_context("venv", "created", source="scanned")
    created.root_path = (tmp_path / ".venv").resolve()
    created.id = tuv.stable_context_id("venv", created.root_path)
    ready.backend.contexts = [interpreter, created, *ready.backend.contexts]
    ready.runner.pending.reverse()  # run the venv creation before the install worker
    ready.step()
    ready.runner.pending.reverse()  # run the rescan before the install worker
    ready.step()
    assert app.context.id == current.id and app.installing
    assert app.message.startswith("Created venv is listed")
    ready.pump()
    assert not app.installing and ready.row("requests").installed_version == "2.32.0"
    assert any(context.id == created.id for context in app.contexts)


def test_install_finishing_for_unselected_context_releases_slot(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("enter", settle=False)
    other = make_context("venv", "other")
    app.contexts.append(other)
    app.context_index = app.contexts.index(other)
    ready.pump()
    assert not app.installing and app.context.id == other.id


def test_create_venv_failure_reports(ready, tmp_path, monkeypatch):
    app = ready.app
    monkeypatch.chdir(tmp_path)
    app.contexts.insert(0, make_context("interpreter", "sys"))
    ready.backend.command_results["-m venv"] = (1, "", "venv failed hard")
    ready.press("f9", "n", "enter")
    assert app.report_open and "venv failed hard" in "\n".join(app.report_lines)
    assert app.message.startswith("Venv creation failed")


def test_runner_uv_repair_failure_blocks_mutation(driver, monkeypatch):
    default_rows(driver.backend)
    context = make_context("venv", "proj", provider=None)
    driver.backend.provider = None
    driver.backend.command_results["pip --version"] = (0, "pip 24", "")
    driver.backend.command_results["pip install uv"] = (1, "", "No matching distribution for uv")
    driver.start([context])
    app = driver.app
    assert app.mutation_blocked_reason == "Tuv runner uv installation failed"
    assert app.info_open
    assert len(app.rows) == 1 and app.rows[0].operational_error and app.rows[0].status == "failed"
    text = "\n".join(driver.screen())
    assert "Tuv operational error" in text and "No matching distribution for uv" in text
    driver.press("esc")
    driver.press("enter")
    assert driver.backend.popen_commands == []
    driver.press("f2", settle=False)
    assert app.message == "Tuv runner uv installation failed"
    driver.press("delete", "n", "p", " ")
    assert app.prompt is None and app.input_mode is None


def test_runner_uv_repair_success_continues(driver):
    default_rows(driver.backend)
    context = make_context("venv", "proj", provider=None)
    driver.backend.provider = None
    driver.backend.command_results["pip --version"] = [(1, "", "no pip"), (0, "pip 24", "")]
    driver.backend.command_results["ensurepip"] = (0, "", "")
    driver.backend.command_results["pip install uv"] = (0, "installed", "")
    driver.backend.command_results["-m uv --version"] = (0, "uv 1.0", "")
    driver.backend.contexts = [context]
    driver.app.start_context_discovery()
    driver.step()
    assert driver.app.message.startswith("Installing uv into Tuv runner venv")
    driver.backend.provider = PROVIDER
    driver.pump()
    assert "uv installed into Tuv runner venv" in driver.messages
    assert driver.app.mutation_blocked_reason is None
    assert len(driver.app.rows) == 4
    joined = [" ".join(cmd) for cmd in driver.backend.commands]
    assert any("ensurepip" in cmd for cmd in joined) and any("pip install uv" in cmd for cmd in joined)


def test_refresh_key_blocked_while_installing(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.press("r", settle=False)
    assert app.message == "Refresh waits until the current install finishes"
    ready.pump()


def test_refresh_failure_after_install_continues_queue(ready):
    app = ready.app
    ready.focus("requests")
    ready.press("enter", settle=False)
    ready.focus("rich")
    ready.press("enter", settle=False)
    ready.backend.list_error = RuntimeError("list failed")
    ready.step()
    ready.step()
    assert any(message.startswith("Refresh failed") for message in ready.messages)
    assert app.installing and ready.row("rich").status == "installing"
    ready.backend.list_error = None
    ready.pump()
    assert [cmd[-1] for cmd in ready.installs()] == ["Requests==2.32.0", "rich==14.0.0"]
    assert ready.row("rich").installed_version == "14.0.0"


def test_terminal_too_small_and_render_diff(ready):
    app = ready.app
    app.terminal.width = 30
    app.terminal.height = 5
    screen = ready.screen()
    assert screen[0].startswith("Terminal too small (30x5)") and len(screen) == 5
    app.terminal.width = 120
    app.terminal.height = 30
    first = app.render()
    app.write_render_diff(first)
    assert app.terminal.writes[-1] == tuv.HOME + first
    ready.press("down")
    second = app.render()
    app.write_render_diff(second)
    assert app.terminal.writes[-1].count("\x1b[") >= 2 and "\x1b[4;1H" in app.terminal.writes[-1]
    assert len(app.terminal.writes[-1]) < len(second)
    app.write_render_diff(second)
    assert len(app.terminal.writes) == 2


def test_modal_dims_background_and_embeds_title(ready):
    app = ready.app
    ready.press("f9")
    rendered = app.render().split("\n")
    assert rendered[0].startswith(tuv.MODAL_BACKDROP)
    assert any("┌ Context selector" in line for line in rendered)
    ready.press("esc")
    assert not app.render().split("\n")[0].startswith(tuv.MODAL_BACKDROP)


def test_run_loop_processes_keys_and_quits(ready):
    app = ready.app
    app.terminal.keys = ["down", "q"]
    assert app.run() == 0
    assert app.should_quit and app.focus_index == 1
    assert app.terminal.writes


def test_pin_persists_across_instances(ready, home, threads, backend, monkeypatch):
    app = ready.app
    ready.focus("requests")
    ready.press("p")
    assert "requests" in app.pinned_names()
    monkeypatch.setattr(tuv.signal, "signal", lambda *a, **k: None)
    fresh = tuv.TuvApp()
    assert fresh.pinned_by_context == {app.context.id: {"requests"}}


def test_color_hint_during_refresh(ready):
    app = ready.app
    ready.press("r", settle=False)
    ready.step()
    requests = ready.row("requests")
    assert requests.status == "loading" and requests.color_hint == "outdated"
    assert ready.row("urllib3").color_hint == "current"
    rendered = app.render().split("\n")
    assert tuv.YELLOW in rendered[4] and tuv.LIGHT_GREEN in rendered[6]
    ready.pump()

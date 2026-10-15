"""Pure helper behaviour: versions, keys, text layout, sanitizing, metadata parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

import tuv


def test_version_key_orders_pep440_and_tolerates_garbage():
    values = ["1.10", "1.2", "1.0rc1", "1.0", "2.0a1", "weird-3", "weird-10"]
    ordered = sorted(values, key=tuv.version_key)
    assert ordered[:5] == ["1.0rc1", "1.0", "1.2", "1.10", "2.0a1"]
    assert ordered[5:] == ["weird-3", "weird-10"]


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("requests-2.32.3-py3-none-any.whl", "2.32.3"),
        ("https://files.example/requests-2.32.3.tar.gz?token=abc", "2.32.3"),
        ("Requests-2.0.0.zip", "2.0.0"),
        ("requests-2.32.3-py3-none-any.whl.metadata", None),
        ("other-2.32.3-py3-none-any.whl", None),
        ("not-a-dist", None),
    ],
)
def test_version_from_distribution_filename(filename, expected):
    assert tuv.version_from_distribution_filename(filename, "requests") == expected


def test_versions_from_simple_json_filters_yanked_and_requires_python():
    data = {
        "files": [
            {"filename": "pkg-1.0-py3-none-any.whl", "yanked": False},
            {"filename": "pkg-1.1-py3-none-any.whl", "yanked": "bad"},
            {"filename": "pkg-1.1.tar.gz", "yanked": False},
            {"filename": "pkg-2.0-py3-none-any.whl", "yanked": True},
            {"filename": "pkg-3.0-py3-none-any.whl", "requires-python": ">=3.99"},
        ]
    }
    versions, yanked = tuv.versions_from_simple_json(data, "pkg", "3.12.1")
    assert versions == {"1.0", "1.1", "2.0"}
    assert yanked == {"2.0"}
    only_versions = {"versions": ["1.0", "1.5"]}
    assert tuv.versions_from_simple_json(only_versions, "pkg") == ({"1.0", "1.5"}, set())
    assert tuv.versions_from_simple_json("garbage", "pkg") == (set(), set())


def test_versions_from_simple_html_reads_pep503_attributes():
    html = """
    <html><body>
    <a href="../../pkg-1.0-py3-none-any.whl#sha256=1">pkg-1.0-py3-none-any.whl</a>
    <a href="pkg-1.2.tar.gz" data-yanked="defect">pkg-1.2.tar.gz</a>
    <a href="pkg-1.3.tar.gz" data-requires-python="&gt;=3.13">pkg-1.3.tar.gz</a>
    <a href="other-9.9.tar.gz">other</a>
    </body></html>
    """
    versions, yanked = tuv.versions_from_simple_html(html, "pkg", "3.12.1")
    assert versions == {"1.0", "1.2"}
    assert yanked == {"1.2"}


def test_requires_python_allows():
    assert tuv.requires_python_allows(None, "3.12.1")
    assert tuv.requires_python_allows(">=3.8", "3.12.1")
    assert not tuv.requires_python_allows(">=3.13", "3.12.1")
    assert not tuv.requires_python_allows("garbage>>", "3.12.1")


@pytest.mark.parametrize(
    "data, expected",
    [
        ("\r", "enter"),
        ("\n", "enter"),
        ("\x1b", "esc"),
        ("\x03", "quit"),
        ("\x04", "quit"),
        ("\x7f", "backspace"),
        ("\x08", "backspace"),
        ("a", "a"),
        ("Q", "Q"),
        (" ", " "),
        ("\x1b[A", "up"),
        ("\x1b[21~", "f10"),
        ("\x1bOQ", "f2"),
        ("\x01", None),
        ("\x1b[Z", None),
    ],
)
def test_normalize_key(data, expected):
    assert tuv.normalize_key(data) == expected


def test_sanitize_terminal_text_strips_sequences_and_controls():
    text = "a\x1b[31mred\x1b[0m b\x1b]0;title\x07c\x1b]8;;http://x\x1b\\d\x1bPdcs\x1b\\e\tf\ng\rh\x07i\u202ej\x01k"
    assert tuv.sanitize_terminal_text(text) == "ared bcde f g hijk"
    assert tuv.sanitize_terminal_text(12) == "12"
    assert tuv.sanitize_terminal_text("trailing\x1b") == "trailing"


def test_display_width_and_truncate_handle_wide_and_combining_characters():
    assert tuv.display_width("abc") == 3
    assert tuv.display_width("日本語") == 6
    assert tuv.display_width("e\u0301") == 1
    assert tuv.truncate("hello world", 8) == "hello w~"
    assert tuv.truncate("日本語テキスト", 7) == "日本語~"
    assert tuv.truncate("abc", 0) == ""
    assert tuv.truncate("abc", 1) == "a"
    assert tuv.truncate("日", 1) == "~"
    assert tuv.pad_display("日", 4) == "日  "
    assert tuv.truncate("x\x1b[31my", 10) == "xy"


def test_slice_display_respects_cell_boundaries():
    assert tuv.slice_display("abcdef", 2, 3) == "cde"
    assert tuv.slice_display("日本語", 0, 3) == "日"
    assert tuv.slice_display("日本語", 1, 4) == "本"
    assert tuv.slice_display("abc", 5, 3) == ""
    assert tuv.slice_display("abc", 0, 0) == ""


def test_paste_box_keeps_dimmed_margins():
    lines = [tuv.MODAL_BACKDROP + "0123456789" + tuv.RESET, "0123456789"]
    box = ["[box]"]
    result = tuv.paste_box(lines, box, 0, 2, 10)
    assert tuv.strip_ansi(result[0]) == "01[box]789"
    assert result[0].startswith(tuv.MODAL_BACKDROP + "01" + tuv.RESET + "[box]")
    assert result[1] == "0123456789"
    assert tuv.paste_box(lines, ["a", "b", "c"], 1, 0, 10)[1] == "a123456789"


def test_box_border_embeds_title():
    assert tuv.box_border(12, "top", "Title") == "┌ Title ───┐"
    assert tuv.box_border(12, "top") == "┌──────────┐"
    assert tuv.box_border(12, "bottom") == "└──────────┘"
    assert tuv.box_border(8, "top", "Information") == "┌ In~ ─┐"
    assert tuv.box_line("hi", 8) == "│ hi   │"


def test_columns_scale_with_width():
    assert tuv.table_columns(130) == (80, 20, 20, 10)
    assert tuv.table_columns(72) == (22, 20, 20, 10)
    assert tuv.table_columns(60) == (18, 16, 16, 10)
    assert tuv.table_columns(40) == (12, 16, 16, 10)


def test_step_index_clamps():
    assert tuv.step_index(0, "up", 5, 2) == 0
    assert tuv.step_index(0, "down", 5, 2) == 1
    assert tuv.step_index(4, "down", 5, 2) == 4
    assert tuv.step_index(1, "pagedown", 5, 2) == 3
    assert tuv.step_index(3, "pageup", 5, 10) == 0
    assert tuv.step_index(2, "end", 5, 2) == 4
    assert tuv.step_index(2, "home", 5, 2) == 0
    assert tuv.step_index(2, "x", 5, 2) == 2
    assert tuv.step_index(3, "down", 0, 2) == 0


def test_decode_key_sequences():
    assert tuv.decode_key(b"", True) == (None, 0)
    assert tuv.decode_key(b"\x1b[A", True) == ("up", 3)
    assert tuv.decode_key(b"\x1b[Axyz", True) == ("up", 3)
    assert tuv.decode_key(b"\x1b", True) == (None, 0)
    assert tuv.decode_key(b"\x1b", False) == ("esc", 1)
    assert tuv.decode_key(b"\x1b[", True) == (None, 0)
    assert tuv.decode_key(b"\x1b[2", True) == (None, 0)
    assert tuv.decode_key(b"\x1b[2", False) == (None, 3)
    assert tuv.decode_key(b"\x1b[1;5Cq", True) == (None, 6)
    assert tuv.decode_key(b"\x1bq", True) == ("esc", 1)
    assert tuv.decode_key(b"q", True) == ("q", 1)
    assert tuv.decode_key(b"\r", True) == ("enter", 1)
    assert tuv.decode_key("é".encode(), True) == ("é", 2)
    assert tuv.decode_key("é".encode()[:1], True) == (None, 0)
    assert tuv.decode_key("é".encode()[:1], False) == (None, 1)
    assert tuv.decode_key(b"\x80", True) == (None, 1)
    assert tuv.decode_key("日".encode() + b"x", True) == ("日", 3)


def test_command_detail_and_formatting_helpers():
    detail = tuv.command_detail(["uv", "pip install", "x"], 2, "out1\nout2\n", "", 1.26)
    assert detail.splitlines()[0] == 'Command: uv "pip install" x'
    assert "Exit code: 2" in detail and "Elapsed: 1.3s" in detail
    assert detail.endswith("stderr:\n(empty)")
    assert tuv.last_lines("a\n\nb\nc\n", 2) == "b\nc"
    assert tuv.last_lines("", 2) == ""
    assert tuv.quote_arg("") == '""'
    assert tuv.quote_arg('a "b"') == '"a \\"b\\""'
    no_cmd = tuv.command_detail(["x"], 0, "", "", 0, include_command=False)
    assert not no_cmd.startswith("Command")


def test_redact_url_and_simple_project_url():
    assert tuv.redact_url("https://user:pw@host:8443/simple/?token=1#f") == "https://host:8443/simple/"
    assert tuv.simple_project_url("https://pypi.org/simple", "Requests_Oauth") == "https://pypi.org/simple/requests-oauth/"
    assert tuv.simple_project_url("https://pypi.org/simple/", "x") == "https://pypi.org/simple/x/"
    assert tuv.simple_project_url("https://h.example/simple?token=t", "x") == "https://h.example/simple/x/?token=t"


def test_dependency_name_from_requirement_evaluates_markers_and_extras():
    env = {"sys_platform": "linux", "python_version": "3.12", "python_full_version": "3.12.1"}
    assert tuv.dependency_name_from_requirement("Requests>=2 ; python_version >= '3.8'", env) == "requests"
    assert tuv.dependency_name_from_requirement("win32-only ; sys_platform == 'win32'", env) is None
    assert tuv.dependency_name_from_requirement("colorama ; extra == 'cli'", env, {"cli"}) == "colorama"
    assert tuv.dependency_name_from_requirement("colorama ; extra == 'cli'", env, set()) is None
    assert tuv.dependency_name_from_requirement("Foo_Bar[x]>=1", env) == "foo-bar"
    assert tuv.dependency_name_from_requirement("junk >>> ???", env) == "junk"
    assert tuv.extra_names_from_requirement("x ; extra == 'A' or extra == \"b_c\"") == {"a", "b-c"}
    assert tuv.extra_names_from_requirement("garbage ;; extra == 'Raw_Name'") == {"Raw_Name"}
    assert tuv.extra_marker_values({"Raw_Name"}) == ["Raw_Name", "raw-name", "raw_name"]


def test_single_flight_shares_work_and_caches_successes():
    import threading
    import time

    flight = tuv.SingleFlight()
    for retry, expected_calls in ((False, 1), (True, 2)):
        calls: list[int] = []
        gate = threading.Event()
        results: list[object] = []

        def compute():
            calls.append(1)
            gate.wait(2)
            return None

        owner = threading.Thread(target=lambda: results.append(flight.get("k", compute, 5, retry=retry)))
        owner.start()
        time.sleep(0.05)
        waiter = threading.Thread(target=lambda: results.append(flight.get("k", compute, 5, retry=retry)))
        waiter.start()
        time.sleep(0.05)
        gate.set()
        owner.join()
        waiter.join()
        assert results == [None, None] and len(calls) == expected_calls
    assert flight.get("s", lambda: "value", 1) == "value"
    assert flight.get("s", lambda: "other", 1) == "value"
    flight.clear()
    assert flight.get("s", lambda: "other", 1) == "other"
    expiring = tuv.SingleFlight(ttl=0.0)
    assert expiring.get("x", lambda: 1, 1) == 1 and expiring.get("x", lambda: 2, 1) == 2


def test_posix_key_reader_bursts_partial_sequences_and_eof():
    terminal = tuv.Terminal()
    feeds = [b"\x1b[Aq\xc3\xa9\x1b[", b""]

    def fill(timeout, ignore_wake=False):
        data = feeds.pop(0) if feeds else b""
        if not data:
            terminal._eof = True
            return False
        terminal._buffer += data
        return True

    terminal._fill = fill
    keys = [terminal._read_posix_key(0.1) for _ in range(5)]
    assert keys == ["up", "q", "é", None, "quit"]
    assert terminal._buffer == b""


def test_short_description():
    assert tuv.short_description(None) is None
    assert tuv.short_description("  ") is None
    assert tuv.short_description(" a \n b ") == "a b"
    assert len(tuv.short_description("x" * 400)) == 180


def test_parse_py_launcher_output():
    out = " -V:3.12 *        C:\\Python312\\python.exe\n -V:3.11          C:\\Users\\x\\AppData\\python.exe\n"
    assert tuv.parse_py_launcher_output(out) == [Path("C:\\Python312\\python.exe"), Path("C:\\Users\\x\\AppData\\python.exe")]


def test_stable_context_id_normalizes_case_on_windows():
    a = tuv.stable_context_id("venv", Path("/Envs/Proj"))
    b = tuv.stable_context_id("venv", Path("/envs/proj"))
    assert (a == b) == tuv.IS_WINDOWS
    assert a.startswith("venv:")


def test_load_outdated_targets_parses_uv_json(monkeypatch):
    from conftest import make_context

    context = make_context("venv", "x")
    data = [{"name": "Requests", "version": "1.0", "latest_version": "2.0"}, {"name": "b", "latest": "2.1"}, {"name": "c"}, "junk"]
    monkeypatch.setattr(tuv, "run_uv_json", lambda ctx, args: data)
    assert tuv.load_outdated_targets(context) == ({"requests": "2.0", "b": "2.1"}, None)
    monkeypatch.setattr(tuv, "run_uv_json", lambda ctx, args: {"not": "a list"})
    assert tuv.load_outdated_targets(context) == ({}, "Outdated data unavailable: uv returned unexpected data")
    monkeypatch.setattr(tuv, "run_uv_json", lambda ctx, args: (_ for _ in ()).throw(RuntimeError("offline\nreally")))
    targets, warning = tuv.load_outdated_targets(context)
    assert targets == {} and warning == "Outdated data unavailable: offline\nreally"


def test_build_package_rows_parses_uv_json(monkeypatch):
    from conftest import make_context

    context = make_context("venv", "x")
    data = [{"name": "Zed", "version": "1.0"}, {"name": "alpha", "version": "2.0"}, {"name": "nover"}]
    monkeypatch.setattr(tuv, "run_uv_json", lambda ctx, args: data)
    rows = tuv.build_package_rows(context, {"zed"})
    assert [(r.name, r.display_name, r.installed_version, r.target_version, r.status, r.updated_in_session) for r in rows] == [
        ("alpha", "alpha", "2.0", "2.0", "loading", False),
        ("zed", "Zed", "1.0", "1.0", "loading", True),
    ]
    assert rows[0].candidate_versions == ["2.0"]


def test_pins_roundtrip_and_quarantine(home):
    assert tuv.load_pins() == {}
    pins, error = tuv.update_pinned_package("venv:a", "Requests", True)
    assert error is None and pins == {"venv:a": {"requests"}}
    pins, _ = tuv.update_pinned_package("venv:a", "rich", True)
    pins, _ = tuv.update_pinned_package("venv:a", "requests", False)
    assert pins == {"venv:a": {"rich"}}
    pins, _ = tuv.update_pinned_package("venv:a", "rich", False)
    assert pins == {}
    assert tuv.load_pins() == {}
    path = tuv.pins_file_path()
    path.write_text("{not json", encoding="utf-8")
    assert tuv.load_pins() == {}
    assert not path.exists()
    assert list(path.parent.glob("pins.corrupt-*.json"))
    assert tuv.parse_pins_data({"a": ["X", " "], "b": "nope", 3: ["c"]}) == {"a": {"x"}}


def test_pins_lock_blocks_concurrent_writer(home, monkeypatch):
    lock = tuv.pins_file_path().with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("1", encoding="utf-8")
    monkeypatch.setattr(tuv.time, "sleep", lambda s: None)
    pins, error = tuv.update_pinned_package("venv:a", "requests", True)
    assert error is not None and "lock" in error
    assert pins == {}
    lock.unlink()
    pins, error = tuv.update_pinned_package("venv:a", "requests", True)
    assert error is None and pins == {"venv:a": {"requests"}}

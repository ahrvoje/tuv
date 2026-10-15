"""Effective uv index resolution, index credentials, and version fetching."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

import tuv
from conftest import make_context

CONTEXT = make_context("venv", "idx", version="3.12.1")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in [
        "UV_NO_INDEX", "PIP_NO_INDEX", "UV_FIND_LINKS", "PIP_FIND_LINKS", "UV_INDEX", "UV_EXTRA_INDEX_URL",
        "PIP_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "PIP_INDEX_URL", "UV_INDEX_STRATEGY",
        "UV_CONFIG_FILE", "UV_NO_CONFIG", "NETRC", "APPDATA", "XDG_CONFIG_HOME", "PROGRAMDATA", "XDG_CONFIG_DIRS",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "programdata"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "xdgdirs"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def urls(config):
    return [(entry.name, entry.url) for entry in config.entries]


needs_tomllib = pytest.mark.skipif(tuv.tomllib is None, reason="uv configuration files need tomllib (Python 3.11+)")


def test_default_index_is_pypi(clean_env):
    config = tuv.effective_index_config("requests", CONTEXT)
    assert urls(config) == [("pypi", "https://pypi.org/simple/")]
    assert config.strategy == "first-index"


def test_environment_indexes_take_precedence(clean_env, monkeypatch):
    monkeypatch.setenv("UV_INDEX", "corp=https://corp.example/simple https://other.example/simple")
    monkeypatch.setenv("UV_DEFAULT_INDEX", "https://mirror.example/simple")
    monkeypatch.setenv("UV_INDEX_STRATEGY", "unsafe-best-match")
    config = tuv.effective_index_config("requests", CONTEXT)
    assert urls(config) == [
        ("corp", "https://corp.example/simple"),
        (None, "https://other.example/simple"),
        (None, "https://mirror.example/simple"),
    ]
    assert config.strategy == "unsafe-best-match"


@needs_tomllib
def test_pyproject_indexes_sources_and_explicit(clean_env):
    (clean_env / "pyproject.toml").write_text(
        """
[project]
name = "demo"

[tool.uv]
index-strategy = "unsafe-first-match"

[[tool.uv.index]]
name = "internal"
url = "https://internal.example/simple"
explicit = true

[[tool.uv.index]]
name = "mirror"
url = "https://mirror.example/simple"
default = true

[tool.uv.sources]
Private_Pkg = { index = "internal" }
marked = [{ index = "internal", marker = "python_version < '3.0'" }, { index = "mirror" }]
gitpkg = { git = "https://example/repo" }
""",
        encoding="utf-8",
    )
    config = tuv.effective_index_config("requests", CONTEXT)
    assert urls(config) == [("mirror", "https://mirror.example/simple")]
    assert config.strategy == "unsafe-first-match"
    pinned = tuv.effective_index_config("private-pkg", CONTEXT)
    assert urls(pinned) == [("internal", "https://internal.example/simple")]
    marked = tuv.effective_index_config("marked", CONTEXT)
    assert urls(marked) == [("mirror", "https://mirror.example/simple")]
    with pytest.raises(RuntimeError):
        tuv.effective_index_config("gitpkg", CONTEXT)


@needs_tomllib
def test_uv_toml_and_pip_settings(clean_env, monkeypatch):
    (clean_env / "uv.toml").write_text(
        """
[pip]
index-url = "https://pipidx.example/simple"
extra-index-url = ["https://extra.example/simple"]
""",
        encoding="utf-8",
    )
    config = tuv.effective_index_config("requests", CONTEXT)
    assert urls(config) == [(None, "https://extra.example/simple"), (None, "https://pipidx.example/simple")]
    monkeypatch.setenv("UV_NO_CONFIG", "1")
    assert urls(tuv.effective_index_config("requests", CONTEXT)) == [("pypi", "https://pypi.org/simple/")]
    monkeypatch.delenv("UV_NO_CONFIG")
    explicit = clean_env / "custom.toml"
    explicit.write_text('default-index = "https://custom.example/simple"\n', encoding="utf-8")
    monkeypatch.setenv("UV_CONFIG_FILE", str(explicit))
    assert urls(tuv.effective_index_config("requests", CONTEXT)) == [(None, "https://custom.example/simple")]
    monkeypatch.setenv("UV_CONFIG_FILE", str(clean_env / "missing.toml"))
    with pytest.raises(RuntimeError):
        tuv.effective_index_config("requests", CONTEXT)


@needs_tomllib
def test_user_config_is_used_after_project(clean_env):
    user = Path(tuv.user_uv_config_path())
    user.parent.mkdir(parents=True)
    user.write_text('[[index]]\nname = "user"\nurl = "https://user.example/simple"\n', encoding="utf-8")
    assert urls(tuv.effective_index_config("requests", CONTEXT)) == [
        ("user", "https://user.example/simple"),
        ("pypi", "https://pypi.org/simple/"),
    ]


@pytest.mark.parametrize(
    "env, value",
    [("UV_NO_INDEX", "1"), ("UV_FIND_LINKS", "/wheels"), ("UV_INDEX_STRATEGY", "bogus")],
)
def test_unsupported_configuration_raises(clean_env, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(RuntimeError):
        tuv.effective_index_config("requests", CONTEXT)


@needs_tomllib
def test_config_no_index_find_links_exclude_newer_raise(clean_env):
    for body in ['no-index = true\n', 'find-links = ["/w"]\n', 'exclude-newer = "2024-01-01"\n']:
        (clean_env / "uv.toml").write_text(body, encoding="utf-8")
        with pytest.raises(RuntimeError):
            tuv.effective_index_config("requests", CONTEXT)


def test_index_authorization_sources(clean_env, monkeypatch, tmp_path):
    entry = tuv.IndexEntry("corp", "https://u%40x:p%3Aw@corp.example/simple/")
    url, auth = tuv.index_authorization(entry, CONTEXT)
    assert url == "https://corp.example/simple/"
    assert auth == "Basic " + __import__("base64").b64encode(b"u@x:p:w").decode()
    monkeypatch.setenv("UV_INDEX_CORP_USERNAME", "env")
    monkeypatch.setenv("UV_INDEX_CORP_PASSWORD", "secret")
    _, auth = tuv.index_authorization(entry, CONTEXT)
    assert auth == "Basic " + __import__("base64").b64encode(b"env:secret").decode()
    never = tuv.IndexEntry("corp", "https://corp.example/simple/", authenticate="never")
    with pytest.raises(RuntimeError):
        tuv.index_authorization(never, CONTEXT)
    monkeypatch.delenv("UV_INDEX_CORP_USERNAME")
    monkeypatch.delenv("UV_INDEX_CORP_PASSWORD")
    assert tuv.index_authorization(never, CONTEXT) == ("https://corp.example/simple/", None)
    netrc = tmp_path / "netrc"
    netrc.write_text("machine corp.example login nu password npw\n", encoding="utf-8")
    monkeypatch.setenv("NETRC", str(netrc))
    plain = tuv.IndexEntry("corp", "https://corp.example/simple/")
    _, auth = tuv.index_authorization(plain, CONTEXT)
    assert auth == "Basic " + __import__("base64").b64encode(b"nu:npw").decode()
    netrc.write_text("", encoding="utf-8")
    always = tuv.IndexEntry("corp", "https://corp.example/simple/", authenticate="always")
    monkeypatch.setattr(tuv, "run_command", lambda args, timeout=None: __import__("subprocess").CompletedProcess(args, 1, "", ""))
    tuv._INDEX_AUTH_CACHE.clear()
    with pytest.raises(RuntimeError):
        tuv.index_authorization(always, CONTEXT)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type: str):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


@pytest.fixture
def fake_http(monkeypatch):
    routes: dict[str, object] = {}
    requests: list[tuple[str, dict[str, str]]] = []

    def urlopen(request, timeout=None):
        requests.append((request.full_url, dict(request.headers)))
        response = routes.get(request.full_url)
        if response is None:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        if isinstance(response, Exception):
            raise response
        body, content_type = response
        return FakeResponse(body, content_type)

    monkeypatch.setattr(tuv.urllib.request, "urlopen", urlopen)
    tuv._VERSION_LOOKUP_CACHE.clear()
    tuv._INDEX_AUTH_CACHE.clear()
    return routes, requests


def simple_json(*files):
    return json.dumps({"files": [dict(f) for f in files]}).encode(), "application/vnd.pypi.simple.v1+json"


def test_fetch_versions_first_index_strategy(clean_env, monkeypatch, fake_http):
    routes, requests = fake_http
    monkeypatch.setenv("UV_INDEX", "a=https://a.example/simple b=https://b.example/simple")
    routes["https://b.example/simple/pkg/"] = simple_json({"filename": "pkg-1.0.tar.gz"}, {"filename": "pkg-2.0.tar.gz", "yanked": True})
    routes["https://pypi.org/simple/pkg/"] = simple_json({"filename": "pkg-3.0.tar.gz"})
    versions, yanked = tuv.fetch_available_versions("pkg", CONTEXT)
    assert versions == ["1.0", "2.0"] and yanked == {"2.0"}
    assert [u for u, _ in requests] == ["https://a.example/simple/pkg/", "https://b.example/simple/pkg/"]
    assert requests[0][1]["Accept"].startswith("application/vnd.pypi.simple.v1+json")
    requests.clear()
    assert tuv.fetch_available_versions("pkg", CONTEXT) == (["1.0", "2.0"], {"2.0"})
    assert requests == []


def test_fetch_versions_best_match_merges_and_html(clean_env, monkeypatch, fake_http):
    routes, requests = fake_http
    monkeypatch.setenv("UV_INDEX", "a=https://a.example/simple")
    monkeypatch.setenv("UV_INDEX_STRATEGY", "unsafe-best-match")
    routes["https://a.example/simple/pkg/"] = (b'<a href="pkg-1.0.tar.gz">x</a><a href="pkg-2.0.tar.gz" data-yanked>y</a>', "text/html")
    routes["https://pypi.org/simple/pkg/"] = simple_json({"filename": "pkg-2.0.tar.gz"}, {"filename": "pkg-3.0.tar.gz"})
    versions, yanked = tuv.fetch_available_versions("pkg", CONTEXT)
    assert versions == ["1.0", "2.0", "3.0"]
    assert yanked == set()


def test_fetch_versions_errors_are_redacted_and_not_found(clean_env, monkeypatch, fake_http):
    routes, requests = fake_http
    monkeypatch.setenv("UV_INDEX", "a=https://user:tok@a.example/simple?token=qsecret")
    routes["https://a.example/simple/pkg/?token=qsecret"] = urllib.error.URLError("boom qsecret")
    with pytest.raises(RuntimeError) as excinfo:
        tuv.fetch_available_versions("pkg", CONTEXT)
    assert [url for url, _ in requests] == ["https://a.example/simple/pkg/?token=qsecret"]
    assert "qsecret" not in str(excinfo.value) and "tok" not in str(excinfo.value)
    assert "a.example" in str(excinfo.value) and "boom" in str(excinfo.value)
    routes.clear()
    with pytest.raises(RuntimeError) as excinfo:
        tuv.fetch_available_versions("missing", CONTEXT)
    assert "not found" in str(excinfo.value)


def test_fetch_versions_retries_auth_challenge_with_store(clean_env, monkeypatch, fake_http):
    routes, requests = fake_http
    monkeypatch.setenv("UV_INDEX", "corp=https://corp.example/simple")
    calls: list[list[str]] = []

    def run_command(args, timeout=None):
        calls.append(list(args))
        return __import__("subprocess").CompletedProcess(args, 0, "storetoken\n", "")

    monkeypatch.setattr(tuv, "run_command", run_command)
    state = {"n": 0}

    def urlopen(request, timeout=None):
        state["n"] += 1
        requests.append((request.full_url, dict(request.headers)))
        if "Authorization" not in request.headers:
            raise urllib.error.HTTPError(request.full_url, 401, "auth", {}, None)
        return FakeResponse(*simple_json({"filename": "pkg-1.0.tar.gz"}))

    monkeypatch.setattr(tuv.urllib.request, "urlopen", urlopen)
    versions, _ = tuv.fetch_available_versions("pkg", CONTEXT)
    assert versions == ["1.0"]
    assert state["n"] == 2
    assert calls and calls[0][-3:] == ["auth", "token", "corp.example"]

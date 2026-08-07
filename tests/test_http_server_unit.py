import io
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import cherrypy
import pytest
from cherrypy.lib import static as cherrypy_static

from repeater.web import http_server as hs


def test_log_buffer_emit_collects_messages():
    buf = hs.LogBuffer(max_lines=2)
    rec1 = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    rec2 = logging.LogRecord("x", logging.ERROR, __file__, 2, "boom", (), None)
    rec3 = logging.LogRecord("x", logging.WARNING, __file__, 3, "warn", (), None)

    buf.emit(rec1)
    buf.emit(rec2)
    buf.emit(rec3)

    assert len(buf.logs) == 2
    assert buf.logs[-1]["level"] == "WARNING"
    assert "warn" in buf.logs[-1]["message"]


def test_log_buffer_emit_redacts_sensitive_values():
    buf = hs.LogBuffer(max_lines=5)
    rec = logging.LogRecord(
        "auth",
        logging.DEBUG,
        __file__,
        10,
        "auth password=secret123 token=abc123 Authorization: Bearer deadbeef",
        (),
        None,
    )

    buf.emit(rec)

    assert len(buf.logs) == 1
    entry = buf.logs[0]
    assert "secret123" not in entry["message"]
    assert "abc123" not in entry["message"]
    assert "deadbeef" not in entry["message"]
    assert "[REDACTED]" in entry["message"]
    assert "raw_message" not in entry


def test_log_buffer_emit_includes_exception_text_without_crashing():
    buf = hs.LogBuffer(max_lines=5)
    try:
        raise RuntimeError("boom password=secret123")
    except RuntimeError:
        rec = logging.LogRecord(
            "x",
            logging.ERROR,
            __file__,
            20,
            "failure while sending advert",
            (),
            sys.exc_info(),
        )

    buf.emit(rec)

    assert len(buf.logs) == 1
    assert "exception" in buf.logs[0]
    assert "RuntimeError" in buf.logs[0]["exception"]
    assert "secret123" not in buf.logs[0]["exception"]


def test_doc_endpoint_routes_and_openapi_json_paths(monkeypatch):
    api = SimpleNamespace(docs=lambda: "docs-html")
    doc = hs.DocEndpoint(api)

    assert doc.index() == "docs-html"
    assert doc.docs() == "docs-html"

    monkeypatch.setattr(
        cherrypy, "response", SimpleNamespace(headers={}, status=200), raising=False
    )

    # success path
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO("openapi: 3.0.0\n"))
    out = doc.openapi_json()
    assert cherrypy.response.headers["Content-Type"] == "application/json"
    assert b"openapi" in out

    # not found
    def _missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("builtins.open", _missing)
    out = doc.openapi_json()
    assert cherrypy.response.status == 404
    assert b"not found" in out

    # generic error
    def _err(*_args, **_kwargs):
        raise RuntimeError("bad")

    monkeypatch.setattr("builtins.open", _err)
    out = doc.openapi_json()
    assert cherrypy.response.status == 500
    assert b"Error loading OpenAPI spec" in out


def test_stats_app_index_and_default_routing(monkeypatch, tmp_path):
    index_html = tmp_path / "index.html"
    index_html.write_text("<html>ok</html>", encoding="utf-8")

    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)

    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})

    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="GET"), raising=False)
    response = SimpleNamespace(headers={})
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    assert app.index() == "<html>ok</html>"
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"

    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="OPTIONS"), raising=False)
    assert app.default("anything") == ""

    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(method="GET"), raising=False)
    with pytest.raises(cherrypy.NotFound):
        app.default("api")

    assert app.default("ws", "packets") == ""
    assert app.default("route") == "<html>ok</html>"


def _static_test_app(monkeypatch, tmp_path, accept_encoding="", response_headers=None):
    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)
    monkeypatch.setattr(
        cherrypy,
        "request",
        SimpleNamespace(
            method="GET",
            headers={"Accept-Encoding": accept_encoding} if accept_encoding else {},
        ),
        raising=False,
    )
    response = SimpleNamespace(headers=dict(response_headers or {}))
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})
    return app, response


def _capture_static_serve(monkeypatch):
    served = []

    def serve_file(path, content_type=None):
        served.append((Path(path), content_type))
        return b"streamed"

    monkeypatch.setattr(cherrypy_static, "serve_file", serve_file)
    return served


def test_stats_app_negotiates_precompressed_hashed_assets(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.js"
    logical_asset.write_bytes(b"original")
    Path(f"{logical_asset}.br").write_bytes(b"brotli")
    Path(f"{logical_asset}.gz").write_bytes(b"gzip")

    app, response = _static_test_app(
        monkeypatch,
        tmp_path,
        accept_encoding="gzip;q=0.5, br;q=1",
        response_headers={"Vary": "Origin"},
    )
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", logical_asset.name) == b"streamed"
    assert served == [(Path(f"{logical_asset}.br"), "text/javascript")]
    assert response.headers["Content-Encoding"] == "br"
    assert response.headers["Vary"] == "Origin, Accept-Encoding"
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_stats_app_serves_gzip_when_brotli_is_not_accepted(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.css"
    logical_asset.write_bytes(b"original")
    Path(f"{logical_asset}.br").write_bytes(b"brotli")
    gzip_asset = Path(f"{logical_asset}.gz")
    gzip_asset.write_bytes(b"gzip")

    app, response = _static_test_app(
        monkeypatch,
        tmp_path,
        accept_encoding="br;q=0, gzip",
    )
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", logical_asset.name) == b"streamed"
    assert served == [(gzip_asset, "text/css")]
    assert response.headers["Content-Encoding"] == "gzip"


def test_stats_app_preserves_byte_range_serving(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.js"
    logical_asset.write_bytes(b"0123456789")

    app, _ = _static_test_app(monkeypatch, tmp_path)

    original_request = cherrypy.serving.request
    original_response = cherrypy.serving.response
    request = cherrypy._cprequest.Request(
        original_request.local,
        original_request.remote,
        server_protocol="HTTP/1.1",
    )
    request.headers["Range"] = "bytes=2-5"
    response = cherrypy._cprequest.Response()
    cherrypy.serving.load(request, response)
    try:
        body = app._serve_static_file(str(assets), (logical_asset.name,))
        assert b"".join(body) == b"2345"
        assert response.status == "206 Partial Content"
        assert response.headers["Content-Range"] == "bytes 2-5/10"
        assert response.headers["Accept-Ranges"] == "bytes"
    finally:
        cherrypy.serving.load(original_request, original_response)


def test_stats_app_uses_identity_when_encodings_are_rejected(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.js"
    logical_asset.write_bytes(b"original")
    Path(f"{logical_asset}.br").write_bytes(b"brotli")
    Path(f"{logical_asset}.gz").write_bytes(b"gzip")

    app, response = _static_test_app(
        monkeypatch,
        tmp_path,
        accept_encoding="br;q=0, gzip;q=0",
    )
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", logical_asset.name) == b"streamed"
    assert served == [(logical_asset, "text/javascript")]
    assert "Content-Encoding" not in response.headers
    assert response.headers["Vary"] == "Accept-Encoding"


def test_stats_app_does_not_negotiate_an_explicit_sidecar_path(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    requested_asset = assets / "archive.gz"
    requested_asset.write_bytes(b"direct")
    Path(f"{requested_asset}.br").write_bytes(b"unrelated")

    app, response = _static_test_app(monkeypatch, tmp_path, accept_encoding="br")
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", requested_asset.name) == b"streamed"
    assert served == [(requested_asset, "application/octet-stream")]
    assert "Content-Encoding" not in response.headers


def test_stats_app_ignores_sidecar_symlinks_outside_static_root(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    logical_asset = assets / "app-AbCd1234.js"
    logical_asset.write_bytes(b"original")
    outside_sidecar = tmp_path / "outside.br"
    outside_sidecar.write_bytes(b"outside")
    Path(f"{logical_asset}.br").symlink_to(outside_sidecar)

    app, response = _static_test_app(monkeypatch, tmp_path, accept_encoding="br")
    served = _capture_static_serve(monkeypatch)

    assert app.default("assets", logical_asset.name) == b"streamed"
    assert served == [(logical_asset, "text/javascript")]
    assert "Content-Encoding" not in response.headers


def test_stats_app_rejects_static_traversal_into_prefix_sibling(monkeypatch, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    sibling = tmp_path / "assets-secret"
    sibling.mkdir()
    (sibling / "leak.js").write_bytes(b"secret")

    app, _ = _static_test_app(monkeypatch, tmp_path)

    with pytest.raises(cherrypy.NotFound):
        app._serve_static_file(str(assets), ("..", "assets-secret", "leak.js"))


def test_stats_app_exposes_compiled_ui_favicon(monkeypatch, tmp_path):
    favicon = b"compiled-ui-favicon"
    favicon_path = tmp_path / "favicon.ico"
    favicon_path.write_bytes(favicon)

    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)
    response = SimpleNamespace(headers={})
    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(headers={}), raising=False)
    monkeypatch.setattr(cherrypy, "response", response, raising=False)
    served = []

    def serve_file(path, content_type=None):
        served.append((Path(path), content_type))
        return Path(path).read_bytes()

    monkeypatch.setattr(cherrypy_static, "serve_file", serve_file)

    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})

    assert app.favicon_ico() == favicon
    assert served == [(favicon_path, "image/x-icon")]
    assert response.headers["Vary"] == "Accept-Encoding"


def test_stats_app_index_error_paths(monkeypatch, tmp_path):
    fake_api = SimpleNamespace(config_manager=object(), docs=lambda: "d")
    monkeypatch.setattr(hs, "APIEndpoints", lambda *args, **kwargs: fake_api)

    app = hs.StatsApp(config={"web": {"web_path": str(tmp_path)}})

    with pytest.raises(cherrypy.HTTPError):
        app.index()

    # Force generic open() exception branch
    def _explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("builtins.open", _explode)
    (tmp_path / "index.html").write_text("ignored", encoding="utf-8")
    with pytest.raises(cherrypy.HTTPError):
        app.index()


def test_http_server_utility_methods(monkeypatch, tmp_path):
    def _fake_init_auth(self):
        self.jwt_handler = object()
        self.token_manager = object()

    monkeypatch.setattr(hs.HTTPStatsServer, "_init_auth_handlers", _fake_init_auth)
    monkeypatch.setattr(
        hs,
        "StatsApp",
        lambda *args, **kwargs: SimpleNamespace(api=SimpleNamespace(config_manager=object())),
    )
    monkeypatch.setattr(hs, "AuthEndpoints", lambda *args, **kwargs: object())
    monkeypatch.setattr(hs, "DocEndpoint", lambda *_args, **_kwargs: object())

    server = hs.HTTPStatsServer(
        config={"web": {"cors_enabled": False}}, config_path=str(Path(tmp_path) / "cfg.yml")
    )

    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}), raising=False)
    out = server._json_error_handler(401, "no", "", "")
    assert '"success": false' in out

    install_called = {"v": False}
    monkeypatch.setattr(hs.cherrypy_cors, "install", lambda: install_called.__setitem__("v", True))
    server._setup_server_cors()
    assert install_called["v"] is True

    exited = {"v": False}
    monkeypatch.setattr(
        cherrypy,
        "engine",
        SimpleNamespace(exit=lambda: exited.__setitem__("v", True)),
        raising=False,
    )
    server.stop()
    assert exited["v"] is True

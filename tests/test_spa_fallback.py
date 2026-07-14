"""SPA deep-link fallback for production static hosting."""

from pathlib import Path

from fastapi.testclient import TestClient


def _make_static_dir(tmp_path: Path) -> Path:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><html><body>spa-root</body></html>",
        encoding="utf-8",
    )
    assets = static_dir / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")
    return static_dir


def test_spa_deep_link_serves_index_html(tmp_path):
    from app.main import create_app

    static_dir = _make_static_dir(tmp_path)
    app = create_app(static_dir=str(static_dir))
    client = TestClient(app)

    for path in ("/chat", "/knowledge", "/admin/projects", "/login"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "spa-root" in response.text, path
        assert "text/html" in response.headers.get("content-type", "")


def test_spa_fallback_does_not_swallow_api_or_docs(tmp_path):
    from app.main import create_app

    static_dir = _make_static_dir(tmp_path)
    app = create_app(static_dir=str(static_dir))
    client = TestClient(app)

    # OpenAPI/docs are real FastAPI routes, not SPA pages.
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "spa-root" not in docs.text

    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    assert openapi.headers.get("content-type", "").startswith("application/json")

    # /api/* should not fall back to index.html (404 or real API error envelope).
    api_missing = client.get("/api/does-not-exist")
    assert api_missing.status_code != 200 or "spa-root" not in api_missing.text
    assert "spa-root" not in (api_missing.text or "")


def test_root_still_serves_index_when_present(tmp_path):
    from app.main import create_app

    static_dir = _make_static_dir(tmp_path)
    app = create_app(static_dir=str(static_dir))
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    assert "spa-root" in response.text

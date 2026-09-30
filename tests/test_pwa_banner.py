"""PWA / 静的フロントエンドの検証。

旧 `tests/test_pwa_banner.py` は Streamlit の `render_pwa_install_banner()`
の存在を検査していた（Streamlit 廃止で対象が消えた）。
A6 が `static/` に実装した PWA 資産に対して、配信経路と内容 Commitments を検証する。
"""

import json
import re
from pathlib import Path

import pytest

from retro_radio.server import STATIC_DIR


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


def _read(name: str) -> str:
    path = STATIC / name
    assert path.exists(), f"static/{name} が存在しない"
    return path.read_text(encoding="utf-8")


# --- 資産の存在 ----------------------------------------------------------------
def test_static_dir_configured():
    assert STATIC_DIR.exists()
    assert STATIC_DIR == STATIC


@pytest.mark.parametrize(
    "name", ["index.html", "app.js", "app.css", "manifest.json", "service-worker.js"]
)
def test_static_assets_exist(name):
    assert (STATIC / name).is_file(), f"static/{name} が無い"


def test_index_references_app_js():
    """index.html が /static/app.js を参照している"""
    html = _read("index.html")
    assert "/static/app.js" in html or 'src="app.js"' in html


def test_index_references_app_css():
    assert "/static/app.css" in _read("index.html")


def test_index_references_manifest():
    assert "manifest.json" in _read("index.html")


# --- 配信経路 ------------------------------------------------------------------
def test_static_assets_are_served(client):
    for name in ("app.js", "app.css", "manifest.json", "service-worker.js", "index.html"):
        res = client.get(f"/static/{name}")
        assert res.status_code == 200, f"/static/{name} -> {res.status_code}"


def test_index_is_served_at_root(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_unknown_static_asset_is_404(client):
    assert client.get("/static/definitely-missing.js").status_code == 404


# --- manifest ------------------------------------------------------------------
def test_manifest_is_valid_json():
    manifest = json.loads(_read("manifest.json"))
    for key in ("name", "start_url", "display", "icons"):
        assert key in manifest, f"manifest.json に {key} が無い"


def test_manifest_display_is_standalone():
    assert json.loads(_read("manifest.json"))["display"] in ("standalone", "fullscreen", "minimal-ui")


def test_manifest_declares_icons_with_masks():
    """(maskable アイコン在内 significant に) アイコンが宣言されている"""
    manifest = json.loads(_read("manifest.json"))
    assert manifest["icons"]
    for icon in manifest["icons"]:
        assert icon["src"]
        assert icon["sizes"]
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"]), (
        "maskable アイコンが無い（Android の-adaptive アイコン対応が壊れる）"
    )


def test_manifest_start_url_is_root():
    assert json.loads(_read("manifest.json"))["start_url"] in ("/", "/static/index.html")


def test_manifest_has_theme_and_background_colors():
    manifest = json.loads(_read("manifest.json"))
    for key in ("theme_color", "background_color"):
        assert re.fullmatch(r"#[0-9a-fA-F]{3,8}", manifest[key])


# --- Service Worker ------------------------------------------------------------
def test_service_worker_only_caches_get_requests():
    """旧実装は POST にも caches.put して例外を投げ 있었다"""
    sw = _read("service-worker.js")
    assert re.search(r"request\.method\s*!==?\s*['\"]GET['\"]", sw), (
        "非 GET リクエストを早期 return するガードが無い"
    )


def test_service_worker_is_registered_by_app_js():
    """Service Worker の登録は app.js が行う（index.html ではない）"""
    app_js = _read("app.js")
    assert "navigator.serviceWorker.register" in app_js
    assert "registerServiceWorker()" in app_js


def test_service_worker_registration_in_app_js():
    app_js = _read("app.js")
    assert "serviceWorker" in app_js


# --- app.js の契約 -------------------------------------------------------------
def test_app_js_normalizes_playlist_item_type():
    """playlist[].type は小文字が正（models/radio.py の PlaylistItemType）"""
    app_js = _read("app.js")
    assert re.search(r"toUpperCase\(\)", app_js), "type の大文字小文字の正規化が無い"


def test_app_js_calls_generate_endpoint():
    assert "/api/generate" in _read("app.js")


def test_app_js_reads_audio_endpoint():
    assert "/api/audio/" in _read("app.js") or "audio_url" in _read("app.js")


def test_app_js_has_no_streamlit_placeholder():
    assert "streamlit" not in _read("app.js").lower()


def test_no_removed_ui_modules_referenced():
    """削除済み retro_radio.ui / retro_radio.i18n への参照が復活していないこと"""
    import retro_radio

    for name in ("ui", "i18n"):
        assert not hasattr(retro_radio, name), f"retro_radio.{name} が復活している"

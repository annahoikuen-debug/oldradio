"""環境設定テンプレートのテスト。

`.env.example` は「コピーしてすぐ起動できる」状態であるべき。
A8 が `config.py` の全フィールドと1対1で対応させたため、
ここでは **Settings の全フィールドが .env.example に存在すること** を検証する
（従来は7項目の部分一致のみだった）。

`RETRO_RADIO_CORS_ORIGINS` は pydantic-settings が JSON として解析するため、
`*` やカンマ区切りだと `SettingsError` で起動に失敗する。
"""

import re
import subprocess
from pathlib import Path


from retro_radio.config import Settings


ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"


def _env_example_text() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def _env_example_keys() -> set:
    keys = set()
    for line in _env_example_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match:
            keys.add(match.group(1))
    return keys


def test_env_example_exists():
    assert ENV_EXAMPLE.exists()


def test_gittemplate_exists():
    assert (ROOT / ".gittemplate").exists()


def test_env_example_is_utf8():
    """ロケール(cp932).Employee なく読めること"""
    content = _env_example_text()
    assert content
    assert "�" not in content, "文字化けが残っている"


def test_env_example_covers_all_settings_fields():
    """config.py の全フィールドに対応するキーが .env.example にある"""
    keys = _env_example_keys()
    missing = sorted(
        f"RETRO_RADIO_{name.upper()}" for name in Settings.model_fields if f"RETRO_RADIO_{name.upper()}" not in keys
    )
    assert not missing, f".env.example に設定項目が欠けている: {missing}"


def test_env_example_has_no_unknown_keys():
    """config.py に存在しない設定が混ざっていないこと"""
    valid = {f"RETRO_RADIO_{name.upper()}" for name in Settings.model_fields}
    unknown = sorted(key for key in _env_example_keys() if key.startswith("RETRO_RADIO_") and key not in valid)
    assert not unknown, f"未知の RETRO_RADIO_ 設定がある: {unknown}"


def test_env_example_gemini_model_is_not_retired():
    """既定モデルが退役モデルになっていないこと"""
    match = re.search(r"^RETRO_RADIO_GEMINI_MODEL=(.+)$", _env_example_text(), re.M)
    assert match, "GEMINI_MODEL の記載が無い"
    assert match.group(1).strip() == Settings.model_fields["gemini_model"].default


def test_env_example_cors_origins_is_json_array():
    """CORS 許可オリジンは JSON 配列形式（* やカンマ区切りだと起動時に SettingsError）"""
    match = re.search(r"^RETRO_RADIO_CORS_ORIGINS=(.+)$", _env_example_text(), re.M)
    assert match, "CORS_ORIGINS の記載が無い"
    value = match.group(1).strip().strip('"').strip("'")
    assert value.startswith("["), f"JSON 配列形式でない: {value!r}"
    assert "*" not in value

    import json

    parsed = json.loads(value)
    assert parsed and all(o.startswith("http") for o in parsed)


def test_env_example_contains_no_real_secrets():
    """実キーが混入していないこと"""
    content = _env_example_text()
    assert "AIza" not in content, "Gemini の実キーが混入している"
    assert "sk_live" not in content
    assert "whsec_" not in content


def test_env_example_default_year_matches_config():
    match = re.search(r"^RETRO_RADIO_DEFAULT_YEAR=(\d+)$", _env_example_text(), re.M)
    assert match
    assert int(match.group(1)) == Settings.model_fields["default_year"].default


def test_env_example_required_keys_present():
    content = _env_example_text()
    for key in (
        "RETRO_RADIO_CACHE_TTL",
        "RETRO_RADIO_MAX_RETRIES",
        "RETRO_RADIO_GEMINI_MODEL",
        "RETRO_RADIO_TTS_LANGUAGE",
        "RETRO_RADIO_DEFAULT_YEAR",
        "RETRO_RADIO_MIN_YEAR",
        "RETRO_RADIO_MAX_YEAR",
        "RETRO_RADIO_DATABASE_URL",
    ):
        assert key in content, f".env.example に {key} が無い"


def test_env_example_omits_streamlit_settings():
    """Streamlit 用の設定が混ざっていないこと"""
    content = _env_example_text().lower()
    assert "streamlit" not in content


def test_no_dotenv_committed():
    """実際の `.env`（平文シークレットを含む）は **git に載せない**。

    開発マシンには `.env` が存在してよい。`.gitignore` にも載っており、
    ローカルに実ファイルがあること自体は問題ない。守りたいのは
    「追跡されている（= コミットされ得る）」ことだけ。
    """
    dotenv = ROOT / ".env"
    if not dotenv.exists():
        return
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode != 0, (
        ".env が git に追跡されています（実 API キーが流出しています）"
    )

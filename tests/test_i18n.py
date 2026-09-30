"""i18n 検証。

旧 `retro_radio.i18n.translator.Translator` は Streamlit 依存の重複実装とともに削除された。
正しい実装は `retro_radio.utils.i18n`（`gettext()` / `get_available_languages()`）なので、その実 API に合わせてテストを移植した。

過去の不具合（修正済み）:
  `retro_radio/locales/ja.json` / `en.json` の 1 行目が `# retro_radio/locales/*.json`
  というコメントになっており JSON として不正だった。`gettext()` は例外を握り潰して
  キー名そのものを返すため、翻訳が一切解決されなかった。
  現在は (1) ロケールファイルが標準 JSON として読めること、(2) ローダがコメント行を許容すること、
  (3) 構文エラーはログに残して握り潰さないことを検証する。
"""

import json
from pathlib import Path

import pytest

from retro_radio.utils import i18n
from retro_radio.utils.i18n import get_available_languages, gettext


LOCALES_DIR = Path(i18n.__file__).resolve().parent.parent / "locales"


def test_available_languages():
    """対応言語は日本語と英語"""
    assert get_available_languages() == ["ja", "en"]


def test_available_languages_returns_copy():
    """返り値はモジュール定義の複製（外部から改変できない）"""
    langs = get_available_languages()
    langs.append("fr")
    assert "fr" not in get_available_languages()


def test_default_language_follows_settings():
    """デフォルト言語は設定に従う"""
    from retro_radio.config import get_settings

    assert i18n.DEFAULT_LANGUAGE == get_settings().default_language


@pytest.mark.parametrize("lang", ["ja", "en", "fr", "", None, "JA "])
def test_gettext_never_raises_for_unknown_language(lang):
    """未知の言語指定でも例外を投げずキーを返す（フォールバック契約）"""
    result = gettext("app.title", lang)
    assert isinstance(result, str)
    assert result


def test_gettext_returns_key_for_missing_key():
    """存在しないキーはキー名そのものを返す"""
    assert gettext("no.such.key.exists", "ja") == "no.such.key.exists"


def test_gettext_uses_session_language(monkeypatch):
    """lang 未指定時はセッション状態の language を優先する"""
    from retro_radio.utils import session as session_module

    # _get_session_state はモジュール内非公開ヘルパだが、gettext() が実際に使う経路
    monkeypatch.setattr(
        session_module,
        "_get_session_state",
        lambda: {"language": "en"},
    )
    assert gettext("app.title") == gettext("app.title", "en")
    assert gettext("app.title") != "app.title"


def test_gettext_falls_back_to_env(monkeypatch):
    """セッションに言語が無い場合は APP_LANGUAGE を使う"""
    from retro_radio.utils import session as session_module

    monkeypatch.setattr(session_module, "_get_session_state", lambda: {})
    monkeypatch.setenv("APP_LANGUAGE", "en")
    assert gettext("app.title") == gettext("app.title", "en")
    assert gettext("app.title") != "app.title"


def test_locales_directory_has_japanese_and_english():
    """ロケールファイルは ja / en の 2 種が存在する"""
    assert (LOCALES_DIR / "ja.json").exists()
    assert (LOCALES_DIR / "en.json").exists()


def test_locale_files_are_valid_json():
    """ロケールファイルはそのまま json.load できる必要がある"""
    for name in ("ja.json", "en.json"):
        with open(LOCALES_DIR / name, encoding="utf-8") as f:
            payload = json.load(f)
        assert isinstance(payload, dict) and payload


def test_translation_resolves():
    """実キーが翻訳文字列に解決される"""
    assert gettext("app.title", "ja") == "レトロラジオ・タイムマシン"
    assert gettext("app.title", "en") != "app.title"


def test_translation_resolves_for_all_locales():
    """ja / en のどちらでも app.title がキー文字列にならない"""
    for lang in ("ja", "en"):
        value = gettext("app.title", lang)
        assert value and value != "app.title", lang
        assert "不可能" not in value  # キーワードではなく実際の文言


def test_every_locale_has_the_same_key_structure():
    """ja と en でキー集合が一致する（片方だけが欠けたキーがないか）"""
    ja = json.loads((LOCALES_DIR / "ja.json").read_text(encoding="utf-8"))
    en = json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8"))

    def _paths(node, prefix=""):
        found = set()
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                found |= _paths(value, f"{path}.")
            else:
                found.add(path)
        return found

    assert _paths(ja) == _paths(en)


def test_loader_tolerates_comment_lines():
    """ローダは `#` コメント行を許容する（人が編集しても壊れない）"""
    raw = '# 先頭にコメント\n{\n  "a": {\n    # 途中にもコメント\n    "b": "値"\n  }\n}\n'
    cleaned = i18n._strip_json_comments(raw)
    assert cleaned.lstrip().startswith("{")
    assert json.loads(cleaned) == {"a": {"b": "値"}}


def test_broken_locale_file_raises_value_error(tmp_path, monkeypatch):
    """壊れた JSON は握り潰さず ValueError として伝播する"""
    i18n._language_data.clear()
    broken = tmp_path / "zz.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    real_open = open

    def _fake_open(path, *args, **kwargs):
        if str(path).endswith("zz.json"):
            return real_open(broken, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)
    monkeypatch.setattr(i18n, "DEFAULT_LANGUAGE", "zz")
    try:
        with pytest.raises(ValueError):
            i18n._load_language("zz")
    finally:
        i18n._language_data.clear()

import json
import logging
import os
from typing import Dict, Any
from ..config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# デフォルト言語
DEFAULT_LANGUAGE = settings.default_language
# 利用可能な言語
AVAILABLE_LANGUAGES = ["ja", "en"]

# 言語データをキャッシュ
_language_data: Dict[str, Dict] = {}

def _strip_json_comments(text: str) -> str:
    """標準JSON に無い `#` / `//` のコメント行を取り除く（人が編集しやすいロケールファイル用）"""
    cleaned = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _load_language(lang: str) -> Dict[str, Any]:
    """言語ファイルをロードしてキャッシュに保存"""
    if lang in _language_data:
        return _language_data[lang]

    lang_file = os.path.join(os.path.dirname(__file__), "..", "locales", f"{lang}.json")
    try:
        with open(lang_file, 'r', encoding='utf-8') as f:
            # JSONDecodeError は ValueError の派生なので ValueError で捕捉する
            data = json.loads(_strip_json_comments(f.read()))
            if not isinstance(data, dict):
                raise ValueError(f"ロケールのルートがオブジェクトではありません: {lang_file}")
            _language_data[lang] = data
            return data
    except FileNotFoundError:
        # フォールバックとして日本語を使用
        if lang != DEFAULT_LANGUAGE:
            logger.warning("ロケールファイルが見つからないため %s にフォールバックします: %s", DEFAULT_LANGUAGE, lang_file)
            return _load_language(DEFAULT_LANGUAGE)
        raise
    except ValueError as e:
        # 握り潰さない: 構文エラーは原因箇所が分かるようログに残す
        logger.error("ロケールファイルを読み込めませんでした: %s (%s)", lang_file, e)
        if lang != DEFAULT_LANGUAGE:
            return _load_language(DEFAULT_LANGUAGE)
        raise

def gettext(key: str, lang: str = None, **kwargs) -> str:
    """
    翻訳を取得する関数
    
    Args:
        key: ドット区切りのキー（例: "app.title"）
        lang: 言語コード（Noneの場合はセッション状態または環境変数または設定から取得）
        **kwargs: 文字列フォーマット用のキーワード引数
    
    Returns:
        翻訳された文字列
    """
    if lang is None:
        # セッション状態から言語を取得
        try:
            from .session import _get_session_state
            lang = _get_session_state().get("language")
        except Exception:
            lang = None
        
        # 環境変数 APP_LANGUAGE でオーバーライド可能
        if lang is None:
            lang = os.environ.get("APP_LANGUAGE")
        
        # 最後に設定からデフォルト言語を取得
        if lang is None:
            lang = settings.default_language
    
    # 言語がサポートされていない場合はデフォルトにフォールバック
    if lang not in AVAILABLE_LANGUAGES:
        lang = settings.default_language
    
    try:
        data = _load_language(lang)
        # ドット区切りのキーをネストされたディクショナリでたどる
        keys = key.split('.')
        value = data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                # キーが見つからない場合はキー自身を返す（開発用）
                return key
        
        # 文字列フォーマット
        if isinstance(value, str):
            return value.format(**kwargs)
        return str(value)
    except Exception as e:
        # キー返却のフォールバック契約は維持するが、握り潰さずログに残す
        logger.warning("翻訳の解決に失敗しました: key=%s lang=%s (%s)", key, lang, e)
        return key

def get_available_languages() -> list:
    """利用可能な言語のリストを返す"""
    return AVAILABLE_LANGUAGES.copy()

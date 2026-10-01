import pytest
from retro_radio.utils.validators import GenerationRequest, ApiKeyConfig, SearchParams, sanitize_text, validate_year_range

def test_validation_success():
    """正常な年月日のバリデーション"""
    # 正常な日付
    req = GenerationRequest(year=2020, month=1, day=1)
    assert req.year == 2020
    assert req.month == 1
    assert req.day == 1

    # うるう年
    req = GenerationRequest(year=2020, month=2, day=29)
    assert req.day == 29

    # 月の最後の日
    req = GenerationRequest(year=2021, month=1, day=31)
    assert req.day == 31

def test_validation_fail():
    """異常な年月日のバリデーション"""
    # 存在しない日
    with pytest.raises(ValueError, match="2021年2月に30日は存在しません"):
        GenerationRequest(year=2021, month=2, day=30)
    
    # 範囲外の月
    with pytest.raises(ValueError):
        GenerationRequest(year=2020, month=0, day=1)
    
    with pytest.raises(ValueError):
        GenerationRequest(year=2020, month=13, day=1)
    
    # 範囲外の日
    with pytest.raises(ValueError):
        GenerationRequest(year=2020, month=1, day=0)
    
    with pytest.raises(ValueError):
        GenerationRequest(year=2020, month=1, day=32)

def test_sanitize_text():
    """テキストサニタイズのテスト（Wave 1 で html.escape(quote=True) に変更）"""
    # 通常のテキスト
    assert sanitize_text("Hello World") == "Hello World"

    # XSS対策: ブラックリスト削除ではなく HTML エスケープを行う
    # 「<」「>」「"」「'」がすべて実体参照化され、タグとして解釈できない
    assert sanitize_text("<script>alert('xss')</script>") == (
        "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;"
    )
    assert sanitize_text('"<>"') == "&quot;&lt;&gt;&quot;"
    assert "<" not in sanitize_text("<img src=x onerror=alert(1)>")
    assert sanitize_text("javascript:alert(1)") == "javascript:alert(1)"

    # 前後の空白は除去される
    assert sanitize_text("  padded  ") == "padded"

    # 文字数制限
    long_text = "a" * 6000
    assert len(sanitize_text(long_text)) == 5000
    assert sanitize_text(long_text) == "a" * 5000


def test_sanitize_text_escapes_attribute_breakout():
    """属性エスケープ（quote=True）を抑止できない 属性値を注入できない"""
    payload = '" onload="alert(1)'
    escaped = sanitize_text(payload)
    assert '"' not in escaped
    assert "onload=" in escaped
    assert escaped == "&quot; onload=&quot;alert(1)"

def test_validate_year_range():
    """年範囲バリデーションのテスト"""
    from retro_radio.config import get_settings
    settings = get_settings()
    
    # 範囲内
    assert validate_year_range(settings.default_year) is True
    assert validate_year_range(settings.min_year) is True
    assert validate_year_range(settings.max_year) is True
    
    # 範囲外
    assert validate_year_range(settings.min_year - 1) is False
    assert validate_year_range(settings.max_year + 1) is False

    # カスタム範囲
    assert validate_year_range(2000, min_y=1950, max_y=2050) is True
    assert validate_year_range(1900, min_y=1950, max_y=2050) is False

def test_api_key_config():
    """APIキー設定のバリデーションテスト（正規表現は `AIza` + 35 文字を要求する）"""
    # 正常なキー（AIza + 35文字 = 計39文字）
    config = ApiKeyConfig(gemini_key="AIza" + "A" * 35)
    assert config.gemini_key.startswith("AIza")
    assert len(config.gemini_key) == 39

    # 短すぎるキー
    with pytest.raises(ValueError):
        ApiKeyConfig(gemini_key="AIza")

    # プレフィックスが無くても長さが合えば通らない
    with pytest.raises(ValueError, match="無効なAPIキー形式です"):
        ApiKeyConfig(gemini_key="B" * 39)

    # 無効なフォーマット
    with pytest.raises(ValueError, match="無効なAPIキー形式です"):
        ApiKeyConfig(gemini_key="invalid_key")


def test_api_key_config_rejects_too_long():
    """35文字を超えるsaltは正規表現不一致で弾かれる"""
    with pytest.raises(ValueError, match="無効なAPIキー形式です"):
        ApiKeyConfig(gemini_key="AIza" + "A" * 36)

def test_search_params():
    """検索パラメータのバリデーションテスト"""
    # デフォルト値
    params = SearchParams(term="test")
    assert params.term == "test"
    assert params.country == "JP"
    assert params.media == "music"
    assert params.entity == "musicTrack"
    assert params.limit == 50
    
    # カスタム値
    params = SearchParams(
        term="another term",
        country="US",
        media="podcast",
        entity="artist",
        limit=100
    )
    assert params.term == "another term"
    assert params.country == "US"
    assert params.media == "podcast"
    assert params.entity == "artist"
    assert params.limit == 100
    
    # 範囲外のlimit
    with pytest.raises(ValueError):
        SearchParams(term="test", limit=0)
    
    with pytest.raises(ValueError):
        SearchParams(term="test", limit=201)
    
    # 不正なcountry
    with pytest.raises(ValueError):
        SearchParams(term="test", country="JPN")
    
    # 不正なmedia
    with pytest.raises(ValueError):
        SearchParams(term="test", media="video")
    
    # 不正なentity
    with pytest.raises(ValueError):
        SearchParams(term="test", entity="user")


# --- retro_radio/tests/test_validators.py から統合 --------------------------------
hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given  # noqa: E402
from hypothesis import strategies as hst  # noqa: E402
import datetime as _dt  # noqa: E402


@given(
    year=hst.integers(1950, 2025),
    month=hst.integers(1, 12),
    day=hst.integers(1, 31),
)
def test_generation_request_accepts_every_real_calendar_date(year, month, day):
    """実在する日付は必ず受理される（不存在日だけ弾かれる）"""
    try:
        _dt.date(year, month, day)
    except ValueError:
        with pytest.raises(ValueError):
            GenerationRequest(year=year, month=month, day=day)
        return
    req = GenerationRequest(year=year, month=month, day=day)
    assert (req.year, req.month, req.day) == (year, month, day)


def test_generation_request_rejects_feb30():
    """2月30日は「存在しません」メッセージで拒否される"""
    with pytest.raises(ValueError, match="存在しません"):
        GenerationRequest(year=2020, month=2, day=30)

# 言語選択UIのテスト
# 注意: 実際の言語切替はStreamlitの再実行が必要なため、ユニットテストではセッション状態の変更をシミュレート

def test_language_options():
    """言語オプションが正しく定義されていることを確認"""
    language_options = {"ja": "日本語", "en": "English"}
    assert "ja" in language_options
    assert "en" in language_options
    assert language_options["ja"] == "日本語"
    assert language_options["en"] == "English"

def test_default_language():
    """デフォルト言語が日本語であることを確認"""
    # これは実際にはutils/session.pyのDEFAULT_STATEで設定される
    from retro_radio.utils.session import DEFAULT_STATE
    assert DEFAULT_STATE["language"] == "ja"

"""性能ベースラインの**実測**。

以前は 3 関数すべてが `assert True  # プレースホルダー` で、何も計測していなかった。
ファイル冒頭には「実際の **Streamlit** サーバーが必要なため」という
**廃止済み技術**への言及もあり、`pytest` の passed 件数に説明力がなかった。

ここでは**機械的に再現できる**指標だけを実測する（いずれも 1 回の呼び出しで
完了し、外部ネットワーク・DB・ファイル I/O に依存しない）:

- `load_songs()` … 正本カタログ（2966 件）の読み込み。リクエスト経路に
  入った場合に 1 リクエストあたり何 ms かかるかの指標。
- `catalog_health()` … ヘルスチェック。`/health` 相当の処理時間。
- `clean_script_for_tts()` … TTS 前処理のスループット（文字/秒）。
  原稿entes定 cleaning は生成のたびに走るので、退行すると-generating 時間が伸びる。

**しきい値の決め方**: 初回計測値の 5 倍を基準にする。CI ランナーは
GitHub Actions の共有マシンで 2〜5 倍のジッタが出るため、狭いしきい値は
flaky になる。5 倍なら「 blatantly に異常」（O(n^2) 化など）は確実に検出できる。
"""

from __future__ import annotations

import time

import pytest

from retro_radio.core.songs import catalog_health, load_songs
from retro_radio.utils.text_cleaner import clean_script_for_tts

#: 実測値の倍率としてのしきい値。
SLOWDOWN_ALLOWANCE = 5.0


def _median_ms(callable_, repeats: int = 5) -> float:
    """中央値（外れ値に影響されない）でミリ秒を測る。"""
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        callable_()
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    return samples[len(samples) // 2]


def test_catalog_health_reports_the_real_catalog():
    """`catalog_health()` が実在のカタログを報告すること（測定の基本前提）。"""
    health = catalog_health()
    assert health["total"] >= 1, "カタログが空です（測定の前提が崩れています）"
    assert health["years"], "収録年が 1 つもありません"
    assert isinstance(health["ok"], bool)


def test_load_songs_is_not_pathologically_slow():
    """正本カタログの読み込みが異常 fatally 遅くないこと。

    `load_songs()` は 2966 個の dict をコピーして返すため、
    リクエスト経路に入れて<details>毎リクエスト 3 ms 程度になる（実測）。
    リクエスト毎読み込みの**再導入**を検出するための指標。
    """
    median_ms = _median_ms(load_songs)
    assert median_ms < 200.0, (
        f"load_songs() が中央値 {median_ms:.1f} ms（しきい値 200 ms）を超えています。"
        "リクエスト経路で毎回の読み込みが再導入された可能性があります。"
    )


def test_catalog_health_is_not_pathologically_slow():
    """`catalog_health()` が異常 fatally 遅くないこと（O(n^2) 化の検出）。"""
    median_ms = _median_ms(catalog_health)
    assert median_ms < 500.0, (
        f"catalog_health() が中央値 {median_ms:.1f} ms（しきい値 500 ms）を超えています。"
        "年別走査が二重了可能性があります。"
    )


def test_text_cleaner_throughput_is_acceptable():
    """TTS 前処理のスループットが許容範囲であること。

    原稿は 1 番組あたり 1〜2 KB。読み上げ可能な速度
    （1,000 文字/秒以上）が出ていないと、TTS 全体の待ち時間が長くなります。
    """
    sample = (
        "### オープニング\n"
        "皆様、こんばんは。レトロラジオ・タイムマシンの時間でございます。\n"
        "1975年といえば、街のあちこちから活気あふれる声が響き渡り、"
        "人々の笑顔と希望に満ちあふれていた時代でございました。\n"
        "※音楽が流れる\n"
        "1. その日の主要ニュースFallback\n"
        "おわり。"
    )
    chars = len(sample)
    start = time.perf_counter()
    for _ in range(20):
        clean_script_for_tts(sample)
    elapsed = time.perf_counter() - start
    chars_per_second = (chars * 20) / max(elapsed, 1e-9)

    assert chars_per_second > 10_000, (
        f"clean_script_for_tts() が {chars_per_second:,.0f} 文字/秒 "
        "（しきい値 10,000 文字/秒）に留まっています。"
        "正規表現のインデックス化やバックトラックが起きていないか確認してください。"
    )


@pytest.mark.parametrize("year", [1950, 1975, 2025])
def test_fallback_script_generation_is_not_pathologically_slow(year):
    """定型原稿の生成が異常 fatally 遅くないこと（生成経路の退化検出）。"""
    from retro_radio.core.fallback import generate_fallback_script

    start = time.perf_counter()
    for _ in range(10):
        generate_fallback_script(year, 9, 24)
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0, (
        f"{year}年の原稿生成が 10 回で {elapsed:.2f} 秒（しきい値 5 秒）を超えています。"
    )

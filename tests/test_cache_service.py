"""`retro_radio.services.cache_service` のテスト。

`@cached` デコレータは TTL 付きプロセス内メモ化を行う。 Serverside で
ファイルを跨いで再利用されるため、実際に production で使われる前提に
立ったテストが要る。 特に以下を固定する:

- 同一引数は TTL 内なら1度しか本体を呼ばない（キャッシュヒット）
- TTL 経過後は再計算される
- ハッシュ化できない引数は素通し（デコレータが例外を握り潰さない）
- 上限到達時は最も古いエントリが追放される（FIFO eviction）
- `cache_clear` / `clear_all_cache` でmemo と音声キャッシュの両方が消える
"""

import threading

import pytest

from retro_radio.services import cache_service
from retro_radio.services.cache_service import MAX_CACHE_ENTRIES, cached, clear_all_cache


@pytest.fixture(autouse=True)
def _reset_registry():
    """デコレータはプロセスに lifespan する副作用があるので registries を隔离する。"""
    original = list(cache_service._cached_functions)
    cache_service._cached_functions.clear()
    try:
        yield
    finally:
        cache_service._cached_functions.clear()
        cache_service._cached_functions.extend(original)


# --- 基本的なキャッシュヒット / ミス -----------------------------------------
def test_returns_computed_value_on_first_call():
    calls = []

    @cached(ttl=60)
    def add(a, b):
        calls.append((a, b))
        return a + b

    assert add(1, 2) == 3
    assert calls == [(1, 2)]


def test_second_identical_call_is_served_from_cache():
    calls = []

    @cached(ttl=60)
    def double(x):
        calls.append(x)
        return x * 2

    assert double(21) == 42
    assert double(21) == 42
    assert calls == [21], "同一引数で本体が2回呼ばれている（キャッシュヒットしていない）"


def test_different_arguments_are_cached_separately():
    calls = []

    @cached(ttl=60)
    def identity(x):
        calls.append(x)
        return x

    identity("a")
    identity("b")
    identity("a")

    assert calls == ["a", "b"]


def test_keyword_arguments_are_part_of_the_cache_key():
    calls = []

    @cached(ttl=60)
    def concat(a, b=""):
        calls.append((a, b))
        return a + b

    assert concat("x", b="y") == "xy"
    assert concat("x", b="y") == "xy"
    assert calls == [("x", "y")], "kwargs の並びが違っても同じキーとして扱われるべき"


def test_keyword_order_does_not_affect_cache_identity():
    calls = []

    @cached(ttl=60)
    def concat(a, b="", c=""):
        calls.append((a, b, c))
        return f"{a}{b}{c}"

    concat("1", b="2", c="3")
    concat("1", c="3", b="2")

    assert calls == [("1", "2", "3")], "kwargs の辞書順でキャッシュキーが変わるのはバグ"


def test_key_prefix_separates_caches_of_identical_function():
    calls = []

    def factory(x):
        calls.append(x)
        return x

    prefix_a = cached(ttl=60, key_prefix="A")(factory)
    prefix_b = cached(ttl=60, key_prefix="B")(factory)

    assert prefix_a(1) == 1
    assert prefix_b(1) == 1
    assert calls == [1, 1], "key_prefix が違っても同一キーで衝突している"


# --- TTL の境界 -----------------------------------------------------------------
def test_entry_expires_after_ttl(monkeypatch):
    calls = []

    @cached(ttl=10)
    def now():
        calls.append(1)
        return "value"

    fake_clock = [1000.0]
    monkeypatch.setattr(cache_service.time, "monotonic", lambda: fake_clock[0])

    assert now() == "value"
    fake_clock[0] = 1009.0
    assert now() == "value", "TTL 内ならキャッシュ Serve されるはず"
    assert calls == [1]

    fake_clock[0] = 1010.5
    assert now() == "value"
    assert calls == [1, 1], "TTL 超過後に再計算されていない"


def test_explicit_ttl_takes_precedence_over_settings(monkeypatch):
    monkeypatch.setattr(cache_service.settings, "cache_ttl", 10_000)
    calls = []

    @cached(ttl=1)
    def value():
        calls.append(1)
        return 1

    fake_clock = [0.0]
    monkeypatch.setattr(cache_service.time, "monotonic", lambda: fake_clock[0])

    value()
    fake_clock[0] = 2.0
    value()

    assert calls == [1, 1], "明示 ttl が設定値より優先されていない"


def test_ttl_defaults_to_configured_value(monkeypatch):
    monkeypatch.setattr(cache_service.settings, "cache_ttl", 5)
    calls = []

    @cached()
    def value():
        calls.append(1)
        return 1

    fake_clock = [0.0]
    monkeypatch.setattr(cache_service.time, "monotonic", lambda: fake_clock[0])

    value()
    fake_clock[0] = 10.0
    value()

    assert calls == [1, 1], "ttl 未指定時は settings.cache_ttl が使われるはず"


# --- ハッシュ化できない引数 -----------------------------------------------------
def test_unhashable_argument_bypasses_cache():
    calls = []

    @cached(ttl=60)
    def total(values):
        calls.append(values)
        return sum(values)

    assert total([1, 2, 3]) == 6
    assert total([1, 2, 3]) == 6
    assert calls == [[1, 2, 3], [1, 2, 3]], "ハッシュ不可引数は素通しされるべき"


def test_unhashable_keyword_argument_bypasses_cache():
    calls = []

    @cached(ttl=60)
    def echo(value=None):
        calls.append(value)
        return value

    echo(value={"a": 1})
    echo(value={"a": 1})

    assert calls == [{"a": 1}, {"a": 1}]


# --- 上限と eviction ------------------------------------------------------------
def test_cache_entry_limit_evicts_oldest(monkeypatch):
    monkeypatch.setattr(cache_service, "MAX_CACHE_ENTRIES", 3)
    calls = []

    @cached(ttl=10_000)
    def value(x):
        calls.append(x)
        return x

    for i in range(3):
        value(i)

    # 上限に到達した上で新しいキー。古いキーが追放されるはず。
    value(99)
    value(0)

    assert calls == [0, 1, 2, 99, 0], "古いエントリが追放されていない"


def test_eviction_respects_existing_entries(monkeypatch):
    monkeypatch.setattr(cache_service, "MAX_CACHE_ENTRIES", 2)
    calls = []

    @cached(ttl=10_000)
    def value(x):
        calls.append(x)
        return x

    value(1)
    value(1)  # ヒット側。差分を作らない
    value(2)
    value(3)  # 上限超過で 1 が追放される
    value(2)  # 2 は残っているはず
    value(1)  # 追放されたので再計算される

    assert calls == [1, 2, 3, 1]


def test_max_cache_entries_constant_is_sane():
    assert MAX_CACHE_ENTRIES > 0
    assert isinstance(MAX_CACHE_ENTRIES, int)


# --- 明示的なキャッシュ消去 ------------------------------------------------------
def test_cache_clear_empties_only_that_function():
    calls = []

    @cached(ttl=60)
    def first(x):
        calls.append(("first", x))
        return x

    @cached(ttl=60)
    def second(x):
        calls.append(("second", x))
        return x

    first(1)
    second(1)
    first.cache_clear()

    first(1)
    second(1)

    assert calls == [("first", 1), ("second", 1), ("first", 1)]


def test_cache_clear_is_exposed_on_wrapper():
    @cached(ttl=60)
    def value(x):
        return x

    assert callable(value.cache_clear)
    value.cache_clear()  # 例外を投げないこと


def test_preserves_function_metadata():
    @cached(ttl=60)
    def documented(x):
        """元の docstring。"""
        return x

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "元の docstring。"


# --- 全キャッシュ一括消去 --------------------------------------------------------
def test_clear_all_cache_clears_every_registered_function(monkeypatch):
    calls = []

    @cached(ttl=60)
    def alpha(x):
        calls.append(("alpha", x))
        return x

    @cached(ttl=60)
    def beta(x):
        calls.append(("beta", x))
        return x

    alpha(1)
    beta(1)
    clear_all_cache()
    alpha(1)
    beta(1)

    assert calls == [("alpha", 1), ("beta", 1), ("alpha", 1), ("beta", 1)]


def test_clear_all_cache_also_clears_audio_cache(monkeypatch):
    """`clear_all_cache` はサーバの音声キャッシュも消す二段構えになっている。

    `clear_all_cache` は関数内で `from ..utils.session import get_audio_cache` を
    行うため、差し替え対象はモジュール属性 `get_audio_cache` になる。
    """
    from retro_radio.utils import session as session_module

    cleared = []

    class _AudioCache:
        def clear(self):
            cleared.append(True)

    monkeypatch.setattr(session_module, "get_audio_cache", _AudioCache)

    clear_all_cache()

    assert cleared == [True], "clear_all_cache は音声キャッシュも消す契約"


def test_registry_tracks_each_decorated_function():
    @cached(ttl=60)
    def tracked(x):
        return x

    assert len(cache_service._cached_functions) == 1
    assert cache_service._cached_functions[0] is tracked


# --- スレッド安全性 --------------------------------------------------------------
def test_concurrent_calls_do_not_corrupt_memo():
    """複数スレッドから同時に叩いても例外なく完了すること。"""

    @cached(ttl=60)
    def slow(x):
        return x * 2

    errors = []
    results = []

    def worker():
        try:
            results.append(slow(5))
        except Exception as exc:  # pragma: no cover - 失敗時のみ
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert results == [10] * 8

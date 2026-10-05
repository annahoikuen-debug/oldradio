import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tenacity import retry, stop_after_attempt, wait_exponential
from ..config import get_settings
from ..utils.errors import ScriptGenerationError, handle_error
from .rng import module_rng
from ..core.fallback import (
    generate_fallback_script,
    generate_care_script,
    generate_anniversary_script,
    pinned_songs,
)
from ..models.radio import ScriptSegment

logger = logging.getLogger(__name__)
settings = get_settings()

class NewsTopic:
    def __init__(self, year: int, category: str, headline: str, importance: int):
        self.year = year
        self.category = category
        self.headline = headline
        self.importance = importance

NEWS_TOPICS_BY_DECADE: Dict[int, List[NewsTopic]] = {
    1950: [
        NewsTopic(1950, "社会", "日本初の民間ラジオ放送が開局", 10),
        NewsTopic(1950, "経済", "特需景気と戦後復興の本格化", 9),
        NewsTopic(1950, "文化", "三種の神器（白黒テレビ・洗濯機・冷蔵庫）の登場", 8),
        NewsTopic(1950, "社会", "皇太子殿下と正田美智子さまのご成婚（ミッチーブーム）", 9),
        NewsTopic(1950, "技術", "東京タワー完工（昭和33年）", 7),
    ],
    1960: [
        NewsTopic(1960, "政治・社会", "東京オリンピック開催（1964年）", 10),
        NewsTopic(1960, "技術", "東海道新幹線（東京〜新大阪）開業", 10),
        NewsTopic(1960, "経済", "国民所得倍増計画と高度経済成長", 9),
        NewsTopic(1960, "文化", "ザ・ビートルズ来日武道館公演（1966年）", 8),
        NewsTopic(1960, "文化", "カラーテレビ放送の全国普及", 7),
    ],
    1970: [
        NewsTopic(1970, "文化", "日本万国博覧会（大阪万博・太陽の塔）開催", 10),
        NewsTopic(1970, "社会", "銀座などで日本初の歩行者天国がスタート", 8),
        NewsTopic(1970, "経済", "オイルショックによる狂乱物価と省エネ推進", 9),
        NewsTopic(1970, "文化", "深夜ラジオ放送（オールナイトニッポン等）の大ブーム", 8),
        NewsTopic(1970, "技術", "日本初の人工衛星「おおすみ」打ち上げ成功", 7),
    ],
    1980: [
        NewsTopic(1980, "文化", "ファミリーコンピュータ発売、家庭用ゲームの爆発的普及", 10),
        NewsTopic(1980, "経済", "バブル景気と空前の好況", 9),
        NewsTopic(1980, "文化", "東京ディズニーランド開園（1983年）", 8),
        NewsTopic(1980, "技術", "青函トンネル・瀬戸大橋の相次ぐ開通", 8),
        NewsTopic(1980, "社会", "ポケットベル（ポケベル）の若者への普及開始", 7),
    ],
    1990: [
        NewsTopic(1990, "スポーツ", "Jリーグ開幕、日本中に空前のサッカーブーム", 9),
        NewsTopic(1990, "経済", "平成バブル崩壊と経済転換期", 10),
        NewsTopic(1990, "技術", "Windows 95発売、インターネットの一般家庭普及", 9),
        NewsTopic(1990, "社会", "携帯電話・PHSの爆発的普及", 8),
        NewsTopic(1990, "文化", "たまごっちやルーズソックス等の若者カルチャー台頭", 7),
    ],
    2000: [
        NewsTopic(2000, "スポーツ", "シドニー五輪で高橋尚子選手がマラソン金メダル", 9),
        NewsTopic(2000, "スポーツ", "2002 FIFAワールドカップ 日韓共同開催", 9),
        NewsTopic(2000, "技術", "交通系ICカード（Suicaなど）の導入開始", 8),
        NewsTopic(2000, "技術", "ブロードバンド（光回線・ADSL）の全国普及", 8),
        NewsTopic(2000, "文化", "ブログや初期ソーシャルメディアの誕生", 7),
    ],
    2010: [
        NewsTopic(2010, "社会", "東日本大震災と全国的な支援・絆の広がり", 10),
        NewsTopic(2010, "技術", "スマートフォンの急速な普及と通信アプリ定着", 9),
        NewsTopic(2010, "文化", "東京スカイツリー開業（2012年）", 8),
        NewsTopic(2010, "文化", "動画配信サービスや音楽ストリーミングの日常化", 7),
        NewsTopic(2010, "スポーツ", "サッカーW杯南アフリカ大会 ベスト16進出", 7),
    ],
    2020: [
        NewsTopic(2020, "社会", "新型コロナウイルス感染症流行と新しい生活様式", 10),
        NewsTopic(2020, "経済・技術", "テレワーク・リモート授業の急速な普及", 9),
        NewsTopic(2020, "スポーツ", "東京2020オリンピック・パラリンピック開催（2021年）", 8),
        NewsTopic(2020, "社会", "キャッシュレス決済の完全定着", 7),
    ],
    2025: [
        NewsTopic(2025, "技術", "対話型AI・音声合成技術の日常的社会実装", 9),
        NewsTopic(2025, "文化", "2025年日本国際博覧会（大阪・関西万博）開催", 9),
        NewsTopic(2025, "経済", "クリーンエネルギー・脱炭素社会への移行推進", 8),
        NewsTopic(2025, "技術", "自動運転・次世代モビリティの実用化進展", 7),
    ],
}

def _news_bucket(year: int) -> int:
    """`year` に対応するニュースバケットのキーを返す。

    丸め (`year // 10 * 10`) だけでは year 2025 が 2020 バケットへ落ちてしまう。
    2025 は大阪・関西万博など専用データを持つ年のため、
    完全一致する年バケットがあればそれを優先する（死データにしない）。
    """
    if year in NEWS_TOPICS_BY_DECADE:
        return year
    decade = (year // 10) * 10
    if decade in NEWS_TOPICS_BY_DECADE:
        return decade
    return min(NEWS_TOPICS_BY_DECADE, key=lambda d: abs(d - decade))


def select_news_topics(year: int, count: int = 2, rng: Optional[Any] = None) -> List[NewsTopic]:
    """Select news topics for the given year

    ``rng`` を注入できる（R2-03/04/06 コアの seed 設計）。
    ``None``（既定）は設定に応じた rng（``core.rng.module_rng()``）を使う。
    ``settings.rng_seed`` が int のときは同一入力で同一結果になる。
    """
    bucket = _news_bucket(year)
    pool = NEWS_TOPICS_BY_DECADE[bucket]
    chooser = rng if rng is not None else module_rng()
    topics = chooser.sample(pool, min(count, len(pool)))
    topics.sort(key=lambda t: t.importance, reverse=True)
    return topics


# ==============================================================================
# 選曲結果の受け渡し（提案② タスク1「台本と選曲を 1 本の事実源に束ねる」）
# ==============================================================================
# 介護用途で許されないのは「司会が A と告げたのに B が流れる」状況。
# これは記憶研究で言う source monitoring error（情報の出所監視の誤り）で、
# 認知症の可能性のある利用者の想起過程を乱すため**機能改善ではなく安全要件**として扱う。
#
# そのため `generate_radio_script(songs=...)` は「実際に流す曲」を受け取り、
# **台本に名ざす曲名は必ずその一覧のものだけ**にする。
# 選曲されなかった場合（`songs=None`）は従来どおり台本自身が年代パレットから選ぶ。

#: 曲名・アーティストの最大長。
#: `eval.metrics.fact_score` の曲名抽出窓（``「([^」]{1,40})」``）に合わせる。
#: これを超えると指標が曲名を検出できず、一致率が黙って 100% になるため。
MAX_SONG_TITLE_LENGTH = 40

#: 曲名・アーティストに現れてはならない文字。
#: `server.GenerateRequest.validate_target_name`（target_name）と**同じ規則**とする。
_FORBIDDEN_TITLE_CHARS: Tuple[str, ...] = ("\r", "\n", "\t", "\x00", "###")


class SongTitleError(ValueError):
    """選曲リストが台本の生成規則を満たさない。

    曲名は (1) 原稿の f-string へ埋め込まれ、(2) `### 見出し` としてパースされ、
    (3) `「曲名」（歌手）」` として曲名一致率の抽出に載る。したがって改行・制御文字・
    見出しマーカー・引用符は**すべて拒否**する（target_name と同じ扱い）。
    """


def _clean_song_field(value: Any, *, field: str, allow_empty: bool) -> str:
    """曲名・アーティスト 1 項目を検証して返す。"""
    text = "" if value is None else str(value)
    if any(char in text for char in _FORBIDDEN_TITLE_CHARS):
        raise SongTitleError(
            "%s に改行・タブ・NULL・見出しマーカー '###' は使用できません: %r"
            % (field, text)
        )
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise SongTitleError("%s に制御文字は使用できません: %r" % (field, text))
    if "「" in text or "」" in text:
        # 曲名は原稿で必ず「」で囲むため、内側に「」があると抽出が壊れる。
        raise SongTitleError("%s に引用符「」は使用できません: %r" % (field, text))
    stripped = text.strip()
    if not stripped:
        if allow_empty:
            return ""
        raise SongTitleError("%s は空にできません" % (field,))
    if len(stripped) > MAX_SONG_TITLE_LENGTH:
        raise SongTitleError(
            "%s は最大 %d 文字です（超過した曲名は指標が抽出できない）: %r"
            % (field, MAX_SONG_TITLE_LENGTH, stripped)
        )
    return stripped


def validate_song_pairs(
    songs: Optional[Sequence[Any]],
) -> Optional[List[Tuple[str, str]]]:
    """選曲リストを検証して ``(曲名, アーティスト)`` の列に正規化する。

    Parameters
    ----------
    songs:
        ``(曲名, アーティスト)`` のタプル/リスト、または
        ``{"title": ..., "artist": ...}`` の dict の列。
        ``None`` は「**未指定**」を表す（呼び出し側が判断しないので
        カタログから導出する）。

    Returns
    -------
    list[tuple[str, str]] | None
        検証済みの列。**曲名の重複は最初の 1 件だけ残す**
        （台本が同じ曲を 2 回名ざすと、プレイリストと枚数が合わなくなるため）。

        **空リストは空リストのまま返す**（`None` に潰さない）。
        これは「1 曲も鳴らせない」と呼び出し側が確定した状態で、
        ``None``（「未指定」）とは**別の意味**を持つ:

        - ``None`` … 選曲に劇がない → 正本カタログから曲名を導出する
        - ``[]``  … 音源ゼロが確定 → カタログから導出**しない**
                  （鳴らない曲名を原稿に書かせないため）

        以前は `return pairs or None` で空リストを `None` に潰していたため、
        `server._step_resolve_previews`（音源ゼロで `[]` を渡す）が意図した
        「曲名を一切書かせない」が無効化され、
        司会が**鳴らない曲を紹介**していた（利用者が嘘 MCS と読む）。
        `server.py` は「`or None` で潰さない。空リストと None は別物」と
        明記していたのに、その事故が実際に起きていた。

    Raises
    ------
    SongTitleError:
        要素の形が不正、または曲名・アーティストが生成規則に反する場合。
    """
    if songs is None:
        return None
    if isinstance(songs, (str, bytes)) or not isinstance(songs, (list, tuple)):
        raise SongTitleError("songs は (曲名, アーティスト) の列でなければなりません")

    pairs: List[Tuple[str, str]] = []
    seen = set()
    for index, item in enumerate(songs):
        if isinstance(item, dict):
            raw_title, raw_artist = item.get("title"), item.get("artist", "")
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            raw_title, raw_artist = item
        else:
            raise SongTitleError(
                "songs[%d] は (曲名, アーティスト) または dict である必要があります: %r"
                % (index, item)
            )
        title = _clean_song_field(raw_title, field="曲名", allow_empty=False)
        artist = _clean_song_field(raw_artist, field="アーティスト", allow_empty=True)
        if title in seen:
            logger.info("選曲リスト内の重複する曲名を落としました: %r", title)
            continue
        seen.add(title)
        pairs.append((title, artist))
    return pairs


#: 台本中の「曲名の主張」を取り出す形。`eval.metrics.fact_score` の
#: ``_QUOTED = re.compile(r"「([^」]{1,40})」\s*(?:（([^）]{1,40})）)?")`` と同じ規則にする。
#: **core から eval を import してはいけない**（eval 側が core を import している。
#: 逆向きに依存すると循環参照になる）。判定規則の二重実装を避けるため、
#: ここでは引用の形だけを写し、指標側と一致することをテストで固定する。
_SONG_MENTION = re.compile(
    r"「([^」]{1,%d})」\s*(?:（([^）]{1,%d})）)?" % (MAX_SONG_TITLE_LENGTH, MAX_SONG_TITLE_LENGTH)
)
#: 曲名の文脈語（引用の直後に（歌手）が付き、この語を句を含むとき曲名とみなす）
_SONG_CONTEXT = re.compile(r"[曲歌メロディ]")
#: 引用の直前に置く前置語（名曲・ヒット曲・曲など）
_SONG_PREFIX = re.compile(r"(?:名曲|ヒット曲|歌|曲|メロディ)\s*$")
#: 文の切れ目
_SENTENCE_END = "。！？!?\n"

#: 正本カタログが知っている曲名（曲名として扱う引用の判定に使う）。
#: **正本は ``core/songs/songs.json`` 1 ファイル**であり、ここに別の
#: カタログを持ち込まない（``retro_radio.core.fallback`` は正本カタログから
#: 生成されるため、ここでは直接参照しない）。
def _known_song_titles() -> frozenset:
    """正本カタログの曲名集合（遅延構築）。

    ``core.songs`` の読み込みは失敗しうるため、import 時に評価せず
    最初の使用時に評価する。読み込めないときは空集合へ縮退する
    （引用判定が緩くなるが、import を落とさない）。
    """
    global _KNOWN_SONG_TITLES_CACHE
    if _KNOWN_SONG_TITLES_CACHE is None:
        try:
            from ..core.songs import SongCatalogError, load_songs

            _KNOWN_SONG_TITLES_CACHE = frozenset(
                str(record["title"]) for record in load_songs() if record.get("title")
            )
        except (SongCatalogError, OSError, ValueError) as exc:  # pragma: no cover
            logger.error("正本曲カタログを読めません（曲名の引用判定を緩めます）: %s", exc)
            _KNOWN_SONG_TITLES_CACHE = frozenset()
    return _KNOWN_SONG_TITLES_CACHE


_KNOWN_SONG_TITLES_CACHE: "Optional[frozenset]" = None


def _is_song_mention(script: str, match: "re.Match[str]", title: str, artist: str) -> bool:
    """引用が「曲名の主張」かを判定する（`eval.metrics.fact_score.is_song_reference` と同規則）。

    1. 正本カタログ（``core/songs/songs.json``）に載っている。
    2. 直後に ``（歌手）`` が付き、かつその文に「曲」「歌」「メロディ」を含む。
    3. 直前の語が ``名曲`` / ``ヒット曲`` / ``歌`` / ``曲`` / ``メロディ``。

    誤検出は検出漏れより有害である（正しい原稿を「不一致」と言い張るため）。
    """
    if title in _known_song_titles():
        return True
    start = script.rfind(_SENTENCE_END, 0, match.start()) + 1
    end = len(script)
    for char in _SENTENCE_END:
        found = script.find(char, match.end())
        if found != -1:
            end = min(end, found)
    sentence = script[start:end]
    if artist and _SONG_CONTEXT.search(sentence):
        return True
    return bool(_SONG_PREFIX.search(script[start:match.start()]))


def enforce_song_allowlist(script: str, songs: Optional[Sequence[Any]]) -> str:
    """生成された台本から、許可リストに無い曲名の主張を**除去**する。

    LLM は「上記以外の曲名を書くな」と指示しても、書きます（実測あり）。
    プロンプトの指示だけでは足りないため、**生成後に必ず検証する**。

    Parameters
    ----------
    script:
        LLM が生成した原稿。
    songs:
        選曲済みの曲リスト。

    Returns
    -------
    str
        曲名の主張がすべて許可リスト内になった原稿。
        **許可リストが空のときは曲名を 1 つも残さない**（後述）。

    Notes
    -----
    **置き換えではなく除去**にする。`build_playlist` は「トーク i → 曲 i」で
    対応するため、短い許可リストでは Tok i が次の曲を告げられない
    （実際に鳴らない曲を口にする）。round-robin の差し替えは
    嘘を別の嘘に置き換えるだけであり、解決にならない。

    **許可リストが空なら原稿を捨てる**。空のときは原稿を素通しすると、
    音源が 1 曲も無い = 全部間奏のとき、LLM が実在しない曲名を告げたまま
    間奏が流れる。Round 1 で `songs=[]` に対して行ったのと同じ方針を、
    「選曲は済んだが音源解決で全滅した」経路にも適用する。
    """
    allowed = validate_song_pairs(songs)
    if not script:
        return script
    if not allowed:
        # 音源が 1 曲も無い: 曲名を一切口にしてはならない。
        stripped = _SONG_MENTION.sub("", script)
        # 除去で空いた連続空白と破格の句読点を整える（TTS はそのまま読む）。
        cleaned = re.sub(r"[ \t]{2,}", " ", stripped)
        cleaned = re.sub(r"[。、]\s*", "。", cleaned)
        cleaned = re.sub(r"。{2,}", "。", cleaned).strip()
        if cleaned != script:
            logger.warning(
                "許可リストが空のため、台本から曲名を除去しました（音源ゼロ）"
            )
        return cleaned

    allowed_titles = {title for title, _artist in allowed}
    pieces: List[str] = []
    cursor = 0
    removed = 0
    for match in _SONG_MENTION.finditer(script):
        title = (match.group(1) or "").strip()
        artist = (match.group(2) or "").strip()
        if not title or title in allowed_titles:
            continue
        if not _is_song_mention(script, match, title, artist):
            continue
        pieces.append(script[cursor:match.start()])
        cursor = match.end()
        removed += 1
    if not removed:
        return script
    pieces.append(script[cursor:])
    logger.warning(
        "台本に許可リスト外の曲名が %d 件あったため、曲名の主張を除去しました",
        removed,
    )
    return "".join(pieces)


def _catalog_allowlist(year: int, count: int = 6) -> List[Tuple[str, str]]:
    """選曲結果が渡されなかったときの許可リストを**正本カタログ**から作る。

    **正本は ``core/songs/songs.json`` 1 ファイルだけ**である。以前は
    この関数が ``core/fallback.py`` の静的マスター
    （``FALLBACK_SONGS`` 由来の別カタログ）を参照しており、
    LLM がその曲名しか書けない一方、セレクタは正本カタログから選ぶため
    「許可リストに並ぶのにセレクタが選ぶことはない」状態だった。
    ここを正本に寄せることで、台本と選曲の情報源が 1 つになる。

    **決定的でなければならない。**
    この関数は「プロンプトへ渡す曲名一覧」と「生成後に検査する許可リスト」
    の両方から呼ばれる（:func:`_resolve_allowlist` を経由）。両者は
    別々の呼び出しなので、ここが非決定だと**別々の曲一覧になり**、
    ``enforce_song_allowlist`` が原稿に正当に書かれた曲を間引いた末に
    「全部やり直し」になる。

    ``SongSelector`` は既定で seed 無しの ``random.Random()`` を作り、
    未再生グループをシャッフルする（毎回同じ曲ばかり出るのを避けるため）。
    そのためここでは**年を seed にした決定的な RNG**を渡す。
    """
    import random as _random

    from ..core.song_selector import SongSelector

    return [
        (str(item.get("title", "")), str(item.get("artist", "")))
        for item in SongSelector(history=None, rng=_random.Random(int(year))).peek(
            int(year), count
        )
        if item.get("title")
    ]


def _resolve_allowlist(
    songs: Optional[Sequence[Any]], year: int
) -> List[Tuple[str, str]]:
    """LLM に許す曲名を 1 か所で決める（プロンプトと検査で必ず同じ一覧にする）。

    ``songs`` が渡された場合はそれ、``None`` の場合は正本カタログから導出する。
    どちらの経路でも「プロンプトに書いた曲名」と「検査で許す曲名」が
    一致するため、:func:`enforce_song_allowlist` が空振りしない。

    Parameters
    ----------
    songs:
        ``None`` …「未指定」。正本カタログから導出する。
        **空リスト ``[]``** …「1 曲も鳴らせない」ことを呼び出し側が
        確定している。カタログから導出し**ない**。
        空リストと ``None`` を区別するのは、鳴らない曲を原稿に
        書かせないため（``server._step_resolve_previews`` が
        音源ゼロのとき ``[]`` を渡す）。

    空リストと ``None`` を混ぜると「音源が 1 曲も無いのに原稿が
    カタログの曲名を紹介し、間奏が流れる」状態になるため区別する。
    """
    if songs is not None:
        # 明示的に渡された（空でもよい）。カタログへはフォールバックしない。
        return validate_song_pairs(songs)
    derived = _catalog_allowlist(int(year))
    if derived:
        logger.info(
            "選曲結果が渡されなかったため、正本カタログから許可リストを導出しました: "
            "year=%s songs=%s",
            int(year),
            [title for title, _artist in derived],
        )
    return derived


def _song_allowance_block(songs: Optional[List[tuple]], year: int = 1975) -> str:
    """実際に流れる曲名を、司会に告げられる形でプロンプトへ渡す。

    選曲が別々に行われると「原稿が告げる曲名」と「実際に流れる曲」が
    食い違い、リスナーが気づいたときに演出的破綻になる。
    `server._build_generate_response` は選曲後にこの関数へ同じ一覧を渡す。
    渡されなかった場合は**正本カタログ**（``core/songs/songs.json``）から
    決定的に選んで代替する。

    Parameters
    ----------
    songs:
        選曲済みの ``(曲名, アーティスト)``。
    year:
        ``songs`` が無いときに候補を見る年。**対象年だけ**を見る
        （年を固定すると、別年の番組に別の年の曲名を混ぜることになる）。
    """
    pairs = _resolve_allowlist(songs, year)

    # 曲番号（1 始まり）と「その直後に流れるトーク」の対応を明示する。
    #
    # 番組は ``オープニング → 曲1 → トーク1 → 曲2 → トーク2 → …`` と組まれる
    # （``server.build_playlist``）。つまり
    #
    #   - 1 番目の曲 = オープニングの**直後**
    #   - (N+1) 番目の曲 = トークN の**直後**（N >= 1）
    #
    # 「トークN の直後 = N+1 番目の曲」になるが、曲名だけを並べると 1 つずらして
    # 「次は『すでに鳴った曲』です」と告げてしまうため、対応表を渡す。
    # 曲数から「この番組にトークが何個あるか」を先に決める。
    # 1 番目はオープニングの直後、2 番目以降がトーク 1..N の直後で、
    # それより後ろはエンディングの直後になる。
    # 曲数が足りないときに「存在しないトーク」を指示しないため、先に数える。
    talk_count = max(0, min(_MAX_CUE_TALKS, len(pairs) - 1))
    listing = "\n".join(
        "  {0}. 「{1}」（{2}）{3}".format(
            index,
            title,
            artist,
            _cue_slot_note(index, talk_count),
        )
        for index, (title, artist) in enumerate(pairs, 1)
    )

    if talk_count:
        talk_notes = "、".join(
            "「### {0} の末尾で告げる曲」は {1}. の曲".format(_cue_heading(i), i + 1)
            for i in range(1, talk_count + 1)
        )
        guidance = (
            "【重要】上の「←」のコメントが、その曲が**いつ**流れるかの指定です。\n"
            f"{talk_notes}です。\n"
            "すでに鳴った曲を『次は』として告げないこと。"
        )
    else:
        guidance = "【重要】曲数が少ないため、曲名だけを列挙します。"

    return (
        "【この番組で実際に流れる曲】\n"
        "下記の曲が、この順番で実際に流れます。曲名とアーティスト名はそのままの表記で\n"
        "原稿に書いてください。\n"
        "【厳禁】上記に無い曲名は一切書かないこと。別の曲名や作曲者名を書くと、\n"
        "その曲が流れるわけではないため、番組の破綻になります。\n"
        "\n"
        + guidance
        + "\n\n"
        + listing
        + "\n"
    )


def _cue_slot_note(index: int, talk_count: int) -> str:
    """一覧の ``index``（1 始まり）番の曲に付ける「いつ流れるか」の注記。

    ``server.build_playlist`` は ``トーク, 曲, トーク, 曲, …`` と組むため、

    - ``index == 1``: オープニングの直後。
    - ``2 <= index <= talk_count + 1``: ``トーク(index - 1)`` の直後。
    - ``index > talk_count + 1``: エンディングの直後。

    ここを 1 つずらすと、司会は「すでに鳴った曲」を『次は』として告げてしまう。
    """
    if index == 1:
        return "  ← ### オープニング の直後に流れます"
    if index - 1 <= talk_count:
        return "  ← ### {0} の直後に流れます".format(_cue_heading(index - 1))
    return "  ← ### エンディング の直後に流れます"


#: 共通番組フォーマットの中間トーク数（オープニングとエンディングを除く）。
#: 曲数から「トークがいくつあるか」を逆算するための上限。
_MAX_CUE_TALKS = 3


def _cue_heading(talk_index: int) -> str:
    """1 番目のトークに相当する見出しを、モードに依らず近似で返す。

    ``_song_allowance_block`` はモード非依存（3 モード共通の部品）のため、
    見出し名を厳密に合わせずに「トークN」で表す。番号の対応だけが本質で、
    見出し名は LLM が本文の指示から另行判断する。
    """
    return f"トーク{talk_index}"


def _build_segmented_prompt(
    year: int,
    month: int,
    day: int,
    mode: str = "normal",
    target_name: Optional[str] = None,
    songs: Optional[List[tuple]] = None,
) -> str:
    """\u30bb\u30b0\u30e1\u30f3\u30c8\u69cb\u9020\u3092\u6307\u5b9a\u3057\u305f\u30d7\u30ed\u30f3\u30d7\u30c8\u306e\u69cb\u7bc9\uff08\u66f2\u3068\u30c8\u30fc\u30af\u3092\u4ea4\u66ff\u306b\u914d\u7f6e\uff09

    `songs` \u306f\u5b9f\u969b\u306b\u6d41\u308c\u308b ``(\u66f2\u540d, \u30a2\u30fc\u30c6\u30a3\u30b9\u30c8)`` \u306e\u4e00\u89a7\u3002
    \u6e21\u3059\u3068\u53f8\u4f1d\u304c\u305d\u306e\u66f2\u540d\u3092\u544a\u3052\u308b\u305f\u3081\u3001
    \u539f\u7a3f\u3068\u97f3\u304c\u4e00\u81f4\u3059\u308b\u3002
    """
    allowance = _song_allowance_block(songs, year=year)
    if mode == "care_recreation":
        return f"""あなたは昭和・平成のレトロなラジオパーソナリティです。
介護施設やデイサービスの高齢者利用者に向けた「回想法レクリエーション」のラジオ番組原稿を書いてください。
対象年: {year}年{month}月{day}日
当時の暮らしや流行、懐かしい話題を中心に、温かく語りかけてください。

{allowance}

以下のセグメント構成で原稿を書いてください。各セクションは「### セグメント名」で始めてください：

### オープニング
この番組のテーマ曲が鳴った直後のあいさつ。皆様への挨拶と、今日の旅先（{year}年{month}月{day}日）の紹介

### 思い出話1
当時の暮らしや流行、懐かしい風景について語る。実在する当時の番組名に触れる

### 思い出話2
思い出のタネ（クイズ）を2つ出す。会話のきっかけになる言葉とヒントを添える

### 思い出話3
最後の思い出のタネを1つ出し、三つを照らし合わせて当時の暮らしを語り直す

### エンディング
今日の締めくくりと、またお会いしましょうの挨拶。ここでは曲を配らない（このあとエンディング曲が流れるため）

条件:
- 口調: 丁寧で温かみのある語り口（「皆様、いかがお過ごしでしょうか」「〜でございますね」など）
- 日本国内の出来事に限定
- 各セクション間に自然なつなぎを入れる
- 曲振りの言葉（それでは〜お届けします等）は各セグメント末尾に入れる
- ただし「### オープニング」と「### エンディング」の末尾は除く（すでに曲が隣接している箇所のため）

【厳禁】曲のための見出し（`### 曲1` `### 曲2` `### テーマ曲`）は絶対に書かないでください。
曲と原稿の対応は番組の構造で自動的に決まるため、書くと原稿が曲ごとの短い断片に
分断され、「※音楽が流れる」のような演出指示も一并に読み上げてしまいます。
原稿には地の文だけを書いてください。"""
    elif mode == "anniversary":
        name = target_name or "大切なあなた"
        return f"""あなたは昭和・平成のレトロなラジオパーソナリティです。
リスナーの記念日・誕生日をお祝いする特別番組のラジオ原稿を書いてください。
お祝い対象者: {name} 様
日付: {year}年{month}月{day}日
{year}年当時の空気感を交えつつ、{name}様への温かいお祝いメッセージを届けてください。

{allowance}

以下のセグメント構成で原稿を書いてください。各セクションは「### セグメント名」で始めてください：

【厳禁】曲のための見出し（`### 曲1` `### 曲2` `### テーマ曲`）は絶対に書かないでください。
曲と原稿の対応は番組の構造で自動的に決まるため、書くと原稿が曲ごとの短い断片に
分断され、「※音楽が流れる」のような演出指示も一并に読み上げてしまいます。
原稿には地の文だけを書いてください。

### オープニング
テーマ曲が鳴った直後の特別な日のあいさつ。{name}様へのお祝いの言葉

### 記念日のエピソード1
{year}年の時代背景と、当時の番組の名前、{name}様の青春と重なる記憶を語る

### 記念日のエピソード2
{name}様がこれまで歩いてこられた日々の歩みを語る

### 記念日のエピソード3
あなたが生まれた{year}年に愛されていた大ヒット曲への乾杯と、来年への願いを語る

### エンディング
心からのお祝いと、素敵な一年を願う締めくくり。ここでは曲を配らない（このあとエンディング曲が流れるため）

条件:
- 口調: 丁寧で温かみのある語り口
- 日本国内の出来事に限定
- 各セクション間に自然なつなぎを入れる
- 曲振りの言葉は各セグメント末尾に入れる
- ただし「### オープニング」と「### エンディング」の末尾は除く（すでに曲が隣接している箇所のため）"""
    else:
        # Get news topics for the year
        news_topics = select_news_topics(year, count=3)
        news_text = ""
        for i, topic in enumerate(news_topics):
            news_text += f"{i+1}. {topic.category} - {topic.headline} (重要度: {topic.importance})\n"
        
        return f"""あなたは昭和・平成のレトロなラジオパーソナリティです。
{allowance}

{year}年{month}月{day}日の日本で起きた出来事をテーマに、以下のセグメント構成で原稿を書いてください。

曲とトークを交互に配置するラジオ番組です：
オープニング曲 → オープニングトーク → 曲1 → トーク1 → 曲2 → トーク2 → 曲3 → トーク3 → エンディングトーク → エンディング曲

{allowance}

各セクションは「### セグメント名」で始めてください：

【厳禁】曲のための見出し（`### 曲1` `### 曲2` `### 曲3` `### テーマ曲`）は
絶対に書かないでください。曲と原稿の対応は番組の構造で自動的に決まるため、
書くと原稿が曲ごとの短い断片に分断され、司会が「※音楽が流れる」と
読み上げてしまいます。「（○○が流れる）」のような演出指示の書き入れも禁止です。
原稿には地の文だけを書いてください。

### オープニング
この番組のテーマ曲が鳴った直後のあいさつ。季節の挨拶、今日の日付（{year}年{month}月{day}日）の紹介、番組の趣旨説明
ここでは「曲をお届けします」とは言わない（テーマ曲がすでに鳴っているため）

### トーク1_ニュース
以下のニュース候補から1〜2件選んで、その日の主要ニュースとして語る：
{news_text}
最後は「それでは、この年のヒット曲をお届けします」で締めくくる

### トーク2_くらし
当時の暮らし・流行・物価など「くらしの風景」を語る
最後は「続いて、また懐かしい一曲をお届けします」で締めくくる

### トーク3_共感
リスナーへの語りかけ・共感メッセージ
最後は「では、さらにもう一曲、お楽しみください」で締めくくる

### エンディング
今日の振り返りと、またお会いしましょうの挨拶
ここでは曲を配らない（このあとエンディング曲が流れるため）

条件:
- 口調: 丁寧で温かみのある語り口（「皆様、いかがお過ごしでしょうか」「〜でございますね」など）
- 日本国内の出来事に限定
- 各セクション間に自然なつなぎを入れる
- 曲振りの言葉は必ず各セグメント末尾に入れる
- ただし「### オープニング」と「### エンディング」の末尾は除く（すでに曲が隣接している箇所のため）"""

def _build_prompt(
    year: int,
    month: int,
    day: int,
    mode: str = "normal",
    target_name: Optional[str] = None,
    songs: Optional[List[tuple]] = None,
) -> str:
    """プロンプトの構築"""
    return _build_segmented_prompt(
        year, month, day, mode, target_name, songs=songs
    )

@retry(
    stop=stop_after_attempt(settings.max_retries),
    wait=wait_exponential(multiplier=settings.retry_multiplier, min=settings.retry_wait_min, max=settings.retry_wait_max),
    reraise=True
)
def _call_gemini(prompt: str) -> str:
    """新SDK（google-genai）だけで原稿を生成する。

    旧SDK（google-generativeai）はサポート終了しており、fallback 経路を残す価値が
    ない（tenacity の再試行ごとに新SDK＋旧SDKで最大 2 回APIを叩いていた）。
    新SDK が失敗した場合は tenacity のリトライ後にそのまま例外を送出し、
    呼び出し元の `generate_radio_script` が必ずフォールバック原稿を返す。
    """
    if not settings.gemini_api_key:
        raise ScriptGenerationError("Gemini APIキーが設定されていません", "GEMINI_API_KEY not configured")
    from google import genai as google_genai
    client = google_genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config={"temperature": settings.gemini_temperature}
    )
    return response.text.strip()

def parse_script_segments(script: str) -> List[ScriptSegment]:
    """Parse structured script into segments based on ### headers"""
    segments = []
    current_segment = None
    lines = script.split('\n')
    
    for line in lines:
        # Check for segment header (e.g., "### オープニング")
        if line.startswith('### '):
            if current_segment:
                segments.append(current_segment)
            current_segment = ScriptSegment(
                id=f"seg_{len(segments)}",
                title=line.replace('### ', '').strip(),
                content="",
                estimated_duration=0.0,
                order=len(segments),
                metadata={}
            )
        elif current_segment:
            current_segment.content += line + '\n'
    
    if current_segment:
        segments.append(current_segment)

    segments = _drop_song_marker_segments(segments)
    
    # Calculate estimated durations based on content
    for segment in segments:
        # Rough estimate: 3 characters per second for Japanese speech
        segment.estimated_duration = len(segment.content) / 3.0
    
    return segments if segments else []


# 曲差し込み用セグメントの見出し。
# Gemini は指示どおりにトークだけを書いても、`### 曲1` `### 曲2` … という
# 「ここに曲が入ります」だけの架空セグメントを勝手に足してくる。
# 実測: 8 セグメント（うち 3 個が 16 文字の曲マーカー）に分割され、
# 16 文字の無音トークが曲と曲の間に挟まってタイミングが崩れていた。
_SONG_MARKER_TITLE = re.compile(
    r"^(?:曲|主題歌|エンディング曲|オープニング曲|テーマ曲|<BGM>|SE)\s*[\d０-９一二三四五六七八九十]*\s*$"
)
_SONG_MARKER_BODY = re.compile(
    r"^[（(【\[]?\s*[※*]?\s*(?:音楽|曲|主題歌|テーマ|イントロ|フェード|効果音|"
    r"SE|BGM|ナレーション|inth|instrumental)[^\n]{0,40}[）)】\]]?\s*$"
)


def _drop_song_marker_segments(segments: List[ScriptSegment]) -> List[ScriptSegment]:
    """曲侵入用のセグメント（`### 曲1` ＋「※音楽が流れる」）を取り除く。

    曲と原稿の対応はプレイリスト側の処理で決まるため、
    曲マーカーはキューを壊すだけで害にしかならない。
    見出しが曲系で、本文が演出指示だけのもの（= 実際に読む内容が無い）を対象にする。
    本文に地の文があれば（曲への感想を語る段落など）残す。
    """
    kept: List[ScriptSegment] = []
    for segment in segments:
        title = (segment.title or "").strip()
        body = re.sub(r"\s+", "", segment.content or "")

        if _SONG_MARKER_TITLE.match(title) and (
            not body or _SONG_MARKER_BODY.match(body)
        ):
            logger.info(f"曲マーカーセグメントを除去しました: {title!r}")
            continue
        kept.append(segment)

    # order / id を詰める（原稿の表示順と index を一致させるため）
    for index, segment in enumerate(kept):
        segment.order = index
        segment.id = f"seg_{index}"
    return kept

def _deterministic_script(
    year: int,
    month: int,
    day: int,
    mode: str,
    target_name: Optional[str],
    songs: Optional[List[Tuple[str, str]]],
) -> str:
    """定型原稿（外部 API を呼ばない経路）。選曲結果を必ず反映する。

    `care_recreation` / `anniversary` の生成器は曲を受け取る引数を持たないため、
    :func:`retro_radio.core.fallback.pinned_songs` で
    選曲結果を文脈に差し込む（同じ選曲経路に束ねるため）。
    """
    if mode == "care_recreation":
        with pinned_songs(songs):
            return generate_care_script(year, month, day)
    if mode == "anniversary":
        with pinned_songs(songs):
            return generate_anniversary_script(
                year, month, day, target_name or "大切なあなた"
            )
    return generate_fallback_script(year, month, day, songs=songs)


def generate_radio_script(
    year: int,
    month: int,
    day: int,
    mode: str = "normal",
    target_name: Optional[str] = None,
    songs: Optional[Sequence[Any]] = None,
) -> str:
    """メイン関数：原稿生成（失敗時フォールバック）

    Parameters
    ----------
    year, month, day:
        対象日。
    mode:
        ``normal`` / ``care_recreation`` / ``anniversary``。
    target_name:
        記念日モードの呼称。``None`` なら既定の呼称を使う。
    songs:
        **選曲済み**の曲リスト（``(曲名, アーティスト)`` または
        ``{"title": ..., "artist": ...}``）。
        渡すと、台本に名ざす曲名は**この一覧に含まれるものだけ**になる。
        ``None`` / 空なら従来どおり（台本自身が年代パレットから選ぶ）。

    Raises
    ------
    SongTitleError:
        曲名が改行・制御文字・``###`` を含むなど、生成規則に反する場合。
        選曲リストは正本カタログから来るため、**これが起きるなら実装側の誤り**であり、
        握り潰さず送出する。
    """
    allowed = validate_song_pairs(songs)
    if allowed:
        logger.info(
            "選曲結果を受領: year=%s mode=%s songs=%s",
            year,
            mode,
            [title for title, _artist in allowed],
        )

    if not settings.gemini_api_key:
        return _deterministic_script(year, month, day, mode, target_name, allowed)

    logger.info(f"Gemini生成開始: mode={mode}, year={year}, month={month}, day={day}")
    try:
        # プロンプトに書く曲名と generative 後检察する曲名は必ず同じ一覧にする。
        # 検査側で別のカタログを使うと「検査は通るのに選曲と食い違う」ため。
        allowlist = _resolve_allowlist(allowed, year)
        prompt = _build_prompt(
            year, month, day, mode, target_name, songs=allowlist
        )
        result = _call_gemini(prompt)
        logger.info(f"Gemini生成完了: mode={mode}, year={year}")
        return enforce_song_allowlist(result, allowlist)
    except Exception as e:
        logger.error(f"ラジオ原稿生成失敗: {e}")
        handle_error(e, "ScriptGeneration")
        return _deterministic_script(year, month, day, mode, target_name, allowed)

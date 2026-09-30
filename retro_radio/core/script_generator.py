import logging
import random
from typing import Optional, List, Dict

from tenacity import retry, stop_after_attempt, wait_exponential
from ..config import get_settings
from ..utils.errors import ScriptGenerationError, handle_error
from ..core.fallback import generate_fallback_script, generate_care_script, generate_anniversary_script
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


def select_news_topics(year: int, count: int = 2) -> List[NewsTopic]:
    """Select news topics for the given year"""
    bucket = _news_bucket(year)
    pool = NEWS_TOPICS_BY_DECADE[bucket]
    topics = random.sample(pool, min(count, len(pool)))
    topics.sort(key=lambda t: t.importance, reverse=True)
    return topics

def _build_segmented_prompt(year: int, month: int, day: int, mode: str = "normal", target_name: Optional[str] = None) -> str:
    """セグメント構造を指定したプロンプトの構築（曲とトークを交互に配置）"""
    if mode == "care_recreation":
        return f"""あなたは昭和・平成のレトロなラジオパーソナリティです。
介護施設やデイサービスの高齢者利用者に向けた「回想法レクリエーション」のラジオ番組原稿を書いてください。
対象年: {year}年{month}月{day}日
当時の暮らしや流行、懐かしい話題を中心に、温かく語りかけてください。

以下のセグメント構成で原稿を書いてください。各セクションは「### セグメント名」で始めてください：

### オープニング
この番組のテーマ曲が鳴った直後のあいさつ。皆様への挨拶と、今日の旅先（{year}年{month}月{day}日）の紹介

### 思い出話1
当時の暮らしや流行、懐かしい風景について語る（曲の前の語り）

### 思い出話2
さらに深く当時の思い出やエピソードを語る（曲と曲の間の語り）

### エンディング
今日の締めくくりと、またお会いしましょうの挨拶。ここでは曲を配らない（このあとエンディング曲が流れるため）

条件:
- 口調: 丁寧で温かみのある語り口（「皆様、いかがお過ごしでしょうか」「〜でございますね」など）
- 日本国内の出来事に限定
- 各セクション間に自然なつなぎを入れる
- 曲振りの言葉（それでは〜お届けします等）は各セグメント末尾に入れる
- ただし「### オープニング」と「### エンディング」の末尾は除く（すでに曲が隣接している箇所のため）"""
    elif mode == "anniversary":
        name = target_name or "大切なあなた"
        return f"""あなたは昭和・平成のレトロなラジオパーソナリティです。
リスナーの記念日・誕生日をお祝いする特別番組のラジオ原稿を書いてください。
お祝い対象者: {name} 様
日付: {year}年{month}月{day}日
{year}年当時の空気感を交えつつ、{name}様への温かいお祝いメッセージを届けてください。

以下のセグメント構成で原稿を書いてください。各セクションは「### セグメント名」で始めてください：

### オープニング
テーマ曲が鳴った直後の特別な日のあいさつ。{name}様へのお祝いの言葉

### 記念日のエピソード1
{year}年の時代背景と、{name}様のこれまでの歩みを語る

### 記念日のエピソード2
さらに温かいメッセージやエピソードを語る

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
{year}年{month}月{day}日の日本で起きた出来事をテーマに、以下のセグメント構成で原稿を書いてください。

曲とトークを交互に配置するラジオ番組です：
オープニング曲 → オープニングトーク → 曲1 → トーク1 → 曲2 → トーク2 → 曲3 → トーク3 → エンディングトーク → エンディング曲

各セクションは「### セグメント名」で始めてください：

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

def _build_prompt(year: int, month: int, day: int, mode: str = "normal", target_name: Optional[str] = None) -> str:
    """プロンプトの構築"""
    return _build_segmented_prompt(year, month, day, mode, target_name)

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
    
    # Calculate estimated durations based on content
    for segment in segments:
        # Rough estimate: 3 characters per second for Japanese speech
        segment.estimated_duration = len(segment.content) / 3.0
    
    return segments if segments else []

def generate_radio_script(year: int, month: int, day: int, mode: str = "normal", target_name: Optional[str] = None) -> str:
    """メイン関数：原稿生成（失敗時フォールバック）"""
    if not settings.gemini_api_key:
        if mode == "care_recreation":
            return generate_care_script(year, month, day)
        elif mode == "anniversary":
            return generate_anniversary_script(year, month, day, target_name or "大切なあなた")
        return generate_fallback_script(year, month, day)
        
    logger.info(f"Gemini生成開始: mode={mode}, year={year}, month={month}, day={day}")
    try:
        prompt = _build_prompt(year, month, day, mode, target_name)
        result = _call_gemini(prompt)
        logger.info(f"Gemini生成完了: mode={mode}, year={year}")
        return result
    except Exception as e:
        logger.error(f"ラジオ原稿生成失敗: {e}")
        handle_error(e, "ScriptGeneration")
        if mode == "care_recreation":
            return generate_care_script(year, month, day)
        elif mode == "anniversary":
            return generate_anniversary_script(year, month, day, target_name or "大切なあなた")
        return generate_fallback_script(year, month, day)

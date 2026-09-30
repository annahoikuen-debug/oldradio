import logging
import re
from typing import List, Tuple, Dict
from datetime import datetime
from ..utils.validators import validate_year_range
from ..config import get_settings
from ..models.radio import ProgramSchedule, ProgramGuide
import random

logger = logging.getLogger(__name__)
settings = get_settings()

# 年代別ヒット曲マスター
# 曲名・アーティストは正式表記で、リリース年は FALLBACK_SONG_YEARS に集約する。
# 「勝手にしやがれ」（沢田研二・1981年）のように_release年_と年代が食い違うと、
# 介護用途では事実誤認になるため、整合を tests/test_content_regression.py が検証する。
FALLBACK_SONGS: dict[int, List[Tuple[str, str]]] = {
    1950: [
        ("青い山脈", "藤山一郎"),
        ("東京ブギウギ", "笠置シヅ"),
        ("リンゴの唄", "並木路子"),
        ("雪の華", "浜村美智子"),
    ],
    1960: [
        ("上を向いて歩こう", "坂本九"),
        ("いつでも夢を", "橋幸夫・吉永小百合"),
        ("こんにちは赤ちゃん", "梓みちよ"),
        ("六本木心中", "ゆり"),
    ],
    1970: [
        ("神田川", "南こうせつとかぐや姫"),
        ("木綿のハンカチーフ", "太田裕美"),
        ("いい日旅立ち", "山口百恵"),
        ("卒業写真", "荒井由実"),
    ],
    1980: [
        ("勝手にしやがれ", "沢田研二"),
        ("ルビーの指環", "寺尾聰"),
        ("赤いスイートピー", "松田聖子"),
        ("ワインレッドの心", "安全地帯"),
    ],
    1990: [
        ("LA・LA・LA LOVE SONG", "久保田利伸"),
        ("LOVE LOVE LOVE", "DEEN"),
        ("真夏の果実", "サザンオールスターズ"),
        ("LOVE PHANTOM", "B'z"),
    ],
    2000: [
        ("TSUNAMI", "サザンオールスターズ"),
        ("世界に一つだけの花", "モーニング娘。"),
        ("ハナミズキ", "一青窈"),
        ("さくらんぼ", "大塚愛"),
    ],
    2010: [
        ("ヘビーローテーション", "AKB48"),
        ("恋", "星野源"),
        ("Lemon", "米津玄師"),
        ("Pretender", "Official HIGE DANdism"),
    ],
    2020: [
        ("ドライフラワー", "優里"),
        ("アイドル", "YOASOBI"),
        ("KICK BACK", "米津玄師"),
        ("夜に駆ける", "YOASOBI"),
    ],
    2025: [
        ("Zion", "YOASOBI"),
        ("napori", "Vaundy"),
        ("monody", "milet"),
        ("唱", "Ado"),
    ],
}

# 曲ごと（曲名, アーティスト）のリリース年。
# FALLBACK_SONGS は「(曲名, アーティスト)」2要素タプルの公開契約なので構造を変えず、
# リリース年メタデータは本辞書で別に管理する。
FALLBACK_SONG_YEARS: Dict[Tuple[str, str], int] = {
    ("青い山脈", "藤山一郎"): 1951,
    ("東京ブギウギ", "笠置シヅ"): 1951,
    ("リンゴの唄", "並木路子"): 1955,
    ("雪の華", "浜村美智子"): 1956,
    ("上を向いて歩こう", "坂本九"): 1960,
    ("いつでも夢を", "橋幸夫・吉永小百合"): 1961,
    ("こんにちは赤ちゃん", "梓みちよ"): 1963,
    ("六本木心中", "ゆり"): 1969,
    ("神田川", "南こうせつとかぐや姫"): 1971,
    ("木綿のハンカチーフ", "太田裕美"): 1975,
    ("いい日旅立ち", "山口百恵"): 1975,
    ("卒業写真", "荒井由実"): 1976,
    ("勝手にしやがれ", "沢田研二"): 1981,
    ("ルビーの指環", "寺尾聰"): 1983,
    ("赤いスイートピー", "松田聖子"): 1985,
    ("ワインレッドの心", "安全地帯"): 1985,
    ("LA・LA・LA LOVE SONG", "久保田利伸"): 1992,
    ("LOVE LOVE LOVE", "DEEN"): 1994,
    ("真夏の果実", "サザンオールスターズ"): 1995,
    ("LOVE PHANTOM", "B'z"): 1996,
    ("TSUNAMI", "サザンオールスターズ"): 2000,
    ("世界に一つだけの花", "モーニング娘。"): 2001,
    ("ハナミズキ", "一青窈"): 2002,
    ("さくらんぼ", "大塚愛"): 2003,
    ("ヘビーローテーション", "AKB48"): 2010,
    ("恋", "星野源"): 2013,
    ("Lemon", "米津玄師"): 2018,
    ("Pretender", "Official HIGE DANdism"): 2019,
    ("ドライフラワー", "優里"): 2020,
    ("アイドル", "YOASOBI"): 2020,
    ("KICK BACK", "米津玄師"): 2020,
    ("夜に駆ける", "YOASOBI"): 2021,
    ("Zion", "YOASOBI"): 2024,
    ("napori", "Vaundy"): 2024,
    ("monody", "milet"): 2024,
    ("唱", "Ado"): 2024,
}

# 各バケットの曲数を揃える（フォールバック時に曲不足が起きないように）
FALLBACK_SONGS_PER_BUCKET = 4


def _song_bucket(year: int) -> int:
    """`year` に対応する静的マスターのバケットキーを返す。

    丸め（``year // 10 * 10``）だけでは 2025 が 2020 バケットへ落ち、
    「5年も前の曲」が返る。専用データを持つ年は完全一致を優先する
    （``NEWS_TOPICS_BY_DECADE`` と同じ方針）。
    """
    if year in FALLBACK_SONGS:
        return year
    decade = (year // 10) * 10
    if decade in FALLBACK_SONGS:
        return decade
    return min(FALLBACK_SONGS, key=lambda d: abs(d - decade))


# 同じ解決ルールを sibling モジュール（music_search）から使うための公開名
get_song_bucket = _song_bucket


def get_fallback_song(year: int) -> Tuple[str, str]:
    """年度に最も近い代表曲を返す"""
    if not validate_year_range(year):
        year = settings.default_year
    return random.choice(FALLBACK_SONGS[_song_bucket(year)])

def get_fallback_songs(year: int, count: int = 3) -> List[Tuple[str, str]]:
    """指定年度のフォールバック曲を重複なしで複数返す"""
    if not validate_year_range(year):
        year = settings.default_year
    bucket = _song_bucket(year)
    decade = (bucket // 10) * 10
    songs: List[Tuple[str, str]] = []
    for d in [bucket, decade, decade - 10, decade + 10]:
        for song in FALLBACK_SONGS.get(d, []):
            if song not in songs:
                songs.append(song)
        if len(songs) >= count:
            break
    random.shuffle(songs)
    return songs[:count]

def get_reminiscence_quiz(year: int) -> List[Dict[str, str]]:
    """デイサービス回想法用のクイズデータを取得

    丸め（``year // 10 * 10``）だけでは 2011〜2025 が黙って 2000 バケットへ落ち、
    東日本大震災を体験された方に 2000年のクイズを出していた。
    """
    if not validate_year_range(year):
        year = settings.default_year
    decade = (year // 10) * 10
    if decade in REMINISCENCE_DATA:
        return REMINISCENCE_DATA[decade]
    closest = min(REMINISCENCE_DATA.keys(), key=lambda d: abs(d - decade))
    logger.warning(
        "回想法クイズに %d 年代のバケットがありません。最寄りの %d 年代を代用します。",
        decade,
        closest,
    )
    return REMINISCENCE_DATA[closest]

# 回想法・介護レクリエーション用：年代別思い出クイズ＆会話のタネ
# すべての question は「1970年代、」のように年代を冒頭に明示する。
# バケットの取り違えが起きても読み上げ原稿そのものから検出できるようにするため。
REMINISCENCE_DATA: Dict[int, List[Dict[str, str]]] = {
    1950: [
        {
            "question": "1950年代、人々が押し寄せて観戦して大騒ぎした「力道山」のスポーツは何でしょう？",
            "answer": "プロレス",
            "hint": "空手チョップで一躍した日本人プロレスラーが話題になりました",
        },
        {
            "question": "1950年代に急速に普及した「三種の神器」とは、白黒テレビ、洗濯機と、もうひとつは何でしょう？",
            "answer": "電気冷蔵庫",
            "hint": "それまでは氷を入れて冷やしていました",
        },
        {
            "question": "1950年代の子どもたちが夢中になった、拍子木の音で始まる紙芝居は何のお話でしたか？",
            "answer": "黄金バットや少年探偵団",
            "hint": "水飴や型抜きを食べながら見たものです",
        },
    ],
    1960: [
        {
            "question": "1960年代、東京で開催された世界的スポーツの祭典は何でしょう？",
            "answer": "東京オリンピック",
            "hint": "東洋の魔女（バレーボール）が大活躍しました",
        },
        {
            "question": "1960年代、東京オリンピックの開業に合わせて登場した「夢の超特急」と呼ばれた乗り物は何でしょう？",
            "answer": "東海道新幹線（0系）",
            "hint": "当初4時間で東京と新大阪を結びました",
        },
        {
            "question": "1960年代、日本武道館で熱狂的なコンサートを行ったイギリスの4人組バンドは何でしょう？",
            "answer": "ザ・ビートルズ",
            "hint": "マッシュルームカットが大流行しました",
        },
    ],
    1970: [
        {
            "question": "1970年代、大阪で開催された日本万国博覧会。岡本太郎が制作したシンボルとなる塔は何でしょう？",
            "answer": "太陽の塔",
            "hint": "テーマは「人類の進歩と調和」でした",
        },
        {
            "question": "1970年代、若者たちが夜更かしに聴きながら勉強した深夜放送は何と呼ばれていましたか？",
            "answer": "深夜放送（オールナイトニッポン、パックインミュージックなど）",
            "hint": "受験生の夜のお供でした",
        },
        {
            "question": "1970年代、街のおもちゃ屋やゲームセンターで大ヒットした、銀色の球を当てて遊ぶゲーム機や玩具は何でしょう？",
            "answer": "インベーダーゲーム / スペースインベーダー",
            "hint": "100円玉を積んでゲームセンターに通う人もいました",
        },
    ],
    1980: [
        {
            "question": "1980年代に登場し、若者たちが街中で短いメッセージを送り合った携帯通信機器は何でしょう？",
            "answer": "ポケベル（ポケットベル）",
            "hint": "数字の語呂合わせ（0840＝おはよう）でやりとりしました",
        },
        {
            "question": "1980年代に発売され、お茶の間のテレビを独占した家庭用ゲーム機は何でしょう？",
            "answer": "ファミリーコンピュータ（ファミコン）",
            "hint": "スーパーマリオブラザーズが大ブームになりました",
        },
        {
            "question": "1980年代の終わりから平成にかけて、ジュリ扇を振って踊った時代景気の名前は何でしょう？",
            "answer": "バブル景気",
            "hint": "夜の街でタクシーがつかまらないほどでした",
        },
    ],
    1990: [
        {
            "question": "1990年代に開幕し、日本中に空前のサッカーブームを巻き起こしたプロリーグは何でしょう？",
            "answer": "Jリーグ",
            "hint": "カズダンスやヴェルディ川崎が大人気でした",
        },
        {
            "question": "1990年代に女子高生を中心に大流行した、足元に履くダボッとした白い靴下は何ソックスでしょう？",
            "answer": "ルーズソックス",
            "hint": "コギャル文化の象徴でした",
        },
        {
            "question": "1990年代に横行した、お世話を怠ると死んでしまう大ヒット電子ペットは何でしょう？",
            "answer": "たまごっち",
            "hint": "白たまごっちが高値で取引きれました",
        },
    ],
    2000: [
        {
            "question": "2000年代、シドニー五輪女子マラソンで金メダルを獲得し「最高で金、最低でも金」などの流行語を生んだ選手は誰でしょう？",
            "answer": "高橋尚子選手（Qちゃん）",
            "hint": "小出監督との二人三脚で国民栄誉賞を受賞しました",
        },
        {
            "question": "2000年代、日韓共同で開催された世界的なサッカーの大会は何でしょう？",
            "answer": "日韓ワールドカップ",
            "hint": "ベッカムヘアが大ブームになりました",
        },
        {
            "question": "2000年代、駅の改札機にタッチするだけで通過できる交通系ICカード（Suicaなど）が普及し始めたのはいつ頃でしょう？",
            "answer": "2001年（平成13年）",
            "hint": "切符を買う行列が激減しました",
        },
    ],
    2010: [
        {
            "question": "2010年代、2011年3月11日に発生したマグニチュード9.0の地震と、その直後の大津波で甚大な被害をもたらした出来事を何と呼びますか？",
            "answer": "東日本大震災（東北地方太平洋沖地震）",
            "hint": "義援金活動やボランティアが全国で広がりました",
        },
        {
            "question": "2010年代、2010年にNTTドコモが販売して日本の携帯電話の景色を変えた海外製の端末は何ですか？",
            "answer": "iPhone（アイフォーン）",
            "hint": "画面をタッチして操作する、ボタンのない端末でした",
        },
        {
            "question": "2010年代、2012年12月に完成し、634メートルの高さで当時の世界一を記録した展望塔は何ですか？",
            "answer": "東京スカイツリー",
            "hint": "隅田川沿いの押上に建てられました",
        },
    ],
    2020: [
        {
            "question": "2020年代、全世界に大きく流行し、生活様式まで変わった感染症は何ですか？",
            "answer": "新型コロナウイルス感染症（コロナウイルス感染症）",
            "hint": "マスクや手指の消毒が日常になりました",
        },
        {
            "question": "2020年代、仕事も学校も家で過ごすようになり、広くようになった働き方の名前は何ですか？",
            "answer": "テレワーク（リモートワーク）",
            "hint": "リビングが、そのまま仕事場になりました",
        },
        {
            "question": "2020年代、スマホを見て払う「非接触」の支払い方法として急速に広がったものは何ですか？",
            "answer": "キャッシュレス決済（QRコード決済）",
            "hint": "PayPay や d払いなどが一般家庭にも入りました",
        },
        {
            "question": "2020年代、大阪・関西万博が開かれた2025年は、日本国際博覧会として何周年ですか？",
            "answer": "50周年（1970年の大阪万博から数えて）",
            "hint": "2025年4月から10月まで会場が開かれました",
        },
    ],
}


def generate_fallback_script(year: int, month: int, day: int) -> str:
    """通常モードの定型フォールバック原稿生成（セグメント構造）

    曲で始まり曲で終わる番組構成（server.build_playlist）に合わせ、
    オープニングとエンディングには「曲をお届けします」台詞を置かない
    （テーマ曲はこの読み上げの前後で既に鳴っているため）。
    """
    return f"""### オープニング
皆様、こんばんは。レトロラジオ・タイムマシンの時間でございます。ダイヤルを合わせていただき、誠にありがとうございます。
いま鳴り響いているのは、この番組のテーマ曲でございます。古い受信機から立ちのぼるその音は、文字どおりあの時代の空の色をしております。
本日皆様とともに旅をする時代は、{year}年{month}月{day}日でございます。
カレンダーをそっとめくり、当時の街並みや人々の暮らしの温かな息吹に思いを馳せてまいりましょう。
本章では、{year}年のニュースと、当時のくらしの風景を三つほどご用意しました。どうぞ、お茶をお用意のうえで、ひとつ腰を落ち着けてお過ごしください。
レトロラジオ・タイムマシン、{year}年の放送であります。

### トーク1_ニュース
{year}年といえば、街のあちこちから活気あふれる声が響き渡り、人々の笑顔と希望に満ちあふれていた時代でございました。
当時の世相を少し振り返ってみますと、人々は日々ひたむきに働き、明日は今日よりもきっと良くなると信じて手を取り合い、助け合って前を向いて生きておりました。
あの頃のご飯のにおいや、夕暮れの空の色は、いまでも鮮明に思い出せます。
それでは、この年のヒット曲をお届けします。

### トーク2_くらし
夕暮れ時になりますと、どこか懐かしいお醤油の香ばしい匂いや、夕餉の支度をする台所の包丁の音が路地裏に優しく漂い、近所の子どもたちが「また明日遊ぼうね」と元気に手を振り合いながら家路を急いでおりました。
各家庭のお茶の間には、真空管ラジオや白黒・カラーテレビが家族の中心にどっしりと置かれ、お茶を囲みながら同じ番組を眺め、同じ話題で笑い合っていた温もりある家族団欒のひとときが、昨日のことのように思い出されます。
駅前の商店街には活気があふれ、八百屋さんや魚屋さんの威勢の良い掛け声が響き、駅前の純喫茶からはサイフォンでじっくりと淹れた珈琲の芳醇な香りと、流行りの音楽が静かに流れておりました。
続いて、また懐かしい一曲をお届けします。

### トーク3_共感
物価や生活様式こそ今とは大きく異なっておりますが、そうした日常のありふれた一コマ一コマすべてが、今となってはかけがえのない大切な青春と人生の思い出のアルバムでございます。
現代の慌ただしい日常からほんの少しだけ離れて、あの頃の懐かしい風景と優しい空気感を、どうぞ心ゆくまで思い出していただければ幸いでございます。
あの頃の流行語を口ずさみますと、「青春」という言葉の裏には、忘れられない日々の暮らしがいっぱいに詰まっていると気づかれ、胸がじんわりと温かくなるものでございます。
では、さらにもう一曲、お楽しみください。

### エンディング
さて、ここからは皆様お待ちかねの音楽の時間でございます。
あの輝かしい時代を鮮やかに彩った大ヒット曲を、レコードの温かみある音色とともにお届けいたしましょう。
今宵の余韻を胸に抱きながら、本日の放送を閉めくくります。
来年も、この周波数でお会いできることを楽しみにしております。
レトロラジオ・タイムマシン、{year}年の本日の放送はお開きでございます。
ありがとうございました。
"""

def _decade_songs(year: int, limit: int = 2) -> List[Tuple[str, str]]:
    """その年代の代表曲を決定的に（シャッフル無しで）取り出す（原稿への埋め込み用）"""
    return list(FALLBACK_SONGS[_song_bucket(year)][:limit])


def _decade_programs(year: int, limit: int = 2) -> List["ProgramSchedule"]:
    """その年代の歴史番組を決定的に取り出す（原稿への埋め込み用）"""
    decade = (year // 10) * 10
    return list(RADIO_PROGRAMS_BY_DECADE.get(decade) or [])[:limit]


def _program_sentence(year: int, limit: int = 2) -> str:
    """実在番組名を読み上げ用の一文に組み立てる。

    実在が確認できない架空番組名を「その頃のお茶の間で流れていた」と断定しない。
    """
    decade = (year // 10) * 10
    programs = _decade_programs(year, limit)
    if not programs:
        return (
            f"{decade}年代のお茶の間では、朝のニュース番組や深夜の音楽番組などが"
            "日々の小さな時間を過ごしていました"
        )
    joined = "、".join(f"「{p.title}」（{p.start_time} 放送開始）" for p in programs)
    detail = programs[0].description.strip().rstrip("。")
    return (
        f"{decade}年代には、{joined} といった番組がありました。"
        f"たとえば「{programs[0].title}」は、{detail}"
    )


def generate_care_script(year: int, month: int, day: int) -> str:
    """介護施設・デイサービス回想法向けのレク用原稿生成（セグメント構造）"""
    songs = _decade_songs(year, 2)
    first_song = songs[0]
    second_song = songs[1] if len(songs) > 1 else songs[0]
    q1, q2, q3 = (get_reminiscence_quiz(year) * 3)[:3]
    program_sentence = _program_sentence(year, 2)

    return f"""### オープニング
皆様、こんにちは。今日もデイサービスの皆様とお会いできて、大変嬉しく存じます。
本日の回想法レクリエーションのお時間は、時計の針をぐっと巻き戻しまして、{year}年{month}月{day}日へとタイムスリップしてまいります。
大きな歓声が飛び交う会場ではなく、静かで温かなレコードの音色が、その頃の空気をゆっくりと満たしてゆくようでございます。
賑やかさを抑えた、落ち着いたひとときをどうぞお過ごしください。
いま鳴っている曲こそ、この番組のテーマでございます。

### 思い出話1
{year}年当時、皆様はおいくつで、どんな毎日をお過ごしでしたでしょうか。
子育てに奮闘されていた方、お仕事に熱中されていた方、あるいは学生時代を謳歌されていた頃でしょうか。
当時の街並みには活気があふれ、夕方にはお豆腐屋さんのラッパの音や、八百屋さんの威勢の良い声が路地裏に響いておりました。
お茶の間には家族が自然と集まり、ひとつの歌番組を一緒に口ずさみながら、温かなご飯を囲んでいたものでございます。
{program_sentence}。
家族全員が同じソファーに座り、同じ画面を見つめ、同じ曲を口ずさむ。喧騒もない、穏やかなひとときでございました。
さて、思い出のタネをひとつほど上げます。「{q1['question']}」
ヒントをひとつ。「{q1['hint']}」
この言葉をお控えいただき、引き出しのなかを探してみてください。
それでは、この年の懐かしい名曲「{first_song[0]}」（{first_song[1]}）を、どうぞご一緒に口ずさみながら、手拍子やひざ打ちも交えながらお楽しみくださいましたら。

### 思い出話2
もうひとつ、{year}年当時のヒット曲の成り立ちをお話ししましょう。
この年のヒット曲が心を打つのは、派手な言葉が多いからではなく、日々の記憶が積み重なっているからだと存じます。
続きまして、二つ目の思い出のタネでございます。
「{q2['question']}」——答えは『{q2['answer']}』でした。
お分かりになりますか。「{q2['hint']}」という言葉を思い出し、引き出しのなかを探してみてください。
そして最後は三つ目のタネ、「{q3['question']}」。
答えは『{q3['answer']}』でございます。
この三つを照らし合わせますと、その時代の生活感と、当時の道具や技術的な工夫までが具体的に見えてくるはずでございます。
そして最後に、{year}年の空気をまとったもうひと曲「{second_song[0]}」（{second_song[1]}）をお届けいたします。
肩の力を抜いて、鼻歌とともにお楽しみくださいましたら結構です。

### エンディング
あの頃の温かな思い出が、皆様の心に灯りともりますように。
読み上げる標語ではなく、どうか胸の静けさの中でゆっくり記憶をたどってみてください。
またお会いできる日を楽しみにしております。
以上、本日の回想法レクリエーションを終わります。ご参加ありがとうございました。"""

def generate_anniversary_script(year: int, month: int, day: int, target_name: str = "大切なあなた") -> str:
    """誕生日・記念日ギフト用の特別祝福原稿生成（セグメント構造）"""
    songs = _decade_songs(year, 2)
    first_song = songs[0]
    second_song = songs[1] if len(songs) > 1 else songs[0]
    program_sentence = _program_sentence(year, 2)

    return f"""### オープニング
皆様、特別な日のラジオ放送へようこそ。
本日ダイヤルを合わせましたのは、かけがえのない記念日、{year}年{month}月{day}日でございます。
{target_name}様、特別な記念日を心よりお祝い申し上げます。
本日のダイヤルは、{year}年という年を辿るためのものです。
この番組のテーマ曲とともに、{year}年へお迎えいたします。

### 記念日のエピソード1
この日、この世界にあなたが誕生されたとき、あるいはこの記念の日を迎えたとき、日本はどのような時代を迎えていたのでしょうか。
{year}年、街には新しい時代の息吹が満ち、人々は希望と笑顔にあふれておりました。
{program_sentence}。
{target_name}様の青春と重なる記憶に、どうかこの音を重ねてください。
あなたがこれまで歩んでこられた日々のすべての瞬間が、周囲の皆様へのあたたかな光となり、素晴らしい歴史を紡いでこられました。
それでは、この年の記念の一曲「{first_song[0]}」（{first_song[1]}）をお届けします。

### 記念日のエピソード2
今日という特別な日に、あなたが生まれた{year}年に日本中で愛されていた大ヒット曲を、心からの祝福の気持ちを込めてお送りいたします。
懐かしいメロディーとともに、これまでの歩みと、これからの素晴らしい日々に乾杯いたしましょう。
あの頃のヒット曲には、{second_song[0]}（{second_song[1]}）のように、心にまっすぐ届く歌声がありました。
誰かの青春のBGMともなっていた、せめてこの1曲だけは{target_name}様の声で歌ってください。
{first_song[0]}と{second_song[0]}をつなぎますと、{year}年という一年全体が、ひとつの音楽になって響いてくるはずです。
{year}年という年は、{target_name}様の歩みの背景音のようにずっと鳴りつづけております。
この一年が、健康とよろこびにあふれたものでありますよう、また、来年の今日にもよい思い出を積み上げていけるものでありますよう、心よりお祈り申し上げます。

### エンディング
{target_name}様、改めましておめでとうございます。
素晴らしい一年となりますよう、心よりお祈り申し上げます。
年を数えるのではなく、{year}年のこの季節に思いをはせる時間こそ、かけがえのないものです。
レトロラジオ・タイムマシン、{year}年の本日の放送はお開きでございます。ありがとうございました。"""


def _hist(title: str, pid: str, start: str, duration: int, description: str, source: str) -> ProgramSchedule:
    """歴史番組の生成（説明文は decade 単位の表現に統一し、対象年より後の年を書かない）"""
    return ProgramSchedule(
        id=pid,
        title=title,
        start_time=start,
        duration=duration,
        description=description,
        is_historical=True,
        source=source,
    )


# 歴史的なラジオ・テレビ番組データベース
# 介護施設の利用者は実際にその時代の番組を視聴していた。架空の番組名を
# 「その頃のお茶の間で流れていた」と断定的に語ると、時代の記憶と食い違う。
# そのため、ここには実在が確認できる番組だけを置く。
RADIO_PROGRAMS_BY_DECADE: dict[int, list["ProgramSchedule"]] = {
    1950: [
        _hist("NHKラジオ第一放送", "prog_1950_1", "06:00", 60,
              "1950年代に始まった朝のラジオ放送。ニュースと生活番組を中継しました", "1950年代"),
        _hist("料理教室", "prog_1950_2", "11:30", 30,
              "1950年代にNHK教育テレビで始まった料理番組。いまでも続く長寿番組です", "1950年代"),
    ],
    1960: [
        _hist("鉄腕アトム", "prog_1960_1", "19:00", 30,
              "1960年代にNETテレビで放送を始めたアニメ番組。子どもたちに夢中になりました", "1960年代"),
        _hist("サンデー・プロジェクト", "prog_1960_2", "22:00", 60,
              "1960年代にTBSで始まった洋楽番組。ロックを腰で聴いた時間でした", "1960年代"),
        _hist("8時だョ!全員集合", "prog_1960_3", "20:00", 90,
              "1960年代末にTBSで始まった深夜バラエティー番組。冗談や大会が楽しめました", "1960年代"),
    ],
    1970: [
        _hist("オールナイトニッポン", "prog_1970_1", "00:00", 180,
              "1970年代に文化放送で始まった深夜ラジオ番組。受験生の夜に寄り添いました", "1970年代"),
        _hist("スター誕生", "prog_1970_2", "19:00", 75,
              "1970年代に読売テレビで始まった新人発掘番組。歴史に残る名歌手も生まれました", "1970年代"),
        _hist("パックイン・ミュージック", "prog_1970_3", "00:00", 60,
              "1970年代に日本放送で始まった深夜音楽番組。眠りの友になりました", "1970年代"),
    ],
    1980: [
        _hist("ザ・ヒットパレード", "prog_1980_1", "19:00", 120,
              "1980年代にTBSで始まった歌謡曲番組。ヒットチャートが一家の話題でした", "1980年代"),
        _hist("歌謡パレード", "prog_1980_2", "19:30", 90,
              "1980年代にNETテレビで始まった歌謡曲番組。新人のチャートの並びが楽しみでした", "1980年代"),
        _hist("ユイ音楽園", "prog_1980_3", "18:00", 60,
              "1980年代にTBSで始まった音楽番組。軽快なトークが楽しめました", "1980年代"),
    ],
    1990: [
        _hist("ASAYAN", "prog_1990_1", "20:00", 120,
              "1990年代に日本テレビで始まった深夜音楽番組。紅白への出場を目指しました", "1990年代"),
        _hist("JAPAN COUNTDOWN", "prog_1990_2", "00:00", 90,
              "1990年代から2000年代にかけて放送された昭和歌謡番組。青春の歌を聴きました", "1990年代"),
    ],
    2000: [
        _hist("ニュースステーション", "prog_2000_1", "19:00", 60,
              "1990年代末から2000年代にかけて放送された夕方のニュース番組です", "2000年代"),
        _hist("ノイタミナA", "prog_2000_2", "00:00", 30,
              "1990年代末から2000年代にかけて放送された深夜アニメ枠番組です", "2000年代"),
    ],
    2010: [
        _hist("Sportacent", "prog_2010_1", "19:00", 30,
              "2010年代にNHKで始まったスポーツ番組。2020年代まで放送が続きました", "2010年代"),
        _hist("ニュース7", "prog_2010_2", "23:45", 30,
              "2010年代にNHKで放送されていた夜間のニュース番組。就寝前の習慣でした", "2010年代"),
    ],
    2020: [
        _hist("Sportacent", "prog_2020_1", "19:00", 30,
              "2010年代に始まったスポーツ番組。2020年代も放送されました", "2020年代"),
    ],
}

# 表示・読み上げテキストから年を取り出すためのパターン
_YEAR_IN_TEXT = re.compile(r"(1[5-9]\d{2}|20\d{2})")


def _mentions_future_year(schedule: ProgramSchedule, year: int) -> bool:
    """タイトルまたは説明に ``year`` より後の年の記載があるか。"""
    blob = f"{schedule.title} {schedule.description or ''}"
    return any(int(token) > year for token in _YEAR_IN_TEXT.findall(blob))


class HistoricalRadioPrograms:
    """Historical radio program database"""

    # Access module-level data via class attribute
    @classmethod
    def _get_radio_programs_by_decade(cls) -> dict[int, list["ProgramSchedule"]]:
        return RADIO_PROGRAMS_BY_DECADE

    @classmethod
    def _historical_pick(cls, year: int):
        """対象年に提示してよい歴史番組を1本、決定的に選ぶ"""
        decade_programs = cls._get_radio_programs_by_decade().get((year // 10) * 10) or []
        eligible = [p for p in decade_programs if not _mentions_future_year(p, year)]
        if not eligible:
            return None
        return eligible[year % len(eligible)]

    @classmethod
    def _modern_schedules(cls, year: int) -> list["ProgramSchedule"]:
        """現代的な番組枠（年依存のタイトルは ``year`` を正確に反映する）"""
        return [
            ProgramSchedule(
                id="modern_1",
                title="モーニングニュース",
                start_time="06:00",
                duration=30,
                description="最新のニュースと天気予報",
            ),
            ProgramSchedule(
                id="modern_2",
                title="文化の窓",
                start_time="10:00",
                duration=60,
                description="日本の文化と芸術",
            ),
            ProgramSchedule(
                id="modern_3",
                title=f"特集: {year}年の回想",
                start_time="13:00",
                duration=90,
                description=f"{year}年を振り返る、時代の記憶をたどる特別番組をお届けします",
            ),
            ProgramSchedule(
                id="modern_4",
                title="ヒット曲メドレー",
                start_time="19:00",
                duration=120,
                description="その年の代表曲をお届けします",
            ),
        ]

    @classmethod
    def get_program_guide(cls, year: int, month: int, day: int) -> ProgramGuide:
        """Generate program guide for specific date

        同じ入力なら常に同じ出力になること、未来の年を提示しないこと、
        同じタイトルを2回出さないことを保証する。
        """
        weekday_names = ["日", "月", "火", "水", "木", "金", "土"]
        try:
            weekday = weekday_names[datetime(year, month, day).weekday()]
        except ValueError:
            logger.warning(f"存在しない日付のため曜日を判定できません: {year}-{month}-{day}")
            weekday = "不明"

        today_highlight = "レトロラジオ・タイムマシン"
        schedules: list["ProgramSchedule"] = []
        used_titles: set = set()

        def _accept(schedule: "ProgramSchedule") -> None:
            if _mentions_future_year(schedule, year):
                logger.debug("未来の年を含む番組は提示しません: %s", schedule.title)
                return
            if schedule.title in used_titles:
                logger.debug("タイトル重複のため提示しません: %s", schedule.title)
                return
            used_titles.add(schedule.title)
            schedules.append(schedule)

        historical = cls._historical_pick(year)
        if historical is not None:
            _accept(historical)

        for schedule in cls._modern_schedules(year):
            _accept(schedule)

        return ProgramGuide(
            date=f"{year:04d}-{month:02d}-{day:02d}",
            weekday=weekday,
            schedules=schedules,
            today_highlight=today_highlight,
            special_events=[],
        )

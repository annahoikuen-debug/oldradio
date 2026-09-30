"""UI/UX 改善プロジェクト（Wave1 SubC）の契約テスト。

`plans/ui_ux_contract.md` の

- 章0「変更してはならない既存契約」（非回帰条件）
- 章5「SubC の担当: tests/test_ui_ux.py」

を pytest で固定するファイル。

方針は `tests/test_pwa_banner.py` と同じで、HTTP 経由ではなく
`static/index.html` / `static/app.css` / `static/app.js` を
`pathlib.Path` で直接読んで検査する。HTML パーサには依存せず
`re` と `pathlib` だけで判定する。

 Wave1 SubA（index.html）/ SubB（app.css）は実装済み、
 Wave1 SubD / SubE（app.js の新関数）は未実装のため
 `TestJsContract::test_new_functions_defined` などは
 Wave2 完了まで red になる（xfail にはしない）。
"""

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
STYLES = ROOT / "styles"
DESIGN_TOKENS = ROOT / "design_tokens"


# --- 読込ヘルパ ---------------------------------------------------------------
def _read(name: str) -> str:
    """static/ 配下のテキスト資産を読む（tests/test_pwa_banner.py と同じ）"""
    path = STATIC / name
    assert path.exists(), f"static/{name} が存在しない"
    return path.read_text(encoding="utf-8")


def _html() -> str:
    return _read("index.html")


def _css() -> str:
    return _read("app.css")


def _js() -> str:
    return _read("app.js")


# --- HTML 解析ヘルパ（HTML パーサに依存しない） -------------------------------
# 開始タグ（属性が複数行に跨いだものも 1 個のタグとして扱う）
_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")
# 属性名としての id（data-xxx-id 等の誤検出を防ぐ）
_ID_ATTR_RE = re.compile(r'(?<![\w-])id="([^"]+)"')


def _tags(html: str) -> list:
    """HTML 内のすべての開始タグを返す"""
    return _TAG_RE.findall(html)


def _class_token_pattern(class_name: str) -> re.Pattern:
    """class 属性のトークン区切りで class_name を完全一致させるパターン"""
    return re.compile(
        r'(?<![\w-])' + re.escape(class_name) + r'(?![\w-])',
    )


def _tags_with_class(html: str, class_name: str) -> list:
    """指定クラスを持つ開始タグの一覧（`decade-chips` に `decade-chip` を誤検出しない）"""
    pattern = _class_token_pattern(class_name)
    found = []
    for tag in _tags(html):
        for class_attr in re.findall(r'class="([^"]*)"', tag):
            tokens = [token for token in class_attr.split() if pattern.search(token)]
            if tokens:
                found.append(tag)
                break
    return found


def _tag_by_id(html: str, element_id: str):
    """id="element_id" を持つ開始タグを返す（無ければ None）"""
    needle = f'id="{element_id}"'
    for tag in _tags(html):
        if needle in tag:
            return tag
    return None


# --- CSS 解析ヘルパ -----------------------------------------------------------
def _media_blocks(css: str, condition: str) -> list:
    """`@media <condition> { ... }` のブロック本体（波括弧の途中）をすべて返す"""
    blocks = []
    for match in re.finditer(r"@media\s+" + condition + r"\s*\{", css):
        depth = 0
        start = match.end() - 1
        for index in range(start, len(css)):
            char = css[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(css[start + 1:index])
                    break
    return blocks


def _rules(css: str) -> list:
    """(セレクタ文字列, 宣言ブロック) のリストを返す（中では波括弧を使わないもの）"""
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def _is_defined(css: str, selector: str) -> bool:
    """セレクタ文字列として（擬似クラス・属性セレクタ込みで）定義されているか"""
    pattern = _class_token_pattern(selector)
    for selector_text, _body in _rules(css):
        for token in selector_text.split(","):
            if pattern.search(token):
                return True
    return False


def _hidden_selectors(css_chunks) -> list:
    """`display: none` を含むルールのセレクタをすべて集める"""
    hidden = []
    for chunk in css_chunks:
        for selector_text, body in _rules(chunk):
            if re.search(r"display\s*:\s*none", body):
                hidden.append(selector_text)
    return hidden


# --- JS 解析ヘルパ ------------------------------------------------------------
def _function_body(source: str, name: str) -> str:
    """`function name(...) { ... }` の本体を返す"""
    match = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", source)
    assert match, f"function {name}( が app.js に無い"
    depth = 0
    start = match.end() - 1
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1:index]
    raise AssertionError(f"function {name}() の閉じ括弧が見つからない（構文が壊れている）")


# 正規表現リテラル判定に使うキーワードと前置記号
_REGEX_KEYWORDS = {"return", "typeof", "case", "in", "of", "new", "delete", "void"}
_REGEX_PRECEDERS = set("(,=:[!&|?{};")


def _strip_js_noise(source: str) -> str:
    """コメント・文字列・正規表現リテラルを取り除き、括弧カウント用の素な文字列にする。

    app.js の `node --check` が無い環境でも構文の壊れを検知するための軽量検査。
    """
    out = []
    word = ""        # 進行中の識別子
    prev_token = ""  # 直前の非空白トークン（識別子 or 記号 1 文字）
    index = 0
    length = len(source)

    def emit(char: str) -> None:
        nonlocal word, prev_token
        if char.isspace():
            return
        if char.isalnum() or char in "_$":
            word += char
            return
        if word:
            prev_token = word
            word = ""
        prev_token = char

    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        # /* ... */ と // ...
        if char == "/" and nxt in "*/":
            if nxt == "*":
                end = source.find("*/", index + 2)
                index = length if end == -1 else end + 2
            else:
                end = source.find("\n", index)
                index = length if end == -1 else end
            continue
        # '...' "..." `...`
        if char in "'\"`":
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                if source[index] == "\n" and quote != "`":
                    break
                index += 1
            word = ""
            prev_token = "("
            continue
        # /regex/flags（前置トークンから判断する。`/"/g` のような引用符を含むため必須）
        # 直前のトークンは「書きかけの識別子 word」を優先する（`sum / n` を regex と誤判定しない）
        last = word or prev_token
        if char == "/" and (
            last in _REGEX_KEYWORDS
            or (last and last[-1] in _REGEX_PRECEDERS)
        ):
            index += 1
            in_class = False
            while index < length:
                current = source[index]
                if current == "\\":
                    index += 2
                    continue
                if current == "[":
                    in_class = True
                elif current == "]":
                    in_class = False
                elif current == "/" and not in_class:
                    index += 1
                    break
                elif current == "\n":
                    break
                index += 1
            while index < length and source[index].isalpha():
                index += 1
            prev_token = "/"
            continue
        out.append(char)
        emit(char)
        index += 1
    return "".join(out)


# --- 契約データ ----------------------------------------------------------------
# 章0-1: app.js の cacheDom() が参照する、削除・改名・move 禁止の既存 ID（32 個）
EXISTING_IDS = [
    "appIcon", "seniorToggle", "tabModeNormal",
    "tabModeCare", "tabModeAnniversary", "careModeBox",
    "anniversaryModeBox", "careRecreationDate", "anniversaryDate",
    "anniversaryName", "btnPlayRadio", "broadcastOutput",
    "streamStatusTitle", "streamStatusDesc", "btnAudioAction",
    "manuscriptTitle", "manuscriptBody", "btnToggleWriting",
    "btnPrintRecreation", "songTitle", "songArtist",
    "vinylDisk", "recreationQuizBox", "quizList", "tubeBulb",
    "vuMeter", "serviceStatus", "yearBadge", "eraText", "tunerRail",
    "tunerNeedle", "brassKnob",
]

# 章5-1: Wave1 で新設する ID（`.skip-link` はクラスなので別途 test_skip_link_points_to_main で見る）
NEW_IDS = [
    "mainContent",
    "decadeChips", "yearInput", "yearInputHint",
    "btnYearMinus10", "btnYearMinus1", "btnYearPlus1", "btnYearPlus10",
    "generationPanel", "progressTrack", "progressFill", "progressSteps",
    "generationElapsed", "btnCancelGeneration",
    "stateBanner", "stateBannerTitle", "stateBannerMessage", "stateBannerClose",
    "emptyState", "btnEmptyPlay", "btnShowGuide",
    "errorState", "errorStateTitle", "errorStateMessage",
    "errorStateDetail", "btnRetry", "btnToggleErrorDetail",
    "guideDialog", "btnGuideClose",
    "playerCard", "trackIndex", "trackTitle", "trackArtist",
    "seekBar", "seekCurrent", "seekDuration",
    "btnPrevTrack", "btnReplayTrack", "btnNextTrack", "btnMute",
    "volumeControl", "btnLoop", "playlistList",
]

# 章5-1: Wave2（SubD / SubE）で app.js に追加する関数
NEW_JS_FUNCTIONS = [
    "bindDecadeChips", "bindYearStepper", "startProgress", "stopProgress",
    "showStateBanner", "showEmptyState", "showErrorState", "openGuide",
    "bindPlayerControls", "renderPlayerUI", "renderPlaylist", "updateSeekBar",
    "formatTime",
]

# 章5-1: Wave2（SubD / SubE）の改写後も残す既存関数
EXISTING_JS_FUNCTIONS = [
    "setYear", "setMode", "startGeneration", "renderSuccess", "renderError",
    "renderQuiz", "buildQueue", "playIndex", "updateTrackMeta",
    "cancelGeneration", "cacheDom", "bindEvents", "init",
]

DECADES = ["1950", "1960", "1970", "1980", "1990", "2000", "2010", "2020"]

NEW_COMPONENT_CLASSES = [
    ".decade-chip", ".progress-fill", ".player-card", ".error-state",
    ".empty-state", ".state-banner", ".guide-dialog",
]

EXISTING_TOKENS = ["--color-brass-primary", "--color-bg", "--font-serif"]


# =============================================================================
# 1. index.html の契約
# =============================================================================
class TestIndexContract:
    """`static/index.html` の DOM 契約（章0-1 / 章1 / 章5-1）"""

    def test_existing_ids_are_preserved(self):
        """章0-1: 既存 ID 32 個がすべて同じ id="..." として残っている"""
        html = _html()
        missing = [
            element_id
            for element_id in EXISTING_IDS
            if f'id="{element_id}"' not in html
        ]
        assert not missing, f"既存 ID が消えている: {missing}"

    def test_new_ids_exist(self):
        """章5-1: Wave1 で新設した ID がすべて存在する"""
        html = _html()
        missing = [
            element_id
            for element_id in NEW_IDS
            if f'id="{element_id}"' not in html
        ]
        assert not missing, f"新設 ID が無い: {missing}"

    def test_skip_link_points_to_main(self):
        """スキップリンクが body 直下にあり #mainContent を指す"""
        html = _html()
        skip_links = _tags_with_class(html, "skip-link")
        assert skip_links, ".skip-link の要素が無い"
        assert skip_links[0].startswith("<a "), ".skip-link は <a> であるべき"
        assert 'href="#mainContent"' in skip_links[0], (
            'スキップリンクの href="#mainContent" が無い'
        )
        assert 'id="mainContent"' in html, 'id="mainContent" が無い'
        main_tag = _tag_by_id(html, "mainContent")
        assert main_tag is not None and main_tag.startswith("<main "), (
            "mainContent は <main> 要素であるべき"
        )

    def test_no_duplicate_ids(self):
        """HTML 全体で id="..." が重複していない（JS の getElementById が壊れる）"""
        html = _html()
        ids = _ID_ATTR_RE.findall(html)
        duplicated = sorted({value for value in ids if ids.count(value) > 1})
        assert not duplicated, f"重複した id がある: {duplicated}"

    def test_index_references_app_css_and_js(self):
        """章0-3: app.css / app.js への参照が維持されている"""
        html = _html()
        assert "/static/app.css" in html
        assert "/static/app.js" in html

    def test_manifest_reference(self):
        """章0-3: manifest.json への参照が維持されている"""
        assert "manifest.json" in _html()

    def test_theme_color_unchanged(self):
        """章1-1: theme-color は既存値 #221c18 のまま"""
        assert '<meta name="theme-color" content="#221c18">' in _html()


# =============================================================================
# 2. ARIA / アクセシビリティ契約
# =============================================================================
class TestAriaContract:
    """`static/index.html` の ARIA 契約（章1-4〜1-9 / 章5-1）"""

    def test_progressbar_has_role_and_bounds(self):
        """進捗バーが role="progressbar" と 3 つの aria-value を持つ"""
        html = _html()
        progressbar = _tag_by_id(html, "progressTrack")
        assert progressbar is not None, 'id="progressTrack" が無い'
        assert 'role="progressbar"' in progressbar
        for attr in ("aria-valuemin", "aria-valuemax", "aria-valuenow"):
            assert attr in progressbar, f"進捗バーに {attr} が無い"
        assert 'id="progressFill"' in html, "進捗バーの塗り（#progressFill）が無い"

    def test_brass_knob_is_slider(self):
        """ダイヤルノブがキーボード操作できる slider として公開されている"""
        html = _html()
        knob = _tag_by_id(html, "brassKnob")
        assert knob is not None, 'id="brassKnob" が無い'
        assert 'role="slider"' in knob
        for attr in ("aria-valuemin", "aria-valuemax", "aria-valuenow"):
            assert attr in knob, f"#brassKnob に {attr} が無い"
        assert 'tabindex="0"' in knob, "#brassKnob がキーボードで到達できない"

    def test_year_input_is_numeric(self):
        """#yearInput が inputmode="numeric"（モバイルの数字キーボード）を持つ"""
        html = _html()
        year_input = _tag_by_id(html, "yearInput")
        assert year_input is not None, 'id="yearInput" が無い'
        assert 'inputmode="numeric"' in year_input
        assert 'aria-describedby="yearInputHint"' in year_input, (
            "#yearInput がヒント（#yearInputHint）と結び付いていない"
        )

    def test_error_state_has_alert_role(self):
        """エラー状態がスクリーンリーダーに即時通知される"""
        html = _html()
        error_state = _tag_by_id(html, "errorState")
        assert error_state is not None, 'id="errorState" が無い'
        assert 'role="alert"' in error_state

    def test_toggle_buttons_have_aria_pressed(self):
        """#btnMute / #btnLoop が状態を持つトグルボタンとして aria-pressed を持つ"""
        html = _html()
        for element_id in ("btnMute", "btnLoop"):
            tag = _tag_by_id(html, element_id)
            assert tag is not None, f'id="{element_id}" が無い'
            assert "aria-pressed" in tag, f"#{element_id} に aria-pressed が無い"

    def test_dialog_has_labelledby(self):
        """ガイドダイアログがタイトルと結び付いている"""
        html = _html()
        dialog = _tag_by_id(html, "guideDialog")
        assert dialog is not None, 'id="guideDialog" が無い'
        assert dialog.startswith("<dialog "), "#guideDialog は <dialog> であるべき"
        assert 'aria-labelledby="guideDialogTitle"' in dialog
        assert 'id="guideDialogTitle"' in html, "ガイドのタイトル ID が無い"

    def test_decade_chips_are_buttons(self):
        """年代チップがすべて type="button" 付きの <button> で data-decade を持つ"""
        html = _html()
        chips = _tags_with_class(html, "decade-chip")
        assert chips, ".decade-chip の要素が無い"
        for chip in chips:
            assert chip.startswith("<button "), f"年代チップが <button> ではない: {chip}"
            assert 'type="button"' in chip, f"年代チップに type=\"button\" が無い: {chip}"
            assert "data-decade=" in chip, f"年代チップに data-decade が無い: {chip}"

    def test_decade_chips_cover_all_decades(self):
        """年代チップが 1950s〜2020s の 8 個を網羅している"""
        html = _html()
        chips = _tags_with_class(html, "decade-chip")
        found = []
        for chip in chips:
            match = re.search(r'data-decade="(\d{4})"', chip)
            assert match, f"data-decade の値が不正: {chip}"
            found.append(match.group(1))
        assert sorted(found) == sorted(DECADES), (
            f"年代チップの値が違う: {found}（期待: {DECADES}）"
        )
        container = _tag_by_id(html, "decadeChips")
        assert container is not None and 'role="group"' in container, (
            "#decadeChips が role=\"group\" としてまとまっていない"
        )

    def test_all_buttons_have_type(self):
        """すべての <button> が type 属性を持つ（HTML5 の既定は submit のため）"""
        html = _html()
        missing = [tag for tag in _tags(html)
                   if tag.startswith("<button") and "type=" not in tag]
        assert not missing, "type 属性が無い <button> がある:\n" + "\n".join(missing)


# =============================================================================
# 3. app.css の契約
# =============================================================================
class TestCssContract:
    """`static/app.css` のスタイル契約（章2 / 章5-1）"""

    def test_sr_only_class_defined(self):
        """.sr-only が定義済み（app.js が live region 生成に使う）"""
        assert _is_defined(_css(), ".sr-only"), ".sr-only の定義が無い"

    def test_focus_visible_rule_defined(self):
        """キーボード操作時のフォーカスリングが定義されている"""
        assert _is_defined(_css(), ":focus-visible"), ":focus-visible の定義が無い"

    def test_new_component_classes_defined(self):
        """Wave1 で新設した主要コンポーネントのスタイルがすべて定義されている"""
        css = _css()
        missing = [selector for selector in NEW_COMPONENT_CLASSES
                   if not _is_defined(css, selector)]
        assert not missing, f"スタイル未定義のクラス: {missing}"

    def test_existing_tokens_not_broken(self):
        """章2-1: 既存 CSS 変数（背景・真鍮・セリフ体）が消えていない"""
        css = _css()
        missing = [token for token in EXISTING_TOKENS
                   if not re.search(re.escape(token) + r"\s*:", css)]
        assert not missing, f"消えた CSS 変数: {missing}"

    def test_reduced_motion_block_exists(self):
        """モーション低減（prefers-reduced-motion）のメディアクエリが存在する"""
        css = _css()
        assert re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)", css), (
            "@media (prefers-reduced-motion: reduce) が無い"
        )

    def test_prefers_contrast_block_exists(self):
        """高コントラスト時の補強（色だけに依存しない表示）が存在する"""
        css = _css()
        assert re.search(r"@media\s*\(prefers-contrast:\s*more\)", css), (
            "@media (prefers-contrast: more) が無い"
        )

    def test_print_hides_new_components(self):
        """章2-7: 印刷時に新しいコンポーネントが非表示になる"""
        print_blocks = _media_blocks(_css(), r"print")
        assert print_blocks, "@media print ブロックが無い"
        hidden = _hidden_selectors(print_blocks)
        for selector in (".generation-panel", ".player-card"):
            assert any(selector in rule for rule in hidden), (
                f"@media print で {selector} が非表示になっていない"
            )

    def test_css_braces_balanced(self):
        """CSS 全体の波括弧が均衡している（編集中に壊れていない）"""
        css = _css()
        assert css.count("{") == css.count("}"), (
            f"波括弧が不一致: {{ = {css.count('{')}, }} = {css.count('}')}"
        )

    def test_legacy_live_stream_bar_removed_from_print_hide(self):
        """章2-7: 廃止した .live-stream-bar の hide は #playerCard に置き換わっている"""
        print_blocks = _media_blocks(_css(), r"print")
        assert print_blocks, "@media print ブロックが無い"
        hidden = _hidden_selectors(print_blocks)
        assert not any(".live-stream-bar" in rule for rule in hidden), (
            "印刷 hide に廃止済みの .live-stream-bar が残っている"
        )
        assert any("#playerCard" in rule for rule in hidden), (
            "印刷 hide に #playerCard が無い（原稿のみ A4 印刷の挙動が崩れる）"
        )


# =============================================================================
# 4. app.js の契約
# =============================================================================
class TestJsContract:
    """`static/app.js` の契約（章0-3 / 章3 / 章4 / 章5-1）"""

    def test_app_js_braces_balanced(self):
        """括弧が均衡している（編集中に構文が壊れていない）"""
        source = _strip_js_noise(_js())
        for opening, closing in (("{", "}"), ("(", ")"), ("[", "]")):
            assert source.count(opening) == source.count(closing), (
                f"{opening}{closing} が不一致: {opening} = {source.count(opening)}, "
                f"{closing} = {source.count(closing)}"
            )

    def test_existing_function_names_preserved(self):
        """既存関数が Wave2 の改写後も消えていない"""
        js = _js()
        missing = [
            name for name in EXISTING_JS_FUNCTIONS
            if not re.search(r"function\s+" + re.escape(name) + r"\s*\(", js)
        ]
        assert not missing, f"消えた既存関数: {missing}"

    def test_new_functions_defined(self):
        """Wave2（SubD / SubE）が追加する関数が定義されている"""
        js = _js()
        missing = [
            name for name in NEW_JS_FUNCTIONS
            if not re.search(r"function\s+" + re.escape(name) + r"\s*\(", js)
        ]
        assert not missing, f"未実装の関数: {missing}"

    def test_pwa_strings_preserved(self):
        """章0-3: PWA / API 関連の文字列が維持されている"""
        js = _js()
        for needle in ("navigator.serviceWorker.register", "/api/generate",
                       "/api/audio/", "toUpperCase()"):
            assert needle in js, f"app.js に {needle} が無い"

    def test_no_streamlit(self):
        """章0-3: 廃止済みの Streamlit 記述が復活していない"""
        assert "streamlit" not in _js().lower()

    def test_existing_dom_ids_cached(self):
        """章0-1: cacheDom() が既存 ID 32 個を byId() でキャッシュし続ける"""
        cache = _function_body(_js(), "cacheDom")
        missing = [
            element_id for element_id in EXISTING_IDS
            if f"byId('{element_id}')" not in cache
        ]
        assert not missing, f"cacheDom() から消えた ID: {missing}"

    def test_progress_capped_before_success(self):
        """章3-2/3-8: 進捗は応答受信まで 95% で頭打ち（100% は renderSuccess のみ）"""
        js = _js()
        match = re.search(r"PROGRESS_MAX_PERCENT\s*=\s*(\d+)", js)
        assert match, "PROGRESS_MAX_PERCENT が定義されていない"
        assert int(match.group(1)) <= 95, (
            f"PROGRESS_MAX_PERCENT = {match.group(1)} は 95 以下であるべき"
        )


# =============================================================================
# 5. バックエンド / 既存資産の非回帰
# =============================================================================
class TestNoBackendRegression:
    """章0-3: UI 改修によって静的資産・デザイントークンが変わっていない"""

    def test_static_manifest_unchanged(self):
        """manifest.json の start_url は "/" のまま（サービスワーカー登録と整合）"""
        manifest = json.loads(_read("manifest.json"))
        assert manifest["start_url"] == "/", (
            f"start_url が {manifest['start_url']!r} に変わっている"
        )

    def test_generated_css_untouched(self):
        """styles/generated.css（生成物）が存在し、トークン定義の :root を持つ"""
        path = STYLES / "generated.css"
        assert path.is_file(), "styles/generated.css が無い"
        assert ":root" in path.read_text(encoding="utf-8")

    def test_design_tokens_untouched(self):
        """design_tokens/primitives/color.json のブランド色が元のまま"""
        tokens = json.loads(
            (DESIGN_TOKENS / "primitives" / "color.json").read_text(encoding="utf-8")
        )
        value = tokens["properties"]["color-brand-500"]["value"]
        assert value == "#d47300", f"color-brand-500 が {value} に変わっている"

    def test_brand_600_untouched(self):
        """design_tokens/primitives/color.json の color-brand-600 が元のまま"""
        tokens = json.loads(
            (DESIGN_TOKENS / "primitives" / "color.json").read_text(encoding="utf-8")
        )
        value = tokens["properties"]["color-brand-600"]["value"]
        assert value == "#b86200", f"color-brand-600 が {value} に変わっている"

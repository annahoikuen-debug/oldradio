# 評価結果の置き場所

## 自動指標

`python -m eval --out eval/results/YYYYMMDD.json` で日次の自動指標の結果を書き出す。

```bash
python -m eval --offline --out eval/results/$(date +%Y%m%d).json
```

- 既定の `--script-source deterministic`（=`--offline`）はネットワークを触らない決定的モード。CI/再現用途では **必ず `--offline` を付ける**。
- 実サービス（Gemini）の品質確認は `--script-source gemini` で人手実行し、結果が非決定的（ネットワーク/モデル依存）であることを `notes` に記録する。CI のブロッキングゲートには載せない。
- 日次のファイルは **gitignore してよい**（ベースラインが確立したら月次に集約する）。**月次のファイルはコミットする**（傾向を追うため）。

## Best-Worst 人間評価

`eval/bww/README.md` の手順書で評価し、**月 1 ファイル**で保存する。

```
eval/results/YYYYMM.json
```

フォーマットは手順書 5 節の例を参照（`schema_version` / `period` /
`rubric_version` / `raters` / `conditions` / `sessions` / `notes`）。

**人手が必要なため CI には載せない。** 募集と集計は `eval/bww/README.md` 6 節の
運用ルール（**プロンプトを変えるたびに 2 回以上回す**）に従う。

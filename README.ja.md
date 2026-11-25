# kakomon_agent

[中文](README.md) | [English](README.en.md) | **日本語**

日本の大学院入試（情報系）の過去問学習アシスタントです。手元の過去問（PDF・写真）を Claude で知識点ごとにタグ付けした問題データベースに整理し、[Claude Code](https://claude.com/claude-code) 上で MCP サーバーとスキルとして演習できます。

> このリポジトリにはコードのみが含まれ、過去問や過去問から生成したデータは含まれません（著作権は各大学に帰属します）。問題データベースはご自身の過去問から生成してください。

## 機能

- **出題**：1 回に 1 問ずつ、知識点ごとに易しい問題から出題。新しい問題がなくなると関連する知識点へ自動で広げます。復習期限の来た問題を優先します（FSRS）。
- **記録**：小問ごとに「できた / 一部できた / できなかった」、ヒントの回数、所要時間を記録。習熟度は Elo で計算し、忘却による減衰も反映します。
- **問題探し**：大学ごとの頻出分野の集計（出題年度と問題数）、類題検索、説明文からの段階的な検索（コードが候補を集め、Claude が選びます）。
- **解説**：問題データベースに収録された解答を優先します。ない場合は Claude が解答を書き、sympy / numpy で検算したうえで AI 解答として保存し再利用します（`data/solutions/<問題ID>.ai.md`）。人間の解答は `<問題ID>.human.md` に置けば AI 解答より優先されます。
- **問題パネル**：ローカルのブラウザで数式と図を表示します。複数の問題と解答をまとめて表示できます。
- **目標とレポート**：試験までのカウントダウン、学習計画、週次レポート、学習プロフィール。

## 構成

| パス | 内容 |
|---|---|
| `pipeline/` | データパイプライン：ページ単位の文字起こし → 問題の組み立て → 解答・正誤表の紐づけ → タグ付け → 取り込み（既定で Message Batches API を使用） |
| `agent/` | MCP サーバー（23 ツール）、学習ロジック、テスト |
| `.claude/skills/kakomon-tutor/` | 演習の流れ（スキル） |
| `web/` | ローカル Web 画面：対話、問題エリア（問題 / 原本 / 解答）、学習状況、学習概要ページ |
| `data/taxonomy/` | 知識点の体系と別名（287 項目） |
| `data/viewer/` | 問題パネルのページ |

## 使い方

1. 依存パッケージをインストールします（Python 3.12）：

   ```bash
   python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
   ```

2. 過去問を `kakomon/<フォルダ>/` に置きます（PDF、または 1 試験につき 1 フォルダの写真）。大学・研究科・専攻は最上位のフォルダ名から判定します。対応表は `pipeline/build_manifest.py` の `FOLDER_INFO` にあるので、ご自身のフォルダ構成に合わせて書き換えてください。
3. プロジェクト直下に `.env` を作り、`claude-key=<Anthropic API キー>` と 1 行書きます。
4. 問題データベースを作ります（費用がかかります。まず `estimate` で見積もり、`submit` では `--budget` で上限を設定してください）：

   ```bash
   .venv/bin/python -I pipeline/build_manifest.py
   .venv/bin/python -I pipeline/normalize_manifest.py
   .venv/bin/python -I pipeline/run_full.py render --docs all
   .venv/bin/python -I pipeline/run_full.py estimate --docs all
   ```

   続いて `transcribe` → `structure` → `tag` の順に、各段階で `submit --stage ... --docs all`、`wait`、`collect` を実行し、最後に `load` で取り込み、`qa` で検収レポートを出します。各コマンドは `pipeline/run_full.py` 冒頭の説明を参照してください。費用の目安：約 600 試験（約 7,000 ページ）の全量処理で約 80 ドル（Sonnet、Batch API）。

   すでにテキストになっている過去問（1 問 1 ファイルの Markdown、例：[The Kai Project](https://github.com/Myyura/the_kai_project)）は転写と問題の組み立てを省けます。`kakomon/<フォルダ>/` に置いて `index.csv`（列：`path, university, graduate_school, program, admission_year, exam_held, paper_title`）を書き、`run_full.py markdown` を実行してから `tag` 段階と `load` だけを行います。1 問あたり約 0.008 ドル。
5. 学習を始めます。次のどちらでも使えます：
   - **Web 画面**：左に対話、中央に問題（数式を表示、原本を並べて表示）と解答、右に習熟度・学習の傾向・復習予定があり、学習概要ページもあります。

     ```bash
     .venv/bin/python -I web/server.py
     ```

     <http://localhost:18770/> を開きます。各メッセージはローカルの Claude Code がバックグラウンドで処理します（インストールとログインが必要です）。既定は Sonnet で、対話欄の上で Opus に切り替えられます。
     初回起動時に学習用フォルダ `~/kakomon-study` を自動で作ります。本番の学習記録に触れずに試すときは `--sandbox ~/kakomon-try` を付けてください。
   - **ターミナルの Claude Code**：

     ```bash
     .venv/bin/python -I agent/setup_study.py ~/kakomon-study
     ```

     その後 `cd ~/kakomon-study && claude` を実行し、「線形代数の固有値を練習したい」のように話しかけます。問題パネル：<http://localhost:18766/viewer/>。

## テスト

- `agent/test_learner.py`：合成した問題データベースでの複数日シミュレーション。実データは不要です。
- `agent/smoke_test.py`：MCP プロトコル経由で全ツールを呼び出します。作成済みの問題データベースが必要です。
- `agent/dialogue_check.py`：headless の Claude Code で決まった 3 往復の対話を実行し、回答を検査します。費用がかかります（Sonnet で約 1 ドル）。

## ライセンス

[MIT](LICENSE)

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 認証情報の取り扱い

`credentials/` 配下のファイル（サービスアカウントキー）の**中身は読まない**。
存在確認・パス参照までにとどめる。鍵の生成・要求もしない。認証設定はユーザー本人が行う。

`git push` はユーザーの確認を得てから実行する（コミットまでは自律的に進めてよい）。
公開リポジトリ（junya34/shirugpt）のため、`.gitignore` の除外が効いていることを
鍵ファイルの配置前に確認する。

## コマンド

```bash
# セットアップ
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # GCP_PROJECT_ID などを設定

# 起動（利用者認証が無いのでループバックに限定する）
.venv/bin/streamlit run app.py --server.address 127.0.0.1

# 構文チェック
.venv/bin/python -m py_compile app.py src/*.py
```

自動テストスイートは未整備。検証はスクラッチのスクリプトで行っている。
`src/sql_guard.py` は GCP 認証なしで単体検証できるので、SQL 解析まわりを触ったら
`check_sql()` を直接呼ぶ使い捨てスクリプトで確認してから進めること。
BigQuery / Vertex AI に触る変更は `.env` と鍵が揃っている環境でのみ検証できる。

## アーキテクチャ

Streamlit の UI（`app.py`）が Gemini の Function Calling ループ（`src/agent.py`）を回し、
ループが BigQuery ツール（`src/bq_tools.py`）を呼ぶ。SQL は実行前に必ず
`src/sql_guard.py` を通り、結果は `src/summarize.py` で圧縮されてから Gemini に戻る。

### 中心的な不変条件: Gemini 境界を越えるものを絞る

このプロジェクトの設計はほぼこの一点に集約される。**壊さないこと。**

| データ | Gemini に渡す | UI に出す |
|---|---|---|
| データセット/テーブル一覧 | ○ | — |
| スキーマ | 明示的に要求された 1 テーブル分のみ | — |
| クエリ結果 | 要約のみ（行数・列と型・統計・先頭 N 行） | DataFrame 全体 |

- 全テーブルのスキーマを一括でプロンプトに載せてはいけない。
  `list_tables` → `get_table_schema` の 2 段階を維持する。
- `run_query` の tool response に生の行データを混ぜてはいけない。
  `QueryRun.dataframe` は UI 専用、`QueryRun.summary` だけが Gemini 向け。
- 取得済みスキーマと実行済み SQL は `ToolContext` にキャッシュされる。
  同一会話での再取得・再課金を防いでいる。
- `src/charts.py` のグラフ推定も**サーバー側で完結させる**。列の dtype だけで
  種類と軸を決めており、Gemini には一切問い合わせない。グラフのために
  データやスキーマを Gemini 境界の向こうへ送らないこと。

### 利用ログも Gemini 境界の外に置く

`src/usage_log.py` が利用者ごとのトークン量・クエリ量を BigQuery に記録する。
このテーブルには**全利用者の使用量**が入るため、Gemini から見えると
他人の利用状況が自然言語質問経由で漏れる。防御は 3 重:

1. `BQ_LOG_DATASET` は `BQ_ALLOWED_DATASETS` に**含めない**。
   `list_datasets` は allowlist を列挙するだけなので、Gemini には名前すら見えない
2. 仮に名前を推測されても `sql_guard` が allowlist 外の参照を拒否する
3. 混入は `config.load_settings()` が**起動時に `ConfigError` で落とす**
   （運用ミスがサイレントな漏洩ではなくデプロイ時の失敗になる）

**`BQ_LOG_DATASET` を allowlist に足す変更を入れてはいけない。**

書き込みは `insert_rows_json()` による JSON 行の直接挿入で、文字列 SQL を
組み立てない（`sql_guard` を通す経路とは無関係）。`UsageLogger.log_*` は
**例外を外に漏らさない契約**。ログが書けなくてもチャットは止めない。
連続失敗が続く場合はプロセス内で記録を諦める（毎イベントで失敗する
API 往復を繰り返すとチャットの応答が遅くなるため）。

**利用者メールの取得元に注意。** Streamlit 1.42 以降、Community Cloud の
閲覧者制限（Private + 許可メール）だけではアプリに閲覧者のメールが渡らない
（`st.user` は空のまま）。`app.py` の `resolve_viewer_email()` は自前で設定した
`st.login()`（Google OIDC、`secrets.toml` の `[auth]`）に依存している。
`auth_configured()` が `False`（`[auth]` 未設定、主にローカル開発）のときは
ログインゲート自体をスキップする仕様なので、「メールが空＝バグ」ではない。

管理者ページ（`src/admin_page.py`）のアクセス制御は、`st.navigation` から
ページを外すだけに頼らず、**`render_admin_page()` の冒頭で毎回**
閲覧者メールを `ADMIN_EMAILS` と照合する。URL 直打ちを防ぐのはこちらが主防御。

### 中断と再開（確認フロー）

dry run の推定スキャン量が閾値を超えると、ループは**その場で止まって UI に戻る**。
3 ファイルにまたがるので、触る前に流れを把握すること。

1. `bq_tools.run_query()` が `ConfirmationRequired` を送出
2. `agent._run_query()` が `_Pending` に包み直し、`agent.run()` が捕捉
3. `ctx.resume_calls` に処理中の function call 群を退避し `ConfirmationPending` を返す
4. `app.render_confirmation()` がボタンを描画し、`ctx.decisions[key]` に承認/拒否を記録
5. 再度 `agent.run()` を呼ぶと `ctx.resume_calls` から**同じ地点で再開**する

再開時は退避した function call 群を**先頭から再ディスパッチする**。これが安全なのは
ツールが冪等だから: 一覧取得とスキーマ取得は読み取り専用で、`run_query` は SQL の
ハッシュで `ctx.query_cache` を引くため再実行・再課金が起きない。
**この冪等性を壊すとユーザーが承認した瞬間に二重課金が発生する。**

承認/拒否のキーは Gemini が送ってきた元の SQL のハッシュ。`sql_guard` が LIMIT を
付与した後の SQL ではない（再開時に同じ引数で再ディスパッチされるため）。

### SQL ガード

`check_sql()` は自前のスキャナでコメントと文字列リテラルを同じ長さの空白に潰してから
解析する。位置と長さを保つのは、元 SQL のオフセットを崩さないため。
検査は 2 種類のマスクを使い分ける:

- キーワード検査用: 文字列 **と** バッククォート識別子の両方をマスク
  （`更新日` のような列名を DML キーワードと誤検出しないため）
- 参照抽出用: バッククォートの中身は残す（テーブルパスを読むため）

判定の要は「先頭トークンが SELECT / WITH であること」と「複文でないこと」の 2 つ。
危険キーワードのリストは多重防御であって主防御ではない。

**1 要素のテーブル参照（`FROM foo`）は意図的に無視している。** CTE やエイリアス、
`EXTRACT(YEAR FROM col)` の誤検出を避けるため。安全性は別経路で担保している:
クエリジョブに default dataset を設定していないので、修飾なしの参照は BigQuery 側で
必ず失敗する。**`QueryJobConfig` に `default_dataset` を足すとこの防御が崩れる。**

### エラーの二経路

同じ例外を宛先ごとに変換し分ける。

- `friendly_error(exc)` → UI 向けの平易な日本語
- `technical_detail(exc)` → Gemini 向け。構文エラーの内容などをそのまま渡し、
  自力で SQL を修正させる

ツール実行中の例外は `agent._dispatch()` が捕捉して `{"error": ...}` として
tool response に載せる。ループは止めない（Gemini に再試行させる）。

失敗した `run_query` の SQL と `technical_detail()` は、`_dispatch()` が
`_error_report()` で1つの文字列にまとめて `ctx.turn_errors` にも積む。これは
チャット画面の「診断情報」expander（`app.py`）に表示され、**ユーザーにも見える**。
UI向けの `friendly_error` を平易にする方針は変わらないが、SQL・技術的詳細は
診断情報として出す、という現在の仕様。

## 日本語データを扱う上の注意

対象データセット `202506` にはテーブル名・列名が日本語のものが多数ある
（`来店`、`日報`、`月次データ` 等）。

- データセット名 `202506` は数字始まりなので、修飾なしでは書けない。常にバッククォート。
- **`AS` のエイリアスもバッククォートが要る。** BigQuery は裸の日本語識別子を
  `Illegal input character` で拒否する。`SELECT COUNT(*) AS 件数` は構文エラー、
  `` AS `件数` `` なら通る。実データ検証で踏んだ既知の落とし穴で、
  `agent.SYSTEM_INSTRUCTION` に明示的な指示として入っている。
- `summarize._jsonable()` は numpy スカラーを Python の値に戻してから JSON 化する。
  ここを飛ばすと `bool` 列が `"True"` という文字列で Gemini に渡る。

## 設定

プロジェクト ID・データセット名・閾値はすべて `.env` 由来（`src/config.py`）。
**コードにハードコードしない。** テーブル名やカラム構成も埋め込まず、実行時に
`list_tables` / `get_table_schema` で動的取得する。

アクセス範囲は `BQ_ALLOWED_DATASETS` の allowlist で制限され、`sql_guard` が
SQL 解析段階で allowlist 外のデータセットと別プロジェクトへの参照を拒否する。

## スコープ

ロジック検証用のプロトタイプ。**Streamlit Community Cloud に限定公開している**
（アプリを Private にし、許可したメールアドレスの Google ログインのみ閲覧可）。
利用者の識別はこの Streamlit Cloud の閲覧者制限に完全に依存しており、
アプリ自身は認証機構を持たない。

Cloud Run 等への移行、レート制限、アプリ独自の利用者認証・権限管理
（利用者ごとに見えるデータセットを変える等）、Next.js への移行は次フェーズ。
これらの作り込みを先回りして提案しない。

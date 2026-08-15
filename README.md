# ShiruGPT

BigQuery のデータを日本語で質問すると、Gemini（Vertex AI）が自律的に SQL を生成・実行し、
結果を要約して自然言語で回答する Streamlit アプリです。

> **現在はロジック検証用のプロトタイプです。** 本番公開（Cloud Run へのデプロイ、
> 利用者認証、レート制限、複数ユーザー同時利用）は次フェーズのスコープです。

---

## 動作の流れ

```
ユーザーの質問（st.chat_input）
        │
        ▼
   Gemini（Function Calling）
        │  ①どのテーブルがあるか      → list_datasets / list_tables
        │  ②使うテーブルのスキーマだけ → get_table_schema
        │  ③SQL を組み立てて実行      → run_query
        ▼
   BigQuery ──► pandas DataFrame（サーバー側で保持）
        │
        ├─ 要約（行数・列と型・統計・先頭数行）→ Gemini へ返す
        └─ フルの結果 ───────────────────────► Streamlit に直接表示
        │
        ▼
   Gemini が日本語で回答 ＋ 実行 SQL を expander で表示
```

**生の全行データは Gemini に渡しません。** スキーマも必要なテーブル分だけを
段階的に取得します。これによりトークン消費を抑えています。

## 結果の表示

クエリ結果は「グラフ」「表」の 2 タブで表示されます。

グラフの種類は結果の列構成から自動で決まります。

| 結果の形 | 既定のグラフ |
|---|---|
| 日時列 ＋ 数値列 | 折れ線 |
| カテゴリ列 ＋ 数値列 | 棒グラフ |
| 数値列 ＋ 数値列 | 散布図 |
| 上記に当てはまらない | 表のみ |

種類・X 軸・Y 軸は画面上で変更できます。棒グラフは SQL の `ORDER BY` の並び順を
保持します。**この判定はすべてサーバー側で行い、Gemini には問い合わせません。**
グラフのためにデータを Gemini へ送らないための設計です。

---

## セットアップ

### 1. 依存パッケージ

Python 3.11 以上が必要です。

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

### 2. GCP 側の準備

対象プロジェクトで以下の API を有効化してください。

```bash
gcloud services enable bigquery.googleapis.com aiplatform.googleapis.com --project <プロジェクトID>
```

> **API 名称について**: Gemini 呼び出しに使う API は、GCP コンソール上では
> **Agent Platform API**（旧 Vertex AI API）と表示されます。サービス名は
> `aiplatform.googleapis.com` のままなので、上記コマンドはそのまま使えます。
> 有効化直後は反映に数分かかることがあります。

実行者に必要なロール:

| 用途 | ロール |
|---|---|
| BigQuery のデータ読み取り | `roles/bigquery.dataViewer` |
| クエリジョブの実行 | `roles/bigquery.jobUser` |
| Gemini の呼び出し | `roles/aiplatform.user`（Vertex AI ユーザー） |

### 3. 認証

認証方式は 2 つあります。**ローカル開発では方式 A を推奨します。**

#### 方式 A: Application Default Credentials（推奨）

```bash
gcloud auth application-default login
```

```bash
gcloud auth application-default set-quota-project <プロジェクトID>
```

`.env` の `GOOGLE_APPLICATION_CREDENTIALS` は**空のまま**にしてください。

#### 方式 B: サービスアカウントキー

鍵ファイルを `credentials/` 配下に置き、`.env` にそのパスを書きます。

```
GOOGLE_APPLICATION_CREDENTIALS=credentials/gcp-secret-key.json
```

`credentials/` と `*-key.json` などは `.gitignore` 済みです。
起動時にも「鍵ファイルが git 管理外になっているか」を自動チェックし、
除外されていない場合は画面上に警告を表示します。

#### なぜ ADC を推奨するのか

| 観点 | ADC | サービスアカウントキー |
|---|---|---|
| 実体 | OS のユーザー領域に置かれる短命トークン。自動更新される | **秘密鍵ファイルそのもの**。既定では無期限 |
| 漏洩したら | `gcloud auth application-default revoke` で即失効 | 鍵を無効化するまで悪用可能。**リポジトリに push したら即ローテーション必須** |
| 監査ログ | 個人アカウント名義で記録され、誰の操作か追える | サービスアカウント名義になり実行者を特定しづらい |
| 適する場面 | ローカル開発 | サーバー運用（ただし本来は鍵なしの SA アタッチが最善） |

鍵ファイルは「持ち出せる資産」になるため、`.gitignore` 漏れ・チャット共有・
バックアップ経由での流出が事故の典型です。**公開リポジトリを使う本プロジェクトでは
ADC のほうがリスクがはるかに小さくなります。**

### 4. `.env` の作成

```bash
cp .env.example .env
```

| 変数 | 既定値 | 説明 |
|---|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | （空） | SA キーのパス。空なら ADC を使用 |
| `GCP_PROJECT_ID` | — | 対象 GCP プロジェクト ID（必須） |
| `BQ_LOCATION` | （空） | データセットのロケーション（例 `US`, `asia-northeast1`）。空なら自動 |
| `BQ_DEFAULT_DATASET` | `202506` | 既定のデータセット |
| `BQ_ALLOWED_DATASETS` | `202506` | **アクセスを許可するデータセットのallowlist**（カンマ区切り） |
| `VERTEX_LOCATION` | `us-central1` | Gemini を呼ぶリージョン（Agent Platform / 旧 Vertex AI） |
| `GEMINI_MODEL` | `gemini-2.5-flash` | 使用モデル |
| `GEMINI_THINKING_BUDGET` | （空） | 思考トークン予算。空でモデル既定、`0` で無効 |
| `DRY_RUN_CONFIRM_BYTES` | `1073741824`（1GB） | 推定スキャン量がこれを超えたら実行前に確認 |
| `MAX_BYTES_BILLED` | `10737418240`（10GB） | BigQuery に渡す課金上限 |
| `DEFAULT_ROW_LIMIT` | `1000` | 外側に `LIMIT` が無い場合に自動付与する行数 |
| `MAX_RESULT_ROWS` | `5000` | UI に読み込む最大行数 |
| `SAMPLE_ROWS` | `15` | Gemini に渡すサンプル行数 |
| `MAX_TOOL_ITERATIONS` | `12` | 1 ターンあたりの tool 呼び出し上限 |

### 5. 起動

```bash
.venv/bin/streamlit run app.py --server.address 127.0.0.1
```

ブラウザで `http://localhost:8501` が開きます。

> `--server.address 127.0.0.1` を付けないと、Streamlit は既定で全ネットワーク
> インターフェースを待ち受け、同一 LAN 上の他端末から BigQuery のデータが
> 見える状態になります。本アプリには利用者認証がないため、ループバックに
> 限定して起動してください。

---

## 安全機構

| 対策 | 内容 |
|---|---|
| 読み取り専用 | 先頭が `SELECT` / `WITH` 以外のクエリを実行前に拒否。`INSERT` / `UPDATE` / `DELETE` / `DROP` / `CREATE` / `MERGE` 等を多重チェック |
| 複文の拒否 | `SELECT 1; DROP TABLE ...` のようなインジェクションをブロック |
| データセット allowlist | `.env` で許可したデータセット以外への参照を SQL 解析段階で拒否。別プロジェクトへの参照も拒否 |
| コスト確認 | 実行前に `dry_run` で推定スキャン量を取得し、閾値超過時は UI でユーザーに確認 |
| 課金ハードリミット | `maximum_bytes_billed` を明示設定 |
| 自動 `LIMIT` | 外側に `LIMIT` が無い場合に自動付与し、付与した旨を画面と Gemini の双方に通知 |
| エラーの整形 | 生の例外はユーザーに見せず日本語メッセージに変換（技術的詳細は Gemini の自己修正用にのみ渡す） |

SQL の解析はコメントと文字列リテラルを認識するスキャナで前処理しており、
コメントや文字列に埋め込まれたキーワードによる誤検出・回避を防いでいます。
日本語のテーブル名・列名（バッククォート囲み）にも対応しています。

## トークン節約の設計

1. **スキーマの段階的取得** — 全テーブルのスキーマをプロンプトに含めず、
   `list_tables` → `get_table_schema` の 2 段階で必要な分だけ取得します。
2. **結果の要約のみ返却** — `run_query` が Gemini に返すのは行数・列と型・
   数値統計・先頭数行だけです。フルの結果は UI に直接表示します。
3. **セッション内キャッシュ** — 取得済みスキーマと実行済み SQL は
   `st.session_state` に保持し、同一会話内での再取得・再課金を防ぎます。
   キャッシュ状況はサイドバーで確認できます。

---

## ディレクトリ構成

```
shirugpt/
├── app.py              Streamlit UI（チャット・確認ダイアログ・結果表示）
├── requirements.txt
├── .env.example        設定テンプレート
├── .gitignore
├── README.md
└── src/
    ├── config.py       .env の読み込みと検証、鍵ファイルの漏洩チェック
    ├── bq_tools.py     BigQuery ツール 4 種とエラーの日本語化
    ├── sql_guard.py    SQL の安全検査・allowlist 検証・LIMIT 自動付与
    ├── summarize.py    DataFrame → 要約（Gemini に渡す分）
    ├── charts.py       結果の列構成からグラフ種類・軸を推定
    └── agent.py        Gemini Function Calling ループ（中断・再開対応）
```

---

## 既知の制約

- 想定利用は開発者本人によるローカル実行のみです。認証・アクセス制御は未実装です。
- `MAX_RESULT_ROWS` を超える結果は先頭のみ読み込まれます（画面にその旨を表示）。
- 集計クエリでも `LIMIT` が無ければ自動付与されるため、グループ数が上限を超える
  場合は切り捨てが起こります（その旨は画面と Gemini の双方に通知されます）。

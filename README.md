# ShiruGPT

BigQuery のデータを日本語で質問すると、Gemini が自律的に SQL を組み立てて実行し、
結果を要約して日本語で回答する Streamlit アプリです。

```
「来店データで学年別の来店件数を多い順に教えて」
        ↓
学部1年: 320,447件 / 学部2年: 154,374件 / 学部3年: 149,152件 …
（実行された SQL とグラフ・表も画面に表示）
```

> **ロジック検証用のプロトタイプです。** 開発者本人のローカル実行のみを想定しています。
> Cloud Run 等へのデプロイ、利用者認証、レート制限、複数ユーザーの同時利用は
> 次フェーズのスコープです。

---

# 仕組み

## 1. 全体像

4 つのレイヤーが一方向に連なっています。

```
┌─────────────────────────────────────────────────────┐
│ app.py            Streamlit UI                       │
│                   チャット入力・確認ダイアログ・結果表示    │
└───────────────┬─────────────────────────────────────┘
                │ agent.run(contents, ctx)
┌───────────────▼─────────────────────────────────────┐
│ src/agent.py      Gemini Function Calling ループ      │
│                   ツール呼び出しの往復・中断と再開         │
└───────────────┬─────────────────────────────────────┘
                │ 4 つのツール関数
┌───────────────▼─────────────────────────────────────┐
│ src/bq_tools.py   BigQuery アクセス                   │
│  ├ src/sql_guard.py   実行前の SQL 検査（必ず通る）      │
│  └ src/summarize.py   結果を要約に圧縮                  │
└───────────────┬─────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────┐
│ BigQuery                                             │
└─────────────────────────────────────────────────────┘
```

Gemini は BigQuery に直接触れません。**Gemini がするのは「この関数をこの引数で
呼びたい」という意思表示だけ**で、実際に接続してクエリを走らせるのは
`bq_tools.py` の Python コードです。この分離があるおかげで、SQL の検査・
結果の圧縮・キャッシュ・課金上限といった制御を、すべてこちら側で強制できます。

## 2. 1 回の質問で何が起きるか

「来店データで学年別の来店件数を多い順に教えて」と入力したときの実際のトレースです。

| # | 誰が | 何を |
|---|---|---|
| 1 | app.py | 入力を `Content(role="user")` にして `contents` に追記 |
| 2 | Gemini | 「まず何があるか調べたい」→ `list_datasets` を要求 |
| 3 | bq_tools | allowlist 内のデータセットを返す → `202506`（location: US） |
| 4 | Gemini | `list_tables(dataset="202506")` を要求 |
| 5 | bq_tools | 14 テーブルの名前だけを返す（スキーマは含めない） |
| 6 | Gemini | 「`来店` が使えそうだ」→ `get_table_schema(202506, 来店)` を要求 |
| 7 | bq_tools | 14 列の名前と型、行数 838,286、サイズ 149.80 MB を返す |
| 8 | Gemini | 実在する列名 `grade` を使って SQL を生成 → `run_query` を要求 |
| 9 | sql_guard | SELECT 限定・allowlist・複文を検査。`LIMIT 1000` を自動付与 |
| 10 | bq_tools | dry run で 9.58 MB と見積もり → 閾値内なので実行 |
| 11 | summarize | 結果 13 行を「行数・列と型・統計・先頭 15 行」に圧縮 |
| 12 | Gemini | 要約を読んで日本語の回答文を生成（ツール要求なし＝ループ終了） |
| 13 | app.py | 回答・実行 SQL・グラフ・表を画面に描画 |

ツールの呼び出しは **4 回**（ステップ 3, 5, 7, 10）、Gemini への生成リクエストは
**5 回**（ツールを要求した 4 回＋最終回答の 1 回）です。Gemini が複数のツールを
まとめて要求した場合は、その分リクエスト回数は減ります。

## 3. なぜループするのか

Gemini は「答えるために何が必要か」を 1 回では判断しきれません。上のトレースで
言えば、ステップ 2 の時点ではテーブル名も列名も知らない状態です。ここで SQL を
書かせれば、存在しない列名を推測で使うことになります。

そこで「調べる → 分かったことを踏まえて次を決める」を繰り返させます。
ループの終了条件は **Gemini の応答に `function_call` が 1 つも含まれなくなること**です。

```python
# src/agent.py: run()
for _ in range(self.settings.max_tool_iterations):   # 暴走防止の上限（既定 12）
    response = self._generate(contents)
    calls = [p.function_call for p in parts if p.function_call]

    if not calls:
        return AnswerResult(text)          # ツール要求なし = これが最終回答

    contents.append(candidate.content)     # Gemini の「呼びたい」を履歴に追記
    responses = [self._dispatch(call, ctx) for call in calls]
    contents.append(Content(role="user", parts=responses))   # 結果を履歴に追記
    # ループ先頭へ戻り、増えた履歴で再度 Gemini に問う
```

`max_tool_iterations` は、スキーマ取得と SQL 修正を延々と往復するような
状態を打ち切るための保険です。

## 4. 設計の中心 — Gemini 境界を越えるものを絞る

このプロジェクトの設計判断は、ほぼこの一点に集約されます。

| データ | Gemini に渡す | UI に出す |
|---|---|---|
| データセット / テーブル一覧 | ○ | — |
| スキーマ | **明示的に要求された 1 テーブル分のみ** | — |
| クエリ結果 | **要約のみ**（行数・列と型・統計・先頭 N 行） | DataFrame 全体 |

### スキーマの段階的取得

`list_tables` はテーブル**名**だけを返し、列情報は含めません。
列情報が要るテーブルについてだけ、Gemini が改めて `get_table_schema` を呼びます。

全 14 テーブルのスキーマを一括で `SYSTEM_INSTRUCTION` に埋め込めばツールの往復は
減りますが、**関係ないテーブルの列情報まで毎回の呼び出しに乗り続ける**ため、
トータルのトークンはむしろ増えます。加えて BigQuery 側でカラムが変わっても
追従しません。列の意味づけを補いたい場合は、BigQuery のカラム `description` に
設定してください。`get_table_schema` は `description` をそのまま拾って返します。

### クエリ結果の要約

`run_query` の実行結果は、`QueryRun` の中で 2 つに分岐します。

```python
# src/bq_tools.py: execute()
df = rows.to_dataframe(create_bqstorage_client=False)   # 生の DataFrame
summary = summarize_dataframe(df, sample_rows=self.settings.sample_rows, ...)

return QueryRun(
    dataframe=df,        # ← UI 専用。Gemini には渡さない
    summary=summary,     # ← Gemini 向け。これだけが境界を越える
    ...
)
```

Gemini が受け取るのは次のような構造です。生の行データは入りません。

```python
{
  "executed_sql": "SELECT grade, COUNT(*) AS `来店件数` FROM ... LIMIT 1000",
  "bytes_processed": "9.58 MB",
  "result": {
    "row_count": 13,
    "column_count": 2,
    "columns": [
      {"name": "grade", "dtype": "str", "null_count": 0,
       "distinct_count": 13},
      {"name": "来店件数", "dtype": "Int64", "null_count": 0,
       "stats": {"min": 77.0, "max": 320447.0,
                 "mean": 64463.6, "sum": 838027.0}}
    ],
    "sample_rows": [
      {"grade": "学部1年", "来店件数": 320447},
      {"grade": "学部2年", "来店件数": 154374},
      ...                                       # 先頭 15 行まで
    ],
    "sample_row_count": 13
  },
  "notes": ["外側に LIMIT が無かったため LIMIT 1000 を自動付与しました。…"]
}
```

元テーブルは 838,286 行ありますが、Gemini が見るのは集計後のこの構造だけです。

`SAMPLE_ROWS` が既定 15 と控えめなのは、**一度返したツール結果が `contents` に
残り続ける**ためです。会話が進むほど過去のサンプル行も毎回のプロンプトに乗るので、
少なく始めて必要なら `.env` で増やす設計にしています。

### キャッシュ

同一会話内で同じ問い合わせを繰り返させません。状態は `ToolContext` が持ちます。

| キャッシュ | キー | 効果 |
|---|---|---|
| `schema_cache` | `dataset.table` | 同じテーブルのスキーマを再取得しない |
| `listing_cache` | `tables:{dataset}` | 一覧を再取得しない |
| `query_cache` | **SQL 文字列のハッシュ** | 同じ SQL を再実行しない（＝再課金しない） |

サイドバーに現在のキャッシュ件数が表示されます。

## 5. SQL ガード

Gemini が生成した SQL は、**必ず** `check_sql()` を通ってから実行されます。
`SYSTEM_INSTRUCTION` の指示は破られる可能性がある前提で、コード側が機械的に強制します。

### 前処理 — 2 種類のマスク

素朴にキーワードを検索すると、コメントや文字列リテラルの中身に反応してしまいます。
そこで自前のスキャナでコメントと文字列リテラルを**同じ長さの空白に潰して**から
解析します。長さを保つのは、元 SQL のオフセットを崩さないためです。

用途によってマスクの範囲を変えます。

| 用途 | 文字列リテラル | バッククォート識別子 |
|---|---|---|
| 危険キーワードの検査 | 潰す | **潰す** |
| 参照テーブルの抽出 | 潰す | 残す |

キーワード検査でバッククォートの中身も潰すのは、`` `更新日` `` のような列名を
`UPDATE` と誤検出しないためです。逆に参照抽出では
`` `proj.202506.来店` `` を読む必要があるので中身を残します。

### 判定

主防御は次の 2 つです。

1. **先頭トークンが `SELECT` または `WITH` であること** — `CREATE TABLE ... AS SELECT`
   や `EXPORT DATA ... AS SELECT` はここで落ちます
2. **複文でないこと** — `SELECT 1; DROP TABLE ...` をブロック

危険キーワード（`INSERT` / `DELETE` / `DROP` / `MERGE` 等）のリスト検査は
**多重防御であって主防御ではありません**。SQL の構造上、SELECT 文の内部に
DML を埋め込むことはできないため、上の 2 つで論理的には足りています。

### データセット allowlist

`FROM` / `JOIN` の直後、およびバッククォート内の完全修飾参照を抽出し、
`BQ_ALLOWED_DATASETS` 外のデータセットと別プロジェクトへの参照を拒否します。

**1 要素の参照（`FROM foo`）は意図的に無視しています。** CTE 名やエイリアス、
`EXTRACT(YEAR FROM col)` を誤検出しないためです。安全性は別経路で担保しています
— クエリジョブに default dataset を設定していないので、修飾なしの参照は
BigQuery 側で必ず失敗します。

### LIMIT の自動付与

外側に `LIMIT` が無ければ `DEFAULT_ROW_LIMIT`（既定 1000）を付与し、
付与した事実を画面と Gemini の双方に通知します。集計クエリの場合は
「グループ数が上限を超えると切り捨てが起こる」旨も添えます。黙って切り詰めると
結果を誤読させるため、明示することを優先しています。

## 6. コスト確認の中断と再開

dry run の推定スキャン量が `DRY_RUN_CONFIRM_BYTES`（既定 1GB）を超えると、
ループは**その場で止まって UI に戻ります**。3 ファイルにまたがる流れです。

```
bq_tools.run_query()
    dry run で閾値超過を検出 → ConfirmationRequired を送出
        ↓
agent._run_query()
    _Pending に包み直す
        ↓
agent.run()
    ctx.resume_calls に「処理中だった function_call 群」を退避
    ConfirmationPending を返してループを抜ける
        ↓
app.render_confirmation()
    推定スキャン量と SQL を提示し、承認 / 拒否ボタンを描画
    押されたら ctx.decisions[key] に記録
        ↓
agent.run() を再度呼ぶ
    ctx.resume_calls から同じ地点で再開
```

### 再開が安全な理由

再開時は退避した function call 群を**先頭から再ディスパッチ**します。
これが安全なのは、ツールが冪等だからです。

- `list_datasets` / `list_tables` / `get_table_schema` — 読み取り専用
- `run_query` — SQL のハッシュで `query_cache` を引くため、再実行も再課金も起きない

**この冪等性を壊すと、ユーザーが承認した瞬間に二重課金が発生します。**

承認 / 拒否のキーは、Gemini が送ってきた**元の SQL** のハッシュです
（`sql_guard` が LIMIT を付与した後の SQL ではありません）。再開時に同じ引数で
再ディスパッチされるため、元の SQL を基準にする必要があります。

## 7. 結果の表示

クエリ結果は「グラフ」「表」の 2 タブで表示されます。

グラフの種類は列の dtype から自動で決まります。

| 結果の形 | 既定のグラフ |
|---|---|
| 日時列 ＋ 数値列 | 折れ線 |
| カテゴリ列 ＋ 数値列 | 棒グラフ |
| 数値列 ＋ 数値列 | 散布図 |
| 上記に当てはまらない | 表のみ |

種類・X 軸・Y 軸は画面上で変更できます。棒グラフは `sort=False` を指定して
SQL の `ORDER BY` の並び順を保持します（Streamlit の既定では X 軸で並べ替えられます）。

**この判定はすべてサーバー側で完結し、Gemini には問い合わせません。**
グラフのためにデータを Gemini 境界の向こうへ送らない、という 4 章の方針の延長です。
追加のトークンはゼロです。

## 8. エラーの二経路

同じ例外を、宛先ごとに変換し分けます。

| 変換 | 宛先 | 内容 |
|---|---|---|
| `friendly_error(exc)` | UI | 平易な日本語。生の例外は見せない |
| `technical_detail(exc)` | Gemini | 構文エラーの内容などをそのまま渡す |

Gemini には技術的詳細を渡します。エラー内容が分からないと自力で SQL を
修正できないためです。ツール実行中の例外は `agent._dispatch()` が捕捉して
`{"error": ...}` として tool response に載せ、**ループは止めません**
（Gemini に再試行させます）。

---

# 日本語データを扱う上の注意

対象データセット `202506` には、テーブル名・列名が日本語のものが多数あります
（14 テーブル中 11 個が日本語を含む: `来店`、`日報`、`日次データ`、`月次データ`、
`月毎目標値`、`イベント`、`ユーザー`、`目標値`、`Meetup参加` ほか）。

- データセット名 `202506` は数字始まりのため、修飾なしでは書けません。常にバッククォート。
- **`AS` のエイリアスにもバッククォートが必要です。** BigQuery は裸の日本語識別子を
  `Illegal input character` で拒否します。

  ```sql
  SELECT COUNT(*) AS 件数    FROM `proj.202506.来店`   -- 構文エラー
  SELECT COUNT(*) AS `件数`  FROM `proj.202506.来店`   -- OK
  ```

  実データ検証で踏んだ落とし穴で、`agent.SYSTEM_INSTRUCTION` に明示的な指示として
  入れてあります。

---

# セットアップ

## 1. リポジトリの取得

初めて使う場合は clone します。

```bash
git clone https://github.com/junya34/shirugpt.git
```

```bash
cd shirugpt
```

既に手元にある場合は、最新の変更を取り込みます。

```bash
git pull origin main
```

## 2. 依存パッケージ

Python 3.11 以上が必要です。

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

## 3. GCP 側の準備

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
| 利用ログの記録（任意） | ログ用データセットに `roles/bigquery.dataEditor` |

### 利用ログを使う場合（任意）

`BQ_LOG_DATASET` を設定すると、利用者ごとのトークン量・クエリ量が
BigQuery に記録され、管理者ページ（`ADMIN_EMAILS` のメールのみ閲覧可）で
日別・月別に確認できます。設定しなければこの機能はオフになり、
アプリは従来どおり動作します。

> **`BQ_LOG_DATASET` の名前を `BQ_ALLOWED_DATASETS` に含めないでください。**
> 含めると全利用者の使用量が Gemini から参照可能になります。
> 混入している場合はアプリが起動時に停止して知らせます。

データセットは初回起動時に自動作成を試みますが、それには
`roles/bigquery.user`（データセット作成権限）が必要です。付与したくない場合は
事前に手で作り、そのデータセットにだけ `dataEditor` を付与してください。

```bash
bq --location=<ロケーション> mk --dataset <プロジェクトID>:shirugpt_usage_log
```

**利用者ごとに識別するには、閲覧者のログインが別途必要です。** Streamlit は
バージョン 1.42 以降、Community Cloud の閲覧者制限（Private + 許可メール）だけでは
アプリ側にメールアドレスを渡さなくなりました。利用ログ・管理者ページで
「誰が」を識別するには、Google を識別プロバイダとした `st.login()` を
自前で設定する必要があります。

1. [Google Cloud Console](https://console.cloud.google.com/apis/credentials) で
   OAuth 2.0 クライアント ID を作成する（アプリケーションの種類: ウェブアプリケーション）。
   承認済みのリダイレクト URI に、アプリの公開 URL に `/oauth2callback` を
   付けたものを登録する（例: `https://<アプリ名>.streamlit.app/oauth2callback`）。
2. Streamlit Cloud の Secrets に、既存の内容に追記する形で次を追加する
   （`client_id` と `client_secret` は 1 で発行された値。`cookie_secret` は
   ランダムな文字列を自分で用意する。**この値は Claude Code には貼り付けず、
   自分で Secrets に直接入力すること**）:

   ```toml
   [auth]
   redirect_uri = "https://<アプリ名>.streamlit.app/oauth2callback"
   cookie_secret = "（ランダムな文字列）"
   client_id = "（Google の client_id）"
   client_secret = "（Google の client_secret）"
   server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
   ```

3. 保存すると自動的に再起動する。以後、アプリを開くと「Google でログイン」
   ボタンが表示され、ログインしたメールアドレスが利用ログと管理者ページの
   識別に使われる。

`[auth]` を設定しなければこのログインゲート自体が現れず、アプリは
（利用者を識別できない状態で）これまでどおり動く。ローカル開発では
通常 `[auth]` を設定しないため、影響はない。

## 4. 認証

認証方式は 2 つあります。開発者本人は方式 A（ADC）を推奨しますが、
同僚が手元で動かす場合は、共有された鍵を使う方式 B が簡単です。

### 方式 A: Application Default Credentials（開発者本人向け・推奨）

```bash
gcloud auth application-default login
```

```bash
gcloud auth application-default set-quota-project <プロジェクトID>
```

`.env` の `GOOGLE_APPLICATION_CREDENTIALS` は**空のまま**にしてください。

### 方式 B: サービスアカウントキー（同僚が手元で動かす場合）

サービスアカウントキー（`gcp-secret-key.json`）は、共有されている Google Drive の
[credentials フォルダ](https://drive.google.com/drive/folders/1S0DochaRO3HwDqqclTJZVR-B9UEPRdEw?usp=drive_link)
に置いてあります。次の手順で配置してください。

1. 上記の Google Drive フォルダから `gcp-secret-key.json` をダウンロードする
2. リポジトリ直下に `credentials/` ディレクトリを作り、その中に置く

   ```bash
   mkdir -p credentials
   ```

   ```
   shirugpt/
   └── credentials/
       └── gcp-secret-key.json   ← ここに配置
   ```

3. `.env` にパスを書く

   ```
   GOOGLE_APPLICATION_CREDENTIALS=credentials/gcp-secret-key.json
   ```

`credentials/` と `*-key.json` などは `.gitignore` 済みです。
起動時にも「鍵ファイルが git 管理外になっているか」を `git check-ignore` で
自動チェックし、除外されていない場合は画面上に警告を表示します。
**鍵ファイルはリポジトリにコミットしないでください。** 公開リポジトリのため、
`git status` で `credentials/` が追跡対象に出てこないことを起動前に確認してください。

### なぜ ADC を推奨するのか

| 観点 | ADC | サービスアカウントキー |
|---|---|---|
| 実体 | OS のユーザー領域に置かれる短命トークン。自動更新される | **秘密鍵ファイルそのもの**。既定では無期限 |
| 漏洩したら | `gcloud auth application-default revoke` で即失効 | 鍵を無効化するまで悪用可能。**リポジトリに push したら即ローテーション必須** |
| 監査ログ | 個人アカウント名義で記録され、誰の操作か追える | サービスアカウント名義になり実行者を特定しづらい |
| 適する場面 | ローカル開発 | サーバー運用（ただし本来は鍵なしの SA アタッチが最善） |

鍵ファイルは「持ち出せる資産」になるため、`.gitignore` 漏れ・チャット共有・
バックアップ経由での流出が事故の典型です。**公開リポジトリを使う本プロジェクトでは
ADC のほうがリスクがはるかに小さくなります。**

## 5. `.env` の作成

```bash
cp .env.example .env
```

| 変数 | 既定値 | 説明 |
|---|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | （空） | SA キーのパス（`credentials/gcp-secret-key.json` 等）。空なら ADC を使用 |
| `GCP_PROJECT_ID` | — | 対象 GCP プロジェクト ID（必須） |
| `BQ_LOCATION` | （空） | データセットのロケーション（例 `US`, `asia-northeast1`）。空なら自動 |
| `BQ_DEFAULT_DATASET` | `202506` | 既定のデータセット |
| `BQ_ALLOWED_DATASETS` | `202506` | **アクセスを許可するデータセットの allowlist**（カンマ区切り） |
| `VERTEX_LOCATION` | `us-central1` | Gemini を呼ぶリージョン（Agent Platform / 旧 Vertex AI） |
| `GEMINI_MODEL` | `gemini-2.5-flash` | 使用モデル |
| `GEMINI_THINKING_BUDGET` | （空） | 思考トークン予算。空でモデル既定、`0` で無効 |
| `DRY_RUN_CONFIRM_BYTES` | `1073741824`（1GB） | 推定スキャン量がこれを超えたら実行前に確認 |
| `MAX_BYTES_BILLED` | `10737418240`（10GB） | BigQuery に渡す課金上限。超えるクエリは BigQuery 側で失敗 |
| `DEFAULT_ROW_LIMIT` | `1000` | 外側に `LIMIT` が無い場合に自動付与する行数 |
| `MAX_RESULT_ROWS` | `5000` | UI に読み込む最大行数（メモリ保護） |
| `SAMPLE_ROWS` | `15` | Gemini に渡すサンプル行数 |
| `MAX_TOOL_ITERATIONS` | `12` | 1 ターンあたりの tool 呼び出しループ上限 |
| `BQ_LOG_DATASET` | （空） | 利用ログの記録先データセット。空で機能オフ。**allowlist に含めない** |
| `BQ_LOG_TABLE` | `usage_events` | 利用ログのテーブル名 |
| `ADMIN_EMAILS` | （空） | 管理者ページを開けるメールアドレス（カンマ区切り） |
| `ADMIN_ALLOW_LOCAL` | （空） | ローカル開発時のみ管理者ページを開く。**Cloud の Secrets に入れない** |

すべて `.env` 由来で、コードにハードコードされている値はありません。
テーブル名やカラム構成も埋め込まず、実行時に動的取得します。

## 6. 起動

```bash
.venv/bin/streamlit run app.py --server.address 127.0.0.1
```

ブラウザで `http://localhost:8501` が開きます。

> `--server.address 127.0.0.1` を付けないと、Streamlit は既定で全ネットワーク
> インターフェースを待ち受け、同一 LAN 上の他端末から BigQuery のデータが
> 見える状態になります。本アプリには利用者認証がないため、ループバックに
> 限定して起動してください。

---

# 安全機構の一覧

| 対策 | 内容 | 実装 |
|---|---|---|
| 読み取り専用 | 先頭が `SELECT` / `WITH` 以外を拒否。危険キーワードを多重チェック | `sql_guard` |
| 複文の拒否 | `SELECT 1; DROP TABLE ...` をブロック | `sql_guard` |
| データセット allowlist | 許可外データセット・別プロジェクトへの参照を拒否 | `sql_guard` |
| 修飾なし参照の遮断 | クエリジョブに default dataset を設定しない | `bq_tools` |
| コスト確認 | dry run の推定量が閾値超過なら UI で確認 | `bq_tools` → `agent` → `app` |
| 課金ハードリミット | `maximum_bytes_billed` を明示設定 | `bq_tools` |
| 自動 `LIMIT` | 外側に `LIMIT` が無ければ付与し、その旨を通知 | `sql_guard` |
| 再課金の防止 | SQL ハッシュでクエリ結果をキャッシュ | `agent` |
| ループ上限 | 1 ターンの tool 往復を `MAX_TOOL_ITERATIONS` で打ち切り | `agent` |
| エラーの整形 | 生の例外をユーザーに見せない | `bq_tools` |
| 鍵の漏洩チェック | 起動時に `git check-ignore` で鍵ファイルの除外を確認 | `config` |

---

# データとセキュリティに関する注意

## サービスアカウントキーの取り扱い

`gcp-secret-key.json` は BigQuery と Gemini を呼び出せる実質的な「パスワード」です。
以下を徹底してください。

- **配布経路を限定する** — 共有された Google Drive の
  [credentials フォルダ](https://drive.google.com/drive/folders/1S0DochaRO3HwDqqclTJZVR-B9UEPRdEw?usp=drive_link)
  からのみ取得してください。メール添付・チャットへの直接貼り付け・個人のクラウド
  ストレージへのコピーはしないでください。転送経路が増えるほど漏洩の機会が増えます
- **リポジトリにコミットしない** — `credentials/` は `.gitignore` 済みで、起動時にも
  `git check-ignore` で除外されているかを自動チェックします（[セットアップ 4 章](#4-認証)参照）。
  それでも `git add -A` の後は `git status` で `credentials/` が出てこないことを確認する習慣を
  持ってください
- **端末外に持ち出さない** — ローカルの `credentials/gcp-secret-key.json` 以外の場所
  （デスクトップ、ダウンロードフォルダ、USBメモリ等）にコピーしないでください
- **不要になったら削除し、必要ならローテーションする** — 端末の紛失・盗難、退職・
  異動、あるいは誤って外部に漏れた可能性がある場合は、ローカルの鍵ファイルを削除する
  だけでなく、GCP コンソールでそのサービスアカウントキーを**無効化**し、新しい鍵を
  発行してください（[セットアップ 3 章](#3-gcp-側の準備)のロールを持つサービスアカウントの鍵を再発行）
- **付与する権限は必要最小限に保つ** — このサービスアカウントには
  `bigquery.dataViewer` / `bigquery.jobUser` / `aiplatform.user` 以外のロールを
  追加しないでください。権限が大きいほど、鍵が漏れた際の被害も大きくなります

ローカル開発を自分のGCPアカウントで行う場合は、鍵ファイルそのものを作らない
「[認証](#4-認証)」章の方式 A（ADC）の方が本質的に安全です（短命トークンで
自動更新され、`gcloud auth application-default revoke` で即座に失効できるため）。

## Gemini に送ったデータの扱い

このアプリは Gemini を **Vertex AI（Gemini Enterprise Agent Platform）経由**で
呼び出しています（[src/agent.py](src/agent.py) の `genai.Client(vertexai=True, ...)`）。
これは一般消費者向けの Gemini アプリ（gemini.google.com）とは別の提供形態で、
データの扱いに関する保証が異なります。

Google Cloud の公式ドキュメントには次のように明記されています。

> Gemini doesn't use your prompts or its responses as data to train its models.
> （Gemini はプロンプトや応答をモデルの学習データとして使用しません）

つまり、このアプリが Gemini に送る内容 — ユーザーの質問文、テーブルのスキーマ、
クエリ結果の要約（「仕組み」章の 4 節「Gemini 境界を越えるものを絞る」で説明した
要約データ）— は、Google 側のモデルの学習には使われません。

ただし、これは「Gemini に送った内容がどこにも残らない」という意味ではありません。
不正利用防止のための一時的なログ保持など、別目的でのデータ処理は行われる場合があります。
正確な保持期間・処理内容は、利用中の Google Cloud の契約条件と最新のデータガバナンス
ドキュメントを直接ご確認ください。

Sources:
- [How Gemini for Google Cloud uses your data | Google Cloud Documentation](https://docs.cloud.google.com/gemini/docs/discover/data-governance)

---

# ディレクトリ構成

```
shirugpt/
├── app.py              Streamlit UI（チャット・確認ダイアログ・グラフ/表）
├── requirements.txt
├── .env.example        設定テンプレート
├── .gitignore
├── CLAUDE.md           Claude Code 向けの開発ガイド
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

# 既知の制約

- 想定利用は開発者本人によるローカル実行のみです。利用者認証・レート制限は未実装です。
- `MAX_RESULT_ROWS` を超える結果は先頭のみ読み込まれます（画面にその旨を表示）。
- 集計クエリでも `LIMIT` が無ければ自動付与されるため、グループ数が上限を超える
  場合は切り捨てが起こります（その旨は画面と Gemini の双方に通知されます）。
- 自動テストスイートは未整備です。`src/sql_guard.py` は GCP 認証なしで単体検証
  できるため、SQL 解析まわりを変更した際は `check_sql()` を直接呼んで確認してください。
- Gemini が生成する SQL の正しさは保証されません。実行された SQL は必ず画面の
  expander に表示されるので、重要な判断に使う前に内容をご確認ください。

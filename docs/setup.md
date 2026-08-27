# セットアップ

[README](../README.md) の続き。詳細なセットアップ手順です。

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

管理者ページの「使用制限」タブから、利用者ごとに**月間の使用金額（USD）の
上限**（Gemini トークン代 + BigQuery クエリ代の合計）を設定できます
（個別編集・全員への一括適用の両方に対応）。
未設定の利用者には既定値（`$1.00`、`src/config.py` の
`DEFAULT_MONTHLY_LIMIT_USD`）が適用されます。上限に達すると、その回答は
最後まで完了した上で、次の質問からブロックされます（毎月1日 0:00 JST に
自動的にリセット）。この判定にはログイン中のメールアドレスが必要なので、
`[auth]`（`st.login()`）を設定していない環境（ローカル開発など）では
無効になります。

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

### 利用者を追加する方法

新しい人がアプリを使えるようにするには、メールアドレスを
登録する必要があります。1つでも欠けると、その人は使えません。

1. **Google Cloud Console の OAuth 同意画面（テストユーザー）** —
   `https://console.cloud.google.com/auth/audience?hl=ja&walkthrough_id=bigquery--bigquery-quickstart-query-public-dataset&project=fair-solution-453613-e2`
   「OAuth 同意画面」→「対象」を開き、
   「Add user」でログインを許可するメールアドレスを追加する。登録した人のみアクセスできる。
2. **`ADMIN_EMAILS`（管理者権限が必要な場合のみ）** — Streamlit Cloud の
   Secrets（または `.env`）の `ADMIN_EMAILS` にカンマ区切りで追加する。
   管理者ページ（利用状況・使用制限の設定）を開けるかどうかだけを決める、
   1・2 とは別の権限

`[auth]` を設定しなければこのログインゲート自体が現れず、アプリは
（利用者を識別できない状態で）これまでどおり動く。ローカル開発では
通常 `[auth]` を設定しないため、影響はない。

## 4. 認証

認証方式は 2 つあります。個人の GCP アカウントで検証する場合は方式 A（ADC）、
このプロジェクト用に共有しているサービスアカウントの鍵を使う場合は方式 B です。

### 方式 A: Application Default Credentials（個人アカウントでの検証向け）

```bash
gcloud auth application-default login
```

```bash
gcloud auth application-default set-quota-project <プロジェクトID>
```

`.env` の `GOOGLE_APPLICATION_CREDENTIALS` は**空のまま**にしてください。

### 方式 B: サービスアカウントキー（共有鍵を Google Drive から取得）

本プロジェクトでは単一のサービスアカウントの鍵を Google Drive 経由で共有し、
`credentials/` に配置して使う運用にしています。同一アカウントのみで使うため、
Drive フォルダの共有範囲を個別に管理する必要はありません。

サービスアカウント鍵の保管場所:
[Google Drive](https://drive.google.com/drive/folders/1S0DochaRO3HwDqqclTJZVR-B9UEPRdEw?usp=drive_link)

- `gcp-secret-key.json` … BigQuery / Vertex AI（Gemini）共通

次の手順で配置してください。

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
| `BQ_LIMIT_TABLE` | `user_limits` | 月次使用制限のテーブル名（`BQ_LOG_DATASET` と同じデータセット内） |
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
> 見える状態になります。ローカル実行では通常 `[auth]`（`st.login()`）を
> 設定しないため認証が効かず、誰でもアクセスできてしまいます。ループバックに
> 限定して起動してください。

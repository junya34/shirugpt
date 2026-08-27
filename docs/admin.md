# 利用ログ・月次使用制限・管理者ページ

[README](../README.md) の続き。有効化の手順自体は
[docs/setup.md「GCP 側の準備」](setup.md#3-gcp-側の準備)内の「利用ログを使う場合（任意）」
を参照してください。ここでは仕組みの詳細を説明します。

## 1. Gemini 境界の外に置く理由

`src/usage_log.py`（`UsageLogger`）が利用者ごとのトークン量・クエリ量を BigQuery に
記録します。このテーブルには**全利用者の使用量**が入るため、Gemini から見えると
他人の利用状況が自然言語質問経由で漏れます。防御は 3 重です。

1. `BQ_LOG_DATASET` は `BQ_ALLOWED_DATASETS` に**含めない**。
   `list_tables` は allowlist を列挙するだけなので、Gemini には名前すら見えない
2. 仮に名前を推測されても `sql_guard` が allowlist 外の参照を拒否する
3. 混入は `config.load_settings()` が**起動時に `ConfigError` で落とす**
   （運用ミスがサイレントな漏洩ではなく、デプロイ時の失敗になる）

**`BQ_LOG_DATASET` を allowlist に足す変更を入れてはいけません。**

書き込みは `insert_rows_json()` による JSON 行の直接挿入で、文字列 SQL を
組み立てません（`sql_guard` を通す経路とは無関係です）。`UsageLogger.log_*` は
**例外を外に漏らさない契約**です。ログが書けなくてもチャットは止まりません。
連続失敗（既定 5 回）が続く場合は、そのプロセス内では記録を諦めます
（毎イベントで失敗する API 往復を繰り返すと、チャットの応答が遅くなるため）。

## 2. 利用者メールの取得

Streamlit 1.42 以降、Community Cloud の閲覧者制限（Private + 許可メール）だけでは
アプリに閲覧者のメールが渡りません（`st.user` は空のまま）。`app.py` の
`resolve_viewer_email()` は自前で設定した `st.login()`（Google OIDC、
`secrets.toml` の `[auth]`）に依存しています。`auth_configured()` が `False`
（`[auth]` 未設定、主にローカル開発）のときはログインゲート自体をスキップする
仕様なので、「メールが空＝バグ」ではありません。

## 3. 月次の使用制限（`user_limits`）

利用者ごとの月間 USD 上限（既定 `DEFAULT_MONTHLY_LIMIT_USD`、`src/config.py`）を
`user_limits` / `user_limits_staging` テーブルで管理します。両方とも
`BQ_LOG_DATASET`（`log_dataset`）の中に置くので、上記の Gemini 境界の防御
（allowlist から除外・`sql_guard` の拒否・起動時ガード）が**追加のコード無しで
そのまま適用**されます。

### `weekly_limit_usd` という列名について

**BigQuery 側の列名は `weekly_limit_usd` のままです。** 元は週次だった制限を
月次に切り替えた際、デプロイ済みテーブルの移行を避けるため列名は変更していません。
値の意味は「月間の USD 上限」に変わっています。これは意図的な不整合であり、
「週次に戻すべき」と誤って直さないでください。Python 側の識別子（関数名・
クラス名・UI 文言）はすべて月次の名前に揃っています。

### 書き込み方式が違う

`usage_events` は高頻度の `insert_rows_json`（ストリーミング）ですが、
`user_limits` は管理者が低頻度で編集する設定データなので、ステージングテーブルへ
`WRITE_TRUNCATE` → 本体テーブルへ `MERGE`（upsert のみ、DELETE 分岐なし）という
別方式（`UsageLogger.save_limits()`）です。`user_limits` 自体には streaming
insert を一切行わないので、ストリーミングバッファ直後の DML 制約は最初から
関係ありません。同時保存は BigQuery が競合エラーとして検出するので、リトライ
機構は作らず例外をそのまま管理者に見せます。

### circuit breaker の対象が違う

`ensure_table()`（チャットの高頻度書き込み経路から呼ばれる）は `enabled`
（連続失敗 5 回で無効化）に従いますが、`_ensure_limits_infra()`（管理者操作専用）
は従いません。チャット側のログ書き込みが何度失敗していても、管理者は制限を
設定できるべきだからです。

### 集計対象

Gemini トークン代 + BigQuery クエリ代の合計です。`MonthlyUsage.cost_usd()`
（`usage_log.py`）が両方を合算します。

### ブロック判定はセッション内カウンタとのハイブリッド

`app.py` の `_effective_monthly_usage_usd()` は、月替わり・セッション開始時
だけ BigQuery に問い合わせて基準値を取り、以降は同一セッション内の増分
（`ctx.session_prompt_tokens` / `session_output_tokens` / `session_billed_bytes`、
既に同期的に加算済み）をインメモリで加算する近似値を使います。
`st.cache_data(ttl=60)` のような時間ベースのキャッシュだけに頼ると、60 秒以内の
連投で上限をすり抜けられるため採用していません。読み取り失敗時は `None` を
返し、判定不能＝ブロックしません（フェイルオープン）。

### 「その回答は完了させてから、次で止める」仕様

ブロック判定はターン開始前（`st.chat_input` を無効化するタイミング）でのみ
行います。確認待ち（`ConfirmationPending`、`render_confirmation()`）の承認/拒否
ボタンはブロック対象にしません — それは開始時点で上限未満だったターンの続きだ
からです。

## 4. 管理者ページのアクセス制御

管理者ページ（`src/admin_page.py`）のアクセス制御は、`st.navigation` から
ページを外すだけに頼らず、**`render_admin_page()` の冒頭で毎回**閲覧者メールを
`ADMIN_EMAILS` と照合します。Streamlit は rerun ごとにスクリプト全体を再実行
するため、この照合は必ず効きます。URL 直打ちを防ぐのはこちらが主防御です
（ナビゲーションから外すのは UX の便宜に過ぎません）。

`ADMIN_ALLOW_LOCAL` はローカル開発時のみ認証なしで管理者ページを開ける
バイパスです。**Streamlit Cloud の Secrets には入れないでください。**

## 5. どこで何が確認できるか

既定のテーブル名は `usage_events` / `user_limits`（+ `user_limits_staging`） /
`prompt_logs`（`src/config.py`、それぞれ `BQ_LOG_TABLE` / `BQ_LIMIT_TABLE` /
`BQ_PROMPT_LOG_TABLE` で変更可）です。管理者ページはこのうち集計・課金判断に
必要な最小限しか見せておらず、それ以外は BigQuery を直接クエリする必要があります。

### 管理者ページ（Streamlit UI、`ADMIN_EMAILS` のみ閲覧可）

**利用状況タブ**（`usage_events` を集計した表示）
- 期間・集計単位（日別/月別）を指定
- 利用者×期間ごとの: 入力トークン・出力トークン・合計トークン・推定金額(USD)・
  クエリ量(バイト)・Gemini呼び出し回数・クエリ実行回数・**エラー数**（合計件数のみ）
- 期間全体の合計メトリクス（合計トークン・推定金額・クエリ量）

**使用制限タブ**（`user_limits` の読み書き）
- 利用者ごとの月次上限(USD)、最終更新日時の一覧
- 全員一括設定・個別編集・保存

### BigQuery を直接見ないとわからないもの

| データ | テーブル | 管理者ページに出ない理由 |
|---|---|---|
| **質問文そのもの**（`prompt_text`） | `prompt_logs` | `log_prompt()` で書き込まれるが、`admin_page.py` はどこからも読んでいない |
| エラーの内訳（`error_source`・`error_code`・`error_text`） | `usage_events`（`event_type='error'`） | 表には合計件数（`COUNTIF`）しか出していない。種類別・内容別に見たいなら SQL で直接見る必要がある |
| `session_id` / `turn_id` 単位の行 | `usage_events` | 表は利用者×期間で集約済み。1 ターン単位の生ログは出ていない |
| 使用モデル名（`model`） | `usage_events` | 集計列に含めていない |
| `thinking_tokens`（思考トークンの内訳） | `usage_events` | 診断用の列で、`output_tokens` 合計にしか反映されない |
| `user_limits_staging` の中身 | `user_limits_staging` | `save_limits()` の中間テーブル。MERGE 後は本体テーブルしか見せない |

個々の質問内容・エラー詳細・生イベントを調べたいときは、GCP コンソールの
BigQuery エディタ等で該当テーブルを直接クエリしてください
（`BQ_LOG_DATASET` は Gemini の allowlist に含まれていないため、
このアプリのチャット経由では参照できません — [1 章](#1-gemini-境界の外に置く理由)参照）。

## 6. 既知の制約

- 月次の使用制限は、同一アカウントを複数タブ・複数デバイスで**同時に**使った
  場合、判定にわずかな誤差が出ることがあります（各セッションが自分の消費量
  だけをリアルタイムに把握するため）。数十人規模の社内利用を前提に許容している
  制約で、厳密な排他制御は行っていません。

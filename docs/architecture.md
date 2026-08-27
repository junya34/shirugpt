# 仕組み

[README](../README.md) の続き。ShiruGPT の内部構造の詳細解説です。

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
| `friendly_error(exc)` | UI（回答本文） | 平易な日本語 |
| `technical_detail(exc)` | Gemini | 構文エラーの内容などをそのまま渡す |

Gemini には技術的詳細を渡します。エラー内容が分からないと自力で SQL を
修正できないためです。ツール実行中の例外は `agent._dispatch()` が捕捉して
`{"error": ...}` として tool response に載せ、**ループは止めません**
（Gemini に再試行させます）。

失敗した `run_query` の SQL と技術的詳細は、回答本文には出しませんが、
画面の「診断情報」expander（折りたたみ表示）には出します。原因調査のために
あえて UI 側にも残していますが、常に見える回答文には混ぜない、という区別です。

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

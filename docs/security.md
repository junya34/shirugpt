# 安全機構の一覧

[README](../README.md) の続き。

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

これらの実装の詳細な説明は [docs/architecture.md](architecture.md) を参照してください。

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
  `git check-ignore` で除外されているかを自動チェックします（[セットアップ 4 章](setup.md#4-認証)参照）。
  それでも `git add -A` の後は `git status` で `credentials/` が出てこないことを確認する習慣を
  持ってください
- **端末外に持ち出さない** — ローカルの `credentials/gcp-secret-key.json` 以外の場所
  （デスクトップ、ダウンロードフォルダ、USBメモリ等）にコピーしないでください
- **不要になったら削除し、必要ならローテーションする** — 端末の紛失・盗難、退職・
  異動、あるいは誤って外部に漏れた可能性がある場合は、ローカルの鍵ファイルを削除する
  だけでなく、GCP コンソールでそのサービスアカウントキーを**無効化**し、新しい鍵を
  発行してください（[セットアップ 3 章](setup.md#3-gcp-側の準備)のロールを持つサービスアカウントの鍵を再発行）
- **付与する権限は必要最小限に保つ** — このサービスアカウントには
  `bigquery.dataViewer` / `bigquery.jobUser` / `aiplatform.user` 以外のロールを
  追加しないでください。権限が大きいほど、鍵が漏れた際の被害も大きくなります

ローカル開発を自分のGCPアカウントで行う場合は、鍵ファイルそのものを作らない
「[認証](setup.md#4-認証)」章の方式 A（ADC）の方が本質的に安全です（短命トークンで
自動更新され、`gcloud auth application-default revoke` で即座に失効できるため）。

## Gemini に送ったデータの扱い

このアプリは Gemini を **Vertex AI（Gemini Enterprise Agent Platform）経由**で
呼び出しています（[src/agent.py](../src/agent.py) の `genai.Client(vertexai=True, ...)`）。
これは一般消費者向けの Gemini アプリ（gemini.google.com）とは別の提供形態で、
データの扱いに関する保証が異なります。

Google Cloud の公式ドキュメントには次のように明記されています。

> Gemini doesn't use your prompts or its responses as data to train its models.
> （Gemini はプロンプトや応答をモデルの学習データとして使用しません）

つまり、このアプリが Gemini に送る内容 — ユーザーの質問文、テーブルのスキーマ、
クエリ結果の要約（[docs/architecture.md](architecture.md) の 4 節「Gemini 境界を越えるものを絞る」で
説明した要約データ）— は、Google 側のモデルの学習には使われません。

ただし、これは「Gemini に送った内容がどこにも残らない」という意味ではありません。
不正利用防止のための一時的なログ保持など、別目的でのデータ処理は行われる場合があります。
正確な保持期間・処理内容は、利用中の Google Cloud の契約条件と最新のデータガバナンス
ドキュメントを直接ご確認ください。

Sources:
- [How Gemini for Google Cloud uses your data | Google Cloud Documentation](https://docs.cloud.google.com/gemini/docs/discover/data-governance)

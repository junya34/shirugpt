"""Vertex AI (Gemini) の Function Calling ループ。

設計上のポイント:
  * スキーマは list_tables → get_table_schema の 2 段階でしか渡さない
  * クエリ結果は要約だけを tool response として返す（生データは渡さない）
  * 取得済みスキーマ・実行済みクエリはコンテキスト側でキャッシュし再取得させない
  * 高コストクエリは ConfirmationPending を返して一旦ループを中断し、
    ユーザーの承認後に同じ地点から再開できる
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import types

from .bq_tools import (
    BigQueryTools,
    BQToolError,
    ConfirmationRequired,
    QueryRun,
    friendly_error,
    technical_detail,
)
from .config import Settings, human_bytes
from .sql_guard import SQLGuardError

SYSTEM_INSTRUCTION = """あなたは BigQuery のデータ分析アシスタントです。
ユーザーの日本語の質問に対し、提供されたツールを使って BigQuery を調べ、日本語で回答します。

## 進め方
1. どのテーブルを見るか不明なときは list_datasets → list_tables でテーブル名を確認する。
2. 使いそうなテーブルにだけ get_table_schema を呼び、列名と型を確認する。
   全テーブルのスキーマを片端から取得してはいけない。多くても 3〜4 テーブルに絞る。
3. 確認した実在の列名だけを使って SQL を組み立て、run_query で実行する。
4. 返ってくるのは行数・列情報・統計・先頭数行のサンプルだけです。
   全行データは画面側でユーザーに表示されるので、あなたが全件を見る必要はありません。
5. 結果をもとに日本語で簡潔に答える。数値は単位を添え、必要なら次に見るとよい観点を一言添える。

## SQL を書くときの規則
- テーブルは必ず `プロジェクトID.データセット.テーブル名` の形でバッククォートで囲む。
- テーブル名・列名に日本語や記号が含まれる場合も必ずバッククォートで囲む。
- 読み取り専用の SELECT 文のみ。INSERT / UPDATE / DELETE / CREATE 等は実行できません。
- SELECT * は避け、必要な列だけを指定する（スキャン量の削減）。
- 件数を数えるだけなら COUNT(*)、傾向を見るなら GROUP BY と集計関数を使う。
  生データを大量に取り出すのではなく、まず集計する。
- パーティション列がある場合は WHERE で期間を絞る。
- 「今月」「先月」などの相対日付は CURRENT_DATE() を基準に計算する。
- 推測した列名を使ってはいけない。必ず get_table_schema で確認した列名を使う。

## エラーへの対処
run_query が error を返したら、内容を読んで SQL を修正し、最大 2 回まで再試行する。
それでも解決しない場合は、何が分からなかったかをユーザーに日本語で説明する。
スキャン量が大きすぎると言われた場合は、期間を絞る・列を減らす・集計に変えるなどして作り直す。

## 回答スタイル
- 日本語で、事実に基づいて簡潔に。データにない内容を推測で補わない。
- 結果が 0 件だったときは「該当なし」と明言し、条件の見直し案を提示する。
- SQL 全文を回答本文に貼り付ける必要はありません（画面に別途表示されます）。
"""


def _sql_key(sql: str) -> str:
    return hashlib.sha256(" ".join(sql.split()).encode("utf-8")).hexdigest()[:16]


@dataclass
class ToolContext:
    """1 会話分のツール実行状態。Streamlit の session_state に載せる。"""

    tools: BigQueryTools
    settings: Settings
    schema_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    listing_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    query_cache: dict[str, QueryRun] = field(default_factory=dict)
    decisions: dict[str, str] = field(default_factory=dict)
    resume_calls: list[Any] | None = None

    # 直近ターンの記録（UI 表示用）
    turn_runs: list[QueryRun] = field(default_factory=list)
    turn_errors: list[str] = field(default_factory=list)
    turn_tool_calls: list[str] = field(default_factory=list)

    def start_turn(self) -> None:
        self.turn_runs = []
        self.turn_errors = []
        self.turn_tool_calls = []


@dataclass
class AnswerResult:
    text: str


@dataclass
class ConfirmationPending:
    key: str
    sql: str
    estimated_bytes: int
    threshold_bytes: int
    notes: list[str] = field(default_factory=list)


class _Pending(Exception):
    def __init__(self, key: str, need: ConfirmationRequired):
        super().__init__("confirmation required")
        self.key = key
        self.need = need


def build_declarations() -> list[types.FunctionDeclaration]:
    return [
        types.FunctionDeclaration(
            name="list_datasets",
            description=(
                "アクセスが許可されている BigQuery データセットの一覧を返す。"
                "どのデータセットを見ればよいか分からないときに最初に呼ぶ。"
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
        types.FunctionDeclaration(
            name="list_tables",
            description=(
                "指定したデータセット内のテーブル名一覧を返す。スキーマ（列情報）は含まれない。"
                "どのテーブルが存在するかを確認するために使う。"
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "dataset": types.Schema(
                        type=types.Type.STRING,
                        description="データセット ID（例: 202506）",
                    )
                },
                required=["dataset"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_table_schema",
            description=(
                "指定した 1 テーブルのスキーマ（列名・型・パーティション情報）を返す。"
                "SQL を書く前に、使う予定のテーブルについてのみ呼ぶこと。"
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "dataset": types.Schema(
                        type=types.Type.STRING, description="データセット ID"
                    ),
                    "table": types.Schema(
                        type=types.Type.STRING,
                        description="テーブル名（list_tables で得た正確な名前）",
                    ),
                },
                required=["dataset", "table"],
            ),
        ),
        types.FunctionDeclaration(
            name="run_query",
            description=(
                "読み取り専用の SELECT クエリを BigQuery で実行し、結果の要約"
                "（行数・列と型・数値統計・先頭数行のサンプル）を返す。"
                "全行データは返らない（ユーザーの画面には別途表示される）。"
                "テーブルは必ずバッククォートで完全修飾すること。"
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "sql": types.Schema(
                        type=types.Type.STRING,
                        description="実行する SELECT 文（1 ステートメントのみ）",
                    ),
                    "purpose": types.Schema(
                        type=types.Type.STRING,
                        description="このクエリで何を確認したいかの短い説明（日本語）",
                    ),
                },
                required=["sql"],
            ),
        ),
    ]


class GeminiAgent:
    def __init__(self, settings: Settings, client: genai.Client | None = None):
        self.settings = settings
        self.client = client or genai.Client(
            vertexai=True,
            project=settings.gcp_project,
            location=settings.vertex_location,
        )
        self.declarations = build_declarations()

    # -- 生成 ---------------------------------------------------------
    def _config(self) -> types.GenerateContentConfig:
        kwargs: dict[str, Any] = {
            "system_instruction": SYSTEM_INSTRUCTION,
            "tools": [types.Tool(function_declarations=self.declarations)],
            "temperature": 0.1,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        }
        if self.settings.thinking_budget is not None:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self.settings.thinking_budget
            )
        return types.GenerateContentConfig(**kwargs)

    def _generate(self, contents: list[types.Content]):
        return self.client.models.generate_content(
            model=self.settings.gemini_model,
            contents=contents,
            config=self._config(),
        )

    # -- ツール実行 ---------------------------------------------------
    def _call_tool(
        self, name: str, args: dict[str, Any], ctx: ToolContext
    ) -> dict[str, Any]:
        if name == "list_datasets":
            if "datasets" not in ctx.listing_cache:
                ctx.listing_cache["datasets"] = ctx.tools.list_datasets()
            return ctx.listing_cache["datasets"]

        if name == "list_tables":
            dataset = str(args.get("dataset", "")).strip()
            cache_key = f"tables:{dataset}"
            if cache_key not in ctx.listing_cache:
                ctx.listing_cache[cache_key] = ctx.tools.list_tables(dataset)
            return ctx.listing_cache[cache_key]

        if name == "get_table_schema":
            dataset = str(args.get("dataset", "")).strip()
            table = str(args.get("table", "")).strip()
            cache_key = f"{dataset}.{table}"
            if cache_key in ctx.schema_cache:
                cached = dict(ctx.schema_cache[cache_key])
                cached["cached"] = True
                return cached
            schema = ctx.tools.get_table_schema(dataset, table)
            ctx.schema_cache[cache_key] = schema
            return schema

        if name == "run_query":
            return self._run_query(args, ctx)

        raise BQToolError(f"未知のツール「{name}」が呼び出されました。")

    def _run_query(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        sql = str(args.get("sql", "")).strip()
        if not sql:
            raise BQToolError("sql が指定されていません。")

        key = _sql_key(sql)

        # 同一 SQL の再実行を防ぐ（再課金と重複表示の回避）
        if key in ctx.query_cache:
            run = ctx.query_cache[key]
            payload = self._query_payload(run)
            payload["cached"] = True
            return payload

        if ctx.decisions.get(key) == "denied":
            return {
                "error": (
                    "スキャン量が大きいため、ユーザーがこのクエリの実行を承認しませんでした。"
                    "対象期間を絞る、列を減らす、集計に変えるなどしてスキャン量を減らした"
                    "別のクエリを作り直してください。"
                )
            }

        approved = ctx.decisions.get(key) == "approved"
        try:
            run = ctx.tools.run_query(sql, approved=approved)
        except ConfirmationRequired as need:
            raise _Pending(key, need) from None

        ctx.query_cache[key] = run
        ctx.turn_runs.append(run)
        return self._query_payload(run)

    @staticmethod
    def _query_payload(run: QueryRun) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "executed_sql": run.sql,
            "bytes_processed": human_bytes(run.estimated_bytes),
            "result": run.summary,
        }
        if run.notes:
            payload["notes"] = run.notes
        return payload

    def _dispatch(self, call: Any, ctx: ToolContext) -> types.Part:
        name = call.name
        args = dict(call.args or {})
        label = name
        if name == "run_query" and args.get("purpose"):
            label = f"run_query（{args['purpose']}）"
        elif name in ("list_tables", "get_table_schema"):
            detail = ".".join(
                str(args[k]) for k in ("dataset", "table") if args.get(k)
            )
            label = f"{name}（{detail}）"
        ctx.turn_tool_calls.append(label)

        try:
            payload = self._call_tool(name, args, ctx)
        except _Pending:
            raise
        except (SQLGuardError, BQToolError) as exc:
            ctx.turn_errors.append(str(exc))
            payload = {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - Gemini に自己修正させる
            ctx.turn_errors.append(friendly_error(exc))
            payload = {
                "error": friendly_error(exc),
                "technical_detail": technical_detail(exc),
            }

        return types.Part.from_function_response(name=name, response=payload)

    # -- ループ本体 ---------------------------------------------------
    def run(
        self, contents: list[types.Content], ctx: ToolContext
    ) -> AnswerResult | ConfirmationPending:
        """1 ターンを最後まで進める。確認が必要な場合は中断して状態を残す。

        contents は破壊的に更新される（Gemini の応答とツール結果を追記）。
        """
        pending_calls = ctx.resume_calls
        ctx.resume_calls = None

        for _ in range(self.settings.max_tool_iterations):
            if pending_calls is None:
                response = self._generate(contents)
                candidate = (response.candidates or [None])[0]
                if candidate is None or candidate.content is None:
                    return AnswerResult(
                        "モデルから有効な応答が得られませんでした。質問を言い換えてお試しください。"
                    )

                parts = candidate.content.parts or []
                calls = [p.function_call for p in parts if p.function_call]
                text = "".join(p.text for p in parts if p.text)

                if not calls:
                    if not text and str(candidate.finish_reason).endswith("MAX_TOKENS"):
                        return AnswerResult(
                            "回答が長くなりすぎて途中で打ち切られました。"
                            "質問をもう少し絞ってお試しください。"
                        )
                    return AnswerResult(
                        text or "回答を生成できませんでした。質問を言い換えてお試しください。"
                    )

                contents.append(candidate.content)
                pending_calls = calls

            try:
                responses = [self._dispatch(call, ctx) for call in pending_calls]
            except _Pending as pending:
                # ここで中断。承認後に同じ pending_calls から再開する。
                ctx.resume_calls = pending_calls
                return ConfirmationPending(
                    key=pending.key,
                    sql=pending.need.sql,
                    estimated_bytes=pending.need.estimated_bytes,
                    threshold_bytes=pending.need.threshold_bytes,
                    notes=list(pending.need.notes),
                )

            contents.append(types.Content(role="user", parts=responses))
            pending_calls = None

        return AnswerResult(
            "調査の手順が想定より多くなったため、途中で打ち切りました。"
            "質問をもう少し具体的にしていただけますか。"
        )

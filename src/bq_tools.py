"""BigQuery 側のツール実装。

Gemini から Function Calling で呼ばれる 4 つの関数を提供する:
    list_datasets / list_tables / get_table_schema / run_query

認証は ADC（Application Default Credentials）に委ねる。
このモジュールは認証情報を一切生成・保持しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from .config import Settings, human_bytes
from .sql_guard import GuardResult, SQLGuardError, check_sql
from .summarize import summarize_dataframe


class BQToolError(RuntimeError):
    """ツール実行時の想定内エラー（Gemini とユーザーの双方に返す）。"""


@dataclass
class ConfirmationRequired(Exception):
    """dry run の推定スキャン量が閾値を超えたため、実行前に確認が必要。"""

    sql: str
    original_sql: str
    estimated_bytes: int
    threshold_bytes: int
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - 表示用
        return (
            f"推定スキャン量 {human_bytes(self.estimated_bytes)} が閾値 "
            f"{human_bytes(self.threshold_bytes)} を超えています。"
        )


@dataclass
class QueryRun:
    """1 回のクエリ実行の結果。dataframe は UI 用、summary のみ Gemini に渡す。"""

    sql: str
    original_sql: str
    estimated_bytes: int
    billed_bytes: int
    dataframe: pd.DataFrame
    summary: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    truncated: bool = False
    purpose: str = ""


def _flatten_schema(
    fields: list[bigquery.SchemaField], prefix: str = ""
) -> list[dict[str, Any]]:
    """ネストした RECORD 型をドット区切りの列名に展開する。"""
    out: list[dict[str, Any]] = []
    for field_ in fields:
        name = f"{prefix}{field_.name}"
        entry: dict[str, Any] = {"name": name, "type": field_.field_type}
        if field_.mode and field_.mode != "NULLABLE":
            entry["mode"] = field_.mode
        if field_.description:
            entry["description"] = field_.description
        out.append(entry)
        if field_.field_type in ("RECORD", "STRUCT") and field_.fields:
            out.extend(_flatten_schema(list(field_.fields), prefix=f"{name}."))
    return out


class BigQueryTools:
    """allowlist で保護された BigQuery アクセス。"""

    def __init__(self, settings: Settings, client: bigquery.Client | None = None):
        self.settings = settings
        self.client = client or bigquery.Client(
            project=settings.gcp_project,
            location=settings.bq_location,
        )

    # -- allowlist ---------------------------------------------------
    def _require_allowed(self, dataset: str) -> str:
        dataset = (dataset or "").strip().strip("`")
        if not dataset:
            raise BQToolError("データセット名が指定されていません。")
        if not self.settings.is_dataset_allowed(dataset):
            raise BQToolError(
                f"データセット「{dataset}」へのアクセスは許可されていません。"
                f"利用できるのは次のみです: {', '.join(self.settings.allowed_datasets)}"
            )
        return dataset

    # -- tools -------------------------------------------------------
    def list_datasets(self) -> dict[str, Any]:
        """allowlist に載っていて、かつ実在するデータセットを返す。"""
        available: list[dict[str, Any]] = []
        missing: list[str] = []
        for name in self.settings.allowed_datasets:
            ref = bigquery.DatasetReference(self.settings.gcp_project, name)
            try:
                dataset = self.client.get_dataset(ref)
            except gexc.NotFound:
                missing.append(name)
                continue
            except gexc.Forbidden as exc:
                raise BQToolError(
                    f"データセット「{name}」を参照する権限がありません。"
                ) from exc
            entry: dict[str, Any] = {"dataset": dataset.dataset_id}
            if dataset.location:
                entry["location"] = dataset.location
            if dataset.description:
                entry["description"] = dataset.description
            available.append(entry)

        result: dict[str, Any] = {
            "project": self.settings.gcp_project,
            "datasets": available,
        }
        if missing:
            result["not_found"] = missing
        return result

    def list_tables(self, dataset: str) -> dict[str, Any]:
        """データセット内のテーブル一覧。スキーマは含めない（トークン節約）。"""
        dataset_id = self._require_allowed(dataset)
        ref = bigquery.DatasetReference(self.settings.gcp_project, dataset_id)
        try:
            items = list(self.client.list_tables(ref))
        except gexc.NotFound as exc:
            raise BQToolError(
                f"データセット「{dataset_id}」が見つかりません。"
            ) from exc
        except gexc.Forbidden as exc:
            raise BQToolError(
                f"データセット「{dataset_id}」を参照する権限がありません。"
            ) from exc

        tables = [
            {"table": item.table_id, "kind": (item.table_type or "TABLE")}
            for item in items
        ]
        return {
            "project": self.settings.gcp_project,
            "dataset": dataset_id,
            "table_count": len(tables),
            "tables": tables,
        }

    def get_table_schema(self, dataset: str, table: str) -> dict[str, Any]:
        """1 テーブル分のスキーマだけを返す。"""
        dataset_id = self._require_allowed(dataset)
        table_id = (table or "").strip().strip("`")
        if not table_id:
            raise BQToolError("テーブル名が指定されていません。")

        ref = bigquery.TableReference(
            bigquery.DatasetReference(self.settings.gcp_project, dataset_id),
            table_id,
        )
        try:
            meta = self.client.get_table(ref)
        except gexc.NotFound as exc:
            raise BQToolError(
                f"テーブル `{self.settings.gcp_project}.{dataset_id}.{table_id}` が"
                "見つかりません。list_tables で正確なテーブル名を確認してください。"
            ) from exc
        except gexc.Forbidden as exc:
            raise BQToolError(
                f"テーブル「{table_id}」を参照する権限がありません。"
            ) from exc

        info: dict[str, Any] = {
            "full_name": f"{self.settings.gcp_project}.{dataset_id}.{table_id}",
            "sql_reference": f"`{self.settings.gcp_project}.{dataset_id}.{table_id}`",
            "kind": meta.table_type,
            "columns": _flatten_schema(list(meta.schema)),
        }
        if meta.num_rows is not None:
            info["num_rows"] = int(meta.num_rows)
        if meta.num_bytes is not None:
            info["size"] = human_bytes(meta.num_bytes)
        if meta.description:
            info["description"] = meta.description

        # パーティション / クラスタリングはスキャン量削減のヒントになる
        if meta.time_partitioning:
            info["time_partitioning"] = {
                "field": meta.time_partitioning.field or "_PARTITIONTIME",
                "type": meta.time_partitioning.type_,
            }
            info["cost_hint"] = (
                "パーティション列で WHERE 絞り込みを行うとスキャン量を大幅に削減できます。"
            )
        if meta.range_partitioning:
            info["range_partitioning"] = {"field": meta.range_partitioning.field}
        if meta.clustering_fields:
            info["clustering_fields"] = list(meta.clustering_fields)

        return info

    # -- クエリ実行 ---------------------------------------------------
    def dry_run(self, sql: str) -> int:
        """推定スキャンバイト数を返す（課金なし）。"""
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = self.client.query(sql, job_config=job_config, location=self.settings.bq_location)
        return int(job.total_bytes_processed or 0)

    def prepare_query(self, sql: str) -> tuple[GuardResult, int]:
        """SQL を検査・整形し、dry run で推定スキャン量を得る。"""
        guard = check_sql(
            sql,
            project=self.settings.gcp_project,
            allowed_datasets=self.settings.allowed_datasets,
            default_row_limit=self.settings.default_row_limit,
        )
        estimated = self.dry_run(guard.sql)
        return guard, estimated

    def execute(
        self, guard: GuardResult, estimated_bytes: int, *, purpose: str = ""
    ) -> QueryRun:
        """検査済みの SQL を実行して DataFrame と要約を返す。"""
        job_config = bigquery.QueryJobConfig(
            maximum_bytes_billed=self.settings.max_bytes_billed,
            use_query_cache=True,
        )
        job = self.client.query(
            guard.sql, job_config=job_config, location=self.settings.bq_location
        )
        cap = self.settings.max_result_rows
        rows = job.result(max_results=cap + 1)
        df = rows.to_dataframe(create_bqstorage_client=False)

        truncated = len(df) > cap
        if truncated:
            df = df.head(cap)

        notes = list(guard.notes)
        if truncated:
            notes.append(
                f"結果が MAX_RESULT_ROWS({cap}) を超えたため、先頭 {cap} 行のみ読み込みました。"
            )

        summary = summarize_dataframe(
            df, sample_rows=self.settings.sample_rows, truncated=truncated
        )

        return QueryRun(
            sql=guard.sql,
            original_sql=guard.original_sql,
            estimated_bytes=estimated_bytes,
            billed_bytes=int(job.total_bytes_billed or 0),
            dataframe=df,
            summary=summary,
            notes=notes,
            truncated=truncated,
            purpose=purpose,
        )

    def run_query(
        self, sql: str, *, approved: bool | None = None, purpose: str = ""
    ) -> QueryRun:
        """検査 → dry run → （必要なら確認）→ 実行。

        approved が None のまま閾値を超えた場合は ConfirmationRequired を送出する。
        approved=True なら閾値超過でもそのまま実行する。
        """
        guard, estimated = self.prepare_query(sql)

        if estimated > self.settings.max_bytes_billed:
            raise BQToolError(
                f"推定スキャン量 {human_bytes(estimated)} が上限 "
                f"{human_bytes(self.settings.max_bytes_billed)} を超えるため実行できません。"
                "期間や列を絞り込んでください。"
            )

        if estimated > self.settings.dry_run_confirm_bytes and not approved:
            raise ConfirmationRequired(
                sql=guard.sql,
                original_sql=guard.original_sql,
                estimated_bytes=estimated,
                threshold_bytes=self.settings.dry_run_confirm_bytes,
                notes=list(guard.notes),
            )

        return self.execute(guard, estimated, purpose=purpose)


# -- エラーメッセージの日本語化 ---------------------------------------
def friendly_error(exc: Exception) -> str:
    """生の例外を、ユーザー向けの分かりやすい日本語に変換する。"""
    if isinstance(exc, (SQLGuardError, BQToolError)):
        return str(exc)

    text = str(exc)

    # API 未有効化（初回セットアップで頻出）
    if "has not been used in project" in text or "SERVICE_DISABLED" in text:
        api = "必要な API"
        if "aiplatform" in text or "Agent Platform" in text:
            # コンソール上の表示名は Agent Platform API（旧 Vertex AI API）。
            # サービス名は aiplatform.googleapis.com のまま
            api = "Agent Platform API（旧 Vertex AI API / aiplatform.googleapis.com）"
        elif "bigquery" in text:
            api = "BigQuery API (bigquery.googleapis.com)"
        return (
            f"{api} が GCP プロジェクトで有効化されていません。\n\n"
            "次のコマンドで有効化してから、数分待って再度お試しください。\n\n"
            "```\n"
            "gcloud services enable aiplatform.googleapis.com bigquery.googleapis.com \\\n"
            "  --project <プロジェクトID>\n"
            "```"
        )

    # サービスアカウントに必要なロールが無い
    if "aiplatform.endpoints.predict" in text or "aiplatform.user" in text:
        return (
            "Gemini を呼び出す権限がありません。"
            "使用中のアカウントに「Vertex AI ユーザー」"
            "(roles/aiplatform.user) を付与してください。"
        )

    if isinstance(exc, gexc.NotFound):
        return (
            "参照先のテーブルまたはデータセットが見つかりませんでした。"
            "テーブル名を確認するか、質問を言い換えて再度お試しください。"
        )
    if isinstance(exc, gexc.Forbidden):
        return (
            "BigQuery へのアクセス権限が不足しています。"
            "GCP 側のロール（BigQuery データ閲覧者 / ジョブユーザー）をご確認ください。"
        )
    if isinstance(exc, gexc.Unauthorized):
        return (
            "認証に失敗しました。ターミナルで "
            "`gcloud auth application-default login` を実行してから再度お試しください。"
        )
    if isinstance(exc, gexc.BadRequest):
        message = str(exc)
        if "maximum bytes billed" in message.lower():
            return (
                "クエリのスキャン量が上限を超えたため中止されました。"
                "対象期間や列を絞り込んでください。"
            )
        if "syntax error" in message.lower():
            return (
                "生成された SQL に構文エラーがありました。"
                "質問を具体的に言い換えると改善する場合があります。"
            )
        return "クエリを実行できませんでした。条件を変えて再度お試しください。"
    if isinstance(exc, gexc.TooManyRequests):
        return "リクエストが集中しています。少し時間をおいて再度お試しください。"
    if isinstance(exc, gexc.ServiceUnavailable):
        return "BigQuery 側が一時的に応答していません。少し待って再度お試しください。"

    name = type(exc).__name__
    if "DefaultCredentials" in name:
        return (
            "GCP の認証情報が見つかりません。ターミナルで "
            "`gcloud auth application-default login` を実行してください。"
        )
    return "処理中に問題が発生しました。内容を変えて再度お試しください。"


def technical_detail(exc: Exception, limit: int = 600) -> str:
    """Gemini の自己修正用に、技術的なエラー内容を短く整形する。"""
    text = f"{type(exc).__name__}: {exc}"
    return text[:limit]

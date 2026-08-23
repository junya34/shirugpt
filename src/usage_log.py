"""利用者ごとの使用量を BigQuery に記録する。

**このモジュールは Gemini 境界の外側にある。** Gemini からは絶対に呼ばれない。

- 書き込みは `insert_rows_json()` による JSON 行の直接挿入で、文字列 SQL を
  一切組み立てない。したがって `sql_guard.check_sql()` を通す経路とは無関係。
- 記録先データセット（`BQ_LOG_DATASET`）は `BQ_ALLOWED_DATASETS` の外に置く。
  混入していた場合は `config.load_settings()` が起動時に落とす。
- `log_*` は例外を外に漏らさない。ログが書けなくてもチャットは止めない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from .config import Settings, bq_cost_usd, gemini_cost_usd

logger = logging.getLogger(__name__)

_JST = ZoneInfo("Asia/Tokyo")

# メールが取れないローカル実行などで使う。NULL / 空文字は GROUP BY で紛れるため
# 明示的な定数を入れる。
UNKNOWN_USER = "(unknown)"

EVENT_GEMINI_CALL = "gemini_call"
EVENT_BQ_QUERY = "bq_query"
EVENT_ERROR = "error"

# insert_rows_json のタイムアウト（秒）。BigQuery の一時障害が
# ユーザーのチャット応答を長時間ブロックしないようにする。
_INSERT_TIMEOUT = 5.0

# 連続失敗がこの回数に達したらプロセスが生きている間はログを諦める。
# 権限不足などが恒久的な場合、毎イベントで失敗する API 往復を繰り返すと
# チャットの応答が目に見えて遅くなるため、無駄打ちに上限を設ける。
_MAX_FAILURES = 5

_SCHEMA = [
    bigquery.SchemaField("event_time", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("event_type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_email", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("turn_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("model", "STRING"),
    bigquery.SchemaField("prompt_tokens", "INTEGER"),
    # output_tokens は課金・UI・週次制限の既存計算と互換性を保つため、
    # これまでと同じ意味（thinking 分を含む合計）のまま変更しない。
    # thinking の内訳だけを別途 thinking_tokens に記録する（診断用、
    # 課金計算には使わない。output_tokens との二重計上ではなく内訳）。
    bigquery.SchemaField("output_tokens", "INTEGER"),
    bigquery.SchemaField("thinking_tokens", "INTEGER"),
    bigquery.SchemaField("total_tokens", "INTEGER"),
    bigquery.SchemaField("billed_bytes", "INTEGER"),
    # event_type = "error" のときだけ使う列。
    # error_source: "empty_response" | "tool_error" | "gemini_call_error"
    bigquery.SchemaField("error_source", "STRING"),
    bigquery.SchemaField("error_code", "INTEGER"),
    # Gemini に渡す technical_detail（600文字）とは別に、こちらは BigQuery
    # 保存用でトークン予算の制約が無いため切り詰めない（実測では数百文字程度）。
    bigquery.SchemaField("error_text", "STRING"),
]

_PROMPT_SCHEMA = [
    bigquery.SchemaField("turn_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("user_email", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("event_time", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("prompt_text", "STRING", mode="REQUIRED"),
]

_LIMIT_SCHEMA = [
    bigquery.SchemaField("user_email", "STRING", mode="REQUIRED"),
    # 列名は weekly_limit_usd のまま（週次→月次への切り替え時に BigQuery の
    # 列名までは変更していない。デプロイ済みテーブルの移行を避けるための
    # 意図的な判断。値の意味は「月間の USD 上限」に変わっている）。
    bigquery.SchemaField("weekly_limit_usd", "FLOAT", mode="REQUIRED"),
    bigquery.SchemaField("updated_at", "TIMESTAMP", mode="REQUIRED"),
]


def jst_month_bounds_utc(now_utc: datetime | None = None) -> tuple[datetime, datetime]:
    """「今月」（JST 1日 0:00 〜 次月1日 0:00 未満）を UTC の [開始, 終了) で返す。

    `datetime.now()`（サーバーのシステム TZ 依存）は使わず、常に UTC を
    経由して JST の月境界を計算する。
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        raise ValueError("now_utc は timezone-aware でなければなりません")

    now_jst = now_utc.astimezone(_JST)
    first_of_month: date = now_jst.date().replace(day=1)
    start_jst = datetime.combine(first_of_month, time.min, tzinfo=_JST)
    if first_of_month.month == 12:
        next_month = first_of_month.replace(year=first_of_month.year + 1, month=1)
    else:
        next_month = first_of_month.replace(month=first_of_month.month + 1)
    end_jst = datetime.combine(next_month, time.min, tzinfo=_JST)
    return start_jst.astimezone(timezone.utc), end_jst.astimezone(timezone.utc)


@dataclass
class MonthlyUsage:
    prompt_tokens: int
    output_tokens: int
    billed_bytes: int

    def cost_usd(self) -> float:
        return gemini_cost_usd(self.prompt_tokens, self.output_tokens) + bq_cost_usd(
            self.billed_bytes
        )


class UsageLogger:
    """使用量イベントの記録と、管理者ページ向けの読み出し。"""

    def __init__(self, settings: Settings, client: bigquery.Client | None = None):
        self.settings = settings
        self._client = client
        self._ready = False
        self._failures = 0

    # -- 内部 ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.settings.logging_enabled and self._failures < _MAX_FAILURES

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= _MAX_FAILURES:
            logger.warning(
                "利用ログの書き込みが %d 回続けて失敗したため、"
                "このプロセスでは記録を諦めます（チャットは通常どおり動作します）。",
                self._failures,
            )

    @property
    def table_id(self) -> str:
        return (
            f"{self.settings.gcp_project}."
            f"{self.settings.log_dataset}.{self.settings.log_table}"
        )

    @property
    def limits_table_id(self) -> str:
        return (
            f"{self.settings.gcp_project}."
            f"{self.settings.log_dataset}.{self.settings.limit_table}"
        )

    @property
    def _limits_staging_table_id(self) -> str:
        return f"{self.limits_table_id}_staging"

    @property
    def prompt_log_table_id(self) -> str:
        return (
            f"{self.settings.gcp_project}."
            f"{self.settings.log_dataset}.{self.settings.prompt_log_table}"
        )

    def _get_client(self) -> bigquery.Client:
        if self._client is None:
            self._client = bigquery.Client(
                project=self.settings.gcp_project,
                location=self.settings.bq_location,
            )
        return self._client

    def _create_dataset(self, client: bigquery.Client) -> None:
        dataset_ref = bigquery.Dataset(
            f"{self.settings.gcp_project}.{self.settings.log_dataset}"
        )
        if self.settings.bq_location:
            dataset_ref.location = self.settings.bq_location
        client.create_dataset(dataset_ref, exists_ok=True)

    def ensure_table(self) -> None:
        """データセット・イベントログ・制限テーブル・プロンプトログを用意する。

        チャットの高頻度書き込み経路から呼ばれるため、失敗しても例外を
        投げない（`_record_failure()` で circuit breaker に記録するだけ）。
        成功したときだけ内部フラグを立てる。失敗はキャッシュしないので、
        権限が後から付与されれば次の呼び出しで自然に回復する。
        """
        if not self.enabled or self._ready:
            return
        try:
            client = self._get_client()
            self._create_dataset(client)

            table = bigquery.Table(self.table_id, schema=_SCHEMA)
            # 管理者ページのクエリは常に日付範囲で絞るのでパーティションが効く。
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY, field="event_time"
            )
            table.clustering_fields = ["user_email", "event_type"]
            client.create_table(table, exists_ok=True)

            client.create_table(
                bigquery.Table(self.limits_table_id, schema=_LIMIT_SCHEMA),
                exists_ok=True,
            )
            client.create_table(
                bigquery.Table(self._limits_staging_table_id, schema=_LIMIT_SCHEMA),
                exists_ok=True,
            )

            prompt_table = bigquery.Table(self.prompt_log_table_id, schema=_PROMPT_SCHEMA)
            prompt_table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY, field="event_time"
            )
            prompt_table.clustering_fields = ["user_email"]
            client.create_table(prompt_table, exists_ok=True)

            self._ready = True
            self._failures = 0
        except Exception:  # noqa: BLE001 - ログ機能の失敗でアプリを止めない
            logger.warning("利用ログテーブルの初期化に失敗しました。", exc_info=True)
            self._record_failure()

    def _ensure_limits_infra(self) -> None:
        """制限テーブルの用意（管理者操作専用）。

        書き込み側の circuit breaker（`enabled`）には従わない。チャットの
        ログ書き込みが何度失敗していても、管理者は制限を設定できるべき
        だから。失敗時は例外をそのまま上げる（`save_limits()` の契約）。
        """
        client = self._get_client()
        self._create_dataset(client)
        client.create_table(
            bigquery.Table(self.limits_table_id, schema=_LIMIT_SCHEMA),
            exists_ok=True,
        )
        client.create_table(
            bigquery.Table(self._limits_staging_table_id, schema=_LIMIT_SCHEMA),
            exists_ok=True,
        )

    def _insert(self, row: dict[str, Any], table_id: str | None = None) -> None:
        if not self.enabled:
            return
        self.ensure_table()
        if not self._ready:
            return
        try:
            errors = self._get_client().insert_rows_json(
                table_id or self.table_id, [row], timeout=_INSERT_TIMEOUT
            )
            if errors:
                logger.warning("利用ログの書き込みでエラー: %s", errors)
                self._record_failure()
        except Exception:  # noqa: BLE001 - チャットを止めないため必ず握る
            logger.warning("利用ログの書き込みに失敗しました。", exc_info=True)
            self._record_failure()

    # -- 記録（例外を外に出さない） ------------------------------------
    def log_prompt(
        self,
        *,
        user_email: str,
        session_id: str,
        turn_id: str,
        prompt_text: str,
    ) -> None:
        """ユーザーが入力したプロンプトを1ターン1行で記録する。"""
        self._insert(
            {
                "turn_id": turn_id,
                "session_id": session_id,
                "user_email": user_email or UNKNOWN_USER,
                "event_time": _now_iso(),
                "prompt_text": prompt_text,
            },
            table_id=self.prompt_log_table_id,
        )

    def log_gemini_call(
        self,
        *,
        user_email: str,
        session_id: str,
        turn_id: str,
        prompt_tokens: int,
        output_tokens: int,
        total_tokens: int,
        thinking_tokens: int = 0,
    ) -> None:
        self._insert(
            {
                "event_time": _now_iso(),
                "event_type": EVENT_GEMINI_CALL,
                "user_email": user_email or UNKNOWN_USER,
                "session_id": session_id,
                "turn_id": turn_id,
                "model": self.settings.gemini_model,
                "prompt_tokens": int(prompt_tokens),
                "output_tokens": int(output_tokens),
                "thinking_tokens": int(thinking_tokens),
                "total_tokens": int(total_tokens),
            }
        )

    def log_bq_query(
        self,
        *,
        user_email: str,
        session_id: str,
        turn_id: str,
        billed_bytes: int,
    ) -> None:
        self._insert(
            {
                "event_time": _now_iso(),
                "event_type": EVENT_BQ_QUERY,
                "user_email": user_email or UNKNOWN_USER,
                "session_id": session_id,
                "turn_id": turn_id,
                "billed_bytes": int(billed_bytes),
            }
        )

    def log_error(
        self,
        *,
        user_email: str,
        session_id: str,
        turn_id: str,
        error_source: str,
        error_code: int | None = None,
        error_text: str | None = None,
    ) -> None:
        """エラー・診断情報を1行記録する。

        error_source: "empty_response"（Gemini が空応答を返した）/
        "tool_error"（ツール実行エラー）/ "gemini_call_error"（Gemini API
        呼び出し自体の失敗）のいずれか。
        """
        self._insert(
            {
                "event_time": _now_iso(),
                "event_type": EVENT_ERROR,
                "user_email": user_email or UNKNOWN_USER,
                "session_id": session_id,
                "turn_id": turn_id,
                "error_source": error_source,
                "error_code": error_code,
                "error_text": error_text,
            }
        )

    # -- 読み出し（管理者ページ用。ここは例外を上げてよい） --------------
    def query_usage(self, start: Any, end: Any) -> pd.DataFrame:
        """[start, end] の日付範囲を日次×利用者で集計して返す。

        `end` の当日分を含めるため、SQL 側では end + 1 日未満で比較する。
        SQL はコード側で固定し、日付はクエリパラメータで渡す（UI からの
        フリーテキストを SQL に混ぜない）。

        判定に `self.enabled` は使わない。あれは書き込み失敗の打ち切りを
        含むため、書き込みが失敗していても読み取りは可能にする。
        """
        if not self.settings.logging_enabled:
            return pd.DataFrame(
                columns=[
                    "event_date",
                    "user_email",
                    "prompt_tokens",
                    "output_tokens",
                    "total_tokens",
                    "billed_bytes",
                    "gemini_calls",
                    "bq_queries",
                ]
            )

        sql = f"""
            SELECT
              DATE(event_time) AS event_date,
              user_email,
              SUM(IFNULL(prompt_tokens, 0)) AS prompt_tokens,
              SUM(IFNULL(output_tokens, 0)) AS output_tokens,
              SUM(IFNULL(total_tokens, 0))  AS total_tokens,
              SUM(IFNULL(billed_bytes, 0))  AS billed_bytes,
              COUNTIF(event_type = '{EVENT_GEMINI_CALL}') AS gemini_calls,
              COUNTIF(event_type = '{EVENT_BQ_QUERY}')    AS bq_queries
            FROM `{self.table_id}`
            WHERE event_time >= TIMESTAMP(@start)
              AND event_time <  TIMESTAMP(DATE_ADD(@end, INTERVAL 1 DAY))
            GROUP BY event_date, user_email
            ORDER BY event_date, user_email
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start", "DATE", start),
                bigquery.ScalarQueryParameter("end", "DATE", end),
            ],
            maximum_bytes_billed=self.settings.max_bytes_billed,
            use_query_cache=True,
        )
        job = self._get_client().query(
            sql, job_config=job_config, location=self.settings.bq_location
        )
        return job.result().to_dataframe(create_bqstorage_client=False)

    def monthly_usage(
        self, user_email: str, start_utc: datetime, end_utc: datetime
    ) -> MonthlyUsage:
        """[start_utc, end_utc) の Gemini トークン量 + BigQuery 課金バイト量を
        1利用者分だけ集計する（月次の使用制限はこの両方を対象にする）。

        `enabled`（書き込み側 circuit breaker）は見ない。読み取りは
        `query_usage()` と同じ扱い。
        """
        if not self.settings.logging_enabled:
            return MonthlyUsage(prompt_tokens=0, output_tokens=0, billed_bytes=0)

        sql = f"""
            SELECT
              SUM(IF(event_type = '{EVENT_GEMINI_CALL}', IFNULL(prompt_tokens, 0), 0))
                AS prompt_tokens,
              SUM(IF(event_type = '{EVENT_GEMINI_CALL}', IFNULL(output_tokens, 0), 0))
                AS output_tokens,
              SUM(IF(event_type = '{EVENT_BQ_QUERY}', IFNULL(billed_bytes, 0), 0))
                AS billed_bytes
            FROM `{self.table_id}`
            WHERE user_email = @user_email
              AND event_time >= @start
              AND event_time <  @end
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("user_email", "STRING", user_email),
                bigquery.ScalarQueryParameter("start", "TIMESTAMP", start_utc),
                bigquery.ScalarQueryParameter("end", "TIMESTAMP", end_utc),
            ],
            maximum_bytes_billed=self.settings.max_bytes_billed,
            use_query_cache=True,
        )
        job = self._get_client().query(
            sql, job_config=job_config, location=self.settings.bq_location
        )
        row = next(iter(job.result()), None)
        if row is None:
            return MonthlyUsage(prompt_tokens=0, output_tokens=0, billed_bytes=0)
        return MonthlyUsage(
            prompt_tokens=int(row.prompt_tokens or 0),
            output_tokens=int(row.output_tokens or 0),
            billed_bytes=int(row.billed_bytes or 0),
        )

    def get_limits(self) -> pd.DataFrame:
        """`user_limits` の全件を返す。テーブルが無ければ空の DataFrame。"""
        columns = ["user_email", "weekly_limit_usd", "updated_at"]
        if not self.settings.logging_enabled:
            return pd.DataFrame(columns=columns)
        try:
            job = self._get_client().query(
                f"SELECT {', '.join(columns)} FROM `{self.limits_table_id}`",
                location=self.settings.bq_location,
            )
            return job.result().to_dataframe(create_bqstorage_client=False)
        except gexc.NotFound:
            return pd.DataFrame(columns=columns)

    def list_known_users(self) -> list[str]:
        """利用履歴があるか、制限が既に設定されている利用者のメール一覧。"""
        if not self.settings.logging_enabled:
            return []
        sql = f"""
            SELECT user_email FROM `{self.table_id}`
            WHERE user_email != '{UNKNOWN_USER}'
            UNION DISTINCT
            SELECT user_email FROM `{self.limits_table_id}`
        """
        try:
            job = self._get_client().query(sql, location=self.settings.bq_location)
            return sorted(row.user_email for row in job.result())
        except gexc.NotFound:
            return []

    def save_limits(self, updates: pd.DataFrame) -> None:
        """利用者ごとの月次制限額を upsert する（他の利用者の行は変更しない）。

        ステージングテーブルへ全件 WRITE_TRUNCATE で書き込んだ上で MERGE する。
        `user_limits` 自体には streaming insert を一切行わないので、
        ストリーミングバッファ由来の DML 制約は最初から関係ない。
        MERGE 中の同時実行は BigQuery 側が競合エラーとして検出するため、
        例外をそのまま呼び出し元（管理者ページ）に上げる。
        """
        if updates.empty:
            return

        updates = updates[["user_email", "weekly_limit_usd"]].copy()
        updates["user_email"] = updates["user_email"].astype(str).str.strip()
        if (updates["user_email"].isin(["", UNKNOWN_USER])).any():
            raise ValueError("user_email が空、または予約語です。")
        if (updates["weekly_limit_usd"].astype(float) < 0).any():
            raise ValueError("weekly_limit_usd は 0 以上にしてください。")
        updates = updates.drop_duplicates(subset="user_email", keep="last")
        updates["weekly_limit_usd"] = updates["weekly_limit_usd"].astype(float)
        updates["updated_at"] = datetime.now(timezone.utc)

        self._ensure_limits_infra()
        client = self._get_client()
        client.load_table_from_dataframe(
            updates,
            self._limits_staging_table_id,
            job_config=bigquery.LoadJobConfig(
                schema=_LIMIT_SCHEMA,
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            ),
        ).result()

        client.query(
            f"""
            MERGE `{self.limits_table_id}` AS target
            USING `{self._limits_staging_table_id}` AS source
            ON target.user_email = source.user_email
            WHEN MATCHED THEN UPDATE SET
              weekly_limit_usd = source.weekly_limit_usd,
              updated_at = source.updated_at
            WHEN NOT MATCHED THEN
              INSERT (user_email, weekly_limit_usd, updated_at)
              VALUES (source.user_email, source.weekly_limit_usd, source.updated_at)
            """,
            location=self.settings.bq_location,
        ).result()


def _now_iso() -> str:
    """挿入用の現在時刻（UTC, ISO 8601）。"""
    return datetime.now(timezone.utc).isoformat()

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
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from google.cloud import bigquery

from .config import Settings

logger = logging.getLogger(__name__)

# メールが取れないローカル実行などで使う。NULL / 空文字は GROUP BY で紛れるため
# 明示的な定数を入れる。
UNKNOWN_USER = "(unknown)"

EVENT_GEMINI_CALL = "gemini_call"
EVENT_BQ_QUERY = "bq_query"

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
    bigquery.SchemaField("output_tokens", "INTEGER"),
    bigquery.SchemaField("total_tokens", "INTEGER"),
    bigquery.SchemaField("billed_bytes", "INTEGER"),
]


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

    def _get_client(self) -> bigquery.Client:
        if self._client is None:
            self._client = bigquery.Client(
                project=self.settings.gcp_project,
                location=self.settings.bq_location,
            )
        return self._client

    def ensure_table(self) -> None:
        """データセットとテーブルを用意する。失敗しても例外を投げない。

        成功したときだけ内部フラグを立てる。失敗はキャッシュしないので、
        権限が後から付与されれば次の呼び出しで自然に回復する。
        """
        if not self.enabled or self._ready:
            return
        try:
            client = self._get_client()
            dataset_ref = bigquery.Dataset(
                f"{self.settings.gcp_project}.{self.settings.log_dataset}"
            )
            if self.settings.bq_location:
                dataset_ref.location = self.settings.bq_location
            client.create_dataset(dataset_ref, exists_ok=True)

            table = bigquery.Table(self.table_id, schema=_SCHEMA)
            # 管理者ページのクエリは常に日付範囲で絞るのでパーティションが効く。
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY, field="event_time"
            )
            table.clustering_fields = ["user_email", "event_type"]
            client.create_table(table, exists_ok=True)
            self._ready = True
            self._failures = 0
        except Exception:  # noqa: BLE001 - ログ機能の失敗でアプリを止めない
            logger.warning("利用ログテーブルの初期化に失敗しました。", exc_info=True)
            self._record_failure()

    def _insert(self, row: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self.ensure_table()
        if not self._ready:
            return
        try:
            errors = self._get_client().insert_rows_json(
                self.table_id, [row], timeout=_INSERT_TIMEOUT
            )
            if errors:
                logger.warning("利用ログの書き込みでエラー: %s", errors)
                self._record_failure()
        except Exception:  # noqa: BLE001 - チャットを止めないため必ず握る
            logger.warning("利用ログの書き込みに失敗しました。", exc_info=True)
            self._record_failure()

    # -- 記録（例外を外に出さない） ------------------------------------
    def log_gemini_call(
        self,
        *,
        user_email: str,
        session_id: str,
        turn_id: str,
        prompt_tokens: int,
        output_tokens: int,
        total_tokens: int,
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


def _now_iso() -> str:
    """挿入用の現在時刻（UTC, ISO 8601）。"""
    return datetime.now(timezone.utc).isoformat()

"""クエリ結果の DataFrame を、Gemini に渡すための小さな要約に落とす。

生データは Gemini には渡さない（トークン節約とデータ最小化のため）。
フルの結果は Streamlit 側で DataFrame として直接表示する。
"""

from __future__ import annotations

import datetime as _dt
import decimal
import math
from typing import Any

import numpy as np
import pandas as pd

_MAX_CELL_CHARS = 120
_MAX_STAT_COLUMNS = 30


def _jsonable(value: Any) -> Any:
    """Gemini の function response に載せられる JSON 互換値へ変換する。"""
    if value is None:
        return None
    # numpy スカラー（np.bool_ / np.int64 など）は Python の値に戻す
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (int, str)):
        return _truncate(value) if isinstance(value, str) else value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return str(value)
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    if value is pd.NaT:
        return None
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in list(value.items())[:20]}
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return _truncate(str(value))


def _truncate(text: str) -> str:
    if len(text) > _MAX_CELL_CHARS:
        return text[:_MAX_CELL_CHARS] + f"…(全{len(text)}文字)"
    return text


def summarize_dataframe(
    df: pd.DataFrame,
    *,
    sample_rows: int = 15,
    truncated: bool = False,
) -> dict[str, Any]:
    """DataFrame から行数・列情報・数値統計・サンプル行だけを抜き出す。"""
    summary: dict[str, Any] = {
        "row_count": int(len(df)),
        "column_count": int(df.shape[1]),
    }
    if truncated:
        summary["truncated"] = True
        summary["truncation_note"] = (
            "結果は表示上限で打ち切られています。全体像が必要な場合は集計クエリに変更してください。"
        )

    if df.empty:
        summary["columns"] = [
            {"name": str(col), "dtype": str(df[col].dtype)} for col in df.columns
        ]
        summary["sample_rows"] = []
        summary["note"] = "クエリは成功しましたが該当する行がありませんでした。"
        return summary

    columns: list[dict[str, Any]] = []
    for col in df.columns[:_MAX_STAT_COLUMNS]:
        series = df[col]
        info: dict[str, Any] = {
            "name": str(col),
            "dtype": str(series.dtype),
            "null_count": int(series.isna().sum()),
        }
        if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            numeric = pd.to_numeric(series, errors="coerce").dropna()
            if not numeric.empty:
                info["stats"] = {
                    "min": _jsonable(float(numeric.min())),
                    "max": _jsonable(float(numeric.max())),
                    "mean": _jsonable(float(numeric.mean())),
                    "sum": _jsonable(float(numeric.sum())),
                }
        elif pd.api.types.is_datetime64_any_dtype(series):
            valid = series.dropna()
            if not valid.empty:
                info["stats"] = {
                    "min": _jsonable(valid.min()),
                    "max": _jsonable(valid.max()),
                }
        else:
            nunique = int(series.nunique(dropna=True))
            info["distinct_count"] = nunique
            if nunique <= 10:
                info["distinct_values"] = [
                    _jsonable(v) for v in series.dropna().unique()[:10]
                ]
        columns.append(info)

    summary["columns"] = columns
    if df.shape[1] > _MAX_STAT_COLUMNS:
        summary["columns_omitted"] = int(df.shape[1] - _MAX_STAT_COLUMNS)

    head = df.head(sample_rows)
    summary["sample_rows"] = [
        {str(col): _jsonable(row[col]) for col in head.columns}
        for _, row in head.iterrows()
    ]
    summary["sample_row_count"] = int(len(head))

    return summary

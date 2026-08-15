"""クエリ結果からグラフの種類と軸を推定する。

**この推定はすべてサーバー側で完結する。** Gemini には一切問い合わせない。
グラフのためにデータやスキーマを Gemini 境界の向こうへ送らないための設計で、
`summarize` と同じ「Gemini に渡すものを絞る」方針の延長にある。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# 棒グラフはカテゴリが多すぎると読めないので上限を設ける
MAX_BAR_CATEGORIES = 60

KIND_LABELS = {
    "bar": "棒グラフ",
    "line": "折れ線",
    "area": "面グラフ",
    "scatter": "散布図",
}


@dataclass
class ChartSpec:
    kind: str
    x: str
    y: list[str]
    reason: str = ""
    candidates_x: list[str] = field(default_factory=list)
    candidates_y: list[str] = field(default_factory=list)


def _classify(df: pd.DataFrame) -> tuple[list[str], list[str], list[str]]:
    """列を (日時, 数値, カテゴリ) に分類する。"""
    datetimes: list[str] = []
    numerics: list[str] = []
    categoricals: list[str] = []

    for col in df.columns:
        series = df[col]
        if pd.api.types.is_datetime64_any_dtype(series):
            datetimes.append(str(col))
        elif pd.api.types.is_bool_dtype(series):
            categoricals.append(str(col))
        elif pd.api.types.is_numeric_dtype(series):
            numerics.append(str(col))
        else:
            # DATE / TIME は object 型で来ることがあるので中身で判定する
            valid = series.dropna()
            if not valid.empty and hasattr(valid.iloc[0], "isoformat"):
                datetimes.append(str(col))
            else:
                categoricals.append(str(col))

    return datetimes, numerics, categoricals


def suggest_chart(df: pd.DataFrame) -> ChartSpec | None:
    """DataFrame の列構成からグラフを推定する。描けなければ None。"""
    if df is None or df.empty or len(df) < 2 or df.shape[1] < 2:
        return None

    datetimes, numerics, categoricals = _classify(df)
    if not numerics:
        return None

    candidates_x = datetimes + categoricals + numerics

    # 時系列があれば折れ線
    if datetimes:
        return ChartSpec(
            kind="line",
            x=datetimes[0],
            y=numerics[:3],
            reason=f"日時列「{datetimes[0]}」があるため時系列として表示しています。",
            candidates_x=candidates_x,
            candidates_y=numerics,
        )

    # カテゴリ × 数値 は棒グラフ
    if categoricals and len(df) <= MAX_BAR_CATEGORIES:
        return ChartSpec(
            kind="bar",
            x=categoricals[0],
            y=numerics[:3],
            reason=f"カテゴリ列「{categoricals[0]}」と数値列の組み合わせです。",
            candidates_x=candidates_x,
            candidates_y=numerics,
        )

    # 数値同士は散布図
    if len(numerics) >= 2:
        return ChartSpec(
            kind="scatter",
            x=numerics[0],
            y=numerics[1:2],
            reason="数値列が複数あるため散布図として表示しています。",
            candidates_x=candidates_x,
            candidates_y=numerics,
        )

    if categoricals:
        return ChartSpec(
            kind="bar",
            x=categoricals[0],
            y=numerics[:1],
            reason=(
                f"カテゴリが {len(df)} 件あります。"
                f"棒グラフは {MAX_BAR_CATEGORIES} 件程度までが見やすいため、"
                "必要に応じて SQL 側で上位に絞り込んでください。"
            ),
            candidates_x=candidates_x,
            candidates_y=numerics,
        )

    return None

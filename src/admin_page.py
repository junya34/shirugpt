"""管理者専用の利用状況ページ。

アクセス制御は二段構え:

1. `app.py` が `st.navigation` に渡すページ一覧から管理者以外には外す（UX の便宜）
2. **この関数の冒頭で毎回** 閲覧者メールを allowlist と照合する（実際の防御）

Streamlit は rerun ごとにスクリプト全体を再実行するため、2 は必ず効く。
1 だけに頼ると URL 直打ちで到達される可能性があるので、主防御にはしない。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from .config import (
    BQ_PRICE_PER_TIB_USD,
    DEFAULT_MONTHLY_LIMIT_USD,
    GEMINI_INPUT_PRICE_PER_1M_USD,
    GEMINI_OUTPUT_PRICE_PER_1M_USD,
    GEMINI_PRICE_AS_OF,
    Settings,
    bq_cost_usd,
    gemini_cost_usd,
    human_bytes,
)
from .fx import usd_to_jpy
from .usage_log import UsageLogger

# 日付範囲の上限。ログテーブルが将来肥大化したときのスキャン量の保険。
_MAX_RANGE_DAYS = 400


@st.cache_data(ttl=60, show_spinner=False)
def _load_usage(_logger: UsageLogger, start: date, end: date) -> pd.DataFrame:
    """ログを取得する。`_logger` は先頭アンダースコアでハッシュ対象から除外。

    キャッシュキーは (start, end) のみ。rerun のたびに BigQuery へ
    問い合わせて課金が増えるのを防ぐ。
    """
    return _logger.query_usage(start, end)


def _rollup(df: pd.DataFrame, unit: str) -> pd.DataFrame:
    """日別のログを表示単位ごとに集約し、金額列を足す。"""
    out = df.copy()
    out["event_date"] = pd.to_datetime(out["event_date"])
    if unit == "月別":
        out["期間"] = out["event_date"].dt.strftime("%Y-%m")
    else:
        out["期間"] = out["event_date"].dt.strftime("%Y-%m-%d")

    grouped = (
        out.groupby(["期間", "user_email"], as_index=False)[
            [
                "prompt_tokens",
                "output_tokens",
                "total_tokens",
                "billed_bytes",
                "gemini_calls",
                "bq_queries",
            ]
        ]
        .sum()
        .sort_values(["期間", "user_email"], ascending=[False, True])
    )
    grouped["推定金額(USD)"] = [
        gemini_cost_usd(p, o) + bq_cost_usd(b)
        for p, o, b in zip(
            grouped["prompt_tokens"], grouped["output_tokens"], grouped["billed_bytes"]
        )
    ]
    return grouped


def _display_frame(grouped: pd.DataFrame) -> pd.DataFrame:
    """表示用に列名を日本語化し、バイト数を読める形にする。"""
    view = pd.DataFrame(
        {
            "期間": grouped["期間"],
            "利用者": grouped["user_email"],
            "入力トークン": grouped["prompt_tokens"],
            "出力トークン": grouped["output_tokens"],
            "合計トークン": grouped["total_tokens"],
            "推定金額(USD)": grouped["推定金額(USD)"].map(lambda v: f"${v:,.4f}"),
            "クエリ量": grouped["billed_bytes"].map(human_bytes),
            "Gemini呼び出し": grouped["gemini_calls"],
            "クエリ実行": grouped["bq_queries"],
        }
    )
    return view


def render_admin_page(settings: Settings, logger: UsageLogger, viewer_email: str) -> None:
    # --- アクセス制御（毎 rerun で必ず通る） ------------------------
    is_admin = settings.is_admin(viewer_email)
    dev_bypass = settings.admin_allow_local and not viewer_email
    if not is_admin and not dev_bypass:
        # ページの存在自体を匂わせない汎用的な文言にする
        st.error("お探しのページは見つかりませんでした。")
        st.stop()

    st.title("🛠️ 利用状況・使用制限")
    if dev_bypass:
        st.warning(
            "**開発モード: 認証なしで表示中**（ADMIN_ALLOW_LOCAL が有効）。"
            "この設定を Streamlit Cloud の Secrets に入れないでください。"
        )

    if not settings.logging_enabled:
        st.info(
            "利用ログが無効です。`BQ_LOG_DATASET` を設定すると記録が始まります"
            "（`BQ_ALLOWED_DATASETS` には含めないでください）。"
        )
        return

    tab_usage, tab_limits = st.tabs(["利用状況", "使用制限"])
    with tab_usage:
        _render_usage_tab(logger)
    with tab_limits:
        _render_limits_tab(logger)


def _render_usage_tab(logger: UsageLogger) -> None:
    # --- 期間の指定 -------------------------------------------------
    today = date.today()
    col1, col2 = st.columns(2)
    start = col1.date_input("開始日", value=today - timedelta(days=29), max_value=today)
    end = col2.date_input("終了日", value=today, max_value=today)

    if start > end:
        st.warning("開始日が終了日より後になっています。")
        return
    if (end - start).days > _MAX_RANGE_DAYS:
        st.warning(f"期間は最大 {_MAX_RANGE_DAYS} 日までにしてください。")
        return

    unit = st.radio("集計単位", ["日別", "月別"], horizontal=True)

    # --- 取得と表示 -------------------------------------------------
    try:
        df = _load_usage(logger, start, end)
    except Exception as exc:  # noqa: BLE001 - 管理者向けなので原因を見せる
        st.error("利用ログの取得に失敗しました。")
        st.code(str(exc))
        return

    if df.empty:
        st.info("この期間に記録された利用はありません。")
        return

    grouped = _rollup(df, unit)

    total_prompt = int(grouped["prompt_tokens"].sum())
    total_output = int(grouped["output_tokens"].sum())
    total_bytes = int(grouped["billed_bytes"].sum())
    total_cost_usd = gemini_cost_usd(total_prompt, total_output) + bq_cost_usd(total_bytes)
    m1, m2, m3 = st.columns(3)
    m1.metric("合計トークン", f"{int(grouped['total_tokens'].sum()):,}")
    m2.metric("推定金額", f"${total_cost_usd:,.4f}")
    m2.caption(f"≈ ¥{usd_to_jpy(total_cost_usd):,.1f}")
    m3.metric("クエリ量", human_bytes(total_bytes))

    st.dataframe(_display_frame(grouped), width="stretch", hide_index=True)

    st.caption(
        f"推定金額は Gemini トークン（{GEMINI_PRICE_AS_OF} 時点のレート、"
        rf"入力 \${GEMINI_INPUT_PRICE_PER_1M_USD}/100万トークン、"
        rf"出力 \${GEMINI_OUTPUT_PRICE_PER_1M_USD}/100万トークン）と "
        rf"BigQuery クエリ（\${BQ_PRICE_PER_TIB_USD}/TiB）の合計金額です。"
        f"実際の請求金額は Google Cloud Console の[課金]ページをご覧ください。"
    )


@st.cache_data(ttl=60, show_spinner=False)
def _load_limits(_logger: UsageLogger) -> pd.DataFrame:
    """既知の利用者一覧に、設定済みの月次制限額（無ければ既定値）を合わせる。"""
    known = _logger.list_known_users()
    limits = _logger.get_limits()
    merged = pd.DataFrame({"user_email": known}).merge(
        limits[["user_email", "weekly_limit_usd", "updated_at"]], on="user_email", how="left"
    )
    merged["weekly_limit_usd"] = merged["weekly_limit_usd"].fillna(DEFAULT_MONTHLY_LIMIT_USD)
    return merged.sort_values("user_email").reset_index(drop=True)


def _render_limits_tab(logger: UsageLogger) -> None:
    # st.success() の直後に st.rerun() すると、再実行後の描画には反映されず
    # メッセージが実質見えなくなる。session_state に積んで次の描画で出す。
    flash = st.session_state.pop("_limits_flash", None)
    if flash:
        st.success(flash)

    st.subheader("全員に一括設定")
    col1, col2 = st.columns([2, 1])
    bulk_value = col1.number_input(
        "月次上限 (USD)",
        min_value=0.0,
        value=DEFAULT_MONTHLY_LIMIT_USD,
        step=0.5,
        format="%.2f",
        key="bulk_limit_value",
    )
    col1.caption(f"≈ ¥{usd_to_jpy(bulk_value):,.0f}")
    if col2.button("全員に適用", type="primary"):
        users = logger.list_known_users()
        if not users:
            st.warning("既知の利用者がいません（まだ利用履歴がありません）。")
        else:
            try:
                logger.save_limits(
                    pd.DataFrame(
                        {"user_email": users, "weekly_limit_usd": [bulk_value] * len(users)}
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 管理者向けなので原因を見せる
                st.error(
                    "保存に失敗しました。他の管理者が同時に保存した可能性があります。"
                    "再読み込みして再度お試しください。"
                )
                st.code(str(exc))
            else:
                st.session_state["_limits_flash"] = (
                    f"{len(users)} 名に ${bulk_value:.2f} を適用しました。"
                )
                _load_limits.clear()
                st.rerun()

    st.divider()
    st.subheader("利用者ごとの設定")
    try:
        df = _load_limits(logger)
    except Exception as exc:  # noqa: BLE001 - 管理者向けなので原因を見せる
        st.error("利用者一覧の取得に失敗しました。")
        st.code(str(exc))
        return

    if df.empty:
        st.info("既知の利用者がいません（まだ利用履歴がありません）。")
        return

    edited = st.data_editor(
        df,
        column_config={
            "user_email": st.column_config.TextColumn("利用者", disabled=True),
            "weekly_limit_usd": st.column_config.NumberColumn(
                "月次上限 (USD)", min_value=0.0, step=0.1, format="$%.2f"
            ),
            "updated_at": st.column_config.DatetimeColumn("最終更新", disabled=True),
        },
        hide_index=True,
        width="stretch",
        num_rows="fixed",  # 手入力での行追加は不可。新規利用者は利用履歴の出現で自動的に一覧化される
        key="limits_editor",
    )
    if st.button("保存"):
        try:
            logger.save_limits(edited[["user_email", "weekly_limit_usd"]])
        except Exception as exc:  # noqa: BLE001 - 管理者向けなので原因を見せる
            st.error(
                "保存に失敗しました。他の管理者が同時に保存した可能性があります。"
                "再読み込みして再度お試しください。"
            )
            st.code(str(exc))
        else:
            st.session_state["_limits_flash"] = "保存しました。"
            _load_limits.clear()
            st.rerun()

    st.caption(
        f"制限が未設定の利用者には既定値 ${DEFAULT_MONTHLY_LIMIT_USD:.2f} が適用されます。"
        "月は1日0:00（JST）にリセットされます。"
    )

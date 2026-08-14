"""ShiruGPT — BigQuery を自然言語で分析する Streamlit アプリ（プロトタイプ）。

起動:
    streamlit run app.py
"""

from __future__ import annotations

from typing import Any

import streamlit as st
from google.genai import types

from src.agent import (
    AnswerResult,
    ConfirmationPending,
    GeminiAgent,
    ToolContext,
)
from src.bq_tools import BigQueryTools, QueryRun, friendly_error
from src.config import (
    ConfigError,
    Settings,
    credentials_leak_warning,
    human_bytes,
    load_settings,
)

st.set_page_config(page_title="ShiruGPT", page_icon="📊", layout="wide")

AUTH_HELP = """\
GCP の認証情報が見つからないか、権限が不足しています。ターミナルで次を実行してください。

```
gcloud auth application-default login
gcloud auth application-default set-quota-project <プロジェクトID>
```
"""


# --------------------------------------------------------------------
# 初期化
# --------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def build_runtime() -> tuple[Settings, BigQueryTools, GeminiAgent]:
    settings = load_settings()
    return settings, BigQueryTools(settings), GeminiAgent(settings)


def init_state(settings: Settings, tools: BigQueryTools) -> ToolContext:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "contents" not in st.session_state:
        st.session_state.contents = []
    if "pending" not in st.session_state:
        st.session_state.pending = None
    if "ctx" not in st.session_state:
        st.session_state.ctx = ToolContext(tools=tools, settings=settings)
    return st.session_state.ctx


def reset_conversation(settings: Settings, tools: BigQueryTools) -> None:
    st.session_state.messages = []
    st.session_state.contents = []
    st.session_state.pending = None
    st.session_state.ctx = ToolContext(tools=tools, settings=settings)


# --------------------------------------------------------------------
# 表示
# --------------------------------------------------------------------
def render_run(run: QueryRun, idx: int, msg_idx: int) -> None:
    rows = len(run.dataframe)
    label = f"実行した SQL — {rows:,} 行 / スキャン {human_bytes(run.estimated_bytes)}"
    with st.expander(label):
        st.code(run.sql, language="sql")
        for note in run.notes:
            st.caption(f"ℹ️ {note}")
        if run.billed_bytes:
            st.caption(f"課金対象バイト: {human_bytes(run.billed_bytes)}")

    if rows:
        st.dataframe(run.dataframe, width="stretch", height=min(400, 60 + rows * 35))
        st.download_button(
            "結果を CSV でダウンロード",
            data=run.dataframe.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"result_{msg_idx}_{idx}.csv",
            mime="text/csv",
            key=f"dl_{msg_idx}_{idx}",
        )
    else:
        st.info("このクエリの結果は 0 件でした。")


def render_message(message: dict[str, Any], msg_idx: int) -> None:
    with st.chat_message(message["role"]):
        if message.get("is_error"):
            st.warning(message["text"])
        else:
            st.markdown(message["text"])

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            with st.expander(f"実行したツール（{len(tool_calls)} 回）"):
                for call in tool_calls:
                    st.markdown(f"- `{call}`")

        for idx, run in enumerate(message.get("runs") or []):
            render_run(run, idx, msg_idx)


def render_sidebar(settings: Settings, ctx: ToolContext, tools: BigQueryTools) -> None:
    with st.sidebar:
        st.header("設定")
        auth_label = (
            "サービスアカウントキー"
            if settings.auth_mode == "service_account"
            else "ADC（ユーザー認証）"
        )
        st.markdown(
            f"""
- **プロジェクト**: `{settings.gcp_project}`
- **許可データセット**: {", ".join(f"`{d}`" for d in settings.allowed_datasets)}
- **モデル**: `{settings.gemini_model}` ({settings.vertex_location})
- **認証方式**: {auth_label}
"""
        )
        st.divider()
        st.subheader("コスト上限")
        st.markdown(
            f"""
- 確認を求める閾値: **{human_bytes(settings.dry_run_confirm_bytes)}**
- 課金上限: **{human_bytes(settings.max_bytes_billed)}**
- 自動 LIMIT: **{settings.default_row_limit:,} 行**
"""
        )
        st.divider()
        st.subheader("キャッシュ状況")
        st.markdown(
            f"""
- 取得済みスキーマ: **{len(ctx.schema_cache)}** テーブル
- 実行済みクエリ: **{len(ctx.query_cache)}** 件
"""
        )
        st.caption("同じスキーマ・同じ SQL は再取得せず、トークンと課金を節約します。")
        st.divider()
        if st.button("会話をリセット", width="stretch"):
            reset_conversation(settings, tools)
            st.rerun()


# --------------------------------------------------------------------
# ターン処理
# --------------------------------------------------------------------
def process_turn(agent: GeminiAgent, ctx: ToolContext) -> None:
    try:
        result = agent.run(st.session_state.contents, ctx)
    except Exception as exc:  # noqa: BLE001 - ユーザーには整形して見せる
        st.session_state.pending = None
        st.session_state.messages.append(
            {
                "role": "assistant",
                "text": friendly_error(exc),
                "runs": list(ctx.turn_runs),
                "tool_calls": list(ctx.turn_tool_calls),
                "is_error": True,
            }
        )
        st.rerun()
        return

    if isinstance(result, ConfirmationPending):
        st.session_state.pending = result
    else:
        assert isinstance(result, AnswerResult)
        st.session_state.pending = None
        st.session_state.messages.append(
            {
                "role": "assistant",
                "text": result.text,
                "runs": list(ctx.turn_runs),
                "tool_calls": list(ctx.turn_tool_calls),
            }
        )
    st.rerun()


def render_confirmation(
    pending: ConfirmationPending, agent: GeminiAgent, ctx: ToolContext
) -> None:
    with st.chat_message("assistant"):
        st.warning(
            f"このクエリは推定 **{human_bytes(pending.estimated_bytes)}** をスキャンします"
            f"（確認閾値 {human_bytes(pending.threshold_bytes)}）。実行してよろしいですか？"
        )
        st.code(pending.sql, language="sql")
        for note in pending.notes:
            st.caption(f"ℹ️ {note}")

        left, right, _ = st.columns([1, 1, 3])
        approve = left.button("実行する", type="primary", key="approve")
        deny = right.button("実行しない", key="deny")

    if approve or deny:
        ctx.decisions[pending.key] = "approved" if approve else "denied"
        st.session_state.pending = None
        with st.spinner("処理中…"):
            process_turn(agent, ctx)


# --------------------------------------------------------------------
# メイン
# --------------------------------------------------------------------
def main() -> None:
    st.title("📊 ShiruGPT")
    st.caption("BigQuery のデータを日本語で質問できる分析アシスタント（プロトタイプ）")

    try:
        settings, tools, agent = build_runtime()
    except ConfigError as exc:
        st.error(str(exc))
        st.stop()
        return
    except Exception as exc:  # noqa: BLE001
        st.error(friendly_error(exc))
        st.markdown(AUTH_HELP)
        st.stop()
        return

    leak = credentials_leak_warning(settings)
    if leak:
        st.error(f"⚠️ {leak}")

    ctx = init_state(settings, tools)
    render_sidebar(settings, ctx, tools)

    for msg_idx, message in enumerate(st.session_state.messages):
        render_message(message, msg_idx)

    pending = st.session_state.pending
    if pending is not None:
        render_confirmation(pending, agent, ctx)

    prompt = st.chat_input(
        "データについて質問してください（例: 来店データの件数を教えて）",
        disabled=pending is not None,
    )
    if not prompt:
        if not st.session_state.messages and pending is None:
            st.info(
                "まずは「どんなテーブルがありますか？」と聞いてみてください。"
                "許可されたデータセット内のテーブルを調べて回答します。"
            )
        return

    st.session_state.messages.append({"role": "user", "text": prompt})
    st.session_state.contents.append(
        types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
    )
    ctx.start_turn()

    with st.chat_message("user"):
        st.markdown(prompt)
    with st.spinner("BigQuery を調べています…"):
        process_turn(agent, ctx)


main()

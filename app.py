"""ShiruGPT — BigQuery を自然言語で分析する Streamlit アプリ（プロトタイプ）。

起動:
    streamlit run app.py
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import streamlit as st
from google.genai import types

from src.admin_page import render_admin_page
from src.agent import (
    AnswerResult,
    ConfirmationPending,
    GeminiAgent,
    ToolContext,
)
from src.bq_tools import BigQueryTools, QueryRun, friendly_error
from src.charts import KIND_LABELS, ChartSpec, suggest_chart
from src.config import (
    GEMINI_INPUT_PRICE_PER_1M_USD,
    GEMINI_OUTPUT_PRICE_PER_1M_USD,
    GEMINI_PRICE_AS_OF,
    ConfigError,
    Settings,
    credentials_leak_warning,
    gemini_cost_usd,
    human_bytes,
    load_settings,
)
from src.usage_log import UsageLogger

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
_SECRET_ENV_KEYS = (
    "GCP_PROJECT_ID",
    "BQ_LOCATION",
    "BQ_DEFAULT_DATASET",
    "BQ_ALLOWED_DATASETS",
    "VERTEX_LOCATION",
    "GEMINI_MODEL",
    "GEMINI_THINKING_BUDGET",
    "DRY_RUN_CONFIRM_BYTES",
    "MAX_BYTES_BILLED",
    "DEFAULT_ROW_LIMIT",
    "MAX_RESULT_ROWS",
    "SAMPLE_ROWS",
    "MAX_TOOL_ITERATIONS",
    "BQ_LOG_DATASET",
    "BQ_LOG_TABLE",
    "ADMIN_EMAILS",
    # ADMIN_ALLOW_LOCAL は .env 専用（認証を迂回するフラグなので
    # Streamlit Cloud の Secrets からは意図的に読まない）
)


def resolve_viewer_email() -> str:
    """ログイン中の閲覧者のメールアドレス。取得できなければ空文字。

    Streamlit Community Cloud の閲覧者制限が有効なとき、ログイン中の
    メールアドレスがここに入る。ローカル実行では取得できない。
    `st.user`（新 API）と `st.experimental_user`（旧 API）の両方に対応する。
    """
    for attr in ("user", "experimental_user"):
        obj = getattr(st, attr, None)
        if obj is None:
            continue
        try:
            email = getattr(obj, "email", None)
        except Exception:  # noqa: BLE001 - 未ログイン時に例外を出す実装に備える
            email = None
        if email:
            return str(email)
    return ""


def _bootstrap_secrets() -> None:
    """Streamlit Cloud の Secrets を環境変数へ写す。

    ローカルでは secrets.toml が存在しないため st.secrets へのアクセスが
    例外になる。その場合は何もせず .env 由来の設定に委ねる。
    サービスアカウントキーは中身を検査せずそのまま一時ファイルへ書き出し、
    パスだけを GOOGLE_APPLICATION_CREDENTIALS に渡す（config.py の
    「パスしか扱わない」設計はそのまま維持する）。
    """
    try:
        secrets = dict(st.secrets)
    except Exception:
        return

    for key in _SECRET_ENV_KEYS:
        if key in secrets and not os.getenv(key):
            os.environ[key] = str(secrets[key])

    if "gcp_service_account" in secrets and not os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS"
    ):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(dict(secrets["gcp_service_account"]), f)
            key_path = f.name
        os.chmod(key_path, 0o600)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_path


@st.cache_resource(show_spinner=False)
def build_runtime() -> tuple[Settings, BigQueryTools, GeminiAgent, UsageLogger]:
    _bootstrap_secrets()
    settings = load_settings()
    usage_logger = UsageLogger(settings)
    # ベストエフォート。失敗しても例外は投げず、起動を止めない。
    usage_logger.ensure_table()
    return settings, BigQueryTools(settings), GeminiAgent(settings), usage_logger


def _new_context(
    settings: Settings,
    tools: BigQueryTools,
    usage_logger: UsageLogger,
    user_email: str,
) -> ToolContext:
    return ToolContext(
        tools=tools,
        settings=settings,
        usage_logger=usage_logger,
        user_email=user_email,
    )


def init_state(
    settings: Settings,
    tools: BigQueryTools,
    usage_logger: UsageLogger,
    user_email: str,
) -> ToolContext:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "contents" not in st.session_state:
        st.session_state.contents = []
    if "pending" not in st.session_state:
        st.session_state.pending = None
    if "ctx" not in st.session_state:
        st.session_state.ctx = _new_context(
            settings, tools, usage_logger, user_email
        )
    return st.session_state.ctx


def reset_conversation(
    settings: Settings,
    tools: BigQueryTools,
    usage_logger: UsageLogger,
    user_email: str,
) -> None:
    st.session_state.messages = []
    st.session_state.contents = []
    st.session_state.pending = None
    st.session_state.ctx = _new_context(settings, tools, usage_logger, user_email)


# --------------------------------------------------------------------
# 表示
# --------------------------------------------------------------------
CHART_FUNCS = {
    "bar": st.bar_chart,
    "line": st.line_chart,
    "area": st.area_chart,
    "scatter": st.scatter_chart,
}


def render_table(run: QueryRun, idx: int, msg_idx: int) -> None:
    rows = len(run.dataframe)
    st.dataframe(run.dataframe, width="stretch", height=min(400, 60 + rows * 35))
    st.download_button(
        "結果を CSV でダウンロード",
        data=run.dataframe.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"result_{msg_idx}_{idx}.csv",
        mime="text/csv",
        key=f"dl_{msg_idx}_{idx}",
    )


def render_chart(df, spec: ChartSpec, key_prefix: str) -> None:
    kinds = list(KIND_LABELS)
    col1, col2, col3 = st.columns([2, 3, 4])
    kind = col1.selectbox(
        "種類",
        options=kinds,
        index=kinds.index(spec.kind),
        format_func=lambda k: KIND_LABELS[k],
        key=f"{key_prefix}_kind",
    )
    x = col2.selectbox(
        "X 軸",
        options=spec.candidates_x,
        index=spec.candidates_x.index(spec.x) if spec.x in spec.candidates_x else 0,
        key=f"{key_prefix}_x",
    )
    y_options = [c for c in spec.candidates_y if c != x]
    if not y_options:
        st.info("Y 軸に使える数値列がありません。X 軸を変更してください。")
        return
    y = col3.multiselect(
        "Y 軸（数値列）",
        options=y_options,
        default=[c for c in spec.y if c in y_options] or y_options[:1],
        key=f"{key_prefix}_y",
    )
    if not y:
        st.info("Y 軸の列を 1 つ以上選んでください。")
        return

    kwargs = {"x": x, "y": y}
    if kind == "bar":
        # SQL 側の ORDER BY を尊重する（既定では X 軸で並べ替えられてしまう）
        kwargs["sort"] = False
    try:
        CHART_FUNCS[kind](df, **kwargs)
    except Exception:  # noqa: BLE001 - 描画不能な組み合わせは表にフォールバック
        st.warning(
            "この列の組み合わせではグラフを描画できませんでした。"
            "軸の指定を変えるか「表」タブをご覧ください。"
        )
        return
    if spec.reason:
        st.caption(spec.reason)


def render_run(run: QueryRun, idx: int, msg_idx: int) -> None:
    rows = len(run.dataframe)
    label = f"実行した SQL — {run.purpose}" if run.purpose else "実行した SQL"
    with st.expander(label):
        st.code(run.sql, language="sql")
        for note in run.notes:
            st.caption(f"ℹ️ {note}")
        if run.billed_bytes:
            st.caption(
                f"実行クエリ量: {human_bytes(run.billed_bytes)}\n"
                f"結果件数: {rows:,} 件"
            )

    if not rows:
        st.info("このクエリの結果は 0 件でした。")
        return

    spec = suggest_chart(run.dataframe)
    if spec is None:
        render_table(run, idx, msg_idx)
        return

    tab_chart, tab_table = st.tabs(["グラフ", "表"])
    with tab_chart:
        render_chart(run.dataframe, spec, f"chart_{msg_idx}_{idx}")
    with tab_table:
        render_table(run, idx, msg_idx)


def render_message(message: dict[str, Any], msg_idx: int) -> None:
    with st.chat_message(message["role"]):
        if message.get("is_error"):
            st.warning(message["text"])
        else:
            st.markdown(message["text"])

        errors = message.get("errors") or []
        if errors:
            with st.expander(f"診断情報（{len(errors)} 件）"):
                for err in errors:
                    st.code(err)

        for idx, run in enumerate(message.get("runs") or []):
            render_run(run, idx, msg_idx)


def render_sidebar(
    settings: Settings,
    ctx: ToolContext,
    tools: BigQueryTools,
    usage_logger: UsageLogger,
    user_email: str,
) -> None:
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
        st.subheader("セッション使用量")
        cost_usd = gemini_cost_usd(
            ctx.session_prompt_tokens, ctx.session_output_tokens
        )
        st.markdown(
            f"""
- Gemini トークン: **{ctx.session_total_tokens:,}**
  （入力 {ctx.session_prompt_tokens:,} / 出力 {ctx.session_output_tokens:,}）
  ≈ **${cost_usd:.4f}**
- 実行クエリ量合計: **{human_bytes(ctx.session_billed_bytes)}**
"""
        )
        st.caption(
            f"会話をリセットするまでの累計です。Gemini の金額は "
            f"{GEMINI_PRICE_AS_OF} 時点のレート"
            rf"（入力 \${GEMINI_INPUT_PRICE_PER_1M_USD}/100万トークン、"
            rf"出力 \${GEMINI_OUTPUT_PRICE_PER_1M_USD}/100万トークン）による概算です。"
        )
        st.divider()
        if st.button("会話をリセット", width="stretch"):
            reset_conversation(settings, tools, usage_logger, user_email)
            st.rerun()

        # TODO: 管理者ページが表示されない問題の一時診断用。原因判明後に削除する。
        st.divider()
        st.caption(
            f"[診断] 検出した閲覧者メール: `{user_email or '(取得できず)'}` / "
            f"管理者リストの件数: {len(settings.admin_emails)} / "
            f"管理者判定: {settings.is_admin(user_email)}"
        )


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
                "errors": list(ctx.turn_errors),
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
                "errors": list(ctx.turn_errors),
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
# ページ
# --------------------------------------------------------------------
def render_chat_page(
    settings: Settings,
    tools: BigQueryTools,
    agent: GeminiAgent,
    usage_logger: UsageLogger,
    user_email: str,
) -> None:
    st.title("📊 ShiruGPT")
    st.caption("SHIRUCAFEデータの可視化・分析AI")

    leak = credentials_leak_warning(settings)
    if leak:
        st.error(f"⚠️ {leak}")

    ctx = init_state(settings, tools, usage_logger, user_email)
    render_sidebar(settings, ctx, tools, usage_logger, user_email)

    for msg_idx, message in enumerate(st.session_state.messages):
        render_message(message, msg_idx)

    pending = st.session_state.pending
    if pending is not None:
        render_confirmation(pending, agent, ctx)

    prompt = st.chat_input(
        "質問してください（例: 〇〇店の参加率の推移をグラフで見せて）",
        disabled=pending is not None,
    )
    if not prompt:
        if not st.session_state.messages and pending is None:
            st.info(
                "プロトタイプ版です。AIは必ずしも正しい回答を返すとは限りません。"
            )
        return

    st.session_state.messages.append({"role": "user", "text": prompt})
    st.session_state.contents.append(
        types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
    )
    ctx.start_turn()

    with st.chat_message("user"):
        st.markdown(prompt)
    with st.spinner("データベースを調べています…"):
        process_turn(agent, ctx)


# --------------------------------------------------------------------
# メイン
# --------------------------------------------------------------------
def main() -> None:
    try:
        settings, tools, agent, usage_logger = build_runtime()
    except ConfigError as exc:
        st.error(str(exc))
        st.stop()
        return
    except Exception as exc:  # noqa: BLE001
        st.error(friendly_error(exc))
        st.markdown(AUTH_HELP)
        st.stop()
        return

    user_email = resolve_viewer_email()

    pages = [
        st.Page(
            lambda: render_chat_page(
                settings, tools, agent, usage_logger, user_email
            ),
            title="チャット",
            icon="💬",
            default=True,
        )
    ]
    # 管理者以外にはページ自体を渡さない（ナビゲーションに出さないための措置）。
    # 実際のアクセス制御は render_admin_page の冒頭で毎回行う。
    if settings.is_admin(user_email) or (
        settings.admin_allow_local and not user_email
    ):
        pages.append(
            st.Page(
                lambda: render_admin_page(settings, usage_logger, user_email),
                title="利用状況",
                icon="🛠️",
                url_path="usage",
            )
        )

    if len(pages) > 1:
        st.navigation(pages).run()
    else:
        # ページが 1 つだけならナビゲーションを出さず、従来どおりの見た目にする
        render_chat_page(settings, tools, agent, usage_logger, user_email)


main()

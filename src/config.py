"""環境変数（.env）からアプリ設定を読み込む。

認証情報の中身はここでは一切扱わない。扱うのは方式の選択と、
サービスアカウントキーを使う場合の「ファイルパス」だけ。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")


class ConfigError(RuntimeError):
    """.env の設定が不足・不正なときに送出する。"""


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"環境変数 {name} は整数で指定してください（現在: {raw!r}）") from exc


def _opt_int(name: str) -> int | None:
    raw = _str(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"環境変数 {name} は整数で指定してください（現在: {raw!r}）") from exc


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = _str(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    gcp_project: str
    bq_location: str | None
    default_dataset: str
    allowed_datasets: tuple[str, ...]

    credentials_path: str | None

    vertex_location: str
    gemini_model: str
    thinking_budget: int | None

    dry_run_confirm_bytes: int
    max_bytes_billed: int
    default_row_limit: int
    max_result_rows: int
    sample_rows: int
    max_tool_iterations: int

    # 利用ログ（Gemini 境界の外。allowed_datasets には絶対に含めない）
    log_dataset: str
    log_table: str
    admin_emails: tuple[str, ...]
    admin_allow_local: bool

    def is_dataset_allowed(self, dataset: str) -> bool:
        return dataset in self.allowed_datasets

    def is_admin(self, email: str) -> bool:
        if not email:
            return False
        return email.strip().lower() in {e.lower() for e in self.admin_emails}

    @property
    def logging_enabled(self) -> bool:
        return bool(self.log_dataset and self.log_table)

    @property
    def auth_mode(self) -> str:
        return "service_account" if self.credentials_path else "adc"


def _resolve_credentials() -> str | None:
    """サービスアカウントキーのパスを解決する。鍵の中身には触れない。

    パスが指定されていれば絶対パス化して GOOGLE_APPLICATION_CREDENTIALS に
    設定し直す（Streamlit の実行ディレクトリに依存しないようにするため）。
    指定が無ければ None を返し、ADC にフォールバックする。
    """
    raw = _str("GOOGLE_APPLICATION_CREDENTIALS")
    if not raw:
        return None

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    path = path.resolve()

    if not path.exists():
        raise ConfigError(
            f"GOOGLE_APPLICATION_CREDENTIALS に指定されたファイルが存在しません: {path}\n"
            "パスを確認するか、値を空にして ADC "
            "(gcloud auth application-default login) を使用してください。"
        )
    if not path.is_file():
        raise ConfigError(
            f"GOOGLE_APPLICATION_CREDENTIALS はファイルを指す必要があります: {path}"
        )

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
    return str(path)


def load_settings() -> Settings:
    project = _str("GCP_PROJECT_ID")
    if not project:
        raise ConfigError(
            "GCP_PROJECT_ID が未設定です。.env.example を .env にコピーして値を設定してください。"
        )

    default_dataset = _str("BQ_DEFAULT_DATASET")
    allowed = _csv("BQ_ALLOWED_DATASETS", default_dataset)
    if not allowed:
        raise ConfigError(
            "BQ_ALLOWED_DATASETS が空です。アクセスを許可するデータセットを1つ以上指定してください。"
        )
    # 既定データセットは常に allowlist に含める
    if default_dataset and default_dataset not in allowed:
        allowed = (default_dataset, *allowed)

    # 利用ログのデータセットは Gemini 境界の外に置く。allowlist に混入すると
    # list_datasets で名前が見え、sql_guard も参照を通してしまい、
    # 他利用者の使用量が自然言語質問経由で漏れる。設定ミスを起動時に落とす。
    log_dataset = _str("BQ_LOG_DATASET")
    if log_dataset and log_dataset in allowed:
        raise ConfigError(
            f"BQ_LOG_DATASET「{log_dataset}」を BQ_ALLOWED_DATASETS に含めないでください。"
            "利用ログが Gemini からアクセス可能になります。"
        )

    return Settings(
        gcp_project=project,
        bq_location=_str("BQ_LOCATION") or None,
        default_dataset=default_dataset,
        allowed_datasets=allowed,
        credentials_path=_resolve_credentials(),
        vertex_location=_str("VERTEX_LOCATION", "us-central1"),
        gemini_model=_str("GEMINI_MODEL", "gemini-2.5-flash"),
        thinking_budget=_opt_int("GEMINI_THINKING_BUDGET"),
        dry_run_confirm_bytes=_int("DRY_RUN_CONFIRM_BYTES", 1024**3),
        max_bytes_billed=_int("MAX_BYTES_BILLED", 10 * 1024**3),
        default_row_limit=_int("DEFAULT_ROW_LIMIT", 1000),
        max_result_rows=_int("MAX_RESULT_ROWS", 5000),
        sample_rows=_int("SAMPLE_ROWS", 15),
        max_tool_iterations=_int("MAX_TOOL_ITERATIONS", 12),
        log_dataset=log_dataset,
        log_table=_str("BQ_LOG_TABLE", "usage_events"),
        admin_emails=_csv("ADMIN_EMAILS"),
        admin_allow_local=_bool("ADMIN_ALLOW_LOCAL"),
    )


def credentials_leak_warning(settings: Settings) -> str | None:
    """鍵ファイルがリポジトリ内にあり git 管理外になっていない場合に警告文を返す。

    公開リポジトリへの誤コミットを防ぐための最後の砦。
    """
    if not settings.credentials_path:
        return None

    path = Path(settings.credentials_path)
    try:
        relative = path.relative_to(REPO_ROOT)
    except ValueError:
        return None  # リポジトリ外に置かれているので誤コミットの心配はない

    import subprocess

    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(relative)],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    # 0 = 無視される（安全）, 1 = 無視されない, 128 = git リポジトリでない等
    if result.returncode == 1:
        return (
            f"鍵ファイル `{relative}` がリポジトリ内にあり、.gitignore で除外されていません。"
            "このままではコミットに含まれる恐れがあります。"
        )
    return None


# Gemini のトークン単価（USD / 1M トークン）。レート改定時はここを更新する。
# サイドバーと管理者ページの両方がこれを参照する（定数を二重に持たない）。
GEMINI_PRICE_AS_OF = "2026-08-19"
GEMINI_INPUT_PRICE_PER_1M_USD = 0.30
GEMINI_OUTPUT_PRICE_PER_1M_USD = 2.50


def gemini_cost_usd(prompt_tokens: int, output_tokens: int) -> float:
    """トークン数から概算金額（USD）を求める。output には thinking 分も含める。"""
    return (
        prompt_tokens / 1_000_000 * GEMINI_INPUT_PRICE_PER_1M_USD
        + output_tokens / 1_000_000 * GEMINI_OUTPUT_PRICE_PER_1M_USD
    )


def human_bytes(num: float) -> str:
    """バイト数を人間が読める形式にする。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{num:.0f} {unit}"
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} TB"

"""USD → JPY の換算レート取得（表示用の参考値）。

Frankfurter API（ECB参考レート、APIキー不要）を呼ぶ。外部APIなので
失敗を前提に設計する:

- 直前に取得できたレートがあればプロセス内で使い回す（TTLで再取得）
- 一度も取得できていなければ固定のフォールバックレートを使う
- 例外は外に漏らさない。この値は表示用の参考情報でしかなく、
  取得できないことがチャット機能に影響してはいけない
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

_FRANKFURTER_URL = "https://api.frankfurter.app/latest"
_TIMEOUT_SECONDS = 3.0
_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6時間ごとに再取得を試みる

# 一度も取得できなかった場合のフォールバック。レート改定時はここを更新する。
FALLBACK_USD_JPY_RATE = 150.0

_cached_rate: float | None = None
_cached_at: float = 0.0


def usd_to_jpy_rate() -> float:
    """USD→JPY の換算レート。取得できなければ直前の値、無ければ固定値。"""
    global _cached_rate, _cached_at

    now = time.monotonic()
    if _cached_rate is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached_rate

    try:
        response = requests.get(
            _FRANKFURTER_URL,
            params={"from": "USD", "to": "JPY"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        rate = float(response.json()["rates"]["JPY"])
    except Exception:  # noqa: BLE001 - 表示用の参考値なので失敗してもアプリを止めない
        logger.warning("USD→JPY 換算レートの取得に失敗しました。", exc_info=True)
        return _cached_rate if _cached_rate is not None else FALLBACK_USD_JPY_RATE

    _cached_rate = rate
    _cached_at = now
    return rate


def usd_to_jpy(amount_usd: float) -> float:
    """USD の金額を JPY に換算する（表示用の概算）。"""
    return amount_usd * usd_to_jpy_rate()

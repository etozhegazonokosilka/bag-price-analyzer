"""
обертка над ZenRows

важно:
- api key не должен попадать в логи (не логируем query string)
- используем только requests, без локальных прокси: весь обход антибота делает ZenRows"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, Tuple

import requests

from utils.domain import domain_of
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name, "").strip()
        return float(raw) if raw else float(default)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name, "").strip()
        return int(raw) if raw else int(default)
    except Exception:
        return int(default)

_ZENROWS_ENDPOINT = os.getenv(
    "ZENROWS_ENDPOINT",
    "https://api.zenrows.com/v1/",
).strip() or "https://api.zenrows.com/v1/"
_ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY", "").strip()
_ZENROWS_TIMEOUT_SEC = _env_float("ZENROWS_TIMEOUT_SEC", 60.0)
_ZENROWS_MODE = os.getenv("ZENROWS_MODE", "auto").strip().lower()
_ZENROWS_PROXY_COUNTRY = os.getenv("ZENROWS_PROXY_COUNTRY", "").strip().lower()
_ZENROWS_CUSTOM_HEADERS = _env_flag("ZENROWS_CUSTOM_HEADERS", "1")
_ZENROWS_ORIGINAL_STATUS = _env_flag("ZENROWS_ORIGINAL_STATUS", "0")
_ZENROWS_SESSION_ENABLED = _env_flag("ZENROWS_SESSION_ENABLED", "0")
_ZENROWS_MAX_ATTEMPTS = max(1, _env_int("ZENROWS_MAX_ATTEMPTS", 1))
_ZENROWS_RETRY_WAIT_SEC = max(0.0, _env_float("ZENROWS_RETRY_WAIT_SEC", 1.5))
_ZENROWS_JOLICLOSET_MAX_ATTEMPTS = max(1, _env_int("ZENROWS_JOLICLOSET_MAX_ATTEMPTS", 2))
_ZENROWS_JOLICLOSET_TIMEOUT_SEC = max(10.0, _env_float("ZENROWS_JOLICLOSET_TIMEOUT_SEC", 40.0))

# домены, для которых используем zenrows. если переменная пустая, по умолчанию включаем trr+tlc (если есть ключ)
_ZENROWS_DOMAINS_ENV = os.getenv("ZENROWS_DOMAINS", "").strip()
if _ZENROWS_DOMAINS_ENV:
    _ZENROWS_DOMAINS = {x.strip().lower() for x in _ZENROWS_DOMAINS_ENV.split(",") if x and x.strip()}
else:
    _ZENROWS_DOMAINS = {"therealreal.com", "theluxurycloset.com"} if _ZENROWS_API_KEY else set()


def zenrows_enabled_for_url(url: str) -> bool:
    """возвращает True, если для url нужно использовать ZenRows"""
    if not _ZENROWS_API_KEY:
        return False
    d = domain_of(url)
    if not d:
        return False
    for item in _ZENROWS_DOMAINS:
        if d == item or d.endswith(f".{item}"):
            return True
    return False


def _make_session_id(url: str) -> int:
    # session_id у zenrows должен быть int 1..99999; делаем детерминированный, чтобы ретраи держали один айпи
    h = abs(hash(url)) % 99999
    return int(h) + 1


def _zenrows_policy_for_url(target_url: str) -> Tuple[int, float]:
    """
    возвращает политику попыток и таймаута под конкретный домен
    для jolicloset даём более "длинный" профиль, т.к. антибот там жёстче
"""
    attempts = max(1, int(_ZENROWS_MAX_ATTEMPTS))
    read_timeout_sec = max(10.0, float(_ZENROWS_TIMEOUT_SEC))

    d = (domain_of(target_url) or "").lower()
    if d.endswith("jolicloset.com"):
        attempts = max(attempts, int(_ZENROWS_JOLICLOSET_MAX_ATTEMPTS))
        read_timeout_sec = max(read_timeout_sec, float(_ZENROWS_JOLICLOSET_TIMEOUT_SEC))

    return attempts, read_timeout_sec


def fetch_url_via_zenrows(
    target_url: str,
    *,
    wait_for: str | None = None,
    wait_ms: int | None = None,
) -> Tuple[int, str, str, Optional[str], Dict[str, Any]]:
    """забирает HTML через ZenRows

    возвращает:
    (status_code, html, final_url, error, meta)
"""
    if not _ZENROWS_API_KEY:
        return 0, "", target_url, "ZENROWS_API_KEY is missing", {}

    params: Dict[str, Any] = {
        "url": target_url,
        "apikey": _ZENROWS_API_KEY,
    }

    # auto режим: zenrows сам подбирает конфигурацию; js_render/premium_proxy не задаем, чтобы не конфликтовать
    if _ZENROWS_MODE == "auto":
        params["mode"] = "auto"

    if _ZENROWS_PROXY_COUNTRY:
        params["proxy_country"] = _ZENROWS_PROXY_COUNTRY

    if _ZENROWS_ORIGINAL_STATUS:
        params["original_status"] = "true"

    if _ZENROWS_SESSION_ENABLED:
        params["session_id"] = str(_make_session_id(target_url))

    # эти параметры можно использовать и в auto (zenrows сам включит js render, если нужно)
    if wait_for:
        params["wait_for"] = wait_for
    if wait_ms is not None:
        try:
            params["wait"] = str(int(wait_ms))
        except Exception:
            pass

    headers: Dict[str, str] = {"User-Agent": "Mozilla/5.0"}
    if _ZENROWS_CUSTOM_HEADERS:
        # используем базовые заголовки "как браузер"; zenrows применит их к целевому запросу
        params["custom_headers"] = "true"
        headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

    target_domain = domain_of(target_url)
    max_attempts, read_timeout_sec = _zenrows_policy_for_url(target_url)
    for attempt in range(max_attempts):
        session = requests.Session()
        session.trust_env = False
        started = time.time()
        try:
            resp = session.get(
                _ZENROWS_ENDPOINT,
                params=params,
                headers=headers,
                timeout=(5.0, read_timeout_sec),
            )

            meta = {
                "zenrows_cost": resp.headers.get("X-Request-Cost"),
                "zenrows_concurrency_limit": resp.headers.get("Concurrency-Limit"),
                "zenrows_concurrency_remaining": resp.headers.get("Concurrency-Remaining"),
                "zenrows_request_id": resp.headers.get("X-Request-Id") or resp.headers.get("X-Request-ID"),
                "elapsed_sec": round(time.time() - started, 2),
                "attempt": f"{attempt + 1}/{max_attempts}",
            }

            # не логируем endpoint/params, чтобы не утек apikey
            log_event(
                log,
                "zenrows.fetch",
                level="info",
                target_domain=target_domain,
                status_code=getattr(resp, "status_code", None),
                attempt=f"{attempt + 1}/{max_attempts}",
                cost=meta.get("zenrows_cost"),
                elapsed_sec=meta.get("elapsed_sec"),
            )

            return int(resp.status_code), resp.text or "", target_url, None, meta

        except Exception as e:
            if attempt < (max_attempts - 1):
                log_exception(
                    log,
                    "zenrows.retry",
                    e,
                    level="warning",
                    target_domain=target_domain,
                    attempt=f"{attempt + 1}/{max_attempts}",
                    wait_sec=_ZENROWS_RETRY_WAIT_SEC,
                    timeout_sec=read_timeout_sec,
                )
                if _ZENROWS_RETRY_WAIT_SEC > 0:
                    time.sleep(_ZENROWS_RETRY_WAIT_SEC)
                continue

            log_exception(
                log,
                "zenrows.error",
                e,
                level="warning",
                target_domain=target_domain,
                attempt=f"{attempt + 1}/{max_attempts}",
                timeout_sec=read_timeout_sec,
            )
            return 0, "", target_url, str(e), {
                "elapsed_sec": round(time.time() - started, 2),
                "attempt": f"{attempt + 1}/{max_attempts}",
            }
        finally:
            session.close()

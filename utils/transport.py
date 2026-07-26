"""
http-транспорт с опциональным резервным переходом на curl_cffi

возвращает единый кортеж из 4 элементов:
(status_code, text, final_url, error)"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

import requests

from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)

# кэш для проверок ip прокси (опционально)
_PROXY_IP_CACHE: dict[str, str] = {}


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}


def _should_use_curl_cffi() -> bool:
    return _env_flag("USE_CURL_CFFI")


def _parse_timeout(timeout: Any) -> Tuple[float, float]:
    """нормализует timeout к float-значениям (connect, read)"""
    if isinstance(timeout, (int, float)):
        return (5.0, float(timeout))
    if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
        try:
            return (float(timeout[0]), float(timeout[1]))
        except Exception:
            return (5.0, 30.0)
    return (5.0, 30.0)


def check_proxy_ip(proxy_url: str) -> Optional[str]:
    """опциональная диагностика прокси через ifconfig.me

    по умолчанию отключена (включается через PROXY_IP_CHECK=1)
"""
    if not proxy_url:
        return None
    if not _env_flag("PROXY_IP_CHECK"):
        return None

    if proxy_url in _PROXY_IP_CACHE:
        return _PROXY_IP_CACHE[proxy_url]

    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        resp = requests.get(
            "https://ifconfig.me/ip",
            proxies=proxies,
            timeout=(5.0, 10.0),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code == 200:
            ip = (resp.text or "").strip()
            if ip:
                _PROXY_IP_CACHE[proxy_url] = ip
                return ip
        return None
    except Exception:
        return None


def fetch_url(
    url: str,
    proxy_url: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Any = 30.0,
    method: str = "GET",
    **kwargs: Any,
) -> Tuple[int, str, str, Optional[str]]:
    """получает url через requests, при необходимости с резервным переходом на curl_cffi"""

    method_u = (method or "GET").upper()

    # нормализуем таймауты
    connect_timeout, read_timeout = _parse_timeout(timeout)

    proxies: Optional[Dict[str, str]] = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

        # минимальный connect-timeout для прокси (явно задается через env, чтобы не было "скрытых" 6 секунд)
        try:
            proxy_connect_floor = float(os.getenv("PROXY_CONNECT_TIMEOUT_FLOOR", "10"))
        except Exception:
            proxy_connect_floor = 10.0
        connect_timeout = max(connect_timeout, proxy_connect_floor)

    if "timeout" in kwargs:
        # держим логику timeout централизованной
        log_event(
            log,
            "http.timeout.kwargs_override",
            level="warning",
            transport="requests",
            provided=str(kwargs.get("timeout")),
        )
        kwargs.pop("timeout", None)

    timeout_tuple = (float(connect_timeout), float(read_timeout))

    # опциональная диагностика прокси
    if proxy_url and _env_flag("PROXY_IP_CHECK"):
        ip = check_proxy_ip(proxy_url)
        log_event(
            log,
            "proxy.ipcheck",
            level="debug" if ip else "warning",
            proxy=proxy_url,
            ip=ip,
        )

    # основной запрос через requests
    try:
        session = requests.Session()
        session.trust_env = False

        if headers:
            session.headers.update(headers)

        log_event(
            log,
            "http.request.start",
            level="debug",
            transport="requests",
            method=method_u,
            timeout=timeout_tuple,
            proxy=proxy_url,
        )

        if method_u == "GET":
            resp = session.get(url, proxies=proxies, timeout=timeout_tuple, **kwargs)
        elif method_u == "POST":
            resp = session.post(url, proxies=proxies, timeout=timeout_tuple, **kwargs)
        else:
            resp = session.request(method_u, url, proxies=proxies, timeout=timeout_tuple, **kwargs)

        session.close()

        log_event(
            log,
            "http.request.ok",
            level="debug",
            transport="requests",
            status_code=getattr(resp, "status_code", None),
            final_url=getattr(resp, "url", None),
        )

        return (resp.status_code, resp.text, resp.url, None)

    except (
        requests.exceptions.ProxyError,
        requests.exceptions.Timeout,
        requests.exceptions.ReadTimeout,
        requests.exceptions.ConnectTimeout,
    ) as e:
        # опциональный резервный переход на curl_cffi при ошибках, связанных с прокси
        log_exception(
            log,
            "http.request.error",
            e,
            level="warning",
            transport="requests",
            proxy=proxy_url,
            url=url,
        )

        if proxy_url and _should_use_curl_cffi():
            try:
                from curl_cffi import requests as curl_requests

                curl_session = curl_requests.Session()
                curl_session.trust_env = False

                if headers:
                    curl_session.headers.update(headers)

                curl_proxies = {"http": proxy_url, "https": proxy_url}

                log_event(
                    log,
                    "http.request.fallback",
                    level="warning",
                    transport="curl_cffi",
                    method=method_u,
                    proxy=proxy_url,
                )

                if method_u == "GET":
                    cresp = curl_session.get(url, proxies=curl_proxies, timeout=timeout, **kwargs)
                elif method_u == "POST":
                    cresp = curl_session.post(url, proxies=curl_proxies, timeout=timeout, **kwargs)
                else:
                    cresp = curl_session.request(method_u, url, proxies=curl_proxies, timeout=timeout, **kwargs)

                curl_session.close()

                return (cresp.status_code, cresp.text, cresp.url, None)

            except ImportError as ie:
                log_exception(log, "http.request.fallback_unavailable", ie, level="warning")
                return (0, "", url, f"requests failed: {e}, curl_cffi not available")
            except Exception as ce:
                log_exception(log, "http.request.fallback_failed", ce, level="warning")
                return (0, "", url, f"requests failed: {e}, curl_cffi failed: {ce}")

        return (0, "", url, str(e))

    except Exception as e:
        log_exception(log, "http.request.unhandled", e, level="error", transport="requests", url=url)
        return (0, "", url, str(e))

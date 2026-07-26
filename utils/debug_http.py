"""
небольшие помощники для диагностики http и сохранения debug html
модуль по умолчанию работает тихо; пишет структурированные логи через utils.logger"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from utils.domain import domain_of
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


def _collapse_spaces(value: str) -> str:
    return " ".join((value or "").split())


def _first_match(text: str, keywords: list[str]) -> str | None:
    for keyword in keywords:
        if keyword in text:
            return keyword
    return None


def _extract_title_and_visible_text(content: str) -> tuple[str, str]:
    if not content:
        return "", ""

    try:
        soup = BeautifulSoup(content, "html.parser")

        title_text = ""
        if soup.title:
            title_text = _collapse_spaces(soup.title.get_text(" ", strip=True).lower())

        for tag in soup.find_all(["script", "style", "noscript", "template"]):
            try:
                tag.decompose()
            except Exception:
                pass

        visible_text = _collapse_spaces(soup.get_text(" ", strip=True).lower())
        return title_text, visible_text
    except Exception:
        raw = _collapse_spaces((content or "").lower())
        return "", raw


def diagnose_response(
    url: str,
    status_code: int,
    final_url: str,
    content: str,
    content_type: str = "",
    proxy_used: str = "",
    headers: dict | None = None,
) -> dict:
    """диагностирует http-ответ на блокировки, редиректы и капчу"""

    domain = domain_of(url)
    content_lower = (content or "").lower()
    title_text, visible_text = _extract_title_and_visible_text(content or "")
    title_and_visible = f"{title_text} {visible_text}".strip()

    headers_lower: Dict[str, str] = {}
    if headers:
        try:
            headers_lower = {
                str(k).lower(): (v.lower() if isinstance(v, str) else str(v).lower())
                for k, v in headers.items()
            }
        except Exception:
            headers_lower = {}

    # жесткие маркеры: высокоуверенные признаки блокировки
    hard_markers = {
        "access_denied": [
            "access to this page has been denied",
            "sorry, you have been blocked",
            "you have been blocked",
            "request blocked",
            "forbidden",
            "access denied",
        ],
        "bot_challenge_text": [
            "before we continue",
            "press & hold to confirm you are",
            "press & hold",
            "verify you are human",
            "checking your browser",
            "just a moment",
            "attention required",
            "why have i been blocked",
            "unusual traffic",
            "security check",
        ],
        "js_block_page": [
            "please enable js and disable any ad blocker",
            "please enable javascript and disable any ad blocker",
            "enable javascript and cookies to continue",
        ],
        "perimeterx": [
            "px-captcha",
            "captcha.px-cloud.net",
            "window._pxappid",
            "window._pxuuid",
            "perimeterx",
        ],
        "datadome": [
            "captcha-delivery.com",
            "geo.captcha-delivery.com",
            "ct.captcha-delivery.com",
            "datadome",
        ],
    }
    soft_markers = {
        "enable_js": ["enable javascript", "javascript is disabled", "please enable javascript"],
        "cookie_wall": ["accept cookies", "cookie consent", "enable cookies", "manage cookies"],
    }

    found_markers: Dict[str, str] = {}

    # жесткие маркеры по видимому тексту (без script/style)
    for marker_type in ("access_denied", "bot_challenge_text", "js_block_page"):
        keyword = _first_match(title_and_visible, hard_markers[marker_type])
        if keyword:
            found_markers[marker_type] = keyword

    # жесткие маркеры по сырому html для известных антибот-провайдеров
    for marker_type in ("perimeterx", "datadome"):
        keyword = _first_match(content_lower, hard_markers[marker_type])
        if keyword:
            found_markers[marker_type] = keyword

    # проверка cloudflare-челленджа: не считаем блоком просто слово "cloudflare" в скриптах/иконках
    has_cf_challenge_script = (
        "/cdn-cgi/challenge-platform/" in content_lower
        or "cf-chl-" in content_lower
        or "challenge-platform/scripts/jsd" in content_lower
    )
    if has_cf_challenge_script:
        cf_text_keyword = _first_match(
            title_and_visible,
            [
                "attention required",
                "just a moment",
                "checking your browser",
                "verify you are human",
                "before we continue",
                "captcha",
            ],
        )
        if cf_text_keyword:
            found_markers["cloudflare_challenge"] = cf_text_keyword

    # мягкие маркеры только по видимому тексту
    for marker_type, keywords in soft_markers.items():
        keyword = _first_match(visible_text, keywords)
        if keyword:
            found_markers[marker_type] = keyword

    # определение прокси-аутентификации
    proxy_auth_detected = False
    if status_code == 407:
        proxy_auth_detected = True
        found_markers["proxy_auth"] = "407 status"
    elif headers_lower and "proxy-authenticate" in headers_lower:
        proxy_auth_detected = True
        found_markers["proxy_auth"] = "proxy-authenticate header"
    elif status_code == 407 and "proxy authentication required" in content_lower:
        proxy_auth_detected = True
        found_markers["proxy_auth"] = "407 + text"

    hard_found = any(
        key in found_markers
        for key in (
            "access_denied",
            "bot_challenge_text",
            "js_block_page",
            "perimeterx",
            "datadome",
            "cloudflare_challenge",
            "proxy_auth",
        )
    )

    # итоговый статус
    if status_code == 407 or proxy_auth_detected:
        status = "proxy_auth_required"
    elif status_code in (401, 403):
        status = "blocked"
    elif status_code >= 400:
        status = "error"
    elif hard_found:
        status = "blocked"
    elif final_url != url:
        status = "redirected"
    else:
        status = "ok"

    return {
        "domain": domain,
        "status_code": status_code,
        "final_url": final_url,
        "redirected": final_url != url,
        "content_type": content_type or "",
        "content_size": len(content or ""),
        "content_preview": (content or "")[:300],
        "found_markers": found_markers,
        "status": status,
        "proxy_used": proxy_used or "",
    }


def print_diagnosis(diagnosis: dict, verbose: bool = True) -> None:
    """устаревший хелпер: отправляет диагностику в логгер вместо print()"""

    if not isinstance(diagnosis, dict):
        log_event(log, "http.diagnosis.invalid", level="warning", diagnosis_type=type(diagnosis).__name__)
        return

    log_event(
        log,
        "http.diagnosis",
        level="info",
        domain=diagnosis.get("domain"),
        status=diagnosis.get("status"),
        status_code=diagnosis.get("status_code"),
        redirected=diagnosis.get("redirected"),
        content_type=diagnosis.get("content_type"),
        content_size=diagnosis.get("content_size"),
        proxy=diagnosis.get("proxy_used") or None,
        final_url=diagnosis.get("final_url"),
    )

    markers = diagnosis.get("found_markers") or {}
    if markers:
        log_event(log, "http.diagnosis.markers", level="warning", markers=markers)

    if verbose and diagnosis.get("content_preview"):
        log_event(log, "http.diagnosis.preview", level="debug", preview=diagnosis.get("content_preview"))


def save_debug_html(url: str, content: str, reason: str = "no_data") -> Optional[str]:
    """сохраняет html-снимок для отладки"""

    domain = domain_of(url)
    os.makedirs("debug", exist_ok=True)

    safe_reason = (reason or "no_data").replace("/", "_").replace("\\", "_")
    filename = f"debug/{domain}_{safe_reason}.html"

    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content or "")

        log_event(log, "debug.html.saved", level="info", path=filename, reason=safe_reason)
        return filename
    except Exception as e:
        log_exception(log, "debug.html.save_error", e, level="error", path=filename, reason=safe_reason)
        return None


def check_proxy_format(proxy_str: str) -> dict:
    """проверяет формат строки прокси"""

    result = {
        "valid": False,
        "format": None,
        "scheme": None,
        "error": None,
    }

    if not proxy_str:
        result["error"] = "empty"
        return result

    if proxy_str.startswith("http://") or proxy_str.startswith("https://"):
        result["scheme"] = "http" if proxy_str.startswith("http://") else "https"

        if "@" in proxy_str:
            result["format"] = "http://user:pass@ip:port"
            result["valid"] = True
        else:
            result["format"] = "http://ip:port"
            result["valid"] = True
    else:
        result["error"] = "missing scheme"

    return result


def test_proxy_auth(proxy_dict: dict, test_url: str = "https://httpbin.org/ip") -> dict:
    """быстрый тест прокси через requests"""

    import requests

    result = {
        "success": False,
        "status_code": None,
        "error": None,
        "response": None,
    }

    try:
        response = requests.get(test_url, proxies=proxy_dict, timeout=10)
        result["success"] = True
        result["status_code"] = response.status_code
        result["response"] = (response.text or "")[:200]
    except requests.exceptions.ProxyError as e:
        result["error"] = f"proxy error: {e}"
    except requests.exceptions.Timeout:
        result["error"] = "timeout"
    except Exception as e:
        result["error"] = f"error: {e}"

    return result

"""
быстрый fallback через openrouter для заполнения пустых полей карточки товара"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from typing import Any

import requests
from bs4 import BeautifulSoup

from config import (
    OPENROUTER_API_KEY,
    OPENROUTER_FALLBACK_ALLOW_PAID,
    OPENROUTER_FALLBACK_CONNECT_TIMEOUT_SEC,
    OPENROUTER_FALLBACK_ENABLED,
    OPENROUTER_FALLBACK_MAX_CONCURRENCY,
    OPENROUTER_FALLBACK_MAX_HTML_CHARS,
    OPENROUTER_FALLBACK_MAX_TOKENS,
    OPENROUTER_FALLBACK_MODELS,
    OPENROUTER_FALLBACK_QUEUE_WAIT_SEC,
    OPENROUTER_FALLBACK_TIMEOUT_SEC,
)
from utils.logger import get_logger, log_event, log_exception
from utils.price import normalize_currency_code, parse_price_and_currency

log = get_logger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODELS = ("openrouter/free", "openai/gpt-oss-20b:free")
_RAW_MODELS = tuple(m for m in OPENROUTER_FALLBACK_MODELS if m) or _DEFAULT_MODELS
_MAX_HTML_CHARS = max(2000, int(OPENROUTER_FALLBACK_MAX_HTML_CHARS))
_MAX_TOKENS = max(64, int(OPENROUTER_FALLBACK_MAX_TOKENS))
_MAX_CONCURRENCY = max(1, int(OPENROUTER_FALLBACK_MAX_CONCURRENCY))
_QUEUE_WAIT_SEC = max(0.0, float(OPENROUTER_FALLBACK_QUEUE_WAIT_SEC))
_CONNECT_TIMEOUT_SEC = max(0.5, float(OPENROUTER_FALLBACK_CONNECT_TIMEOUT_SEC))
_READ_TIMEOUT_SEC = max(1.0, float(OPENROUTER_FALLBACK_TIMEOUT_SEC))
_REQUEST_TIMEOUT = (_CONNECT_TIMEOUT_SEC, _READ_TIMEOUT_SEC)
_FREE_POLICY_RECHECK_SEC = max(60, int(os.getenv("OPENROUTER_FREE_POLICY_RECHECK_SEC", "1800")))
_MAX_MODEL_TRIES = max(1, int(os.getenv("OPENROUTER_FALLBACK_MAX_MODEL_TRIES", "2")))
_MODEL_RETRY_JITTER_SEC = max(0.0, float(os.getenv("OPENROUTER_FALLBACK_MODEL_RETRY_JITTER_SEC", "0.10")))
_MODEL_COOLDOWN_429_SEC = max(10, int(os.getenv("OPENROUTER_FALLBACK_MODEL_COOLDOWN_429_SEC", "90")))
_MODEL_COOLDOWN_TIMEOUT_SEC = max(5, int(os.getenv("OPENROUTER_FALLBACK_MODEL_COOLDOWN_TIMEOUT_SEC", "45")))
_MODEL_COOLDOWN_5XX_SEC = max(10, int(os.getenv("OPENROUTER_FALLBACK_MODEL_COOLDOWN_5XX_SEC", "60")))
_MODEL_COOLDOWN_NOT_FOUND_SEC = max(60, int(os.getenv("OPENROUTER_FALLBACK_MODEL_COOLDOWN_NOT_FOUND_SEC", "900")))
_MODEL_COOLDOWN_UNKNOWN_SEC = max(10, int(os.getenv("OPENROUTER_FALLBACK_MODEL_COOLDOWN_UNKNOWN_SEC", "30")))
_MODEL_COOLDOWN_EMPTY_SEC = max(5, int(os.getenv("OPENROUTER_FALLBACK_MODEL_COOLDOWN_EMPTY_SEC", "20")))

_SOLD_MARKERS = ("sold", "sold out", "out of stock", "unavailable", "ended", "продано", "нет в наличии")
_SKIP_STATUS_MARKERS = ("blocked", "proxy_auth_required", "catalog", "redirect_mismatch")
_EMPTY_MARKERS = {"", "unknown", "none", "null", "n/a", "na"}

_SEMAPHORE = threading.BoundedSemaphore(_MAX_CONCURRENCY)
_SESSION = requests.Session()
_SESSION.trust_env = False
_SESSION.headers.update(
    {
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost",
        "X-Title": "serpapi-parser",
    }
)
_MISSING_KEY_WARNED = False
_FREE_POLICY_BLOCKED_UNTIL_TS = 0.0
_FREE_POLICY_WARNED = False
_MODEL_COOLDOWN_LOCK = threading.Lock()
_MODEL_COOLDOWN_UNTIL: dict[str, float] = {}
_MODEL_COOLDOWN_REASON: dict[str, str] = {}


def _is_free_model_name(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return bool(normalized) and (normalized == "openrouter/free" or normalized.endswith(":free"))


def _expand_model_candidates(models: tuple[str, ...], allow_paid_fallback: bool) -> tuple[str, ...]:
    # по умолчанию используем только заданные модели; платные fallback включаются отдельным флагом
    out: list[str] = []
    seen: set[str] = set()
    skipped_non_free: list[str] = []
    for model in models:
        candidate = (model or "").strip()
        if not candidate:
            continue
        if not allow_paid_fallback and not _is_free_model_name(candidate):
            skipped_non_free.append(candidate)
            continue
        if candidate not in seen:
            out.append(candidate)
            seen.add(candidate)
        if allow_paid_fallback and candidate.endswith(":free"):
            paid_candidate = candidate[: -len(":free")].strip()
            if paid_candidate and paid_candidate not in seen:
                out.append(paid_candidate)
                seen.add(paid_candidate)
    if not out:
        out = [m for m in _DEFAULT_MODELS if _is_free_model_name(m)] or list(_DEFAULT_MODELS)
    if skipped_non_free:
        log_event(
            log,
            "openrouter.fallback.skip_non_free_models",
            level="warning",
            models=skipped_non_free,
        )
    return tuple(out)

_MODELS = _expand_model_candidates(_RAW_MODELS, OPENROUTER_FALLBACK_ALLOW_PAID)


def _get_model_cooldown_left(model: str, *, now_ts: float | None = None) -> float:
    now = now_ts if now_ts is not None else time.time()
    with _MODEL_COOLDOWN_LOCK:
        until = float(_MODEL_COOLDOWN_UNTIL.get(model, 0.0))
    return max(0.0, until - now)


def _set_model_cooldown(model: str, seconds: int, reason: str) -> None:
    if seconds <= 0:
        return
    until_ts = time.time() + float(seconds)
    with _MODEL_COOLDOWN_LOCK:
        prev_until = float(_MODEL_COOLDOWN_UNTIL.get(model, 0.0))
        if until_ts > prev_until:
            _MODEL_COOLDOWN_UNTIL[model] = until_ts
            _MODEL_COOLDOWN_REASON[model] = reason


def _clear_model_cooldown(model: str) -> None:
    with _MODEL_COOLDOWN_LOCK:
        _MODEL_COOLDOWN_UNTIL.pop(model, None)
        _MODEL_COOLDOWN_REASON.pop(model, None)


def _pick_models_for_request(seed: str) -> list[str]:
    if not _MODELS:
        return []

    model_count = len(_MODELS)
    start_idx = abs(hash(seed)) % model_count
    ordered = list(_MODELS[start_idx:]) + list(_MODELS[:start_idx])
    now = time.time()
    ready: list[str] = []
    cooling: list[tuple[str, float]] = []
    for model in ordered:
        cooldown_left = _get_model_cooldown_left(model, now_ts=now)
        if cooldown_left > 0:
            cooling.append((model, cooldown_left))
        else:
            ready.append(model)

    max_tries = max(1, min(_MAX_MODEL_TRIES, model_count))
    if len(ready) >= max_tries:
        return ready[:max_tries]

    selected = list(ready)
    if len(selected) < max_tries and cooling:
        cooling_sorted = sorted(cooling, key=lambda x: x[1])
        selected.extend(model for model, _ in cooling_sorted[: max_tries - len(selected)])
    return selected[:max_tries]


def _mark_free_policy_blocked() -> None:
    global _FREE_POLICY_BLOCKED_UNTIL_TS, _FREE_POLICY_WARNED
    _FREE_POLICY_BLOCKED_UNTIL_TS = time.time() + _FREE_POLICY_RECHECK_SEC
    _FREE_POLICY_WARNED = False


def _is_free_policy_blocked() -> bool:
    return time.time() < _FREE_POLICY_BLOCKED_UNTIL_TS


def _clean_text(value: Any, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split()).strip()
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _is_missing_text(value: Any, unknown_as_empty: bool = True) -> bool:
    text = _clean_text(value, limit=120)
    if not text:
        return True
    if unknown_as_empty and text.lower() in _EMPTY_MARKERS:
        return True
    return False


def _is_missing_status(status: str | None) -> bool:
    return _is_missing_text(status, unknown_as_empty=True)


def _is_sold_status(status: str | None) -> bool:
    if not status:
        return False
    low = str(status).lower()
    return any(marker in low for marker in _SOLD_MARKERS)


def _should_skip_status(status: str | None) -> bool:
    if not status:
        return False
    low = str(status).strip().lower()
    return any(marker in low for marker in _SKIP_STATUS_MARKERS)


def _append_unique(lines: list[str], seen: set[str], raw_line: str | None, budget: int) -> int:
    if budget <= 0:
        return 0
    line = _clean_text(raw_line, limit=900)
    if not line or line in seen:
        return budget
    if len(line) + 1 > budget:
        line = line[: max(0, budget - 1)].rstrip()
    if len(line) < 4:
        return budget
    lines.append(line)
    seen.add(line)
    return budget - len(line) - 1


def _compact_tag(tag: Any) -> str | None:
    name = getattr(tag, "name", None)
    if not name:
        return None
    name = str(name).lower()
    if name in {"script", "style", "noscript", "svg", "path"}:
        return None

    try:
        text = tag.get_text(" ", strip=True)
    except Exception:
        return None
    text = _clean_text(text, limit=220)
    if not text:
        return None

    attrs: list[str] = []
    for attr_name in ("id", "class", "itemprop", "data-testid", "data-cy", "aria-label"):
        try:
            attr_val = tag.get(attr_name)
        except Exception:
            attr_val = None
        if not attr_val:
            continue
        if isinstance(attr_val, (list, tuple)):
            attr_text = " ".join(str(v) for v in attr_val[:4])
        else:
            attr_text = str(attr_val)
        attr_text = _clean_text(attr_text, limit=80)
        if attr_text:
            attrs.append(f'{attr_name}="{attr_text}"')
    attrs_str = f" {' '.join(attrs)}" if attrs else ""
    return f"<{name}{attrs_str}>{text}</{name}>"


def _build_compact_html_snapshot(soup: BeautifulSoup) -> str:
    # оставляем только сигналы, которые полезны для price/status/title/condition
    budget = _MAX_HTML_CHARS
    lines: list[str] = []
    seen: set[str] = set()

    if soup.title:
        budget = _append_unique(lines, seen, f"<title>{soup.title.get_text(strip=True)}</title>", budget)

    meta_keywords = ("title", "price", "amount", "currency", "availability", "stock", "condition", "product")
    for meta in soup.find_all("meta", limit=120):
        key = (
            meta.get("property")
            or meta.get("name")
            or meta.get("itemprop")
            or meta.get("data-testid")
            or ""
        )
        key_low = str(key).lower()
        if key_low and not any(k in key_low for k in meta_keywords):
            continue
        content = _clean_text(meta.get("content"), limit=260)
        if not content:
            continue
        budget = _append_unique(lines, seen, f'<meta {key}="{content}">', budget)
        if budget <= 0:
            break

    for script in soup.find_all("script", limit=50):
        script_type = str(script.get("type") or "").lower()
        if "ld+json" not in script_type:
            continue
        raw_script = script.string if script.string is not None else script.get_text(" ", strip=True)
        compact_script = _clean_text(raw_script, limit=1800)
        if not compact_script:
            continue
        budget = _append_unique(lines, seen, f'<script type="application/ld+json">{compact_script}</script>', budget)
        if budget <= 0:
            break

    selector_limits: tuple[tuple[str, int], ...] = (
        ("h1, h2, h3", 8),
        ("[itemprop*='name'], [itemprop*='price'], [itemprop*='availability'], [itemprop*='condition']", 30),
        ("[class*='price'], [id*='price'], [data-testid*='price'], [data-cy*='price']", 35),
        ("[class*='status'], [id*='status'], [class*='stock'], [id*='stock'], [data-testid*='availability']", 30),
        ("[class*='condition'], [id*='condition'], [data-testid*='condition'], [data-cy*='condition']", 25),
        ("button, a", 18),
    )
    for selector, limit in selector_limits:
        if budget <= 0:
            break
        try:
            matches = soup.select(selector)
        except Exception:
            continue
        added = 0
        for tag in matches:
            if added >= limit or budget <= 0:
                break
            line = _compact_tag(tag)
            new_budget = _append_unique(lines, seen, line, budget)
            if new_budget != budget:
                added += 1
                budget = new_budget

    if budget > 320:
        page_text = _clean_text(soup.get_text(" ", strip=True), limit=min(1600, budget - 10))
        budget = _append_unique(lines, seen, f"<text>{page_text}</text>" if page_text else None, budget)

    snapshot = "\n".join(lines).strip()
    if len(snapshot) > _MAX_HTML_CHARS:
        snapshot = snapshot[:_MAX_HTML_CHARS]
    return snapshot


def _build_messages(url: str, domain: str, requested_fields: list[str], compact_html: str) -> list[dict[str, str]]:
    fields_template = "{\n" + ",\n".join(f'  "{field}": null' for field in requested_fields) + "\n}"

    system_prompt = (
        "Extract missing product fields from HTML. Return JSON only. "
        "No explanations, no markdown fences. Use null for unknown values."
    )
    user_prompt = (
        f"url: {url}\n"
        f"domain: {domain}\n"
        f"missing_fields: {', '.join(requested_fields)}\n"
        "rules:\n"
        "1) return ONLY this JSON shape:\n"
        f"{fields_template}\n"
        "2) price must be a number without currency symbols (example: 1299.99).\n"
        "3) currency must be ISO code (USD/EUR/GBP...).\n"
        "4) use only facts from html; do not invent.\n"
        "5) if a field is not found, return null.\n"
        "compressed_html:\n"
        f"{compact_html}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _extract_content(payload: dict[str, Any]) -> str | None:
    try:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = choices[0].get("message", {})
        content = message.get("content")
    except Exception:
        return None

    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip() if parts else None
    return None


def _extract_json(content: str | None) -> dict[str, Any] | None:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _request_model(model: str, messages: list[dict[str, str]]) -> dict[str, Any] | None:
    if not OPENROUTER_API_KEY:
        return None

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": _MAX_TOKENS,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}

    def _post(request_payload: dict[str, Any]) -> requests.Response | None:
        try:
            return _SESSION.post(_OPENROUTER_URL, json=request_payload, headers=headers, timeout=_REQUEST_TIMEOUT)
        except Exception as e:
            log_exception(log, "openrouter.fallback.request_error", e, level="warning", model=model)
            _set_model_cooldown(model, _MODEL_COOLDOWN_TIMEOUT_SEC, "request_error")
            return None

    resp = _post(payload)
    if resp is None:
        return None

    if resp.status_code >= 400:
        body_preview = _clean_text(resp.text, limit=220) or ""
        body_preview_low = body_preview.lower()
        if resp.status_code == 400 and ("response_format" in body_preview_low or "json_object" in body_preview_low):
            payload_no_format = dict(payload)
            payload_no_format.pop("response_format", None)
            resp = _post(payload_no_format)
            if resp is None:
                return None
            body_preview = _clean_text(resp.text, limit=220) or body_preview
            body_preview_low = body_preview.lower()
        if (
            resp.status_code == 404
            and "data policy" in body_preview_low
            and "free model publication" in body_preview_low
        ):
            _mark_free_policy_blocked()
        if resp.status_code >= 400:
            if resp.status_code == 429:
                _set_model_cooldown(model, _MODEL_COOLDOWN_429_SEC, "rate_limited")
            elif resp.status_code == 404:
                _set_model_cooldown(model, _MODEL_COOLDOWN_NOT_FOUND_SEC, "not_found")
            elif resp.status_code >= 500:
                _set_model_cooldown(model, _MODEL_COOLDOWN_5XX_SEC, "upstream_error")
            else:
                _set_model_cooldown(model, _MODEL_COOLDOWN_UNKNOWN_SEC, f"status_{resp.status_code}")
            log_event(
                log,
                "openrouter.fallback.bad_status",
                level="warning",
                model=model,
                status_code=resp.status_code,
                body=body_preview,
            )
            return None

    try:
        payload_json = resp.json()
    except Exception as e:
        log_exception(log, "openrouter.fallback.bad_json", e, level="warning", model=model)
        _set_model_cooldown(model, _MODEL_COOLDOWN_UNKNOWN_SEC, "bad_json")
        return None

    content = _extract_content(payload_json)
    parsed_json = _extract_json(content)
    if parsed_json is None:
        _set_model_cooldown(model, _MODEL_COOLDOWN_EMPTY_SEC, "empty_payload")
    return parsed_json


def _normalize_extracted(
    raw: dict[str, Any],
    requested_fields: list[str],
    current_currency: str | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}

    requested = set(requested_fields)
    title_raw = raw.get("title") or raw.get("name") or raw.get("product_name")
    status_raw = raw.get("status") or raw.get("availability")
    condition_raw = raw.get("condition") or raw.get("item_condition")
    price_raw = raw.get("price")
    currency_raw = raw.get("currency") or raw.get("currency_code") or raw.get("price_currency")
    price_text_raw = raw.get("price_text") or raw.get("price_raw")

    if "title" in requested:
        title = _clean_text(title_raw, limit=260)
        if title and title.lower() not in _EMPTY_MARKERS:
            out["title"] = title

    if "status" in requested:
        status = _clean_text(status_raw, limit=80)
        if status and status.lower() not in _EMPTY_MARKERS:
            out["status"] = status

    if "condition" in requested:
        condition = _clean_text(condition_raw, limit=90)
        if condition and condition.lower() not in _EMPTY_MARKERS:
            out["condition"] = condition

    parsed_price: float | None = None
    parsed_currency = normalize_currency_code(currency_raw)
    if price_raw is not None:
        if isinstance(price_raw, (int, float)):
            parsed_price = float(price_raw)
        else:
            as_text = str(price_raw).strip()
            parsed_price, parsed_cur = parse_price_and_currency(as_text)
            parsed_currency = parsed_currency or normalize_currency_code(parsed_cur)
            if parsed_price is None:
                try:
                    parsed_price = float(as_text.replace(",", "").replace(" ", ""))
                except Exception:
                    parsed_price = None

    if parsed_price is None and price_text_raw is not None:
        parsed_price, parsed_cur = parse_price_and_currency(str(price_text_raw))
        parsed_currency = parsed_currency or normalize_currency_code(parsed_cur)

    if parsed_price is not None and parsed_price <= 0:
        parsed_price = None

    if "price" in requested and parsed_price is not None:
        out["price"] = parsed_price
    if "currency" in requested:
        if parsed_currency:
            out["currency"] = parsed_currency
        elif parsed_price is not None:
            out["currency"] = normalize_currency_code(current_currency) or "USD"

    return out


def enrich_listing_fields_via_openrouter(
    *,
    soup: BeautifulSoup | None,
    url: str,
    domain: str,
    title: str | None,
    price: float | None,
    currency: str | None,
    status: str | None,
    condition: str | None,
    include_condition: bool,
) -> dict[str, Any]:
    global _MISSING_KEY_WARNED, _FREE_POLICY_WARNED
    # не запускаем fallback, если нет ключа или он выключен
    if not OPENROUTER_FALLBACK_ENABLED:
        return {}
    if not OPENROUTER_API_KEY:
        if not _MISSING_KEY_WARNED:
            log_event(log, "openrouter.fallback.disabled_no_key", level="warning")
            _MISSING_KEY_WARNED = True
        return {}
    if _is_free_policy_blocked():
        if not _FREE_POLICY_WARNED:
            log_event(
                log,
                "openrouter.fallback.skip_free_policy_blocked",
                level="warning",
                hint="enable 'Free model publication' in OpenRouter privacy settings",
                recheck_sec=_FREE_POLICY_RECHECK_SEC,
            )
            _FREE_POLICY_WARNED = True
        return {}
    if soup is None:
        return {}
    if _should_skip_status(status):
        return {}

    requested_fields: list[str] = []
    if _is_missing_text(title, unknown_as_empty=True):
        requested_fields.append("title")
    if _is_missing_status(status):
        requested_fields.append("status")
    if price is None and not _is_sold_status(status):
        requested_fields.append("price")
    if "price" in requested_fields or (price is not None and _is_missing_text(currency, unknown_as_empty=True)):
        requested_fields.append("currency")
    if include_condition and _is_missing_text(condition, unknown_as_empty=True):
        requested_fields.append("condition")

    if not requested_fields:
        return {}

    compact_html = _build_compact_html_snapshot(soup)
    if len(compact_html) < 40:
        log_event(log, "openrouter.fallback.skip_small_html", level="debug", domain=domain)
        return {}

    if not _SEMAPHORE.acquire(timeout=_QUEUE_WAIT_SEC):
        log_event(
            log,
            "openrouter.fallback.skip_busy",
            level="debug",
            domain=domain,
            requested_fields=requested_fields,
        )
        return {}

    try:
        log_event(
            log,
            "openrouter.fallback.start",
            level="info",
            domain=domain,
            requested_fields=requested_fields,
            html_chars=len(compact_html),
        )
        messages = _build_messages(
            url=url,
            domain=domain,
            requested_fields=requested_fields,
            compact_html=compact_html,
        )
        model_seed = f"{domain}|{url}|{','.join(requested_fields)}"
        models_for_request = _pick_models_for_request(model_seed)
        if not models_for_request:
            log_event(log, "openrouter.fallback.no_models", level="warning", domain=domain)
            return {}

        for idx, model in enumerate(models_for_request):
            cooldown_left = _get_model_cooldown_left(model)
            if cooldown_left > 0:
                log_event(
                    log,
                    "openrouter.fallback.model_on_cooldown",
                    level="debug",
                    domain=domain,
                    model=model,
                    cooldown_left_sec=round(cooldown_left, 2),
                )
            log_event(log, "openrouter.fallback.model_try", level="debug", domain=domain, model=model)
            raw = _request_model(model=model, messages=messages)
            if not raw:
                log_event(log, "openrouter.fallback.model_failed", level="warning", domain=domain, model=model)
                if idx < (len(models_for_request) - 1) and _MODEL_RETRY_JITTER_SEC > 0:
                    time.sleep(_MODEL_RETRY_JITTER_SEC + random.uniform(0.0, 0.05))
                continue
            extracted = _normalize_extracted(raw, requested_fields, current_currency=currency)
            if extracted:
                _clear_model_cooldown(model)
                log_event(
                    log,
                    "openrouter.fallback.ok",
                    level="info",
                    domain=domain,
                    model=model,
                    requested_fields=requested_fields,
                    filled_fields=list(extracted.keys()),
                    html_chars=len(compact_html),
                )
                return extracted
            log_event(
                log,
                "openrouter.fallback.model_empty",
                level="warning",
                domain=domain,
                model=model,
            )
            _set_model_cooldown(model, _MODEL_COOLDOWN_EMPTY_SEC, "model_empty")
            if idx < (len(models_for_request) - 1) and _MODEL_RETRY_JITTER_SEC > 0:
                time.sleep(_MODEL_RETRY_JITTER_SEC + random.uniform(0.0, 0.05))
    except Exception as e:
        log_exception(log, "openrouter.fallback.error", e, level="warning", domain=domain)
    finally:
        _SEMAPHORE.release()

    log_event(
        log,
        "openrouter.fallback.empty",
        level="warning",
        domain=domain,
        requested_fields=requested_fields,
    )
    return {}

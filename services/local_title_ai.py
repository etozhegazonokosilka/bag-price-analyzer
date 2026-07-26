"""
локальный ai-gate для отбора релевантных ссылок по названиям serpapi"""

from __future__ import annotations

import ast
import json
import os
import re
import unicodedata
from collections import Counter
from typing import Any
from urllib.parse import urlparse

import requests

from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)

_STOPWORDS = {
    "bag",
    "bags",
    "handbag",
    "handbags",
    "women",
    "woman",
    "men",
    "man",
    "small",
    "medium",
    "large",
    "mini",
    "vintage",
    "authentic",
    "black",
    "brown",
    "white",
    "pink",
    "red",
    "blue",
    "green",
    "new",
    "used",
    "preloved",
    "with",
    "without",
    "and",
    "for",
    "the",
    "this",
    "that",
}
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "1" if default else "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, min_value: int) -> int:
    raw = (os.getenv(name, str(default)) or "").strip()
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(min_value, value)


def _env_float(name: str, default: float, min_value: float) -> float:
    raw = (os.getenv(name, str(default)) or "").strip()
    try:
        value = float(raw)
    except Exception:
        value = default
    return max(min_value, value)


def _clean_text(value: Any, limit: int = 240) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split()).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _url_domain(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = urlparse(str(value).strip())
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _extract_json_payload(raw_text: str) -> dict[str, Any] | None:
    text = (raw_text or "").strip()
    if not text:
        return None

    def _try_parse(fragment: str) -> dict[str, Any] | None:
        candidate = (fragment or "").strip()
        if not candidate:
            return None
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(candidate)
            if isinstance(parsed, dict):
                return json.loads(json.dumps(parsed, ensure_ascii=False))
        except Exception:
            return None
        return None

    parsed_direct = _try_parse(text)
    if parsed_direct:
        return parsed_direct

    # частый случай: ответ пришел в markdown-блоке ```json ... ```
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        parsed_fenced = _try_parse(fence_match.group(1))
        if parsed_fenced:
            return parsed_fenced

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        parsed_fragment = _try_parse(text[start : end + 1])
        if parsed_fragment:
            return parsed_fragment

    return None


def _extract_message_content_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for part in content:
        if isinstance(part, str):
            chunks.append(part)
            continue
        if not isinstance(part, dict):
            continue
        text_value = part.get("text")
        if isinstance(text_value, str) and text_value.strip():
            chunks.append(text_value)
            continue
        content_value = part.get("content")
        if isinstance(content_value, str) and content_value.strip():
            chunks.append(content_value)
            continue
        value = part.get("value")
        if isinstance(value, str) and value.strip():
            chunks.append(value)
    return "\n".join(chunks).strip()


def _normalize_brand_token(value: str | None) -> str:
    if not value:
        return ""
    # приводим акценты/диакритику к ascii, чтобы "sac à main" и "sac a main" сравнивались одинаково
    ascii_text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()
    return " ".join(normalized.split())


def _candidate_has_brand(candidate: dict[str, Any], target_brand: str | None) -> bool:
    brand = _normalize_brand_token(target_brand)
    if not brand:
        return True
    title = _normalize_brand_token(candidate.get("title"))
    url = _normalize_brand_token(candidate.get("url"))
    return bool(brand and (brand in title or brand in url))


def _tokenize_title(value: str) -> list[str]:
    normalized = _normalize_brand_token(value)
    if not normalized:
        return []
    tokens = [token for token in _TOKEN_RE.findall(normalized) if token not in _STOPWORDS]
    return tokens


def _build_keywords_from_target_name(target_name: str | None, *, limit: int) -> list[str]:
    normalized = _normalize_brand_token(target_name)
    if not normalized:
        return []

    parts = [part for part in normalized.split() if part]
    if not parts:
        return []

    candidates: list[str] = []

    # сначала добавляем фразы, потом отдельные токены
    for ngram_size in (3, 2):
        if len(parts) < ngram_size:
            continue
        for idx in range(len(parts) - ngram_size + 1):
            phrase = " ".join(parts[idx : idx + ngram_size]).strip()
            if len(phrase) < 3:
                continue
            candidates.append(phrase)

    for token in parts:
        if len(token) < 3:
            continue
        if token in _STOPWORDS:
            continue
        candidates.append(token)

    keywords: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        keywords.append(item)
        if len(keywords) >= limit:
            break
    return keywords


def _build_fallback_target_name(
    candidates: list[dict[str, Any]],
    *,
    selected_indexes: list[int] | None,
    target_brand: str | None,
    keyword_hints: list[str] | None,
    max_tokens: int = 5,
) -> str | None:
    if not candidates:
        return None

    valid_indexes: list[int] = []
    for raw_idx in selected_indexes or []:
        if isinstance(raw_idx, int) and 0 <= raw_idx < len(candidates):
            valid_indexes.append(raw_idx)
    if not valid_indexes:
        valid_indexes = list(range(min(len(candidates), 25)))

    brand_tokens = [token for token in _normalize_brand_token(target_brand).split() if token]

    token_counter: Counter[str] = Counter()
    phrase_counter: Counter[str] = Counter()
    for idx in valid_indexes:
        tokens = _tokenize_title(candidates[idx].get("title") or "")
        if not tokens:
            continue
        token_counter.update(tokens)
        for ngram_size in (3, 2):
            if len(tokens) < ngram_size:
                continue
            for start_idx in range(len(tokens) - ngram_size + 1):
                phrase = " ".join(tokens[start_idx : start_idx + ngram_size]).strip()
                if phrase:
                    phrase_counter[phrase] += 1

    hint_tokens: list[str] = []
    for hint in keyword_hints or []:
        for token in _tokenize_title(hint):
            if token in brand_tokens:
                continue
            if token not in hint_tokens:
                hint_tokens.append(token)

    model_tokens: list[str] = []
    for phrase, count in phrase_counter.most_common(20):
        if count < 2 and model_tokens:
            continue
        parts = [part for part in phrase.split() if part and part not in brand_tokens]
        if not parts:
            continue
        for part in parts:
            if part in model_tokens:
                continue
            model_tokens.append(part)
            if len(model_tokens) >= max_tokens:
                break
        if len(model_tokens) >= max_tokens:
            break

    if len(model_tokens) < max_tokens:
        for token, _ in token_counter.most_common(40):
            if token in brand_tokens or token in model_tokens:
                continue
            model_tokens.append(token)
            if len(model_tokens) >= max_tokens:
                break

    ordered_tokens: list[str] = []
    for token in [*brand_tokens, *hint_tokens, *model_tokens]:
        if not token or token in _STOPWORDS:
            continue
        if token in ordered_tokens:
            continue
        ordered_tokens.append(token)
        if len(ordered_tokens) >= (len(brand_tokens) + max_tokens):
            break

    minimum_tokens = 1 if brand_tokens else 2
    if len(ordered_tokens) < minimum_tokens:
        return None

    def _pretty(token: str) -> str:
        if len(token) <= 3:
            return token.upper()
        return token.capitalize()

    final_len = max(2, min(len(ordered_tokens), len(brand_tokens) + max_tokens))
    return " ".join(_pretty(token) for token in ordered_tokens[:final_len]).strip() or None


def _fallback_select_indexes(
    candidates: list[dict[str, Any]],
    *,
    keep_limit: int,
    target_brand: str | None,
) -> tuple[list[int], dict[int, str]]:
    # детерминированный fallback на случай ошибки модели:
    # используем частотные токены + приоритет exact_matches + совпадение бренда
    token_weights: dict[str, int] = {}
    for item in candidates[:30]:
        for token in _tokenize_title(item.get("title") or ""):
            token_weights[token] = token_weights.get(token, 0) + 1

    scored: list[tuple[int, int, int]] = []
    for idx, item in enumerate(candidates):
        score = 0
        if (item.get("serp_source") or "") == "exact_matches":
            score += 4
        if _candidate_has_brand(item, target_brand):
            score += 3
        for token in _tokenize_title(item.get("title") or ""):
            score += token_weights.get(token, 0)
        # сортировка по score desc, затем по рангу serpapi (idx asc)
        scored.append((score, -idx, idx))

    scored.sort(reverse=True)
    selected = [idx for _, _, idx in scored[:keep_limit]]
    reason_by_index = {
        idx: "детерминированный fallback (без ответа локальной модели)"
        for idx in range(len(candidates))
        if idx not in set(selected)
    }
    return selected, reason_by_index


def _normalize_keyword_list(raw: Any, *, limit: int) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        items = [str(raw)]

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize_brand_token(item)
        if not normalized:
            continue
        parts = [p.strip() for p in re.split(r"[|,;/]+", normalized) if p.strip()]
        if not parts:
            parts = [normalized]
        for part in parts:
            if len(part) < 3:
                continue
            if part in _STOPWORDS:
                continue
            if part in seen:
                continue
            seen.add(part)
            out.append(part)
            if len(out) >= limit:
                return out
    return out


def _build_fallback_keyword_profile(
    candidates: list[dict[str, Any]],
    *,
    keyword_limit: int,
    target_brand: str | None,
) -> dict[str, Any]:
    token_counter: Counter[str] = Counter()
    phrase_counter: Counter[str] = Counter()

    for item in candidates:
        tokens = _tokenize_title(item.get("title") or "")
        if not tokens:
            continue
        token_counter.update(tokens)
        for left, right in zip(tokens, tokens[1:]):
            phrase = f"{left} {right}".strip()
            if phrase and left != right:
                phrase_counter[phrase] += 1

    keywords: list[str] = []
    for phrase, count in phrase_counter.most_common(keyword_limit * 2):
        if count < 2:
            continue
        if phrase not in keywords:
            keywords.append(phrase)
        if len(keywords) >= keyword_limit:
            break

    if len(keywords) < keyword_limit:
        for token, _ in token_counter.most_common(keyword_limit * 3):
            if token in keywords:
                continue
            keywords.append(token)
            if len(keywords) >= keyword_limit:
                break

    fallback_target_name = _build_fallback_target_name(
        candidates,
        selected_indexes=None,
        target_brand=target_brand,
        keyword_hints=keywords,
        max_tokens=4,
    )

    return {
        "target_name": fallback_target_name,
        "keywords": keywords[:keyword_limit],
        "negative_keywords": [],
    }


def _extract_http_error_detail(response: requests.Response, *, limit: int = 320) -> str:
    status_code = getattr(response, "status_code", None)
    details: list[str] = [f"status={status_code}" if status_code is not None else "status=unknown"]

    payload: Any = None
    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        error_obj = payload.get("error")
        if isinstance(error_obj, dict):
            err_type = _clean_text(error_obj.get("type"), limit=80)
            err_code = _clean_text(error_obj.get("code"), limit=80)
            err_message = _clean_text(error_obj.get("message"), limit=limit)
            if err_type:
                details.append(f"type={err_type}")
            if err_code:
                details.append(f"code={err_code}")
            if err_message:
                details.append(f"message={err_message}")
            return "; ".join(details)

        payload_text = _clean_text(json.dumps(payload, ensure_ascii=False), limit=limit)
        if payload_text:
            details.append(f"body={payload_text}")
            return "; ".join(details)

    body_text = _clean_text(getattr(response, "text", ""), limit=limit)
    if body_text:
        details.append(f"body={body_text}")
    return "; ".join(details)


def _is_response_format_compat_error(error_detail: str) -> bool:
    lowered = (error_detail or "").lower()
    if "response_format" not in lowered:
        return False
    markers = (
        "unknown",
        "unsupported",
        "not support",
        "not allowed",
        "unrecognized",
        "invalid",
        "extra fields",
    )
    return any(marker in lowered for marker in markers)


def _post_llm_json(
    *,
    provider: str,
    endpoint: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    connect_timeout: float,
    read_timeout: float,
    num_ctx: int,
    num_predict: int,
    temperature: float,
    api_key: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    normalized_provider = (provider or "openai").strip().lower()
    if normalized_provider != "openai":
        raise ValueError("поддерживается только LOCAL_TITLE_AI_PROVIDER=openai")
    if not api_key:
        raise ValueError("не задан OPENAI API key для LOCAL_TITLE_AI_PROVIDER=openai")
    json_retry_attempts = _env_int("LOCAL_TITLE_AI_JSON_RETRY_ATTEMPTS", 1, 0)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": num_predict,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _call_chat(request_payload: dict[str, Any]) -> requests.Response:
        request_kwargs: dict[str, Any] = {
            "json": request_payload,
            "headers": headers,
            "timeout": (connect_timeout, read_timeout),
        }
        if proxy_url:
            request_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        return requests.post(
            endpoint,
            **request_kwargs,
        )

    def _request_json_with_fallback(request_payload: dict[str, Any]) -> dict[str, Any]:
        response = _call_chat(request_payload)
        if response.status_code >= 400:
            first_error_detail = _extract_http_error_detail(response)
            can_retry_without_response_format = (
                request_payload.get("response_format") is not None
                and response.status_code in {400, 404, 405, 415, 422, 500, 501}
                and _is_response_format_compat_error(first_error_detail)
            )

            if can_retry_without_response_format:
                log_event(
                    log,
                    "local_title_ai.response_format_retry",
                    level="info",
                    provider=provider,
                    model=model,
                    status_code=response.status_code,
                )
                fallback_payload = dict(request_payload)
                fallback_payload.pop("response_format", None)
                response = _call_chat(fallback_payload)
                if response.status_code >= 400:
                    second_error_detail = _extract_http_error_detail(response)
                    raise RuntimeError(
                        f"openai_request_failed: {second_error_detail}; "
                        f"initial_error={first_error_detail}"
                    )
            else:
                raise RuntimeError(f"openai_request_failed: {first_error_detail}")

        try:
            return response.json()
        except Exception as exc:
            body_text = _clean_text(getattr(response, "text", ""), limit=320)
            raise RuntimeError(
                f"openai_invalid_json_response: status={response.status_code}; body={body_text or 'empty'}"
            ) from exc

    request_payload = dict(payload)
    last_content = ""
    for parse_attempt in range(json_retry_attempts + 1):
        data = _request_json_with_fallback(request_payload)

        choices = data.get("choices") if isinstance(data, dict) else None
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        message = first_choice.get("message") if isinstance(first_choice, dict) else {}
        content = _extract_message_content_text(message)
        last_content = content or ""

        parsed = _extract_json_payload(content)
        if parsed:
            return parsed

        if parse_attempt >= json_retry_attempts:
            break

        log_event(
            log,
            "local_title_ai.json_retry",
            level="warning",
            provider=provider,
            model=model,
            attempt=parse_attempt + 1,
            max_attempts=json_retry_attempts + 1,
        )

        if content.strip():
            repair_system_prompt = (
                "исправь ответ в строго валидный json-объект. "
                "верни только json без markdown и без пояснений."
            )
            repair_user_prompt = (
                "исправь этот ответ модели в валидный JSON-объект без потери смысла:\n"
                + _clean_text(content, limit=6000)
            )
            request_payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": repair_system_prompt},
                    {"role": "user", "content": repair_user_prompt},
                ],
                "temperature": 0.0,
                "max_tokens": max(num_predict, 220),
                "response_format": {"type": "json_object"},
            }
        else:
            # если контент пустой — делаем повтор исходного запроса с более мягкой температурой
            request_payload = {
                **payload,
                "temperature": 0.0,
                "max_tokens": max(num_predict, 220),
            }

    raise ValueError(f"модель вернула невалидный json: {_clean_text(last_content, limit=240) or 'empty'}")


def _score_by_keyword_profile(
    candidate: dict[str, Any],
    *,
    target_brand: str | None,
    keywords: list[str],
    negative_keywords: list[str],
) -> int:
    title = _normalize_brand_token(candidate.get("title"))
    url = _normalize_brand_token(candidate.get("url"))
    text = f"{title} {url}".strip()
    source = _normalize_brand_token(candidate.get("serp_source"))

    score = 0
    if source == "exact matches" or source == "exact_matches":
        score += 6
    if _candidate_has_brand(candidate, target_brand):
        score += 5
    elif target_brand:
        score -= 8

    for keyword in keywords:
        if not keyword:
            continue
        if " " in keyword:
            if keyword in text:
                score += 8
        else:
            if re.search(rf"\\b{re.escape(keyword)}\\b", text):
                score += 4

    for neg in negative_keywords:
        if not neg:
            continue
        if neg in text:
            score -= 5

    return score


def select_candidates_by_title_ai(
    candidates: list[dict[str, Any]],
    *,
    target_brand: str | None,
    brand_hint: str | None,
    keep_limit: int,
    forced_target_name: str | None = None,
    forced_keywords: list[str] | None = None,
    forced_negative_keywords: list[str] | None = None,
) -> dict[str, Any]:
    enabled = _env_bool("LOCAL_TITLE_AI_ENABLED", True)
    requested_provider = (os.getenv("LOCAL_TITLE_AI_PROVIDER", "openai") or "openai").strip().lower()
    provider = "openai"
    model = (os.getenv("LOCAL_TITLE_AI_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini").strip()
    connect_timeout = _env_float("LOCAL_TITLE_AI_CONNECT_TIMEOUT_SEC", 2.0, 0.2)
    read_timeout = _env_float("LOCAL_TITLE_AI_READ_TIMEOUT_SEC", 25.0, 1.0)
    num_ctx = _env_int("LOCAL_TITLE_AI_NUM_CTX", 2048, 512)
    num_predict = _env_int("LOCAL_TITLE_AI_NUM_PREDICT", 120, 32)
    temperature = _env_float("LOCAL_TITLE_AI_TEMPERATURE", 0.1, 0.0)
    max_candidates = _env_int("LOCAL_TITLE_AI_MAX_CANDIDATES", 220, 20)
    keyword_chunk_size = _env_int("LOCAL_TITLE_AI_KEYWORD_CHUNK_SIZE", 80, 20)
    keyword_max_chunks = _env_int("LOCAL_TITLE_AI_KEYWORD_MAX_CHUNKS", 4, 1)
    keyword_limit = _env_int("LOCAL_TITLE_AI_KEYWORD_LIMIT", 10, 3)
    shortlist_limit_cfg = _env_int("LOCAL_TITLE_AI_SHORTLIST_LIMIT", 60, 10)
    two_stage_enabled = _env_bool("LOCAL_TITLE_AI_TWO_STAGE_ENABLED", True)
    openai_base_url = (
        os.getenv("LOCAL_TITLE_AI_OPENAI_BASE_URL", os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
        or "https://api.openai.com/v1"
    ).strip().rstrip("/")
    openai_api_key = (
        os.getenv("LOCAL_TITLE_AI_OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("CHATGPT_API_KEY")
        or os.getenv("CHATGPT_TOKEN")
        or ""
    ).strip()
    openai_proxy_url = (
        os.getenv("LOCAL_TITLE_AI_OPENAI_PROXY_URL")
        or os.getenv("OPENAI_PROXY_URL")
        or ""
    ).strip()

    endpoint = f"{openai_base_url}/chat/completions"
    endpoint_api_key = openai_api_key
    if requested_provider != "openai":
        log_event(
            log,
            "local_title_ai.provider_forced_openai",
            level="warning",
            requested_provider=requested_provider,
        )

    if not candidates:
        return {
            "ok": True,
            "mode": "empty",
            "model": f"{provider}:{model}",
            "selected_indexes": [],
            "reason_by_index": {},
            "target_name": None,
            "error": None,
            "used_candidates": 0,
            "ignored_candidates": 0,
        }

    used_candidates = min(len(candidates), max_candidates)
    working = candidates[:used_candidates]
    ignored_candidates = max(0, len(candidates) - used_candidates)
    keep_limit = max(1, min(keep_limit, used_candidates))

    if not enabled:
        selected = list(range(keep_limit))
        return {
            "ok": True,
            "mode": "disabled",
            "model": f"{provider}:{model}",
            "selected_indexes": selected,
            "reason_by_index": {
                idx: "локальный ai-gate отключен"
                for idx in range(used_candidates)
                if idx not in set(selected)
            },
            "target_name": None,
            "error": None,
            "used_candidates": used_candidates,
            "ignored_candidates": ignored_candidates,
        }

    target_brand_text = _clean_text(target_brand or "", limit=80) or "unknown"
    brand_hint_text = _clean_text(brand_hint or "", limit=80) or "none"
    selected_indexes: list[int] = []
    reason_by_index: dict[int, str] = {}
    mode = "ai_two_stage"
    target_name: str | None = None
    error_text: str | None = None
    shortlist_size = 0
    shortlist_indexes: list[int] = []
    model_keywords: list[str] = []
    negative_keywords: list[str] = []
    forced_target_name_clean = _clean_text(forced_target_name, limit=180) or None
    forced_keywords_norm = _normalize_keyword_list(
        forced_keywords,
        limit=max(keyword_limit * 3, keyword_limit),
    )
    forced_negative_keywords_norm = _normalize_keyword_list(
        forced_negative_keywords,
        limit=max(3, keyword_limit),
    )
    has_forced_profile = bool(forced_target_name_clean or forced_keywords_norm)

    try:
        # этап 1: построение профиля модели по ключевым словам
        fallback_profile = _build_fallback_keyword_profile(
            working,
            keyword_limit=keyword_limit,
            target_brand=target_brand,
        )
        if has_forced_profile:
            mode = "ai_forced_profile_shortlist_only"
            target_name = forced_target_name_clean
            model_keywords = list(forced_keywords_norm)
            if not model_keywords:
                model_keywords = _build_keywords_from_target_name(
                    forced_target_name_clean,
                    limit=keyword_limit,
                )
            negative_keywords = list(forced_negative_keywords_norm)
        elif two_stage_enabled:
            keyword_votes: Counter[str] = Counter()
            negative_votes: Counter[str] = Counter()
            target_name_votes: Counter[str] = Counter()

            for chunk_idx in range(keyword_max_chunks):
                start = chunk_idx * keyword_chunk_size
                if start >= used_candidates:
                    break
                chunk = working[start : start + keyword_chunk_size]
                if not chunk:
                    continue

                chunk_lines: list[str] = []
                for local_idx, item in enumerate(chunk, start=1):
                    title = _clean_text(item.get("title") or "", limit=120)
                    site = _clean_text(item.get("site") or "", limit=32)
                    src = _clean_text(item.get("serp_source") or "", limit=24)
                    chunk_lines.append(f"{local_idx}. title={title} | site={site} | source={src}")

                keyword_system_prompt = (
                    "ты выделяешь признаки одной модели сумки по заголовкам объявлений. "
                    "режим recall-first: не отбрасывай кандидата только из-за краткого бренда вроде lv. "
                    "если заголовки на французском/итальянском/испанском/немецком, сначала мысленно переведи в английские эквиваленты. "
                    "объединяй синонимы модели между языками в один профиль. "
                    "верни только json."
                )
                keyword_user_prompt = (
                    f"brand_hint: {brand_hint_text}\n"
                    f"target_brand: {target_brand_text}\n"
                    "задача: выдели короткие ключевые слова/фразы модели (по фасону/линейке), "
                    "не заужай по цвету и мелким деталям.\n"
                    "brand alias: считай lv, louis vuitton, louis-vuitton эквивалентными, если контекст про luxury wallet/bag.\n"
                    "важно: названия могут быть не на английском; учитывай переводы и вариации написания.\n"
                    "target_name верни в английской нормализованной форме.\n"
                    "верни json формата:\n"
                    "{\n"
                    '  "target_name": "строка или null",\n'
                    '  "keywords": ["слово или фраза"],\n'
                    '  "negative_keywords": ["слово или фраза"]\n'
                    "}\n"
                    "кандидаты:\n"
                    + "\n".join(chunk_lines)
                )
                try:
                    parsed_chunk = _post_llm_json(
                        provider=provider,
                        endpoint=endpoint,
                        model=model,
                        system_prompt=keyword_system_prompt,
                        user_prompt=keyword_user_prompt,
                        connect_timeout=connect_timeout,
                        read_timeout=read_timeout,
                    num_ctx=num_ctx,
                    num_predict=max(80, num_predict),
                    temperature=temperature,
                    api_key=endpoint_api_key,
                    proxy_url=openai_proxy_url or None,
                )
                    chunk_keywords = _normalize_keyword_list(
                        parsed_chunk.get("keywords") or parsed_chunk.get("model_keywords"),
                        limit=keyword_limit * 3,
                    )
                    chunk_negative = _normalize_keyword_list(
                        parsed_chunk.get("negative_keywords"),
                        limit=max(3, keyword_limit),
                    )
                    chunk_target_name = _clean_text(
                        parsed_chunk.get("target_name")
                        or parsed_chunk.get("model_name")
                        or parsed_chunk.get("bag_name")
                        or parsed_chunk.get("profile_name"),
                        limit=180,
                    )
                    keyword_votes.update(chunk_keywords)
                    negative_votes.update(chunk_negative)
                    if chunk_target_name:
                        target_name_votes[chunk_target_name] += 1
                except Exception as chunk_exc:
                    log_exception(
                        log,
                        "local_title_ai.keyword_chunk_error",
                        chunk_exc,
                        level="warning",
                        model=model,
                        chunk_index=chunk_idx,
                    )

            if keyword_votes:
                model_keywords = [k for k, _ in keyword_votes.most_common(keyword_limit)]
            else:
                model_keywords = list(fallback_profile.get("keywords") or [])

            if negative_votes:
                negative_keywords = [k for k, _ in negative_votes.most_common(max(3, keyword_limit // 2))]
            else:
                negative_keywords = []

            if target_name_votes:
                target_name = target_name_votes.most_common(1)[0][0]
            else:
                target_name = _clean_text(fallback_profile.get("target_name"), limit=180) or None
        else:
            mode = "ai_one_stage_shortlist_only"
            model_keywords = list(fallback_profile.get("keywords") or [])
            negative_keywords = list(fallback_profile.get("negative_keywords") or [])
            target_name = _clean_text(fallback_profile.get("target_name"), limit=180) or None

        if not model_keywords:
            model_keywords = list(fallback_profile.get("keywords") or [])

        if not target_name:
            target_name = _build_fallback_target_name(
                working,
                selected_indexes=None,
                target_brand=target_brand,
                keyword_hints=model_keywords,
                max_tokens=4,
            )

        # этап 2: shortlist по профилю модели
        scored: list[tuple[int, int, int]] = []
        for idx, item in enumerate(working):
            score = _score_by_keyword_profile(
                item,
                target_brand=target_brand,
                keywords=model_keywords,
                negative_keywords=negative_keywords,
            )
            exact_priority = 0 if (item.get("serp_source") or "") == "exact_matches" else 1
            scored.append((score, exact_priority, idx))

        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        shortlist_size = min(used_candidates, max(keep_limit, shortlist_limit_cfg))
        shortlist_indexes = [idx for _, _, idx in scored[:shortlist_size]]
        shortlist_set = set(shortlist_indexes)

        for idx in range(used_candidates):
            if idx not in shortlist_set:
                reason_by_index[idx] = "не попал в shortlist по ключевым словам модели"

        if not shortlist_indexes:
            mode = "fallback_empty_shortlist"
            selected_indexes, fallback_reason = _fallback_select_indexes(
                working,
                keep_limit=keep_limit,
                target_brand=target_brand,
            )
            reason_by_index.update(fallback_reason)
        else:
            shortlist_items = [working[idx] for idx in shortlist_indexes]
            final_lines: list[str] = []
            for local_id, original_idx in enumerate(shortlist_indexes, start=1):
                item = working[original_idx]
                title = _clean_text(item.get("title") or "", limit=140)
                site = _clean_text(item.get("site") or "", limit=40)
                src = _clean_text(item.get("serp_source") or "", limit=24)
                final_lines.append(f"{local_id}. title={title} | site={site} | source={src}")

            final_system_prompt = (
                "выбери наиболее вероятные объявления той же модели сумки. "
                "режим recall-first: если сомневаешься, лучше оставить кандидата. "
                "считай эквивалентными названия модели на разных языках. "
                "считай брендовые алиасы эквивалентными: lv == louis vuitton. "
                "сравнивай по смыслу модели, а не по точному совпадению языка заголовка. "
                "верни только json."
            )
            final_user_prompt = (
                f"brand_hint: {brand_hint_text}\n"
                f"target_brand: {target_brand_text}\n"
                f"target_name: {_clean_text(target_name or 'none', limit=120)}\n"
                f"keywords: {', '.join(model_keywords[:keyword_limit])}\n"
                f"negative_keywords: {', '.join(negative_keywords[:max(3, keyword_limit // 2)])}\n"
                f"limit_selected: {keep_limit}\n"
                "правила:\n"
                "1) выбери только id из списка ниже.\n"
                "2) бренд и модель важнее цвета; lv считать эквивалентом louis vuitton.\n"
                "3) если кандидат похож по модели/линейке, лучше включить.\n"
                "4) не придумывай новые id.\n"
                "5) reason_by_id опционален (можно пустой объект).\n"
                "6) target_name обязателен: верни best guess на английском даже при сомнениях.\n"
                "верни json формата:\n"
                "{\n"
                '  "accepted_ids": [1,2,3],\n'
                '  "reason_by_id": {"4":"другая модель"},\n'
                '  "target_name": "обязательная строка на английском"\n'
                "}\n"
                "кандидаты:\n"
                + "\n".join(final_lines)
            )

            try:
                parsed_final = _post_llm_json(
                    provider=provider,
                    endpoint=endpoint,
                    model=model,
                    system_prompt=final_system_prompt,
                    user_prompt=final_user_prompt,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    num_ctx=num_ctx,
                    num_predict=num_predict,
                    temperature=temperature,
                    api_key=endpoint_api_key,
                    proxy_url=openai_proxy_url or None,
                )
                raw_ids = parsed_final.get("accepted_ids")
                if not isinstance(raw_ids, list):
                    raw_ids = parsed_final.get("selected_ids")
                if not isinstance(raw_ids, list):
                    raw_ids = parsed_final.get("ids")
                if not isinstance(raw_ids, list):
                    raw_ids = []
                parsed_target_name = _clean_text(
                    parsed_final.get("target_name")
                    or parsed_final.get("model_name")
                    or parsed_final.get("bag_name")
                    or parsed_final.get("profile_name"),
                    limit=180,
                )
                if parsed_target_name:
                    target_name = parsed_target_name
                accepted_local_ids: list[int] = []
                for raw_id in raw_ids:
                    try:
                        value = int(raw_id)
                    except Exception:
                        continue
                    if 1 <= value <= len(shortlist_items) and value not in accepted_local_ids:
                        accepted_local_ids.append(value)

                mapped_indexes = [shortlist_indexes[item_id - 1] for item_id in accepted_local_ids]
                if target_brand:
                    mapped_indexes = [
                        idx for idx in mapped_indexes if _candidate_has_brand(working[idx], target_brand)
                    ]

                if not mapped_indexes:
                    mode = "fallback_no_ai_ids"
                    selected_indexes = shortlist_indexes[:keep_limit]
                else:
                    selected_indexes = mapped_indexes[:keep_limit]

                raw_reason_by_id = parsed_final.get("reason_by_id")
                if isinstance(raw_reason_by_id, dict):
                    selected_set = set(selected_indexes)
                    for local_id, original_idx in enumerate(shortlist_indexes, start=1):
                        if original_idx in selected_set:
                            continue
                        reason = _clean_text(
                            raw_reason_by_id.get(str(local_id)) or raw_reason_by_id.get(local_id),
                            limit=180,
                        )
                        if reason:
                            reason_by_index[original_idx] = reason
            except Exception as stage2_exc:
                mode = "fallback_error"
                error_text = str(stage2_exc)
                log_exception(
                    log,
                    "local_title_ai.error",
                    stage2_exc,
                    level="warning",
                    provider=provider,
                    model=model,
                    endpoint=endpoint,
                )
                selected_indexes = shortlist_indexes[:keep_limit]
    except Exception as exc:
        mode = "fallback_unexpected"
        error_text = str(exc)
        log_exception(
            log,
            "local_title_ai.error",
            exc,
            level="warning",
            provider=provider,
            model=model,
            endpoint=endpoint,
        )
        selected_indexes, reason_by_index = _fallback_select_indexes(
            working,
            keep_limit=keep_limit,
            target_brand=target_brand,
        )

    selected_set = set(selected_indexes)

    if not target_name:
        target_name = _build_fallback_target_name(
            working,
            selected_indexes=selected_indexes or shortlist_indexes,
            target_brand=target_brand,
            keyword_hints=model_keywords,
            max_tokens=4,
        )

    for idx in range(used_candidates):
        if idx in selected_set:
            continue
        if idx in reason_by_index:
            continue
        reason_by_index[idx] = "не прошёл финальный ai-отбор по модели"

    log_event(
        log,
        "local_title_ai.result",
        level="info",
        mode=mode,
        provider=provider,
        model=model,
        keywords_count=len(model_keywords),
        shortlist_size=shortlist_size,
        used_candidates=used_candidates,
        ignored_candidates=ignored_candidates,
        selected=len(selected_indexes),
        keep_limit=keep_limit,
        has_forced_profile=has_forced_profile,
        has_target_name=bool(target_name),
        has_error=bool(error_text),
    )

    return {
        "ok": True,
        "mode": mode,
        "model": f"{provider}:{model}",
        "selected_indexes": selected_indexes,
        "reason_by_index": reason_by_index,
        "target_name": target_name,
        "error": error_text,
        "used_candidates": used_candidates,
        "ignored_candidates": ignored_candidates,
        "keywords": model_keywords,
        "negative_keywords": negative_keywords,
        "shortlist_size": shortlist_size,
    }

"""
flask маршруты api"""

import os
import json
import time
import uuid
import re
import copy
from collections import Counter
from datetime import datetime
from statistics import median
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED, CancelledError
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from flask import Flask, request, jsonify, send_from_directory, abort

# переменные окружения загружаются в main.py

from config import (
    SERPAPI_KEY,
    CLIP_MODEL_NAME,
    ENABLE_CACHE,
    CACHE_TTL,
    CACHE_DIR,
    ALLOWED_DOMAINS,
    SIMILARITY_THRESHOLD,
    ENABLE_COLOR_CHECK,
    COLOR_SIMILARITY_THRESHOLD,
    ENABLE_PHASH_CHECK,
    PHASH_THRESHOLD,
    MAX_RESULTS_TO_CHECK,
    BATCH_SIZE,
    MAX_PAGES_TO_SCRAPE,
    MAX_PARALLEL_SCRAPERS,
    FILTER_SOLD_ITEMS,
)
from utils.image import (
    save_temp_image,
    load_pil_image_from_path,
    load_images_parallel,
    silhouette_shape_similarity,
)
from utils.price import parse_price_and_currency, to_usd, normalize_currency_code
from utils.domain import BRAND_NAMES, domain_of, is_product_page_url, is_ebay_domain
from utils.report import get_results_dir, build_results_timestamp
from services.clip import (
    init_clip_model,
    get_clip_model,
    get_clip_device,
    get_image_embedding,
    get_dominant_colors,
    get_perceptual_hash,
    compute_image_similarity_batch,
)
from services.serpapi import serpapi_google_lens, extract_results_from_serpapi
from services.cache import get_image_hash, load_from_cache, save_to_cache
from services.local_title_ai import select_candidates_by_title_ai
from services.report_queue import enqueue_report_generation, get_report_task_status
from scrapers import scrape_by_domain

from utils.logger import get_logger, log_event, log_exception, set_context_values, clear_context, get_trace_id

# создаем flask приложение
app = Flask(__name__)

log = get_logger(__name__)

_MOJIBAKE_RE = re.compile(
    r"(?:\u0440\u045f|\u0432\u045a|\u0432\u0459|\u0432\u2020|\u043f\u0451\u040f)"
)
_MOJIBAKE_BAD_CHARS = set(
    "\u0403\u0453\u201a\u2026\u2020\u2021\u20ac\u2030\u0409\u2039\u040a\u040b\u040f\u0452\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u2122\u0459\u203a\u045a\u045b\u045f\u040e\u045e\u0408\xa4\u0490\xa6\xa7\u0401\xa9\u0404\xab\xac\xae\u0407\xb0\xb1\u0406\u0456\u0491\xb5\xb6\xb7\u0451\u2116\u0454\xbb\u0458\u0405\u0455\u0457"
)


def _mojibake_score(text: str) -> int:
    if not text:
        return 0
    pattern_hits = len(_MOJIBAKE_RE.findall(text))
    bad_chars = sum(1 for ch in text if ch in _MOJIBAKE_BAD_CHARS)
    bad_pairs = 0
    for i in range(len(text) - 1):
        if text[i] in {"Р", "С"} and text[i + 1] in _MOJIBAKE_BAD_CHARS:
            bad_pairs += 1
    replacement_chars = text.count("�")
    return pattern_hits * 2 + bad_chars * 3 + bad_pairs * 4 + replacement_chars * 6


def _repair_mojibake(text: str) -> str:
    # восстанавливаем строку после битой перекодировки utf-8/cp1251
    if not text:
        return ""
    src = str(text)
    has_bad = any(ch in _MOJIBAKE_BAD_CHARS for ch in src)
    has_markers = bool(_MOJIBAKE_RE.search(src)) or "�" in src
    if not has_bad and not has_markers:
        return src

    candidates = [src]
    for src_enc in ("cp1251", "cp866", "latin1"):
        try:
            candidates.append(src.encode(src_enc).decode("utf-8"))
        except Exception:
            continue

    best = src
    best_score = _mojibake_score(src)
    for candidate in candidates[1:]:
        score = _mojibake_score(candidate)
        if score < best_score:
            best = candidate
            best_score = score

    # принимаем исправление только если стало заметно лучше
    if best is not src and best_score < _mojibake_score(src):
        return best
    return best


def _strip_visual_markers(text: str) -> str:
    # убираем emoji/маркеры, чтобы логи не ломались в консоли на винде
    markers = [
        "\u2713",
        "\u2717",
        "\u2705",
        "\u26A0",
        "\ufe0f",
        "\u274c",
        "\u2192",
        "\u21b7",
        "\u2139",
        "\U0001F4CA",
        "\U0001F4C4",
        "\U0001F517",
        "\U0001F916",
        "\U0001F5D1",
        "\U0001F9F9",
        "\U0001F4E1",
        "\U0001F4A1",
        "\U0001F4BE",
        "📊",
        "📄",
        "🔗",
        "🤖",
        "🗑",
        "🧹",
        "📡",
        "💡",
        "💾",
        "✓",
        "✗",
        "✅",
        "❌",
        "⚠",
        "→",
        "↷",
        "ℹ",
        "️",
    ]
    out = text
    for marker in markers:
        out = out.replace(marker, "")
    return out


def _api_console_print(*args, **kwargs):
    # прокидываем legacy print() в структурированный лог
    try:
        sep = kwargs.get("sep", " ")
        msg = sep.join("" if a is None else str(a) for a in args)
    except Exception:
        msg = " ".join(str(a) for a in args)

    msg = _repair_mojibake(msg)
    msg = msg.replace("\r", " ").replace("\n", " ")
    msg = " ".join(msg.split()).strip()
    if not msg:
        return

    # пропускаем декоративные разделители
    if len(msg) >= 20 and set(msg) <= {"=", "-", "_", "─"}:
        return

    msg = _strip_visual_markers(msg)
    msg = " ".join(msg.split()).strip()
    if not msg:
        return

    lower = msg.lower()
    level = "info"
    if (
        "ошиб" in lower
        or "error" in lower
        or "exception" in lower
        or "traceback" in lower
    ):
        level = "error"
    elif (
        "вниман" in lower
        or "warning" in lower
        or "blocked" in lower
        or "не удалось" in lower
        or "таймаут" in lower
        or "timeout" in lower
    ):
        level = "warning"

    event = "api.console"
    if "serpapi" in lower:
        event = "api.serpapi"
    elif "кэш" in lower or "cache" in lower:
        event = "api.cache"
    elif "фильтр" in lower or "фильтрац" in lower:
        event = "api.filter"
    elif "парсинг" in lower or "scrape" in lower:
        event = "api.scrape"
    elif "embedding" in lower or "clip" in lower:
        event = "api.clip"
    elif "время" in lower or "сек" in lower:
        event = "api.timing"

    log_event(log, event, level=level, msg=msg)

print = _api_console_print  # type: ignore[assignment], то есть игнорируем ошибку несовместимости


@app.before_request
def _before_request_logging():
    trace_id = f"r{uuid.uuid4().hex[:8]}"
    set_context_values(trace_id=trace_id, domain="api", url=request.path)
    log_event(log, "api.request.start", level="info", method=request.method, path=request.path)


@app.after_request
def _after_request_logging(response):
    try:
        log_event(log, "api.request.end", level="info", status_code=getattr(response, "status_code", None))
    except Exception:
        pass
    return response


@app.teardown_request
def _teardown_request_logging(exc):
    try:
        if exc is not None:
            log_exception(log, "api.request.exception", exc, level="error")
    finally:
        clear_context()

_RESULTS_FILE_RE = re.compile(r"^(?:report|result)_[A-Za-z0-9._-]+\.(?:json|html|pdf|docx)$")


def _project_root_dir() -> str:
    # возвращает корень проекта относительно текущего файла
    return os.path.dirname(os.path.dirname(__file__))


def _build_report_url_path(report_filename: str | None) -> str | None:
    # строит относительный url отчёта для клиента api
    safe_name = os.path.basename(str(report_filename or "").strip())
    if not safe_name:
        return None
    return f"/results/{safe_name}"


def _cleanup_temp_path(temp_path: str | None) -> None:
    # удаляет временный файл изображения и его пустую папку
    if not temp_path:
        return

    try:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    except Exception:
        pass

    try:
        temp_dir = os.path.dirname(temp_path)
        if temp_dir and os.path.isdir(temp_dir) and not os.listdir(temp_dir):
            os.rmdir(temp_dir)
    except Exception:
        pass


def _read_request_text_value(*names: str) -> str | None:
    # ищет первое непустое текстовое значение в form и query string
    for source in (request.form, request.args):
        for name in names:
            value = source.get(name)
            if value is None:
                continue
            text = _repair_mojibake(str(value)).replace("\xa0", " ").strip()
            if text:
                return text
    return None


def _normalize_currency_words(text: str | None) -> str:
    # приводит русские сокращения валют к iso-кодам
    if not text:
        return ""

    normalized = _repair_mojibake(str(text)).replace("\xa0", " ").strip()
    normalized = re.sub(r"(?iu)\bруб(?:\.|ля|лей|ль)?\b", "RUB", normalized)
    normalized = re.sub(r"(?iu)\bдол(?:л(?:ар(?:ов|а)?)?)?\.?\b", "USD", normalized)
    return normalized.strip()


def _extract_avito_price_context() -> dict:
    # читает цену avito из запроса и готовит конвертацию в usd
    avito_price_text = _read_request_text_value("avito_price", "avito_price_text", "avito_price_rub")
    avito_currency_text = _read_request_text_value("avito_currency")

    context = {
        "avito_price_original": None,
        "avito_currency_original": None,
        "avito_price_usd": None,
        "avito_price_error": None,
    }

    if not avito_price_text and not avito_currency_text:
        return context

    normalized_price_text = _normalize_currency_words(avito_price_text)
    currency_hint = normalize_currency_code(_normalize_currency_words(avito_currency_text))

    parsed_price, parsed_currency = parse_price_and_currency(normalized_price_text)
    if parsed_price is None and normalized_price_text:
        fallback_currency = currency_hint or "RUB"
        parsed_price, parsed_currency = parse_price_and_currency(f"{fallback_currency} {normalized_price_text}")

    if parsed_price is None:
        context["avito_price_error"] = "не удалось распознать цену avito"
        return context

    final_currency = normalize_currency_code(parsed_currency or currency_hint or "RUB") or "RUB"
    context["avito_price_original"] = round(float(parsed_price), 2)
    context["avito_currency_original"] = final_currency

    price_usd = to_usd(float(parsed_price), final_currency)
    if price_usd is None:
        context["avito_price_error"] = f"не удалось конвертировать {parsed_price} {final_currency} в USD"
        return context

    context["avito_price_usd"] = round(float(price_usd), 2)
    return context


def _strip_transient_response_fields(payload: dict | None) -> dict:
    # удаляет из payload поля, которые не должны попадать в кэш
    cleaned = copy.deepcopy(payload or {})
    for key in (
        "saved_to",
        "report_file",
        "report_filename",
        "report_format",
        "report_file_url",
        "report_url",
        "report_task_id",
        "report_status",
        "report_ready",
        "report_error",
        "avito_price_original",
        "avito_currency_original",
        "avito_price_usd",
        "avito_price_error",
        "cached",
        "cache_age",
    ):
        cleaned.pop(key, None)
    return cleaned


def _apply_request_context_to_payload(base_payload: dict, avito_context: dict) -> dict:
    # накладывает на базовый ответ request-зависимые поля
    payload = _strip_transient_response_fields(base_payload)

    if base_payload.get("cached"):
        payload["cached"] = True
    if base_payload.get("cache_age") is not None:
        payload["cache_age"] = base_payload.get("cache_age")

    payload["avito_price_original"] = avito_context.get("avito_price_original")
    payload["avito_currency_original"] = avito_context.get("avito_currency_original")
    payload["avito_price_usd"] = avito_context.get("avito_price_usd")
    payload["avito_price_error"] = avito_context.get("avito_price_error")
    return payload


def _persist_response_artifacts(response_payload: dict, artifact_ts: str, generated_at: datetime) -> dict:
    # сохраняет json-слепок ответа и ставит отчет в очередь
    results_dir = get_results_dir(_project_root_dir())
    report_filename = f"report_{artifact_ts}.html"
    report_path = os.path.join(results_dir, report_filename)
    report_url = _build_report_url_path(report_filename)

    queue_state = enqueue_report_generation(
        response_payload,
        artifact_ts=artifact_ts,
        generated_at=generated_at,
        project_root=_project_root_dir(),
    )

    if queue_state.get("task_id"):
        response_payload["report_file"] = report_path
        response_payload["report_filename"] = report_filename
        response_payload["report_url"] = report_url
        response_payload["report_file_url"] = report_url
    else:
        response_payload["report_file"] = None
        response_payload["report_filename"] = None
        response_payload["report_url"] = None
        response_payload["report_file_url"] = None
    response_payload["report_format"] = "html"
    response_payload["report_task_id"] = queue_state.get("task_id")
    response_payload["report_status"] = queue_state.get("status")
    response_payload["report_ready"] = False
    response_payload["report_error"] = queue_state.get("error")

    out_path = os.path.join(results_dir, f"result_{artifact_ts}.json")

    if not response_payload:
        print("предупреждение: response_payload пустой, нечего сохранять")
        return response_payload

    print(f"сохранение результата в файл: {out_path}")
    print(f"   - статус: {response_payload.get('status')}")
    print(f"   - количество items: {len(response_payload.get('items', []))}")
    print(f"   - медиана цен: {response_payload.get('median_price_usd')}")

    json_str = json.dumps(response_payload, ensure_ascii=False, indent=2)
    if not json_str:
        raise ValueError("json сериализация вернула пустую строку")

    print(f"   - размер JSON: {len(json_str)} символов")
    with open(out_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(json_str)

    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        print(f"файл успешно сохранен: {out_path} ({os.path.getsize(out_path)} байт)")
        response_payload["saved_to"] = out_path
        return response_payload

    raise IOError(f"файл создан, но пустой или не записался: {out_path}")


def _get_filter_reason_text(
    reason_code: str,
    clip_sim: float,
    color_sim: float = None,
    phash_dist: int = None,
    final_sim: float | None = None,
    similarity_threshold: float | None = None,
    color_threshold: float | None = None,
    phash_threshold: int | None = None,
) -> str:
    # формирует человекочитаемую причину фильтрации
    similarity_threshold_value = (
        float(similarity_threshold)
        if similarity_threshold is not None
        else float(SIMILARITY_THRESHOLD)
    )
    color_threshold_value = (
        float(color_threshold)
        if color_threshold is not None
        else float(COLOR_SIMILARITY_THRESHOLD)
    )
    phash_threshold_value = (
        int(phash_threshold)
        if phash_threshold is not None
        else int(PHASH_THRESHOLD)
    )
    if reason_code == "clip_below_threshold":
        final_value = float(final_sim) if final_sim is not None else float(clip_sim)
        return (
            f"итоговая схожесть ниже порога "
            f"(итоговая {final_value:.1%}, CLIP {clip_sim:.1%}, порог {similarity_threshold_value:.0%})"
        )
    elif reason_code == "phash_penalty_final_below":
        phash_str = str(phash_dist) if phash_dist is not None else "n/a"
        final_value = float(final_sim) if final_sim is not None else float(clip_sim)
        return (
            f"схожесть после штрафа pHash ниже порога "
            f"(итоговая {final_value:.1%}, CLIP {clip_sim:.1%}, "
            f"pHash {phash_str}, порог {similarity_threshold_value:.0%})"
        )
    elif reason_code == "color_mismatch":
        color_str = f"{color_sim:.1%}" if color_sim is not None else "n/a"
        return f"не совпал цвет ({color_str}, порог {color_threshold_value:.0%})"
    elif reason_code == "phash_mismatch":
        phash_str = str(phash_dist) if phash_dist is not None else "n/a"
        return f"PHASH не прошел (distance {phash_str}, порог {phash_threshold_value})"
    elif reason_code == "catalog_page":
        return "каталог/категория (не страница товара)"
    elif reason_code == "unsupported_domain":
        return "неподдерживаемый домен"
    elif reason_code == "shape_gate_mismatch":
        return "не совпал силуэт модели"
    elif reason_code == "brand_gate_mismatch":
        return "не совпал бренд"
    elif reason_code == "sold_item":
        return "товар продан"
    else:
        return f"отфильтровано ({reason_code})"


def _normalize_status_text(status: str | None) -> str | None:
    if status is None:
        return None
    text = _repair_mojibake(str(status)).strip()
    if not text:
        return None

    low = text.lower()
    blocked_markers = ("blocked", "proxy_auth_required", "access denied", "captcha")
    sold_markers = ("sold out", "sold", "out of stock", "unavailable", "ended", "нет в наличии", "продано")
    available_markers = ("available", "in stock", "buy now", "add to cart", "в продаже", "в наличии")

    if any(marker in low for marker in blocked_markers):
        return "Blocked"
    if any(marker in low for marker in sold_markers):
        return "Sold"
    if any(marker in low for marker in available_markers):
        return "Available"
    if "catalog" in low or "каталог" in low:
        return "Catalog"
    if low in {"unknown", "неизвестно", "none", "null", "n/a"}:
        return "Unknown"
    return text


def _is_sold_status(status: str | None) -> bool:
    # проверяет, что статус указывает на проданный товар
    normalized = _normalize_status_text(status)
    if not normalized:
        return False
    return normalized.lower() == "sold"


def _is_available_status(status: str | None) -> bool:
    # проверяет, что статус указывает на товар в продаже
    normalized = _normalize_status_text(status)
    if not normalized:
        return False
    return normalized.lower() == "available"


def _is_catalog_status(status: str | None) -> bool:
    # проверяет, что статус указывает на каталог
    normalized = _normalize_status_text(status)
    if not normalized:
        return False
    s = normalized.lower().strip()
    return s == "catalog"


def _is_blocked_status(status: str | None) -> bool:
    normalized = _normalize_status_text(status)
    if not normalized:
        return False
    return normalized.lower().strip() == "blocked"

_RANK_WEIGHTS = {
    1: 1.00,
    2: 0.70,
    3: 0.65,
}

_RANK_DOMAINS = {
    1: {"rebag.com", "ebay.com", "poshmark.com", "vestiairecollective.com"},
    2: {"theluxurycloset.com", "jolicloset.com", "yoogiscloset.com", "therealreal.com"},
    3: {
        "fashionphile.com",
        "celebrityowned.com",
        "aretrotale.com",
        "dallasdesignerhandbags.com",
        "popchill.com",
        "designerexchange.com",
        "annsfabulousfinds.com",
    },
}

_RANK_SITE_KEYS = {
    1: {"rebag", "ebay", "poshmark", "vestiairecollective"},
    2: {"theluxurycloset", "jolicloset", "yoogiscloset", "therealreal"},
    3: {
        "fashionphile",
        "celebrityowned",
        "aretrotale",
        "dallasdesignerhandbags",
        "popchill",
        "designerexchange",
        "annsfabulousfinds",
    },
}


def _site_or_domain_rank(item: dict) -> int | None:
    """
    определяет ранг источника для алгоритма цены

    использует и `site`, и домен URL, чтобы корректно покрывать
    варианты вроде `shop.rebag.com` и региональные домены eBay
"""
    site_key = _repair_mojibake(str(item.get("site") or "")).strip().lower()
    domain = domain_of(item.get("url") or "")
    domain = (domain or "").strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]

    if site_key == "ebay" or domain.startswith("ebay."):
        return 1

    for rank, site_keys in _RANK_SITE_KEYS.items():
        if site_key in site_keys:
            return rank

    for rank, domains in _RANK_DOMAINS.items():
        for base_domain in domains:
            if domain == base_domain or domain.endswith(f".{base_domain}"):
                return rank
    return None


def _compute_rank_price(prices: list[float]) -> dict | None:
    """
    считает цену внутри ранга по правилам из документа:
    медиана -> коридор [0.7x, 1.2x] -> clamp -> среднее clamp-цен
"""
    if not prices:
        return None
    med = median(prices)
    floor = med * 0.7
    ceiling = med * 1.2

    clamped_prices: list[float] = []
    clamped_low = 0
    clamped_high = 0
    for price in prices:
        adj_price = float(price)
        if adj_price < floor:
            adj_price = floor
            clamped_low += 1
        elif adj_price > ceiling:
            adj_price = ceiling
            clamped_high += 1
        clamped_prices.append(adj_price)

    rank_price = sum(clamped_prices) / len(clamped_prices)
    return {
        "count": len(prices),
        "median_usd": round(med, 2),
        "floor_usd": round(floor, 2),
        "ceiling_usd": round(ceiling, 2),
        "price_usd": round(rank_price, 2),
        "clamped_low_count": clamped_low,
        "clamped_high_count": clamped_high,
    }


def _compute_market_price_for_status(items: list[dict], status_kind: str) -> dict:
    """
    считает рыночную цену отдельно для группы статуса (продано/в наличии)
    по ранговому алгоритму из документа средней цены
"""
    normalized_kind = (status_kind or "").strip().lower()
    if normalized_kind not in {"sold", "available"}:
        return {
            "status_kind": normalized_kind or "unknown",
            "market_price_usd": None,
            "raw_median_usd": None,
            "count_items": 0,
            "count_priced": 0,
            "count_priced_ranked": 0,
            "count_priced_unranked": 0,
            "rank3_used": False,
            "rank3_guard_triggered": False,
            "rank3_guard_reason": "invalid_status_kind",
            "rank_weights": {"rank1": 0.0, "rank2": 0.0, "rank3": 0.0},
            "ranks": {},
        }

    ranked_prices: dict[int, list[float]] = {1: [], 2: [], 3: []}
    unranked_prices: list[float] = []
    raw_prices: list[float] = []
    matched_items = 0

    for item in items:
        status = item.get("status")
        if normalized_kind == "sold":
            if not _is_sold_status(status):
                continue
        elif not _is_available_status(status):
            continue

        matched_items += 1
        price = item.get("price")
        if price is None:
            continue
        try:
            price_value = float(price)
        except Exception:
            continue
        if price_value <= 0:
            continue

        raw_prices.append(price_value)
        rank = _site_or_domain_rank(item)
        if rank in ranked_prices:
            ranked_prices[rank].append(price_value)
        else:
            unranked_prices.append(price_value)

    rank_results: dict[int, dict] = {}
    for rank in (1, 2, 3):
        rank_calc = _compute_rank_price(ranked_prices[rank])
        if rank_calc is not None:
            rank_results[rank] = rank_calc

    price_rank1 = rank_results.get(1, {}).get("price_usd")
    price_rank2 = rank_results.get(2, {}).get("price_usd")
    price_rank3 = rank_results.get(3, {}).get("price_usd")

    rank3_used = False
    rank3_guard_triggered = False
    rank3_guard_reason = None
    anchor = price_rank1 if price_rank1 is not None else price_rank2

    if price_rank3 is not None:
        if anchor is None:
            rank3_used = True
            rank3_guard_reason = "no_anchor"
        else:
            low_guard = 0.4 * float(anchor)
            high_guard = 1.6 * float(anchor)
            if price_rank3 < low_guard:
                rank3_guard_triggered = True
                rank3_guard_reason = "rank3_below_anchor_guard"
            elif price_rank3 > high_guard:
                rank3_guard_triggered = True
                rank3_guard_reason = "rank3_above_anchor_guard"
            else:
                rank3_used = True
                rank3_guard_reason = "within_anchor_guard"

    n1 = len(ranked_prices[1])
    n2 = len(ranked_prices[2])
    n3 = len(ranked_prices[3])

    if n1 == 1:
        n1_eff = 2
    elif n1 == 2:
        n1_eff = 3
    else:
        n1_eff = n1

    w1 = (n1_eff * _RANK_WEIGHTS[1]) if price_rank1 is not None else 0.0
    w2 = (n2 * _RANK_WEIGHTS[2]) if price_rank2 is not None else 0.0
    w3 = (n3 * _RANK_WEIGHTS[3]) if (price_rank3 is not None and rank3_used) else 0.0
    w_sum = w1 + w2 + w3

    market_price = None
    if w_sum > 0:
        weighted_sum = (
            (float(price_rank1) * w1 if price_rank1 is not None else 0.0)
            + (float(price_rank2) * w2 if price_rank2 is not None else 0.0)
            + (float(price_rank3) * w3 if price_rank3 is not None else 0.0)
        )
        market_price = round(weighted_sum / w_sum, 2)

    raw_median = round(median(raw_prices), 2) if raw_prices else None
    priced_ranked_count = n1 + n2 + n3

    return {
        "status_kind": normalized_kind,
        "market_price_usd": market_price,
        "raw_median_usd": raw_median,
        "count_items": matched_items,
        "count_priced": len(raw_prices),
        "count_priced_ranked": priced_ranked_count,
        "count_priced_unranked": len(unranked_prices),
        "rank3_used": rank3_used,
        "rank3_guard_triggered": rank3_guard_triggered,
        "rank3_guard_reason": rank3_guard_reason,
        "rank_weights": {
            "rank1": round(w1, 3),
            "rank2": round(w2, 3),
            "rank3": round(w3, 3),
        },
        "ranks": {
            "rank1": rank_results.get(1),
            "rank2": rank_results.get(2),
            "rank3": rank_results.get(3),
        },
    }

_BRAND_EXTRA_ALIASES = {
    "christian dior": "dior",
    "louis vuitton": "louis vuitton",
    "louisvuitton": "louis vuitton",
    "lv": "louis vuitton",
    "l v": "louis vuitton",
    "saint laurent": "saint laurent",
    "ysl": "saint laurent",
    "bottega veneta": "bottega veneta",
    "bottegaveneta": "bottega veneta",
    "dolce gabbana": "dolce gabbana",
    "d and g": "dolce gabbana",
    "d g": "dolce gabbana",
    "miu miu": "miumiu",
}


def _normalize_brand_token(text: str | None) -> str:
    if not text:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()
    return " ".join(normalized.split())


def _build_brand_alias_pairs() -> list[tuple[str, str]]:
    alias_to_brand = {}

    for raw_brand in BRAND_NAMES:
        canonical = _normalize_brand_token(raw_brand.replace("-", " "))
        if not canonical:
            continue
        candidates = {
            raw_brand,
            raw_brand.replace("-", " "),
            raw_brand.replace("-", ""),
            canonical,
            canonical.replace(" ", ""),
            canonical.replace(" ", "-"),
        }
        for alias in candidates:
            normalized_alias = _normalize_brand_token(alias)
            if normalized_alias:
                alias_to_brand[normalized_alias] = canonical

    for raw_alias, raw_brand in _BRAND_EXTRA_ALIASES.items():
        normalized_alias = _normalize_brand_token(raw_alias)
        normalized_brand = _normalize_brand_token(raw_brand)
        if normalized_alias and normalized_brand:
            alias_to_brand[normalized_alias] = normalized_brand

    return sorted(alias_to_brand.items(), key=lambda kv: len(kv[0]), reverse=True)

_BRAND_ALIAS_PAIRS = _build_brand_alias_pairs()


def _detect_brand_in_text(text: str | None) -> str | None:
    normalized = _normalize_brand_token(text)
    if not normalized:
        return None

    padded = f" {normalized} "
    for alias, canonical in _BRAND_ALIAS_PAIRS:
        if f" {alias} " in padded:
            return canonical
    return None


def _detect_brand_in_result(item: dict) -> str | None:
    if not isinstance(item, dict):
        return None
    for field_name in ("title", "url"):
        brand = _detect_brand_in_text(item.get(field_name))
        if brand:
            return brand
    return None


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name, str(default)) or "").strip()
    try:
        return float(raw)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    fallback = "1" if default else "0"
    raw = (os.getenv(name, fallback) or fallback).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _extract_product_token_for_dedup(domain: str, path: str) -> str:
    path_lc = (path or "").lower()
    if domain.endswith("vestiairecollective.com"):
        m = re.search(r"-(\d+)\.shtml$", path_lc)
        if m:
            return f"vestiaire:{m.group(1)}"
    if domain.endswith("popchill.com"):
        m = re.search(r"/product/(\d+)", path_lc)
        if m:
            return f"popchill:{m.group(1)}"
    if domain.endswith("poshmark.com"):
        m = re.search(r"/listing/[^/?#]*-([0-9a-f]{24})(?:/|$)", path_lc)
        if m:
            return f"poshmark:{m.group(1)}"
    if domain.startswith("ebay.") or domain.endswith(".ebay.com"):
        m = re.search(r"/itm/(\d+)", path_lc)
        if m:
            return f"ebay:{m.group(1)}"
    if domain.endswith("rebag.com"):
        m = re.search(r"(\d{6,})/?$", path_lc)
        if m:
            return f"rebag:{m.group(1)}"
    if domain.endswith("therealreal.com"):
        slug = path_lc.rstrip("/").split("/")[-1]
        if slug:
            return f"therealreal:{slug}"
    if domain.endswith("jolicloset.com"):
        # большинство карточек jolicloset содержит стабильный числовой id в хвосте slug
        m = re.search(r"--(\d+)(?:/|$)", path_lc)
        if m:
            return f"jolicloset:{m.group(1)}"
    return ""


def _canonical_candidate_key(url: str) -> str:
    try:
        parts = urlsplit((url or "").strip())
    except Exception:
        return (url or "").strip()

    if not parts.netloc:
        return (url or "").strip()

    domain = (domain_of(url) or parts.netloc or "").strip().lower()
    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    # locale segment не влияет на ID товара у части маркетплейсов
    if (
        domain.endswith("popchill.com")
        or domain.endswith("theluxurycloset.com")
        or domain.endswith("vestiairecollective.com")
    ):
        path = re.sub(r"^/[a-z]{2}(?:-[a-z]{2})?/", "/", path, flags=re.IGNORECASE)

    product_token = _extract_product_token_for_dedup(domain, path)
    if product_token:
        return f"{domain}|{product_token}"

    filtered_qs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        k = key.strip().lower()
        if not k:
            continue
        if k.startswith("utm_") or k in {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src"}:
            continue
        filtered_qs.append((k, value))
    filtered_qs.sort(key=lambda pair: (pair[0], pair[1]))
    normalized_query = urlencode(filtered_qs, doseq=True)
    return urlunsplit(("https", domain, path or "/", normalized_query, ""))


def _dedupe_candidates_before_scrape(candidates: list[dict]) -> tuple[list[dict], int]:
    seen_keys = set()
    deduped = []
    dropped = 0
    for item in candidates:
        url = str(item.get("url") or "").strip()
        if not url:
            deduped.append(item)
            continue
        dedup_key = _canonical_candidate_key(url)
        if dedup_key in seen_keys:
            dropped += 1
            continue
        seen_keys.add(dedup_key)
        deduped.append(item)
    return deduped, dropped


def _infer_target_brand(raw_results: list[dict], brand_hint: str | None) -> tuple[str | None, dict]:
    infer_top_k = max(3, int(os.getenv("SERPAPI_BRAND_GATE_INFER_TOP_K", "20")))
    min_count = max(1, int(os.getenv("SERPAPI_BRAND_GATE_MIN_COUNT", "3")))
    min_ratio = min(1.0, max(0.0, _env_float("SERPAPI_BRAND_GATE_MIN_RATIO", 0.55)))

    meta = {
        "mode": "disabled",
        "top_k": infer_top_k,
        "min_count": min_count,
        "min_ratio": min_ratio,
        "known_brand_hits": 0,
        "counts": {},
        "selected_brand": None,
    }

    hint_brand = _detect_brand_in_text(brand_hint)
    if hint_brand:
        meta["mode"] = "request_hint"
        meta["selected_brand"] = hint_brand
        return hint_brand, meta

    exact_results = [r for r in raw_results if (r.get("serp_source") or "") == "exact_matches"]
    other_results = [r for r in raw_results if (r.get("serp_source") or "") != "exact_matches"]
    ranked_sample = (exact_results + other_results)[:infer_top_k]

    counts = {}
    known_hits = 0
    for item in ranked_sample:
        brand = _detect_brand_in_result(item)
        if not brand:
            continue
        known_hits += 1
        counts[brand] = counts.get(brand, 0) + 1

    meta["counts"] = counts
    meta["known_brand_hits"] = known_hits
    if not counts:
        meta["mode"] = "auto_no_brand_found"
        return None, meta

    best_brand, best_count = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    ratio = best_count / max(1, known_hits)
    meta["winner_count"] = best_count
    meta["winner_ratio"] = round(ratio, 3)

    if best_count >= min_count and ratio >= min_ratio:
        meta["mode"] = "auto_inferred"
        meta["selected_brand"] = best_brand
        return best_brand, meta

    meta["mode"] = "auto_weak_signal"
    return None, meta


def filter_price_outliers(items):
    """
    глобальная фильтрация выбросов отключена

    по алгоритму из файла "средняя цена.docx" цены не должны
    удаляться на общем уровне
    коррекция выбросов выполняется внутри каждого ранга
    через _compute_rank_price
"""
    if not items:
        return items, []

    print(f"\n{'='*70}")
    print(f"{'ГЛОБАЛЬНАЯ ФИЛЬТРАЦИЯ ВЫБРОСОВ ОТКЛЮЧЕНА':^70}")
    print(f"{'='*70}")
    print("  По алгоритму средней цены выбросы не удаляются глобально.")
    print("  Коррекция выполняется внутри каждого ранга:")
    print("  median -> floor=0.7x -> ceiling=1.2x -> clamp -> average")
    print(f"  Передано товаров без удаления: {len(items)}")
    print(f"{'='*70}\n")

    return items, []


@app.route("/analyze", methods=["POST"])
def analyze():
    # общее время начала обработки
    total_start_time = time.time()
    stage_times = {}

    request_trace_id = get_trace_id()
    # 1. принять фото от пользователя
    if "image" not in request.files:
        return jsonify({"status": "error", "message": "не передано поле image"}), 400
    image_file = request.files["image"]
    if image_file.filename == "":
        return jsonify({"status": "error", "message": "пустое имя файла"}), 400
    brand_hint = (request.form.get("brand") or request.args.get("brand") or "").strip()
    avito_context = _extract_avito_price_context()
    artifact_now = datetime.now()
    artifact_ts = build_results_timestamp(artifact_now)
    temp_path = None

    try:
        # 2. сохранить временно на диск
        stage_start = time.time()
        temp_path = save_temp_image(image_file)
        image_hash = get_image_hash(temp_path)
        stage_times["1. Сохранение и хеширование изображения"] = time.time() - stage_start

        # 2.1. проверяем кэш перед обработкой
        stage_start = time.time()
        cached_result = load_from_cache(image_hash)
        stage_times["2. Проверка кэша"] = time.time() - stage_start

        if cached_result is not None:
            print(f"✓ возвращаем результат из кэша (хеш: {image_hash[:16]}...)")
            response_payload = _apply_request_context_to_payload(cached_result, avito_context)
            try:
                response_payload = _persist_response_artifacts(
                    response_payload,
                    artifact_ts=artifact_ts,
                    generated_at=artifact_now,
                )
            except Exception as e:
                print(f"ошибка при сохранении результата в файл: {e}")
                log_exception(log, 'api.traceback', e, level='error')
            return jsonify(response_payload)

        print(f"→ изображение не найдено в кэше, начинаем обработку (хеш: {image_hash[:16]}...)")

        # загрузка изображения
        stage_start = time.time()
        src_img = load_pil_image_from_path(temp_path)
        stage_times["3. Загрузка изображения"] = time.time() - stage_start

        # 3. выполнить поиск по фото через serpapi
        stage_start = time.time()
        try:
            serp_json = serpapi_google_lens(temp_path)
        except Exception as e:
            stage_times["4. SerpAPI запрос (Google Lens)"] = time.time() - stage_start
            log_exception(log, "api.serpapi.request_error", e, level="error")
            return jsonify(
                {
                    "status": "error",
                    "message": f"serpapi временно недоступен: {str(e)}",
                    "stage": "serpapi_request",
                }
            )
        stage_times["4. SerpAPI запрос (Google Lens)"] = time.time() - stage_start

        # отладочный вывод: что вернул serpapi
        exact_matches_count = len(serp_json.get("exact_matches") or [])
        visual_matches_count = len(serp_json.get("visual_matches") or [])
        organic_count = len(serp_json.get("organic_results") or [])
        shopping_count = len(serp_json.get("shopping_results") or [])
        print(
            f"📊 SerpAPI вернул: exact_matches={exact_matches_count}, "
            f"visual_matches={visual_matches_count}, organic={organic_count}, shopping={shopping_count}"
        )

        # проверяем наличие ошибок или других полей
        if "error" in serp_json:
            error_msg = serp_json.get('error', '')
            print(f"⚠️ SerpAPI вернул ошибку: {error_msg}")
            if "hasn't returned any results" in error_msg.lower():
                print(f"💡 Google Lens не нашел результатов для этого изображения.")
                print(f"   Это может быть нормально, если:")
                print(f"   - изображение уникальное или редкое")
                print(f"   - Google Lens не может получить доступ к URL изображения")
                print(f"   - изображение не распознается Google Lens")
        if "search_metadata" in serp_json:
            status = serp_json.get("search_metadata", {}).get("status")
            print(f"📊 Статус запроса SerpAPI: {status}")
            # проверяем google_lens_url для отладки
            google_lens_url = serp_json.get("search_metadata", {}).get("google_lens_url", "")
            if google_lens_url:
                print(f"🔗 Google Lens URL: {google_lens_url}")

            # выводим все ключи ответа для диагностики
            if (
                exact_matches_count == 0
                and visual_matches_count == 0
                and organic_count == 0
                and shopping_count == 0
            ):
                print(f"⚠️ SerpAPI вернул пустой результат. Доступные ключи в ответе: {list(serp_json.keys())[:20]}")
                # проверяем альтернативные поля
                for key in [
                    "knowledge_graph",
                    "related_searches",
                    "inline_images",
                    "images_results",
                    "detections",
                    "best_guess",
                ]:
                    if key in serp_json:
                        value = serp_json.get(key)
                        if isinstance(value, list):
                            count = len(value)
                            if count > 0:
                                print(f"  → найдено в {key}: {count} результатов")
                        elif isinstance(value, dict):
                            print(f"  → найдено в {key}: {type(value).__name__} с ключами {list(value.keys())[:5]}")
                        else:
                            print(f"  → найдено в {key}: {str(value)[:100]}")

                # сохраняем полный ответ для диагностики (первые 1000 символов)
                serp_json_str = json.dumps(serp_json, indent=2, ensure_ascii=False)[:2000]
                print(f"📄 Первые 2000 символов ответа SerpAPI:\n{serp_json_str}...")

        # 4. получить json-результаты (ссылки и изображения)
        stage_start = time.time()
        raw_results = extract_results_from_serpapi(serp_json)
        stage_times["5. Извлечение результатов из SerpAPI"] = time.time() - stage_start
        print(f"📊 После extract_results_from_serpapi: {len(raw_results)} результатов")

        # отладочный вывод: какие домены приходят
        if raw_results:
            domains_found = {}
            for r in raw_results:
                url = r.get("url") or ""
                if url:
                    d = domain_of(url)
                    domains_found[d] = domains_found.get(d, 0) + 1
            print(f"📊 Найденные домены: {domains_found}")
            print(f"Разрешенные домены: {list(ALLOWED_DOMAINS.keys())}")

        # 5. lens-like этап: отбор кандидатных ссылок максимально близко к выдаче Google Lens
        # сначала фильтруем по доменам/товарным URL на всем пуле serpapi,
        # затем формируем:
        # - top_no_similarity: кандидаты для fast pricing без similarity-фильтра
        # - similarity_tail: хвост для классической similarity-проверки
        stage_start = time.time()
        lens_like_exact_first = os.getenv("LENS_LIKE_EXACT_FIRST", "1").strip().lower() in {"1", "true", "yes"}
        visual_similarity_enabled = os.getenv("VISUAL_SIMILARITY_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
        local_title_ai_enabled = os.getenv("LOCAL_TITLE_AI_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
        top_results_no_similarity = max(
            0,
            int(os.getenv("SERPAPI_TOP_RESULTS_NO_SIMILARITY", "20")),
        )
        max_per_domain_top_no_similarity = max(
            0,
            int(os.getenv("SERPAPI_MAX_PER_DOMAIN_TOP_NO_SIMILARITY", "3")),
        )
        max_per_domain_similarity_candidates = max(
            0,
            int(os.getenv("SERPAPI_MAX_PER_DOMAIN_SIMILARITY_CANDIDATES", "6")),
        )
        brand_gate_enabled = os.getenv("SERPAPI_BRAND_GATE_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
        brand_gate_target = None
        brand_gate_meta = {
            "mode": "disabled",
            "selected_brand": None,
            "counts": {},
            "known_brand_hits": 0,
        }
        if brand_gate_enabled:
            brand_gate_target, brand_gate_meta = _infer_target_brand(raw_results, brand_hint)
        print(
            f'INFO reason="brand_gate" enabled={int(brand_gate_enabled)} '
            f"target_brand={(brand_gate_target or 'none')} mode={brand_gate_meta.get('mode')} "
            f"known_brand_hits={brand_gate_meta.get('known_brand_hits', 0)} "
            f"counts={brand_gate_meta.get('counts', {})}"
        )
        shape_gate_enabled = (
            visual_similarity_enabled
            and os.getenv("SERPAPI_SHAPE_GATE_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
        )
        shape_gate_threshold = min(1.0, max(0.0, _env_float("SERPAPI_SHAPE_GATE_THRESHOLD", 0.42)))
        shape_gate_image_size = max(96, int(os.getenv("SERPAPI_SHAPE_GATE_IMAGE_SIZE", "224")))
        shape_gate_workers = max(1, int(os.getenv("SERPAPI_SHAPE_GATE_MAX_WORKERS", "8")))
        shape_gate_connect_timeout = max(0.5, _env_float("SERPAPI_SHAPE_GATE_CONNECT_TIMEOUT_SEC", 1.8))
        shape_gate_read_timeout = max(1.0, _env_float("SERPAPI_SHAPE_GATE_READ_TIMEOUT_SEC", 3.0))
        shape_gate_top_enabled = os.getenv(
            "SERPAPI_SHAPE_GATE_TOP_ENABLED",
            "1",
        ).strip().lower() in {"1", "true", "yes"}
        shape_gate_top_threshold = min(
            1.0,
            max(0.0, _env_float("SERPAPI_SHAPE_GATE_TOP_THRESHOLD", shape_gate_threshold)),
        )
        shape_gate_top_image_size = max(
            96,
            int(os.getenv("SERPAPI_SHAPE_GATE_TOP_IMAGE_SIZE", str(shape_gate_image_size))),
        )

        filtered_top_domain_only = []
        filtered_similarity_candidates = []
        filtered_out_domains = {}
        filtered_out_domain_caps = {}
        filtered_out_catalogs = 0
        filtered_out_brand_mismatch = 0
        filtered_out_shape_mismatch = 0
        shape_checked = 0
        shape_unchecked = 0
        top_domain_counts = {}
        tail_domain_counts = {}
        lens_like_domain_pool = []
        # список отфильтрованных товаров для отчёта
        rejected_items = []

        for r in raw_results:
            url = r.get("url") or ""
            if not url:
                continue
            item_brand = _detect_brand_in_result(r)
            if brand_gate_target and item_brand and item_brand != brand_gate_target:
                filtered_out_brand_mismatch += 1
                d = domain_of(url)
                rejected_items.append({
                    "url": url,
                    "title": r.get("title", ""),
                    "site": ALLOWED_DOMAINS.get(d, d),
                    "reason": f"не совпал бренд (ожидался {brand_gate_target}, найден {item_brand})",
                    "reason_code": "brand_gate_mismatch",
                    "expected_brand": brand_gate_target,
                    "detected_brand": item_brand,
                })
                continue
            d = domain_of(url)
            if d in ALLOWED_DOMAINS:
                if is_product_page_url(url):
                    lens_like_domain_pool.append(r)
                else:
                    filtered_out_catalogs += 1
                    from urllib.parse import urlparse
                    path = urlparse(url).path
                    path_parts = [p for p in path.lower().split('/') if p]
                    last_part = path_parts[-1] if path_parts else ""
                    if last_part in ["chanel", "gucci", "louis-vuitton", "prada", "dior", "hermes"]:
                        reason_detail = f"заканчивается на бренд '{last_part}'"
                    elif last_part in ["bags", "handbags", "women", "men", "shoes", "accessories"]:
                        reason_detail = f"заканчивается на категорию '{last_part}'"
                    elif len(path_parts) <= 2:
                        reason_detail = f"короткий путь ({len(path_parts)} уровня)"
                    else:
                        reason_detail = "не содержит ID товара"
                    print(f"  ⚠ пропущен каталог (lens_like_pool): {url[:70]}... ({reason_detail})")
                    rejected_items.append({
                        "url": url,
                        "title": r.get("title", ""),
                        "site": ALLOWED_DOMAINS.get(d, d),
                        "reason": f"каталог/категория ({reason_detail})",
                        "reason_code": "catalog_page",
                    })
            else:
                filtered_out_domains[d] = filtered_out_domains.get(d, 0) + 1
                rejected_items.append({
                    "url": url,
                    "title": r.get("title", ""),
                    "site": d,
                    "reason": f"неподдерживаемый домен ({d})",
                    "reason_code": "unsupported_domain",
                })

        if lens_like_exact_first:
            exact_pool = [r for r in lens_like_domain_pool if (r.get("serp_source") or "") == "exact_matches"]
            other_pool = [r for r in lens_like_domain_pool if (r.get("serp_source") or "") != "exact_matches"]
            lens_like_ranked_pool = exact_pool + other_pool
        else:
            lens_like_ranked_pool = list(lens_like_domain_pool)

        selected_urls = set()

        def _take_with_cap(candidates, out_bucket, bucket_name, per_domain_cap, domain_counts, limit=None):
            for r in candidates:
                if limit is not None and len(out_bucket) >= limit:
                    break
                url = r.get("url") or ""
                if not url or url in selected_urls:
                    continue
                d = domain_of(url)
                if per_domain_cap > 0 and domain_counts.get(d, 0) >= per_domain_cap:
                    filtered_out_domain_caps[d] = filtered_out_domain_caps.get(d, 0) + 1
                    rejected_items.append({
                        "url": url,
                        "title": r.get("title", ""),
                        "site": ALLOWED_DOMAINS.get(d, d),
                        "reason": f"превышен лимит домена ({d}, cap={per_domain_cap}, bucket={bucket_name})",
                        "reason_code": "domain_cap",
                    })
                    continue
                out_bucket.append(r)
                selected_urls.add(url)
                domain_counts[d] = domain_counts.get(d, 0) + 1

        _take_with_cap(
            lens_like_ranked_pool,
            filtered_top_domain_only,
            "top_no_similarity",
            max_per_domain_top_no_similarity,
            top_domain_counts,
            limit=top_results_no_similarity if top_results_no_similarity > 0 else 0,
        )
        if shape_gate_enabled and shape_gate_top_enabled and filtered_top_domain_only:
            top_thumb_urls = [r.get("image") or "" for r in filtered_top_domain_only]
            top_workers = min(shape_gate_workers, len(top_thumb_urls))
            top_loaded_images = load_images_parallel(
                top_thumb_urls,
                max_workers=top_workers,
                timeout_connect=shape_gate_connect_timeout,
                timeout_read=shape_gate_read_timeout,
            )

            shape_filtered_top = []
            for r, top_img in zip(filtered_top_domain_only, top_loaded_images):
                url = r.get("url") or ""
                d = domain_of(url) if url else ""

                if top_img is None:
                    shape_unchecked += 1
                    shape_filtered_top.append(r)
                    continue

                shape_score = silhouette_shape_similarity(src_img, top_img, size=shape_gate_top_image_size)
                if shape_score is None:
                    shape_unchecked += 1
                    shape_filtered_top.append(r)
                    continue

                shape_checked += 1
                r["_shape_similarity"] = round(shape_score, 3)
                if shape_score < shape_gate_top_threshold:
                    filtered_out_shape_mismatch += 1
                    rejected_items.append({
                        "url": url,
                        "title": r.get("title", ""),
                        "site": ALLOWED_DOMAINS.get(d, d),
                        "reason": (
                            f"не совпал силуэт модели "
                            f"(shape={shape_score:.3f}, порог={shape_gate_top_threshold:.2f})"
                        ),
                        "reason_code": "shape_gate_mismatch",
                        "shape_similarity": round(shape_score, 3),
                        "shape_threshold": shape_gate_top_threshold,
                    })
                    continue

                shape_filtered_top.append(r)

            filtered_top_domain_only = shape_filtered_top
            selected_urls = set()
            top_domain_counts = {}
            for r in filtered_top_domain_only:
                url = r.get("url") or ""
                if not url:
                    continue
                selected_urls.add(url)
                d = domain_of(url)
                top_domain_counts[d] = top_domain_counts.get(d, 0) + 1

        _take_with_cap(
            lens_like_ranked_pool,
            filtered_similarity_candidates,
            "similarity_tail",
            max_per_domain_similarity_candidates,
            tail_domain_counts,
            limit=None,
        )
        filtered = filtered_top_domain_only + filtered_similarity_candidates

        lens_like_source_counts = {}
        for item in lens_like_ranked_pool:
            src = item.get("serp_source") or "unknown"
            lens_like_source_counts[src] = lens_like_source_counts.get(src, 0) + 1

        lens_like_meta = {
            "exact_first": lens_like_exact_first,
            "domain_pool_total": len(lens_like_domain_pool),
            "ranked_pool_total": len(lens_like_ranked_pool),
            "top_no_similarity": len(filtered_top_domain_only),
            "similarity_tail": len(filtered_similarity_candidates),
            "source_counts": lens_like_source_counts,
            "brand_gate": {
                "enabled": brand_gate_enabled,
                "target_brand": brand_gate_target,
                "filtered_mismatch": filtered_out_brand_mismatch,
                "mode": brand_gate_meta.get("mode"),
                "known_brand_hits": brand_gate_meta.get("known_brand_hits", 0),
                "counts": brand_gate_meta.get("counts", {}),
            },
            "shape_gate": {
                "enabled": shape_gate_enabled and shape_gate_top_enabled,
                "threshold": shape_gate_top_threshold,
                "checked": shape_checked,
                "unchecked": shape_unchecked,
                "filtered_mismatch": filtered_out_shape_mismatch,
            },
        }

        stage_times["6. Lens-like отбор кандидатов"] = time.time() - stage_start
        print(
            f"📊 Lens-like: {len(filtered)} кандидатов "
            f"(top_no_similarity={len(filtered_top_domain_only)}, "
            f"similarity_tail={len(filtered_similarity_candidates)}, "
            f"источники={lens_like_source_counts}, "
            f"brand_target={(brand_gate_target or 'none')}, "
            f"brand_mismatch={filtered_out_brand_mismatch}, "
            f"shape_checked={shape_checked}, "
            f"shape_unchecked={shape_unchecked}, "
            f"shape_mismatch={filtered_out_shape_mismatch}, "
            f"отфильтровано доменов: {filtered_out_domains}, "
            f"ограничено по доменам: {filtered_out_domain_caps}, "
            f"каталогов: {filtered_out_catalogs})"
        )
        print(
            f'INFO reason="pricing_stage_start" candidates_total={len(filtered)} '
            f"top_no_similarity={len(filtered_top_domain_only)} "
            f"similarity_tail={len(filtered_similarity_candidates)}"
        )

        # 6. гибридный отбор кандидатов:
        # - visual-base: основа через clip/opencv (если включен visual режим)
        # - ai-tail: аккуратный добор из хвоста через локальный ai-gate
        stage_start = time.time()
        title_ai_keep_limit = max(
            1,
            int(os.getenv("LOCAL_TITLE_AI_KEEP_LIMIT", str(max(top_results_no_similarity, 15)))),
        )
        if filtered:
            title_ai_keep_limit = min(title_ai_keep_limit, len(filtered))
        else:
            title_ai_keep_limit = 0

        ai_tail_enabled = os.getenv("SERPAPI_AI_TAIL_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
        ai_tail_limit = max(
            0,
            int(
                os.getenv(
                    "SERPAPI_AI_TAIL_LIMIT",
                    os.getenv("SERPAPI_TAIL_AI_LIMIT", "5"),
                )
            ),
        )
        ai_tail_max_candidates = max(
            1,
            int(os.getenv("SERPAPI_AI_TAIL_MAX_CANDIDATES", "40")),
        )
        visual_base_keep_limit = max(
            1,
            int(os.getenv(
                "SERPAPI_VISUAL_BASE_KEEP_LIMIT",
                str(max(top_results_no_similarity, MAX_RESULTS_TO_CHECK)),
            )),
        )
        hybrid_order = (os.getenv("SERPAPI_HYBRID_ORDER", "visual_first") or "visual_first").strip().lower()
        ai_first_mode = (
            visual_similarity_enabled
            and hybrid_order in {"ai_first", "ai_first_then_visual_tail", "title_first"}
        )
        ai_first_top_k = max(
            1,
            int(os.getenv("SERPAPI_AI_FIRST_TOP_K", str(max(top_results_no_similarity, 20)))),
        )
        visual_tail_limit = max(
            0,
            int(os.getenv("SERPAPI_VISUAL_TAIL_LIMIT", str(visual_base_keep_limit))),
        )
        ai_target_tail_enabled = _env_bool("SERPAPI_AI_TARGET_TAIL_ENABLED", True)
        ai_target_tail_limit = max(
            0,
            int(os.getenv("SERPAPI_AI_TARGET_TAIL_LIMIT", str(max(visual_tail_limit, ai_first_top_k)))),
        )
        ai_target_tail_max_candidates = max(
            1,
            int(os.getenv("SERPAPI_AI_TARGET_TAIL_MAX_CANDIDATES", str(max(ai_first_top_k, 40)))),
        )
        ai_target_tail_strict = _env_bool("SERPAPI_AI_TARGET_TAIL_STRICT", True)
        ai_first_process_all_candidates = _env_bool("SERPAPI_AI_FIRST_PROCESS_ALL_CANDIDATES", True)
        ai_first_selected_to_clip = _env_bool("SERPAPI_AI_FIRST_SELECTED_TO_CLIP", True)
        ai_first_selected_clip_raw_pass = _env_bool("SERPAPI_AI_FIRST_SELECTED_CLIP_RAW_PASS", True)
        ai_first_selected_similarity_relax = max(
            0.0,
            min(0.20, _env_float("SERPAPI_AI_FIRST_SELECTED_SIMILARITY_RELAX", 0.02)),
        )
        ai_first_selected_similarity_threshold = max(
            0.0,
            min(1.0, SIMILARITY_THRESHOLD - ai_first_selected_similarity_relax),
        )
        ai_first_selected_color_similarity_relax = max(
            0.0,
            min(0.40, _env_float("SERPAPI_AI_FIRST_SELECTED_COLOR_SIMILARITY_RELAX", 0.08)),
        )
        ai_first_selected_color_similarity_threshold = max(
            0.0,
            min(1.0, COLOR_SIMILARITY_THRESHOLD - ai_first_selected_color_similarity_relax),
        )
        ai_first_selected_phash_relax = max(
            0,
            min(64, int(os.getenv("SERPAPI_AI_FIRST_SELECTED_PHASH_RELAX", "10"))),
        )
        ai_first_selected_phash_threshold = max(
            0,
            int(PHASH_THRESHOLD) + int(ai_first_selected_phash_relax),
        )

        kept = []
        visual_base = []
        ai_tail_pool = []
        visual_tail_pool = []
        ai_tail_reason_by_index = {}
        kept_urls = set()
        title_ai_meta = {
            "enabled": local_title_ai_enabled,
            "visual_similarity_enabled": visual_similarity_enabled,
            "mode": "disabled",
            "model": None,
            "target_name": None,
            "error": None,
            "keep_limit": title_ai_keep_limit,
            "selected": 0,
            "rejected": 0,
            "used_candidates": 0,
            "ignored_candidates": 0,
            "visual_selected": 0,
            "ai_tail_selected": 0,
            "ai_tail_pool": 0,
            "ai_tail_enabled": bool(ai_tail_enabled and visual_similarity_enabled),
            "ai_tail_limit": ai_tail_limit,
            "visual_base_keep_limit": visual_base_keep_limit,
            "hybrid_order": hybrid_order,
            "ai_first_top_k": ai_first_top_k,
            "visual_tail_limit": visual_tail_limit,
            "visual_tail_pool": 0,
            "visual_tail_checked": 0,
            "visual_tail_no_preview": 0,
            "ai_target_tail_enabled": bool(ai_first_mode and ai_target_tail_enabled and local_title_ai_enabled),
            "ai_target_tail_limit": ai_target_tail_limit,
            "ai_target_tail_mode": "disabled",
            "ai_target_tail_selected": 0,
            "ai_target_tail_rejected": 0,
            "ai_target_tail_used": 0,
            "ai_first_process_all_candidates": bool(ai_first_mode and ai_first_process_all_candidates),
            "ai_first_selected_to_clip": bool(ai_first_mode and ai_first_selected_to_clip),
            "ai_first_selected_clip_raw_pass": bool(ai_first_mode and ai_first_selected_clip_raw_pass),
            "ai_first_selected_similarity_relax": round(ai_first_selected_similarity_relax, 3),
            "ai_first_selected_similarity_threshold": round(ai_first_selected_similarity_threshold, 3),
            "ai_first_selected_color_similarity_relax": round(ai_first_selected_color_similarity_relax, 3),
            "ai_first_selected_color_similarity_threshold": round(ai_first_selected_color_similarity_threshold, 3),
            "ai_first_selected_phash_relax": int(ai_first_selected_phash_relax),
            "ai_first_selected_phash_threshold": int(ai_first_selected_phash_threshold),
        }
        ai_profile_target_name: str | None = None
        ai_profile_keywords: list[str] = []
        ai_profile_negative_keywords: list[str] = []
        visual_tail_filtered_candidates_for_backfill: list[dict] = []

        if filtered and visual_similarity_enabled and not ai_first_mode:
            try:
                thumb_urls = [r.get("image") or "" for r in filtered]
                max_workers = min(shape_gate_workers, len(thumb_urls)) if thumb_urls else 1
                loaded_images = load_images_parallel(
                    thumb_urls,
                    max_workers=max_workers,
                    timeout_connect=shape_gate_connect_timeout,
                    timeout_read=shape_gate_read_timeout,
                )

                src_embedding = get_image_embedding(src_img)
                src_colors = get_dominant_colors(src_img, k=3) if ENABLE_COLOR_CHECK else None
                src_phash = get_perceptual_hash(src_img) if ENABLE_PHASH_CHECK else None

                available_indexes = []
                available_images = []
                for idx, img in enumerate(loaded_images):
                    if img is not None:
                        available_indexes.append(idx)
                        available_images.append(img)

                visual_metrics = [None] * len(filtered)
                if available_images:
                    batch_results = compute_image_similarity_batch(
                        src_embedding,
                        available_images,
                        src_colors=src_colors,
                        src_phash=src_phash,
                    )
                    for idx, batch_result in zip(available_indexes, batch_results):
                        visual_metrics[idx] = batch_result

                for idx, candidate in enumerate(filtered):
                    url = candidate.get("url") or ""
                    d = domain_of(url) if url else ""
                    metric = visual_metrics[idx]
                    working_candidate = {**candidate}

                    if metric is None:
                        ai_tail_reason_by_index[len(ai_tail_pool)] = "не удалось получить preview для visual-проверки"
                        ai_tail_pool.append(working_candidate)
                        continue

                    clip_sim = float(metric.get("clip_similarity") or 0.0)
                    final_sim = float(metric.get("final_similarity") or 0.0)
                    color_sim = metric.get("color_similarity")
                    phash_dist = metric.get("phash_distance")
                    raw_filter_reason = str(metric.get("filter_reason") or "")

                    working_candidate["clip_similarity"] = round(clip_sim, 3)
                    working_candidate["similarity"] = round(final_sim, 3)
                    if color_sim is not None:
                        working_candidate["color_similarity"] = round(float(color_sim), 3)
                    if phash_dist is not None:
                        working_candidate["phash_distance"] = int(phash_dist)

                    if not bool(metric.get("filtered")) and final_sim >= SIMILARITY_THRESHOLD:
                        visual_base.append(working_candidate)
                        continue

                    reason_code = "clip_below_threshold"
                    if "color_similarity_below_threshold" in raw_filter_reason:
                        reason_code = "color_mismatch"
                    elif "phash" in raw_filter_reason:
                        reason_code = "phash_mismatch"
                    elif "clip_similarity" in raw_filter_reason:
                        reason_code = "clip_below_threshold"
                    elif (
                        not bool(metric.get("filtered"))
                        and clip_sim >= SIMILARITY_THRESHOLD
                        and final_sim < SIMILARITY_THRESHOLD
                        and phash_dist is not None
                    ):
                        reason_code = "phash_penalty_final_below"

                    ai_tail_reason_by_index[len(ai_tail_pool)] = _get_filter_reason_text(
                        reason_code,
                        clip_sim=clip_sim,
                        color_sim=float(color_sim) if color_sim is not None else None,
                        phash_dist=int(phash_dist) if phash_dist is not None else None,
                        final_sim=final_sim,
                    )
                    ai_tail_pool.append(working_candidate)

                # основа visual-base: сортируем по источнику/схожести и режем лимитом
                visual_base.sort(
                    key=lambda x: (
                        0 if (x.get("serp_source") or "") == "exact_matches" else 1,
                        -(float(x.get("similarity") or 0.0)),
                        0 if is_ebay_domain(domain_of(x.get("url") or "")) else 1,
                    )
                )
                if len(visual_base) > visual_base_keep_limit:
                    visual_overflow = visual_base[visual_base_keep_limit:]
                    visual_base = visual_base[:visual_base_keep_limit]
                    for overflow_item in visual_overflow:
                        ai_tail_reason_by_index[len(ai_tail_pool)] = (
                            f"вышел за лимит visual-base (cap={visual_base_keep_limit})"
                        )
                        ai_tail_pool.append(overflow_item)

                for visual_rank, candidate in enumerate(visual_base, start=1):
                    kept_item = {
                        **candidate,
                        "_similarity_bypassed": False,
                        "_selected_by_visual": True,
                        "_selection_priority": 0,
                        "_title_ai_rank": visual_rank,
                    }
                    kept.append(kept_item)
                    if candidate.get("url"):
                        kept_urls.add(candidate.get("url"))

                title_ai_meta.update({
                    "mode": "hybrid_visual_ai_tail" if ai_tail_enabled else "visual_only",
                    "used_candidates": len(filtered),
                    "ignored_candidates": 0,
                    "visual_selected": len(visual_base),
                    "ai_tail_pool": len(ai_tail_pool),
                })
            except Exception as visual_exc:
                log_exception(log, "api.visual_gate.error", visual_exc, level="warning")
                ai_tail_pool = list(filtered)
                ai_tail_reason_by_index = {}
                title_ai_meta.update({
                    "mode": "visual_error_fallback_ai",
                    "error": str(visual_exc),
                    "used_candidates": len(filtered),
                    "ignored_candidates": 0,
                    "visual_selected": 0,
                    "ai_tail_pool": len(ai_tail_pool),
                })
        elif ai_first_mode:
            if ai_first_process_all_candidates:
                ai_top_k_effective = len(filtered)
            else:
                ai_top_k_effective = min(len(filtered), ai_first_top_k)
            ai_tail_pool = list(filtered[:ai_top_k_effective])
            ai_tail_reason_by_index = {}
            visual_tail_pool = list(filtered[ai_top_k_effective:])
            title_ai_meta.update({
                "mode": "hybrid_ai_first_visual_tail",
                "used_candidates": len(filtered),
                "ignored_candidates": 0 if ai_first_process_all_candidates else max(
                    0,
                    len(filtered) - ai_top_k_effective,
                ),
                "visual_selected": 0,
                "ai_tail_pool": len(ai_tail_pool),
                "visual_tail_pool": len(visual_tail_pool),
            })
        else:
            ai_tail_pool = list(filtered)
            title_ai_meta.update({
                "mode": "ai_full_pool",
                "used_candidates": len(filtered),
                "ignored_candidates": 0,
                "visual_selected": 0,
                "ai_tail_pool": len(ai_tail_pool),
            })

        # добор хвоста через ai-gate: в visual-режиме ограниченный, в ai-only полномасштабный
        ai_selected_indexes = []
        ai_reason_by_index = {}
        ai_rank_by_index = {}
        ai_working_pool = []
        ai_selected_set = set()
        ai_first_clip_pool = []
        ai_first_clip_urls = set()
        if ai_tail_pool:
            if ai_first_mode:
                if ai_first_selected_to_clip and ai_first_process_all_candidates:
                    effective_ai_keep_limit = len(ai_tail_pool)
                else:
                    effective_ai_keep_limit = title_ai_keep_limit
            elif visual_similarity_enabled:
                effective_ai_keep_limit = min(title_ai_keep_limit, ai_tail_limit)
                if not ai_tail_enabled:
                    effective_ai_keep_limit = 0
            else:
                effective_ai_keep_limit = title_ai_keep_limit

            effective_ai_keep_limit = max(0, min(effective_ai_keep_limit, len(ai_tail_pool)))
            if ai_first_mode and ai_first_process_all_candidates:
                ai_working_count = len(ai_tail_pool)
            else:
                ai_working_count = min(len(ai_tail_pool), ai_tail_max_candidates)
            ai_working_pool = ai_tail_pool[:ai_working_count]

            if effective_ai_keep_limit > 0 and ai_working_pool:
                if local_title_ai_enabled:
                    ai_gate_result = select_candidates_by_title_ai(
                        ai_working_pool,
                        target_brand=brand_gate_target,
                        brand_hint=brand_hint,
                        keep_limit=effective_ai_keep_limit,
                    )
                    ai_selected_indexes = list(ai_gate_result.get("selected_indexes") or [])
                    ai_reason_by_index = dict(ai_gate_result.get("reason_by_index") or {})
                    ai_mode_value = str(ai_gate_result.get("mode") or "ai_gate")
                    title_ai_meta.update({
                        "mode": (
                            f"hybrid_ai_first_visual_tail:{ai_mode_value}"
                            if ai_first_mode
                            else ai_mode_value
                        ),
                        "model": ai_gate_result.get("model"),
                        "target_name": ai_gate_result.get("target_name"),
                        "error": ai_gate_result.get("error"),
                        "used_candidates": int(ai_gate_result.get("used_candidates") or 0),
                        "ignored_candidates": int(ai_gate_result.get("ignored_candidates") or 0),
                    })
                    ai_profile_target_name_raw = ai_gate_result.get("target_name")
                    ai_profile_target_name = (
                        " ".join(str(ai_profile_target_name_raw).replace(
                            "\r",
                            " ",
                        ).replace("\n", " ").split()).strip()[:180]
                        if ai_profile_target_name_raw
                        else None
                    ) or None
                    ai_profile_keywords = list(ai_gate_result.get("keywords") or [])
                    ai_profile_negative_keywords = list(ai_gate_result.get("negative_keywords") or [])
                elif (not visual_similarity_enabled) or ai_first_mode:
                    ai_selected_indexes = list(range(effective_ai_keep_limit))
                    ai_reason_by_index = {
                        idx: "локальный ai-gate отключен, выбран по позиции в выдаче"
                        for idx in range(len(ai_working_pool))
                        if idx not in set(ai_selected_indexes)
                    }
                    title_ai_meta.update({
                        "mode": "disabled_by_env_ai_first" if ai_first_mode else "disabled_by_env",
                        "used_candidates": len(ai_working_pool),
                        "ignored_candidates": 0,
                    })
                else:
                    title_ai_meta.update({
                        "mode": "visual_only_ai_disabled",
                        "used_candidates": 0,
                        "ignored_candidates": len(ai_working_pool),
                    })

            ai_selected_set = set(ai_selected_indexes)
            ai_rank_by_index = {idx: rank for rank, idx in enumerate(ai_selected_indexes, start=1)}

            for idx, candidate in enumerate(ai_working_pool):
                full_url = candidate.get("url") or ""
                d = domain_of(full_url) if full_url else ""

                if idx in ai_selected_set:
                    if ai_first_mode and ai_first_selected_to_clip:
                        if full_url and full_url in ai_first_clip_urls:
                            continue
                        ai_first_clip_pool.append({
                            **candidate,
                            "_selected_by_title_ai": True,
                            "_ai_first_selected_for_clip": True,
                            "_title_ai_rank": ai_rank_by_index.get(idx, 10**6),
                        })
                        if full_url:
                            ai_first_clip_urls.add(full_url)
                        continue
                    if full_url and full_url in kept_urls:
                        continue
                    kept_item = {
                        **candidate,
                        "_similarity_bypassed": True,
                        "_selected_by_title_ai": True,
                        "_selection_priority": 0 if ai_first_mode else (1 if visual_similarity_enabled else 0),
                        "_title_ai_rank": ai_rank_by_index.get(idx, 10**6),
                    }
                    kept.append(kept_item)
                    if full_url:
                        kept_urls.add(full_url)
                    continue

                reason_text = (
                    ai_reason_by_index.get(idx)
                    or ai_tail_reason_by_index.get(idx)
                    or "отфильтровано локальным ai-gate по названиям"
                )
                rejected_items.append({
                    "url": full_url,
                    "title": candidate.get("title", ""),
                    "site": ALLOWED_DOMAINS.get(d, d),
                    "reason": reason_text,
                    "reason_code": "title_ai_gate_filtered",
                })

            # кандидаты сверх ai_working_count не отдаем в модель, но показываем в rejected
            for idx, candidate in enumerate(ai_tail_pool[ai_working_count:], start=ai_working_count):
                full_url = candidate.get("url") or ""
                d = domain_of(full_url) if full_url else ""
                rejected_items.append({
                    "url": full_url,
                    "title": candidate.get("title", ""),
                    "site": ALLOWED_DOMAINS.get(d, d),
                    "reason": "не рассмотрен ai-tail (ограничение по количеству кандидатов)",
                    "reason_code": "title_ai_tail_ignored_limit",
                })

        if ai_first_mode and ai_first_selected_to_clip:
            visual_tail_pool = list(ai_first_clip_pool)
            title_ai_meta["visual_tail_pool"] = len(visual_tail_pool)
            title_ai_meta["ai_target_tail_mode"] = "ai_first_selected_to_clip"
            title_ai_meta["ai_target_tail_selected"] = len(visual_tail_pool)
            title_ai_meta["ai_target_tail_rejected"] = 0

        # в режиме ai_first: до visual/clip отдельно прогоняем хвост через ai-gate
        if (
            ai_first_mode
            and visual_tail_pool
            and ai_target_tail_enabled
            and local_title_ai_enabled
            and not ai_first_selected_to_clip
        ):
            tail_keep_limit = min(
                len(visual_tail_pool),
                ai_target_tail_limit if ai_target_tail_limit > 0 else len(visual_tail_pool),
            )
            tail_working_count = min(len(visual_tail_pool), ai_target_tail_max_candidates)
            tail_working_pool = visual_tail_pool[:tail_working_count]
            tail_overflow_pool = visual_tail_pool[tail_working_count:]
            title_ai_meta["ai_target_tail_used"] = tail_working_count

            if tail_keep_limit <= 0:
                title_ai_meta["ai_target_tail_mode"] = "ai_target_tail_disabled_by_limit"
            elif not (ai_profile_target_name or ai_profile_keywords):
                title_ai_meta["ai_target_tail_mode"] = "ai_target_tail_skipped_no_profile"
                if ai_target_tail_strict:
                    visual_tail_pool = []
                    title_ai_meta["ai_target_tail_rejected"] = len(tail_working_pool) + len(tail_overflow_pool)
                    for candidate in tail_working_pool:
                        full_url = candidate.get("url") or ""
                        d = domain_of(full_url) if full_url else ""
                        rejected_items.append({
                            "url": full_url,
                            "title": candidate.get("title", ""),
                            "site": ALLOWED_DOMAINS.get(d, d),
                            "reason": "хвост отклонён: профиль модели не определен ai-gate",
                            "reason_code": "title_ai_target_tail_no_profile",
                        })
                    for candidate in tail_overflow_pool:
                        full_url = candidate.get("url") or ""
                        d = domain_of(full_url) if full_url else ""
                        rejected_items.append({
                            "url": full_url,
                            "title": candidate.get("title", ""),
                            "site": ALLOWED_DOMAINS.get(d, d),
                            "reason": "не рассмотрен tail ai-gate (ограничение по количеству кандидатов)",
                            "reason_code": "title_ai_target_tail_ignored_limit",
                        })
            else:
                tail_gate_result = select_candidates_by_title_ai(
                    tail_working_pool,
                    target_brand=brand_gate_target,
                    brand_hint=brand_hint,
                    keep_limit=min(tail_keep_limit, len(tail_working_pool)),
                    forced_target_name=ai_profile_target_name,
                    forced_keywords=ai_profile_keywords,
                    forced_negative_keywords=ai_profile_negative_keywords,
                )
                tail_selected_indexes = set(tail_gate_result.get("selected_indexes") or [])
                tail_reason_by_index = dict(tail_gate_result.get("reason_by_index") or {})
                tail_selected_pool = []
                tail_rejected_buffer = []

                for idx, candidate in enumerate(tail_working_pool):
                    if idx in tail_selected_indexes:
                        tail_selected_pool.append(candidate)
                        continue
                    full_url = candidate.get("url") or ""
                    d = domain_of(full_url) if full_url else ""
                    tail_rejected_buffer.append({
                        "url": full_url,
                        "title": candidate.get("title", ""),
                        "site": ALLOWED_DOMAINS.get(d, d),
                        "reason": tail_reason_by_index.get(idx) or "отфильтровано ai-gate по профилю целевой модели",
                        "reason_code": "title_ai_target_tail_filtered",
                    })

                tail_overflow_rejected = []
                for candidate in tail_overflow_pool:
                    full_url = candidate.get("url") or ""
                    d = domain_of(full_url) if full_url else ""
                    tail_overflow_rejected.append({
                        "url": full_url,
                        "title": candidate.get("title", ""),
                        "site": ALLOWED_DOMAINS.get(d, d),
                        "reason": "не рассмотрен tail ai-gate (ограничение по количеству кандидатов)",
                        "reason_code": "title_ai_target_tail_ignored_limit",
                    })

                if tail_selected_pool:
                    visual_tail_pool = tail_selected_pool
                    rejected_items.extend(tail_rejected_buffer)
                    rejected_items.extend(tail_overflow_rejected)
                    title_ai_meta["ai_target_tail_mode"] = str(tail_gate_result.get("mode") or "ai_target_tail")
                    title_ai_meta["ai_target_tail_selected"] = len(tail_selected_pool)
                    title_ai_meta["ai_target_tail_rejected"] = len(tail_rejected_buffer) + len(tail_overflow_rejected)
                elif ai_target_tail_strict:
                    visual_tail_pool = []
                    rejected_items.extend(tail_rejected_buffer)
                    rejected_items.extend(tail_overflow_rejected)
                    title_ai_meta["ai_target_tail_mode"] = "ai_target_tail_strict_empty"
                    title_ai_meta["ai_target_tail_selected"] = 0
                    title_ai_meta["ai_target_tail_rejected"] = len(tail_rejected_buffer) + len(tail_overflow_rejected)
                else:
                    visual_tail_pool = tail_working_pool
                    rejected_items.extend(tail_overflow_rejected)
                    title_ai_meta["ai_target_tail_mode"] = "ai_target_tail_empty_fallback_visual"
                    title_ai_meta["ai_target_tail_selected"] = len(visual_tail_pool)
                    title_ai_meta["ai_target_tail_rejected"] = len(tail_overflow_rejected)

        visual_tail_selected = 0
        visual_tail_checked = 0
        visual_tail_no_preview = 0
        if ai_first_mode and visual_tail_pool:
            try:
                thumb_urls = [r.get("image") or "" for r in visual_tail_pool]
                max_workers = min(shape_gate_workers, len(thumb_urls)) if thumb_urls else 1
                loaded_images = load_images_parallel(
                    thumb_urls,
                    max_workers=max_workers,
                    timeout_connect=shape_gate_connect_timeout,
                    timeout_read=shape_gate_read_timeout,
                )

                src_embedding = get_image_embedding(src_img)
                src_colors = get_dominant_colors(src_img, k=3) if ENABLE_COLOR_CHECK else None
                src_phash = get_perceptual_hash(src_img) if ENABLE_PHASH_CHECK else None

                available_indexes = []
                available_images = []
                for idx, img in enumerate(loaded_images):
                    if img is not None:
                        available_indexes.append(idx)
                        available_images.append(img)

                visual_metrics = [None] * len(visual_tail_pool)
                if available_images:
                    batch_results = compute_image_similarity_batch(
                        src_embedding,
                        available_images,
                        src_colors=src_colors,
                        src_phash=src_phash,
                    )
                    for idx, batch_result in zip(available_indexes, batch_results):
                        visual_metrics[idx] = batch_result

                effective_visual_tail_limit = visual_tail_limit if visual_tail_limit > 0 else len(visual_tail_pool)
                for idx, candidate in enumerate(visual_tail_pool):
                    full_url = candidate.get("url") or ""
                    d = domain_of(full_url) if full_url else ""
                    if visual_tail_selected >= effective_visual_tail_limit:
                        rejected_items.append({
                            "url": full_url,
                            "title": candidate.get("title", ""),
                            "site": ALLOWED_DOMAINS.get(d, d),
                            "reason": f"вышел за лимит visual-tail (cap={effective_visual_tail_limit})",
                            "reason_code": "visual_tail_cap",
                        })
                        continue

                    metric = visual_metrics[idx]
                    if metric is None:
                        visual_tail_no_preview += 1
                        rejected_items.append({
                            "url": full_url,
                            "title": candidate.get("title", ""),
                            "site": ALLOWED_DOMAINS.get(d, d),
                            "reason": "не удалось получить preview для visual-tail проверки",
                            "reason_code": "visual_tail_no_preview",
                        })
                        continue

                    visual_tail_checked += 1
                    working_candidate = {**candidate}
                    clip_sim = float(metric.get("clip_similarity") or 0.0)
                    final_sim = float(metric.get("final_similarity") or 0.0)
                    color_sim = metric.get("color_similarity")
                    phash_dist = metric.get("phash_distance")
                    raw_filter_reason = str(metric.get("filter_reason") or "")

                    working_candidate["clip_similarity"] = round(clip_sim, 3)
                    working_candidate["similarity"] = round(final_sim, 3)
                    if color_sim is not None:
                        working_candidate["color_similarity"] = round(float(color_sim), 3)
                    if phash_dist is not None:
                        working_candidate["phash_distance"] = int(phash_dist)

                    allow_ai_selected_relaxed_checks = (
                        ai_first_mode
                        and ai_first_selected_to_clip
                        and bool(working_candidate.get("_ai_first_selected_for_clip"))
                    )
                    allow_raw_clip_pass_for_ai_selected = (
                        allow_ai_selected_relaxed_checks
                        and ai_first_selected_clip_raw_pass
                    )
                    effective_similarity_threshold = (
                        ai_first_selected_similarity_threshold
                        if allow_ai_selected_relaxed_checks
                        else SIMILARITY_THRESHOLD
                    )
                    effective_color_threshold = (
                        ai_first_selected_color_similarity_threshold
                        if allow_ai_selected_relaxed_checks
                        else COLOR_SIMILARITY_THRESHOLD
                    )
                    effective_phash_threshold = (
                        ai_first_selected_phash_threshold
                        if allow_ai_selected_relaxed_checks
                        else PHASH_THRESHOLD
                    )
                    pass_by_final_similarity = (
                        (not bool(metric.get("filtered")))
                        and final_sim >= effective_similarity_threshold
                    )
                    pass_by_raw_clip_for_ai_selected = (
                        (not bool(metric.get("filtered")))
                        and allow_raw_clip_pass_for_ai_selected
                        and clip_sim >= effective_similarity_threshold
                    )
                    color_ok_for_ai_selected = (
                        (not ENABLE_COLOR_CHECK)
                        or (color_sim is None)
                        or (float(color_sim) >= float(effective_color_threshold))
                    )
                    phash_ok_for_ai_selected = (
                        (not ENABLE_PHASH_CHECK)
                        or (phash_dist is None)
                        or (int(phash_dist) <= int(effective_phash_threshold))
                    )
                    pass_by_relaxed_components_for_ai_selected = (
                        allow_ai_selected_relaxed_checks
                        and clip_sim >= effective_similarity_threshold
                        and color_ok_for_ai_selected
                        and phash_ok_for_ai_selected
                    )

                    if (
                        pass_by_final_similarity
                        or pass_by_raw_clip_for_ai_selected
                        or pass_by_relaxed_components_for_ai_selected
                    ):
                        if full_url and full_url in kept_urls:
                            continue
                        if pass_by_raw_clip_for_ai_selected and not pass_by_final_similarity:
                            working_candidate["_visual_pass_mode"] = "raw_clip_for_ai_selected"
                        elif pass_by_relaxed_components_for_ai_selected and not pass_by_final_similarity:
                            working_candidate["_visual_pass_mode"] = "relaxed_color_phash_for_ai_selected"
                        kept_item = {
                            **working_candidate,
                            "_similarity_bypassed": False,
                            "_selected_by_visual": True,
                            "_selection_priority": 1,
                            "_title_ai_rank": int(working_candidate.get("_title_ai_rank") or (10**6 + idx)),
                        }
                        kept.append(kept_item)
                        if full_url:
                            kept_urls.add(full_url)
                        visual_tail_selected += 1
                        continue

                    reason_code = "clip_below_threshold"
                    if "color_similarity_below_threshold" in raw_filter_reason:
                        reason_code = "color_mismatch"
                    elif "phash" in raw_filter_reason:
                        reason_code = "phash_mismatch"
                    elif "clip_similarity" in raw_filter_reason:
                        reason_code = "clip_below_threshold"
                    elif (
                        not bool(metric.get("filtered"))
                        and clip_sim >= effective_similarity_threshold
                        and final_sim < effective_similarity_threshold
                        and phash_dist is not None
                    ):
                        reason_code = "phash_penalty_final_below"

                    if allow_ai_selected_relaxed_checks:
                        visual_tail_filtered_candidates_for_backfill.append({
                            **candidate,
                            "_backfill_origin": "visual_tail_filtered_ai_selected",
                            "_backfill_reason_code": reason_code,
                            "_backfill_clip_similarity": round(clip_sim, 3),
                        })

                    rejected_items.append({
                        "url": full_url,
                        "title": candidate.get("title", ""),
                        "site": ALLOWED_DOMAINS.get(d, d),
                        "reason": _get_filter_reason_text(
                            reason_code,
                            clip_sim=clip_sim,
                            color_sim=float(color_sim) if color_sim is not None else None,
                            phash_dist=int(phash_dist) if phash_dist is not None else None,
                            final_sim=final_sim,
                            similarity_threshold=effective_similarity_threshold,
                            color_threshold=effective_color_threshold,
                            phash_threshold=effective_phash_threshold,
                        ),
                        "reason_code": "visual_tail_filtered",
                    })
            except Exception as visual_tail_exc:
                log_exception(log, "api.visual_tail.error", visual_tail_exc, level="warning")
                for candidate in visual_tail_pool:
                    full_url = candidate.get("url") or ""
                    d = domain_of(full_url) if full_url else ""
                    rejected_items.append({
                        "url": full_url,
                        "title": candidate.get("title", ""),
                        "site": ALLOWED_DOMAINS.get(d, d),
                        "reason": "ошибка visual-tail, кандидат отклонен",
                        "reason_code": "visual_tail_error",
                    })

        if ai_first_mode:
            title_ai_meta["visual_selected"] = visual_tail_selected
            title_ai_meta["visual_tail_checked"] = visual_tail_checked
            title_ai_meta["visual_tail_no_preview"] = visual_tail_no_preview

        title_ai_meta["ai_tail_selected"] = len(ai_selected_set)
        title_ai_meta["selected"] = len(kept)
        title_ai_meta["rejected"] = max(0, len(filtered) - len(kept))
        lens_like_meta["title_ai_gate"] = title_ai_meta
        stage_times["7. Локальный AI-gate (названия)"] = time.time() - stage_start

        print(
            f"🤖 локальный ai-gate: mode={title_ai_meta.get('mode')} "
            f"selected={title_ai_meta.get('selected')} "
            f"rejected={title_ai_meta.get('rejected')} "
            f"used={title_ai_meta.get('used_candidates')} "
            f"ignored={title_ai_meta.get('ignored_candidates')} "
            f"visual_selected={title_ai_meta.get('visual_selected')} "
            f"visual_tail_pool={title_ai_meta.get('visual_tail_pool')} "
            f"visual_tail_checked={title_ai_meta.get('visual_tail_checked')} "
            f"visual_tail_no_preview={title_ai_meta.get('visual_tail_no_preview')} "
            f"ai_target_tail_mode={title_ai_meta.get('ai_target_tail_mode')} "
            f"ai_target_tail_selected={title_ai_meta.get('ai_target_tail_selected')} "
            f"ai_target_tail_rejected={title_ai_meta.get('ai_target_tail_rejected')} "
            f"ai_tail_selected={title_ai_meta.get('ai_tail_selected')} "
            f"ai_first_process_all={int(bool(title_ai_meta.get('ai_first_process_all_candidates')))} "
            f"ai_first_selected_to_clip={int(bool(title_ai_meta.get('ai_first_selected_to_clip')))} "
            f"ai_first_selected_clip_raw_pass={int(bool(title_ai_meta.get('ai_first_selected_clip_raw_pass')))} "
            f"ai_first_selected_similarity_relax={title_ai_meta.get('ai_first_selected_similarity_relax')} "
            f"ai_first_selected_similarity_threshold={title_ai_meta.get('ai_first_selected_similarity_threshold')} "
            "ai_first_selected_color_similarity_relax="
            f"{title_ai_meta.get('ai_first_selected_color_similarity_relax')} "
            "ai_first_selected_color_similarity_threshold="
            f"{title_ai_meta.get('ai_first_selected_color_similarity_threshold')} "
            f"ai_first_selected_phash_relax={title_ai_meta.get('ai_first_selected_phash_relax')} "
            f"ai_first_selected_phash_threshold={title_ai_meta.get('ai_first_selected_phash_threshold')} "
            f"model={title_ai_meta.get('model') or 'none'}"
        )

        # сортируем kept: visual-base -> ai-tail, затем exact_matches, затем ebay
        def sort_key(x):
            url = x.get("url", "")
            domain = domain_of(url)
            is_ebay = is_ebay_domain(domain)
            selection_priority = int(x.get("_selection_priority", 1))
            ai_rank = int(x.get("_title_ai_rank") or 10**6)
            exact_priority = 0 if (x.get("serp_source") or "") == "exact_matches" else 1
            sim_score = float(x.get("similarity") or x.get("clip_similarity") or 0.0)
            return (selection_priority, ai_rank, exact_priority, -sim_score, 0 if is_ebay else 1)

        kept.sort(key=sort_key)
        canonical_dedup_enabled = _env_bool("SCRAPE_CANONICAL_DEDUP_ENABLED", True)
        dedup_dropped = 0
        if canonical_dedup_enabled:
            kept, dedup_dropped = _dedupe_candidates_before_scrape(kept)
            if dedup_dropped > 0:
                print(
                    f'INFO reason="scrape_candidate_dedup" '
                    f"dropped={dedup_dropped} kept={len(kept)} mode=canonical_url"
                )

        # итоговый диапазон кандидатов перед парсингом:
        # стараемся держать список в пределах [min, max] при наличии подходящих ссылок
        keep_min_candidates = max(0, int(os.getenv("SERPAPI_KEEP_MIN_CANDIDATES", "8")))
        keep_max_candidates = max(1, int(os.getenv("SERPAPI_KEEP_MAX_CANDIDATES", "15")))
        if keep_max_candidates < keep_min_candidates:
            keep_max_candidates = keep_min_candidates

        range_backfilled = 0
        range_trimmed = 0

        if len(kept) < keep_min_candidates and filtered:
            seen_keys = set()
            for item in kept:
                item_url = str(item.get("url") or "").strip()
                if item_url:
                    seen_keys.add(_canonical_candidate_key(item_url))

            fallback_pool = sorted(
                filtered,
                key=lambda x: (
                    0 if (x.get("serp_source") or "") == "exact_matches" else 1,
                    0 if is_ebay_domain(domain_of(x.get("url") or "")) else 1,
                ),
            )

            for candidate in fallback_pool:
                if len(kept) >= keep_min_candidates:
                    break
                full_url = str(candidate.get("url") or "").strip()
                if not full_url:
                    continue
                dedup_key = _canonical_candidate_key(full_url)
                if dedup_key in seen_keys:
                    continue

                kept_item = {
                    **candidate,
                    "_similarity_bypassed": True,
                    "_selected_by_keep_range_backfill": True,
                    "_selection_priority": 2,
                    "_title_ai_rank": 10**7 + range_backfilled,
                }
                kept.append(kept_item)
                seen_keys.add(dedup_key)
                range_backfilled += 1

        if keep_max_candidates > 0 and len(kept) > keep_max_candidates:
            overflow = kept[keep_max_candidates:]
            kept = kept[:keep_max_candidates]
            range_trimmed = len(overflow)
            for candidate in overflow:
                full_url = candidate.get("url") or ""
                d = domain_of(full_url) if full_url else ""
                rejected_items.append({
                    "url": full_url,
                    "title": candidate.get("title", ""),
                    "site": ALLOWED_DOMAINS.get(d, d),
                    "reason": f"вышел за лимит итогового списка (cap={keep_max_candidates})",
                    "reason_code": "keep_range_max_cap",
                })

        if range_backfilled > 0:
            kept.sort(key=sort_key)

        if range_backfilled > 0 or range_trimmed > 0:
            print(
                f'INFO reason="keep_range_applied" '
                f"min={keep_min_candidates} max={keep_max_candidates} "
                f"backfilled={range_backfilled} trimmed={range_trimmed} "
                f"final={len(kept)}"
            )

        reject_reason_counts = Counter(
            str(item.get("reason_code") or "unknown")
            for item in rejected_items
        )
        print(
            f'INFO reason="filter_reject_stats" total={len(rejected_items)} '
            f"domain_cap={reject_reason_counts.get('domain_cap', 0)} "
            f"visual_tail_filtered={reject_reason_counts.get('visual_tail_filtered', 0)} "
            f"title_ai_gate_filtered={reject_reason_counts.get('title_ai_gate_filtered', 0)} "
            f"shape_gate_mismatch={reject_reason_counts.get('shape_gate_mismatch', 0)} "
            f"unsupported_domain={reject_reason_counts.get('unsupported_domain', 0)}"
        )

        refine_require_condition = _env_bool("SCRAPE_REFINE_REQUIRE_CONDITION", False)

        print(
            f"итого: найдено {len(kept)} результатов "
            f"(visual_selected={title_ai_meta.get('visual_selected', 0)}, "
            f"ai_target_tail_selected={title_ai_meta.get('ai_target_tail_selected', 0)}, "
            f"title_ai_selected={title_ai_meta.get('ai_tail_selected', 0)}, "
            f"title_ai_rejected={title_ai_meta.get('rejected', 0)}, "
            f"dedup_dropped={dedup_dropped})"
        )

        # 7. спарсить данные с каждой страницы (параллельно для ускорения)
        def process_single_item(
            r,
            index,
            total,
            *,
            allow_zenrows_fallback=True,
            allow_ai_fallback=False,
            pass_name="full",
            fast_mode=False,
        ):
            """обрабатывает один элемент из списка kept: парсит страницу и формирует результат"""
            url = r["url"]
            domain = domain_of(url)
            site = ALLOWED_DOMAINS.get(domain, domain)
            title = r.get("title") or None
            price = None
            currency = None
            item_status = None
            condition = None
            country = None
            needs_refine = False

            # сохраняем serpapi цену для fallback (но не используем сразу)
            serp_price = r.get("serp_price")
            serp_currency = r.get("serp_currency") or "USD"

            # приоритет 1: пробуем распарсить страницу (самый надежный источник актуальных данных!)
            # парсим только если не достигнут лимит страниц
            need_scraping = index < MAX_PAGES_TO_SCRAPE

            # логируем решение о парсинге
            if not need_scraping:
                domain = domain_of(url)
                print(
                    f"  [{index + 1}/{total}] ⚠ парсинг {domain} пропущен: "
                    f"достигнут лимит страниц ({index}/{MAX_PAGES_TO_SCRAPE})"
                )

            if need_scraping:
                try:
                    domain = domain_of(url)
                    print(f"  [{index+1}/{total}] парсинг {domain}: {url[:60]}...")

                    # универсальная распаковка результата парсера
                    # разные парсеры возвращают разное количество аргументов (3 или 4)
                    result = scrape_by_domain(
                        url,
                        include_meta=True,
                        trace_id=request_trace_id,
                        allow_zenrows_fallback=allow_zenrows_fallback,
                        allow_ai_fallback=allow_ai_fallback,
                        fast_mode=fast_mode,
                    )
                    scraped_title = None
                    scraped_price = None
                    scraped_currency = None
                    scraped_status = None
                    scraped_condition = None
                    scraped_country = None

                    if isinstance(result, tuple):
                        if len(result) >= 6:
                            # парсер вернул (title, price, currency, status, condition, country)
                            (
                                scraped_title,
                                scraped_price,
                                scraped_currency,
                                scraped_status,
                                scraped_condition,
                                scraped_country,
                            ) = result[:6]
                        elif len(result) == 5:
                            # парсер вернул (title, price, currency, status, condition)
                            scraped_title, scraped_price, scraped_currency, scraped_status, scraped_condition = result
                        elif len(result) == 4:
                            # парсер вернул (title, price, currency, status)
                            scraped_title, scraped_price, scraped_currency, scraped_status = result
                        elif len(result) == 3:
                            # парсер вернул (title, price, currency) без статуса
                            scraped_title, scraped_price, scraped_currency = result
                        elif len(result) == 2:
                            # парсер вернул (title, price) без валюты и статуса
                            scraped_title, scraped_price = result
                        else:
                            print(
                                f"    [{index + 1}/{total}] ⚠ парсер вернул "
                                f"неожиданное количество значений: {len(result)}"
                            )
                    else:
                        print(f"    [{index+1}/{total}] ⚠ парсер вернул не tuple: {type(result)}")

                    # используем title со страницы только если он валидный
                    if scraped_title and scraped_title not in [
                        "403 ERROR",
                        "Sorry, you have been blocked",
                        "Access Denied",
                    ]:
                        title = scraped_title

                    # приоритет: цена со страницы (самый актуальный источник)
                    if scraped_price is not None and scraped_price > 20:
                        price = scraped_price
                        currency = scraped_currency or currency or "USD"
                        print(f"    [{index+1}/{total}] ✓ найдена цена со страницы: {price} {currency}")
                    elif scraped_price is not None and scraped_price <= 20:
                        print(
                            f"    [{index + 1}/{total}] ⚠ найдена цена {scraped_price}, "
                            "но она слишком маленькая (возможно не цена товара), игнорируем"
                        )
                    else:
                        print(f"    [{index+1}/{total}] ✗ цена не найдена на странице (scraped_price={scraped_price})")

                    # статус всегда берем со страницы если есть
                    if scraped_status:
                        item_status = _normalize_status_text(scraped_status) or scraped_status
                        print(f"    [{index+1}/{total}] ✓ найден статус со страницы: {item_status}")
                    else:
                        print(f"    [{index+1}/{total}] ⚠ статус не вернулся из парсера")

                    # состояние / страна
                    if scraped_condition:
                        condition = scraped_condition
                    if scraped_country:
                        country = scraped_country

                    missing_price_for_refine = (scraped_price is None) and (not _is_sold_status(item_status))
                    missing_status_for_refine = not (_normalize_status_text(scraped_status) if scraped_status else "")
                    missing_condition_for_refine = not (str(scraped_condition).strip() if scraped_condition else "")
                    needs_refine = (
                        missing_price_for_refine
                        or missing_status_for_refine
                        or (refine_require_condition and missing_condition_for_refine)
                    )
                    if needs_refine and not (allow_zenrows_fallback and allow_ai_fallback):
                        print(
                            f'INFO reason="scrape_selective_refine_candidate" '
                            f"pass={pass_name} site={site} "
                            f"missing_price={int(missing_price_for_refine)} "
                            f"missing_status={int(missing_status_for_refine)} "
                            f"missing_condition={int(missing_condition_for_refine)}"
                        )

                    if _is_catalog_status(item_status):
                        print(f"    [{index+1}/{total}] ✗ каталог, пропускаем: {site} | {url[:60]}...")
                        return None, None, False

                    # для ряда сайтов sold-страницы считаем без цены
                    sold_without_price_domains = {"yoogiscloset.com", "fashionphile.com"}
                    if domain in sold_without_price_domains and _is_sold_status(item_status):
                        if price is not None:
                            print(f"    [{index+1}/{total}] ↷ {domain}: продано, сбрасываем цену со страницы")
                        price = None
                        currency = None

                except Exception as e:
                    print(f"    [{index+1}/{total}] ✗ ошибка при парсинге {domain_of(url)}: {e}")
                    log_exception(log, 'api.traceback', e, level='error')

            # приоритет 2: если не нашли цену на странице, используем serpapi как fallback
            # для проданных товаров используем цену из serpapi даже если она < 20 usd (может быть историческая цена)
            if price is None:
                if _is_blocked_status(item_status):
                    print(f"    [{index+1}/{total}] ↷ статус blocked, цену из serpapi не используем")
                elif _is_sold_status(item_status):
                    # для проданных товаров используем цену из serpapi без ограничения в 20 usd
                    if domain in {"yoogiscloset.com", "fashionphile.com"}:
                        print(f"    [{index+1}/{total}] ↷ {domain}: продано, цену из serpapi не используем")
                    else:
                        if serp_price is not None and serp_price > 0:
                            price = serp_price
                            currency = serp_currency
                            print(
                                f"    [{index + 1}/{total}] ✓ использована цена из serpapi "
                                f"для проданного товара (fallback): {price} {currency}"
                            )
                        elif serp_price is not None:
                            print(
                                f"    [{index + 1}/{total}] ⚠ serpapi цена {serp_price} "
                                "некорректна для проданного товара"
                            )
                else:
                    # для товаров в продаже используем только разумные цены (больше 20 usd)
                    if serp_price is not None and serp_price > 20:
                        price = serp_price
                        currency = serp_currency
                        print(f"    [{index+1}/{total}] ✓ использована цена из serpapi (fallback): {price} {currency}")
                    elif serp_price is not None:
                        print(f"    [{index+1}/{total}] ⚠ serpapi цена {serp_price} слишком маленькая, игнорируем")

            # если всё ещё нет цены, пробуем извлечь из title
            # но только если цена разумная (больше 20 usd)
            if (
                price is None
                and title
                and not _is_blocked_status(item_status)
                and not (domain in {"yoogiscloset.com", "fashionphile.com"} and _is_sold_status(item_status))
            ):
                p, c = parse_price_and_currency(title)
                if p is not None and p > 20:
                    price = p
                    currency = c or currency
                    print(f"    [{index+1}/{total}] ✓ найдена цена в title: {price} {currency}")

            # если валюта не определена, но есть цена, ставим usd по умолчанию
            if price is not None and currency is None:
                currency = "USD"

            # конвертация валюты в usd и фильтрация неразумных цен
            # фильтруем слишком маленькие цены (меньше 20 usd после конвертации - вероятно не цена товара)
            # сохраняем оригинальную цену и валюту для отчетности
            original_price = price
            original_currency = currency
            price_usd = None
            price_rejected = False

            if price is not None:
                # конвертируем в usd для проверки и итогового результата
                if currency and currency.upper() == "USD":
                    price_usd = price
                else:
                    # пытаемся конвертировать в usd через сервис конвертации
                    price_usd = to_usd(price, currency)
                    if price_usd is None:
                        print(f"    [{index+1}/{total}] ⚠ не удалось конвертировать {price} {currency} в USD")
                        # если конвертация не удалась, но цена разумная, используем её
                        # но только если это не явно неправильная валюта (например, zar с маленькой ценой)
                        if currency and currency.upper() == "ZAR" and price < 100:
                            print(
                                f"    [{index + 1}/{total}] ✗ цена {price} {currency} "
                                "слишком маленькая для ZAR "
                                "(вероятно ошибка парсинга), игнорируем"
                            )
                            price_usd = None
                            price_rejected = True
                        else:
                            # сохраняем оригинальную цену, но price_usd остается none
                            pass

                # проверяем разумность цены в usd
                if price_usd is not None and price_usd < 20:
                    print(
                        f"    [{index + 1}/{total}] ⚠ цена {price_usd} USD "
                        f"(из {price} {currency}) слишком маленькая, не используем её"
                    )
                    price_usd = None
                    price_rejected = True

            # если цену явно отбросили как мусор, не держим original_*,
            # иначе ниже можно ошибочно посчитать товар "в продаже"
            if price_rejected:
                price = None
                currency = None
                original_price = None
                original_currency = None

            # определяем статус по приоритету:
            # 1. если парсер вернул статус - используем его (высший приоритет)
            # 2. если статус не вернулся, но есть цена - "в продаже"
            # 3. если нет ни статуса, ни цены - "неизвестно"
            final_status = _normalize_status_text(item_status)

            if not final_status:
                # статус не вернулся из парсера
                if price_usd is not None or original_price is not None:
                    # есть цена - значит товар в продаже
                    final_status = "Available"
                    print(f"    [{index+1}/{total}] ℹ статус не получен, но есть цена -> устанавливаем 'в продаже'")
                else:
                    # нет ни статуса, ни цены - неизвестно
                    final_status = "Unknown"
                    print(f"    [{index+1}/{total}] ℹ статус и цена не получены -> устанавливаем 'неизвестно'")

            # фильтруем проданные товары только если включена опция filter_sold_items
            # пропускаем только если статус явно указывает на продажу
            if FILTER_SOLD_ITEMS and final_status:
                if _is_sold_status(final_status):
                    print(f"  [{index+1}/{total}] ✗ пропущен (статус: {final_status}): {site} | {url[:60]}...")
                    return None, None, False  # возвращаем none, чтобы не добавлять в результаты

            item = {
                "site": site,
                "title": title,
                "price": round(price_usd, 2) if price_usd is not None else None,  # цена в usd
                "currency": "USD",  # всегда usd в результате
                "price_original": round(
                    original_price,
                    2,
                ) if original_price is not None else None,  # оригинальная цена
                "currency_original": original_currency,  # оригинальная валюта
                "status": final_status,
                "condition": condition,
                "country": country,
                "url": url,
                "image": r.get("image"),
                "similarity": round(r.get("similarity", 0.0), 3) if r.get("similarity") is not None else None,
            }
            # добавляем детальную информацию о схожести (если есть)
            if r.get("clip_similarity") is not None:
                item["clip_similarity"] = round(r.get("clip_similarity"), 3)
            if r.get("color_similarity") is not None:
                item["color_similarity"] = round(r.get("color_similarity"), 3)
            if r.get("phash_distance") is not None:
                item["phash_distance"] = r.get("phash_distance")
            if r.get("_shape_similarity") is not None:
                item["shape_similarity"] = round(r.get("_shape_similarity"), 3)

            # логируем финальные данные для отладки
            if price_usd is not None:
                print(
                    f"  [{index + 1}/{total}] → добавлен в результат: {site} "
                    f"| цена={price_usd:.2f} USD | статус={final_status} "
                    f"| схожесть={item['similarity']}"
                )
            else:
                print(
                    f"  [{index + 1}/{total}] → добавлен в результат: {site} "
                    f"| цена={original_price} {original_currency} "
                    f"(конвертация недоступна) | статус={final_status} "
                    f"| схожесть={item['similarity']}"
                )

            return item, price_usd, needs_refine

        # быстрый проход и выборочный резервный сценарий
        items = []
        usd_prices = []
        scrape_timeout_by_pass = {}
        scrape_backfill_enabled = False
        scrape_backfill_min_items = 0
        scrape_backfill_top_k = 0
        scrape_backfill_timeout_sec = 0

        stage_start = time.time()
        print(f"\n=== парсинг {len(kept)} сайтов (макс. {MAX_PARALLEL_SCRAPERS} потоков) ===")

        if len(kept) == 0:
            print('INFO reason="scrape_no_targets" details="no items to scrape after filtering"')
            items = []
            usd_prices = []
        else:
            scrape_stage_timeout_sec = int(os.getenv("SCRAPE_STAGE_TIMEOUT_SEC", "35"))
            scrape_stage_grace_sec = max(0.0, float(os.getenv("SCRAPE_STAGE_GRACE_SEC", "3")))
            scrape_refine_grace_sec = max(0.0, float(os.getenv("SCRAPE_REFINE_GRACE_SEC", "2")))
            scrape_pipeline_budget_sec = max(20, int(os.getenv("SCRAPE_PIPELINE_BUDGET_SEC", "35")))
            effective_stage_budget_sec = min(scrape_stage_timeout_sec, scrape_pipeline_budget_sec, 35)
            fast_pass_top_k = max(1, int(os.getenv("SCRAPE_FAST_PASS_TOP_K", "20")))
            fast_pass_ebay_cap = max(0, int(os.getenv("SCRAPE_FAST_PASS_EBAY_CAP", "1")))
            fast_pass_timeout_sec = max(8, int(os.getenv("SCRAPE_FAST_PASS_TIMEOUT_SEC", "24")))
            fast_pass_min_items = max(1, int(os.getenv("SCRAPE_FAST_PASS_MIN_ITEMS", "5")))
            fast_pass_overflow_k = max(0, int(os.getenv("SCRAPE_FAST_PASS_OVERFLOW_K", "3")))
            fast_pass_max_per_domain = max(0, int(os.getenv("SCRAPE_FAST_PASS_MAX_PER_DOMAIN", "3")))
            fast_pass_max_trr = max(0, int(os.getenv("SCRAPE_FAST_PASS_MAX_THEREALREAL", "2")))
            fast_priority_domains = {
                item.strip().lower()
                for item in (os.getenv(
                    "SCRAPE_FAST_PRIORITY_DOMAINS",
                    "ebay.com,poshmark.com,vestiairecollective.com,rebag.com,popchill.com,yoogiscloset.com",
                ) or "").split(",")
                if item and item.strip()
            }
            slow_priority_domains = {
                item.strip().lower()
                for item in (os.getenv(
                    "SCRAPE_SLOW_PRIORITY_DOMAINS",
                    "therealreal.com,theluxurycloset.com,jolicloset.com,fashionphile.com",
                ) or "").split(",")
                if item and item.strip()
            }
            scrape_recovery_enabled = os.getenv("SCRAPE_RECOVERY_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
            scrape_recovery_min_items = max(1, int(os.getenv("SCRAPE_RECOVERY_MIN_ITEMS", "8")))
            scrape_recovery_top_k = max(0, int(os.getenv("SCRAPE_RECOVERY_TOP_K", "8")))
            scrape_recovery_timeout_sec = max(4, int(os.getenv("SCRAPE_RECOVERY_TIMEOUT_SEC", "10")))
            scrape_recovery_extra_budget_sec = max(0, int(os.getenv("SCRAPE_RECOVERY_EXTRA_BUDGET_SEC", "10")))
            scrape_recovery_allow_zenrows = os.getenv(
                "SCRAPE_RECOVERY_ALLOW_ZENROWS",
                "1",
            ).strip().lower() in {"1", "true", "yes"}
            scrape_backfill_enabled = os.getenv("SCRAPE_BACKFILL_ENABLED", "1").strip().lower() in {"1", "true", "yes"}
            scrape_backfill_min_items = max(
                1,
                int(os.getenv("SCRAPE_BACKFILL_MIN_ITEMS", str(scrape_recovery_min_items))),
            )
            scrape_backfill_top_k = max(0, int(os.getenv("SCRAPE_BACKFILL_TOP_K", "6")))
            scrape_backfill_timeout_sec = max(4, int(os.getenv("SCRAPE_BACKFILL_TIMEOUT_SEC", "12")))
            scrape_backfill_allow_zenrows = os.getenv(
                "SCRAPE_BACKFILL_ALLOW_ZENROWS",
                "1",
            ).strip().lower() in {"1", "true", "yes"}
            selective_refine_max = max(0, int(os.getenv("SCRAPE_SELECTIVE_REFINE_MAX", "2")))
            selective_refine_timeout_sec = max(3, int(os.getenv("SCRAPE_SELECTIVE_REFINE_TIMEOUT_SEC", "5")))
            selective_refine_workers = max(1, int(os.getenv("SCRAPE_SELECTIVE_REFINE_WORKERS", "1")))

            def _domain_matches_list(domain_value, domain_list):
                d = (domain_value or "").strip().lower()
                if not d or not domain_list:
                    return False
                for item in domain_list:
                    if d == item or d.endswith(f".{item}"):
                        return True
                return False

            def _pick_fast_sort_key(candidate):
                url_candidate = candidate.get("url") or ""
                d_candidate = domain_of(url_candidate) if url_candidate else ""
                serp_source = str(candidate.get("serp_source") or "").strip().lower()
                selection_priority = int(candidate.get("_selection_priority", 1))
                title_ai_rank = int(candidate.get("_title_ai_rank") or (10**6))
                sim_score = float(candidate.get("similarity") or candidate.get("clip_similarity") or 0.0)

                domain_priority = 1
                if is_ebay_domain(d_candidate) or _domain_matches_list(d_candidate, fast_priority_domains):
                    domain_priority = 0
                elif _domain_matches_list(d_candidate, slow_priority_domains):
                    domain_priority = 2

                exact_priority = 0 if serp_source == "exact_matches" else 1
                return (
                    domain_priority,
                    exact_priority,
                    selection_priority,
                    title_ai_rank,
                    -sim_score,
                )

            def _pick_fast_targets(candidates, limit, ebay_cap):
                ordered_candidates = sorted(candidates, key=_pick_fast_sort_key)
                picked = []
                picked_urls = set()
                ebay_count = 0
                domain_counts = {}
                for candidate in ordered_candidates:
                    url_candidate = candidate.get("url")
                    if not url_candidate or url_candidate in picked_urls:
                        continue
                    d_candidate = domain_of(url_candidate)
                    if is_ebay_domain(d_candidate) and ebay_count >= ebay_cap:
                        continue
                    per_domain_cap = fast_pass_max_per_domain
                    if d_candidate.endswith("therealreal.com") and fast_pass_max_trr > 0:
                        per_domain_cap = fast_pass_max_trr
                    if per_domain_cap > 0 and domain_counts.get(d_candidate, 0) >= per_domain_cap:
                        continue
                    picked.append(candidate)
                    picked_urls.add(url_candidate)
                    domain_counts[d_candidate] = domain_counts.get(d_candidate, 0) + 1
                    if is_ebay_domain(d_candidate):
                        ebay_count += 1
                    if len(picked) >= limit:
                        break
                if len(picked) < limit:
                    for candidate in ordered_candidates:
                        url_candidate = candidate.get("url")
                        if not url_candidate or url_candidate in picked_urls:
                            continue
                        picked.append(candidate)
                        picked_urls.add(url_candidate)
                        if len(picked) >= limit:
                            break
                return picked

            def _run_scrape_pass(
                pass_name,
                targets,
                *,
                allow_zenrows_fallback,
                allow_ai_fallback,
                fast_mode,
                timeout_sec,
                max_workers,
                collect_refine,
            ):
                pass_items = []
                pass_prices = []
                pass_refine_urls = []
                if not targets:
                    return pass_items, pass_prices, pass_refine_urls

                executor = ThreadPoolExecutor(max_workers=max_workers)
                future_to_item = {}
                pending = set()
                completed_count = 0
                try:
                    future_to_item = {
                        executor.submit(
                            process_single_item,
                            r,
                            idx,
                            len(targets),
                            allow_zenrows_fallback=allow_zenrows_fallback,
                            allow_ai_fallback=allow_ai_fallback,
                            pass_name=pass_name,
                            fast_mode=fast_mode,
                        ): (r, idx)
                        for idx, r in enumerate(targets)
                    }
                    pending = set(future_to_item.keys())
                    deadline = time.time() + max(3, timeout_sec)
                    last_progress_log = 0.0

                    def _consume_done(done_futures):
                        nonlocal pass_items, pass_prices, pass_refine_urls
                        for future in done_futures:
                            try:
                                parsed = future.result()
                                if parsed is None:
                                    continue
                                item = None
                                price_usd = None
                                needs_refine_flag = False
                                if isinstance(parsed, tuple):
                                    if len(parsed) >= 3:
                                        item, price_usd, needs_refine_flag = parsed[:3]
                                    elif len(parsed) == 2:
                                        item, price_usd = parsed
                                if item is None:
                                    continue
                                pass_items.append(item)
                                if price_usd is not None:
                                    pass_prices.append(price_usd)
                                if collect_refine and needs_refine_flag and item.get("url"):
                                    pass_refine_urls.append(item.get("url"))
                            except CancelledError:
                                continue
                            except Exception as e:
                                r, idx = future_to_item.get(future, ({}, -1))
                                print(f"  [{idx+1}/{len(targets)}] ошибка при обработке {r.get(
                                    'url',
                                    'unknown',
                                )}: {e}")
                                log_exception(log, "api.traceback", e, level="error")

                    while pending:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            break
                        done, pending = wait(pending, timeout=min(1.0, remaining), return_when=FIRST_COMPLETED)
                        if done:
                            completed_count += len(done)
                            _consume_done(done)
                        now_ts = time.time()
                        if done and (now_ts - last_progress_log >= 2.0):
                            print(
                                f'INFO reason="scrape_progress" pass={pass_name} '
                                f"completed={completed_count} pending={len(pending)} "
                                f"timeout_left_sec={int(max(0, remaining))}"
                            )
                            last_progress_log = now_ts

                    if pending:
                        scrape_timeout_by_pass[pass_name] = scrape_timeout_by_pass.get(pass_name, 0) + len(pending)
                        print(
                            f'INFO reason="scrape_stage_timeout_partial_result" '
                            f"pass={pass_name} pending={len(pending)} timeout_sec={int(timeout_sec)}"
                        )
                        for future in pending:
                            future.cancel()

                        pass_grace_sec = scrape_refine_grace_sec if pass_name == "refine" else scrape_stage_grace_sec
                        if pass_grace_sec > 0:
                            grace_deadline = time.time() + pass_grace_sec
                            while pending and time.time() < grace_deadline:
                                grace_left = grace_deadline - time.time()
                                done_grace, pending = wait(
                                    pending,
                                    timeout=min(0.5, max(0.0, grace_left)),
                                    return_when=FIRST_COMPLETED,
                                )
                                if not done_grace:
                                    continue
                                completed_count += len(done_grace)
                                _consume_done(done_grace)

                            if pending:
                                print(
                                    f'INFO reason="scrape_stage_grace_finished" '
                                    f"pass={pass_name} grace_sec={pass_grace_sec:.1f} still_pending={len(pending)}"
                                )
                finally:
                    wait_for_workers = os.getenv(
                        "SCRAPE_WAIT_FOR_WORKERS_ON_TIMEOUT",
                        "0",
                    ).strip().lower() in {"1", "true", "yes"}
                    executor.shutdown(wait=wait_for_workers, cancel_futures=True)
                    # если явно включено ожидание воркеров, применяем поздние результаты,
                    # завершившиеся после таймаута/грации, чтобы ответ не расходился с логами
                    if wait_for_workers and pending:
                        done_after_shutdown = {f for f in pending if f.done()}
                        if done_after_shutdown:
                            completed_count += len(done_after_shutdown)
                            _consume_done(done_after_shutdown)
                            pending -= done_after_shutdown
                            print(
                                f'INFO reason="scrape_late_results_applied" '
                                f"pass={pass_name} items={len(done_after_shutdown)} pending={len(pending)}"
                            )
                        if pending:
                            print(
                                f'INFO reason="scrape_late_results_missing" '
                                f"pass={pass_name} pending={len(pending)}"
                            )

                return pass_items, pass_prices, pass_refine_urls

            serpapi_two_phase_enabled = os.getenv(
                "SERPAPI_TWO_PHASE_STRATEGY",
                "1",
            ).strip().lower() in {"1", "true", "yes"}
            serpapi_phase1_exact_top_k = max(1, int(os.getenv("SERPAPI_PHASE1_EXACT_TOP_K", "12")))
            serpapi_phase2_visual_top_k = max(0, int(os.getenv("SERPAPI_PHASE2_VISUAL_TOP_K", "8")))
            serpapi_phase2_min_priced_items = max(1, int(os.getenv("SERPAPI_PHASE2_MIN_PRICED_ITEMS", "8")))
            serpapi_phase2_min_items = max(1, int(os.getenv("SERPAPI_PHASE2_MIN_ITEMS", "8")))

            fast_limit = min(len(kept), fast_pass_top_k, MAX_PAGES_TO_SCRAPE)
            exact_candidates = [r for r in kept if (r.get("serp_source") or "") == "exact_matches"]
            non_exact_candidates = [r for r in kept if (r.get("serp_source") or "") != "exact_matches"]

            phase1_pool = exact_candidates if (serpapi_two_phase_enabled and exact_candidates) else kept
            phase1_limit = min(len(phase1_pool), fast_limit)
            if serpapi_two_phase_enabled and exact_candidates:
                phase1_limit = min(phase1_limit, serpapi_phase1_exact_top_k)

            fast_targets = _pick_fast_targets(phase1_pool, phase1_limit, fast_pass_ebay_cap)
            fast_target_urls = {target.get("url") for target in fast_targets if target.get("url")}
            print(
                f'INFO reason="scrape_fast_pass_start" total_kept={len(kept)} '
                f"fast_targets={len(fast_targets)} ebay_cap={fast_pass_ebay_cap} "
                f"stage_budget_sec={effective_stage_budget_sec} "
                f"two_phase={int(serpapi_two_phase_enabled)} "
                f"phase1_exact_pool={len(exact_candidates)} phase2_other_pool={len(non_exact_candidates)}"
            )

            fast_workers = max(1, min(MAX_PARALLEL_SCRAPERS, len(fast_targets)))
            fast_budget = min(fast_pass_timeout_sec, effective_stage_budget_sec)
            fast_items, fast_prices, refine_urls = _run_scrape_pass(
                "fast_phase1" if serpapi_two_phase_enabled else "fast",
                fast_targets,
                allow_zenrows_fallback=False,
                allow_ai_fallback=False,
                fast_mode=True,
                timeout_sec=fast_budget,
                max_workers=fast_workers,
                collect_refine=True,
            )
            items = fast_items
            usd_prices = fast_prices
            ordered_targets = list(fast_targets)

            phase2_added = 0
            if (
                serpapi_two_phase_enabled
                and exact_candidates
                and non_exact_candidates
                and serpapi_phase2_visual_top_k > 0
            ):
                phase1_priced_items = sum(1 for item in items if item.get("price") is not None)
                phase2_needed = (
                    len(items) < serpapi_phase2_min_items
                    or phase1_priced_items < serpapi_phase2_min_priced_items
                )
                time_left_for_phase2 = effective_stage_budget_sec - (time.time() - stage_start)
                if phase2_needed and time_left_for_phase2 > 3:
                    phase2_candidates = [
                        candidate
                        for candidate in non_exact_candidates
                        if candidate.get("url") and candidate.get("url") not in fast_target_urls
                    ]
                    phase2_limit = min(len(phase2_candidates), serpapi_phase2_visual_top_k)
                    overall_left = max(0, fast_limit - len(fast_targets))
                    if overall_left > 0:
                        phase2_limit = min(phase2_limit, overall_left)

                    if phase2_limit > 0:
                        phase2_targets = _pick_fast_targets(phase2_candidates, phase2_limit, fast_pass_ebay_cap)
                        if phase2_targets:
                            phase2_workers = max(1, min(MAX_PARALLEL_SCRAPERS, len(phase2_targets)))
                            phase2_budget = min(max(4, int(time_left_for_phase2)), fast_pass_timeout_sec)
                            print(
                                f'INFO reason="scrape_fast_pass_phase2_start" '
                                f"targets={len(phase2_targets)} timeout_sec={phase2_budget} "
                                f"phase1_items={len(items)} phase1_priced={phase1_priced_items}"
                            )
                            phase2_items, phase2_prices, phase2_refine_urls = _run_scrape_pass(
                                "fast_phase2",
                                phase2_targets,
                                allow_zenrows_fallback=False,
                                allow_ai_fallback=False,
                                fast_mode=True,
                                timeout_sec=phase2_budget,
                                max_workers=phase2_workers,
                                collect_refine=True,
                            )
                            items.extend(phase2_items)
                            usd_prices.extend(phase2_prices)
                            refine_urls.extend(phase2_refine_urls)
                            ordered_targets.extend(phase2_targets)
                            fast_targets.extend(phase2_targets)
                            fast_target_urls.update(
                                target.get("url") for target in phase2_targets if target.get("url")
                            )
                            phase2_added = len(phase2_items)
                            print(
                                f'INFO reason="scrape_fast_pass_phase2_done" '
                                f"items_added={len(phase2_items)} "
                                f"refine_candidates_added={len(phase2_refine_urls)}"
                            )
                        else:
                            print('INFO reason="scrape_fast_pass_phase2_skip_no_targets" details="pick_fast_targets returned empty"')
                    else:
                        print(
                            f'INFO reason="scrape_fast_pass_phase2_skip_limit" '
                            f"phase2_candidates={len(phase2_candidates)} phase2_limit={phase2_limit}"
                        )
                else:
                    print(
                        f'INFO reason="scrape_fast_pass_phase2_skip_not_needed" '
                        f"phase1_items={len(items)} phase1_priced={phase1_priced_items} "
                        f"min_items={serpapi_phase2_min_items} min_priced={serpapi_phase2_min_priced_items} "
                        f"time_left_sec={int(max(0, time_left_for_phase2))}"
                    )

            print(
                f'INFO reason="scrape_fast_pass_done" items={len(items)} '
                f"refine_candidates={len(refine_urls)} phase2_items_added={phase2_added}"
            )

            if len(items) < fast_pass_min_items and fast_pass_overflow_k > 0:
                time_left = effective_stage_budget_sec - (time.time() - stage_start)
                overflow_candidates = [
                    candidate
                    for candidate in kept
                    if candidate.get("url") and candidate.get("url") not in fast_target_urls
                ][:fast_pass_overflow_k]
                if overflow_candidates and time_left > 3:
                    overflow_workers = max(1, min(MAX_PARALLEL_SCRAPERS, len(overflow_candidates)))
                    overflow_budget = min(max(3, int(time_left)), 8)
                    print(
                        f'INFO reason="scrape_fast_pass_overflow_start" '
                        f"targets={len(overflow_candidates)} timeout_sec={overflow_budget}"
                    )
                    overflow_items, overflow_prices, overflow_refine_urls = _run_scrape_pass(
                        "fast_overflow",
                        overflow_candidates,
                        allow_zenrows_fallback=False,
                        allow_ai_fallback=False,
                        fast_mode=True,
                        timeout_sec=overflow_budget,
                        max_workers=overflow_workers,
                        collect_refine=True,
                    )
                    items.extend(overflow_items)
                    usd_prices.extend(overflow_prices)
                    refine_urls.extend(overflow_refine_urls)
                    ordered_targets.extend(overflow_candidates)
                    fast_target_urls.update(
                        candidate.get("url") for candidate in overflow_candidates if candidate.get("url")
                    )
                    print(
                        f'INFO reason="scrape_fast_pass_overflow_done" '
                        f"items_added={len(overflow_items)} refine_candidates_added={len(overflow_refine_urls)}"
                    )

            if scrape_recovery_enabled and len(items) < scrape_recovery_min_items and scrape_recovery_top_k > 0:
                targeted_urls = {target.get("url") for target in ordered_targets if target.get("url")}
                recovery_pool = [
                    candidate
                    for candidate in kept
                    if candidate.get("url") and candidate.get("url") not in targeted_urls
                ]
                recovery_limit = min(len(recovery_pool), scrape_recovery_top_k)
                recovery_targets = _pick_fast_targets(recovery_pool, recovery_limit, fast_pass_ebay_cap)
                if recovery_targets:
                    time_left = effective_stage_budget_sec - (time.time() - stage_start)
                    recovery_budget_window = max(0, int(time_left)) + scrape_recovery_extra_budget_sec
                    if recovery_budget_window > 3:
                        recovery_workers = max(1, min(MAX_PARALLEL_SCRAPERS, len(recovery_targets)))
                        recovery_budget = min(scrape_recovery_timeout_sec, recovery_budget_window)
                        print(
                            f'INFO reason="scrape_recovery_pass_start" '
                            f"targets={len(recovery_targets)} timeout_sec={recovery_budget} "
                            f"items_now={len(items)} target_items={scrape_recovery_min_items} "
                            f"allow_zenrows={int(scrape_recovery_allow_zenrows)}"
                        )
                        recovery_items, recovery_prices, recovery_refine_urls = _run_scrape_pass(
                            "recovery",
                            recovery_targets,
                            allow_zenrows_fallback=scrape_recovery_allow_zenrows,
                            allow_ai_fallback=False,
                            fast_mode=True,
                            timeout_sec=recovery_budget,
                            max_workers=recovery_workers,
                            collect_refine=True,
                        )
                        items.extend(recovery_items)
                        usd_prices.extend(recovery_prices)
                        refine_urls.extend(recovery_refine_urls)
                        ordered_targets.extend(recovery_targets)
                        fast_target_urls.update(
                            target.get("url") for target in recovery_targets if target.get("url")
                        )
                        print(
                            f'INFO reason="scrape_recovery_pass_done" '
                            f"items_added={len(recovery_items)} "
                            f"refine_candidates_added={len(recovery_refine_urls)} "
                            f"items_total={len(items)}"
                        )
                    else:
                        print(
                            f'INFO reason="scrape_recovery_pass_skip_budget" '
                            f"items_now={len(items)} budget_window_sec={recovery_budget_window}"
                        )
                else:
                    print(
                        f'INFO reason="scrape_recovery_pass_skip_no_targets" '
                        f"items_now={len(items)}"
                    )

            if (
                scrape_backfill_enabled
                and len(items) < scrape_backfill_min_items
                and scrape_backfill_top_k > 0
                and visual_tail_filtered_candidates_for_backfill
            ):
                targeted_urls = {target.get("url") for target in ordered_targets if target.get("url")}
                backfill_pool = []
                backfill_seen_urls = set()
                for candidate in visual_tail_filtered_candidates_for_backfill:
                    candidate_url = candidate.get("url")
                    if not candidate_url or candidate_url in targeted_urls or candidate_url in backfill_seen_urls:
                        continue
                    backfill_pool.append(candidate)
                    backfill_seen_urls.add(candidate_url)

                backfill_limit = min(len(backfill_pool), scrape_backfill_top_k)
                backfill_targets = _pick_fast_targets(backfill_pool, backfill_limit, fast_pass_ebay_cap)
                if backfill_targets:
                    time_left = effective_stage_budget_sec - (time.time() - stage_start)
                    backfill_budget_window = max(0, int(time_left)) + scrape_recovery_extra_budget_sec
                    if backfill_budget_window > 3:
                        backfill_workers = max(1, min(MAX_PARALLEL_SCRAPERS, len(backfill_targets)))
                        backfill_budget = min(scrape_backfill_timeout_sec, backfill_budget_window)
                        print(
                            f'INFO reason="scrape_backfill_gpt_start" '
                            f"targets={len(backfill_targets)} timeout_sec={backfill_budget} "
                            f"items_now={len(items)} target_items={scrape_backfill_min_items} "
                            f"allow_zenrows={int(scrape_backfill_allow_zenrows)}"
                        )
                        backfill_items, backfill_prices, backfill_refine_urls = _run_scrape_pass(
                            "backfill_gpt_selected",
                            backfill_targets,
                            allow_zenrows_fallback=scrape_backfill_allow_zenrows,
                            allow_ai_fallback=False,
                            fast_mode=True,
                            timeout_sec=backfill_budget,
                            max_workers=backfill_workers,
                            collect_refine=True,
                        )
                        items.extend(backfill_items)
                        usd_prices.extend(backfill_prices)
                        refine_urls.extend(backfill_refine_urls)
                        ordered_targets.extend(backfill_targets)
                        fast_target_urls.update(
                            target.get("url") for target in backfill_targets if target.get("url")
                        )
                        print(
                            f'INFO reason="scrape_backfill_gpt_done" '
                            f"items_added={len(backfill_items)} "
                            f"refine_candidates_added={len(backfill_refine_urls)} "
                            f"items_total={len(items)}"
                        )
                    else:
                        print(
                            f'INFO reason="scrape_backfill_gpt_skip_budget" '
                            f"items_now={len(items)} budget_window_sec={backfill_budget_window}"
                        )
                else:
                    print(
                        f'INFO reason="scrape_backfill_gpt_skip_no_targets" '
                        f"items_now={len(items)} pool={len(backfill_pool)}"
                    )

            time_left = effective_stage_budget_sec - (time.time() - stage_start)
            if selective_refine_max > 0 and time_left > 2 and refine_urls:
                refine_url_set = set(refine_urls)
                refine_targets = [
                    target for target in ordered_targets if target.get("url") in refine_url_set
                ][:selective_refine_max]
                if refine_targets:
                    refine_workers = max(1, min(selective_refine_workers, len(refine_targets)))
                    refine_budget = min(selective_refine_timeout_sec, max(3, int(time_left)))
                    print(
                        f'INFO reason="scrape_selective_refine_start" '
                        f"targets={len(refine_targets)} timeout_sec={refine_budget}"
                    )
                    refined_items, _, _ = _run_scrape_pass(
                        "refine",
                        refine_targets,
                        allow_zenrows_fallback=True,
                        allow_ai_fallback=False,
                        fast_mode=False,
                        timeout_sec=refine_budget,
                        max_workers=refine_workers,
                        collect_refine=False,
                    )
                    if refined_items:
                        items_by_url = {}
                        for item in items:
                            url_item = item.get("url")
                            if url_item:
                                items_by_url[url_item] = item
                        for refined_item in refined_items:
                            refined_url = refined_item.get("url")
                            if refined_url:
                                items_by_url[refined_url] = refined_item

                        ordered_urls = []
                        seen_urls = set()
                        for target in ordered_targets:
                            target_url = target.get("url")
                            if target_url and target_url not in seen_urls:
                                ordered_urls.append(target_url)
                                seen_urls.add(target_url)
                        items = [items_by_url[url_item] for url_item in ordered_urls if url_item in items_by_url]
                        usd_prices = [
                            item.get("price")
                            for item in items
                            if item.get("price") is not None and item.get("price") > 0
                        ]
                    print(
                        f'INFO reason="scrape_selective_refine_done" '
                        f"items_refined={len(refined_items)}"
                    )

        stage_times["11. Парсинг страниц (параллельно)"] = time.time() - stage_start
        scraping_time = stage_times["11. Парсинг страниц (параллельно)"]
        print(f"=== парсинг завершен за {scraping_time:.2f} сек ===\n")

        # 8. фильтрация выбросов по цене (мусор: рассрочки, аксессуары, ошибки парсинга)
        stage_start = time.time()
        items, price_outliers = filter_price_outliers(items)

        # добавляем отфильтрованные выбросы в список отклоненных товаров
        for outlier in price_outliers:
            rejected_items.append({
                "url": outlier.get("url"),
                "title": outlier.get("title"),
                "site": outlier.get("site"),
                "reason": outlier.get("reason"),
                "reason_code": "price_outlier",
                "price": outlier.get("price"),
                "currency": outlier.get("currency"),
            })

        scrape_timeout_total = sum(int(v) for v in scrape_timeout_by_pass.values())
        blocked_items_count = sum(1 for item in items if _is_blocked_status(item.get("status")))
        final_reject_reason_counts = Counter(
            str(item.get("reason_code") or "unknown")
            for item in rejected_items
        )
        print(
            f'INFO reason="scrape_diagnostics" '
            f"blocked_items={blocked_items_count} "
            f"timeout_futures={scrape_timeout_total} "
            f"timeouts_by_pass={dict(scrape_timeout_by_pass)} "
            f"domain_cap={final_reject_reason_counts.get('domain_cap', 0)} "
            f"visual_tail_filtered={final_reject_reason_counts.get('visual_tail_filtered', 0)}"
        )

        # пересчитываем список цен после фильтрации
        usd_prices = []
        for item in items:
            price = item.get("price")
            if price is not None and price > 0:
                usd_prices.append(price)

        stage_times["11.5. Фильтрация выбросов по цене"] = time.time() - stage_start

        # 9. расчет рыночной цены по ранговой модели отдельно для Sold/Available
        stage_start = time.time()
        pricing_sold = _compute_market_price_for_status(items, "sold")
        pricing_available = _compute_market_price_for_status(items, "available")

        median_price_sold = pricing_sold.get("market_price_usd")
        median_price_available = pricing_available.get("market_price_usd")
        median_price_raw = median(usd_prices) if usd_prices else None

        # основной показатель: в приоритете товары "в продаже", затем "продано"
        median_price = (
            median_price_available
            if median_price_available is not None
            else (median_price_sold if median_price_sold is not None else median_price_raw)
        )

        stage_times["12. Вычисление медианы и формирование ответа"] = time.time() - stage_start

        # логируем итоговую статистику
        print(f"\n=== итоговая статистика обработки ===")
        print(f"обработано результатов из kept: {len(kept)}")
        print(f"добавлено в финальный JSON: {len(items)}")
        print(f"найдено цен: {len(usd_prices)}")
        print(
            f"рыночная цена (Available): {median_price_available} USD"
            if median_price_available is not None
            else "рыночная цена (Available): не вычислена"
        )
        print(
            f"рыночная цена (Sold): {median_price_sold} USD"
            if median_price_sold is not None
            else "рыночная цена (Sold): не вычислена"
        )
        print(
            f"сырьевая медиана (все цены): {median_price_raw} USD"
            if median_price_raw is not None
            else "сырьевая медиана (все цены): не вычислена"
        )
        print(
            f"итоговая медиана: {median_price} USD"
            if median_price is not None
            else "итоговая медиана: не вычислена"
        )
        print(f"=====================================\n")

        # 10. сформировать ответ и сохранить в файл для удобства
        stage_start = time.time()

        # формируем настройки для передачи в ответ
        settings_payload = {
            "similarity_threshold": SIMILARITY_THRESHOLD,
            "color_similarity_threshold": COLOR_SIMILARITY_THRESHOLD,
            "enable_color_check": ENABLE_COLOR_CHECK,
            "enable_phash_check": ENABLE_PHASH_CHECK,
            "phash_threshold": PHASH_THRESHOLD,
            "visual_similarity_enabled": visual_similarity_enabled,
            "local_title_ai_enabled": local_title_ai_enabled,
            "local_title_ai_mode": title_ai_meta.get("mode"),
            "local_title_ai_model": title_ai_meta.get("model"),
            "local_title_ai_target_name": title_ai_meta.get("target_name"),
            "local_title_ai_keep_limit": title_ai_meta.get("keep_limit"),
            "serpapi_hybrid_order": hybrid_order,
            "serpapi_ai_first_top_k": ai_first_top_k,
            "serpapi_ai_target_tail_enabled": ai_target_tail_enabled,
            "serpapi_ai_target_tail_limit": ai_target_tail_limit,
            "serpapi_ai_target_tail_max_candidates": ai_target_tail_max_candidates,
            "serpapi_ai_target_tail_strict": ai_target_tail_strict,
            "serpapi_ai_first_selected_similarity_relax": ai_first_selected_similarity_relax,
            "serpapi_ai_first_selected_color_similarity_relax": ai_first_selected_color_similarity_relax,
            "serpapi_ai_first_selected_phash_relax": ai_first_selected_phash_relax,
            "scrape_backfill_enabled": scrape_backfill_enabled if len(kept) > 0 else False,
            "scrape_backfill_min_items": scrape_backfill_min_items if len(kept) > 0 else None,
            "scrape_backfill_top_k": scrape_backfill_top_k if len(kept) > 0 else None,
            "scrape_backfill_timeout_sec": scrape_backfill_timeout_sec if len(kept) > 0 else None,
        }

        # отладочный вывод настроек
        print(f"\n=== НАСТРОЙКИ ФИЛЬТРАЦИИ (передаются в ответ) ===")
        print(f"SIMILARITY_THRESHOLD: {SIMILARITY_THRESHOLD}")
        print(f"COLOR_SIMILARITY_THRESHOLD: {COLOR_SIMILARITY_THRESHOLD}")
        print(f"ENABLE_COLOR_CHECK: {ENABLE_COLOR_CHECK}")
        print(f"ENABLE_PHASH_CHECK: {ENABLE_PHASH_CHECK}")
        print(f"PHASH_THRESHOLD: {PHASH_THRESHOLD}")
        print(f"VISUAL_SIMILARITY_ENABLED: {visual_similarity_enabled}")
        print(f"LOCAL_TITLE_AI_ENABLED: {local_title_ai_enabled}")
        print(f"LOCAL_TITLE_AI_MODE: {title_ai_meta.get('mode')}")
        print(f"LOCAL_TITLE_AI_MODEL: {title_ai_meta.get('model')}")
        print(f"===============================================\n")

        response_payload = {
            "status": "ok",
            "ai_target_name": title_ai_meta.get("target_name"),
            "median_price_usd": round(median_price, 2) if median_price is not None else None,
            "median_price_available_usd": round(
                median_price_available,
                2,
            ) if median_price_available is not None else None,
            "median_price_sold_usd": round(median_price_sold, 2) if median_price_sold is not None else None,
            "median_price_raw_usd": round(median_price_raw, 2) if median_price_raw is not None else None,
            "items": items,
            "filtered_items": rejected_items,  # отфильтрованные товары с причинами
            "settings": settings_payload,  # настройки фильтрации из .env
            "pipeline": {
                "lens_like": lens_like_meta,
                "pricing": {
                    "kept_total": len(kept),
                    "final_items": len(items),
                    "prices_found": len(usd_prices),
                    "sold": pricing_sold,
                    "available": pricing_available,
                },
                "diagnostics": {
                    "blocked_items": blocked_items_count,
                    "scrape_timeouts_total": scrape_timeout_total,
                    "scrape_timeouts_by_pass": dict(scrape_timeout_by_pass),
                    "reject_reason_counts": {
                        str(k): int(v) for k, v in final_reject_reason_counts.items()
                    },
                },
            },
        }

        base_payload_for_cache = _strip_transient_response_fields(response_payload)
        response_payload = _apply_request_context_to_payload(response_payload, avito_context)

        try:
            response_payload = _persist_response_artifacts(
                response_payload,
                artifact_ts=artifact_ts,
                generated_at=artifact_now,
            )
        except Exception as e:
            # логируем ошибку вместо тихого игнорирования
            print(f"ошибка при сохранении результата в файл: {e}")
            log_exception(log, 'api.traceback', e, level='error')

        # 11. сохраняем результат в кэш для будущих запросов
        save_to_cache(image_hash, base_payload_for_cache)
        stage_times["13. Сохранение результатов (json + очередь + кэш)"] = time.time() - stage_start

        # вычисляем общее время обработки
        total_time = time.time() - total_start_time
        stage_times["ОБЩЕЕ ВРЕМЯ ОБРАБОТКИ"] = total_time

        # выводим детальную статистику по времени
        print(f"\n{'='*70}")
        print(f"{'СТАТИСТИКА ПО ВРЕМЕНИ ОБРАБОТКИ':^70}")
        print(f"{'='*70}")

        # сортируем этапы по порядку (кроме общего времени)
        sorted_stages = []
        total_stage = None
        for stage_name, stage_time in stage_times.items():
            if stage_name == "ОБЩЕЕ ВРЕМЯ ОБРАБОТКИ":
                total_stage = (stage_name, stage_time)
            else:
                sorted_stages.append((stage_name, stage_time))

        # выводим этапы по порядку
        for stage_name, stage_time in sorted_stages:
            percentage = (stage_time / total_time * 100) if total_time > 0 else 0
            print(f"  {stage_name:.<55} {stage_time:>8.2f} сек ({percentage:>5.1f}%)")

        # выводим общее время в конце
        if total_stage:
            print(f"{'─'*70}")
            print(f"  {total_stage[0]:.<55} {total_stage[1]:>8.2f} сек")
        print(f"{'='*70}\n")

        return jsonify(response_payload)

    except Exception as e:
        log_exception(log, 'api.traceback', e, level='error')
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        _cleanup_temp_path(temp_path)


@app.route("/results/<path:filename>", methods=["GET"])
def get_result_file(filename: str):
    # отдает сохраненные артефакты отчета по безопасному имени файла
    safe_name = os.path.basename(str(filename or "").strip())
    if not safe_name or safe_name != filename:
        abort(404)
    if not _RESULTS_FILE_RE.fullmatch(safe_name):
        abort(404)

    results_dir = get_results_dir(_project_root_dir())
    file_path = os.path.join(results_dir, safe_name)
    if not os.path.exists(file_path):
        abort(404)

    return send_from_directory(results_dir, safe_name, as_attachment=False)


@app.route("/report-status/<task_id>", methods=["GET"])
def report_status(task_id: str):
    # возвращает статус фоновой генерации pdf-отчета
    payload = get_report_task_status(task_id)
    status = payload.get("status")
    if status == "not_found":
        return jsonify(payload), 404
    if status == "error":
        return jsonify(payload), 503
    return jsonify(payload), 200


@app.route("/health", methods=["GET"])
def health():
    # health check эндпоинт для проверки работоспособности сервиса
    health_status = {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "checks": {}
    }

    # проверка 1: наличие ключа serpapi
    health_status["checks"]["serpapi_key"] = {
        "status": "ok" if SERPAPI_KEY else "error",
        "message": "ключ настроен" if SERPAPI_KEY else "ключ не настроен"
    }

    # проверка 2: модель clip загружена (только когда включен visual режим)
    visual_similarity_enabled = os.getenv("VISUAL_SIMILARITY_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
    if not visual_similarity_enabled:
        health_status["checks"]["clip_model"] = {
            "status": "skip",
            "message": "visual similarity выключен, загрузка clip не требуется",
        }
    else:
        try:
            init_clip_model()
            clip_loaded = get_clip_model() is not None
            health_status["checks"]["clip_model"] = {
                "status": "ok" if clip_loaded else "error",
                "message": f"модель {CLIP_MODEL_NAME} загружена" if clip_loaded else "модель не загружена",
                "device": get_clip_device()
            }
        except Exception as e:
            health_status["checks"]["clip_model"] = {
                "status": "error",
                "message": f"ошибка загрузки модели: {str(e)}"
            }

    # проверка 3: доступность директории кэша
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache_writable = os.access(CACHE_DIR, os.W_OK)
        health_status["checks"]["cache"] = {
            "status": "ok" if cache_writable else "error",
            "message": (
                "директория кэша доступна для записи"
                if cache_writable
                else "директория кэша недоступна для записи"
            ),
            "enabled": ENABLE_CACHE,
            "ttl_seconds": CACHE_TTL,
            "path": CACHE_DIR
        }
    except Exception as e:
        health_status["checks"]["cache"] = {
            "status": "error",
            "message": f"ошибка доступа к кэшу: {str(e)}"
        }

    # проверка 4: доступность директории результатов
    try:
        results_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "results")
        os.makedirs(results_dir, exist_ok=True)
        results_writable = os.access(results_dir, os.W_OK)
        health_status["checks"]["results_dir"] = {
            "status": "ok" if results_writable else "error",
            "message": (
                "директория результатов доступна для записи"
                if results_writable
                else "директория результатов недоступна для записи"
            ),
        }
    except Exception as e:
        health_status["checks"]["results_dir"] = {
            "status": "error",
            "message": f"ошибка доступа к директории результатов: {str(e)}"
        }

    # проверка 5: использование памяти (если доступно)
    try:
        import psutil
        process = psutil.Process()
        memory_info = process.memory_info()
        health_status["checks"]["memory"] = {
            "status": "ok",
            "rss_mb": round(memory_info.rss / 1024 / 1024, 2),
            "vms_mb": round(memory_info.vms / 1024 / 1024, 2)
        }
    except ImportError:
        health_status["checks"]["memory"] = {
            "status": "info",
            "message": "psutil не установлен, информация о памяти недоступна"
        }
    except Exception as e:
        health_status["checks"]["memory"] = {
            "status": "error",
            "message": f"ошибка получения информации о памяти: {str(e)}"
        }

    # определяем общий статус: если хотя бы одна критическая проверка не прошла, статус = error
    critical_checks = ["serpapi_key", "clip_model"]
    has_errors = any(
        health_status["checks"].get(check, {}).get("status") == "error"
        for check in critical_checks
    )

    if has_errors:
        health_status["status"] = "error"
        return jsonify(health_status), 503

    return jsonify(health_status), 200

"""
диспетчер парсеров - выбирает нужный парсер по домену"""

from config import EBAY_API_KEY, OPENROUTER_FALLBACK_ENABLED
import os
import re
import uuid
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from bs4 import BeautifulSoup
from utils.domain import domain_of, is_ebay_domain
from utils.logger import get_logger, log_event, log_exception, set_context_values
from utils.debug_http import diagnose_response
from utils.zenrows import fetch_url_via_zenrows
from services.ebay_api import extract_ebay_item_id, fetch_ebay_item_via_api, marketplace_from_domain
from services.openrouter_fallback import enrich_listing_fields_via_openrouter
from scrapers.base import fetch_html, fetch_html_with_stealth_headers
from scrapers.poshmark import scrape_poshmark
from scrapers.vestiaire import scrape_vestiaire
from scrapers.ebay import scrape_ebay
from scrapers.rebag import scrape_rebag
from scrapers.tlc import scrape_tlc
from scrapers.universal import scrape_universal
from scrapers.fashionphile import scrape_fashionphile
from scrapers.jolicloset import scrape_jolicloset
from scrapers.yoogiscloset import scrape_yoogiscloset
from scrapers.therealreal import scrape_therealreal
from scrapers.celebrityowned import scrape_celebrityowned
from scrapers.aretrotale import scrape_aretrotale
from scrapers.dallasdesignerhandbags import scrape_dallasdesignerhandbags
from scrapers.popchill import scrape_popchill
from scrapers.designerexchange import scrape_designerexchange
from scrapers.annsfabulousfinds import scrape_annsfabulousfinds

log = get_logger(__name__)


def _parse_domain_csv(raw_value: str) -> set[str]:
    values: set[str] = set()
    for chunk in (raw_value or "").split(","):
        domain = chunk.strip().lower()
        if domain:
            values.add(domain)
    return values


def _domain_matches_any(host: str, domains: set[str]) -> bool:
    if not host or not domains:
        return False
    h = host.strip().lower()
    return any(h == item or h.endswith(f".{item}") for item in domains)

_FAST_FORCE_ZENROWS_FALLBACK_DOMAINS = _parse_domain_csv(
    os.getenv("FAST_FORCE_ZENROWS_FALLBACK_DOMAINS", "vestiairecollective.com,jolicloset.com")
)
_FAST_FORCE_ZENROWS_DIRECT_DOMAINS = _parse_domain_csv(
    os.getenv(
        "FAST_FORCE_ZENROWS_DIRECT_DOMAINS",
        "vestiairecollective.com,therealreal.com,jolicloset.com",
    )
)


def _is_sold_status(status: str | None) -> bool:
    # проверяет, что статус указывает на проданный товар
    if not status:
        return False
    s = status.lower()
    sold_markers = [
        "продано",
        "sold",
        "sold out",
        "out of stock",
        "unavailable",
        "ended",
        "нет в наличии",
    ]
    return any(marker in s for marker in sold_markers)


def _ensure_ebay_ul_param(url: str) -> str:
    """добавляет ?_ul=US для eBay, если параметр отсутствует"""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        if "_ul" in qs:
            return url
        qs["_ul"] = ["US"]
        new_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        if "_ul=" in url:
            return url
    return f"{url}&_ul=US" if "?" in url else f"{url}?_ul=US"


def _clean_str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _guess_country_from_url(url: str) -> str | None:
    """
    определение страны по URL (по домену/субдомену/locale в пути)
    возвращает ISO-2 код (например, US, UK) либо None
"""
    if not url:
        return None

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    host = (parsed.netloc or "").lower()
    if not host:
        return None
    # убрать порт
    if ":" in host:
        host = host.split(":", 1)[0]

    d = domain_of(url)

    # eBay региональные домены
    ebay_map = {
        "ebay.com": "US",
        "ebay.ca": "CA",
        "ebay.co.uk": "UK",
        "ebay.de": "DE",
        "ebay.fr": "FR",
        "ebay.it": "IT",
        "ebay.es": "ES",
        "ebay.com.au": "AU",
        "ebay.at": "AT",
        "ebay.be": "BE",
        "ebay.ch": "CH",
        "ebay.ie": "IE",
        "ebay.nl": "NL",
        "ebay.pl": "PL",
        "ebay.in": "IN",
        "ebay.com.sg": "SG",
        "ebay.com.hk": "HK",
        "ebay.com.my": "MY",
        "ebay.ph": "PH",
    }
    if d in ebay_map:
        return ebay_map[d]

    # явная страна из субдомена (например, us.vestiairecollective.com)
    try:
        first = host.split(".")[0]
        if re.fullmatch(r"[a-z]{2}", first) and first not in {"ww", "ws"}:
            return "UK" if first == "uk" else first.upper()
    except Exception:
        pass

    # locale в пути: /us-en/ или /en-us/
    path = (parsed.path or "").lower()
    langs = {"en", "ru", "fr", "de", "it", "es", "pt", "zh", "ja", "ko"}
    m = re.search(r"/([a-z]{2})[-_]([a-z]{2})/", path)
    if m:
        a, b = m.group(1), m.group(2)
        if a in langs and b not in langs:
            return "UK" if b == "uk" else b.upper()
        if b in langs and a not in langs:
            return "UK" if a == "uk" else a.upper()

    # простая эвристика по домену известных сайтов (как минимум чтобы не было пусто)
    known = {
        "poshmark.com": "US",
        "therealreal.com": "US",
        "fashionphile.com": "US",
        "rebag.com": "US",
        "yoogiscloset.com": "US",
        "dallasdesignerhandbags.com": "US",
        "designerexchange.com": "UK",
    }
    return known.get(d)


def _infer_condition_from_title(title: str | None) -> str | None:
    """простейшая эвристика: пытается определить состояние по словам в заголовке"""
    if not title:
        return None
    t = str(title).lower()
    if re.search(r"\b(brand\s+new|new\s+with\s+tags|new\s+in\s+box|bnwt|nwt|unworn)\b", t):
        return "New"
    if re.search(r"\b(pre[- ]?owned|preowned|used|second\s+hand|preloved|vintage)\b", t):
        return "Used"
    return None


def _is_proxy_failure_diagnosis(diagnosis: dict | None) -> bool:
    # определяет, что fetch упал именно по прокси/туннелю, и можно пробовать zenrows fallback
    if not isinstance(diagnosis, dict):
        return False

    status = str(diagnosis.get("status") or "").strip().lower()
    if status in {"proxy_error", "proxy_auth_required"}:
        return True

    marker_blob = ""
    try:
        found_markers = diagnosis.get("found_markers") or {}
        marker_blob = " ".join(f"{k}:{v}" for k, v in found_markers.items()).lower()
    except Exception:
        marker_blob = ""

    error_text = f"{diagnosis.get('error') or ''} {marker_blob}".lower()
    proxy_tokens = (
        "proxy",
        "tunnel",
        "407",
        "err_tunnel_connection_failed",
        "proxyconnect",
        "socks",
        "proxy auth",
        "proxy authentication",
    )

    if status in {
        "transport_error",
        "connection_error",
        "error",
        "failed",
    } and any(token in error_text for token in proxy_tokens):
        return True
    return False


def scrape_by_domain(
    url: str,
    include_meta: bool = False,
    trace_id: str | None = None,
    *,
    allow_zenrows_fallback: bool = True,
    allow_ai_fallback: bool = False,
    fast_mode: bool = False,
):
    """
    выбирает парсер по домену и возвращает результат парсинга

    по умолчанию: (title, price, currency, status)
    если include_meta=True: (title, price, currency, status, condition, country)
"""
    d = domain_of(url)
    effective_allow_ai_fallback = bool(allow_ai_fallback and OPENROUTER_FALLBACK_ENABLED)
    effective_trace_id = trace_id or f"s{uuid.uuid4().hex[:8]}"
    set_context_values(trace_id=effective_trace_id, url=url, domain=d)
    log_event(
        log,
        "scrape.start",
        level="info",
        include_meta=include_meta,
        allow_zenrows_fallback=allow_zenrows_fallback,
        allow_ai_fallback=effective_allow_ai_fallback,
        fast_mode=fast_mode,
    )
    soup = None
    force_fast_zenrows_fallback = fast_mode and _domain_matches_any(
        d,
        _FAST_FORCE_ZENROWS_FALLBACK_DOMAINS,
    )
    force_fast_zenrows_direct = fast_mode and _domain_matches_any(
        d,
        _FAST_FORCE_ZENROWS_DIRECT_DOMAINS,
    )
    effective_allow_zenrows_fallback = allow_zenrows_fallback or force_fast_zenrows_fallback
    if force_fast_zenrows_fallback and not allow_zenrows_fallback:
        log_event(
            log,
            "scrape.zenrows.fast_force_enabled",
            level="info",
            domain=d,
        )
    if force_fast_zenrows_direct:
        log_event(
            log,
            "scrape.zenrows.fast_force_direct_enabled",
            level="info",
            domain=d,
        )

    def _pack(title, price, currency, status, condition=None, country=None):
        title = _clean_str(title)
        currency = _clean_str(currency)
        status = _clean_str(status)
        condition = _clean_str(condition)
        country = _clean_str(country)

        missing_fields: list[str] = []
        if not title:
            missing_fields.append("title")
        if not status:
            missing_fields.append("status")
        if price is None and not _is_sold_status(status):
            missing_fields.append("price")
        if (price is not None or "price" in missing_fields) and not currency:
            missing_fields.append("currency")
        if include_meta and not condition:
            missing_fields.append("condition")

        if soup is not None and effective_allow_ai_fallback and missing_fields:
            log_event(
                log,
                "scrape.ai_fallback.try",
                level="info",
                missing_fields=missing_fields,
                fast_mode=fast_mode,
            )
            try:
                ai_enriched = enrich_listing_fields_via_openrouter(
                    soup=soup,
                    url=url,
                    domain=d,
                    title=title,
                    price=price,
                    currency=currency,
                    status=status,
                    condition=condition,
                    include_condition=include_meta,
                )
                if ai_enriched:
                    ai_title = _clean_str(ai_enriched.get("title"))
                    if ai_title:
                        title = ai_title

                    ai_status = _clean_str(ai_enriched.get("status"))
                    if ai_status:
                        status = ai_status

                    ai_currency = _clean_str(ai_enriched.get("currency"))
                    if ai_currency:
                        currency = ai_currency

                    ai_price = ai_enriched.get("price")
                    if isinstance(ai_price, (int, float)) and ai_price > 0:
                        price = float(ai_price)

                    ai_condition = _clean_str(ai_enriched.get("condition"))
                    if ai_condition:
                        condition = ai_condition
                    log_event(
                        log,
                        "scrape.ai_fallback.ok",
                        level="info",
                        filled_fields=list(ai_enriched.keys()),
                    )
                else:
                    log_event(log, "scrape.ai_fallback.empty", level="warning")
            except Exception as e:
                log_exception(log, "scrape.ai_fallback.error", e, level="warning")

        if not include_meta:
            out = (title, price, currency, status)
            level = "info" if price is not None else ("warning" if status in {
                "blocked",
                "proxy_auth_required",
            } else "debug")
            log_event(
                log,
                "scrape.result",
                level=level,
                include_meta=False,
                has_price=(price is not None),
                price=price,
                currency=currency,
                status=status,
            )
            return out

        if condition is None:
            condition = _infer_condition_from_title(title)
        if country is None:
            country = _guess_country_from_url(url)

        out = (title, price, currency, status, condition, country)
        level = "info" if price is not None else ("warning" if status in {
            "blocked",
            "proxy_auth_required",
        } else "debug")
        log_event(
            log,
            "scrape.result",
            level=level,
            include_meta=True,
            has_price=(price is not None),
            price=price,
            currency=currency,
            status=status,
            condition=condition,
            country=country,
        )
        return out

    condition = None
    country = None
    ebay_api_partial: tuple | None = None

    # для ebay (все региональные домены) сначала пробуем api (быстрее и надежнее)
    if is_ebay_domain(d) and EBAY_API_KEY and EBAY_API_KEY.strip():
        item_id = extract_ebay_item_id(url)
        if item_id:
            marketplace_id = marketplace_from_domain(d)
            log_event(log, "scrape.ebay.api_try", level="debug", item_id=item_id, include_meta=include_meta)
            api_result = fetch_ebay_item_via_api(item_id, include_meta=include_meta, marketplace_id=marketplace_id)
            if isinstance(api_result, tuple) and len(api_result) >= 4:
                if len(api_result) >= 6:
                    ebay_api_partial = (
                        api_result[0],
                        api_result[1],
                        api_result[2],
                        api_result[3],
                        api_result[4],
                        api_result[5],
                    )
                else:
                    ebay_api_partial = (api_result[0], api_result[1], api_result[2], api_result[3], None, None)

                if api_result[1] is not None:
                    log_event(log, "scrape.ebay.api_ok", level="info", item_id=item_id)
                    if len(api_result) >= 6:
                        return _pack(
                            api_result[0],
                            api_result[1],
                            api_result[2],
                            api_result[3],
                            api_result[4],
                            api_result[5],
                        )
                    return _pack(api_result[0], api_result[1], api_result[2], api_result[3])

                log_event(log, "scrape.ebay.api_no_price", level="warning", item_id=item_id)
            else:
                log_event(log, "scrape.ebay.api_empty", level="warning", item_id=item_id)
        else:
            log_event(log, "scrape.ebay.item_id_missing", level="warning")
    # для остальных сайтов или если api не сработал - используем парсинг
    fetch_url = url
    if is_ebay_domain(d):
        fetch_url = _ensure_ebay_ul_param(url)
    fetch_retries = 1
    if is_ebay_domain(d):
        # для ebay по умолчанию держим 1 попытку, чтобы быстрее перейти к zenrows fallback
        try:
            ebay_retries = int((os.getenv("EBAY_FETCH_RETRIES", "1") or "1").strip())
        except Exception:
            ebay_retries = 1
        fetch_retries = max(1, ebay_retries)
    elif (
        d.endswith("therealreal.com")
        or d.endswith("yoogiscloset.com")
        or d.endswith("theluxurycloset.com")
    ):
        # даем второй шанс для сетевых таймаутов на нестабильных доменах
        fetch_retries = 2
    if d.endswith("therealreal.com"):
        # для trr по умолчанию работаем в режиме "1 ip = 1 запрос"
        try:
            trr_retries = int((os.getenv("TRR_FETCH_RETRIES", "1") or "1").strip())
        except Exception:
            trr_retries = 1
        fetch_retries = max(1, trr_retries)

    force_zenrows_for_trr = d.endswith("therealreal.com")
    force_zenrows_for_tlc = d.endswith("theluxurycloset.com")
    soup, diagnosis = fetch_html(
        fetch_url,
        max_retries=fetch_retries,
        allow_zenrows=(effective_allow_zenrows_fallback or force_zenrows_for_trr or force_zenrows_for_tlc),
        force_zenrows=force_fast_zenrows_direct,
        fast_mode=fast_mode,
    )

    # проверяем на блокировку
    if not soup:
        log_event(
            log,
            "scrape.fetch.failed",
            level="warning",
            fetch_url=fetch_url,
            status=(diagnosis.get("status") if diagnosis else None),
            status_code=(diagnosis.get("status_code") if diagnosis else None),
        )

        fetch_status = str((diagnosis or {}).get("status") or "").strip().lower()
        needs_stealth_retry = d.endswith("theluxurycloset.com") or (
            d.endswith("fashionphile.com") and fetch_status in {"transport_error", "timeout", "connection_error"}
        )

        if needs_stealth_retry:
            # дополнительная попытка без playwright/прокси, когда fetch_html вернул пусто
            try:
                retry_soup = fetch_html_with_stealth_headers(fetch_url)
                if retry_soup is not None:
                    soup = retry_soup
                    diagnosis = {
                        "domain": d,
                        "status": "ok",
                        "status_code": 200,
                        "final_url": fetch_url,
                        "content_size": len(str(retry_soup)),
                        "found_markers": {},
                        "proxy_used": "",
                    }
                    log_event(log, "scrape.fetch.stealth_retry_ok", level="info", domain=d)
            except Exception as e:
                log_exception(log, "scrape.fetch.stealth_retry_error", e, level="warning")

    should_try_zenrows_proxy_fallback = _is_proxy_failure_diagnosis(diagnosis)
    if not should_try_zenrows_proxy_fallback and force_fast_zenrows_fallback and isinstance(diagnosis, dict):
        fallback_status = str(diagnosis.get("status") or "").strip().lower()
        if fallback_status in {"blocked", "transport_error", "timeout", "connection_error"}:
            should_try_zenrows_proxy_fallback = True

    if effective_allow_zenrows_fallback and not soup and should_try_zenrows_proxy_fallback:
        # fallback на zenrows включаем только при проблемах с прокси
        try:
            zr_wait_for = None
            if d.endswith("jolicloset.com"):
                zr_wait_for = os.getenv("ZENROWS_WAIT_FOR_JOLICLOSET", "").strip() or "#product h1 span"
            elif d.endswith("therealreal.com"):
                zr_wait_for = os.getenv("ZENROWS_WAIT_FOR_TRR", "").strip() or '[data-testid="product-price/final"]'
            elif d.endswith("theluxurycloset.com"):
                zr_wait_for = os.getenv(
                    "ZENROWS_WAIT_FOR_TLC",
                    "",
                ).strip() or '[class*="ProductPriceV2__newProductPrice"]'

            log_event(
                log,
                "scrape.zenrows.proxy_fallback_try",
                level="warning",
                status=(diagnosis.get("status") if diagnosis else None),
                error=(diagnosis.get("error") if diagnosis else None),
                wait_for=zr_wait_for or None,
            )
            zr_status_code, zr_html, zr_final_url, zr_error, zr_meta = fetch_url_via_zenrows(
                fetch_url,
                wait_for=zr_wait_for,
            )
            if not zr_error and zr_status_code == 200 and zr_html:
                soup = BeautifulSoup(zr_html, "html.parser")
                diagnosis = diagnose_response(
                    url=fetch_url,
                    status_code=zr_status_code,
                    final_url=zr_final_url or fetch_url,
                    content=zr_html,
                    content_type="",
                    proxy_used="zenrows",
                    headers={},
                )
                diagnosis["zenrows"] = zr_meta
                log_event(
                    log,
                    "scrape.zenrows.proxy_fallback_ok",
                    level="info",
                    status=diagnosis.get("status"),
                    status_code=zr_status_code,
                    content_size=len(zr_html),
                    cost=(zr_meta or {}).get("zenrows_cost"),
                )
            else:
                log_event(
                    log,
                    "scrape.zenrows.proxy_fallback_failed",
                    level="warning",
                    status_code=zr_status_code,
                    error=zr_error,
                )
        except Exception as e:
            log_exception(log, "scrape.zenrows.proxy_fallback_error", e, level="warning")
    elif not effective_allow_zenrows_fallback and not soup and should_try_zenrows_proxy_fallback:
        log_event(
            log,
            "scrape.zenrows.proxy_fallback_skip",
            level="info",
            status=(diagnosis.get("status") if diagnosis else None),
        )

    if not soup:
        if is_ebay_domain(d) and ebay_api_partial:
            api_title, api_price, api_currency, api_status, api_condition, api_country = ebay_api_partial
            if api_title or api_price is not None or api_status or api_condition or api_country:
                return _pack(api_title, api_price, api_currency, api_status, api_condition, api_country)
        blocked_status = None
        if diagnosis:
            status = diagnosis.get("status", "unknown")
            if status == "http_error":
                status_code = diagnosis.get("status_code")
                if status_code is None:
                    err_text = str(diagnosis.get("error") or "")
                    code_match = re.search(r"HTTP\s+(\d{3})", err_text, flags=re.I)
                    if code_match:
                        try:
                            status_code = int(code_match.group(1))
                        except Exception:
                            status_code = None
                if (
                    d.endswith("poshmark.com")
                    and "/listing/" in (url or "").lower()
                    and status_code in {404, 410}
                ):
                    log_event(
                        log,
                        "scrape.catalog.poshmark_http_not_found",
                        level="info",
                        status_code=status_code,
                    )
                    return _pack(None, None, None, "catalog")
            if status == "blocked":
                log_event(log, "scrape.blocked", level="warning", status="blocked")
                blocked_status = "blocked"
            elif status == "proxy_auth_required":
                log_event(log, "scrape.blocked", level="warning", status="proxy_auth_required", status_code=407)
                blocked_status = "blocked"  # тоже считаем блокировкой
            elif status == "proxy_error":
                log_event(log, "scrape.proxy_error", level="warning", status="proxy_error")
            elif status in {"transport_error", "timeout"} and (
                d.endswith("therealreal.com") or d.endswith("theluxurycloset.com")
            ):
                # для trr/tlc сетевой таймаут обычно эквивалентен антибот-блокировке
                blocked_status = "blocked"
                log_event(log, "scrape.blocked.soft_network", level="warning", status=status, domain=d)
        return _pack(None, None, None, blocked_status)  # возвращаем "blocked" если есть

    # проверяем, не заблокирована ли страница (даже если soup получен)
    blocked_hint = False
    if diagnosis:
        is_blocked = diagnosis.get("status") in {"blocked", "proxy_auth_required"}

        if is_blocked:
            markers = diagnosis.get("found_markers", {})
            if d.endswith("therealreal.com") or d.endswith("theluxurycloset.com"):
                blocked_hint = True
                log_event(
                    log,
                    "scrape.blocked.soft_override",
                    level="warning",
                    status=(diagnosis.get("status") if diagnosis else None),
                    markers=list(markers.keys()) if markers else None,
                )
            else:
                log_event(
                    log,
                    "scrape.blocked",
                    level="warning",
                    status=(diagnosis.get("status") if diagnosis else None),
                    markers=list(markers.keys()) if markers else None,
                )
                return _pack(None, None, None, "blocked")

    # извлекаем condition/country (best-effort) один раз на основе HTML
    if include_meta:
        try:
            from scrapers.universal import extract_condition_and_country
            condition, country = extract_condition_and_country(soup)
        except Exception:
            condition, country = None, None

    # сначала пробуем специфичные парсеры (eBay в приоритете - все региональные домены)
    result = None
    if is_ebay_domain(d):
        result = scrape_ebay(soup)
        if result and ebay_api_partial:
            api_title, api_price, api_currency, api_status, api_condition, api_country = ebay_api_partial
            merged_title = result[0] or api_title
            merged_price = result[1] if result[1] is not None else api_price
            merged_currency = result[2] or api_currency
            merged_status = result[3]
            if not merged_status or str(merged_status).strip().lower() in {"unknown", "none"}:
                merged_status = api_status
            result = (merged_title, merged_price, merged_currency, merged_status)
            if include_meta:
                if not condition:
                    condition = api_condition
                if not country:
                    country = api_country
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="ebay")
    elif d.endswith("poshmark.com"):
        result = scrape_poshmark(soup)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="poshmark")
    elif d.endswith("vestiairecollective.com"):
        result = scrape_vestiaire(soup)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="vestiaire")
    elif d.endswith("rebag.com"):
        result = scrape_rebag(soup)
    elif d.endswith("theluxurycloset.com"):
        result = scrape_tlc(soup, diagnosis, url)
        if result and result[3] == "redirect_mismatch":
            # не подмешиваем универсальную цену при несоответствии product id
            return _pack(result[0], None, None, None, condition, country)
        if result and _is_sold_status(result[3]) and result[1] is None:
            return _pack(result[0], None, None, result[3], condition, country)
        if result and result[1] is None and not _is_sold_status(result[3]):
            # если получили урезанный html без цены, пробуем прямой fallback без playwright
            try:
                retry_soup = fetch_html_with_stealth_headers(url)
                if retry_soup is not None:
                    retry_result = scrape_tlc(
                        retry_soup,
                        {"status": "ok", "content_size": len(str(retry_soup)), "proxy_used": ""},
                        url,
                    )
                    if retry_result and (retry_result[1] is not None or _is_sold_status(retry_result[3])):
                        result = retry_result
                        log_event(
                            log,
                            "scrape.tlc.retry_price_ok",
                            level="info",
                            price=retry_result[1],
                            status=retry_result[3],
                        )
                    else:
                        log_event(log, "scrape.tlc.retry_price_empty", level="warning")
            except Exception as e:
                log_exception(log, "scrape.tlc.retry_price_error", e, level="warning")
    elif d.endswith("fashionphile.com"):
        result = scrape_fashionphile(soup)
        if result and _is_sold_status(result[3]):
            # на fashionphile для sold out не подмешиваем цены fallback-источников
            return _pack(result[0], None, None, result[3], condition, country)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="fashionphile")
    elif d.endswith("jolicloset.com"):
        result = scrape_jolicloset(soup)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="jolicloset")
    elif d.endswith("yoogiscloset.com"):
        result = scrape_yoogiscloset(soup)
        if result and _is_sold_status(result[3]):
            return _pack(result[0], result[1], result[2], result[3], condition, country)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="yoogiscloset")
    elif d.endswith("therealreal.com"):
        result = scrape_therealreal(soup)
        if result and str(result[3]).strip().lower() == "blocked":
            return _pack(None, None, None, "blocked", condition, country)
        if result and result[3] == "catalog":
            return _pack(result[0], result[1], result[2], result[3], condition, country)
        if result and _is_sold_status(result[3]) and result[1] is None:
            return _pack(result[0], None, None, result[3], condition, country)
        if result and result[1] is None:
            # для trr не делаем прямой stealth retry, чтобы не усиливать anti-bot блокировки
            log_event(log, "scrape.therealreal.no_price", level="warning", skip_universal=False, stealth_retry=False)
    elif d.endswith("celebrityowned.com"):
        result = scrape_celebrityowned(soup)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="celebrityowned")
    elif d.endswith("aretrotale.com"):
        result = scrape_aretrotale(soup)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="aretrotale")
    elif d.endswith("dallasdesignerhandbags.com"):
        result = scrape_dallasdesignerhandbags(soup)
        if result and _is_sold_status(result[3]):
            # не подмешиваем цену fallback-ами для sold страниц dallasdesignerhandbags
            return _pack(result[0], None, None, result[3], condition, country)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="dallasdesignerhandbags")
    elif d.endswith("popchill.com"):
        result = scrape_popchill(soup)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="popchill")
    elif d.endswith("designerexchange.com"):
        result = scrape_designerexchange(soup)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="designerexchange")
    elif d.endswith("annsfabulousfinds.com"):
        result = scrape_annsfabulousfinds(soup)
        if result[1] is None:
            log_event(log, "scrape.parser.no_price", level="debug", parser="annsfabulousfinds")
    # если специфичный парсер не нашел цену, используем универсальный
    if result and result[1] is not None:
        return _pack(result[0], result[1], result[2], result[3], condition, country)

    # для сайтов с js-рендерингом всегда пробуем универсальный парсер как fallback
    js_sites = [
        "poshmark.com", "vestiairecollective.com",
        "fashionphile.com", "therealreal.com", "jolicloset.com",
        "yoogiscloset.com", "celebrityowned.com", "aretrotale.com",
        "dallasdesignerhandbags.com", "popchill.com", "designerexchange.com",
        "annsfabulousfinds.com"
    ]
    if is_ebay_domain(d) or any(d.endswith(site) for site in js_sites):
        log_event(log, "scrape.universal.try", level="debug")
        universal_result = scrape_universal(soup)
        # объединяем результаты: берем лучшее из обоих
        title = result[0] if result and result[0] else universal_result[0]
        price = result[1] if result and result[1] is not None else universal_result[1]
        currency = result[2] if result and result[2] else universal_result[2]
        status = result[3] if result and result[3] else universal_result[3]
        title_low = (title or "").strip().lower()
        blocked_title_markers = (
            "access denied",
            "access to this page has been denied",
            "pardon the interruption",
            "just a moment",
            "checking your browser",
            "attention required",
            "request blocked",
            "px-captcha",
            "perimeterx",
        )
        looks_like_block_page = (not title_low) or any(marker in title_low for marker in blocked_title_markers)
        if blocked_hint and looks_like_block_page and price is None and (not status or str(status).strip().lower() in {
            "unknown",
            "none",
        }):
            status = "blocked"
        if price is None:
            log_event(log, "scrape.universal.no_price", level="debug")
        return _pack(title, price, currency, status, condition, country)

    universal_result = scrape_universal(soup)
    # объединяем результаты: берем лучшее из обоих
    title = result[0] if result and result[0] else universal_result[0]
    price = result[1] if result and result[1] is not None else universal_result[1]
    currency = result[2] if result and result[2] else universal_result[2]
    status = result[3] if result and result[3] else universal_result[3]
    title_low = (title or "").strip().lower()
    blocked_title_markers = (
        "access denied",
        "access to this page has been denied",
        "pardon the interruption",
        "just a moment",
        "checking your browser",
        "attention required",
        "request blocked",
        "px-captcha",
        "perimeterx",
    )
    looks_like_block_page = (not title_low) or any(marker in title_low for marker in blocked_title_markers)
    if blocked_hint and looks_like_block_page and price is None and (not status or str(status).strip().lower() in {
        "unknown",
        "none",
    }):
        status = "blocked"

    return _pack(title, price, currency, status, condition, country)





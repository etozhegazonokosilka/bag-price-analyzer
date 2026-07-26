"""
базовые функции для парсинга страниц"""
import os
import threading
import time
import requests
from bs4 import BeautifulSoup
from typing import Optional, Tuple

from config import USE_RENDER
from utils.domain import domain_of
from utils.proxy_manager import get_proxy_manager
from utils.debug_http import diagnose_response, save_debug_html
from utils.transport import fetch_url
from utils.zenrows import zenrows_enabled_for_url, fetch_url_via_zenrows
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


# ограничиваем параллельный playwright, иначе на windows или в многопотоке браузер часто падает
# типичные ошибки: TargetClosedError и CancelledError
def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name, "").strip()
        v = int(raw) if raw else int(default)
    except Exception:
        v = int(default)
    return max(1, v)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name, "").strip()
        v = float(raw) if raw else float(default)
    except Exception:
        v = float(default)
    return max(0.0, v)


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}

# по умолчанию даем немного параллелизма, иначе очередь на Playwright легко выбивает общий таймаут API
_PLAYWRIGHT_MAX_CONCURRENCY = _env_int("PLAYWRIGHT_MAX_CONCURRENCY", 3)
_PLAYWRIGHT_SEMAPHORE = threading.BoundedSemaphore(_PLAYWRIGHT_MAX_CONCURRENCY)

# режим "1 ip = 1 запрос" для therealreal
_TRR_SERIAL_MODE = _env_flag("TRR_SERIAL_MODE", "1")
_TRR_SERIAL_LOCK = threading.Lock()
_TRR_ROTATE_BEFORE_REQUEST = _env_flag("TRR_ROTATE_BEFORE_REQUEST", "0")
_TRR_ROTATE_URL = os.getenv("TRR_ROTATE_URL", "").strip()
_TRR_ROTATE_TIMEOUT_SEC = _env_float("TRR_ROTATE_TIMEOUT_SEC", 8.0)
_TRR_ROTATE_WAIT_SEC = _env_float("TRR_ROTATE_WAIT_SEC", 1.5)
_TRR_ROTATE_MIN_INTERVAL_SEC = _env_float("TRR_ROTATE_MIN_INTERVAL_SEC", 42.0)
_TRR_ROTATE_RETRY_WAIT_SEC = _env_float("TRR_ROTATE_RETRY_WAIT_SEC", 42.0)
_TRR_ROTATE_MAX_ATTEMPTS = _env_int("TRR_ROTATE_MAX_ATTEMPTS", 2)
_TRR_ROTATE_ENFORCE_INTERVAL = _env_flag("TRR_ROTATE_ENFORCE_INTERVAL", "1")
_TRR_ROTATE_LOCK = threading.Lock()
_TRR_LAST_ROTATE_TS = 0.0
_TRR_DISABLE_RENDER = _env_flag("TRR_DISABLE_RENDER", "1")
_EBAY_SKIP_RENDER = _env_flag("EBAY_SKIP_RENDER", "1")
_EBAY_FETCH_CONNECT_TIMEOUT_SEC = _env_float("EBAY_FETCH_CONNECT_TIMEOUT_SEC", 4.0)
_EBAY_FETCH_READ_TIMEOUT_SEC = _env_float("EBAY_FETCH_READ_TIMEOUT_SEC", 8.0)
_EBAY_FETCH_READ_TIMEOUT_PROXY_SEC = _env_float("EBAY_FETCH_READ_TIMEOUT_PROXY_SEC", 10.0)
_EBAY_RENDER_TIMEOUT_MS = _env_int("EBAY_RENDER_TIMEOUT_MS", 9000)
_EBAY_RENDER_WAIT_UNTIL = (os.getenv(
    "EBAY_RENDER_WAIT_UNTIL",
    "domcontentloaded",
).strip().lower() or "domcontentloaded")
_EBAY_RENDER_RETRY_NETWORKIDLE = _env_flag("EBAY_RENDER_RETRY_NETWORKIDLE", "0")
_FAST_SKIP_RENDER = _env_flag("FAST_SKIP_RENDER", "1")
_FAST_FETCH_MAX_RETRIES = _env_int("FAST_FETCH_MAX_RETRIES", 1)
_FAST_FETCH_CONNECT_TIMEOUT_SEC = _env_float("FAST_FETCH_CONNECT_TIMEOUT_SEC", 3.0)
_FAST_FETCH_READ_TIMEOUT_SEC = _env_float("FAST_FETCH_READ_TIMEOUT_SEC", 8.0)
_FAST_FETCH_READ_TIMEOUT_PROXY_SEC = _env_float("FAST_FETCH_READ_TIMEOUT_PROXY_SEC", 10.0)
_PROXY_CONNECT_TIMEOUT_FLOOR = _env_float("PROXY_CONNECT_TIMEOUT_FLOOR", 10.0)
_ROTATING_FETCH_CONNECT_TIMEOUT_SEC = _env_float("ROTATING_FETCH_CONNECT_TIMEOUT_SEC", 30.0)
_ROTATING_FETCH_READ_TIMEOUT_SEC = _env_float("ROTATING_FETCH_READ_TIMEOUT_SEC", 30.0)
_TRR_FORCE_ZENROWS = _env_flag("TRR_FORCE_ZENROWS", "1")
_TLC_FORCE_ZENROWS = _env_flag("TLC_FORCE_ZENROWS", "1")
_JOLICLOSET_FORCE_ZENROWS = _env_flag("JOLICLOSET_FORCE_ZENROWS", "1")
_HAS_ZENROWS_KEY = bool(os.getenv("ZENROWS_API_KEY", "").strip())
if _EBAY_RENDER_WAIT_UNTIL not in {"domcontentloaded", "load", "networkidle"}:
    _EBAY_RENDER_WAIT_UNTIL = "domcontentloaded"


def _maybe_rotate_trr_ip() -> None:
    # перед запросом trr меняем ip по ссылке ротации
    if not _TRR_ROTATE_BEFORE_REQUEST or not _TRR_ROTATE_URL:
        return

    global _TRR_LAST_ROTATE_TS
    with _TRR_ROTATE_LOCK:
        now = time.time()
        interval_left = _TRR_ROTATE_MIN_INTERVAL_SEC - (now - _TRR_LAST_ROTATE_TS)
        if interval_left > 0:
            if _TRR_ROTATE_ENFORCE_INTERVAL:
                # у astro минимальный интервал смены ip 40 секунд
                log_event(
                    log,
                    "proxy.trr.rotate.wait_interval",
                    level="info",
                    wait_sec=round(interval_left, 2),
                )
                time.sleep(interval_left)
            else:
                log_event(
                    log,
                    "proxy.trr.rotate.skip_interval",
                    level="warning",
                    wait_sec=round(interval_left, 2),
                )
                return

        for attempt in range(_TRR_ROTATE_MAX_ATTEMPTS):
            session = requests.Session()
            session.trust_env = False
            try:
                response = session.get(
                    _TRR_ROTATE_URL,
                    timeout=(3, _TRR_ROTATE_TIMEOUT_SEC),
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if 200 <= response.status_code < 400:
                    _TRR_LAST_ROTATE_TS = time.time()
                    log_event(
                        log,
                        "proxy.trr.rotate.ok",
                        level="info",
                        status_code=response.status_code,
                        attempt=f"{attempt + 1}/{_TRR_ROTATE_MAX_ATTEMPTS}",
                    )
                    if _TRR_ROTATE_WAIT_SEC > 0:
                        time.sleep(_TRR_ROTATE_WAIT_SEC)
                    return

                if response.status_code == 429 and attempt < (_TRR_ROTATE_MAX_ATTEMPTS - 1):
                    wait_retry = max(_TRR_ROTATE_RETRY_WAIT_SEC, _TRR_ROTATE_MIN_INTERVAL_SEC)
                    log_event(
                        log,
                        "proxy.trr.rotate.rate_limited",
                        level="warning",
                        status_code=response.status_code,
                        wait_sec=round(wait_retry, 2),
                        attempt=f"{attempt + 1}/{_TRR_ROTATE_MAX_ATTEMPTS}",
                    )
                    time.sleep(wait_retry)
                    continue

                log_event(
                    log,
                    "proxy.trr.rotate.bad_status",
                    level="warning",
                    status_code=response.status_code,
                    attempt=f"{attempt + 1}/{_TRR_ROTATE_MAX_ATTEMPTS}",
                )
                return
            except Exception as e:
                if attempt < (_TRR_ROTATE_MAX_ATTEMPTS - 1):
                    wait_retry = max(_TRR_ROTATE_RETRY_WAIT_SEC, _TRR_ROTATE_MIN_INTERVAL_SEC)
                    log_exception(
                        log,
                        "proxy.trr.rotate.error_retry",
                        e,
                        level="warning",
                        wait_sec=round(wait_retry, 2),
                        attempt=f"{attempt + 1}/{_TRR_ROTATE_MAX_ATTEMPTS}",
                    )
                    time.sleep(wait_retry)
                    continue
                log_exception(log, "proxy.trr.rotate.error", e, level="warning")
                return
            finally:
                session.close()


def _has_domain_price_signals(domain: str, soup: BeautifulSoup, content: str) -> bool:
    """убеждаемся, что HTML содержит ценовые сигналы"""
    d = (domain or "").lower()

    if d.endswith("therealreal.com"):
        selectors = (
            '[data-testid="product-price/final"]',
            '[data-testid*="product-price/final"]',
            '[class*="product-price-info__final-price"]',
            '[class*="product-price-info__reduced-price"]',
        )
        if any(soup.select_one(sel) for sel in selectors):
            return True

        for attrs in (
            {"property": "product:price:amount"},
            {"property": "og:price:amount"},
            {"itemprop": "price"},
        ):
            meta = soup.find("meta", attrs=attrs)
            if meta and (meta.get("content") or "").strip():
                return True

        html_low = (content or "").lower()
        return any(
            token in html_low
            for token in (
                'data-testid="product-price/final"',
                "data-testid='product-price/final'",
                "product-price-info__final-price",
                "product-price-info__reduced-price",
            )
        )

    if d.endswith("theluxurycloset.com"):
        selectors = (
            '[class*="ProductPriceV2__newProductPrice"]',
            '[class*="ProductPriceV2__newPriceContent"] [class*="newProductPrice"]',
            '[class*="ProductPriceV2"] [class*="newProductPrice"]',
            '[class*="newProductPrice"]',
        )
        if any(soup.select_one(sel) for sel in selectors):
            return True

        for attrs in (
            {"property": "product:price:amount"},
            {"property": "og:price:amount"},
            {"itemprop": "price"},
        ):
            meta = soup.find("meta", attrs=attrs)
            if meta and (meta.get("content") or "").strip():
                return True

        html_low = (content or "").lower()
        if "productpricev2__newproductprice" in html_low:
            return True
        if '"@type":"offer"' in html_low and '"price"' in html_low:
            return True
        return False

    if d.endswith("jolicloset.com"):
        selectors = (
            "#product h1 span",
            "#product [class*='price']",
            "meta[property='product:price:amount']",
            "meta[property='og:price:amount']",
        )
        if any(soup.select_one(sel) for sel in selectors):
            return True

        html_low = (content or "").lower()
        return any(
            token in html_low
            for token in (
                'property="product:price:amount"',
                'property="og:price:amount"',
                "addcartbutton",
                'id="product"',
            )
        )

    return True


def fetch_html(
    url: str,
    max_retries: int = 1,
    _trr_serialized: bool = False,
    allow_zenrows: bool = True,
    force_zenrows: bool = False,
    fast_mode: bool = False,
) -> Tuple[Optional[BeautifulSoup], Optional[dict]]:
    """
    получает html страницы и возвращает beautifulsoup объект с диагностикой

    аргументы:
        url: URL для загрузки
        max_retries: максимальное количество попыток в текущем режиме
        force_zenrows: принудительно использовать zenrows для этого запроса

    возвращает:
        (soup, diagnosis) - BeautifulSoup объект и диагностика, или (None, diagnosis) при ошибке
"""
    d = domain_of(url)
    is_therealreal = d.endswith("therealreal.com")
    is_theluxurycloset = d.endswith("theluxurycloset.com")
    is_jolicloset = d.endswith("jolicloset.com")
    is_ebay = d.endswith("ebay.com")
    manager = get_proxy_manager()
    force_zenrows_for_proxy_pressure = False
    if _HAS_ZENROWS_KEY:
        try:
            force_zenrows_for_proxy_pressure = manager.should_force_zenrows_for_url(url, domain=d)
        except Exception:
            force_zenrows_for_proxy_pressure = False
    if is_therealreal and _TRR_FORCE_ZENROWS and not _HAS_ZENROWS_KEY:
        log_event(
            log,
            "scrape.trr.zenrows_missing_key",
            level="warning",
            url=url,
        )
    if is_theluxurycloset and _TLC_FORCE_ZENROWS and not _HAS_ZENROWS_KEY:
        log_event(
            log,
            "scrape.tlc.zenrows_missing_key",
            level="warning",
            url=url,
        )
    if is_jolicloset and _JOLICLOSET_FORCE_ZENROWS and not _HAS_ZENROWS_KEY:
        log_event(
            log,
            "scrape.jolicloset.zenrows_missing_key",
            level="warning",
            url=url,
        )
    force_zenrows_for_trr = _TRR_FORCE_ZENROWS and is_therealreal and _HAS_ZENROWS_KEY
    force_zenrows_for_tlc = _TLC_FORCE_ZENROWS and is_theluxurycloset and _HAS_ZENROWS_KEY
    force_zenrows_for_jolicloset = _JOLICLOSET_FORCE_ZENROWS and is_jolicloset and _HAS_ZENROWS_KEY
    if force_zenrows_for_trr:
        log_event(log, "scrape.trr.zenrows_forced", level="info", domain=d)
    if force_zenrows_for_tlc:
        log_event(log, "scrape.tlc.zenrows_forced", level="info", domain=d)
    if force_zenrows_for_jolicloset:
        log_event(log, "scrape.jolicloset.zenrows_forced", level="info", domain=d)
    if force_zenrows_for_proxy_pressure:
        log_event(log, "scrape.zenrows.proxy_pressure_forced", level="warning", domain=d)
    force_zenrows_direct = bool(force_zenrows and _HAS_ZENROWS_KEY)
    if force_zenrows and not _HAS_ZENROWS_KEY:
        log_event(
            log,
            "scrape.zenrows.force_missing_key",
            level="warning",
            domain=d,
            url=url,
        )
    if force_zenrows_direct:
        log_event(
            log,
            "scrape.zenrows.force_direct",
            level="info",
            domain=d,
        )
    use_zenrows = (
        force_zenrows_direct
        or force_zenrows_for_trr
        or force_zenrows_for_tlc
        or force_zenrows_for_jolicloset
        or force_zenrows_for_proxy_pressure
        or (allow_zenrows and zenrows_enabled_for_url(url))
    )

    if is_therealreal and _TRR_SERIAL_MODE and not fast_mode and not _trr_serialized and not use_zenrows:
        # для trr держим строго последовательный режим: один запрос в один момент времени
        with _TRR_SERIAL_LOCK:
            _maybe_rotate_trr_ip()
            return fetch_html(
                url,
                max_retries=max_retries,
                _trr_serialized=True,
                allow_zenrows=allow_zenrows,
                force_zenrows=force_zenrows,
                fast_mode=fast_mode,
            )

    if use_zenrows:
        # для выбранных доменов забираем html через zenrows (обходит антибот и не жрет наш proxy-трафик)
        status_code, content, final_url, error, meta = fetch_url_via_zenrows(url)
        if error:
            return None, {"status": "transport_error", "error": error, "zenrows": meta}

        diagnosis = diagnose_response(
            url=url,
            status_code=status_code,
            final_url=final_url,
            content=content,
            content_type="",
            proxy_used="zenrows",
            headers={},
        )
        diagnosis["zenrows"] = meta

        if status_code != 200 or not content:
            return None, diagnosis

        soup = BeautifulSoup(content, "html.parser")

        # если пришел "голый" html без цены (часто это значит, что нужен js render),
        # пробуем второй раз с ожиданием ключевого price-элемента
        if not _has_domain_price_signals(d, soup, content):
            wait_for = None
            if d.endswith("therealreal.com"):
                wait_for = os.getenv("ZENROWS_WAIT_FOR_TRR", "").strip() or '[data-testid="product-price/final"]'
            elif d.endswith("theluxurycloset.com"):
                wait_for = os.getenv(
                    "ZENROWS_WAIT_FOR_TLC",
                    "",
                ).strip() or '[class*="ProductPriceV2__newProductPrice"]'
            elif d.endswith("jolicloset.com"):
                wait_for = os.getenv("ZENROWS_WAIT_FOR_JOLICLOSET", "").strip() or "#product h1 span"

            if wait_for:
                status_code2, content2, final_url2, error2, meta2 = fetch_url_via_zenrows(url, wait_for=wait_for)
                if not error2 and status_code2 == 200 and content2:
                    soup2 = BeautifulSoup(content2, "html.parser")
                    if _has_domain_price_signals(d, soup2, content2):
                        diagnosis2 = diagnose_response(
                            url=url,
                            status_code=status_code2,
                            final_url=final_url2,
                            content=content2,
                            content_type="",
                            proxy_used="zenrows",
                            headers={},
                        )
                        diagnosis2["zenrows"] = {**meta, "retry_wait_for": wait_for, "retry": meta2}
                        return soup2, diagnosis2

        return soup, diagnosis

    is_trr_or_tlc = d.endswith("therealreal.com") or d.endswith("theluxurycloset.com")
    is_rotating_only = False
    try:
        # для доменов rotating-only не делаем direct fallback на requests-уровне
        is_rotating_only = bool(manager._is_rotating_only_domain(url))  # type: ignore[attr-defined]
    except Exception:
        is_rotating_only = d.endswith("therealreal.com")

    # важно, что в fast pass стараемся обходиться без render
    render_allowed = not (fast_mode and _FAST_SKIP_RENDER)

    # определяем, нужен ли обязательный рендер для текущего домена
    needs_render = render_allowed and USE_RENDER and (
        d.endswith("poshmark.com")
        or d.endswith("vestiairecollective.com")
        or (is_ebay and not _EBAY_SKIP_RENDER)
    )

    # определяем сложность домена для таймаутов и fallback
    is_hard = manager._is_hard_domain(url) if hasattr(manager, "_is_hard_domain") else False
    is_strict = manager._is_strict_domain(url) if hasattr(manager, "_is_strict_domain") else False
    is_hard_or_strict = is_hard or is_strict

    # cf-домены с Cloudflare или captcha
    # список: fashionphile, rebag, designerexchange, aretrotale, celebrityowned и dallasdesignerhandbags
    is_cf_domain = d in [
        "fashionphile.com",
        "rebag.com",
        "shop.rebag.com",
        "designerexchange.com",
        "aretrotale.com",
        "celebrityowned.com",
        "dallasdesignerhandbags.com",
        "annsfabulousfinds.com",
    ]

    # ebay и poshmark: сначала Playwright (DIRECT, потом STATIC), затем requests
    is_ebay_or_poshmark = d.endswith("poshmark.com") or (is_ebay and not _EBAY_SKIP_RENDER)

    # для ebay/poshmark и hard/cf доменов: сначала Playwright, затем requests
    if render_allowed and (is_ebay_or_poshmark or is_hard_or_strict or is_cf_domain) and not (
        is_therealreal and _TRR_DISABLE_RENDER
    ):
        # ограничиваем число попыток рендера, чтобы не зависать на Cloudflare/403
        render_retries = _env_int("RENDER_MAX_RETRIES", 1)
        if d.endswith("theluxurycloset.com"):
            render_retries = max(render_retries, 2)
        if is_therealreal:
            render_retries = _env_int("TRR_RENDER_RETRIES", 1)
        rendered, diagnosis = fetch_html_rendered(url, None, max_retries=render_retries)
        if rendered is not None:
            return rendered, diagnosis

        # если рендер дал блокировку:
        # - для therealreal/TLC все равно пробуем requests fallback (часто даёт нормальный HTML);
        # - для остальных доменов возвращаем блокировку сразу
        if diagnosis and diagnosis.get("status") in {"blocked", "proxy_auth_required"}:
            if not (d.endswith("therealreal.com") or d.endswith("theluxurycloset.com")):
                return None, diagnosis
            log_event(
                log,
                "http.render.blocked_fallback_requests",
                level="warning",
                transport="playwright",
                status=diagnosis.get("status"),
            )

        log_event(
            log,
            "http.render.fallback",
            level="warning",
            transport="playwright",
            status=(diagnosis or {}).get("status") if diagnosis else None,
        )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.google.com/",
        "DNT": "1",
    }

    last_diagnosis: dict | None = None
    effective_max_retries = max(1, int(max_retries))
    if fast_mode:
        effective_max_retries = min(effective_max_retries, _FAST_FETCH_MAX_RETRIES)

    # делаем попытки загрузки с учётом лимита повторов
    for retry in range(effective_max_retries):
        proxy_info = None
        proxies = None
        proxy_str = ""
        proxy_type = "direct"

        try:
            if is_therealreal and _TRR_ROTATE_BEFORE_REQUEST and retry > 0:
                # для trr: если предыдущая попытка была заблокирована/упала, пробуем ещё раз с новым ip
                _maybe_rotate_trr_ip()

            # direct-first управляется через DIRECT_FIRST_DOMAINS в proxy_manager
            use_direct_first = False
            if retry == 0:
                try:
                    use_direct_first = bool(
                        # метод относится к внутреннему интерфейсу ProxyManager
                        manager._is_direct_first_domain(url)  # type: ignore[attr-defined]
                    )
                except Exception:
                    use_direct_first = d.endswith("theluxurycloset.com")
            if use_direct_first:
                proxy_info = None
                proxies = None
                log_event(
                    log,
                    "http.fetch.direct_first",
                    transport="requests",
                    attempt=f"{retry + 1}/{effective_max_retries}",
                    domain=d,
                )
            else:
                proxy_info = manager.get_proxy_for_url(url, retry_count=retry)
                proxies = proxy_info.to_requests_dict() if proxy_info else None
                if proxies == {}:
                    proxies = None

            proxy_str = ""
            if proxies:
                # для https url берём https-прокси, чтобы корректно работал rotating split scheme
                proxy_key = "https" if url.startswith("https://") else "http"
                proxy_str = proxies.get(proxy_key) or proxies.get("http", "")
            proxy_type = proxy_info.proxy_type if proxy_info else "direct"

            if (
                proxy_info
                and proxy_info.proxy_type == "rotating"
                and not (is_therealreal and _TRR_ROTATE_BEFORE_REQUEST)
            ):
                try:
                    manager.maybe_rotate_proxy(proxy_info, domain=d, url=url)
                except Exception as e:
                    log_exception(log, "proxy.rotating.rotate.unhandled", e, level="warning", domain=d, url=url)

            # таймауты запроса: (connect, read)
            if is_ebay:
                connect_timeout = max(1.0, float(_EBAY_FETCH_CONNECT_TIMEOUT_SEC))
                if fast_mode:
                    connect_timeout = max(1.0, float(_FAST_FETCH_CONNECT_TIMEOUT_SEC))
                    if proxy_info and proxy_info.proxy_type != "direct":
                        read_timeout = max(1.0, float(_FAST_FETCH_READ_TIMEOUT_PROXY_SEC))
                    else:
                        read_timeout = max(1.0, float(_FAST_FETCH_READ_TIMEOUT_SEC))
                    timeout_tuple = (connect_timeout, read_timeout)
                elif proxy_info and proxy_info.proxy_type != "direct":
                    read_timeout = max(1.0, float(_EBAY_FETCH_READ_TIMEOUT_PROXY_SEC))
                else:
                    read_timeout = max(1.0, float(_EBAY_FETCH_READ_TIMEOUT_SEC))
                if not fast_mode:
                    timeout_tuple = (connect_timeout, read_timeout)
            else:
                if fast_mode:
                    connect_timeout = max(1.0, float(_FAST_FETCH_CONNECT_TIMEOUT_SEC))
                    if proxy_info and proxy_info.proxy_type != "direct":
                        read_timeout = max(1.0, float(_FAST_FETCH_READ_TIMEOUT_PROXY_SEC))
                    else:
                        read_timeout = max(1.0, float(_FAST_FETCH_READ_TIMEOUT_SEC))
                else:
                    connect_timeout = 5  # фиксированный connect для "обычных" доменов
                    base_read_timeout = 14 if is_hard_or_strict else 10
                    if is_trr_or_tlc:
                        base_read_timeout = max(base_read_timeout, 16)
                    if proxy_info and proxy_info.proxy_type != "direct":
                        base_read_timeout += 3
                    max_read_timeout = 24 if is_trr_or_tlc else 18
                    read_timeout = min(max_read_timeout, base_read_timeout + retry * 2)
                timeout_tuple = (connect_timeout, read_timeout)

            if proxy_info and proxy_info.proxy_type != "direct":
                timeout_tuple = (
                    max(float(timeout_tuple[0]), float(_PROXY_CONNECT_TIMEOUT_FLOOR)),
                    float(timeout_tuple[1]),
                )

            if proxy_info and proxy_info.proxy_type == "rotating":
                timeout_tuple = (
                    max(float(timeout_tuple[0]), float(_ROTATING_FETCH_CONNECT_TIMEOUT_SEC)),
                    max(float(timeout_tuple[1]), float(_ROTATING_FETCH_READ_TIMEOUT_SEC)),
                )

            if retry > 0:
                log_event(
                    log,
                    "http.fetch.retry",
                    level="warning",
                    transport="requests",
                    attempt=f"{retry + 1}/{effective_max_retries}",
                    proxy_type=proxy_type,
                    proxy=proxy_str if proxies else None,
                )

            log_event(
                log,
                "http.fetch.attempt",
                transport="requests",
                attempt=f"{retry + 1}/{effective_max_retries}",
                timeout_connect=timeout_tuple[0],
                timeout_read=timeout_tuple[1],
                proxy_type=proxy_type,
                proxy=proxy_str if proxies else None,
            )

            proxy_url_for_transport = proxy_str if proxies else None
            status_code, content, final_url, error = fetch_url(
                url=url,
                proxy_url=proxy_url_for_transport,
                headers=headers,
                timeout=timeout_tuple,
            )

            if error:
                log_event(
                    log,
                    "http.fetch.transport_error",
                    level="warning",
                    transport="requests",
                    attempt=f"{retry + 1}/{effective_max_retries}",
                    proxy_type=proxy_type,
                    proxy=proxy_str if proxies else None,
                    error=error,
                )
                # для hard/strict сайтов делаем аварийный direct fallback,
                # если прокси-запрос упал по сети/таймауту
                tried_direct_fallback = False
                if proxy_info and proxy_info.proxy_type != "direct":
                    manager.mark_proxy_bad(
                        url,
                        retry,
                        f"Transport: {error}",
                        transport="requests",
                        proxy_info=proxy_info,
                    )

                if proxy_info and proxy_info.proxy_type != "direct" and not is_rotating_only and not fast_mode:
                    tried_direct_fallback = True
                    direct_status, direct_content, direct_final_url, direct_error = fetch_url(
                        url=url,
                        proxy_url=None,
                        headers=headers,
                        timeout=timeout_tuple,
                    )
                    if not direct_error and direct_status == 200 and direct_content:
                        direct_diag = diagnose_response(
                            url=url,
                            status_code=direct_status,
                            final_url=direct_final_url,
                            content=direct_content,
                            content_type="",
                            proxy_used="",
                            headers={},
                        )
                        if direct_diag.get("status") not in {"blocked", "proxy_auth_required"}:
                            soup = BeautifulSoup(direct_content, "html.parser")
                            return soup, direct_diag
                    else:
                        log_event(
                            log,
                            "http.fetch.direct_fallback_failed",
                            level="warning",
                            transport="requests",
                            attempt=f"{retry + 1}/{effective_max_retries}",
                            status_code=direct_status,
                            error=direct_error,
                        )

                if retry == effective_max_retries - 1:
                    # если direct fallback тоже не помог, возвращаем финальную диагностику
                    if tried_direct_fallback:
                        return None, {"status": "transport_error", "error": f"proxy+direct failed: {error}"}
                    return None, {"status": "transport_error", "error": error}
                continue

            diagnosis = diagnose_response(
                url=url,
                status_code=status_code,
                final_url=final_url,
                content=content,
                content_type="",
                proxy_used=proxy_str,
                headers={},
            )
            last_diagnosis = diagnosis

            markers = list((diagnosis.get("found_markers") or {}).keys())
            log_event(
                log,
                "http.fetch.response",
                transport="requests",
                attempt=f"{retry + 1}/{effective_max_retries}",
                status_code=status_code,
                status=diagnosis.get("status"),
                size_bytes=len(content) if content is not None else 0,
                markers=markers or None,
            )

            is_blocked = diagnosis.get("status") in {"blocked", "proxy_auth_required"}
            if is_blocked:
                saved = save_debug_html(url, content, f"blocked_{diagnosis.get('status')}")
                if saved:
                    log_event(log, "debug.html.saved", path=saved, reason=f"blocked_{diagnosis.get('status')}")

                if diagnosis.get("status") == "proxy_auth_required" or "403" in str(status_code):
                    if proxy_info and proxy_info.proxy_type != "direct":
                        manager.mark_proxy_bad(
                            url,
                            retry,
                            f"Proxy {diagnosis.get('status')}",
                            transport="requests",
                            proxy_info=proxy_info,
                        )

                if (
                    is_trr_or_tlc
                    and proxy_info
                    and proxy_info.proxy_type != "direct"
                    and not is_rotating_only
                    and not fast_mode
                ):
                    log_event(
                        log,
                        "http.fetch.blocked_direct_fallback",
                        level="warning",
                        transport="requests",
                        attempt=f"{retry + 1}/{effective_max_retries}",
                        blocked_status=diagnosis.get("status"),
                    )
                    direct_status, direct_content, direct_final_url, direct_error = fetch_url(
                        url=url,
                        proxy_url=None,
                        headers=headers,
                        timeout=timeout_tuple,
                    )
                    if not direct_error and direct_status == 200 and direct_content:
                        direct_diag = diagnose_response(
                            url=url,
                            status_code=direct_status,
                            final_url=direct_final_url,
                            content=direct_content,
                            content_type="",
                            proxy_used="",
                            headers={},
                        )
                        direct_soup = BeautifulSoup(direct_content, "html.parser")
                        direct_blocked = direct_diag.get("status") in {"blocked", "proxy_auth_required"}
                        if not direct_blocked:
                            return direct_soup, direct_diag
                        if _has_domain_price_signals(d, direct_soup, direct_content):
                            log_event(
                                log,
                                "http.fetch.blocked_direct_soft_accept",
                                level="warning",
                                transport="requests",
                                blocked_status=direct_diag.get("status"),
                            )
                            return direct_soup, direct_diag
                    else:
                        log_event(
                            log,
                            "http.fetch.blocked_direct_fallback_failed",
                            level="warning",
                            transport="requests",
                            attempt=f"{retry + 1}/{effective_max_retries}",
                            status_code=direct_status,
                            error=direct_error,
                        )

                if retry == effective_max_retries - 1:
                    return None, diagnosis
                continue

            if status_code != 200:
                if retry == effective_max_retries - 1:
                    return None, {"status": "http_error", "error": f"HTTP {status_code}"}
                continue

            soup = BeautifulSoup(content, "html.parser")

            # помечаем прокси как хороший
            if proxy_info and proxy_info.proxy_type != "direct":
                manager.mark_proxy_good(url, proxy_info)

            # если рендер включён и не обязателен, пробуем рендер-ветку
            if render_allowed and USE_RENDER and not needs_render and not (is_ebay and _EBAY_SKIP_RENDER):
                rendered, _ = fetch_html_rendered(url, headers)
                if rendered is not None:
                    return rendered, diagnosis

            return soup, diagnosis

        except requests.exceptions.ProxyError as e:
            log_exception(
                log,
                "http.fetch.proxy_error",
                e,
                level="warning",
                transport="requests",
                attempt=f"{retry + 1}/{effective_max_retries}",
                proxy_type=proxy_type,
                proxy=proxy_str if proxies else None,
            )
            if proxy_info and proxy_info.proxy_type != "direct":
                manager.mark_proxy_bad(
                    url,
                    retry,
                    f"ProxyError: {str(e)[:50]}",
                    transport="requests",
                    proxy_info=proxy_info,
                )
            if retry == effective_max_retries - 1:
                return None, {"status": "proxy_error", "error": str(e), "found_markers": {"proxy_error": str(e)}}
            time.sleep(1)
            continue

        except requests.exceptions.Timeout as e:
            log_exception(
                log,
                "http.fetch.timeout",
                e,
                level="warning",
                transport="requests",
                attempt=f"{retry + 1}/{effective_max_retries}",
                proxy_type=proxy_type,
                proxy=proxy_str if proxies else None,
            )
            if proxy_info and proxy_info.proxy_type != "direct":
                manager.mark_proxy_bad(url, retry, "Timeout", transport="requests", proxy_info=proxy_info)
            if retry == effective_max_retries - 1:
                return None, {"status": "timeout", "error": "превышено время ожидания"}
            time.sleep(1)
            continue

        except requests.exceptions.ConnectionError as e:
            log_exception(
                log,
                "http.fetch.connection_error",
                e,
                level="warning",
                transport="requests",
                attempt=f"{retry + 1}/{effective_max_retries}",
                proxy_type=proxy_type,
                proxy=proxy_str if proxies else None,
            )
            if proxy_info and proxy_info.proxy_type != "direct":
                manager.mark_proxy_bad(
                    url,
                    retry,
                    f"ConnectionError: {str(e)[:50]}",
                    transport="requests",
                    proxy_info=proxy_info,
                )
            if retry == effective_max_retries - 1:
                return None, {"status": "connection_error", "error": str(e)}
            time.sleep(1)
            continue

        except Exception as e:
            log_exception(
                log,
                "http.fetch.error",
                e,
                level="error",
                transport="requests",
                attempt=f"{retry + 1}/{effective_max_retries}",
                proxy_type=proxy_type,
                proxy=proxy_str if proxies else None,
            )
            if retry == effective_max_retries - 1:
                return None, {"status": "error", "error": str(e)}
            time.sleep(1)
            continue
        finally:
            if proxy_info and proxy_info.proxy_type == "rotating":
                try:
                    manager.release_proxy(proxy_info, domain=d, url=url, reason="requests_attempt_done")
                except Exception:
                    pass

    return None, last_diagnosis or {"status": "failed", "error": "не удалось загрузить страницу"}


def fetch_html_rendered(
    url: str,
    headers: dict | None,
    max_retries: int = 2,
) -> Tuple[Optional[BeautifulSoup], Optional[dict]]:
    """
    рендеринг страницы headless-браузером через playwright с обходом блокировок

    аргументы:
        url: URL для загрузки
        headers: HTTP заголовки (опционально)
        max_retries: максимальное количество попыток

    возвращает:
        (soup, diagnosis) - BeautifulSoup объект и диагностика
"""
    d = domain_of(url)
    manager = get_proxy_manager()
    is_slow_site = (
        d.endswith("poshmark.com")
        or d.endswith("vestiairecollective.com")
        or d.endswith("therealreal.com")
        or d.endswith("theluxurycloset.com")
    )
    is_vestiaire = d.endswith("vestiairecollective.com")
    is_ebay = d.startswith("ebay.") or d == "ebay"
    is_therealreal = d.endswith("therealreal.com")
    is_tlc = d.endswith("theluxurycloset.com")

    stealth_func = None
    stealth_class = None
    try:
        from playwright.sync_api import sync_playwright
        # playwright-stealth включен по умолчанию
        try:
            from playwright_stealth import stealth_sync as _stealth_sync
            # в некоторых версиях stealth_sync импортируется как модуль
            # если это так, пытаемся получить функцию внутри модуля
            if callable(_stealth_sync):
                stealth_func = _stealth_sync
            else:
                stealth_func = getattr(_stealth_sync, "stealth_sync", None)
        except ImportError:
            # fallback на другие варианты импорта
            try:
                from playwright_stealth import stealth as _stealth
                if callable(_stealth):
                    stealth_func = _stealth
                else:
                    stealth_func = getattr(_stealth, "stealth", None)
            except ImportError:
                try:
                    from playwright_stealth import Stealth
                    stealth_class = Stealth()
                    stealth_func = None
                except ImportError:
                    stealth_func = None
                    stealth_class = None
    except ImportError:
        log_event(
            log,
            'http.render.unavailable',
            level='warning',
            transport='playwright',
            hint='pip install playwright',
        )
        return None, {"status": "error", "error": "Playwright не установлен"}
    except Exception as e:
        log_exception(log, 'http.render.import_error', e, level='warning', transport='playwright')
        return None, {"status": "error", "error": str(e)}

    # пробуем несколько раз с разными прокси
    for retry in range(max_retries):
        proxy_info = None
        try:
            # получаем прокси для данной попытки
            proxy_info = manager.get_proxy_for_url(url, retry_count=retry)
            proxy_dict = None
            if proxy_info and proxy_info.proxy_type != "direct":
                proxy_dict = proxy_info.to_playwright_dict()

            if retry > 0:
                log_event(
                    log,
                    'http.render.retry',
                    level='warning',
                    transport='playwright',
                    attempt=f'{retry + 1}/{max_retries}',
                )
            # держим слот до конца попытки (включая запуск/навигацию/парсинг)
            with _PLAYWRIGHT_SEMAPHORE, sync_playwright() as p:
                try:
                    # настраиваем браузер для работы на Linux-сервере (Ubuntu) под root
                    # прокси ставится в context, а не в browser.launch
                    launch_options = {
                        'headless': True,  # обязательно для сервера без монитора
                        'args': [
                            "--no-sandbox",  # обязательно для root пользователя
                            "--disable-setuid-sandbox",  # отключаем setuid sandbox
                            "--disable-dev-shm-usage",  # важно для Docker/VPS - использует /tmp вместо /dev/shm
                            "--disable-accelerated-2d-canvas",  # отключаем ускорение 2D canvas
                            "--no-first-run",  # пропускаем первый запуск
                            "--disable-blink-features=AutomationControlled",  # скрываем автоматизацию
                        ],
                    }

                    browser = p.chromium.launch(**launch_options)
                except Exception as launch_error:
                    error_msg = str(launch_error)
                    if "Executable doesn't exist" in error_msg or "headless_shell" in error_msg:
                        log_event(
                            log,
                            'http.render.browser_missing',
                            level='error',
                            transport='playwright',
                            hint='python -m playwright install chromium',
                        )
                        return None, {"status": "error", "error": "Playwright не установлен"}
                    else:
                        log_exception(
                            log,
                            'http.render.launch_error',
                            launch_error,
                            level='warning',
                            transport='playwright',
                        )
                        if retry == max_retries - 1:
                            return None, {"status": "error", "error": str(launch_error)}
                        continue

                try:
                    # реалистичные параметры контекста для лучшего обхода защиты
                    user_agent_context = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    viewport_context = {"width": 1920, "height": 1080}
                    locale_context = "en-US"
                    timezone_context = "America/New_York"

                    # прокси должен быть в context, а не в browser.launch
                    context_options = {
                        "user_agent": user_agent_context,
                        "locale": locale_context,
                        "timezone_id": timezone_context,
                        "java_script_enabled": True,
                        "viewport": viewport_context,
                        "accept_downloads": False,
                        "permissions": [],  # блокируем запросы разрешений
                        "geolocation": None,  # отключаем геолокацию
                    }

                    # добавляем прокси в context если он есть
                    if proxy_dict:
                        context_options["proxy"] = proxy_dict
                        proxy_type = proxy_info.proxy_type if proxy_info else "unknown"
                        log_event(
                            log,
                            'http.render.proxy',
                            level='debug',
                            transport='playwright',
                            proxy_type=proxy_type,
                            proxy_server=proxy_dict.get('server'),
                            proxy_auth=bool(proxy_dict.get('username')),
                        )

                    else:
                        log_event(log, 'http.render.direct', level='debug', transport='playwright')
                    # создаем контекст (таймаут передается в page.goto, а не здесь)
                    context = browser.new_context(
                        **context_options,
                        extra_http_headers={
                            "Accept": (
                                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                "image/avif,image/webp,*/*;q=0.8"
                            ),
                            "Accept-Language": "en-US,en;q=0.9,en",
                            "Accept-Encoding": "gzip, deflate, br",
                            "DNT": "1",
                            "Connection": "keep-alive",
                            "Upgrade-Insecure-Requests": "1",
                            "Sec-Fetch-Dest": "document",
                            "Sec-Fetch-Mode": "navigate",
                            "Sec-Fetch-Site": "none",
                            "Sec-Fetch-User": "?1",
                            "Cache-Control": "max-age=0",
                            "Sec-Ch-Ua-Mobile": "?0",
                            "Referer": "https://www.google.com/",
                            **({
                                "Sec-Ch-Ua": '"Not_A Brand";v="99"',
                                "Sec-Ch-Ua-Platform": '"macOS"',
                                "Sec-Ch-Ua-Arch": '"x86"',
                                "Sec-Ch-Ua-Bitness": '"64"',
                                "Sec-Ch-Ua-Full-Version": '"120.0.6099.109"',
                                "Sec-Ch-Ua-Full-Version-List": '"Not_A Brand";v="99.0.0.0", "Chromium";v="120.0.6099.109", "Google Chrome";v="120.0.6099.109"',
                                "Sec-Ch-Ua-Platform-Version": '"14.1.0"',
                            } if is_vestiaire else {
                                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                                "Sec-Ch-Ua-Platform": '"Windows"',
                            }),
                        },
                    )
                    page = context.new_page()

                    # блокировка ресурсов для экономии трафика (снижение с 1-2 МБ до 50-100 КБ)
                    # вАЖНО: НЕ блокируем scripts/xhr, иначе ломается рендер
                    def block_heavy_resources(route):
                        # блокируем тяжелые ресурсы: изображения, шрифты, медиа и стили
                        request = route.request
                        resource_type = request.resource_type
                        url_lower = request.url.lower()

                        # по типу ресурса
                        if resource_type in ["image", "font", "media", "stylesheet"]:
                            route.abort()
                            return

                        # дополнительная защита по расширению (на случай некорректного resource_type)
                        if any(ext in url_lower for ext in [
                            ".jpg",
                            ".jpeg",
                            ".png",
                            ".webp",
                            ".gif",
                            ".svg",
                            ".css",
                            ".woff",
                            ".woff2",
                            ".ttf",
                            ".otf",
                        ]):
                            route.abort()
                            return

                        # нЕ блокируем: script, xhr, document, websocket и т.д
                        route.continue_()

                    page.route("**/*", block_heavy_resources)
                    log_event(log, 'http.render.traffic_block', level='debug', transport='playwright', enabled=True)
                    # применяем stealth плагин для лучшего обхода защиты (включен по умолчанию)
                    if stealth_func:
                        try:
                            stealth_func(page)
                        except Exception as e:
                            log_exception(log, 'http.render.stealth_error', e, level='debug', transport='playwright')
                    elif stealth_class:
                        try:
                            stealth_class.apply_stealth_sync(page)
                        except Exception as e:
                            log_exception(log, 'http.render.stealth_error', e, level='debug', transport='playwright')
                    # скрываем признаки автоматизации - расширенная версия
                    page.add_init_script("""
                // скрываем webdriver
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });

                // скрываем automation indicators
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });

                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });

                // переопределяем permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );

                // добавляем реалистичные свойства
                Object.defineProperty(navigator, 'hardwareConcurrency', {
                    get: () => 8
                });

                Object.defineProperty(navigator, 'deviceMemory', {
                    get: () => 8
                });

                // имитируем реальное взаимодействие
                Object.defineProperty(navigator, 'maxTouchPoints', {
                    get: () => 0
                });

                // скрываем headless признаки
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_JSON;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Object;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Proxy;
                    """)
                    # дополнительные заголовки если есть
                    if headers:
                        page.set_extra_http_headers({k: str(v) for k, v in headers.items() if k not in ["User-Agent"]})
                    # короткая пауза перед goto, чтобы снизить общий latency
                    time.sleep(0.1 + 0.1 * (hash(url) % 10) / 10)

                    # определяем сложность домена для таймаутов
                    is_hard = manager._is_hard_domain(url) if hasattr(manager, '_is_hard_domain') else False
                    is_strict = manager._is_strict_domain(url) if hasattr(manager, '_is_strict_domain') else False
                    is_hard_or_strict = is_hard or is_strict

                    # ограничиваем таймауты рендера, чтобы один URL не удерживал весь запрос API
                    if is_ebay:
                        timeout = max(1000, int(_EBAY_RENDER_TIMEOUT_MS))
                    else:
                        base_timeout = 15000 if is_hard_or_strict else 12000
                        is_direct = (not proxy_dict) or (proxy_info and proxy_info.proxy_type == "direct")
                        is_proxy = proxy_dict is not None and not is_direct
                        timeout_cap = 18000
                        if is_proxy:
                            base_timeout += 5000 if is_hard_or_strict else 3000
                            timeout_cap = 22000
                        timeout = min(timeout_cap, base_timeout + retry * 2000)

                    wait_until = _EBAY_RENDER_WAIT_UNTIL if is_ebay else "domcontentloaded"

                    log_event(
                        log,
                        'http.render.goto',
                        level='debug',
                        transport='playwright',
                        wait_until=wait_until,
                        timeout_ms=timeout,
                        has_proxy=bool(proxy_dict),
                    )
                    try:
                        # явный таймаут на переход (быстрый отказ: 15–20 секунд)
                        page.goto(url, wait_until=wait_until, timeout=timeout)
                    except Exception as e:
                        error_msg = str(e)
                        log_exception(
                            log,
                            'http.render.goto_error',
                            e,
                            level='warning',
                            transport='playwright',
                            wait_until=wait_until,
                            timeout_ms=timeout,
                            attempt=retry + 1,
                        )
                        # помечаем прокси как плохой если это ошибка прокси/сети/таймаут
                        if proxy_info and proxy_info.proxy_type != "direct":
                            err_lower = error_msg.lower()
                            if (
                                "err_tunnel_connection_failed" in err_lower
                                or "proxy" in err_lower
                                or "403" in err_lower
                                or "timeout" in err_lower
                                or "timed out" in err_lower
                                or "net::" in err_lower
                            ):
                                manager.mark_proxy_bad(
                                    url,
                                    retry,
                                    f"Playwright: {error_msg[:50]}",
                                    transport="playwright",
                                    proxy_info=proxy_info,
                                )

                        # для ebay по умолчанию не делаем долгий networkidle-retry, чтобы ускорить fallback
                        if is_ebay and _EBAY_RENDER_RETRY_NETWORKIDLE and wait_until != "networkidle":
                            try:
                                log_event(
                                    log,
                                    'http.render.goto_retry_networkidle',
                                    level='debug',
                                    transport='playwright',
                                )
                                page.goto(url, wait_until="networkidle", timeout=timeout)
                            except Exception as e2:
                                log_exception(
                                    log,
                                    'http.render.goto_failed',
                                    e2,
                                    level='warning',
                                    transport='playwright',
                                    attempt=retry + 1,
                                )
                                context.close()
                                browser.close()

                                if retry == max_retries - 1:
                                    return None, {"status": "timeout", "error": str(e2)}
                                continue
                        else:
                            context.close()
                            browser.close()

                            if retry == max_retries - 1:
                                return None, {"status": "timeout", "error": error_msg}
                            continue

                    # имитируем человеческое поведение после загрузки
                    try:
                        # короткая пост-загрузка
                        time.sleep(0.2 + 0.2 * (hash(url) % 10) / 10)

                        # cлучайная прокрутка страницы
                        if hash(url) % 3 == 0:  # 33% случаев
                            page.evaluate("""
                                window.scrollTo({
                                    top: Math.random() * 500 + 200,
                                    behavior: 'smooth'
                                });
                            """)
                            time.sleep(0.15)

                        # случайное движение мыши
                        if hash(url) % 2 == 0:  # 50% случаев
                            viewport = page.viewport_size
                            if viewport:
                                x = int(
                                    viewport['width'] * 0.1
                                    + (hash(url) % 80) / 100 * viewport['width'] * 0.8
                                )
                                y = int(
                                    viewport['height'] * 0.1
                                    + (hash(url + 'y') % 80) / 100 * viewport['height'] * 0.8
                                )
                                page.mouse.move(x, y)
                                time.sleep(0.1)

                    except Exception as e:
                        log_exception(log, 'http.render.behavior_error', e, level='debug', transport='playwright')
                    # задаем короткое ожидание для стабилизации dom
                    initial_wait = 450 if is_ebay else (380 if is_slow_site else 220)
                    page.wait_for_timeout(initial_wait)

                    # оптимизированная прокрутка (уменьшено количество и задержки)
                    # прокручиваем только один раз до середины страницы для загрузки динамического контента
                    scroll_wait = 350 if is_ebay else (250 if is_slow_site else 150)

                    # одна прокрутка до середины страницы (достаточно для большинства сайтов)
                    # оборачиваем в try-except, так как страница может перезагрузиться
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                        page.wait_for_timeout(scroll_wait)
                    except Exception as scroll_error:
                        # если произошла навигация во время прокрутки, просто продолжаем
                        if "Execution context was destroyed" not in str(scroll_error):
                            log_exception(
                                log,
                                'http.render.scroll_error',
                                scroll_error,
                                level='debug',
                                transport='playwright',
                            )
                        page.wait_for_timeout(scroll_wait)

                    # для ebay делаем дополнительную прокрутку до конца и ждем элементы с ценой
                    if is_ebay:
                        try:
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(300)
                            # пробуем дождаться загрузки элементов с ценой (уменьшен таймаут)
                            try:
                                page.wait_for_selector(
                                    '[data-testid*="price"], .notranslate, [itemprop="price"]',
                                    timeout=2500,
                                    state="attached",
                                )
                            except Exception:
                                pass  # игнорируем, если элементы не найдены
                        except Exception as scroll_error:
                            # если произошла навигация, просто продолжаем
                            if "Execution context was destroyed" not in str(scroll_error):
                                log_exception(
                                    log,
                                    'http.render.scroll_error',
                                    scroll_error,
                                    level='debug',
                                    transport='playwright',
                                    site='ebay',
                                )
                            page.wait_for_timeout(250)
                    elif is_slow_site:
                        # для медленных сайтов небольшая дополнительная задержка
                        try:
                            page.wait_for_timeout(200)
                        except Exception:
                            pass

                    # ждём появление целевых price-блоков на сайтах, где цена часто приезжает с задержкой
                    if is_therealreal:
                        try:
                            page.wait_for_selector(
                                '[data-testid="product-price/final"], '
                                '[data-testid*="product-price/final"], '
                                '.product-price-info__final-price, '
                                '.product-price-info__reduced-price',
                                timeout=5500,
                                state="attached",
                            )
                        except Exception:
                            pass
                        try:
                            page.wait_for_function(
                                """() => {
                                    const selectors = [
                                        '[data-testid="product-price/final"]',
                                        '[data-testid*="product-price/final"]',
                                        '.product-price-info__final-price',
                                        '.product-price-info__reduced-price'
                                    ];
                                    const nodes = selectors.flatMap((sel) => Array.from(document.querySelectorAll(sel)));
                                    return nodes.some((el) => /\\d/.test((el.textContent || '').trim()));
                                }""",
                                timeout=4000,
                            )
                        except Exception:
                            pass
                    elif is_tlc:
                        try:
                            page.wait_for_selector(
                                '[class*="ProductPriceV2__newProductPrice"], '
                                '[class*="newProductPrice"]',
                                timeout=5500,
                                state="attached",
                            )
                        except Exception:
                            pass
                        try:
                            page.wait_for_function(
                                """() => {
                                    const selectors = [
                                        '[class*="ProductPriceV2__newProductPrice"]',
                                        '[class*="ProductPriceV2__newPriceContent"] [class*="newProductPrice"]',
                                        '[class*="ProductPriceV2"] [class*="newProductPrice"]',
                                        '[class*="newProductPrice"]'
                                    ];
                                    const nodes = selectors.flatMap((sel) => Array.from(document.querySelectorAll(sel)));
                                    return nodes.some((el) => /\\d/.test((el.textContent || '').trim()));
                                }""",
                                timeout=4500,
                            )
                        except Exception:
                            pass

                    content = page.content()
                    render_final_url = url
                    try:
                        render_final_url = page.url or url
                    except Exception:
                        render_final_url = url
                    # безопасно закрываем (playwright иногда кидает TargetClosedError при повторном close)
                    try:
                        context.close()
                    except Exception:
                        pass
                    try:
                        browser.close()
                    except Exception:
                        pass

                    soup = BeautifulSoup(content, "html.parser")

                    # проверяем, не получили ли мы страницу блокировки
                    diagnosis = diagnose_response(
                        url=url,
                        status_code=200,
                        final_url=render_final_url,
                        content=content,
                        content_type="text/html",
                        proxy_used=(proxy_info.proxy_uri if proxy_info and proxy_info.proxy_type != "direct" else ""),
                        headers={},
                    )

                    if diagnosis.get("status") in {"blocked", "proxy_auth_required"}:
                        marker = ",".join((diagnosis.get("found_markers") or {}).keys()) or "blocked"
                        log_event(
                            log,
                            "http.render.blocked",
                            level="warning",
                            transport="playwright",
                            marker=marker,
                            status=diagnosis.get("status"),
                        )
                        save_debug_html(url, content, f"blocked_{diagnosis.get('status')}")

                        if not proxy_info or proxy_info.proxy_type == "direct":
                            return None, diagnosis

                        try:
                            manager.mark_proxy_bad(
                                url,
                                retry,
                                "Playwright: blocked",
                                transport="playwright",
                                proxy_info=proxy_info,
                            )
                        except Exception:
                            pass

                        if retry >= 1 or retry == max_retries - 1:
                            return None, diagnosis
                        continue

                    # therealreal/TLC может возвращать отображаемый HTML без цены
                    # повторяем попытку через другой маршрут/прокси вместо того чтобы принимать неполный контент
                    if (is_therealreal or is_tlc) and not _has_domain_price_signals(d, soup, content):
                        log_event(
                            log,
                            "http.render.incomplete_no_price",
                            level="warning",
                            transport="playwright",
                            domain=d,
                            attempt=f"{retry + 1}/{max_retries}",
                        )
                        if retry < max_retries - 1:
                            continue
                        return None, {
                            "status": "render_incomplete",
                            "error": "rendered_html_has_no_price_signals",
                        }

                    # помечаем прокси как хороший
                    if proxy_info and proxy_info.proxy_type != "direct":
                        manager.mark_proxy_good(url, proxy_info)

                    return soup, diagnosis

                except Exception as e:
                    # ошибка внутри блока with sync_playwright
                    error_msg = str(e)
                    log_exception(log, 'http.render.error', e, level='warning', transport='playwright')
                    if retry == max_retries - 1:
                        return None, {"status": "error", "error": error_msg}
                    continue

        except Exception as e:
            error_msg = str(e)
            # не выводим полный traceback для известных некритичных ошибок playwright
            if "Execution context was destroyed" in error_msg:
                log_event(log, 'http.render.execution_context_destroyed', level='warning', transport='playwright')
            else:
                log_exception(log, 'http.render.unhandled', e, level='warning', transport='playwright')
            if retry == max_retries - 1:
                return None, {"status": "error", "error": error_msg}
            continue
        finally:
            if proxy_info and proxy_info.proxy_type == "rotating":
                try:
                    manager.release_proxy(proxy_info, domain=d, url=url, reason="playwright_attempt_done")
                except Exception:
                    pass

    # если все попытки провалились
    return None, {"status": "failed", "error": "Все попытки Playwright провалились"}


def fetch_html_with_stealth_headers(url: str) -> BeautifulSoup | None:
    """
    резервная функция: обычный requests с stealth headers для обхода защиты
    используется когда рендеринг заблокирован Cloudflare
"""
    try:
        import requests
        import time
        import random
        from fake_useragent import UserAgent

        d = domain_of(url)
        is_vestiaire = d.endswith("vestiairecollective.com")

        # генерируем случайный user agent
        try:
            ua = UserAgent()
            user_agent = ua.random
        except Exception:
            if is_vestiaire:
                user_agents = [
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
                ]
                user_agent = random.choice(user_agents)
            else:
                user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        headers = {
            "User-Agent": user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "Referer": "https://www.google.com/",
        }

        # специальные headers для Vestiaire
        if is_vestiaire:
            headers.update({
                "Sec-Ch-Ua": '"Not_A Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
                "Sec-Ch-Ua-Arch": '"x86"',
                "Sec-Ch-Ua-Bitness": '"64"',
                "Sec-Ch-Ua-Full-Version": '"120.0.6099.109"',
                "Sec-Ch-Ua-Full-Version-List": '"Not_A Brand";v="99.0.0.0", "Chromium";v="120.0.6099.109", "Google Chrome";v="120.0.6099.109"',
                "Sec-Ch-Ua-Platform-Version": '"14.1.0"',
                "Sec-Fetch-Site": "cross-site",
            })

        # пробуем несколько попыток для Vestiaire
        max_attempts = 3 if is_vestiaire else 1

        for attempt in range(max_attempts):
            try:
                if attempt > 0:
                    log_event(
                        log,
                        'http.stealth.retry',
                        level='debug',
                        attempt=f'{attempt + 1}/{max_attempts}',
                        domain=d,
                    )
                    time.sleep(random.uniform(2, 5))  # большая задержка

                    # меняем user agent
                    try:
                        user_agent = ua.random
                    except Exception:
                        user_agent = random.choice([
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
                            "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/121.0"
                        ])
                    headers["User-Agent"] = user_agent

                # используем fetch_url с fallback на curl_cffi
                status_code, content, final_url, error = fetch_url(
                    url=url,
                    proxy_url=None,  # без прокси для fallback
                    headers=headers,
                    timeout=20
                )

                if error or status_code != 200:
                    if attempt == max_attempts - 1:
                        log_event(
                            log,
                            'http.stealth.error',
                            level='warning',
                            error=(error or f'HTTP {status_code}'),
                            status_code=status_code,
                        )
                        return None
                    continue

                soup = BeautifulSoup(content, "html.parser")

                # проверяем, не получили ли мы страницу блокировки
                title_text = ""
                if soup.find("title"):
                    title_text = soup.find("title").get_text().lower()

                if "cloudflare" in title_text or "attention required" in title_text or "blocked" in title_text:
                    if attempt == max_attempts - 1:
                        log_event(log, 'http.stealth.blocked', level='warning', attempts=max_attempts)
                        return None
                    continue
                log_event(log, 'http.stealth.ok', level='info')
                return soup
            except requests.exceptions.RequestException as e:
                if attempt == max_attempts - 1:
                    log_exception(log, 'http.stealth.error', e, level='warning')
                    return None
                continue

    except ImportError:
        log_event(log, 'http.stealth.fake_useragent_missing', level='warning', hint='pip install fake-useragent')
    except Exception as e:
        log_exception(log, 'http.stealth.unhandled', e, level='warning')

    return None

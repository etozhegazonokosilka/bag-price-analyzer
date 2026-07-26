"""
парсер для poshmark.com
использует xpath для поиска элементов"""

from bs4 import BeautifulSoup
import json
import re

from utils.xpath_helper import get_text_by_xpath, xpath_exists
from utils.price import parse_price_and_currency, extract_price_universal, is_valid_price_element
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


def scrape_poshmark(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара poshmark.com

    аргументы:
        soup: объект beautifulsoup со страницей товара

    возвращает:
        tuple (название, цена, валюта, статус)
"""
    title = None
    price = None
    currency = None
    status = None

    def _looks_generic_title(value: str | None) -> bool:
        if not value:
            return True
        t = str(value).strip().lower()
        if not t:
            return True
        generic_titles = {
            "home",
            "poshmark",
            "shop",
        }
        return t in generic_titles or t.startswith("home |") or t.startswith("home -")

    def _normalize_availability(avail: str | None) -> str | None:
        if not avail:
            return None
        a = avail.lower()
        if "instock" in a or "in stock" in a:
            return "В продаже"
        if "soldout" in a or "sold out" in a or "outofstock" in a:
            return "Продано"
        # любой другой статус availability считаем не в продаже
        return "Продано"

    def _extract_from_json_ld() -> tuple[str | None, float | None, str | None, str | None]:
        extracted_title = None
        extracted_price = None
        extracted_currency = None
        extracted_status = None

        def _iter_objects(obj):
            if isinstance(obj, dict):
                yield obj
                for v in obj.values():
                    yield from _iter_objects(v)
            elif isinstance(obj, list):
                for item in obj:
                    yield from _iter_objects(item)

        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue

            for obj in _iter_objects(data):
                if not isinstance(obj, dict):
                    continue
                if not extracted_title:
                    name = obj.get("name")
                    if isinstance(name, str) and name.strip():
                        extracted_title = name.strip()
                offers = obj.get("offers")
                if offers:
                    offers_list = offers if isinstance(offers, list) else [offers]
                    for offer in offers_list:
                        if not isinstance(offer, dict):
                            continue
                        price_str = offer.get("price")
                        if price_str is None:
                            price_str = offer.get("lowPrice") or offer.get("highPrice")
                        if price_str is not None and extracted_price is None:
                            try:
                                extracted_price = float(str(price_str).replace(",", ""))
                            except Exception:
                                pass
                        if extracted_currency is None and offer.get("priceCurrency"):
                            extracted_currency = str(offer.get("priceCurrency")).upper()
                        if extracted_status is None and offer.get("availability"):
                            extracted_status = _normalize_availability(str(offer.get("availability")))

            if extracted_price is not None or extracted_title or extracted_status:
                break

        return extracted_title, extracted_price, extracted_currency, extracted_status

    def _extract_listing_price() -> tuple[float | None, str | None]:
        # ищем цену в явных селекторах листинга, чтобы не брать "оригинальную" цену
        selectors = [
            '[data-test="listing-price"]',
            '[data-testid="listing-price"]',
            'p[data-test="listing-price"] span',
            'p[data-testid="listing-price"] span',
            'span[data-test="listing-price"]',
            'span[data-testid="listing-price"]',
            'p.h1 span',
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if not el:
                continue
            text = el.get_text(" ", strip=True)
            parent_text = el.parent.get_text(" ", strip=True) if el.parent else ""
            if not is_valid_price_element(text, parent_text):
                continue
            parsed_price, parsed_currency = parse_price_and_currency(text)
            if parsed_price is not None and parsed_price > 0:
                return parsed_price, parsed_currency
        return None, None

    def _extract_price_from_inline_scripts() -> tuple[float | None, str | None]:
        patterns = [
            r'"listing_price"\s*:\s*\{[^{}]{0,260}?"amount"\s*:\s*"?(?P<price>\d+(?:\.\d+)?)',
            r'"price_amount"\s*:\s*"?(?P<price>\d+(?:\.\d+)?)',
            r'"price"\s*:\s*"?(?P<price>\d+(?:\.\d+)?)',
        ]
        currency_patterns = [
            r'"currency_code"\s*:\s*"(?P<cur>[A-Z]{3})"',
            r'"price_currency"\s*:\s*"(?P<cur>[A-Z]{3})"',
            r'"priceCurrency"\s*:\s*"(?P<cur>[A-Z]{3})"',
        ]

        for script in soup.find_all("script"):
            text = script.string or script.get_text() or ""
            if not text:
                continue
            lower = text.lower()
            if "listing_price" not in lower and "price_amount" not in lower and '"price"' not in lower:
                continue

            parsed_currency = None
            for cp in currency_patterns:
                cm = re.search(cp, text)
                if cm:
                    parsed_currency = (cm.group("cur") or "").upper() or None
                    if parsed_currency:
                        break

            for pattern in patterns:
                m = re.search(pattern, text, flags=re.I)
                if not m:
                    continue
                raw = m.group("price")
                try:
                    parsed_price = float(str(raw).replace(",", ""))
                except Exception:
                    parsed_price = None
                if parsed_price is not None and parsed_price > 0:
                    return parsed_price, parsed_currency
        return None, None

    jsonld_price = None
    jsonld_currency = None

    # 0) jSON-LD (самый надежный источник)
    try:
        j_title, j_price, j_currency, j_status = _extract_from_json_ld()
        if j_title and not _looks_generic_title(j_title):
            title = j_title
        if j_price is not None and j_price > 0:
            jsonld_price = j_price
        if j_currency:
            jsonld_currency = j_currency
        if j_status:
            status = j_status
    except Exception:
        pass

    # извлекаем название через xpath (если JSON-LD не дал)
    try:
        if not title:
            title_text = get_text_by_xpath(soup, "//h1[contains(@class, 'listing__title-container')]")
            if title_text:
                title = title_text
    except Exception:
        pass

    # title из meta (fallback)
    if not title:
        try:
            meta_title = soup.find("meta", {"property": "og:title"})
            if meta_title and meta_title.get("content"):
                title = meta_title.get("content").strip()
        except Exception:
            pass
    if _looks_generic_title(title):
        title = None

    # 1) цена из явных селекторов листинга
    if price is None:
        found_price, found_currency = _extract_listing_price()
        if found_price is not None:
            price = found_price
            currency = found_currency or currency

    # 2) цена из json-ld, если не нашли в листинге
    if price is None and jsonld_price is not None:
        price = jsonld_price
        currency = jsonld_currency or currency

    # 3) цена из meta-тегов (seo-данные)
    try:
        if price is None:
            meta_price = soup.find("meta", {"property": "og:price:amount"}) or soup.find(
            "meta", {"property": "product:price:amount"}
            )
            if meta_price and meta_price.get("content"):
                meta_val = meta_price.get("content").strip()
                try:
                    meta_price_val = float(meta_val)
                    if meta_price_val > 0:
                        price = meta_price_val
                        log_event(log, 'parse.price.found', level='debug', source='meta', price=price)
                except ValueError:
                    pass

        meta_currency = soup.find("meta", {"property": "og:price:currency"}) or soup.find(
            "meta", {"property": "product:price:currency"}
        )
        if meta_currency and meta_currency.get("content"):
            currency = meta_currency.get("content").strip().upper()
    except Exception as e:
        log_exception(log, 'parse.price.error', e, level='debug', source='meta')
    # 3.5) inline state/scripts (на некоторых страницах Poshmark цена живет только там)
    if price is None:
        try:
            script_price, script_currency = _extract_price_from_inline_scripts()
            if script_price is not None and script_price > 0:
                price = script_price
                currency = currency or script_currency or "USD"
                log_event(log, 'parse.price.found', level='debug', source='script', price=price, currency=currency)
        except Exception as e:
            log_exception(log, 'parse.price.error', e, level='debug', source='script')
    # 4) визуальный xpath по актуальной структуре (listing__info)
    if price is None:
        try:
            direct_price_text = get_text_by_xpath(
                soup,
                "//*[@id='content']//div[contains(@class, 'listing__info')]//p/span[1]",
            )
            if direct_price_text:
                if not is_valid_price_element(direct_price_text, ""):
                    parsed_price, parsed_currency = None, None
                else:
                    parsed_price, parsed_currency = parse_price_and_currency(direct_price_text)
                if parsed_price is not None and parsed_price > 0:
                    price = parsed_price
                    if not currency:
                        currency = parsed_currency or "USD"
                    log_event(
                        log,
                        'parse.price.found',
                        level='debug',
                        source='xpath:listing__info',
                        price=price,
                        currency=currency,
                    )
        except Exception as e:
            log_exception(log, 'parse.price.error', e, level='debug', source='xpath:listing__info')
    # 5) если все ещё нет — универсальная функция и fallback xpath
    if price is None:
        try:
            price, cur = extract_price_universal(soup, "Poshmark", debug=False)
            if price is not None and price > 0:
                currency = currency or cur or "USD"
        except Exception as e:
            log_exception(log, 'parse.price.error', e, level='debug', source='universal')
    if price is None:
        poshmark_specific_xpaths = [
            "//*[@id='content']//div[contains(@class, 'listing__info')]//p/span[1]",
            "//span[@class='' and contains(text(), '$')]",
            "//span[contains(@class, 'price')]",
            "//div[contains(@class, 'price')]//span",
            "//span[contains(@data-testid, 'price')]",
            "//div[contains(@class, 'listing-price')]//span",
        ]

        for xpath in poshmark_specific_xpaths:
            try:
                price_text = get_text_by_xpath(soup, xpath)
                if price_text:
                    parsed_price, parsed_currency = parse_price_and_currency(price_text)
                    if parsed_price is not None and parsed_price > 0:
                        price = parsed_price
                        if not currency:
                            currency = parsed_currency or "USD"
                        log_event(
                            log,
                            'parse.price.found',
                            level='debug',
                            source='xpath',
                            xpath=xpath,
                            price=price,
                            currency=currency,
                        )
                        break
            except Exception:
                continue

    # статус: ищем SOLD / Buy Now в статусе листинга
    try:
        if status is None:
            status_el = soup.select_one(".listing__inventory-status")
            status_text = status_el.get_text(" ", strip=True).lower() if status_el else ""
            sold_markers = ["sold", "sold out", "out of stock", "not for sale", "no longer available"]
            if any(marker in status_text for marker in sold_markers):
                status = "Продано"
            else:
                # если явно не SOLD, пробуем наличие активной кнопки Buy Now
                has_buy_now = xpath_exists(soup, "//button[contains(., 'Buy Now')]")
                status = "В продаже" if has_buy_now else "Unknown"
    except Exception:
        status = status or None

    # если цена не найдена и товар продан, оставляем None
    if price is None:
        currency = None
    elif currency is None:
        currency = "USD"

    return title, price, currency, status

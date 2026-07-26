"""
парсер для ebay"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

from services.currency import convert_to_usd
from utils.logger import get_logger, log_event, log_exception
from utils.price import extract_price_universal, parse_price_and_currency
from utils.xpath_helper import get_text_by_xpath, xpath_exists

log = get_logger(__name__)

_GENERIC_TITLE_VALUES = {
    "ebay",
    "home",
    "shop by category",
    "all categories",
    "seller's other items",
    "clothing, shoes & accessories",
    "women",
    "men",
    "collectibles",
    "jewelry & watches",
    "sports mem, cards & fan shop",
    "women's bags & handbags",
}


def _iter_objects(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_objects(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_objects(item)


def _normalize_availability(avail: str | None) -> str | None:
    if not avail:
        return None
    value = str(avail).lower()
    if "instock" in value or "in stock" in value:
        return "Available"
    if "soldout" in value or "sold out" in value or "outofstock" in value:
        return "Sold"
    return None


def _clean_title(raw: str | None) -> str | None:
    if not raw:
        return None
    title = " ".join(str(raw).split()).strip()
    if not title:
        return None
    title = re.sub(r"\s*[\|\u2013\u2014-]\s*ebay\s*$", "", title, flags=re.I).strip()
    if not title:
        return None
    low = title.lower()
    if any(marker in low for marker in ("service unavailable", "access denied", "blocked", "captcha")):
        return None
    if low in _GENERIC_TITLE_VALUES:
        return None
    if low.startswith("ebay |") or low.startswith("home |"):
        return None
    if low.startswith("category:"):
        return None
    return title


def _detect_currency_from_text(text: str) -> str | None:
    upper = (text or "").upper()
    if "USD" in upper or "$" in text:
        return "USD"
    if "GBP" in upper or "£" in text:
        return "GBP"
    if "EUR" in upper or "€" in text:
        return "EUR"
    return None


def _convert_to_usd_if_needed(price: float | None, currency: str | None) -> tuple[float | None, str | None]:
    if price is None:
        return None, None if currency is None else currency
    if not currency:
        return price, "USD"
    if currency.upper() == "USD":
        return price, "USD"
    try:
        converted = convert_to_usd(price, currency)
        if converted is not None:
            return converted, "USD"
    except Exception:
        pass
    return price, currency


def _extract_from_jsonld(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    title = None
    fallback_title = None
    price = None
    currency = None
    status = None

    def _is_product_like(obj: dict[str, Any]) -> bool:
        raw_type = obj.get("@type")
        types: list[str] = []
        if isinstance(raw_type, str):
            types = [raw_type.lower()]
        elif isinstance(raw_type, list):
            types = [str(x).lower() for x in raw_type if isinstance(x, str)]

        if any("product" in t or "offer" in t for t in types):
            return True
        if any(k in obj for k in ("offers", "price", "itemCondition", "sku", "mpn", "gtin")):
            return True
        return False

    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        for obj in _iter_objects(data):
            if not isinstance(obj, dict):
                continue

            product_like = _is_product_like(obj)

            name = obj.get("name")
            if isinstance(name, str):
                cleaned = _clean_title(name)
                if cleaned:
                    if product_like and title is None:
                        title = cleaned
                    elif fallback_title is None:
                        fallback_title = cleaned

            offers = obj.get("offers")
            offer_list = offers if isinstance(offers, list) else [offers] if offers else []
            if not offer_list and product_like and obj.get("price") is not None:
                offer_list = [obj]

            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue

                if price is None:
                    raw_price = offer.get("price")
                    if raw_price is None:
                        raw_price = offer.get("lowPrice") or offer.get("highPrice")
                    if raw_price is not None:
                        if isinstance(raw_price, (int, float)):
                            parsed_price = float(raw_price)
                        else:
                            parsed_price, _ = parse_price_and_currency(str(raw_price))
                        if parsed_price is not None and parsed_price > 0:
                            price = parsed_price

                if currency is None and offer.get("priceCurrency"):
                    currency = str(offer.get("priceCurrency")).strip().upper()

                if status is None:
                    status = _normalize_availability(str(offer.get("availability") or ""))

    if title is None:
        title = fallback_title

    return title, price, currency, status


def _extract_from_oa(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    title = None
    price = None
    currency = None
    status = None

    def _try_parse_price(val: Any) -> float | None:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            value = float(val)
            return value if value > 0 else None
        parsed, _ = parse_price_and_currency(str(val))
        return parsed

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if "_oa" not in text:
            continue

        data: dict[str, Any] | None = None

        match = re.search(r"_oa\\s*=\\s*JSON\\.parse\\((\".*?\"|'.*?')\\)", text, flags=re.S)
        if match:
            raw_literal = match.group(1)
            try:
                decoded = json.loads(raw_literal)
                data = json.loads(decoded)
            except Exception:
                data = None

        if data is None:
            match = re.search(r"_oa\\s*=\\s*({.*?})\\s*;", text, flags=re.S)
            if match:
                try:
                    data = json.loads(match.group(1))
                except Exception:
                    data = None

        if not isinstance(data, dict):
            # fallback: достаем ключи регулярками прямо из блока
            block = text[text.find("_oa") : text.find("_oa") + 8000] if "_oa" in text else text
            if title is None:
                m_title = re.search(r'"title"\\s*:\\s*"([^\"]+)"', block)
                if m_title:
                    title = _clean_title(m_title.group(1))
            if currency is None:
                m_currency = re.search(r'"priceCurrency"\\s*:\\s*"?([A-Z]{3})"?', block)
                if m_currency:
                    currency = m_currency.group(1).upper()
            if price is None:
                # извлекаем цену только вместе с валютой, чтобы не поймать произвольное число из нерелевантного блока
                pair = re.search(
                    r'"priceCurrency"\\s*:\\s*"?(?P<cur>[A-Z]{3})"?'
                    r'.{0,120}?"price"\\s*:\\s*"?(?P<price>[0-9][0-9.,]*)"?',
                    block,
                    flags=re.S,
                )
                if not pair:
                    pair = re.search(
                        r'"price"\\s*:\\s*"?(?P<price>[0-9][0-9.,]*)"?'
                        r'.{0,120}?"priceCurrency"\\s*:\\s*"?(?P<cur>[A-Z]{3})"?',
                        block,
                        flags=re.S,
                    )
                if pair:
                    price = _try_parse_price(pair.group("price"))
                    if not currency:
                        currency = pair.group("cur").upper()
            if status is None:
                m_availability = re.search(r'"availability"\\s*:\\s*"?([^\",}]+)"?', block)
                if m_availability:
                    status = _normalize_availability(m_availability.group(1))
            if title or price is not None or status:
                break
            continue

        if title is None:
            title = _clean_title(data.get("title") or data.get("name"))
        if price is None:
            price = _try_parse_price(data.get("price") or data.get("lowPrice") or data.get("highPrice"))
        if currency is None and data.get("priceCurrency"):
            currency = str(data.get("priceCurrency")).strip().upper()
        if status is None:
            status = _normalize_availability(str(data.get("availability") or ""))

        if title or price is not None or status:
            break

    return title, price, currency, status


def _extract_from_meta(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None]:
    title = None
    meta_title = soup.find("meta", {"property": "og:title"})
    if meta_title and meta_title.get("content"):
        title = _clean_title(meta_title.get("content"))

    price = None
    currency = None
    price_meta = (
        soup.find("meta", {"property": "product:price:amount"})
        or soup.find("meta", {"property": "og:price:amount"})
        or soup.find("meta", {"itemprop": "price"})
    )
    if price_meta and price_meta.get("content"):
        raw_content = str(price_meta.get("content"))
        try:
            parsed_price = float(raw_content.replace(",", ""))
            parsed_currency = None
        except Exception:
            parsed_price, parsed_currency = parse_price_and_currency(raw_content)
        if parsed_price is not None and parsed_price > 0:
            price = parsed_price
            currency = parsed_currency

    currency_meta = (
        soup.find("meta", {"property": "product:price:currency"})
        or soup.find("meta", {"property": "og:price:currency"})
        or soup.find("meta", {"itemprop": "priceCurrency"})
    )
    if currency_meta and currency_meta.get("content"):
        currency = str(currency_meta.get("content")).strip().upper()

    return title, price, currency


def _extract_price_from_selectors(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    selectors = [
        "#prcIsum",
        "#mm-saleDscPrc",
        "#mainContent .x-price-primary .ux-textspans",
        "#mainContent .x-price-primary span",
        ".x-bin-price__content .ux-textspans",
        ".x-item-condensed-card__price .ux-textspans.ux-textspans--BOLD",
        ".x-item-condensed-card__price .ux-textspans--BOLD",
        ".x-item-condensed-card__price span",
        "div.x-item-condensed-card__price > span",
        "span[itemprop='price']",
    ]
    for selector in selectors:
        for el in soup.select(selector):
            text = (el.get_text(" ", strip=True) or "").strip()
            if not text:
                continue
            parsed_price, parsed_currency = parse_price_and_currency(text)
            if parsed_price is None or parsed_price <= 0:
                continue
            currency = parsed_currency or _detect_currency_from_text(text) or "USD"
            return parsed_price, currency

    xpaths = [
        "/html/body/div[2]/main/div[1]/div[1]/div[3]/div/div/div[5]/div/div/div/div/div[1]/div[2]/div[3]/span",
        "//div[contains(@class, 'x-item-condensed-card__price')]//span[contains(@class, 'ux-textspans--BOLD')]",
    ]
    for xpath in xpaths:
        text = (get_text_by_xpath(soup, xpath) or "").strip()
        if not text:
            continue
        parsed_price, parsed_currency = parse_price_and_currency(text)
        if parsed_price is None or parsed_price <= 0:
            continue
        currency = parsed_currency or _detect_currency_from_text(text) or "USD"
        return parsed_price, currency

    return None, None


def _detect_status(soup: BeautifulSoup, status: str | None) -> str | None:
    if status:
        return status

    try:
        has_sold_marker = xpath_exists(
            soup,
            "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'this listing was ended') "
            "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'listing has ended') "
            "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sold out') "
            "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'out of stock')]",
        )
    except Exception:
        has_sold_marker = False

    has_buy_now = False
    if not has_sold_marker:
        try:
            has_buy_now = (
                xpath_exists(soup, "//span[contains(., 'Buy It Now')]")
                or xpath_exists(soup, "//span[contains(., 'Sofort-Kaufen')]")
                or xpath_exists(soup, "//span[contains(., 'Achat immédiat')]")
                or xpath_exists(soup, "//*[@id='binBtn_btn']")
                or xpath_exists(soup, "//button[contains(@class, 'ux-call-to-action')]")
            )
        except Exception:
            has_buy_now = False

    if has_sold_marker:
        return "Sold"
    if has_buy_now:
        return "Available"
    return "Unknown"


def scrape_ebay(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """парсит страницу сумки eBay и возвращает (title, price, currency, status)"""
    title = None
    price = None
    currency = None
    status = None

    try:
        json_title, json_price, json_currency, json_status = _extract_from_jsonld(soup)
        if json_title:
            title = json_title
        if json_price is not None and json_price > 0:
            price = json_price
        if json_currency:
            currency = json_currency
        if json_status:
            status = json_status
    except Exception as exc:
        log_exception(log, "parse.ebay.jsonld.error", exc, level="debug")

    if price is None or currency is None or status is None or not title:
        try:
            oa_title, oa_price, oa_currency, oa_status = _extract_from_oa(soup)
            if not title and oa_title:
                title = oa_title
            if price is None and oa_price is not None and oa_price > 0:
                price = oa_price
            if currency is None and oa_currency:
                currency = oa_currency
            if status is None and oa_status:
                status = oa_status
        except Exception as exc:
            log_exception(log, "parse.ebay.oa.error", exc, level="debug")

    if not title:
        try:
            h1 = soup.select_one("#mainContent h1") or soup.select_one("h1")
            if h1:
                title = _clean_title(h1.get_text(" ", strip=True))
        except Exception:
            pass

    if price is None:
        try:
            sel_price, sel_currency = _extract_price_from_selectors(soup)
            if sel_price is not None and sel_price > 0:
                price = sel_price
                currency = sel_currency or currency
                log_event(
                    log,
                    "parse.price.found",
                    level="debug",
                    source="ebay_selectors",
                    price=price,
                    currency=currency,
                )
        except Exception as exc:
            log_exception(log, "parse.price.error", exc, level="debug", source="ebay_selectors")

    if price is None or currency is None or not title:
        try:
            meta_title, meta_price, meta_currency = _extract_from_meta(soup)
            if not title and meta_title:
                title = meta_title
            if price is None and meta_price is not None and meta_price > 0:
                price = meta_price
            if currency is None and meta_currency:
                currency = meta_currency
        except Exception as exc:
            log_exception(log, "parse.price.error", exc, level="debug", source="ebay_meta")

    if price is None:
        try:
            uni_price, uni_currency = extract_price_universal(soup, "eBay", debug=False)
            if uni_price is not None and uni_price > 0:
                price = uni_price
                currency = currency or uni_currency or "USD"
                log_event(log, "parse.price.found", level="debug", source="universal", price=price, currency=currency)
        except Exception as exc:
            log_exception(log, "parse.price.error", exc, level="debug", source="universal")

    if price is not None:
        price, currency = _convert_to_usd_if_needed(price, currency or "USD")

    status = _detect_status(soup, status)

    if price is None:
        currency = None
    elif currency is None:
        currency = "USD"

    return title, price, currency, status

"""
парсер для dallasdesignerhandbags.com"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from utils.logger import get_logger, log_event, log_exception
from utils.price import extract_price_universal, parse_price_and_currency
from utils.xpath_helper import get_text_by_xpath, xpath_exists

log = get_logger(__name__)


def _extract_status(soup: BeautifulSoup) -> str:
    # сначала проверяем явные маркеры sold out в DOM
    try:
        sold_xpath = (
            "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sold out') "
            "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'out of stock') "
            "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'unavailable')]"
        )
        if xpath_exists(soup, sold_xpath):
            return "Sold Out"
    except Exception:
        pass

    # затем проверяем явный buy/add-to-cart маркер
    try:
        if xpath_exists(
            soup,
            "//*[self::button or self::a]"
            "[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to cart') "
            "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'buy now')]",
        ):
            return "Available"
    except Exception:
        pass

    # fallback: читаем productJson.available
    try:
        parsed_available: bool | None = None
        for script in soup.find_all("script"):
            text = script.string or script.get_text() or ""
            if "productJson" not in text:
                continue

            # приоритетный вариант: явное присваивание productJson = {...}
            obj_match = re.search(r"productJson\s*=\s*(\{.*?\})\s*;", text, flags=re.I | re.S)
            if obj_match:
                try:
                    data = json.loads(obj_match.group(1))
                    available = data.get("available")
                    if isinstance(available, bool):
                        parsed_available = available
                        break
                except Exception:
                    pass

            # fallback: берём первый флаг available в этом скрипте
            available_match = re.search(r'"available"\s*:\s*(true|false)', text, flags=re.I)
            if available_match:
                parsed_available = available_match.group(1).lower() == "true"
                break

        if parsed_available is not None:
            return "Available" if parsed_available else "Sold Out"
    except Exception:
        pass

    # fallback по старому маркеру
    try:
        if xpath_exists(soup, "//span[contains(@class, 'in-stock')]"):
            return "Available"
    except Exception:
        pass

    return "Sold Out"


def _extract_title(soup: BeautifulSoup) -> str | None:
    for xpath in [
        "//h1[contains(@class, 'product')]",
        "//h2[contains(@class, 'name')]",
        "//h1",
        "//h2",
    ]:
        try:
            value = get_text_by_xpath(soup, xpath)
            if value:
                return value.strip()
        except Exception:
            continue
    return None


def _extract_price_for_available_item(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    try:
        price, currency = extract_price_universal(soup, "DallasDesignerHandbags", debug=False)
        if price is not None and price > 0:
            return price, (currency or "USD")
    except Exception as exc:
        log_exception(log, "parse.price.error", exc, level="warning", source="universal")

    for xpath in [
        "//*[@id='ProductPrice-product-template']",
        "//span[contains(@class, 'price')]",
        "//div[contains(@class, 'price')]//span",
        "//span[@id='ProductPrice-product-template']",
    ]:
        try:
            price_text = get_text_by_xpath(soup, xpath)
            if not price_text:
                continue
            parsed_price, parsed_currency = parse_price_and_currency(price_text)
            if parsed_price is None or parsed_price <= 0:
                continue
            currency = parsed_currency or "USD"
            log_event(
                log,
                "parse.price.found",
                level="debug",
                source="xpath",
                xpath=xpath,
                price=parsed_price,
                currency=currency,
            )
            return parsed_price, currency
        except Exception:
            continue

    return None, None


def scrape_dallasdesignerhandbags(
    soup: BeautifulSoup,
) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара Dallas Designer Handbags
    возвращает: (title, price, currency, status)
"""
    title = _extract_title(soup)
    status = _extract_status(soup)

    # для sold товаров не возвращаем цену
    if status == "Sold Out":
        return title, None, None, status

    price, currency = _extract_price_for_available_item(soup)
    return title, price, currency, status

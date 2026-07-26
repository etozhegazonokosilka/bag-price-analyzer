"""
парсер для jolicloset.com
использует xpath и css-селекторы для извлечения карточки товара"""

import re

from bs4 import BeautifulSoup

from services.currency import convert_to_usd
from utils.price import parse_price_and_currency
from utils.xpath_helper import get_text_by_xpath, xpath_exists


def _first_nonempty(*values: str | None) -> str | None:
    for value in values:
        if value and str(value).strip():
            return str(value).strip()
    return None


def _parse_price_text(raw: str) -> tuple[float | None, str | None]:
    parsed_price, parsed_currency = parse_price_and_currency(raw)
    if parsed_price is not None and parsed_price > 0:
        return parsed_price, parsed_currency

    # fallback на случай нестандартной кодировки валютного символа
    text = str(raw or "")
    text_lower = text.lower()
    currency = None
    if "€" in text or "eur" in text_lower:
        currency = "EUR"
    elif "$" in text or "usd" in text_lower:
        currency = "USD"
    elif "£" in text or "gbp" in text_lower:
        currency = "GBP"

    number_match = re.search(r"\d[\d\s.,]*", text)
    if not number_match:
        return None, currency

    number_text = number_match.group(0).replace("\xa0", " ").strip()
    if not number_text:
        return None, currency

    # поддерживаем форматы 1 234,56 и 1,234.56
    number_text = number_text.replace(" ", "")
    if "," in number_text and "." in number_text:
        if number_text.rfind(",") > number_text.rfind("."):
            number_text = number_text.replace(".", "").replace(",", ".")
        else:
            number_text = number_text.replace(",", "")
    elif "," in number_text and "." not in number_text:
        parts = number_text.split(",")
        if len(parts[-1]) == 2:
            number_text = number_text.replace(",", ".")
        else:
            number_text = number_text.replace(",", "")

    try:
        value = float(number_text)
        if value > 0:
            return value, currency
    except (ValueError, TypeError):
        return None, currency

    return None, currency


def scrape_jolicloset(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара jolicloset.com

    аргументы:
        soup: объект beautifulsoup со страницей товара

    возвращает:
        tuple (название, цена, валюта, статус)
"""
    title = None
    price = None
    currency = None
    status = "Sold Out"

    # извлекаем название с несколькими fallback-ветками
    try:
        title = _first_nonempty(
            get_text_by_xpath(soup, "//*[@id='product']/div[1]/h1/span"),
            get_text_by_xpath(soup, "//*[@id='product']//h1"),
            get_text_by_xpath(soup, "//meta[@property='og:title']/@content"),
        )
    except Exception:
        title = None

    # извлекаем цену из нескольких источников, чтобы не зависеть от одного xpath
    try:
        price_candidates = [
            get_text_by_xpath(soup, "//*[@id='product']/div[1]/div[1]/span"),
            get_text_by_xpath(soup, "//*[@id='product']//span[contains(@class, 'price')]"),
            get_text_by_xpath(soup, "//*[@id='product']//div[contains(@class, 'price')]"),
            get_text_by_xpath(
                soup,
                "//*[@id='product']//span[contains(text(), '€') or contains(text(), '$') or contains(text(), '£')]",
            ),
            get_text_by_xpath(soup, "//meta[@property='product:price:amount']/@content"),
            get_text_by_xpath(soup, "//meta[@property='og:price:amount']/@content"),
        ]

        for raw in price_candidates:
            if not raw:
                continue
            parsed_price, parsed_currency = _parse_price_text(raw)
            if parsed_price is None or parsed_price <= 0:
                continue
            price = parsed_price
            currency = parsed_currency or "EUR"
            break
    except Exception:
        price = None
        currency = None

    # определяем статус по sold-out маркерам и cta покупки
    try:
        has_sold_out = bool(
            soup.select_one("[class*='sold'], [class*='Sold'], [class*='outOfStock'], [class*='out-of-stock']")
        )

        if not has_sold_out:
            page_text = (soup.get_text(" ", strip=True) or "").lower()
            has_sold_out = any(
                marker in page_text
                for marker in (
                    "sold out",
                    "out of stock",
                    "currently unavailable",
                    "vendu",
                    "ausverkauft",
                )
            )

        has_buy_cta = xpath_exists(
            soup,
            "//*[@id='addCartButton']"
            " | //button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to cart')]"
            " | //button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to bag')]"
            " | //button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'buy now')]"
            " | //a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to cart')]",
        )

        if has_sold_out:
            status = "Sold Out"
        elif has_buy_cta:
            status = "Available"
        elif price is not None:
            # если есть валидная цена, считаем товар доступным
            status = "Available"
        else:
            status = "Sold Out"
    except Exception:
        status = "Sold Out"

    # нормализуем валюту при отсутствии цены
    if price is None:
        currency = None

    # приводим все цены к usd, если сервис конвертации доступен
    if price is not None and currency:
        price_usd = convert_to_usd(price, currency)
        if price_usd is not None:
            price = price_usd
            currency = "USD"

    return title, price, currency, status

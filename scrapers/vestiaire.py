"""
парсер для vestiairecollective.com
использует xpath для поиска элементов"""

from bs4 import BeautifulSoup
import json

from services.currency import convert_to_usd
from utils.xpath_helper import get_text_by_xpath, xpath_exists
from utils.price import (
    parse_price_and_currency,
    extract_price_universal,
    extract_price_from_jsonld,
    extract_price_from_scripts,
    extract_price_from_sold_text,
    is_valid_price_element,
    normalize_currency_code,
)


def scrape_vestiaire(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара vestiairecollective.com

    аргументы:
        soup: объект beautifulsoup со страницей товара

    возвращает:
        tuple (название, цена, валюта, статус)
"""
    title = None
    price = None
    currency = None
    status = None

    def _looks_like_catalog() -> bool:
        # определяем, что это каталог (plp), а не карточка товара
        catalog_selectors = [
            "[data-testid*='product-card']",
            "[data-testid*='plp-product-card']",
            "[data-cy*='product-card']",
            ".product-card",
            ".productCard",
            ".plp-product-card",
            ".ProductCard",
        ]
        for sel in catalog_selectors:
            try:
                if len(soup.select(sel)) >= 3:
                    return True
            except Exception:
                continue

        try:
            product_links = [
                a for a in soup.select("a[href]")
                if ".shtml" in a.get("href", "") and "/women-" in a.get("href", "")
            ]
            if len(product_links) >= 4:
                return True
        except Exception:
            pass

        return False

    def _has_product_schema() -> bool:
        # проверяем json-ld на тип product
        try:
            for script in soup.find_all("script", {"type": "application/ld+json"}):
                try:
                    data = json.loads(script.string or "{}")
                except Exception:
                    continue
                if isinstance(data, dict):
                    candidates = [data]
                    if "@graph" in data and isinstance(data["@graph"], list):
                        candidates.extend(data["@graph"])
                    for item in candidates:
                        if not isinstance(item, dict):
                            continue
                        item_type = item.get("@type") or item.get("type")
                        if isinstance(item_type, list):
                            if any(str(t).lower() == "product" for t in item_type):
                                return True
                        elif isinstance(item_type, str):
                            if item_type.lower() == "product":
                                return True
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and str(item.get("@type", "")).lower() == "product":
                            return True
        except Exception:
            return False
        return False

    def _has_product_meta() -> bool:
        # проверяем мета-теги товара
        try:
            if soup.find("meta", {"property": "product:price:amount"}):
                return True
            if soup.find("meta", {"property": "og:price:amount"}):
                return True
            if soup.find("meta", {"itemprop": "price"}):
                return True
            if soup.find("meta", {"itemprop": "priceCurrency"}):
                return True
        except Exception:
            return False
        return False

    # извлекаем название через xpath
    try:
        title_text = get_text_by_xpath(soup, "//span[@data-cy='productTitle_name']")
        if title_text:
            title = title_text
    except Exception:
        pass

    if not title:
        try:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)
        except Exception:
            pass

    # если похоже на каталог, пропускаем
    try:
        has_buy_button = xpath_exists(soup, "//button[@data-cy='pdp_buy_btn']")
    except Exception:
        has_buy_button = False
    has_product_schema = _has_product_schema()
    has_product_meta = _has_product_meta()

    if not has_buy_button and not has_product_schema and not has_product_meta and _looks_like_catalog():
        return None, None, None, "catalog"

    # пробуем json-ld
    if price is None:
        try:
            for script in soup.find_all("script", {"type": "application/ld+json"}):
                try:
                    data = json.loads(script.string or "{}")
                except Exception:
                    continue
                json_price, json_currency = extract_price_from_jsonld(data)
                if json_price is not None and json_price > 0:
                    price = json_price
                    currency = json_currency or currency
                    break
        except Exception:
            pass

    # meta теги цены и валюты
    if price is None:
        try:
            meta_price = (
                soup.find("meta", {"property": "product:price:amount"})
                or soup.find("meta", {"property": "og:price:amount"})
                or soup.find("meta", {"itemprop": "price"})
            )
            if meta_price and meta_price.get("content"):
                price = float(str(meta_price.get("content")).replace(",", "").strip())
        except Exception:
            pass

    if currency is None:
        try:
            meta_currency = (
                soup.find("meta", {"property": "product:price:currency"})
                or soup.find("meta", {"property": "og:price:currency"})
                or soup.find("meta", {"itemprop": "priceCurrency"})
            )
            if meta_currency and meta_currency.get("content"):
                currency = normalize_currency_code(meta_currency.get("content"))
        except Exception:
            pass

    # цена из скриптов
    if price is None:
        try:
            script_price, script_currency = extract_price_from_scripts(soup)
            if script_price is not None and script_price > 0:
                price = script_price
                currency = normalize_currency_code(script_currency) or currency
        except Exception:
            pass

    # специфичные селекторы
    if price is None:
        vestiaire_specific_xpaths = [
            "//*[@id='__next']/div/main/main/div[2]/div/div[3]/div/div[1]/div/ul/li[1]/div/p/span[1]",
            "//span[@data-cy='product_price']",
        ]
        for xpath in vestiaire_specific_xpaths:
            text = get_text_by_xpath(soup, xpath)
            if not text:
                continue
            parsed_price, parsed_currency = parse_price_and_currency(text)
            if parsed_price is not None and parsed_price > 0:
                price = parsed_price
                currency = normalize_currency_code(parsed_currency) or currency
                break

    if price is None:
        selectors = [
            '[data-cy="product_price"]',
            '[data-cy="product_price_sold"]',
            'span[class*="product-price"]',
            'div[class*="product-price"] span',
            'span[class*="price-amount"]',
            'div[class*="price-container"] span',
            'span[data-cy*="price"]',
            'div[data-cy*="price"] span',
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if not el:
                continue
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            # отдельный случай sold at
            if "sold at" in text.lower():
                sold_price, sold_currency = extract_price_from_sold_text(text)
                if sold_price is not None:
                    price = sold_price
                    currency = sold_currency or currency
                    break
            if not is_valid_price_element(text):
                continue
            parsed_price, parsed_currency = parse_price_and_currency(text)
            if parsed_price is not None and parsed_price > 0:
                price = parsed_price
                currency = parsed_currency or currency
                if currency is None:
                    attr_currency = el.get("data-currency") or el.get("data-currency-code")
                    if attr_currency:
                        currency = normalize_currency_code(attr_currency)
                break

    # fallback: универсальный парсер
    if price is None:
        try:
            price, cur = extract_price_universal(soup, "Vestiaire", debug=False)
            if price is not None and price > 0:
                currency = currency or normalize_currency_code(cur)
        except Exception:
            pass

    # проверяем статус через наличие кнопки "buy"
    try:
        has_buy_button = xpath_exists(soup, "//button[@data-cy='pdp_buy_btn']")
        if has_buy_button:
            status = "в продаже"
        else:
            status = status or "продано"
    except Exception:
        status = status or "продано"

    # если цена есть, но валюта не определена, пытаемся угадать по тексту
    if price is not None and currency is None:
        try:
            page_text = soup.get_text(" ", strip=True)
            if "€" in page_text or "EUR" in page_text:
                currency = "EUR"
            elif "£" in page_text or "GBP" in page_text:
                currency = "GBP"
            elif "$" in page_text or "USD" in page_text:
                currency = "USD"
        except Exception:
            pass

    # если цена не найдена, оставляем валюту пустой
    if price is None:
        currency = None
    elif currency and currency.upper() != "USD":
        try:
            converted_price = convert_to_usd(price, currency)
            if converted_price is not None and converted_price > 0:
                price = converted_price
                currency = "USD"
        except Exception:
            pass

    return title, price, currency, status

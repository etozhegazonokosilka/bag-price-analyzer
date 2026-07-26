"""
парсер для fashionphile.com
использует xpath для поиска элементов"""

from bs4 import BeautifulSoup

from utils.xpath_helper import get_text_by_xpath, get_attribute_by_xpath, xpath_exists
from utils.price import parse_price_and_currency, extract_price_universal
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


def scrape_fashionphile(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара fashionphile.com

    аргументы:
        soup: объект beautifulsoup со страницей товара

    возвращает:
        tuple (название, цена, валюта, статус)
"""
    title = None
    price = None
    currency = None
    status = "Sold Out"

    # извлекаем название через xpath
    # используем универсальный поиск h1, так как id контейнера динамический
    try:
        # пробуем найти h1 внутри контейнера с информацией о продукте
        # сначала ищем h1 с классом, связанным с названием
        title_text = get_text_by_xpath(soup, "//h1[contains(@class, 'title') or contains(@class, 'heading')]")
        if not title_text:
            # fallback: просто ищем первый h1 на странице
            title_text = get_text_by_xpath(soup, "//h1")
        if title_text:
            title = title_text.strip()
    except Exception:
        pass

    # извлекаем цену: используем селекторы, которые игнорируют динамические id
    # fashionphile использует id вида "price-template--24380755706159__main", где число - id товара
    # поэтому ищем элементы, где id начинается с "price-template--"

    # сначала пробуем через beautifulsoup с поиском по классу
    try:
        # ищем span с классом price-item
        price_elem = soup.find("span", class_="price-item")
        if price_elem:
            price_text = price_elem.get_text(strip=True)
            if price_text:
                parsed_price, parsed_currency = parse_price_and_currency(price_text)
                if parsed_price is not None and parsed_price > 0:
                    price = parsed_price
                    currency = parsed_currency or "USD"
                    log_event(
                        log,
                        "parse.price.found",
                        level="debug",
                        source="css:.price-item",
                        price=price,
                        currency=currency,
                    )
    except Exception as e:
        log_exception(log, "parse.price.error", e, level="warning", source="css:.price-item")

    # fallback: специфичные xpath для fashionphile (если beautifulsoup не сработал)
    if price is None:
        fashionphile_specific_xpaths = [
            "//span[contains(@class, 'price-item')]",  # основной класс
            "//span[contains(@class, 'price-item--regular')]",
            "//div[starts-with(@id, 'price-template--')]//span",  # поиск по началу id
            "//div[starts-with(@id, 'ProductInfo-template--')]//span[contains(@class, 'price')]",
            "//span[contains(@class, 'price') and contains(@class, 'h5')]",
        ]

        for xpath in fashionphile_specific_xpaths:
            try:
                price_text = get_text_by_xpath(soup, xpath)
                if price_text:
                    parsed_price, parsed_currency = parse_price_and_currency(price_text)
                    if parsed_price is not None and parsed_price > 0:
                        price = parsed_price
                        currency = parsed_currency or "USD"
                        log_event(
                            log,
                            "parse.price.found",
                            level="debug",
                            source="xpath",
                            xpath=xpath,
                            price=price,
                            currency=currency,
                        )
                        break
            except Exception:
                continue

    # проверяем статус:
    # 1) sold-out badge (приоритет)
    # 2) наличие кнопки "Add to Bag"
    # кнопка имеет id вида "ProductSubmitButton-template--24380755706159__main"
    # используем поиск по тексту внутри кнопки и по началу id
    try:
        # метод 0: явный sold-out badge из блока цены
        has_sold_badge = xpath_exists(
            soup,
            "//div[starts-with(@id, 'price-template--')]"
            "//div[contains(@class, 'fp-pdp-badges__container')]"
            "/span[contains(@class, 'price__badge-sold-out')]",
        )
        if not has_sold_badge:
            sold_badge = soup.select_one("div.fp-pdp-badges__container span.price__badge-sold-out")
            has_sold_badge = sold_badge is not None
        if not has_sold_badge:
            has_sold_badge = xpath_exists(
                soup,
                "//div[contains(@class, 'fp-pdp-badges__container')]"
                "//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sold out')]",
            )

        # метод 1: поиск span с текстом "Add to Bag" внутри кнопки
        has_add_to_bag = xpath_exists(soup, "//button//span[contains(., 'Add to Bag')]")
        if not has_add_to_bag:
            # метод 2: поиск кнопки с id, начинающимся с "ProductSubmitButton-template--"
            has_add_to_bag = xpath_exists(soup, "//button[starts-with(@id, 'ProductSubmitButton-template--')]")
        if not has_add_to_bag:
            # метод 3: поиск через beautifulsoup
            button = soup.find("button", id=lambda x: x and x.startswith("ProductSubmitButton-template--"))
            has_add_to_bag = button is not None

        if has_sold_badge:
            status = "Sold Out"
        elif has_add_to_bag:
            status = "в продаже"
        else:
            status = "Sold Out"
    except Exception:
        status = "Sold Out"

    # если цена не найдена и товар продан, оставляем None
    if status == "Sold Out":
        price = None
        currency = None
    if price is None:
        currency = None
    elif currency is None:
        currency = "USD"

    return title, price, currency, status

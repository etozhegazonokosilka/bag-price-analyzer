"""
парсер для shop.rebag.com
использует xpath для поиска элементов"""

from bs4 import BeautifulSoup

from utils.xpath_helper import get_text_by_xpath, xpath_exists
from utils.price import parse_price_and_currency, extract_price_universal
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


def scrape_rebag(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара shop.rebag.com

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
    try:
        title_text = get_text_by_xpath(soup, "//*[@id='pdp__title']")
        if title_text:
            title = title_text
        else:
            # fallback: пробуем другие селекторы для названия
            title_fallbacks = [
                "//meta[@property='og:title']/@content",  # open Graph title - часто самый надежный
                "//h1[contains(@class, 'pdp')]",
                "//h1[@data-testid='product-title']",
                "//div[contains(@class, 'product-title')]",
                "//span[contains(@class, 'product-name')]",
                "//h1",
                "//h2[contains(@class, 'product')]",
                "//div[@data-cy='product-title']",
                "//span[@data-cy='product-name']",
            ]
            for xpath in title_fallbacks:
                try:
                    title_text = get_text_by_xpath(soup, xpath)
                    if title_text and len(title_text.strip()) > 5:  # минимум 5 символов
                        title = title_text.strip()
                        break
                except Exception:
                    continue
    except Exception:
        pass

    # извлекаем цену с помощью универсальной функции многоуровневого поиска
    # (jSON-LD -> JavaScript -> XPath -> CSS селекторы)
    try:
        price, currency = extract_price_universal(soup, "Rebag", debug=False)
        if price is not None and price > 0:
            currency = currency or "USD"
    except Exception as e:
        log_exception(log, 'parse.price.error', e, level='warning', source='universal')
        pass

    # fallback: специфичные XPath для Rebag (если универсальная функция не сработала)
    if price is None:
        rebag_specific_xpaths = [
            "//*[@id='pdp__price']/div/div/div/div/span",  # точный xpath из карточки товара
            "//*[@id='pdp__price']//span[contains(@class, 'rewards-plus-pdp__product-price-value')]",
            "//span[@class='rewards-plus-pdp__product-price-value']",  # оригинальный
            "//span[contains(@class, 'price')]",
            "//div[contains(@class, 'price')]//span",
            "//span[contains(@class, 'product-price')]",
        ]

        for xpath in rebag_specific_xpaths:
            try:
                price_text = get_text_by_xpath(soup, xpath)
                if price_text:
                    parsed_price, parsed_currency = parse_price_and_currency(price_text)
                    if parsed_price is not None and parsed_price > 0:
                        price = parsed_price
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

    # проверяем статус: сначала sold-маркер "Set Back In Stock Alert", затем add-to-cart
    try:
        has_stock_alert = xpath_exists(
            soup,
            "//*[@id='pdp__get-notified']/button"
            "[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'set back in stock alert')]",
        )
        has_add_to_cart = xpath_exists(soup, "//*[@id='add-to-cart-button-input']")
        if has_stock_alert:
            status = "Sold Out"
        elif has_add_to_cart:
            status = "в продаже"
        else:
            status = "Sold Out"
    except Exception:
        status = "Sold Out"

    # если цена не найдена и товар продан, оставляем None
    if price is None:
        currency = None
    elif currency is None:
        currency = "USD"

    return title, price, currency, status

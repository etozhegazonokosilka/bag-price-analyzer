"""
парсер для annsfabulousfinds.com
использует xpath для поиска элементов"""

from bs4 import BeautifulSoup

from utils.xpath_helper import get_text_by_xpath, xpath_exists
from utils.price import parse_price_and_currency, extract_price_universal
from utils.logger import get_logger, log_event

log = get_logger(__name__)


def scrape_annsfabulousfinds(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара annsfabulousfinds.com

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
        title_text = get_text_by_xpath(soup, "//h1[@class='title']")
        if title_text:
            title = title_text
    except Exception:
        pass

    # для этого сайта универсальная функция часто находит неправильную цену из JS
    # поэтому сразу переходим к специфичным XPath селекторам
    price = None
    currency = None

    annsfabulousfinds_specific_xpaths = [
        "//h2[contains(@class, 'actual-price')]",  # оригинальный
        "//span[contains(@class, 'price')]",
        "//div[contains(@class, 'price')]//span",
        "//h2[contains(@class, 'price')]",
        "//h2[contains(text(), '$')]",  # новый: h2 элементы содержащие $
        "//h2[starts-with(text(), '##')]",  # новый: h2 элементы начинающиеся с ##
    ]

    for xpath in annsfabulousfinds_specific_xpaths:
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

    # проверяем статус через наличие кнопки "add to cart"
    try:
        has_add_to_cart = xpath_exists(soup, "//*[@id='add_to_cart']")
        if has_add_to_cart:
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

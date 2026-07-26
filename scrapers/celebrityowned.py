"""
парсер для celebrityowned.com
использует xpath для поиска элементов"""

import re
from bs4 import BeautifulSoup

from utils.xpath_helper import get_text_by_xpath, get_attribute_by_xpath
from utils.price import parse_price_and_currency, extract_price_universal
from services.currency import convert_to_usd
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


def scrape_celebrityowned(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара celebrityowned.com

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
        title_text = get_text_by_xpath(soup, "//h3[@itemprop='name']")
        if title_text:
            title = title_text
    except Exception:
        pass

    # извлекаем цену - приоритет атрибуту content из элемента ProductPrice
    # цена обычно в формате EUR (например: content="2313.95" с символом € в тексте)
    price = None
    currency = None

    # сначала пробуем атрибут content (наиболее надежный источник)
    try:
        price_content = get_attribute_by_xpath(soup, "//*[@id='ProductPrice']", "content")
        if price_content:
            try:
                # атрибут content содержит чистое число без символа валюты
                price_eur = float(price_content.replace(",", "."))
                # конвертируем EUR в USD
                price_usd = convert_to_usd(price_eur, "EUR")
                if price_usd is not None:
                    price = price_usd
                    currency = "USD"
                    log_event(
                        log,
                        'parse.price.found',
                        level='debug',
                        source='attr:content',
                        converted=True,
                        price=price,
                        currency=currency,
                    )
                else:
                    # если конвертация не удалась, оставляем в EUR
                    price = price_eur
                    currency = "EUR"
                    log_event(
                        log,
                        'parse.price.found',
                        level='debug',
                        source='attr:content',
                        converted=False,
                        price=price,
                        currency=currency,
                    )
            except (ValueError, TypeError) as e:
                log_exception(log, 'parse.price.error', e, level='warning', source='attr:content')
    except Exception as e:
        log_exception(log, 'parse.price.error', e, level='warning', source='attr:content')
    # fallback: если атрибут content не сработал, пробуем текст элемента
    if price is None:
        celebrityowned_specific_xpaths = [
            "//*[@id='ProductPrice']",  # основной селектор
            "//span[contains(@class, 'price')]",
            "//div[contains(@class, 'price')]//span",
            "//span[@itemprop='price']",
            "//h4[contains(text(), '$')]",
            "//h4[contains(text(), '€')]",
        ]

        for xpath in celebrityowned_specific_xpaths:
            try:
                price_text = get_text_by_xpath(soup, xpath)
                if price_text:
                    parsed_price, parsed_currency = parse_price_and_currency(price_text)
                    if parsed_price is not None and parsed_price > 0:
                        currency = parsed_currency or "EUR"
                        # конвертируем в USD если валюта EUR
                        if currency and currency.upper() == "EUR":
                            price_usd = convert_to_usd(parsed_price, "EUR")
                            if price_usd is not None:
                                price = price_usd
                                currency = "USD"
                            else:
                                price = parsed_price
                        else:
                            price = parsed_price
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

    # проверяем статус через количество товара
    try:
        count_text = get_text_by_xpath(soup, "//span[contains(@class, 'product-count')]")
        if count_text:
            # извлекаем число из текста
            numbers = re.findall(r'\d+', count_text)
            if numbers:
                count = int(numbers[0])
                if count > 0:
                    status = "в продаже"
                else:
                    status = "Sold Out"
            else:
                status = "Sold Out"
        else:
            status = "Sold Out"
    except Exception:
        status = "Sold Out"

    # если цена не найдена и товар продан, оставляем None
    if price is None:
        currency = None
    elif currency is None:
        currency = "EUR"

    return title, price, currency, status

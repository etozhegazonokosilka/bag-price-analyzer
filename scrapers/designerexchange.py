"""
парсер для designerexchange.com
использует xpath для поиска элементов"""

from bs4 import BeautifulSoup

from utils.xpath_helper import get_text_by_xpath, xpath_exists
from utils.price import parse_price_and_currency, extract_price_universal
from services.currency import convert_to_usd
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


def scrape_designerexchange(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара designerexchange.com

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
        title_text = get_text_by_xpath(soup, "//h4[@class='pname']")
        if title_text:
            title = title_text
    except Exception:
        pass

    # извлекаем цену с помощью универсальной функции многоуровневого поиска
    # (jSON-LD -> JavaScript -> XPath -> CSS селекторы)
    try:
        price, currency = extract_price_universal(soup, "DesignerExchange", debug=False)
        if price is not None and price > 0:
            currency = currency or "GBP"
            # если валюта GBP, конвертируем в USD
            if currency and currency.upper() == "GBP":
                price_usd = convert_to_usd(price, "GBP")
                if price_usd is not None:
                    price = price_usd
                    currency = "USD"
    except Exception as e:
        log_exception(log, 'parse.price.error', e, level='warning', source='universal')
        pass

    # fallback: специфичные XPath для DesignerExchange (если универсальная функция не сработала)
    if price is None:
        designerexchange_specific_xpaths = [
            "//div[contains(@class, 'prodetails-pricebox')]//div[contains(@class, 'head')]",  # оригинальный
            "//span[contains(@class, 'price')]",
            "//div[contains(@class, 'price')]//span",
            "//div[contains(@class, 'prodetails-pricebox')]//span",
        ]

        for xpath in designerexchange_specific_xpaths:
            try:
                price_text = get_text_by_xpath(soup, xpath)
                if price_text:
                    parsed_price, parsed_currency = parse_price_and_currency(price_text)
                    if parsed_price is not None and parsed_price > 0:
                        currency = parsed_currency or "GBP"
                        # конвертируем в USD если нужно
                        if currency and currency.upper() == "GBP":
                            price_usd = convert_to_usd(parsed_price, "GBP")
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

    # проверяем статус через наличие кнопки "ADD TO BASKET"
    try:
        has_add_to_basket = xpath_exists(soup, "//a[contains(., 'ADD TO BASKET')]")
        if has_add_to_basket:
            status = "в продаже"
        else:
            status = "Sold Out"
    except Exception:
        status = "Sold Out"

    # если цена не найдена и товар продан, оставляем None
    if price is None:
        currency = None
    elif currency is None:
        currency = "GBP"

    # финальная конвертация в USD (если удалось)
    if price is not None and currency:
        price_usd = convert_to_usd(price, currency)
        if price_usd is not None:
            price = price_usd
            currency = "USD"

    return title, price, currency, status

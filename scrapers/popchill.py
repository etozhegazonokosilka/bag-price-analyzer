"""
парсер для popchill.com (тайваньский сайт)
использует xpath для поиска элементов"""

import json
from bs4 import BeautifulSoup

from utils.xpath_helper import get_text_by_xpath, xpath_exists
from utils.price import parse_price_and_currency, extract_price_from_jsonld
from services.currency import convert_to_usd
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


def scrape_popchill(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара popchill.com

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
        title_text = get_text_by_xpath(soup, "//*[@id='__next']/div/div/main/div/div[2]/div[1]/div[2]/h1")
        if title_text:
            title = title_text.strip()
    except Exception:
        pass

    # многоуровневый поиск цены для разных версий сайта (zh-TW, zh-HK, en)

    # уровень 1: поиск в meta tags (самый надежный, работает на всех языковых версиях)
    try:
        # ищем og:price:amount или product:price:amount
        meta_price_tags = [
            soup.find("meta", {"property": "og:price:amount"}),
            soup.find("meta", {"property": "product:price:amount"}),
            soup.find("meta", {"name": "product:price:amount"}),
        ]

        for meta_price in meta_price_tags:
            if meta_price and meta_price.get("content"):
                try:
                    price_val = float(meta_price.get("content").replace(",", ""))
                    if price_val > 0:
                        price = price_val
                        currency = "TWD"  # обычно в meta tags валюта не указана, для Popchill это TWD
                        # проверяем currency в отдельном meta tag
                        meta_currency_tags = [
                            soup.find("meta", {"property": "og:price:currency"}),
                            soup.find("meta", {"property": "product:price:currency"}),
                        ]
                        for meta_currency in meta_currency_tags:
                            if meta_currency and meta_currency.get("content"):
                                currency = meta_currency.get("content").upper()
                                break
                        log_event(
                            log,
                            'parse.price.found',
                            level='debug',
                            source='meta',
                            price=price,
                            currency=currency,
                        )
                        break
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        log_exception(log, 'parse.price.error', e, level='debug', source='meta')
    # уровень 2: поиск в JSON-LD (Schema.org) - работает до React рендеринга
    if price is None:
        try:
            jsonld_scripts = soup.find_all('script', {'type': 'application/ld+json'})
            for script in jsonld_scripts:
                try:
                    if script.string:
                        data = json.loads(script.string.strip())
                        parsed_price, parsed_currency = extract_price_from_jsonld(data)
                        if parsed_price is not None and parsed_price > 0:
                            price = parsed_price
                            currency = parsed_currency or "TWD"
                            log_event(
                                log,
                                'parse.price.found',
                                level='debug',
                                source='jsonld',
                                price=price,
                                currency=currency,
                            )
                            break
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception as e:
            log_exception(log, 'parse.price.error', e, level='debug', source='jsonld')
    # уровень 3: извлекаем цену через xpath (работает для zh-TW версии)
    if price is None:
        try:
            price_text = get_text_by_xpath(
                soup,
                "//*[@id='__next']/div/div/main/div/div[2]/div[1]/div[2]/div[1]/div[1]/div/span",
            )
            if price_text:
                # очищаем текст цены от символов валюты и пробелов, оставляем только число
                price_clean = price_text.replace(
                    "TWD",
                    "",
                ).replace("NT$", "").replace("HK$", "").replace(" ", "").replace(",", "").strip()
                try:
                    price_val = float(price_clean)
                    if price_val > 0:
                        # определяем валюту по символу в тексте
                        if "HK$" in price_text:
                            currency = "HKD"
                        elif "NT$" in price_text or "TWD" in price_text:
                            currency = "TWD"
                        else:
                            currency = "TWD"  # по умолчанию для Popchill
                        price = price_val
                        log_event(
                            log,
                            'parse.price.found',
                            level='debug',
                            source='xpath',
                            price=price,
                            currency=currency,
                        )
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    # проверяем статус через наличие кнопки "Купить"
    try:
        has_buy_button = xpath_exists(
            soup,
            "//*[@id='__next']/div/div/main/div/div[2]/div[1]/div[2]/div[4]/div/div/button",
        )
        if has_buy_button:
            status = "в продаже"
        else:
            status = "Sold Out"
    except Exception:
        status = "Sold Out"

    # если цена не найдена и товар продан, оставляем None
    if price is None:
        currency = None
    elif currency is None:
        currency = "TWD"

    return title, price, currency, status

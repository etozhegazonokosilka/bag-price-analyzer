"""
парсер для yoogiscloset.com
использует xpath для поиска элементов"""

from bs4 import BeautifulSoup

import json

from utils.xpath_helper import get_text_by_xpath, xpath_exists, get_attribute_by_xpath
from utils.price import parse_price_and_currency, extract_price_universal


from utils.logger import get_logger, log_event

log = get_logger(__name__)


def _format_log_value(value: object, limit: int = 200) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    if any(ch.isspace() for ch in text) or '"' in text:
        text = text.replace('"', "'")
        return f'"{text}"'
    return text


def _log(prefix: str, message: str, **fields) -> None:
    # единый структурированный лог для yoogiscloset
    level_map = {
        "INFO": "info",
        "ERR": "error",
        "PROXY": "warning",
    }
    level = level_map.get(str(prefix).upper(), "info")
    log_event(log, message, level=level, **fields)


def _is_sold_out_status(status: str | None) -> bool:
    # проверяем, что статус указывает на проданный/отсутствующий товар
    if not status:
        return False
    s = status.lower()
    markers = ["sold", "sold out", "out of stock", "unavailable", "нет в наличии"]
    return any(marker in s for marker in markers)


def extract_status_yoogiscloset(soup: BeautifulSoup) -> str:
    """
    определяет статус товара до парсинга цены
    возвращает "Sold" или "В продаже"
"""
    def _availability_is_sold(value: str) -> bool:
        if not value:
            return False
        v = value.lower()
        sold_markers = [
            "outofstock", "out of stock",
            "soldout", "sold out",
            "unavailable", "no longer available",
        ]
        return any(marker in v for marker in sold_markers)

    def _json_has_sold_availability(obj) -> bool:
        if isinstance(obj, dict):
            for key, val in obj.items():
                if isinstance(key, str) and key.lower() in {"availability", "availabilitystatus"}:
                    if isinstance(val, str) and _availability_is_sold(val):
                        return True
                if isinstance(val, (dict, list)) and _json_has_sold_availability(val):
                    return True
        elif isinstance(obj, list):
            for item in obj:
                if _json_has_sold_availability(item):
                    return True
        return False

    # быстрые проверки по structured data (JSON-LD / meta availability)
    try:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            if not script.string:
                continue
            try:
                data = json.loads(script.string.strip())
            except Exception:
                continue
            if _json_has_sold_availability(data):
                return "Sold"
    except Exception:
        pass

    try:
        availability_selectors = [
            "meta[itemprop='availability']",
            "link[itemprop='availability']",
            "meta[property='product:availability']",
            "meta[name='availability']",
        ]
        for sel in availability_selectors:
            for tag in soup.select(sel):
                content = tag.get("content") or tag.get("href") or tag.get_text(" ", strip=True)
                if content and _availability_is_sold(content):
                    return "Sold"
    except Exception:
        pass

    # быстрые проверки по кнопке и тексту
    try:
        sold_out_text = get_text_by_xpath(soup, "//*[@id='drift-container']/div[2]/div[1]/div/button/span")
        if sold_out_text:
            t = sold_out_text.lower()
            if "recently sold" in t or "sold" in t or "out of stock" in t:
                return "Sold"
    except Exception:
        pass

    try:
        page_text_lower = soup.get_text().lower()
        sold_phrases = [
            "recently sold",
            "sold out",
            "out of stock",
            "no longer available",
            "this item has sold",
        ]
        if any(phrase in page_text_lower for phrase in sold_phrases):
            return "Sold"
    except Exception:
        pass

    # если кнопка "Add To Shopping Bag" есть, но она отключена — считаем, что продано
    add_to_bag_xpath = (
        "//button[contains(., 'Add To Shopping Bag') or contains(., 'Add to Bag') or contains(., 'Add To Bag')]"
    )
    try:
        disabled_attr = get_attribute_by_xpath(soup, add_to_bag_xpath, "disabled")
        aria_disabled = get_attribute_by_xpath(soup, add_to_bag_xpath, "aria-disabled")
        if disabled_attr is not None or (aria_disabled and aria_disabled.strip().lower() in {"true", "1"}):
            return "Sold"
    except Exception:
        pass

    # отсутствие кнопки может быть из-за js/антибота, не считаем это sold автоматически
    try:
        has_add_to_bag = xpath_exists(soup, add_to_bag_xpath)
        if has_add_to_bag:
            return "В продаже"
    except Exception:
        pass

    return "Unknown"


def scrape_yoogiscloset(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара yoogiscloset.com

    аргументы:
        soup: объект beautifulsoup со страницей товара

    возвращает:
        tuple (название, цена, валюта, статус)
"""
    # шАГ 1: определяем статус до парсинга цены
    status = extract_status_yoogiscloset(soup)
    is_sold_out = _is_sold_out_status(status)

    title = None
    try:
        title_text = get_text_by_xpath(soup, "//h1[@itemprop='name']")
        if title_text:
            title = title_text
    except Exception:
        pass

    # шАГ 2: если товар продан/нет в наличии — сразу выходим без поиска цены
    if is_sold_out:
        _log("INFO", "yoogis_sold_out", status=status, action="skip_price")
        return title, None, None, status

    # если товар НЕ продан, продолжаем парсинг
    price = None
    currency = None
    # статус уже определён выше

    # ограничение области поиска цены: ищем ТОЛЬКО в блоке product-info
    # это предотвращает «подхват» цен из блоков «вам также может понравиться»
    product_info_soup = soup
    try:
        # ищем главный контейнер с информацией о товаре
        product_info = soup.find(id="product-info")
        if product_info is None:
            # пробуем альтернативные селекторы для главного контейнера
            product_info = soup.find(class_="product-info")
        if product_info is None:
            product_info = soup.find(class_="product-details")
        if product_info is None:
            # если не нашли специфичный блок, используем весь soup, но с ограничениями
            product_info_soup = soup
        else:
            # используем только блок product-info для поиска цены
            product_info_soup = BeautifulSoup(str(product_info), 'html.parser')
            _log("INFO", "yoogis_price_scope", scope="product-info")
    except Exception:
        # если ошибка при поиске блока, используем весь soup
        product_info_soup = soup

    # извлекаем цену с помощью универсальной функции многоуровневого поиска
    # (jSON-LD -> JavaScript -> XPath -> CSS селекторы)
    # ищем ТОЛЬКО в блоке product-info
    try:
        price, currency = extract_price_universal(product_info_soup, "Yoogi'sCloset", debug=False)
        if price is not None and price > 0:
            currency = currency or "USD"
    except Exception as e:
        _log("ERR", "yoogis_price_error", stage="universal", error=str(e))
        pass

    # fallback: специфичные селекторы для yoogi'scloset
    # важно: ищем ТОЛЬКО в блоке product-info
    if price is None:
        # используем beautifulsoup для поиска в ограниченной области
        try:
            # ищем элементы, содержащие символ $ в тексте, ТОЛЬКО в product-info
            price_candidates = product_info_soup.find_all(string=lambda text: text and '$' in text)
            for candidate in price_candidates:
                try:
                    price_text = candidate.strip()
                    # игнорируем слишком длинные строки (вероятно, это не цена)
                    if len(price_text) > 50:
                        continue
                    parsed_price, parsed_currency = parse_price_and_currency(price_text)
                    if parsed_price is not None and parsed_price > 0:
                        price = parsed_price
                        currency = parsed_currency or "USD"
                        _log("INFO", "yoogis_price_found", source="text", price=price, currency=currency)
                        break
                except Exception:
                    continue

            if price is not None:
                # цена найдена, выходим
                pass
        except Exception as e:
            _log("ERR", "yoogis_price_error", stage="bs_text", error=str(e))

        # если не нашли через beautifulsoup, пробуем xpath (без учета ya-tr-span)
        # ищем ТОЛЬКО в блоке product-info
        if price is None:
            yoogiscloset_specific_xpaths = [
                "//span[@itemprop='price']",
                "//div/span/span[@itemprop='price']",
                "//span[contains(@class, 'price')]",
                "//div[contains(@class, 'price')]//span",
                "//h1/following-sibling::div//text()[contains(., '$')]",  # текстовые узлы после h1
            ]

            for xpath in yoogiscloset_specific_xpaths:
                try:
                    price_text = get_text_by_xpath(product_info_soup, xpath)
                    if price_text:
                        parsed_price, parsed_currency = parse_price_and_currency(price_text)
                        if parsed_price is not None and parsed_price > 0:
                            price = parsed_price
                            currency = parsed_currency or "USD"
                            _log(
                                "INFO",
                                "yoogis_price_found",
                                source="xpath",
                                price=price,
                                currency=currency,
                                xpath=xpath,
                            )
                            break
                except Exception:
                    continue

    # финальный фильтр: если товар продан (Sold), цена ВСЕГДА должна быть None
    if _is_sold_out_status(status):
        _log("INFO", "yoogis_sold_out", status=status, action="force_price_null")
        price = None
        currency = None
        return title, None, None, status

    if status == "Unknown" and price is not None:
        status = "В продаже"

    # если цена не найдена, оставляем None
    if price is None:
        currency = None
    elif currency is None:
        currency = "USD"

    return title, price, currency, status


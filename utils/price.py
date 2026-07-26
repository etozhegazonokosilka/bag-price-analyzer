"""
утилиты для работы с ценами и валютами"""
import re
import requests

# список валют и их символов/кодов
CURRENCY_MAP = {
    "$": "USD", "USD": "USD", "US$": "USD", "US $": "USD",
    "€": "EUR", "EUR": "EUR", "EURO": "EUR", "EUROS": "EUR",
    "£": "GBP", "GBP": "GBP", "GB £": "GBP",
    "¥": "JPY", "JPY": "JPY", "JP ¥": "JPY",
    "₽": "RUB", "RUB": "RUB", "RUR": "RUB",
    "CAD": "CAD", "CA$": "CAD", "C$": "CAD",
    "AUD": "AUD", "AU$": "AUD", "A$": "AUD",
    "CHF": "CHF",
    "CNY": "CNY", "CN¥": "CNY", "RMB": "CNY",
    "INR": "INR", "₹": "INR",
    "KRW": "KRW", "₩": "KRW",
    "MXN": "MXN", "MX$": "MXN",
    "BRL": "BRL", "R$": "BRL",
    "ZAR": "ZAR", "R": "ZAR",
    "TWD": "TWD", "NT$": "TWD", "NT": "TWD",  # тайваньский доллар
    "HKD": "HKD", "HK$": "HKD",  # гонконгский доллар
    "SGD": "SGD", "S$": "SGD",  # сингапурский доллар
}


def normalize_currency_code(value: str | None) -> str | None:
    """
    нормализует код валюты и приводит символы к трехбуквенному коду
"""
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    raw = re.sub(r"(?iu)\bруб(?:\.|ля|лей|ль)?\b", "RUB", raw)

    upper = raw.upper()
    if upper in CURRENCY_MAP:
        return CURRENCY_MAP[upper]

    compact = upper.replace(" ", "")
    if compact in CURRENCY_MAP:
        return CURRENCY_MAP[compact]

    # если строка уже похожа на код валюты
    if re.fullmatch(r"[A-Z]{3}", compact):
        return compact

    # аккуратно ищем известные символы в строке
    for symbol, code in CURRENCY_MAP.items():
        if len(symbol) == 1:
            if symbol == raw:
                return code
        else:
            if symbol in raw or symbol.upper() in upper:
                return code

    return None


def parse_price_and_currency(text: str) -> tuple[float | None, str | None]:
    # улучшенная попытка извлечь цену и валюту из произвольного текста
    # поддержка $, €, £, ¥, ₽, cad, usd, eur, gbp, jpy, rub и других валют
    # поддержка различных форматов: $1,234.56, 1234.56 usd, 1,234.56$, и т.д
    if not text:
        return None, None

    text = text.strip()
    text = re.sub(r"(?iu)\bруб(?:\.|ля|лей|ль)?\b", "RUB", text)
    currency = None

    # сначала определяем валюту по символам/кодам
    text_upper = text.upper()
    for symbol, curr in CURRENCY_MAP.items():
        symbol_upper = symbol.upper()
        # для односимвольных буквенных "валют" (например, "R") не допускаем
        # совпадение внутри обычных слов, иначе в title ловятся ложные ZAR
        if len(symbol_upper) == 1 and symbol_upper.isalpha():
            if re.search(rf"(?<![A-Z0-9]){re.escape(symbol_upper)}(?![A-Z0-9])", text_upper):
                currency = curr
                break
            continue
        if symbol in text or symbol_upper in text_upper:
            currency = curr
            break

    # улучшенные паттерны для поиска цены
    # паттерн 1: символ валюты перед числом ($1,234.56, €1.234,56, £1,234)
    pattern1 = r'([\$€£¥₽₹₩]|USD|EUR|GBP|JPY|RUB|CAD|AUD|CHF|CNY|INR|KRW|MXN|BRL|ZAR|US\$|CA\$|AU\$|MX\$|CN¥|JP¥|GB\s*£|R\$)\s*([0-9]{1,3}(?:[,\s][0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)'
    # паттерн 2: число перед символом валюты (1,234.56$, 1234.56 usd)
    pattern2 = r'([0-9]{1,3}(?:[,\s][0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)\s*([\$€£¥₽₹₩]|USD|EUR|GBP|JPY|RUB|CAD|AUD|CHF|CNY|INR|KRW|MXN|BRL|ZAR|US\$|CA\$|AU\$|MX\$|CN¥|JP¥|GB\s*£|R\$)'
    # паттерн 3: просто число с разделителями (может быть без валюты)
    pattern3 = r'([0-9]{1,3}(?:[,\s][0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)'

    price = None

    # пробуем паттерн 1
    m = re.search(pattern1, text, re.IGNORECASE)
    if m:
        curr_match = m.group(1).upper().strip()
        num_str = m.group(2)
        # определяем валюту из совпадения
        for symbol, curr in CURRENCY_MAP.items():
            if symbol.upper() in curr_match or curr_match in symbol.upper():
                currency = curr
                break
        # очищаем число от разделителей (поддержка европейского и американского формата)
        # европейский формат: 1.234,56 или 1 234,56 (точка/пробел - тысячи, запятая - десятичный)
        # американский формат: 1,234.56 (запятая - тысячи, точка - десятичный)

        # определяем формат: если запятая последняя и после нее 2 цифры - европейский формат
        if "," in num_str:
            comma_pos = num_str.rfind(",")
            after_comma = num_str[comma_pos+1:]
            if len(after_comma) == 2 and after_comma.isdigit():
                # европейский формат: запятая - десятичный разделитель
                num_str = num_str.replace(".", "").replace(" ", "").replace(",", ".")
            else:
                # американский формат: запятая - разделитель тысяч
                num_str = num_str.replace(",", "").replace(" ", "")
        else:
            # только точка - проверяем, тысячи или десятичный
            num_str = num_str.replace(" ", "")
            if "." in num_str:
                parts = num_str.split(".")
                if len(parts) == 2 and len(parts[1]) == 2:
                    # десятичный разделитель
                    num_str = parts[0] + "." + parts[1]
                else:
                    # разделитель тысяч - убираем все точки
                    num_str = num_str.replace(".", "")
        try:
            price = float(num_str)
            if price > 0:
                return price, currency
        except (ValueError, AttributeError):
            pass

    # пробуем паттерн 2
    m = re.search(pattern2, text, re.IGNORECASE)
    if m and price is None:
        num_str = m.group(1)
        curr_match = m.group(2).upper().strip()
        # определяем валюту из совпадения
        for symbol, curr in CURRENCY_MAP.items():
            if symbol.upper() in curr_match or curr_match in symbol.upper():
                currency = curr
                break
        # очищаем число от разделителей (европейский и американский форматы)
        if "," in num_str:
            comma_pos = num_str.rfind(",")
            after_comma = num_str[comma_pos+1:]
            if len(after_comma) == 2 and after_comma.isdigit():
                # европейский формат
                num_str = num_str.replace(".", "").replace(" ", "").replace(",", ".")
            else:
                # американский формат
                num_str = num_str.replace(",", "").replace(" ", "")
        else:
            num_str = num_str.replace(" ", "")
            if "." in num_str:
                parts = num_str.split(".")
                if len(parts) == 2 and len(parts[1]) == 2:
                    num_str = parts[0] + "." + parts[1]
                else:
                    num_str = num_str.replace(".", "")
        try:
            price = float(num_str)
            if price > 0:
                return price, currency
        except (ValueError, AttributeError):
            pass

    # пробуем паттерн 3 (только если есть валюта)
    if price is None and currency:
        m = re.search(pattern3, text)
        if m:
            num_str = m.group(1)
            # очищаем число от разделителей (европейский и американский форматы)
            if "," in num_str:
                comma_pos = num_str.rfind(",")
                after_comma = num_str[comma_pos+1:]
                if len(after_comma) == 2 and after_comma.isdigit():
                    num_str = num_str.replace(".", "").replace(" ", "").replace(",", ".")
                else:
                    num_str = num_str.replace(",", "").replace(" ", "")
            else:
                num_str = num_str.replace(" ", "")
                if "." in num_str:
                    parts = num_str.split(".")
                    if len(parts) == 2 and len(parts[1]) == 2:
                        num_str = parts[0] + "." + parts[1]
                    else:
                        num_str = num_str.replace(".", "")
            try:
                price = float(num_str)
                if price > 0:
                    return price, currency
            except (ValueError, AttributeError):
                pass

    return None, currency


def is_valid_price_element(text: str, element_text: str = "") -> bool:
    # проверяет, является ли элемент с ценой валидной ценой товара
    # фильтрует ложные срабатывания: цены доставки, налоги, скидки и т.д
    if not text:
        return False

    combined_text = (text + " " + element_text).lower()

    # исключаем элементы, которые явно не являются ценами товара
    exclude_keywords = [
        "shipping", "delivery", "tax", "fee", "discount", "sale", "save",
        "original", "was", "compare", "retail", "msrp", "list price",
        "estimated", "approx", "approximately", "starting at", "from",
        "per month", "per week", "per day", "per hour", "installment",
        "deposit", "down payment", "processing", "handling", "service",
        "convenience", "convenience fee", "transaction", "payment",
        "subscription", "membership", "renewal", "activation",
        "insurance", "warranty", "protection", "extended",
        "clearance", "closeout", "liquidation", "wholesale",
        "minimum", "maximum", "range", "between", "up to",
        "starting from", "as low as", "as high as",
        "refund", "return", "exchange", "credit",
    ]

    # проверяем наличие исключающих ключевых слов
    for keyword in exclude_keywords:
        if keyword in combined_text:
            return False

    # проверяем, что цена не слишком маленькая и не слишком большая
    # извлекаем цену и валюту для более точной проверки
    try:
        parsed_price, parsed_currency = parse_price_and_currency(text)
        if parsed_price is not None:
            # максимальные разумные цены в зависимости от валюты (в usd эквиваленте)
            # для большинства товаров цена не должна превышать $10000
            max_prices = {
                "USD": 10000.0,
                "EUR": 9000.0,
                "GBP": 8000.0,
                "CAD": 13000.0,
                "AUD": 13000.0,
                "ZAR": 150000.0,  # порог: 150000 zar ≈ 8000-10000 usd
                "JPY": 1500000.0,  # порог: 1500000 jpy ≈ 10000 usd
                "CNY": 70000.0,  # порог: 70000 cny ≈ 10000 usd
                "INR": 800000.0,  # порог: 800000 inr ≈ 10000 usd
                "KRW": 13000000.0,  # порог: 13000000 krw ≈ 10000 usd
                "TWD": 300000.0,  # порог: 300000 twd ≈ 10000 usd
                "HKD": 80000.0,  # порог: 80000 hkd ≈ 10000 usd
                "SGD": 13000.0,  # порог: 13000 sgd ≈ 10000 usd
            }

            # проверка максимальной цены
            if parsed_currency and parsed_currency.upper() in max_prices:
                max_price = max_prices[parsed_currency.upper()]
                if parsed_price > max_price:
                    return False  # слишком высокая цена - вероятно ошибка парсинга
            elif parsed_price > 10000:  # для неизвестных валют максимум $10000
                return False

            # минимальные разумные цены в зависимости от валюты (смягченные пороги)
            # убираем слишком жесткие проверки, чтобы не фильтровать нормальные цены
            min_prices = {
                "USD": 5.0,  # снижен с 20 до 5
                "EUR": 5.0,  # с 18 до 5
                "GBP": 5.0,  # с 15 до 5
                "CAD": 5.0,  # с 25 до 5
                "AUD": 5.0,  # с 25 до 5
                "ZAR": 50.0,  # с 200 до 50
                "JPY": 500.0,  # с 2000 до 500
                "CNY": 30.0,  # с 100 до 30
                "INR": 300.0,  # с 1000 до 300
                "KRW": 5000.0,  # с 20000 до 5000
                "TWD": 150.0,  # с 500 до 150
                "HKD": 40.0,  # с 120 до 40
                "SGD": 5.0,  # с 20 до 5
            }

            # конвертируем в usd для проверки
            if parsed_currency and parsed_currency.upper() in min_prices:
                min_price = min_prices[parsed_currency.upper()]
                if parsed_price < min_price:
                    return False
            elif parsed_price < 1.0:  # для неизвестных валют минимум $1
                return False
    except (ValueError, AttributeError):
        pass

    return True


def extract_price_from_sold_text(text: str) -> tuple[float | None, str | None]:
    """извлекает цену из текста со статусом 'sold' (например, 'sold at $xxx', 'sold for $xxx', 'Sold at 1563,97 €')"""
    if not text:
        return None, None

    # сначала пробуем использовать parse_price_and_currency для более точного парсинга
    # это обработает европейский формат (запятая как десятичный разделитель)
    price, currency = parse_price_and_currency(text)
    if price is not None and price > 0:
        return price, currency

    # паттерны для поиска цены в тексте о продаже (fallback)
    patterns = [
        r'sold\s+(?:at|for)\s*[\$€£¥]?\s*([\d\s,\.]+)',
        r'sold\s+[\$€£¥]?\s*([\d\s,\.]+)',
        r'([\d\s,\.]+)\s*[\$€£¥]?\s+sold',
        r'sold\s+price[:\s]+[\$€£¥]?\s*([\d\s,\.]+)',
        r'final\s+price[:\s]+[\$€£¥]?\s*([\d\s,\.]+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                price_str = match.group(1).strip()
                # используем parse_price_and_currency для обработки формата
                parsed_price, parsed_currency = parse_price_and_currency(price_str + " " + text)
                if parsed_price and parsed_price > 0 and parsed_price <= 100000:
                    currency = parsed_currency
                    # если валюта не определена, пробуем определить из контекста
                    if not currency:
                        if "$" in text or "USD" in text.upper():
                            currency = "USD"
                        elif "€" in text or "EUR" in text.upper():
                            currency = "EUR"
                        elif "£" in text or "GBP" in text.upper():
                            currency = "GBP"
                        else:
                            currency = "USD"
                    return parsed_price, currency
            except (ValueError, AttributeError):
                continue

    return None, None


def extract_price_from_jsonld(data: dict | list) -> tuple[float | None, str | None]:
    """извлекает цену из json-ld структурированных данных, проверяя все возможные поля"""

    def extract_from_dict(obj: dict) -> tuple[float | None, str | None]:
        """рекурсивно извлекает цену из словаря"""
        if not isinstance(obj, dict):
            return None, None

        # проверяем offers (основной источник)
        offers = obj.get("offers")
        if isinstance(offers, dict):
            # проверяем price напрямую
            price_val = offers.get("price")
            if price_val:
                try:
                    price = float(str(price_val).replace(",", ""))
                    currency = (
                        offers.get("priceCurrency")
                        or offers.get("currency")
                        or offers.get("currencyCode")
                        or "USD"
                    )
                    currency = normalize_currency_code(currency) or "USD"
                    return price, currency
                except (ValueError, TypeError):
                    pass

            # проверяем pricespecification
            price_spec = offers.get("priceSpecification")
            if isinstance(price_spec, dict):
                price_val = price_spec.get("price") or price_spec.get("value")
                if price_val:
                    try:
                        price = float(str(price_val).replace(",", ""))
                        currency = (
                            price_spec.get("priceCurrency")
                            or price_spec.get("currency")
                            or offers.get("priceCurrency")
                            or offers.get("currency")
                            or "USD"
                        )
                        currency = normalize_currency_code(currency) or "USD"
                        return price, currency
                    except (ValueError, TypeError):
                        pass

        # проверяем aggregateoffer (для диапазона цен)
        aggregate_offer = obj.get("aggregateOffer")
        if isinstance(aggregate_offer, dict):
            # берем lowprice или highprice (предпочитаем lowprice)
            price_val = aggregate_offer.get("lowPrice") or aggregate_offer.get("highPrice")
            if price_val:
                try:
                    price = float(str(price_val).replace(",", ""))
                    currency = (
                        aggregate_offer.get("priceCurrency")
                        or aggregate_offer.get("currency")
                        or aggregate_offer.get("currencyCode")
                        or "USD"
                    )
                    currency = normalize_currency_code(currency) or "USD"
                    return price, currency
                except (ValueError, TypeError):
                    pass

        # проверяем price напрямую в объекте
        price_val = obj.get("price")
        if price_val:
            try:
                price = float(str(price_val).replace(",", ""))
                currency = (
                    obj.get("priceCurrency")
                    or obj.get("currency")
                    or obj.get("currencyCode")
                    or "USD"
                )
                currency = normalize_currency_code(currency) or "USD"
                return price, currency
            except (ValueError, TypeError):
                pass

        # рекурсивно проверяем вложенные объекты
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                p, c = extract_from_dict(value) if isinstance(value, dict) else extract_from_list(value)
                if p is not None:
                    return p, c

        return None, None

    def extract_from_list(items: list) -> tuple[float | None, str | None]:
        """извлекает цену из списка объектов"""
        for item in items:
            if isinstance(item, dict):
                p, c = extract_from_dict(item)
                if p is not None:
                    return p, c
            elif isinstance(item, list):
                p, c = extract_from_list(item)
                if p is not None:
                    return p, c
        return None, None

    if isinstance(data, dict):
        return extract_from_dict(data)
    elif isinstance(data, list):
        return extract_from_list(data)

    return None, None


def extract_price_from_scripts(soup) -> tuple[float | None, str | None]:
    # извлекает цену из всех script тегов (не только json-ld)
    # ищет javascript объекты с данными о цене, включая react state и window.__initial_state__
    price = None
    currency = None

    try:
        for script in soup.find_all("script"):
            script_text = script.string or ""
            if not script_text:
                continue

            # паттерны для поиска цены в javascript коде
            price_patterns = [
                # объекты с ценой
                r'["\']price["\']\s*[:=]\s*["\']?([\d,]+\.?\d*)["\']?',
                r'["\']priceValue["\']\s*[:=]\s*["\']?([\d,]+\.?\d*)["\']?',
                r'["\']currentPrice["\']\s*[:=]\s*["\']?([\d,]+\.?\d*)["\']?',
                r'["\']salePrice["\']\s*[:=]\s*["\']?([\d,]+\.?\d*)["\']?',
                r'["\']finalPrice["\']\s*[:=]\s*["\']?([\d,]+\.?\d*)["\']?',
                r'["\']soldPrice["\']\s*[:=]\s*["\']?([\d,]+\.?\d*)["\']?',
                r'["\']amount["\']\s*[:=]\s*["\']?([\d,]+\.?\d*)["\']?',
                r'["\']value["\']\s*[:=]\s*["\']?([\d,]+\.?\d*)["\']?',
                # без кавычек
                r'price\s*[:=]\s*([\d,]+\.?\d*)',
                r'priceValue\s*[:=]\s*([\d,]+\.?\d*)',
                r'currentPrice\s*[:=]\s*([\d,]+\.?\d*)',
                r'soldPrice\s*[:=]\s*([\d,]+\.?\d*)',
                # react state и window.__initial_state__
                r'window\.__INITIAL_STATE__[^}]*price["\']?\s*[:=]\s*["\']?([\d,]+\.?\d*)',
                r'productData[^}]*price["\']?\s*[:=]\s*["\']?([\d,]+\.?\d*)',
                r'itemData[^}]*price["\']?\s*[:=]\s*["\']?([\d,]+\.?\d*)',
                r'listingData[^}]*price["\']?\s*[:=]\s*["\']?([\d,]+\.?\d*)',
            ]

            for pattern in price_patterns:
                matches = re.finditer(pattern, script_text, re.IGNORECASE)
                for match in matches:
                    try:
                        price_str = match.group(1).replace(",", "").strip()
                        price_val = float(price_str)
                        # фильтруем слишком высокие цены (вероятно ошибка парсинга)
                        if price_val > 0 and price_val <= 10000:
                            # ищем валюту в том же скрипте
                            currency_patterns = [
                                r'["\']currency["\']\s*[:=]\s*["\']([A-Z]{3})["\']',
                                r'["\']priceCurrency["\']\s*[:=]\s*["\']([A-Z]{3})["\']',
                                r'currency\s*[:=]\s*["\']([A-Z]{3})["\']',
                            ]
                            for curr_pattern in currency_patterns:
                                curr_match = re.search(curr_pattern, script_text, re.IGNORECASE)
                                if curr_match:
                                    currency = curr_match.group(1).upper()
                                    break

                            if not currency:
                                # пытаемся определить валюту из контекста
                                context_start = max(0, match.start() - 100)
                                context_end = min(len(script_text), match.end() + 100)
                                context = script_text[context_start:context_end]
                                _, detected_currency = parse_price_and_currency(context)
                                if detected_currency:
                                    currency = detected_currency

                            price = price_val
                            if currency:
                                return price, currency
                    except (ValueError, AttributeError, IndexError):
                        continue
                if price:
                    break
            if price:
                break
    except Exception:
        pass

    return price, currency


def to_usd(price: float, currency: str | None) -> float | None:
    # конвертирует цену в usd через сервис конвертации валют
    if price is None:
        return None
    currency_norm = normalize_currency_code(currency)
    if not currency_norm or currency_norm.upper() == "USD":
        return float(price)

    # используем новый сервис конвертации с кэшированием
    try:
        from services.currency import convert_to_usd
        converted = convert_to_usd(price, currency_norm)
        if converted is not None:
            return converted
    except ImportError:
        pass

    # fallback на старый метод если сервис недоступен или вернул none
    return _fallback_to_usd(price, currency_norm)


def _fallback_to_usd(price: float, currency: str) -> float | None:
    # резервный метод конвертации (используется если сервис недоступен)
    try:
        url = f"https://api.exchangerate-api.com/v4/latest/{currency.upper()}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        rate = data.get("rates", {}).get("USD")
        if rate:
            return float(price) * float(rate)
        return None
    except Exception:
        return None


def extract_price_universal(soup, site_name: str = "unknown", debug: bool = None) -> tuple[float | None, str | None]:
    """
    универсальная функция многоуровневого поиска цены на странице товара

    порядок поиска (от наиболее надежного к менее надежному):
    1. jSON-LD структурированные данные
    2. javaScript объекты и переменные
    3. xPath селекторы (множественные варианты)
    4. cSS селекторы (как fallback)

    аргументы:
        soup: BeautifulSoup объект страницы
        site_name: название сайта для логирования
        debug: включить детальное логирование (None = автоопределение из DEBUG_PARSER)

    возвращает:
        tuple (цена, валюта) или (None, None)
"""
    import json
    import os
    from utils.xpath_helper import get_text_by_xpath

    # автоопределение debug режима
    if debug is None:
        debug = os.getenv('DEBUG_PARSER', 'false').lower() in ('true', '1', 'yes', 'on')

    if debug:
        from utils.logger import get_logger, log_event

        _log = get_logger(__name__)

        def print(*args, **kwargs):
            # прокидываем debug print() в структурированный лог
            try:
                sep = kwargs.get("sep", " ")
                msg = sep.join("" if a is None else str(a) for a in args)
            except Exception:
                msg = " ".join(str(a) for a in args)

            msg = msg.replace("\r", " ").replace("\n", " ")
            msg = " ".join(msg.split()).strip()
            if not msg:
                return

            for ch in [
                "\u2713",
                "\u2705",
                "\u26A0",
                "\ufe0f",
                "\u274c",
                "\U0001F50D",
                "\U0001F4CB",
                "\U0001F4BB",
                "\U0001F3AF",
                "\U0001F3A8",
                "\U0001F4A1",
                "\U0001F4CA",
                "\U0001F4C4",
                "\u2192",
            ]:
                msg = msg.replace(ch, "")

            msg = " ".join(msg.split()).strip()
            log_event(_log, "price.debug", level="debug", msg=msg, site=site_name)

        print(
f"🔍 {site_name}: Начинаю многоуровневый поиск цены...")
        print("   Уровни: JSON-LD → JavaScript → XPath → CSS")

    # уРОВЕНЬ 1: JSON-LD структурированные данные (самый надежный источник)
    if debug:
        print("   📋 Уровень 1: Ищу в JSON-LD данных...")
    try:
        jsonld_scripts = soup.find_all('script', {'type': 'application/ld+json'})
        if debug:
            print(f"      Найдено {len(jsonld_scripts)} JSON-LD скриптов")

        for script in jsonld_scripts:
            try:
                if script.string:
                    data = json.loads(script.string.strip())
                    price, currency = extract_price_from_jsonld(data)
                    if price is not None and price > 0 and is_valid_price_element(str(price), ""):
                        if debug:
                            print(f"✅ {site_name}: Цена найдена в JSON-LD: {price} {currency}")
                        return price, currency
            except (json.JSONDecodeError, TypeError):
                continue
    except Exception as e:
        if debug:
            print(f"⚠️ {site_name}: Ошибка при поиске в JSON-LD: {e}")

    # уРОВЕНЬ 2: JavaScript объекты и переменные
    if debug:
        print("   💻 Уровень 2: Ищу в JavaScript объектах...")
    try:
        price, currency = extract_price_from_scripts(soup)
        if price is not None and price > 0 and is_valid_price_element(str(price), ""):
            if debug:
                print(f"✅ {site_name}: Цена найдена в JavaScript: {price} {currency}")
            return price, currency
    except Exception as e:
        if debug:
            print(f"⚠️ {site_name}: Ошибка при поиске в JavaScript: {e}")

    # уРОВЕНЬ 3: XPath селекторы (универсальные для большинства сайтов)
    universal_price_xpaths = [
        # простые паттерны - любые элементы с ценоподобным текстом (самые надежные)
        "//*[contains(text(), '$') and string-length(text()) < 30]",
        "//*[contains(text(), '€') and string-length(text()) < 30]",
        "//*[contains(text(), '£') and string-length(text()) < 30]",
        "//*[contains(text(), '¥') and string-length(text()) < 30]",
        "//*[contains(text(), '₽') and string-length(text()) < 30]",

        # общие паттерны для цен с классами
        "//span[contains(@class, 'price')]",
        "//div[contains(@class, 'price')]",
        "//span[contains(@class, 'price') and contains(text(), '$')]",
        "//div[contains(@class, 'price') and contains(text(), '$')]",
        "//span[contains(@class, 'price') and contains(text(), '€')]",
        "//div[contains(@class, 'price') and contains(text(), '€')]",
        "//span[contains(@class, 'price') and contains(text(), '£')]",
        "//div[contains(@class, 'price') and contains(text(), '£')]",
        "//span[contains(@class, 'price') and contains(text(), '¥')]",
        "//div[contains(@class, 'price') and contains(text(), '¥')]",

        # цены в data атрибутах
        "//*[@data-price and string-length(@data-price) > 0]",
        "//*[@data-amount and string-length(@data-amount) > 0]",
        "//*[@data-value and string-length(@data-value) > 0]",
    ]

    if debug:
        print(f"   🎯 Уровень 3: Пробую универсальные XPath селекторы ({len(universal_price_xpaths)} вариантов)...")

    for xpath in universal_price_xpaths:
        try:
            price_text = get_text_by_xpath(soup, xpath)
            if price_text:
                # очищаем текст от лишнего
                price_text = price_text.strip()
                # проверяем, что это не слишком длинный текст (вероятно не цена)
                if len(price_text) < 50:
                    parsed_price, parsed_currency = parse_price_and_currency(price_text)
                    if parsed_price is not None and parsed_price > 0 and is_valid_price_element(price_text, ""):
                        if debug:
                            print(
                                f"✅ {site_name}: цена найдена по XPath '{xpath}': "
                                f"'{price_text}' -> {parsed_price} {parsed_currency}"
                            )
                        return parsed_price, parsed_currency
                    elif debug:
                        print(
                            f"❌ {site_name}: XPath '{xpath}' нашел текст "
                            f"'{price_text}', но не удалось распарсить цену"
                        )
        except Exception as e:
            if debug:
                print(f"⚠️ {site_name}: Ошибка с XPath '{xpath}': {e}")

    # уРОВЕНЬ 4: CSS селекторы как последний fallback
    if debug:
        print("   🎨 Уровень 4: CSS селекторы как fallback...")
    try:
        # ищем все элементы с ценоподобным текстом
        for element in soup.find_all(text=True):
            text = element.strip()
            if text and len(text) < 30:  # только короткие тексты
                parsed_price, parsed_currency = parse_price_and_currency(text)
                if parsed_price is not None and parsed_price > 0 and is_valid_price_element(text, ""):
                    if debug:
                        print(
                            f"✅ {site_name}: цена найдена в тексте элемента: "
                            f"'{text}' -> {parsed_price} {parsed_currency}"
                        )
                    return parsed_price, parsed_currency
    except Exception as e:
        if debug:
            print(f"⚠️ {site_name}: Ошибка при поиске в текстовых элементах: {e}")

    if debug:
        print(f"❌ {site_name}: Цена не найдена всеми методами")
        print("   💡 Рекомендации:")
        print("      - Проверьте структуру HTML страницы")
        print("      - Возможно цена загружается через AJAX")
        print("      - Попробуйте добавить специфичные XPath для этого сайта")

    return None, None

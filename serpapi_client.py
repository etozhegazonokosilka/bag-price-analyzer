"""
модуль для работы с SerpAPI (обратный поиск изображений через Google Lens)"""
import os
import re
import requests

from utils.logger import get_logger, log_event
from typing import Optional, Tuple, List, Dict

log = get_logger(__name__)


def load_env(filepath: str = ".env") -> None:
    """простая загрузка .env без внешних зависимостей"""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            # убираем BOM если есть
            if content.startswith('\ufeff'):
                content = content[1:]

            for line in content.split('\n'):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    # убираем комментарии после значения (все после #)
                    if "#" in val:
                        val = val.split("#")[0]
                    val = val.strip().strip('"').strip("'")
                    # пропускаем пустые значения
                    if val:
                        os.environ.setdefault(key, val)
    except Exception:
        # безопасно игнорируем ошибки чтения .env
        pass

# порядок важен:
# 1) .env (секреты/клиентские переменные)
# 2) .env.defaults (внутренние дефолты, только для отсутствующих ключей)
load_env(".env")
load_env(".env.defaults")

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")


def parse_price_and_currency(text: str) -> Tuple[Optional[float], Optional[str]]:
    """
    извлекает цену и валюту из произвольного текста
    поддержка $, €, £, ¥, ₽, CAD, USD, EUR, GBP, JPY, RUB и других валют
"""
    if not text:
        return None, None

    text = text.strip()

    # список валют и их символов/кодов
    currency_map = {
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
    }

    currency = None

    # сначала определяем валюту по символам/кодам
    text_upper = text.upper()
    for symbol, curr in currency_map.items():
        if symbol in text or symbol.upper() in text_upper:
            currency = curr
            break

    # улучшенные паттерны для поиска цены
    # паттерн 1: символ валюты перед числом ($1,234.56, €1.234,56, £1,234)
    pattern1 = r'([\$€£¥₽₹₩]|USD|EUR|GBP|JPY|RUB|CAD|AUD|CHF|CNY|INR|KRW|MXN|BRL|ZAR|US\$|CA\$|AU\$|MX\$|CN¥|JP¥|GB\s*£|R\$)\s*([0-9]{1,3}(?:[,\s][0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)'
    # паттерн 2: число перед символом валюты (1,234.56$, 1234.56 USD)
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
        for symbol, curr in currency_map.items():
            if symbol.upper() in curr_match or curr_match in symbol.upper():
                currency = curr
                break
        # очищаем число от разделителей
        num_str = num_str.replace(",", "").replace(" ", "")
        # если есть точка, она может быть разделителем тысяч (1.234) или десятичным (1.23)
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
        for symbol, curr in currency_map.items():
            if symbol.upper() in curr_match or curr_match in symbol.upper():
                currency = curr
                break
        # очищаем число от разделителей
        num_str = num_str.replace(",", "").replace(" ", "")
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

    # пробуем паттерн 3 (только если не нашли валюту ранее)
    if price is None:
        m = re.search(pattern3, text)
        if m:
            num_str = m.group(1)
            # очищаем число от разделителей
            num_str = num_str.replace(",", "").replace(" ", "")
            if "." in num_str:
                parts = num_str.split(".")
                if len(parts) == 2 and len(parts[1]) == 2:
                    num_str = parts[0] + "." + parts[1]
                else:
                    num_str = num_str.replace(".", "")
            try:
                price = float(num_str)
                if price > 0:
                    return price, currency or "USD"  # по умолчанию USD
            except (ValueError, AttributeError):
                pass

    return price, currency


def serpapi_google_lens(image_path: str) -> dict:
    """
    отправляет изображение на анализ через SerpAPI Google Lens
    возвращает JSON ответ от API
"""
    if not SERPAPI_KEY:
        raise RuntimeError("serpapi_key не задан. укажите SERPAPI_KEY в .env")

    # отправляем файл напрямую в SerpAPI
    log_event(log, "serpapi_client.upload", level="info", path=image_path)
    filename = os.path.basename(image_path)
    with open(image_path, "rb") as f:
        files = {"file": (filename, f, "image/jpeg")}
        data = {
            "engine": "google_lens",
            "api_key": SERPAPI_KEY,
        }
        log_event(log, "serpapi_client.request", level="info", engine=data.get("engine"))
        resp = requests.post("https://serpapi.com/search.json", files=files, data=data, timeout=120)
        resp.raise_for_status()
        result = resp.json()

    return result


def extract_results_from_serpapi(data: dict) -> List[Dict]:
    """
    извлекает ссылки, цены, статусы и валюты из ответа SerpAPI
    возвращает список словарей с информацией о товарах
"""
    results = []
    if not isinstance(data, dict):
        return results

    # visual_matches часто содержит нужные нам результаты
    visual_matches = data.get("visual_matches") or []
    for item in visual_matches:
        link = item.get("link") or item.get("source") or ""
        title = item.get("title") or ""

        # пробуем все возможные поля с ценой из visual_matches
        price_text = (
            item.get("price")
            or item.get("extracted_price")
            or item.get("price_with_symbol")
            or item.get("price_str")
            or ""
        )
        price_val, price_cur = parse_price_and_currency(str(price_text))
        # если не нашли, пробуем напрямую
        if price_val is None:
            try:
                if isinstance(item.get("extracted_price"), (int, float)):
                    price_val = float(item.get("extracted_price"))
                    price_cur = "USD"
            except Exception:
                pass
        # если всё ещё нет, пробуем извлечь из title
        if price_val is None and title:
            p, c = parse_price_and_currency(title)
            if p is not None:
                price_val = p
                price_cur = c or price_cur

        # определяем статус (если есть информация о наличии)
        status = "неизвестно"
        if "sold" in title.lower() or "sold" in link.lower():
            status = "продан"
        elif "available" in title.lower() or "in stock" in title.lower():
            status = "в наличии"
        elif "out of stock" in title.lower() or "unavailable" in title.lower():
            status = "нет в наличии"

        if link:
            results.append({
                "url": link,
                "title": title,
                "price": price_val,
                "currency": price_cur or "USD",
                "status": status,
            })

    # также проверяем другие секции serpapi на наличие цен
    # organic_results иногда содержат цены
    organic = data.get("organic_results") or []
    for o in organic:
        link = o.get("link") or ""
        if link:
            title = o.get("title") or ""
            price_text = o.get("price") or o.get("extracted_price") or ""
            price_val, price_cur = parse_price_and_currency(str(price_text))
            if price_val is None and title:
                p, c = parse_price_and_currency(title)
                if p is not None:
                    price_val = p
                    price_cur = c

            # определяем статус
            status = "неизвестно"
            if "sold" in title.lower() or "sold" in link.lower():
                status = "продан"
            elif "available" in title.lower() or "in stock" in title.lower():
                status = "в наличии"
            elif "out of stock" in title.lower() or "unavailable" in title.lower():
                status = "нет в наличии"

            # проверяем, нет ли уже такого url в results
            existing = next((r for r in results if r.get("url") == link), None)
            if existing:
                # обновляем цену если её не было
                if existing.get("price") is None and price_val is not None:
                    existing["price"] = price_val
                    existing["currency"] = price_cur or "USD"
                    existing["status"] = status
            elif link:
                results.append({
                    "url": link,
                    "title": title,
                    "price": price_val,
                    "currency": price_cur or "USD",
                    "status": status,
                })

    # также иногда полезны shopping_results (приоритетный источник цен!)
    shopping = data.get("shopping_results") or []
    for s in shopping:
        link = s.get("link") or ""
        title = s.get("title") or ""

        # пробуем все возможные поля с ценой из serpapi
        price_text = (
            s.get("extracted_price")
            or s.get("price")
            or s.get("price_with_symbol")
            or s.get("price_str")
            or ""
        )
        price_val, price_cur = parse_price_and_currency(str(price_text))
        # если не нашли через parse, пробуем напрямую
        if price_val is None:
            try:
                if isinstance(s.get("extracted_price"), (int, float)):
                    price_val = float(s.get("extracted_price"))
                    price_cur = "USD"  # по умолчанию
            except Exception:
                pass
        # если всё ещё нет, пробуем извлечь из title
        if price_val is None and title:
            p, c = parse_price_and_currency(title)
            if p is not None:
                price_val = p
                price_cur = c or price_cur

        # определяем статус
        status = "неизвестно"
        if "sold" in title.lower() or "sold" in link.lower():
            status = "продан"
        elif "available" in title.lower() or "in stock" in title.lower():
            status = "в наличии"
        elif "out of stock" in title.lower() or "unavailable" in title.lower():
            status = "нет в наличии"

        if link:
            results.append({
                "url": link,
                "title": title,
                "price": price_val,
                "currency": price_cur or "USD",
                "status": status,
            })

    return results

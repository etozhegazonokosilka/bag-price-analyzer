"""
парсер для theluxurycloset.com
использует xpath для поиска элементов"""

import json
import os
import re
from typing import Any
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from utils.xpath_helper import get_text_by_xpath, xpath_exists
from utils.price import parse_price_and_currency, extract_price_universal, extract_price_from_jsonld
from services.currency import convert_to_usd


from utils.logger import get_logger, log_event

log = get_logger(__name__)

_LOG_DEBUG = os.getenv("LOG_LEVEL", "minimal").strip().lower() in {"debug", "verbose", "1", "true", "yes"}
_MIN_VALID_PRODUCT_PRICE = 20.0


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
    # единый структурированный лог для theluxurycloset
    level_map = {
        "INFO": "info",
        "ERR": "error",
        "PROXY": "warning",
    }
    level = level_map.get(str(prefix).upper(), "info")
    log_event(log, message, level=level, msg=message, **fields)


def _log_debug(message: str, **fields) -> None:
    if _LOG_DEBUG:
        log_event(log, message, level="debug", **fields)


def _mask_proxy(proxy: str | None) -> str | None:
    if not proxy:
        return None
    if "@" not in proxy:
        return proxy
    left, right = proxy.split("@", 1)
    creds = left.split(":")
    if len(creds) >= 2:
        left = f"{creds[0]}:****"
    else:
        left = "****"
    return f"{left}@{right}"


def _is_valid_product_price(value: float | None) -> bool:
    # отсекаем служебные/ошибочные числа вместо цены товара
    return value is not None and value >= _MIN_VALID_PRODUCT_PRICE and value < 1_000_000


def _detect_blocked_tlc(soup: BeautifulSoup) -> str | None:
    # быстрые маркеры блокировки/капчи
    title = ""
    try:
        if soup.title:
            title = soup.title.get_text(" ", strip=True).lower()
    except Exception:
        title = ""
    text_sample = ""
    try:
        text_sample = soup.get_text(" ", strip=True).lower()
    except Exception:
        text_sample = ""

    markers = [
        "access denied",
        "forbidden",
        "pardon the interruption",
        "just a moment",
        "checking your browser",
        "cloudflare",
        "captcha",
        "robot check",
        "request blocked",
        "unusual traffic",
        "verify you are human",
    ]
    for marker in markers:
        if marker in title or marker in text_sample:
            return marker
    return None


def _canonical_like_url(soup: BeautifulSoup) -> str | None:
    try:
        link = soup.find("link", rel="canonical")
        if link and link.get("href"):
            return str(link.get("href")).strip()
    except Exception:
        pass
    try:
        og = soup.find("meta", {"property": "og:url"})
        if og and og.get("content"):
            return str(og.get("content")).strip()
    except Exception:
        pass
    return None


def _extract_tlc_product_id(soup: BeautifulSoup) -> str | None:
    url = _canonical_like_url(soup) or ""
    m = re.search(r"-p(?P<pid>\d+)\b", url)
    if m:
        return m.group("pid")
    return None


def _extract_pid_from_url(url: str | None) -> str | None:
    if not url:
        return None
    # ищем pid только в path, чтобы не ловить трекинг из query
    try:
        parsed_path = urlparse(str(url)).path or str(url)
    except Exception:
        parsed_path = str(url)
    m = re.search(r"-p(?P<pid>\d+)\b", parsed_path)
    if m:
        return m.group("pid")
    return None


def _extract_client_redirect_target(soup: BeautifulSoup) -> str | None:
    # ловим клиентский редирект, чтобы не подмешивать цену чужого товара
    pattern = re.compile(r"window\.location\.replace\(['\"]([^'\"]+)['\"]\)", flags=re.I)
    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if "window.location.replace" not in text:
            continue
        match = pattern.search(text)
        if match:
            return (match.group(1) or "").strip() or None
    return None


def _parse_price_value(raw) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if _is_valid_product_price(value) else None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text.replace(",", ""))
        return value if _is_valid_product_price(value) else None
    except Exception:
        parsed_price, _ = parse_price_and_currency(text)
        return parsed_price if _is_valid_product_price(parsed_price) else None


def _extract_price_from_dom_precise(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    # сначала пробуем самые точные xpath карточки товара
    xpath_candidates = [
        "//*[@id='root']/div[2]/div/div/div/div[2]/div/div[3]/div[2]/div/div/div[1]/div/div[3]/div",
        "//*[@id='root']/div[2]/div/div/div/div[2]/div/div[3]/div[2]/div/div/div[1]/div/div[3]/div[1]",
        "//*[@id='root']/div[2]/div/div/div/div[2]/div/div[3]/div[2]/div/div/div[1]/div/div[4]/div[1]",
    ]
    for xpath in xpath_candidates:
        try:
            price_text = get_text_by_xpath(soup, xpath)
            if not price_text:
                continue
            parsed_price, parsed_currency = parse_price_and_currency(price_text)
            if _is_valid_product_price(parsed_price):
                return parsed_price, (parsed_currency or "USD")
        except Exception:
            continue

    selectors = [
        "div.ProductPriceV2__newProductPrice___3VQFU",
        "#root > div:nth-child(2) > div > div > div > div.DesktopWidth__base___3ZRAa > div > div:nth-child(3) > div.NewSppComponent__productDetails___2sbAA > div > div > div:nth-child(1) > div > div.ProductPriceV2__newPriceContent___3mysL > div.ProductPriceV2__newProductPrice___3VQFU",
        "#root > div:nth-child(2) > div > div > div > div.DesktopWidth__base___3ZRAa > div > div:nth-child(3) > div.NewSppComponent__productDetails___2sbAA > div > div > div:nth-child(1) > div > div.ProductPriceV2__newPriceContent___3mysL > div",
        "div.ProductPriceV2__newPriceContent___3mysL > div",
        "[class*='ProductPriceV2__newProductPrice']",
        "[class*='ProductPriceV2__newPriceContent'] [class*='newProductPrice']",
        "[class*='ProductPriceV2'] [class*='newProductPrice']",
        "[class*='newProductPrice']",
        "[data-testid='product-price/final']",
        "[data-testid*='product-price/final']",
        "[data-testid*='product-price'] [data-testid*='final']",
    ]
    candidates: list[tuple[int, int, float, str]] = []

    for selector_index, selector in enumerate(selectors):
        for el in soup.select(selector):
            class_blob = " ".join(el.get("class") or []).lower()
            data_testid = (el.get("data-testid") or "").lower()
            text = (el.get_text(" ", strip=True) or "").strip()
            if not text:
                continue

            # отбрасываем блоки скидки "off on $..."
            text_lower = text.lower()
            if "off on" in text_lower or "percentage" in class_blob or "discountedofferprice" in class_blob:
                continue

            parsed_price, parsed_currency = parse_price_and_currency(text)
            if _is_valid_product_price(parsed_price):
                score = 0
                parents = list(el.parents)[:10]
                parent_class_blob = " ".join(
                    " ".join(parent.get("class") or []) for parent in parents if hasattr(parent, "get")
                ).lower()
                if "newproductprice" in class_blob:
                    score += 30
                if "productpricev2" in class_blob:
                    score += 20
                if "final" in data_testid:
                    score += 25
                if "newsppcomponent__productdetails" in parent_class_blob:
                    score += 40
                if "productpricev2__newpricecontent" in parent_class_blob:
                    score += 20
                candidates.append((score, selector_index, parsed_price, parsed_currency or "USD"))

    if not candidates:
        return None, None

    # при одинаковом весе оставляем более точный селектор, а не случайно меньшую цену
    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, _, best_price, best_currency = candidates[0]
    return best_price, best_currency


def _extract_price_from_html_price_block_regex(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    html = str(soup)
    if not html:
        return None, None

    patterns = [
        re.compile(r'ProductPriceV2__newProductPrice[^>]*>(?P<body>.*?)</div>', flags=re.I | re.S),
        re.compile(r'newProductPrice[^>]*>(?P<body>.*?)</div>', flags=re.I | re.S),
        re.compile(r'data-testid=["\']product-price/final["\'][^>]*>(?P<body>.*?)</div>', flags=re.I | re.S),
    ]

    for pattern in patterns:
        for match in pattern.finditer(html):
            body = match.group("body") or ""
            text = re.sub(r"<[^>]+>", " ", body)
            text = " ".join(text.split()).strip()
            if not text:
                continue
            parsed_price, parsed_currency = parse_price_and_currency(text)
            if _is_valid_product_price(parsed_price):
                return parsed_price, (parsed_currency or "USD")

    return None, None


def _iter_json_objects(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_json_objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_json_objects(item)


def _extract_price_from_jsonld_precise(
    soup: BeautifulSoup, product_id: str | None = None
) -> tuple[float | None, str | None]:
    # ищем цену из offers.price в json-ld как самый стабильный источник
    scripts = soup.find_all("script", {"type": "application/ld+json"})
    if not scripts:
        return None, None

    product_id = product_id or _extract_tlc_product_id(soup)
    candidates: list[tuple[int, float, str]] = []

    def matches_product(obj: dict) -> bool:
        if not product_id:
            return True
        pid = str(product_id)
        probes: list[str] = []
        for key in ("sku", "productID", "productId", "id", "url", "canonicalUrl", "canonicalURL"):
            value = obj.get(key)
            if value is not None:
                probes.append(str(value))

        offers = obj.get("offers")
        if isinstance(offers, dict):
            if offers.get("url"):
                probes.append(str(offers.get("url")))
        elif isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict) and offer.get("url"):
                    probes.append(str(offer.get("url")))

        for text in probes:
            low = text.lower()
            if re.search(rf"\b{re.escape(pid)}\b", text):
                return True
            if f"-p{pid}" in low or f"/p{pid}" in low:
                return True
        return False

    def add_offer_candidate(obj: dict, base_score: int) -> None:
        offers = obj.get("offers")
        offer_list: list[dict] = []
        if isinstance(offers, dict):
            offer_list = [offers]
        elif isinstance(offers, list):
            offer_list = [item for item in offers if isinstance(item, dict)]
        else:
            offer_list = [obj]

        for offer in offer_list:
            raw_price = offer.get("price")
            if raw_price is None:
                raw_price = offer.get("lowPrice") or offer.get("highPrice")
            parsed_price = _parse_price_value(raw_price)
            if parsed_price is None:
                continue

            raw_currency = (
                offer.get("priceCurrency")
                or offer.get("currency")
                or obj.get("priceCurrency")
                or obj.get("currency")
                or "USD"
            )
            currency = str(raw_currency).strip().upper() or "USD"
            candidates.append((base_score, parsed_price, currency))

    for script in scripts:
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue

        parsed_data = None
        for candidate in (raw, raw.rstrip(";").strip()):
            if not candidate:
                continue
            try:
                parsed_data = json.loads(candidate)
                break
            except Exception:
                continue

        if parsed_data is not None:
            for obj in _iter_json_objects(parsed_data):
                if not isinstance(obj, dict):
                    continue

                obj_type = str(obj.get("@type") or "").lower()
                score = 0
                if "product" in obj_type:
                    score += 20
                if matches_product(obj):
                    score += 25
                if score <= 0 and product_id:
                    continue
                if score <= 0:
                    score = 5

                add_offer_candidate(obj, score)
            continue

        # fallback на случай невалидного json-ld, но с явными полями offers.price
        if product_id:
            low_raw = raw.lower()
            if product_id not in raw and f"-p{product_id}" not in low_raw and f"/p{product_id}" not in low_raw:
                continue
        price_match = re.search(r'"price"\s*:\s*"?(?P<price>\d[\d,]*(?:\.\d+)?)"?', raw, flags=re.I)
        if not price_match:
            continue
        parsed_price = _parse_price_value(price_match.group("price"))
        if parsed_price is None:
            continue
        currency_match = re.search(r'"priceCurrency"\s*:\s*"(?P<cur>[A-Z]{3})"', raw, flags=re.I)
        currency = (currency_match.group("cur") if currency_match else "USD").upper()
        candidates.append((15, parsed_price, currency))

    if not candidates:
        return None, None

    # при одинаковом весе оставляем первый найденный вариант из json-ld
    candidates.sort(key=lambda item: -item[0])
    _, price, currency = candidates[0]
    return price, currency


def _extract_status_from_jsonld(soup: BeautifulSoup, product_id: str | None = None) -> str | None:
    # читаем availability из json-ld и привязываем к pid товара
    scripts = soup.find_all("script", {"type": "application/ld+json"})
    product_id = product_id or _extract_tlc_product_id(soup)

    def matches_product(obj: dict) -> bool:
        if not product_id:
            return True
        pid = str(product_id)
        probes: list[str] = []
        for key in ("sku", "productID", "productId", "id", "url", "canonicalUrl", "canonicalURL"):
            value = obj.get(key)
            if value is not None:
                probes.append(str(value))

        offers = obj.get("offers")
        if isinstance(offers, dict):
            if offers.get("url"):
                probes.append(str(offers.get("url")))
        elif isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict) and offer.get("url"):
                    probes.append(str(offer.get("url")))

        for text in probes:
            low = text.lower()
            if re.search(rf"\b{re.escape(pid)}\b", text):
                return True
            if f"-p{pid}" in low or f"/p{pid}" in low:
                return True
        return False

    best_status = None
    best_score = -1

    for script in scripts:
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        stack = [data]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                availability = str(current.get("availability") or "").lower()
                if availability:
                    current_type = str(current.get("@type") or "").lower()
                    score = 0
                    if "product" in current_type:
                        score += 10
                    if matches_product(current):
                        score += 30
                    elif product_id:
                        score = -1

                    if score < 0:
                        stack.extend(current.values())
                        continue

                    if "instock" in availability or "in stock" in availability:
                        if score > best_score:
                            best_status = "Available"
                            best_score = score
                    if (
                        "outofstock" in availability
                        or "out of stock" in availability
                        or "soldout" in availability
                        or "sold out" in availability
                    ):
                        if score > best_score:
                            best_status = "Sold Out"
                            best_score = score
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)

    return best_status


def _extract_preloaded_state_price_regex(
    script_text: str, product_id: str | None
) -> tuple[float | None, str | None]:
    if not script_text:
        return None, None

    low_text = script_text.lower()
    pid = str(product_id) if product_id else None
    segments: list[str] = []
    span_before = 2800
    span_after = 4200

    if pid:
        # ищем куски текста рядом с текущим pid, чтобы не брать цены соседних карточек
        tokens = [
            f"-p{pid}",
            f"/p{pid}",
            f'"id":{pid}',
            f'"id":"{pid}"',
            f'"productid":{pid}',
            f'"productid":"{pid}"',
            f'"product_id":{pid}',
            f'"product_id":"{pid}"',
            f'"alias_with_id":"',
            f'"web_link":"',
        ]
        for token in tokens:
            start = 0
            low_token = token.lower()
            while True:
                idx = low_text.find(low_token, start)
                if idx == -1:
                    break
                left = max(0, idx - span_before)
                right = min(len(script_text), idx + span_after)
                segments.append(script_text[left:right])
                start = idx + len(low_token)

    details_idx = low_text.find('"productdetails"')
    if details_idx != -1:
        segments.append(script_text[max(0, details_idx - 1200) : min(len(script_text), details_idx + 15000)])

    if not segments:
        segments.append(script_text[: min(len(script_text), 180000)])

    price_keys = (
        "price_tlc",
        "display_price",
        "finalPrice",
        "salePrice",
        "currentPrice",
        "newProductPrice",
        "price",
        "amount",
        "value",
    )
    currency_keys = ("priceCurrency", "currency", "currencyCode", "currency_code")
    candidates: list[tuple[int, float, str]] = []

    for segment in segments:
        segment_low = segment.lower()
        for key in price_keys:
            pattern = re.compile(
                rf'"{re.escape(key)}"\s*:\s*"?(?P<price>\d[\d,]*(?:\.\d+)?)"?',
                flags=re.I,
            )
            for match in pattern.finditer(segment):
                parsed = _parse_price_value(match.group("price"))
                if parsed is None:
                    continue

                window = segment[max(0, match.start() - 220) : min(len(segment), match.end() + 220)]
                currency = "USD"
                for c_key in currency_keys:
                    cur_match = re.search(
                        rf'"{re.escape(c_key)}"\s*:\s*"(?P<cur>[A-Za-z]{{3}})"',
                        window,
                        flags=re.I,
                    )
                    if cur_match:
                        currency = cur_match.group("cur").upper()
                        break

                score = 0
                key_low = key.lower()
                if key_low in {"price_tlc", "display_price"}:
                    score += 45
                elif key_low in {"finalprice", "saleprice", "currentprice", "newproductprice"}:
                    score += 35
                else:
                    score += 15
                if '"productdetails"' in segment_low:
                    score += 20
                if pid and (f"-p{pid}" in segment_low or f"/p{pid}" in segment_low):
                    score += 10

                candidates.append((score, parsed, currency))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (-item[0], -item[1]))
    _, price, currency = candidates[0]
    return price, currency


def _extract_price_from_next_data_targeted(
    soup: BeautifulSoup, product_id: str | None = None
) -> tuple[float | None, str | None]:
    product_id = product_id or _extract_tlc_product_id(soup)
    if not product_id:
        return None, None

    script = soup.find("script", {"id": "__NEXT_DATA__"})
    raw = script.string if script else None
    if not raw:
        return None, None

    try:
        data = json.loads(raw.strip())
    except Exception:
        return None, None

    price_keys = [
        "finalPrice",
        "salePrice",
        "sellingPrice",
        "currentPrice",
        "newProductPrice",
        "newPrice",
        "displayPrice",
        "productPrice",
        "discountedPrice",
        "regularPrice",
        "priceValue",
        "price",
        "amount",
        "value",
    ]
    currency_keys = ["priceCurrency", "currency", "currencyCode", "currency_code"]
    identity_keys = {
        "id",
        "productId",
        "itemId",
        "product_id",
        "sku",
        "itemCode",
        "productCode",
        "slug",
        "url",
        "canonicalUrl",
        "canonical_url",
        "href",
        "path",
        "productUrl",
        "productURL",
        "pdpUrl",
        "seoUrl",
        "permalink",
        "handle",
        "link",
    }
    pid_with_prefix = f"p{product_id}"
    candidates: list[tuple[int, float, str]] = []

    def object_matches_product(obj: dict) -> bool:
        for key, value in obj.items():
            if key not in identity_keys:
                continue
            if value is None:
                continue
            if isinstance(value, (int, float)) and str(int(value)) == product_id:
                return True
            text = str(value).lower()
            if key in {"id", "productId", "itemId", "product_id"}:
                if re.search(rf"\b{re.escape(product_id)}\b", text):
                    return True
                continue
            if f"-p{product_id}" in text or f"/p{product_id}" in text:
                return True
            if pid_with_prefix in text:
                return True
        return False

    def read_price_from_obj(obj: dict) -> tuple[float | None, str | None]:
        for key in price_keys:
            if key not in obj:
                continue
            parsed = _parse_price_value(obj.get(key))
            if parsed is None:
                continue
            currency = None
            for c_key in currency_keys:
                if obj.get(c_key):
                    currency = str(obj.get(c_key)).upper().strip()
                    break
            return parsed, (currency or "USD")
        return None, None

    def walk(node, matched: bool = False):
        if isinstance(node, dict):
            this_match = matched or object_matches_product(node)
            if this_match:
                price_val, cur = read_price_from_obj(node)
                if price_val is not None and price_val > 0:
                    # выше score для более приоритетных ключей/контекста
                    score = 10
                    if any(k in node for k in ("finalPrice", "salePrice", "sellingPrice", "currentPrice")):
                        score = 20
                    candidates.append((score, price_val, cur or "USD"))
            for value in node.values():
                walk(value, this_match)
        elif isinstance(node, list):
            for item in node:
                walk(item, matched)

    walk(data, False)
    if not candidates:
        # fallback: вытаскиваем цену regex'ом рядом с текущим product id
        raw_low = raw.lower()
        if product_id in raw_low or f"-p{product_id}" in raw_low or f"/p{product_id}" in raw_low:
            near_patterns = [
                re.compile(
                    rf"-p{re.escape(product_id)}.{{0,1200}}?\"price\"\s*:\s*\"?(?P<price>\d[\d,]*(?:\.\d+)?)\"?",
                    flags=re.I | re.S,
                ),
                re.compile(
                    rf"\"price\"\s*:\s*\"?(?P<price>\d[\d,]*(?:\.\d+)?)\"?.{{0,1200}}?-p{re.escape(product_id)}",
                    flags=re.I | re.S,
                ),
            ]
            for pattern in near_patterns:
                m = pattern.search(raw)
                if not m:
                    continue
                parsed = _parse_price_value(m.group("price"))
                if parsed is not None:
                    cur_m = re.search(r'"priceCurrency"\s*:\s*"(?P<cur>[A-Z]{3})"', raw, flags=re.I)
                    currency = (cur_m.group("cur") if cur_m else "USD").upper()
                    return parsed, currency
        return None, None

    candidates.sort(key=lambda item: (-item[0], -item[1]))
    _, price, currency = candidates[0]
    return price, currency


def _extract_json_object_from_assignment(script_text: str, var_name: str) -> Any | None:
    if not script_text:
        return None
    assign_re = re.compile(rf"(?:window\.)?{re.escape(var_name)}\s*=\s*", flags=re.I)
    m = assign_re.search(script_text)
    if not m:
        return None

    start = script_text.find("{", m.end())
    if start == -1:
        return None

    depth = 0
    in_string = False
    string_quote = ""
    escaped = False
    end = -1

    for idx in range(start, len(script_text)):
        ch = script_text[idx]
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == string_quote:
                in_string = False
            continue

        if ch in {"'", '"'}:
            in_string = True
            string_quote = ch
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break

    if end == -1:
        return None

    raw_obj = script_text[start : end + 1]
    try:
        return json.loads(raw_obj)
    except Exception:
        return None


def _extract_preloaded_state_targeted(soup: BeautifulSoup, product_id: str | None) -> tuple[float | None, str | None]:
    product_id = product_id or _extract_tlc_product_id(soup)
    data = None
    preloaded_script_text = ""

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if "__PRELOADED_STATE__" not in text:
            continue
        if not preloaded_script_text:
            preloaded_script_text = text
        data = _extract_json_object_from_assignment(text, "__PRELOADED_STATE__")
        if data is not None:
            break

    if not isinstance(data, dict):
        return _extract_preloaded_state_price_regex(preloaded_script_text, product_id)

    price_keys = [
        "price_tlc",
        "display_price",
        "finalPrice",
        "salePrice",
        "currentPrice",
        "newProductPrice",
        "price",
        "amount",
        "value",
    ]
    currency_keys = ["priceCurrency", "currency", "currencyCode", "currency_code"]
    identity_keys = [
        "id",
        "productId",
        "product_id",
        "sku",
        "alias_with_id",
        "web_link",
        "url",
        "canonicalUrl",
        "pdpUrl",
        "seoUrl",
        "href",
        "path",
        "link",
    ]

    def matches_product(obj: dict) -> bool:
        if not product_id:
            return True
        pid = str(product_id)
        for key in identity_keys:
            value = obj.get(key)
            if value is None:
                continue
            if isinstance(value, (int, float)):
                try:
                    if str(int(value)) == pid:
                        return True
                except Exception:
                    pass
            text = str(value).lower()
            if re.search(rf"\b{re.escape(pid)}\b", text):
                return True
            if f"-p{pid}" in text or f"/p{pid}" in text:
                return True
        return False

    def read_price(obj: dict) -> tuple[float | None, str | None]:
        for key in price_keys:
            if key not in obj:
                continue
            raw_val = obj.get(key)
            if isinstance(raw_val, dict):
                raw_val = raw_val.get("value") or raw_val.get("amount") or raw_val.get("price")
            parsed = _parse_price_value(raw_val)
            if parsed is None:
                continue
            raw_currency = None
            for c_key in currency_keys:
                if obj.get(c_key):
                    raw_currency = str(obj.get(c_key)).strip().upper()
                    break
            return parsed, (raw_currency or "USD")
        return None, None

    candidates: list[tuple[int, float, str]] = []

    spp = data.get("sppReducer")
    if isinstance(spp, dict):
        details = spp.get("productDetails")
        if isinstance(details, dict):
            if matches_product(details):
                val, cur = read_price(details)
                if val is not None and val > 0:
                    candidates.append((100, val, cur or "USD"))
            # иногда price хранится в дочернем pricing-объекте
            pricing_obj = details.get("pricing") if isinstance(details, dict) else None
            if isinstance(pricing_obj, dict):
                val, cur = read_price(pricing_obj)
                if val is not None and val > 0:
                    candidates.append((90, val, cur or "USD"))

    stack: list[Any] = [data]
    seen = 0
    while stack and seen < 20000:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            if matches_product(node):
                val, cur = read_price(node)
                if val is not None and val > 0:
                    score = 70
                    if any(k in node for k in (
                        "price_tlc",
                        "display_price",
                        "finalPrice",
                        "salePrice",
                        "currentPrice",
                    )):
                        score += 10
                    candidates.append((score, val, cur or "USD"))
                for key, value in node.items():
                    if isinstance(
                        value,
                        dict,
                    ) and key.lower() in {"pricing", "priceinfo", "price_info", "offer", "offers"}:
                        sub_val, sub_cur = read_price(value)
                        if sub_val is not None and sub_val > 0:
                            candidates.append((75, sub_val, sub_cur or "USD"))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append(item)

    if not candidates:
        # fallback на случай частично битого json в __PRELOADED_STATE__
        return _extract_preloaded_state_price_regex(preloaded_script_text, product_id)

    candidates.sort(key=lambda item: (-item[0], -item[1]))
    _, price, currency = candidates[0]
    return price, currency


def scrape_tlc(
    soup: BeautifulSoup,
    diagnosis: dict | None = None,
    source_url: str | None = None,
) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара theluxurycloset.com

    аргументы:
        soup: объект beautifulsoup со страницей товара

    возвращает:
        tuple (название, цена, валюта, статус)
"""
    title = None
    price = None
    currency = None
    status = None

    diag_status = diagnosis.get("status") if isinstance(diagnosis, dict) else None
    diag_size = diagnosis.get("content_size") if isinstance(diagnosis, dict) else None
    proxy_used = diagnosis.get("proxy_used") if isinstance(diagnosis, dict) else None

    diag_blocked_hint = diag_status in {"blocked", "proxy_auth_required"}
    if diag_blocked_hint:
        blocked_marker = _detect_blocked_tlc(soup)
        has_product_signals = False
        try:
            html_low = str(soup).lower()
            has_product_signals = any(
                token in html_low
                for token in (
                    "productdetailscard__productname",
                    "productpricev2__newproductprice",
                    "itemcondition__selectedindex",
                    "sppbuttonwrapper__soldouttitle",
                    "newsppcomponent__productdetails",
                    '"@type":"product"',
                    '"@type": "product"',
                )
            )
            if not has_product_signals and _extract_tlc_product_id(soup):
                has_product_signals = True
        except Exception:
            has_product_signals = False

        if not has_product_signals:
            _log(
                "PROXY",
                "tlc_blocked",
                marker=blocked_marker or diag_status,
                status=diag_status,
                size=diag_size,
                proxy=_mask_proxy(proxy_used),
            )
            return None, None, None, "blocked"

        _log(
            "PROXY",
            "tlc_blocked_soft_override",
            marker=blocked_marker or diag_status,
            status=diag_status,
            size=diag_size,
            proxy=_mask_proxy(proxy_used),
        )

    price_source = None

    # извлекаем название через xpath
    try:
        title_text = get_text_by_xpath(soup, "//h1[contains(@class, 'ProductDetailsCard__productName')]")
        if title_text:
            title = title_text
        else:
            # fallback: пробуем другие селекторы для названия
            title_fallbacks = [
                "//h1[@class='product-title']",
                "//h1[@data-testid='product-name']",
                "//div[contains(@class, 'product-name')]",
                "//span[contains(@class, 'product-title')]",
                "//h1",
                "//h2[contains(@class, 'product')]",
                "//div[@data-cy='product-title']",
                "//span[@data-cy='product-name']",
                "//meta[@property='og:title']/@content",  # заголовок Open Graph
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

    source_pid = _extract_pid_from_url(source_url)
    canonical_pid = _extract_tlc_product_id(soup)
    redirect_target = _extract_client_redirect_target(soup)
    redirect_pid = _extract_pid_from_url(redirect_target)
    product_id_hint = canonical_pid or source_pid

    # если страница редиректит на другой pid, не используем цену этой страницы для исходного URL
    if (
        source_pid
        and ((canonical_pid and canonical_pid != source_pid) or (redirect_pid and redirect_pid != source_pid))
    ):
        _log(
            "INFO",
            "tlc_redirect_mismatch",
            source_pid=source_pid,
            canonical_pid=canonical_pid,
            redirect_pid=redirect_pid,
            redirect_target=redirect_target,
        )
        return title, None, None, "redirect_mismatch"

    # извлекаем цену - используем многоуровневый поиск
    # сначала ищем в мета-данных (JSON-LD, meta tags), затем в HTML
    price = None
    currency = None
    jsonld_precise_price = None
    jsonld_precise_currency = None

    # json-ld складываем в отложенный fallback, чтобы не перебивать более точную цену из dom/state
    try:
        precise_price, precise_currency = _extract_price_from_jsonld_precise(soup, product_id_hint)
        if precise_price is not None and precise_price > 0:
            jsonld_precise_price = precise_price
            jsonld_precise_currency = precise_currency or "USD"
            _log(
                "INFO",
                "tlc_price_candidate",
                source="jsonld_precise",
                price=jsonld_precise_price,
                currency=jsonld_precise_currency,
            )
    except Exception as e:
        _log("ERR", "tlc_error", stage="jsonld_precise", error=str(e))

    # затем пробуем узкие селекторы текущей цены в DOM
    try:
        dom_price, dom_currency = _extract_price_from_dom_precise(soup)
        if dom_price is not None and dom_price > 0:
            price = dom_price
            currency = dom_currency or "USD"
            price_source = "dom_precise"
            _log("INFO", "tlc_price_found", source=price_source, price=price, currency=currency)
    except Exception as e:
        _log("ERR", "tlc_error", stage="dom_precise", error=str(e))

    # fallback на regex по html-блоку цены (устойчив к вложенным span и частично сломанной вёрстке)
    if price is None:
        try:
            regex_price, regex_currency = _extract_price_from_html_price_block_regex(soup)
            if regex_price is not None and regex_price > 0:
                price = regex_price
                currency = regex_currency or "USD"
                price_source = "html_regex"
                _log("INFO", "tlc_price_found", source=price_source, price=price, currency=currency)
        except Exception as e:
            _log("ERR", "tlc_error", stage="html_regex", error=str(e))

    # уровень 1: поиск в meta tags (самый надежный, загружается сразу)
    if price is None:
        try:
            meta_price = (
                soup.find("meta", {"property": "product:price:amount"})
                or soup.find("meta", {"property": "og:price:amount"})
            )
            if meta_price and meta_price.get("content"):
                try:
                    price_val = float(meta_price.get("content").replace(",", ""))
                    if price_val > 0:
                        price = price_val
                        currency = "USD"  # обычно в meta валюта отдельно
                        meta_currency = (
                            soup.find("meta", {"property": "product:price:currency"})
                            or soup.find("meta", {"property": "og:price:currency"})
                        )
                        if meta_currency and meta_currency.get("content"):
                            currency = meta_currency.get("content").upper()
                        price_source = "meta"
                        _log("INFO", "tlc_price_found", source=price_source, price=price, currency=currency)
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            _log("ERR", "tlc_error", stage="meta_tags", error=str(e))

    # уровень 2: поиск в JSON-LD (Schema.org) - загружается сразу, до React рендеринга
    if price is None:
        try:
            jsonld_scripts = soup.find_all('script', {'type': 'application/ld+json'})
            for script in jsonld_scripts:
                try:
                    raw = (script.string or script.get_text() or "").strip()
                    if raw:
                        data = json.loads(raw)
                        parsed_price, parsed_currency = extract_price_from_jsonld(data)
                        if parsed_price is not None and parsed_price > 0:
                            price = parsed_price
                            currency = parsed_currency or "USD"
                            price_source = "jsonld"
                            _log("INFO", "tlc_price_found", source=price_source, price=price, currency=currency)
                            break
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception as e:
            _log("ERR", "tlc_error", stage="jsonld", error=str(e))

    # узкий поиск цены в __NEXT_DATA__ строго по product id
    if price is None:
        try:
            targeted_price, targeted_currency = _extract_price_from_next_data_targeted(
                soup, product_id_hint
            )
            if targeted_price is not None and targeted_price > 0:
                price = targeted_price
                currency = targeted_currency or "USD"
                price_source = "next_data_product"
                _log(
                    "INFO",
                    "tlc_price_found",
                    source=price_source,
                    price=price,
                    currency=currency,
                    product_id=product_id_hint,
                )
        except Exception as e:
            _log("ERR", "tlc_error", stage="next_data_targeted", error=str(e))

    # fallback для SPA-снимков: price в window.__PRELOADED_STATE__
    if price is None:
        try:
            preloaded_price, preloaded_currency = _extract_preloaded_state_targeted(soup, product_id_hint)
            if preloaded_price is not None and preloaded_price > 0:
                price = preloaded_price
                currency = preloaded_currency or "USD"
                price_source = "preloaded_state"
                _log(
                    "INFO",
                    "tlc_price_found",
                    source=price_source,
                    price=price,
                    currency=currency,
                    product_id=product_id_hint,
                )
        except Exception as e:
            _log("ERR", "tlc_error", stage="preloaded_state_targeted", error=str(e))

    # уровень 2.5: поиск в Next.js / React Hydration данных (самый надежный для Next.js сайтов)
    # данные находятся в скрипте до рендеринга страницы
    if price is None:
        try:
            # улучшенная функция рекурсивного поиска цены с учетом валюты
            def find_price_with_currency(obj, depth=0):
                """
                рекурсивно ищет цену в JSON с учетом валюты
                возвращает список найденных цен: [(price, currency, priority), ...]
                priority: 1 = USD цена, 2 = другая валюта, 3 = цена без указания валюты
"""
                if depth > 10:  # ограничиваем глубину рекурсии
                    return []

                found_prices = []

                if isinstance(obj, dict):
                    # игнорируем поля с ценами рассрочки
                    ignore_keys = ["installment", "monthly", "installment_price", "monthly_payment", "emi"]
                    is_installment = any(key in str(obj.keys()).lower() for key in ignore_keys)
                    if is_installment:
                        return []

                    # ищем объекты, содержащие и цену, и валюту
                    has_price = False
                    price_val = None
                    currency_val = None

                    # ключи для цены
                    price_keys = [
                        "price",
                        "salePrice",
                        "sale_price",
                        "finalPrice",
                        "sellingPrice",
                        "currentPrice",
                        "newProductPrice",
                        "newPrice",
                        "displayPrice",
                        "productPrice",
                        "discountedPrice",
                        "regularPrice",
                        "priceValue",
                        "amount",
                        "value",
                    ]
                    # ключи для валюты
                    currency_keys = ["currency", "priceCurrency", "currencyCode", "currency_code"]

                    # ищем цену
                    for pkey in price_keys:
                        if pkey in obj:
                            val = obj[pkey]
                            if isinstance(val, (int, float)) and val > 0:
                                price_val = float(val)
                                has_price = True
                                break
                            elif isinstance(val, str):
                                p, c = parse_price_and_currency(val)
                                if p is not None and p > 0:
                                    price_val = p
                                    currency_val = c
                                    has_price = True
                                    break
                            elif isinstance(val, dict):
                                # цена может быть объектом: {"value": 755, "currency": "USD"}
                                if "value" in val or "amount" in val:
                                    price_from_obj = val.get("value") or val.get("amount")
                                    if isinstance(price_from_obj, (int, float)) and price_from_obj > 0:
                                        price_val = float(price_from_obj)
                                        has_price = True
                                        # проверяем валюту в этом объекте
                                        for ckey in currency_keys:
                                            if ckey in val and val[ckey]:
                                                currency_val = str(val[ckey]).upper()
                                                break
                                        break

                    # ищем валюту (если еще не найдена)
                    if has_price and currency_val is None:
                        for ckey in currency_keys:
                            if ckey in obj and obj[ckey]:
                                currency_val = str(obj[ckey]).upper()
                                break

                    # если нашли цену в этом объекте
                    if has_price and price_val is not None:
                        if currency_val == "USD":
                            priority = 1  # высший приоритет - USD
                            found_prices.append((price_val, currency_val, priority))
                            _log_debug(
                                "tlc_debug_price_candidate",
                                price=price_val,
                                currency=currency_val,
                                priority=priority,
                            )
                        elif currency_val and currency_val != "USD":
                            priority = 2  # средний приоритет - известная не-USD валюта
                            found_prices.append((price_val, currency_val, priority))
                            _log_debug(
                                "tlc_debug_price_candidate",
                                price=price_val,
                                currency=currency_val,
                                priority=priority,
                            )
                        else:
                            # валюта не указана - игнорируем такую цену (может быть ошибкой)
                            _log_debug("tlc_debug_price_ignored", price=price_val, reason="no_currency")

                    # рекурсивный поиск в значениях
                    for key, value in obj.items():
                        # пропускаем ключи рассрочки
                        if any(ign in key.lower() for ign in ignore_keys):
                            continue
                        sub_prices = find_price_with_currency(value, depth + 1)
                        found_prices.extend(sub_prices)

                elif isinstance(obj, list):
                    for item in obj:
                        sub_prices = find_price_with_currency(item, depth + 1)
                        found_prices.extend(sub_prices)

                return found_prices

            # метод 1: ищем скрипт с id="__NEXT_DATA__"
            next_data_script = soup.find("script", {"id": "__NEXT_DATA__"})
            if next_data_script and next_data_script.string:
                try:
                    next_data = json.loads(next_data_script.string.strip())
                    _log_debug("tlc_debug_next_data_parse")

                    # находим все цены в JSON
                    all_prices = find_price_with_currency(next_data)

                    if all_prices:
                        # сортируем по приоритету (1 = USD, 2 = другая валюта, 3 = без валюты)
                        all_prices.sort(key=lambda x: x[2])

                        # берем цену с наивысшим приоритетом
                        best_price, best_currency, best_priority = all_prices[0]

                        if best_currency == "USD":
                            price = best_price
                            currency = "USD"
                            price_source = "next_data"
                            _log("INFO", "tlc_price_found", source=price_source, price=price, currency=currency)
                        else:
                            # конвертируем в USD
                            _log(
                                "INFO",
                                "tlc_currency_convert",
                                source="next_data",
                                from_price=best_price,
                                from_currency=best_currency,
                                to="USD",
                            )
                            price_usd = convert_to_usd(best_price, best_currency)
                            if price_usd is not None:
                                price = price_usd
                                currency = "USD"
                                price_source = "next_data"
                                _log(
                                    "INFO",
                                    "tlc_price_found",
                                    source=price_source,
                                    price=price,
                                    currency=currency,
                                    from_price=best_price,
                                    from_currency=best_currency,
                                )
                            else:
                                # оставляем исходную цену, если конвертация недоступна
                                price = best_price
                                currency = best_currency
                                price_source = "next_data_raw_currency"
                                _log(
                                    "ERR",
                                    "tlc_currency_convert_failed",
                                    source="next_data",
                                    from_price=best_price,
                                    from_currency=best_currency,
                                )

                except (json.JSONDecodeError, TypeError) as e:
                    _log("ERR", "tlc_error", stage="next_data_parse", error=str(e))

            # метод 2: ищем любые скрипты, содержащие "ProductPrice" или цену
            if price is None:
                all_scripts = soup.find_all("script")
                for script in all_scripts:
                    if script.string and ("ProductPrice" in script.string or "product" in script.string.lower()):
                        try:
                            # пробуем распарсить как JSON
                            script_text = script.string.strip()
                            # удаляем возможные JavaScript обертки (window.__DATA__ = {...})
                            if "=" in script_text:
                                script_text = script_text.split("=", 1)[1].strip()
                                if script_text.endswith(";"):
                                    script_text = script_text[:-1]

                            script_data = json.loads(script_text)
                            _log_debug("tlc_debug_script_parse")

                            # находим все цены в JSON
                            all_prices = find_price_with_currency(script_data)

                            if all_prices:
                                # сортируем по приоритету
                                all_prices.sort(key=lambda x: x[2])

                                # берем цену с наивысшим приоритетом
                                best_price, best_currency, best_priority = all_prices[0]

                                if best_currency == "USD":
                                    price = best_price
                                    currency = "USD"
                                    price_source = "script_json"
                                    _log(
                                        "INFO",
                                        "tlc_price_found",
                                        source=price_source,
                                        price=price,
                                        currency=currency,
                                    )
                                    break
                                else:
                                    # конвертируем в USD
                                    _log(
                                        "INFO",
                                        "tlc_currency_convert",
                                        source="script_json",
                                        from_price=best_price,
                                        from_currency=best_currency,
                                        to="USD",
                                    )
                                    price_usd = convert_to_usd(best_price, best_currency)
                                    if price_usd is not None:
                                        price = price_usd
                                        currency = "USD"
                                        price_source = "script_json"
                                        _log(
                                            "INFO",
                                            "tlc_price_found",
                                            source=price_source,
                                            price=price,
                                            currency=currency,
                                            from_price=best_price,
                                            from_currency=best_currency,
                                        )
                                        break
                                    else:
                                        # оставляем исходную цену, если конвертация недоступна
                                        price = best_price
                                        currency = best_currency
                                        price_source = "script_json_raw_currency"
                                        _log(
                                            "ERR",
                                            "tlc_currency_convert_failed",
                                            source="script_json",
                                            from_price=best_price,
                                            from_currency=best_currency,
                                        )
                                        break
                        except (json.JSONDecodeError, ValueError, TypeError):
                            continue
        except Exception as e:
            _log("ERR", "tlc_error", stage="nextjs_data", error=str(e))

    # уровень 3: увеличенная задержка для загрузки React контента (если мета-данные не помогли)
    if price is None:

        # отладочный вывод первых 500 символов HTML
        try:
            html_preview = str(soup)[:500]
            _log_debug("tlc_html_preview", preview=html_preview)
        except Exception as e:
            _log("ERR", "tlc_error", stage="html_preview", error=str(e))

    # уровень 4: поиск в HTML через XPath (после задержки)
    if price is None:
        # основной вариант: XPath с частичным совпадением класса
        try:
            price_text = get_text_by_xpath(soup, "//div[contains(@class, 'ProductPriceV2__newProductPrice')]")
            if price_text:
                # очищаем текст цены от $ и ,
                price_clean = price_text.replace("$", "").replace(",", "").strip()
                try:
                    parsed_price = float(price_clean)
                    if parsed_price > 0:
                        price = parsed_price
                        currency = "USD"
                        price_source = "xpath"
                        _log(
                            "INFO",
                            "tlc_price_found",
                            source=price_source,
                            price=price,
                            currency=currency,
                            xpath="ProductPriceV2__newProductPrice",
                        )
                except ValueError:
                    # fallback на универсальный парсер
                    parsed_price, parsed_currency = parse_price_and_currency(price_text)
                    if parsed_price is not None and parsed_price > 0:
                        price = parsed_price
                        currency = parsed_currency or "USD"
                        price_source = "xpath"
                        _log(
                            "INFO",
                            "tlc_price_found",
                            source=price_source,
                            price=price,
                            currency=currency,
                            xpath="ProductPriceV2__newProductPrice_fallback",
                        )
        except Exception as e:
            _log("ERR", "tlc_error", stage="xpath", error=str(e))

        # fallback 1: пробуем через beautifulsoup с частичным совпадением класса
        if price is None:
            try:
                price_elem = soup.find("div", class_=lambda x: x and "ProductPriceV2__newProductPrice" in x)
                if price_elem:
                    price_text = price_elem.get_text(strip=True)
                    if price_text:
                        price_clean = price_text.replace("$", "").replace(",", "").strip()
                        try:
                            parsed_price = float(price_clean)
                            if parsed_price > 0:
                                price = parsed_price
                                currency = "USD"
                                price_source = "bs"
                                _log(
                                    "INFO",
                                    "tlc_price_found",
                                    source=price_source,
                                    price=price,
                                    currency=currency,
                                    selector="ProductPriceV2__newProductPrice",
                                )
                        except ValueError:
                            parsed_price, parsed_currency = parse_price_and_currency(price_text)
                            if parsed_price is not None and parsed_price > 0:
                                price = parsed_price
                                currency = parsed_currency or "USD"
                                price_source = "bs"
                                _log(
                                    "INFO",
                                    "tlc_price_found",
                                    source=price_source,
                                    price=price,
                                    currency=currency,
                                    selector="ProductPriceV2__newProductPrice_fallback",
                                )
            except Exception as e:
                _log("ERR", "tlc_error", stage="bs", error=str(e))

        # fallback 2: другие XPath селекторы
        if price is None:
            tlc_specific_xpaths = [
                "//*[@id='root']/div[2]/div/div/div/div[2]/div/div[3]/div[2]/div/div/div[1]/div/div[3]/div",
                "//*[@id='root']/div[2]/div/div/div/div[2]/div/div[3]/div[2]/div/div/div[1]/div/div[3]/div[1]",
                "//*[@id='root']/div[2]/div/div/div/div[2]/div/div[3]/div[2]/div/div/div[1]/div/div[4]/div[1]",
                "//*[@id='root']//div[contains(@class, 'ProductPriceV2__newProductPrice')]",
                "//div[contains(@class, 'ProductPrice')]",  # упрощенный вариант для медленной загрузки React
                "//span[contains(@class, 'price')]",
                "//div[contains(@class, 'price')]//span",
                "//div[contains(@class, 'ProductPrice')]//span",
                "//span[contains(text(), '$')]",
                "//div[contains(text(), '$')]",
                "//*[contains(text(), '$') and string-length(text()) < 50]",  # любые элементы с $ и коротким текстом
            ]

            for xpath in tlc_specific_xpaths:
                try:
                    price_text = get_text_by_xpath(soup, xpath)
                    if price_text:
                        parsed_price, parsed_currency = parse_price_and_currency(price_text)
                        if parsed_price is not None and parsed_price > 0:
                            currency = parsed_currency or "USD"
                            # конвертируем в USD если нужно
                            if currency and currency.upper() != "USD":
                                price_usd = convert_to_usd(parsed_price, currency)
                                if price_usd is not None:
                                    price = price_usd
                                    currency = "USD"
                                    price_source = "xpath_fallback"
                                    _log(
                                        "INFO",
                                        "tlc_price_found",
                                        source=price_source,
                                        price=price,
                                        currency=currency,
                                        xpath=xpath,
                                    )
                                    break
                                else:
                                    _log(
                                        "ERR",
                                        "tlc_currency_convert_failed",
                                        source="xpath_fallback",
                                        from_price=parsed_price,
                                        from_currency=currency,
                                        xpath=xpath,
                                    )
                                    # оставляем исходную цену, если конвертация недоступна
                                    price = parsed_price
                                    price_source = "xpath_fallback_raw_currency"
                                    break
                            else:
                                price = parsed_price
                                price_source = "xpath_fallback"
                                _log(
                                    "INFO",
                                    "tlc_price_found",
                                    source=price_source,
                                    price=price,
                                    currency=currency,
                                    xpath=xpath,
                                )
                                break
                except Exception:
                    continue

        # fallback 3 (последней надежды): поиск цены с помощью regex во всем тексте страницы
        # используется, когда все селекторы не сработали (сломанная верстка, динамическая загрузка)
        if price is None:
            try:
                # получаем весь текст страницы
                text_content = soup.get_text()

                # ищем паттерн: $ (или USD) + цифры с возможными запятыми и точкой
                # примеры: $1,234.56, $ 1234.56, USD 1234
                patterns = [
                    r'\$\s?([\d,]+\.?\d*)',  # основной паттерн: $1,234.56
                    r'USD\s?([\d,]+\.?\d*)',  # альтернатива: USD 1234
                    r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s?(?:USD|\$)',  # цена перед USD/$
                ]

                for pattern in patterns:
                    match = re.search(pattern, text_content)
                    if match:
                        # извлекаем числовое значение (группа 1)
                        price_str = match.group(1).replace(',', '')  # убираем запятые
                        try:
                            parsed_price = float(price_str)
                            if parsed_price > 0 and parsed_price < 1000000:  # разумная цена (не больше миллиона)
                                price = parsed_price
                                currency = "USD"
                                price_source = "regex"
                                _log("INFO", "tlc_price_found", source=price_source, price=price, currency=currency)
                                break
                        except ValueError:
                            continue
            except Exception as e:
                _log("ERR", "tlc_error", stage="regex", error=str(e))

    # если точная цена не найдена, используем json-ld как последний fallback
    if price is None and jsonld_precise_price is not None:
        price = jsonld_precise_price
        currency = jsonld_precise_currency or "USD"
        price_source = "jsonld_precise_fallback"
        _log("INFO", "tlc_price_found", source=price_source, price=price, currency=currency)

    # проверяем статус через несколько независимых сигналов
    has_sold_out = False
    try:
        # сначала ищем явный sold-out индикатор в dom
        sold_out_selectors = [
            "div.SppButtonWrapper__soldOuttitle___sT_Ct",
            "#root > div:nth-child(2) > div > div > div > div.DesktopWidth__base___3ZRAa > div > div:nth-child(3) > div.NewSppComponent__productDetails___2sbAA > div > div > div.NewSppComponent__sppButtonWrapper___37tT3 > div > div > div > div > div.SppButtonWrapper__soldOuttitle___sT_Ct",
            "[class*='SppButtonWrapper__soldOuttitle']",
            "[class*='soldOuttitle']",
            "[class*='outOfStock']",
            "[class*='soldout']",
            "[class*='SoldOut']",
        ]
        has_sold_out = any(bool(soup.select_one(sel)) for sel in sold_out_selectors)

        if not has_sold_out:
            for el in soup.select(
                "[class*='soldOut'], [class*='soldout'], [class*='outOfStock'], [class*='unavailable']"
            ):
                text = (el.get_text(" ", strip=True) or "").lower()
                if any(marker in text for marker in ("sold out", "out of stock", "unavailable")):
                    has_sold_out = True
                    break

        if not has_sold_out:
            try:
                has_sold_out = xpath_exists(
                    soup,
                    "//*[@id='root']/div[2]/div/div/div/div[2]/div/div[3]/div[2]/div/div/div[3]/div/div/div/div/div[1]",
                )
            except Exception:
                has_sold_out = False

        if not has_sold_out:
            # fallback по тексту страницы оставляем только как последний уровень
            page_text = (soup.get_text(" ", strip=True) or "").lower()
            has_sold_out = any(
                marker in page_text
                for marker in (
                    "sold out",
                    "out of stock",
                    "currently unavailable",
                    "this item is sold",
                )
            )
    except Exception:
        has_sold_out = False

    has_buy_cta = False
    try:
        # доступность подтверждаем только когда видим явный cta покупки
        has_buy_cta = xpath_exists(
            soup,
            "//*[self::button or self::a]"
            "[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to bag') "
            "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to cart') "
            "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'buy now') "
            "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'shop now')]",
        )
        if not has_buy_cta:
            buy_selectors = [
                "[class*='addToBag']",
                "[class*='addToCart']",
                "[class*='buyNow']",
                "[data-testid*='add-to-bag']",
                "[data-testid*='add-to-cart']",
                "button[name='add']",
            ]
            has_buy_cta = any(bool(soup.select_one(sel)) for sel in buy_selectors)
    except Exception:
        has_buy_cta = False

    jsonld_status = None
    try:
        jsonld_status = _extract_status_from_jsonld(soup, product_id_hint)
    except Exception:
        jsonld_status = None

    if has_sold_out:
        status = "Sold Out"
    elif jsonld_status:
        status = jsonld_status
    elif has_buy_cta:
        status = "Available"
    elif _is_valid_product_price(price):
        # если цена уверенно найдена, считаем товар доступным
        status = "Available"
    else:
        status = None

    # для tlc сохраняем цену даже у sold out, если она явно присутствует на странице

    # удаляем случайные микрозначения, если они все же просочились
    if price is not None and not _is_valid_product_price(price):
        _log("ERR", "tlc_price_invalid", price=price, source=price_source)
        price = None
        currency = None

    # финальная конвертация в USD (обязательная для не-USD валют, например ZAR)
    if price is not None and currency and currency.upper() != "USD":
        try:
            price_usd = convert_to_usd(price, currency)
            if price_usd is not None:
                price = price_usd
                currency = "USD"
            else:
                _log("ERR", "tlc_currency_convert_failed", source="final", from_price=price, from_currency=currency)
                # сохраняем исходное значение/валюту, если сервис конвертации недоступен
        except Exception as e:
            _log(
                "ERR",
                "tlc_currency_convert_error",
                source="final",
                from_price=price,
                from_currency=currency,
                error=str(e),
            )
            # сохраняем исходное значение/валюту, если при конвертации возникла ошибка (исключение)

    # если цена не найдена и товар продан, оставляем None
    if price is None:
        currency = None
        if status and "sold" in status.lower():
            _log("INFO", "tlc_price_missing", cause="sold_out", status=status, size=diag_size)
        else:
            cause = "parser"
            if diag_status in {"blocked", "proxy_auth_required", "proxy_error", "timeout", "transport_error"}:
                cause = "proxy"
            _log("ERR", "tlc_price_missing", cause=cause, status=diag_status, size=diag_size)
    elif currency is None:
        currency = "USD"
        _log("ERR", "tlc_currency_missing", price=price, source=price_source, status=diag_status)

    return title, price, currency, status


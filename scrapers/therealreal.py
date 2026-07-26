"""
парсер для therealreal"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from utils.price import parse_price_and_currency
from utils.xpath_helper import xpath_exists

PROMO_TITLE_MARKERS = (
    "site credit",
    "first purchase",
    "sign in",
    "join now",
    "shop now",
)

_MAX_REASONABLE_PRICE = 1_000_000.0


def _detect_blocked_trr(soup: BeautifulSoup) -> str | None:
    # определяем антибот-страницы perimeterx/cloudflare
    title_text = ""
    try:
        if soup.title:
            title_text = (soup.title.get_text(" ", strip=True) or "").lower()
    except Exception:
        title_text = ""

    html_low = ""
    try:
        html_low = str(soup).lower()
    except Exception:
        html_low = ""

    text_low = ""
    try:
        text_low = (soup.get_text(" ", strip=True) or "").lower()
    except Exception:
        text_low = ""

    markers = [
        "access to this page has been denied",
        "perimeterx",
        "px-captcha",
        "window._pxappid",
        "press & hold to confirm you are a human",
        "captcha.px-cloud.net",
        "attention required",
        "checking your browser",
        "request blocked",
    ]

    for marker in markers:
        if marker in title_text or marker in html_low or marker in text_low:
            return marker
    return None


def _has_trr_product_signals(soup: BeautifulSoup) -> bool:
    # проверяем, что это действительно карточка товара, а не заглушка защиты
    try:
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href") and "/products/" in str(canonical.get("href")).lower():
            return True
    except Exception:
        pass

    try:
        og_url = soup.find("meta", {"property": "og:url"})
        if og_url and og_url.get("content") and "/products/" in str(og_url.get("content")).lower():
            return True
    except Exception:
        pass

    try:
        if soup.select_one('[data-testid="product-price/final"]'):
            return True
        if soup.select_one('[data-testid*="add-to-bag"]'):
            return True
    except Exception:
        pass

    html_low = ""
    try:
        html_low = str(soup).lower()
    except Exception:
        html_low = ""

    if "/products/" in html_low and "therealreal.com/products/" in html_low:
        return True
    if "application/ld+json" in html_low and '"@type":"product"' in html_low:
        return True

    return False


def _iter_objects(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_objects(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_objects(item)


def _clean_title_candidate(value: str | None) -> str | None:
    if not value:
        return None
    title = " ".join(str(value).split()).strip()
    if not title:
        return None
    # фильтруем рекламный текст, который может отображаться вместо названия
    low = title.lower()
    if low in {"the realreal", "home"}:
        return None
    if any(marker in low for marker in PROMO_TITLE_MARKERS):
        return None
    if "|" in title:
        left = title.split("|", 1)[0].strip()
        if left and left.lower() not in {"the realreal", "home"}:
            title = left
    return title if len(title) >= 5 else None


def _extract_title_from_jsonld(soup: BeautifulSoup) -> str | None:
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in _iter_objects(data):
            if not isinstance(obj, dict):
                continue
            name = obj.get("name")
            if isinstance(name, str):
                cleaned = _clean_title_candidate(name)
                if cleaned:
                    return cleaned
    return None


def _is_reasonable_price(value: float | None) -> bool:
    return value is not None and 0 < value < _MAX_REASONABLE_PRICE


def _extract_page_slug(soup: BeautifulSoup) -> str | None:
    url_candidates: list[str] = []
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        url_candidates.append(str(canonical.get("href") or "").strip())
    og_url = soup.find("meta", {"property": "og:url"})
    if og_url and og_url.get("content"):
        url_candidates.append(str(og_url.get("content") or "").strip())

    for url in url_candidates:
        try:
            path = urlparse(url).path
        except Exception:
            path = url
        if not path:
            continue
        parts = [part for part in path.split("/") if part]
        if not parts:
            continue
        if "products" in parts:
            idx = parts.index("products")
            if idx < len(parts) - 1:
                return parts[-1].strip().lower() or None
    return None


def _extract_lowest_price_from_text(text: str) -> tuple[float | None, str | None]:
    # выбираем минимальную цену, чтобы не брать "старую" при наличии скидки
    if not text:
        return None, None

    candidates: list[tuple[float, str | None]] = []
    for match in re.finditer(
        r"(?:US\$|\$)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:USD|US\$)",
        text,
        flags=re.I,
    ):
        raw = (match.group(1) or match.group(2) or "").strip()
        if not raw:
            continue
        try:
            parsed = float(raw.replace(",", ""))
        except Exception:
            parsed, parsed_currency = parse_price_and_currency(raw)
            if not _is_reasonable_price(parsed):
                continue
            candidates.append((parsed, parsed_currency))
            continue
        if _is_reasonable_price(parsed):
            candidates.append((parsed, "USD"))

    if not candidates:
        parsed_price, parsed_currency = parse_price_and_currency(text)
        if not _is_reasonable_price(parsed_price):
            return None, None
        return parsed_price, parsed_currency

    candidates.sort(key=lambda item: item[0])
    price, currency = candidates[0]
    return price, (currency or "USD")


def _extract_price_from_final_block(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    selectors = [
        '[data-testid="product-price/final"]',
        '[data-testid*="product-price/final"]',
        '[data-testid*="product-price"] [data-testid*="final"]',
        '[data-testid*="product-price-current"]',
        '[class*="product-price-info__final-price"]',
        '[class*="product-price-info__reduced-price"]',
        '[class*="product-price"][class*="final"]',
        '[class*="productPrice"][class*="final"]',
        '[class*="product-price"] [class*="final"]',
        '[class*="productPrice"] [class*="final"]',
    ]
    for selector in selectors:
        for el in soup.select(selector):
            text = (el.get_text(" ", strip=True) or "").strip()
            if not text:
                continue
            parsed_price, parsed_currency = _extract_lowest_price_from_text(text)
            if _is_reasonable_price(parsed_price):
                return parsed_price, (parsed_currency or "USD")
    return None, None


def _extract_price_from_jsonld(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        for obj in _iter_objects(data):
            if not isinstance(obj, dict):
                continue

            offers = obj.get("offers")
            candidates: list[dict[str, Any]] = []
            if isinstance(offers, dict):
                candidates.append(offers)
            elif isinstance(offers, list):
                candidates.extend([x for x in offers if isinstance(x, dict)])
            else:
                candidates.append(obj)

            for candidate in candidates:
                raw_price = candidate.get("price")
                if raw_price is None:
                    raw_price = candidate.get("lowPrice") or candidate.get("highPrice")
                if raw_price is None:
                    continue

                parsed_price: float | None = None
                if isinstance(raw_price, (int, float)):
                    parsed_price = float(raw_price)
                else:
                    text = str(raw_price).strip()
                    try:
                        parsed_price = float(text.replace(",", ""))
                    except Exception:
                        parsed_price, _ = parse_price_and_currency(text)

                if parsed_price is None or parsed_price <= 0:
                    continue

                parsed_currency = (
                    candidate.get("priceCurrency")
                    or obj.get("priceCurrency")
                    or "USD"
                )
                currency = str(parsed_currency).strip().upper() or "USD"
                return parsed_price, currency
    return None, None


def _extract_price_from_meta(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    amount_meta_selectors = [
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
    ]
    currency_meta_selectors = [
        {"property": "product:price:currency"},
        {"property": "og:price:currency"},
        {"itemprop": "priceCurrency"},
    ]

    price_value: float | None = None
    for attrs in amount_meta_selectors:
        meta = soup.find("meta", attrs=attrs)
        if not meta:
            continue
        content = (meta.get("content") or "").strip()
        if not content:
            continue
        try:
            parsed_price = float(content.replace(",", ""))
        except Exception:
            parsed_price, _ = parse_price_and_currency(content)
        if parsed_price is not None and parsed_price > 0:
            price_value = parsed_price
            break

    if price_value is None:
        return None, None

    currency_value: str | None = None
    for attrs in currency_meta_selectors:
        meta = soup.find("meta", attrs=attrs)
        if not meta:
            continue
        content = (meta.get("content") or "").strip().upper()
        if content:
            currency_value = content
            break

    return price_value, (currency_value or "USD")


def _extract_price_from_scripts(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    patterns: list[tuple[re.Pattern[str], int, bool]] = [
        (
            re.compile(r'"usdCents"\s*:\s*"?(?P<price>\d{3,})', flags=re.I),
            34,
            True,
        ),
        (
            re.compile(r'"unformatted"\s*:\s*"?(?P<price>\d[\d,]*(?:\.\d+)?)', flags=re.I),
            32,
            False,
        ),
        (
            re.compile(r'"formatted"\s*:\s*"?(?P<price>(?:\\u0024|\$)\s*\d[\d,]*(?:\.\d+)?)', flags=re.I),
            24,
            False,
        ),
        (
            re.compile(r'"finalPrice"\s*:\s*"?(?P<price>(?:\\u0024|\$)?\d[\d,]*(?:\.\d+)?)', flags=re.I),
            30,
            False,
        ),
        (
            re.compile(r'"salePrice"\s*:\s*"?(?P<price>(?:\\u0024|\$)?\d[\d,]*(?:\.\d+)?)', flags=re.I),
            26,
            False,
        ),
        (
            re.compile(r'"currentPrice"\s*:\s*"?(?P<price>(?:\\u0024|\$)?\d[\d,]*(?:\.\d+)?)', flags=re.I),
            24,
            False,
        ),
        (
            re.compile(r'"price"\s*:\s*"?(?P<price>(?:\\u0024|\$)?\d[\d,]*(?:\.\d+)?)', flags=re.I),
            18,
            False,
        ),
        (
            re.compile(
                r'"(?:finalPriceCents|salePriceCents|currentPriceCents|priceCents|priceInCents)"\s*:\s*"?(?P<price>\d{3,})',
                flags=re.I,
            ),
            22,
            True,
        ),
    ]
    slug = _extract_page_slug(soup)
    candidates: list[tuple[int, float, str]] = []

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if not text or "price" not in text.lower():
            continue
        low = text.lower()
        script_bonus = 0
        if slug and slug in low:
            script_bonus += 20
        if "product-price/final" in low or "product-price-info__" in low:
            script_bonus += 12
        if "__next_data__" in low or '"offers"' in low:
            script_bonus += 6

        currency_match = re.search(r'"priceCurrency"\s*:\s*"(?P<cur>[A-Z]{3})"', text)
        default_currency = (currency_match.group("cur") if currency_match else "USD").upper()

        for pattern, base_score, is_cents in patterns:
            for match in pattern.finditer(text):
                raw = (match.group("price") or "").strip()
                if not raw:
                    continue
                # отсекаем промо-блоки рядом с ценой
                ctx_start = max(0, match.start() - 80)
                ctx_end = min(len(text), match.end() + 80)
                local_ctx = text[ctx_start:ctx_end].lower()
                if (
                    "site credit" in local_ctx
                    or "estimated retail" in local_ctx
                    or "est. retail" in local_ctx
                    or "you save" in local_ctx
                ):
                    continue
                normalized_raw = raw.replace("\\u0024", "$")
                try:
                    value = float(normalized_raw.replace("$", "").replace(",", ""))
                except Exception:
                    value, parsed_currency = parse_price_and_currency(normalized_raw)
                    if not _is_reasonable_price(value):
                        continue
                    currency = (parsed_currency or default_currency or "USD").upper()
                    candidates.append((base_score + script_bonus, value, currency))
                    continue

                if is_cents:
                    value = value / 100.0

                if not _is_reasonable_price(value):
                    continue
                candidates.append((base_score + script_bonus, value, default_currency or "USD"))

    # оставляем более мягкий порог: часть карточек не получает bonus, но цена валидная
    min_score = 18 if slug else 16
    candidates = [item for item in candidates if item[0] >= min_score]
    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, value, currency = candidates[0]
    return value, currency


def _extract_price_from_visible_text_near_cta(
    soup: BeautifulSoup,
    *,
    has_add_to_bag: bool,
    has_sold_text: bool,
) -> tuple[float | None, str | None]:
    # ищем цену рядом с блоком add-to-bag; не используем этот fallback для sold страниц
    if has_sold_text or not has_add_to_bag:
        return None, None

    lines: list[str] = []
    for chunk in soup.stripped_strings:
        text = " ".join(str(chunk).split()).strip()
        if text and len(text) <= 160:
            lines.append(text)

    if not lines:
        return None, None

    anchor_idx = None
    for idx, line in enumerate(lines):
        low = line.lower()
        if "add to bag" in low:
            anchor_idx = idx
            break
    if anchor_idx is None:
        anchor_idx = min(len(lines) - 1, 120)

    start = max(0, anchor_idx - 40)
    end = min(len(lines), anchor_idx + 20)
    window = lines[start:end]

    bad_tokens = (
        "site credit",
        "est. retail",
        "estimated retail",
        "price was",
        "you save",
        "save ",
        "klarna",
        "afterpay",
    )
    candidates: list[tuple[int, float, str]] = []

    for line in window:
        low = line.lower()
        if any(token in low for token in bad_tokens):
            continue

        parsed_price, parsed_currency = parse_price_and_currency(line)
        if parsed_price is None or parsed_price <= 0:
            continue

        score = 0
        if "price:" in low:
            score += 10
        elif "price" in low:
            score += 6
        if "now" in low or "current" in low or "sale" in low:
            score += 3
        if line.strip().startswith("$"):
            score += 2

        currency = parsed_currency or "USD"
        candidates.append((score, parsed_price, currency))

    if not candidates:
        return None, None

    # сначала максимально информативная строка, затем минимальная цена (для old/new пары)
    candidates.sort(key=lambda x: (-x[0], x[1]))
    _, value, currency = candidates[0]
    return value, currency


def _extract_price_from_price_lines(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    # ищем явную строку вида "Price: $X", чтобы не зависеть от динамических классов
    lines: list[str] = []
    for chunk in soup.stripped_strings:
        text = " ".join(str(chunk).split()).strip()
        if text and len(text) <= 220:
            lines.append(text)

    if not lines:
        return None, None

    # ограничиваем зону основным блоком товара, не заходя в "похожие товары"
    stop_markers = (
        "you may also like",
        "recently viewed",
        "similar items",
        "more from",
    )
    stop_idx = len(lines)
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(marker in low for marker in stop_markers):
            stop_idx = idx
            break

    window = lines[:stop_idx]
    candidates: list[tuple[int, int, float, str]] = []

    for idx, line in enumerate(window):
        low = line.lower()
        if "price:" not in low:
            continue
        if "price was" in low or "est. retail" in low:
            continue
        parsed_price, parsed_currency = parse_price_and_currency(line)
        if parsed_price is None or parsed_price <= 0:
            continue
        score = 20 if low.strip().startswith("- price:") else 12
        candidates.append((score, idx, parsed_price, parsed_currency or "USD"))

    if candidates:
        # выбираем самую релевантную раннюю строку с "Price:"
        candidates.sort(key=lambda item: (-item[0], item[1]))
        _, _, value, currency = candidates[0]
        return value, currency

    # fallback для разнесённого шаблона: "Price" на одной строке, сумма на следующей
    for idx in range(len(window) - 1):
        head = window[idx].strip().lower()
        normalized_head = re.sub(r"[^a-z\s]", "", head).strip()
        if "price" not in normalized_head:
            continue
        if "price was" in normalized_head or "est retail" in normalized_head:
            continue
        parsed_price, parsed_currency = parse_price_and_currency(window[idx + 1])
        if parsed_price is not None and parsed_price > 0:
            return parsed_price, (parsed_currency or "USD")

    return None, None


def _extract_price_from_html_final_block_regex(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    html = str(soup)
    if not html:
        return None, None

    # ловим цену прямо из final price блока, даже если внутри есть sr-only span
    patterns = [
        re.compile(
            r'(?P<tag>div|span)[^>]*data-testid=["\']product-price/final["\'][^>]*>(?P<body>.*?)</(?P=tag)>',
            flags=re.I | re.S,
        ),
        re.compile(
            r'(?P<tag>div|span)[^>]*class=["\'][^"\']*product-price-info__(?:final-price|reduced-price)[^"\']*["\'][^>]*>(?P<body>.*?)</(?P=tag)>',
            flags=re.I | re.S,
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(html):
            body = match.group("body") or ""
            text = re.sub(r"<[^>]+>", " ", body)
            text = " ".join(text.split()).strip()
            if not text:
                continue
            parsed_price, parsed_currency = _extract_lowest_price_from_text(text)
            if _is_reasonable_price(parsed_price):
                return parsed_price, (parsed_currency or "USD")

    return None, None


def _extract_price_from_embedded_payload(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    html = str(soup)
    if not html:
        return None, None

    patterns: list[tuple[re.Pattern[str], int, bool]] = [
        (
            re.compile(
                r'"__typename"\s*:\s*"Pricing"[\s\S]{0,220}?"usdCents"\s*:\s*"?(?P<price>\d{3,})"',
                flags=re.I,
            ),
            38,
            True,
        ),
        (
            re.compile(
                r'"__typename"\s*:\s*"Pricing"[\s\S]{0,220}?"unformatted"\s*:\s*"?(?P<price>\d[\d,]*(?:\.\d+)?)"',
                flags=re.I,
            ),
            36,
            False,
        ),
        (
            re.compile(
                r'"__typename"\s*:\s*"Pricing"[\s\S]{0,220}?"formatted"\s*:\s*"?(?P<price>(?:\\u0024|\$)\s*\d[\d,]*(?:\.\d+)?)"',
                flags=re.I,
            ),
            34,
            False,
        ),
        (
            re.compile(
                r"(?:product-price/final|product-price-info__(?:final-price|reduced-price))[\s\S]{0,220}?(?:\\u0024|\$)\s*(?P<price>\d[\d,]*(?:\.\d+)?)",
                flags=re.I,
            ),
            34,
            False,
        ),
        (
            re.compile(
                r'"(?:final|current|sale|reduced|display|offer)[A-Za-z0-9_-]*price"\s*:\s*"?(?P<price>(?:\\u0024|\$)?\d[\d,]*(?:\.\d+)?)"',
                flags=re.I,
            ),
            26,
            False,
        ),
        (
            re.compile(
                r'"(?:final|current|sale|reduced|display|offer)[A-Za-z0-9_-]*price(?:cents|incents)?"\s*:\s*"?(?P<price>\d{3,})"',
                flags=re.I,
            ),
            24,
            True,
        ),
        (
            re.compile(
                r'"(?:amount|value)"\s*:\s*"?(?P<price>\d[\d,]*(?:\.\d+)?)"',
                flags=re.I,
            ),
            14,
            False,
        ),
    ]
    bad_tokens = (
        "estimated retail",
        "est. retail",
        "est retail",
        "site credit",
        "you save",
        "shipping",
        "tax",
    )
    currency_pattern = re.compile(r'"(?:priceCurrency|currencyCode|currency)"\s*:\s*"(?P<cur>[A-Z]{3})"', flags=re.I)
    html_lower = html.lower()
    candidates: list[tuple[int, float, str]] = []

    for pattern, base_score, is_cents in patterns:
        for match in pattern.finditer(html):
            raw = (match.group("price") or "").strip()
            if not raw:
                continue

            ctx_start = max(0, match.start() - 180)
            ctx_end = min(len(html), match.end() + 180)
            ctx = html[ctx_start:ctx_end]
            ctx_low = html_lower[ctx_start:ctx_end]

            if any(token in ctx_low for token in bad_tokens):
                continue

            normalized = raw.replace("\\u0024", "$")
            try:
                value = float(normalized.replace("$", "").replace(",", ""))
            except Exception:
                value, parsed_currency = parse_price_and_currency(normalized)
                if not _is_reasonable_price(value):
                    continue
                currency = (parsed_currency or "USD").upper()
                score = base_score + (
                    8
                    if "product-price/final" in ctx_low or "product-price-info__" in ctx_low
                    else 0
                )
                candidates.append((score, value, currency))
                continue

            if is_cents or "cents" in ctx_low:
                value /= 100.0
            if not _is_reasonable_price(value):
                continue

            score = base_score
            if "product-price/final" in ctx_low or "product-price-info__" in ctx_low:
                score += 8
            if "final" in ctx_low or "reduced" in ctx_low:
                score += 4
            if '"price"' in ctx_low or "price:" in ctx_low:
                score += 2
            if score < 14:
                continue

            cur_match = currency_pattern.search(ctx)
            currency = (cur_match.group("cur").upper() if cur_match else "USD")
            candidates.append((score, value, currency))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, price, currency = candidates[0]
    return price, currency


def _extract_price_from_text_patterns(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    # fallback для кейсов, где цена есть в тексте, но не попала в структурированные блоки
    chunks: list[str] = []
    for chunk in soup.stripped_strings:
        text = " ".join(str(chunk).split()).strip()
        if text:
            chunks.append(text)

    if not chunks:
        return None, None

    text_blob = "\n".join(chunks)
    lower_blob = text_blob.lower()
    stop_markers = (
        "you may also like",
        "recently viewed",
        "similar items",
        "more from",
    )
    cut_pos = len(text_blob)
    for marker in stop_markers:
        pos = lower_blob.find(marker)
        if pos != -1 and pos < cut_pos:
            cut_pos = pos
    scope = text_blob[:cut_pos]

    patterns = [
        (r"\b(?:our\s+)?price\s*(?::|-)?\s*(?P<price>\$?\d[\d,]*(?:\.\d+)?)", 14),
        (r"\b(?:sale|current|now)\s+price\s*(?::|-)?\s*(?P<price>\$?\d[\d,]*(?:\.\d+)?)", 16),
        (r"\bnow\s*(?::|-)?\s*(?P<price>\$?\d[\d,]*(?:\.\d+)?)", 10),
    ]
    bad_context_tokens = ("price was", "est. retail", "estimated retail", "site credit")
    candidates: list[tuple[int, float, str]] = []

    for pattern, score in patterns:
        for match in re.finditer(pattern, scope, flags=re.I):
            raw = (match.group("price") or "").strip()
            if not raw:
                continue

            ctx_start = max(0, match.start() - 48)
            ctx_end = min(len(scope), match.end() + 48)
            ctx = scope[ctx_start:ctx_end].lower()
            if any(token in ctx for token in bad_context_tokens):
                continue

            parsed_price, parsed_currency = parse_price_and_currency(raw)
            if parsed_price is None or parsed_price <= 0:
                continue
            candidates.append((score, parsed_price, parsed_currency or "USD"))

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, value, currency = candidates[0]
    return value, currency


def _extract_status_from_jsonld(soup: BeautifulSoup) -> str | None:
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in _iter_objects(data):
            if not isinstance(obj, dict):
                continue
            offers = obj.get("offers")
            offer_list = offers if isinstance(offers, list) else [offers] if offers else []
            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue
                availability = str(offer.get("availability") or "").lower()
                if not availability:
                    continue
                if "outofstock" in availability or "soldout" in availability:
                    return "Sold"
                if "instock" in availability:
                    return "Available"
    return None


def scrape_therealreal(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    """
    парсит страницу товара therealreal (title, price, currency, status)
"""
    title: str | None = None
    price: float | None = None
    currency: str | None = None
    status: str | None = None

    blocked_marker = _detect_blocked_trr(soup)
    if blocked_marker and not _has_trr_product_signals(soup):
        return None, None, None, "blocked"

    title = _extract_title_from_jsonld(soup)

    if not title:
        for selector in [
            '[data-testid="product-name"]',
            "h1",
            'meta[property="og:title"]',
            'meta[name="twitter:title"]',
        ]:
            if selector.startswith("meta"):
                el = soup.select_one(selector)
                raw = (el.get("content") if el else None) if el else None
            else:
                el = soup.select_one(selector)
                raw = el.get_text(" ", strip=True) if el else None
            cleaned = _clean_title_candidate(raw)
            if cleaned:
                title = cleaned
                break

    price, currency = _extract_price_from_final_block(soup)
    if price is None:
        price, currency = _extract_price_from_html_final_block_regex(soup)
    if price is None:
        price, currency = _extract_price_from_embedded_payload(soup)
    if price is None:
        price, currency = _extract_price_from_jsonld(soup)
    if price is None:
        price, currency = _extract_price_from_meta(soup)
    if price is None:
        price, currency = _extract_price_from_scripts(soup)
    if price is None:
        price, currency = _extract_price_from_price_lines(soup)
    if price is None:
        price, currency = _extract_price_from_text_patterns(soup)

    status = _extract_status_from_jsonld(soup)

    sold_selectors = [
        '[data-testid*="sold"]',
        '[class*="sold"]',
    ]
    has_sold_text = any(bool(soup.select_one(sel)) for sel in sold_selectors)
    if not has_sold_text:
        try:
            has_sold_text = xpath_exists(
                soup,
                "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sold out') "
                "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'out of stock') "
                "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'this item has sold') "
                "or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'this item is sold')]",
            )
        except Exception:
            has_sold_text = False

    has_add_to_bag = False
    try:
        has_add_to_bag = (
            bool(soup.select_one('[data-testid*="add-to-bag"]'))
            or bool(soup.select_one('[data-testid*="add_to_bag"]'))
            or bool(soup.select_one("button[type='submit'][name*='bag']"))
            or xpath_exists(
                soup,
                "//*[self::button or self::a]"
                "[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add to bag')]",
            )
        )
    except Exception:
        has_add_to_bag = False

    if has_sold_text:
        status = "Sold"
    elif has_add_to_bag:
        status = "Available"

    if price is None:
        price, currency = _extract_price_from_visible_text_near_cta(
            soup,
            has_add_to_bag=has_add_to_bag,
            has_sold_text=has_sold_text,
        )

    # классификация как каталог только в случае, если не найдено признаков страницы продукта
    if price is None and not has_add_to_bag and not has_sold_text and not title:
        catalog_selectors = [
            "[data-testid*='product-tile']",
            "[data-testid*='product-card']",
            ".product-tile",
            ".product-card",
            ".ProductTile",
            ".ProductCard",
        ]
        is_catalog = False
        for selector in catalog_selectors:
            try:
                if len(soup.select(selector)) >= 3:
                    is_catalog = True
                    break
            except Exception:
                continue

        if not is_catalog:
            try:
                product_links = [a for a in soup.select("a[href]") if "/products/" in (a.get("href") or "")]
                if len(product_links) >= 5:
                    is_catalog = True
            except Exception:
                pass

        if is_catalog:
            return None, None, None, "catalog"

    if price is None:
        currency = None
    elif currency is None:
        currency = "USD"

    return title, price, currency, status

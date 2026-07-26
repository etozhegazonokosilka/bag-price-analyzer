"""
утилиты для работы с доменами и url"""

import re
from urllib.parse import urlparse

from config import ALLOWED_DOMAINS

# списки для проверки каталогов
# популярные названия брендов (lowercase)
BRAND_NAMES = {
    "chanel", "gucci", "louis-vuitton", "louisvuitton", "lv", "hermes",
    "prada", "dior", "fendi", "balenciaga", "bottega-veneta", "bottegaveneta",
    "celine", "givenchy", "valentino", "ysl", "saint-laurent", "burberry",
    "chloe", "loewe", "versace", "dolce-gabbana", "miumiu", "cartier"
}

# популярные категории товаров (lowercase)
CATEGORY_NAMES = {
    "bags", "handbags", "purses", "wallets", "accessories", "shoes",
    "clothing", "jewelry", "watches", "sunglasses", "belts", "scarves",
    "women", "men", "kids", "sale", "new-arrivals", "trending", "shop",
    "luxury", "vintage", "designer", "collections", "categories"
}

# ссылки, которые должны рассматриваться как каталожные/нерелевантные
POSHMARK_BLOCKED_LISTING_SLUGS = {
    "christian-dior-black-leather-jeanne-handbag-695048979b2158e2f362c40a",
}


def is_ebay_domain(domain: str) -> bool:
    """проверяет, является ли домен ebay-доменом (любой региональный)"""
    return domain.startswith("ebay.") or domain == "ebay"


def domain_of(url: str) -> str:
    # извлекает домен из url (без поддоменов)
    try:
        netloc = urlparse(url).netloc.lower()
        # убрать поддомены
        parts = netloc.split(".")
        if len(parts) >= 2:
            # для доменов типа ebay.com.au, ebay.co.uk нужно брать последние 3 части
            # если предпоследняя часть - короткое слово (co, com, etc)
            if len(parts) >= 3 and parts[-2] in ["co", "com", "ne", "or", "ac"]:
                return ".".join(parts[-3:])
            return ".".join(parts[-2:])
        return netloc
    except Exception:
        return ""


def is_supported(url: str) -> bool:
    # проверяет, поддерживается ли домен
    d = domain_of(url)
    return d in ALLOWED_DOMAINS


def is_product_page_url(url: str) -> bool:
    """
    проверяет, является ли url страницей отдельного товара, а не каталогом/категорией
    возвращает true если это страница товара, false если каталог/категория
"""
    if not url:
        return False

    url_lower = url.lower()

    # получаем домен
    d = domain_of(url)

    # ebay (все региональные домены): товар = /itm/ или /p/, каталог = /b/, /sch/, /e/, /shop
    if is_ebay_domain(d):
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        # страницы товаров
        if "/itm/" in url_lower or "/p/" in url_lower:
            return True
        # страницы категорий/каталогов - отфильтровываем
        if (
            "/b/" in url_lower
            or "/sch/" in url_lower
            or "/e/" in url_lower
            or path == "/shop"
            or path.startswith("/shop/")
        ):
            return False
        # если не определили - пропускаем (возможно товар)
        return True

    # poshmark: товар = /listing/, каталог = /category/, /brand/, /search
    if d == "poshmark.com":
        parsed = urlparse(url)
        path_lower = (parsed.path or "").lower()
        if "/listing/" in path_lower:
            last_part = path_lower.rstrip("/").split("/")[-1]
            if last_part in POSHMARK_BLOCKED_LISTING_SLUGS:
                return False
            return True
        if (
            "/browse/" in url_lower
            or "/category/" in url_lower
            or "/brand/" in url_lower
            or "/search" in url_lower
        ):
            return False
        return True

    # vestiaire collective: каталог обычно содержит категории в пути
    if d == "vestiairecollective.com":
        # каталоги часто содержат паттерны типа /women/, /men/, /bags/ без id товара
        # товары обычно имеют числовой id в конце url
        path = urlparse(url).path
        path_lower = path.lower()

        # если в конце пути есть числовой id - это товар
        if re.search(r'/\d+\.shtml$', path) or re.search(r'-\d+\.shtml$', path):
            return True

        # проверяем, заканчивается ли URL на название бренда или категории
        # примеры плохих: /women-bags/handbags/chanel/, /men/bags/gucci/
        path_parts = [p for p in path_lower.split('/') if p]
        if path_parts:
            last_part = path_parts[-1]
            # если последняя часть - бренд или категория, это каталог
            if last_part in BRAND_NAMES or last_part in CATEGORY_NAMES:
                return False
            # если предпоследняя часть - бренд, тоже каталог
            if len(path_parts) >= 2 and path_parts[-2] in BRAND_NAMES:
                return False

        # каталоги категорий (короткие пути)
        if path.endswith('/') and len(path_parts) <= 4:
            return False

        # если путь очень короткий (1-2 уровня) - скорее всего каталог
        if len(path_parts) <= 2:
            return False

        return True

    # rebag: товары имеют конкретный slug с id
    if d == "rebag.com":
        path = urlparse(url).path
        # каталоги: /curation/, /shop/ без конкретного товара
        if "/curation/" in url_lower:
            return False
        if path == "/shop/" or path == "/shop":
            return False
        # товары обычно имеют длинный путь с конкретным названием
        return True

    # the luxury closet: каталоги = /women/, /men/, /bags/ и т.д
    if d == "theluxurycloset.com":
        path = urlparse(url).path
        # каталоги категорий
        catalog_patterns = ["/women/", "/men/", "/bags/", "/shoes/", "/accessories/", "/jewelry/", "/watches/"]
        for pattern in catalog_patterns:
            if path.startswith(pattern) and path.count('/') <= 2:
                return False
        # товары обычно имеют более длинный путь
        return True

    # fashionphile: товары имеют /products/ в пути, каталоги = /shop/, /designers/, /bags/, /sale/
    if d == "fashionphile.com":
        path = urlparse(url).path
        path_lower = path.lower()
        path_parts = [p for p in path_lower.split('/') if p]

        # товары всегда содержат /products/ в пути
        if "/products/" in url_lower:
            return True

        # каталоги: /shop/chanel, /categories/handbags
        catalog_patterns = ["/shop/", "/categories/", "/designers/", "/bags/", "/sale/", "/brands/"]
        for pattern in catalog_patterns:
            if pattern in url_lower:
                return False

        # проверяем, заканчивается ли на бренд или категорию
        if path_parts:
            last_part = path_parts[-1]
            if last_part in BRAND_NAMES or last_part in CATEGORY_NAMES:
                return False

        # короткий путь (1-2 уровня) = каталог
        if len(path_parts) <= 2:
            return False

        return False  # по умолчанию для fashionphile - строгая проверка

    # jolicloset: товары имеют числовой id или slug
    if d == "jolicloset.com":
        path = urlparse(url).path
        path_lower = path.lower()
        path_parts = [p for p in path_lower.split("/") if p]
        last_part = path_parts[-1] if path_parts else ""

        # у карточек Jolicloset обычно есть id в конце slug: ...--623450
        if re.search(r"--\d{4,}$", last_part):
            return True

        # явные каталожные разделы
        catalog_markers = (
            "/designers-women/",
            "/designers-men/",
            "/designers-others/",
            "/designers/",
            "/womens-bags/",
            "/mens-bags/",
            "/womens-shoes/",
            "/womens-clothes/",
            "/womens-accessories/",
            "/decoracion/",
            "/deco/",
            "/bags/",
            "/handbags/",
        )
        if any(marker in path_lower for marker in catalog_markers):
            return False

        # короткий путь и/или хвост-категория - не товар
        if last_part in BRAND_NAMES or last_part in CATEGORY_NAMES:
            return False
        if len(path_parts) <= 4:
            return False

        # по умолчанию для jolicloset лучше строго отсеивать, чем пропускать каталоги
        return False

    # yoogiscloset: товары имеют числовой id
    if d == "yoogiscloset.com":
        path = urlparse(url).path
        # каталоги: /bags/, /designers/, /sale/
        if "/bags/" in url_lower or "/designers/" in url_lower or "/sale/" in url_lower:
            if path.count('/') <= 3:
                return False
        # товары обычно имеют числовой id
        if re.search(r'/\d+', path) or len(path.split('/')) > 3:
            return True
        return False

    # the realreal: товары имеют /products/ с конкретным slug
    if d == "therealreal.com":
        path = urlparse(url).path
        path_lower = path.lower()
        path_parts = [p for p in path_lower.split('/') if p]

        # каталоги: /designers/chanel, /women/handbags, /categories/
        catalog_patterns = ["/designers/", "/categories/", "/women/handbags", "/men/", "/jewelry/", "/watches/"]
        for pattern in catalog_patterns:
            if pattern in url_lower:
                # но если дальше есть конкретный товар - это не каталог
                if "/products/" in url_lower and len(path_parts) >= 5:
                    return True
                return False

        # проверяем, заканчивается ли на бренд или категорию
        if path_parts:
            last_part = path_parts[-1]
            if last_part in BRAND_NAMES or last_part in CATEGORY_NAMES:
                return False

        # товары имеют /products/ с конкретным slug (длинный путь)
        if "/products/" in url_lower and len(path_parts) >= 4:
            return True

        # короткий путь = каталог
        if len(path_parts) <= 2:
            return False

        return False  # по умолчанию для therealreal - строгая проверка

    # celebrityowned: товары имеют slug или id
    if d == "celebrityowned.com":
        path = urlparse(url).path
        # каталоги: /shop/, /collections/
        if path == "/shop" or path == "/shop/" or "/collections/" in url_lower:
            return False
        # товары имеют конкретный путь
        if len(path.split('/')) > 2:
            return True
        return False

    # aretrotale: товары имеют slug
    if d == "aretrotale.com":
        path = urlparse(url).path
        # каталоги: /en-row/, /collections/, /collections/
        if path.startswith("/en-row/") or path.startswith("/en/"):
            if path.count('/') <= 3:
                return False
        if "/collections/" in url_lower:
            return False
        # товары имеют более длинный путь
        return True

    # dallas designer handbags: товары имеют slug или id
    if d == "dallasdesignerhandbags.com":
        path = urlparse(url).path
        # каталоги: /collections/, /collections/
        if "/collections/" in url_lower and path.count('/') <= 3:
            return False
        # товары имеют конкретный путь
        if len(path.split('/')) > 2:
            return True
        return False

    # popchill: товары имеют slug
    if d == "popchill.com":
        path = urlparse(url).path
        # каталоги: /zh-tw/, /en/, /collections/
        if path.startswith("/zh-TW") or path.startswith("/en"):
            if path.count('/') <= 2:
                return False
        if "/collections/" in url_lower:
            return False
        # товары имеют более длинный путь
        return True

    # designer exchange: товары имеют slug или id
    if d == "designerexchange.com":
        path = urlparse(url).path
        # каталоги: /uk/, /us/, /collections/
        if path.startswith("/uk/") or path.startswith("/us/"):
            if path.count('/') <= 3:
                return False
        if "/collections/" in url_lower:
            return False
        # товары имеют более длинный путь
        return True

    # anns fabulous finds: товары имеют slug
    if d == "annsfabulousfinds.com":
        path = urlparse(url).path
        # каталоги: /collections/, /collections/
        if "/collections/" in url_lower and path.count('/') <= 3:
            return False
        # товары имеют конкретный путь
        if len(path.split('/')) > 2:
            return True
        return False

    # для остальных доменов - применяем общие правила
    # проверяем, не заканчивается ли URL на бренд или категорию
    path = urlparse(url).path
    path_lower = path.lower()
    path_parts = [p for p in path_lower.split('/') if p]

    if path_parts:
        last_part = path_parts[-1]
        # если последняя часть пути - известный бренд или категория, это каталог
        if last_part in BRAND_NAMES or last_part in CATEGORY_NAMES:
            return False

    # если путь очень короткий (1 уровень), вероятно это каталог
    if len(path_parts) <= 1:
        return False

    # по умолчанию считаем товаром
    return True


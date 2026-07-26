"""
утилиты для работы с прокси
умный выбор прокси в зависимости от сложности домена"""

import os
import random
from typing import Optional
from urllib.parse import urlparse

# группы доменов по сложности парсинга
EASY_DOMAINS = {
    "annsfabulousfinds.com",
    "aretrotale.com",
    "celebrityowned.com",
    "dallasdesignerhandbags.com",
    "designerexchange.com",
    "popchill.com",
    "shop.rebag.com",
    "rebag.com",  # на случай если без shop
}

HARD_DOMAINS = {
    "ebay.com",
    "ebay.co.uk",  # все региональные ebay
    "ebay.de",
    "ebay.fr",
    "ebay.it",
    "ebay.es",
    "ebay.ca",
    "ebay.com.au",
    "fashionphile.com",
    "jolicloset.com",
    "therealreal.com",
    "theluxurycloset.com",
    "vestiairecollective.com",
    "yoogiscloset.com",
    "poshmark.com",
}

# кэш загруженных прокси
_STATIC_PROXIES = None
_ROTATING_PROXY = None


def load_static_proxies(file_path: str = "proxies_static.txt") -> list[str]:
    """
    загружает static прокси из файла и преобразует в формат для requests

    формат входного файла: ip:port:login:password
    формат выходной строки: http://login:password@ip:port

    аргументы:
        file_path: путь к файлу с прокси

    возвращает:
        список прокси в формате requests
"""
    proxies = []

    if not os.path.exists(file_path):
        print(f"⚠️ Файл с прокси не найден: {file_path}")
        return proxies

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()

                # пропускаем пустые строки и комментарии
                if not line or line.startswith("#"):
                    continue

                # проверяем формат host:port:user:pass
                parts = line.split(":")
                if len(parts) < 4:
                    print(f"пропущен некорректный формат прокси в строке {line_num}: {line}")
                    continue

                host = parts[0].strip()
                port = parts[1].strip()
                user = parts[2].strip()
                password = parts[3].strip()

                scheme = os.getenv("STATIC_PROXY_SCHEME", "http").strip().lower()
                if scheme not in {"http", "https"}:
                    scheme = "http"
                # формируем url для requests: scheme://user:pass@host:port
                proxy_url = f"{scheme}://{user}:{password}@{host}:{port}"
                proxies.append(proxy_url)

        print(f"✅ Загружено {len(proxies)} static прокси из {file_path}")

    except Exception as e:
        print(f"❌ Ошибка при загрузке прокси из {file_path}: {e}")

    return proxies


def get_rotating_proxy() -> Optional[str]:
    """
    получает rotating proxy из переменной окружения

    возвращает:
        строка с rotating proxy или None
"""
    global _ROTATING_PROXY

    if _ROTATING_PROXY is None:
        _ROTATING_PROXY = os.getenv("ROTATING_PROXY_URL", "").strip()
        if _ROTATING_PROXY:
            print(f"✅ Rotating proxy загружен из .env")
        else:
            print("⚠️ ROTATING_PROXY_URL не установлен в .env")

    return _ROTATING_PROXY if _ROTATING_PROXY else None


def extract_domain(url: str) -> str:
    """
    извлекает домен из url (без поддоменов для основных доменов)

    аргументы:
        url: полный url

    возвращает:
        доменное имя
"""
    try:
        netloc = urlparse(url).netloc.lower()
        # убираем www
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def is_hard_domain(url: str) -> bool:
    """
    проверяет, является ли домен сложным для парсинга

    аргументы:
        url: url для проверки

    возвращает:
        true если домен требует rotating proxy
"""
    domain = extract_domain(url)

    # проверяем точное совпадение
    if domain in HARD_DOMAINS:
        return True

    # проверяем для региональных ebay (ebay.*)
    if domain.startswith("ebay."):
        return True

    # проверяем поддомены для известных сложных доменов
    for hard_domain in HARD_DOMAINS:
        if domain.endswith(hard_domain):
            return True

    return False


def get_proxy(url: str) -> Optional[dict]:
    """
    умный выбор прокси в зависимости от домена

    логика:
    - для сложных доменов (HARD) → rotating proxy
    - для простых доменов (EASY) → случайный static proxy
    - если нет подходящих прокси → None (прямое подключение)

    аргументы:
        url: url страницы для парсинга

    возвращает:
        словарь с прокси для requests или None
        формат: {"http": "http://...", "https": "http://..."}
"""
    global _STATIC_PROXIES

    # загружаем static прокси при первом вызове
    if _STATIC_PROXIES is None:
        _STATIC_PROXIES = load_static_proxies()

    # определяем сложность домена
    if is_hard_domain(url):
        # используем rotating proxy для сложных доменов
        rotating = get_rotating_proxy()
        if rotating:
            # вАЖНО: для AstroProxy и некоторых других rotating прокси
            # нужно использовать https:// схему для HTTPS запросов
            # если прокси уже в формате http://, заменяем на https:// для HTTPS
            if rotating.startswith("http://"):
                rotating_https = rotating.replace("http://", "https://", 1)
            else:
                rotating_https = rotating

            return {
                "http": rotating,  # для HTTP запросов используем http://
                "https": rotating_https,  # для HTTPS запросов используем https://
            }
        else:
            print(f"⚠️ Rotating proxy не настроен для сложного домена: {extract_domain(url)}")
            # fallback на static если нет rotating
            if _STATIC_PROXIES:
                proxy = random.choice(_STATIC_PROXIES)
                return {"http": proxy, "https": proxy}
            return None
    else:
        # используем static proxy для простых доменов
        if _STATIC_PROXIES:
            proxy = random.choice(_STATIC_PROXIES)
            return {
                "http": proxy,
                "https": proxy,
            }
        else:
            # если нет static прокси, идем напрямую
            return None


def get_proxy_string(url: str) -> Optional[str]:
    """
    возвращает строку прокси (для playwright и других библиотек)

    аргументы:
        url: url страницы для парсинга

    возвращает:
        строка прокси или None
        для HTTPS URL возвращает https:// версию прокси, для HTTP - http:// версию
"""
    proxy_dict = get_proxy(url)
    if proxy_dict:
        # если URL использует HTTPS, возвращаем https версию прокси
        if url.startswith("https://"):
            return proxy_dict.get("https") or proxy_dict.get("http")
        else:
            return proxy_dict.get("http") or proxy_dict.get("https")
    return None


def reset_proxy_cache():
    """
    сбрасывает кэш прокси (для перезагрузки списка)
"""
    global _STATIC_PROXIES, _ROTATING_PROXY
    _STATIC_PROXIES = None
    _ROTATING_PROXY = None
    print("✅ Кэш прокси сброшен")

# для тестирования
if __name__ == "__main__":
    print("\n=== Тестирование системы прокси ===\n")

    # тестовые url
    test_urls = [
        "https://www.annsfabulousfinds.com/products/test",  # простой домен
        "https://www.ebay.com/itm/123456",  # сложный домен
        "https://www.fashionphile.com/products/test",  # сложный домен
        "https://shop.rebag.com/products/test",  # простой домен
    ]

    for url in test_urls:
        domain = extract_domain(url)
        is_hard = is_hard_domain(url)
        proxy = get_proxy(url)

        print(f"URL: {url}")
        print(f"  Домен: {domain}")
        print(f"  Тип: {'HARD' if is_hard else 'EASY'}")
        print(f"  Прокси: {proxy is not None}")
        if proxy:
            proxy_str = list(proxy.values())[0]
            # скрываем пароль для безопасности
            if "@" in proxy_str:
                parts = proxy_str.split("@")
                print(f"  Строка: {parts[0][:20]}...@{parts[1]}")
            else:
                print(f"  Строка: {proxy_str[:30]}...")
        print()



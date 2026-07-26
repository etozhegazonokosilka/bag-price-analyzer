"""
вспомогательные утилиты"""

from __future__ import annotations

import importlib
from typing import Any

# важно: не импортируем подмодули здесь напрямую, чтобы не ловить циклические импорты
# пример цепочки: config -> utils.logger -> utils -> utils.domain -> config
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    # изображение
    "save_temp_image": ("utils.image", "save_temp_image"),
    "load_pil_image_from_path": ("utils.image", "load_pil_image_from_path"),
    "load_pil_image_from_url": ("utils.image", "load_pil_image_from_url"),
    "load_images_parallel": ("utils.image", "load_images_parallel"),
    # цена
    "parse_price_and_currency": ("utils.price", "parse_price_and_currency"),
    "is_valid_price_element": ("utils.price", "is_valid_price_element"),
    "extract_price_from_sold_text": ("utils.price", "extract_price_from_sold_text"),
    "extract_price_from_jsonld": ("utils.price", "extract_price_from_jsonld"),
    "extract_price_from_scripts": ("utils.price", "extract_price_from_scripts"),
    "to_usd": ("utils.price", "to_usd"),
    # домен
    "domain_of": ("utils.domain", "domain_of"),
    "is_supported": ("utils.domain", "is_supported"),
    "is_product_page_url": ("utils.domain", "is_product_page_url"),
    # прокси
    "get_proxy": ("utils.proxy", "get_proxy"),
    "get_proxy_string": ("utils.proxy", "get_proxy_string"),
    "is_hard_domain": ("utils.proxy", "is_hard_domain"),
    "load_static_proxies": ("utils.proxy", "load_static_proxies"),
    "reset_proxy_cache": ("utils.proxy", "reset_proxy_cache"),
    # менеджер прокси
    "get_proxy_manager": ("utils.proxy_manager", "get_proxy_manager"),
    "ProxyManager": ("utils.proxy_manager", "ProxyManager"),
    "ProxyInfo": ("utils.proxy_manager", "ProxyInfo"),
    # дебаг
    "diagnose_response": ("utils.debug_http", "diagnose_response"),
    "print_diagnosis": ("utils.debug_http", "print_diagnosis"),
    "save_debug_html": ("utils.debug_http", "save_debug_html"),
    "check_proxy_format": ("utils.debug_http", "check_proxy_format"),
    "test_proxy_auth": ("utils.debug_http", "test_proxy_auth"),
}

__all__ = list(_LAZY_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr = _LAZY_EXPORTS[name]
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | set(_LAZY_EXPORTS.keys()))

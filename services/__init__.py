"""
сервисы (интеграции с внешними api и моделями)"""

# cLIP функции опциональны - требуют torch/open_clip
try:
    from services.clip import (
        init_clip_model,
        get_image_embedding,
        compute_image_similarity_batch,
        compute_image_similarity,
        get_dominant_colors,
        compare_colors,
        get_perceptual_hash,
        compare_phash,
    )
except ImportError:
    # если torch/open_clip не установлены, функции будут None
    init_clip_model = None
    get_image_embedding = None
    compute_image_similarity_batch = None
    compute_image_similarity = None
    get_dominant_colors = None
    compare_colors = None
    get_perceptual_hash = None
    compare_phash = None

from services.serpapi import (
    serpapi_google_lens,
    extract_results_from_serpapi,
)
from services.cache import (
    get_image_hash,
    load_from_cache,
    save_to_cache,
)
from services.ebay_api import (
    extract_ebay_item_id,
    get_ebay_oauth_token,
    fetch_ebay_item_via_api,
)
from services.currency import (
    CurrencyConverter,
    get_currency_converter,
    convert_to_usd,
)

__all__ = [
    # clip модель
    "init_clip_model",
    "get_image_embedding",
    "compute_image_similarity_batch",
    "compute_image_similarity",
    "get_dominant_colors",
    "compare_colors",
    "get_perceptual_hash",
    "compare_phash",
    # сервис serpapi
    "serpapi_google_lens",
    "extract_results_from_serpapi",
    # кэш
    "get_image_hash",
    "load_from_cache",
    "save_to_cache",
    # ибей апи
    "extract_ebay_item_id",
    "get_ebay_oauth_token",
    "fetch_ebay_item_via_api",
    # курс
    "CurrencyConverter",
    "get_currency_converter",
    "convert_to_usd",
]

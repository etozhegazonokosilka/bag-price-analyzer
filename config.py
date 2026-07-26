"""
модуль конфигурации приложения
все настройки загружаются из переменных окружения или же - .env файла"""

import os

from utils.logger import get_logger, log_event, log_exception


def load_env(filepath: str = ".env") -> None:
    # простая загрузка .env без внешних зависимостей
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

# загружаем .env при импорте модуля
# порядок важен:
# 1) .env (секреты/клиентские переменные)
# 2) .env.defaults (внутренние дефолты, только для отсутствующих ключей)
load_env(".env")
load_env(".env.defaults")

# === api ключи ===
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
SERPAPI_LENS_LOCATION = os.getenv("SERPAPI_LENS_LOCATION", "Miami,Florida,United States").strip()
SERPAPI_LENS_GL = os.getenv("SERPAPI_LENS_GL", "us").strip().lower()
SERPAPI_LENS_HL = os.getenv("SERPAPI_LENS_HL", "en").strip().lower()
SERPAPI_LENS_GOOGLE_DOMAIN = os.getenv("SERPAPI_LENS_GOOGLE_DOMAIN", "google.com").strip().lower()
SERPAPI_LENS_UULE = os.getenv("SERPAPI_LENS_UULE", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_FALLBACK_ENABLED = os.getenv("OPENROUTER_FALLBACK_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
OPENROUTER_FALLBACK_MODELS = tuple(
    model.strip()
    for model in os.getenv(
        "OPENROUTER_FALLBACK_MODELS",
        "openrouter/free,openai/gpt-oss-20b:free",
    ).split(",")
    if model.strip()
)
OPENROUTER_FALLBACK_ALLOW_PAID = os.getenv(
    "OPENROUTER_FALLBACK_ALLOW_PAID",
    "0",
).strip().lower() in {"1", "true", "yes"}
OPENROUTER_FALLBACK_TIMEOUT_SEC = float(os.getenv("OPENROUTER_FALLBACK_TIMEOUT_SEC", "6"))
OPENROUTER_FALLBACK_CONNECT_TIMEOUT_SEC = float(os.getenv("OPENROUTER_FALLBACK_CONNECT_TIMEOUT_SEC", "2"))
OPENROUTER_FALLBACK_MAX_HTML_CHARS = int(os.getenv("OPENROUTER_FALLBACK_MAX_HTML_CHARS", "9000"))
OPENROUTER_FALLBACK_MAX_TOKENS = int(os.getenv("OPENROUTER_FALLBACK_MAX_TOKENS", "140"))
OPENROUTER_FALLBACK_MAX_CONCURRENCY = int(os.getenv("OPENROUTER_FALLBACK_MAX_CONCURRENCY", "2"))
OPENROUTER_FALLBACK_QUEUE_WAIT_SEC = float(os.getenv("OPENROUTER_FALLBACK_QUEUE_WAIT_SEC", "0.25"))

# === ebay api настройки ===
EBAY_API_KEY = os.getenv("EBAY_API_KEY", "").strip()
EBAY_API_SECRET = os.getenv("EBAY_API_SECRET", "").strip()
EBAY_API_ENV = os.getenv("EBAY_API_ENV", "production").strip()

# === настройки прокси ===
ROTATING_PROXY_URL = os.getenv("ROTATING_PROXY_URL", "").strip()
STATIC_PROXIES_FILE = os.getenv("STATIC_PROXIES_FILE", "proxies_static.txt").strip()
STATIC_PROXY_SCHEME = os.getenv("STATIC_PROXY_SCHEME", "http").strip().lower()

# === пороги фильтрации изображений ===
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.85"))
ENABLE_COLOR_CHECK = os.getenv("ENABLE_COLOR_CHECK", "1").strip() in {"1", "true", "yes"}
COLOR_SIMILARITY_THRESHOLD = float(os.getenv("COLOR_SIMILARITY_THRESHOLD", "0.65"))
ENABLE_PHASH_CHECK = os.getenv("ENABLE_PHASH_CHECK", "1").strip() in {"1", "true", "yes"}
PHASH_THRESHOLD = int(os.getenv("PHASH_THRESHOLD", "12"))

# === фильтрация проданных товаров ===
FILTER_SOLD_ITEMS = os.getenv("FILTER_SOLD_ITEMS", "0").strip() in {"1", "true", "yes"}

# === параметры обработки ===
MAX_RESULTS_TO_CHECK = int(os.getenv("MAX_RESULTS_TO_CHECK", "30"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "6"))
MAX_PAGES_TO_SCRAPE = int(os.getenv(
    "MAX_PAGES_TO_SCRAPE",
    "30",
))  # увеличено с 10 до 30 для парсинга всех найденных товаров
SKIP_SCRAPING_IF_SERPAPI_DATA = os.getenv("SKIP_SCRAPING_IF_SERPAPI_DATA", "1").strip() in {"1", "true", "yes"}
MAX_PARALLEL_SCRAPERS = int(os.getenv("MAX_PARALLEL_SCRAPERS", "8"))

# === модель clip ===
CLIP_MODEL_NAME = os.getenv("CLIP_MODEL_NAME", "ViT-L-14")
CLIP_PRETRAINED = "openai"
CLIP_PREWARM_ON_START = os.getenv("CLIP_PREWARM_ON_START", "1").strip().lower() in {"1", "true", "yes"}

# === настройки сервера ===
PORT = int(os.getenv("PORT", "5002"))
USE_RENDER = os.getenv("USE_RENDER", "0").strip() in {"1", "true", "yes"}

# === кэширование ===
ENABLE_CACHE = os.getenv("ENABLE_CACHE", "1").strip() in {"1", "true", "yes"}
CACHE_TTL = int(os.getenv("CACHE_TTL", "86400"))
CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(os.path.dirname(__file__), "cache"))

# === очередь pdf-отчетов ===
REPORT_QUEUE_ENABLED = os.getenv("REPORT_QUEUE_ENABLED", "0").strip() in {"1", "true", "yes"}
REPORT_QUEUE_REDIS_URL = os.getenv("REPORT_QUEUE_REDIS_URL", "redis://127.0.0.1:6379/0").strip()
REPORT_QUEUE_NAME = os.getenv("REPORT_QUEUE_NAME", "reports").strip() or "reports"
REPORT_QUEUE_RESULT_TTL_SEC = int(os.getenv("REPORT_QUEUE_RESULT_TTL_SEC", "86400"))
REPORT_QUEUE_JOB_TIMEOUT_SEC = int(os.getenv("REPORT_QUEUE_JOB_TIMEOUT_SEC", "180"))

# === разрешенные домены для парсинга ===
ALLOWED_DOMAINS = {
    "poshmark.com": "poshmark",
    "vestiairecollective.com": "vestiairecollective",
    # ebay - все региональные домены
    "ebay.com": "ebay",  # сША
    "ebay.ca": "ebay",  # канада
    "ebay.co.uk": "ebay",  # великобритания
    "ebay.de": "ebay",  # германия
    "ebay.fr": "ebay",  # франция
    "ebay.it": "ebay",  # италия
    "ebay.es": "ebay",  # испания
    "ebay.com.au": "ebay",  # австралия
    "ebay.at": "ebay",  # австрия
    "ebay.be": "ebay",  # бельгия
    "ebay.ch": "ebay",  # швейцария
    "ebay.ie": "ebay",  # ирландия
    "ebay.nl": "ebay",  # нидерланды
    "ebay.pl": "ebay",  # польша
    "ebay.in": "ebay",  # индия
    "ebay.com.sg": "ebay",  # сингапур
    "ebay.com.hk": "ebay",  # гонконг
    "ebay.com.my": "ebay",  # малайзия
    "ebay.ph": "ebay",  # филиппины
    # другие сайты
    "rebag.com": "rebag",
    "theluxurycloset.com": "theluxurycloset",
    "fashionphile.com": "fashionphile",
    "jolicloset.com": "jolicloset",
    "yoogiscloset.com": "yoogiscloset",
    "therealreal.com": "therealreal",
    "celebrityowned.com": "celebrityowned",
    "aretrotale.com": "aretrotale",
    "dallasdesignerhandbags.com": "dallasdesignerhandbags",
    "popchill.com": "popchill",
    "designerexchange.com": "designerexchange",
    "annsfabulousfinds.com": "annsfabulousfinds",
}


def print_config():
    """логирует текущую конфигурацию в единый лог-формат (вместо print)"""
    log = get_logger(__name__)

    # выводим только факт наличия credentials, без значений и префиксов
    if EBAY_API_KEY:
        log_event(
            log,
            "config.ebay.client_id",
            level="info",
            present=True,
            client_id_len=len(EBAY_API_KEY),
            env=EBAY_API_ENV,
        )
        if EBAY_API_SECRET:
            log_event(log, "config.ebay.client_secret", level="info", present=True, secret_len=len(EBAY_API_SECRET))
        else:
            log_event(log, "config.ebay.client_secret", level="warning", present=False)
    else:
        log_event(log, "config.ebay.client_id", level="warning", present=False)

    # сводные настройки фильтрации/параллельности
    log_event(
        log,
        "config.filter",
        level="info",
        similarity_threshold=SIMILARITY_THRESHOLD,
        enable_color_check=ENABLE_COLOR_CHECK,
        color_similarity_threshold=COLOR_SIMILARITY_THRESHOLD,
        enable_phash_check=ENABLE_PHASH_CHECK,
        phash_threshold=PHASH_THRESHOLD,
        filter_sold_items=FILTER_SOLD_ITEMS,
        max_results_to_check=MAX_RESULTS_TO_CHECK,
        batch_size=BATCH_SIZE,
        max_parallel_scrapers=MAX_PARALLEL_SCRAPERS,
    )

    # кэширование: создаём папку при старте и пишем событие
    if ENABLE_CACHE:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            log_event(log, "config.cache", level="info", enabled=True, cache_dir=CACHE_DIR, cache_ttl_sec=CACHE_TTL)
        except Exception as e:
            log_exception(log, "config.cache", e, level="warning", enabled=True, cache_dir=CACHE_DIR)
    else:
        log_event(log, "config.cache", level="warning", enabled=False)

    # очередь отчетов
    log_event(
        log,
        "config.report_queue",
        level="info" if REPORT_QUEUE_ENABLED else "warning",
        enabled=REPORT_QUEUE_ENABLED,
        queue_name=REPORT_QUEUE_NAME,
        result_ttl_sec=REPORT_QUEUE_RESULT_TTL_SEC,
        job_timeout_sec=REPORT_QUEUE_JOB_TIMEOUT_SEC,
    )


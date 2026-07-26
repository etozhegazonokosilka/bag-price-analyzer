"""
провайдеры курсов валют и функции конвертации

публичный API:
- CurrencyConverter
- get_currency_converter()
- convert_to_usd()"""

from __future__ import annotations

import os
import re
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from utils.logger import get_logger, log_event

log = get_logger(__name__)


def _currency_console_print(*args, **kwargs):
    # перенаправляем legacy print() в структурированный лог
    try:
        sep = kwargs.get("sep", " ")
        msg = sep.join("" if a is None else str(a) for a in args)
    except Exception:
        msg = " ".join(str(a) for a in args)

    msg = msg.replace("\r", " ").replace("\n", " ")
    msg = " ".join(msg.split()).strip()
    if not msg:
        return

    level = "info"
    lower = msg.lower()
    if lower.startswith("error") or "error" in lower:
        level = "error"
    elif lower.startswith("warning") or "warning" in lower or "timeout" in lower:
        level = "warning"

    log_event(log, "currency.console", level=level, msg=msg)


print = _currency_console_print  # type: ignore[assignment]


# переменные окружения
EXCHANGE_RATE_API_KEY = os.getenv("EXCHANGE_RATE_API_KEY", "").strip()
EXCHANGE_RATE_API_URL = os.getenv("EXCHANGE_RATE_API_URL", "https://v6.exchangerate-api.com/v6").strip()
CURRENCY_API_URL = os.getenv("CURRENCY_API_URL", "https://api.exchangerate-api.com/v4/latest").strip()
CURRENCY_API_KEY = os.getenv("CURRENCY_API_KEY", "").strip()
CURRENCY_CACHE_TTL = int(os.getenv("CURRENCY_CACHE_TTL", "3600"))
CURRENCY_REQUEST_TIMEOUT = int(os.getenv("CURRENCY_REQUEST_TIMEOUT", "10"))
CURRENCY_MAX_RETRIES = int(os.getenv("CURRENCY_MAX_RETRIES", "3"))
CURRENCY_PROVIDER_MODE = os.getenv("CURRENCY_PROVIDER_MODE", "v6_first").strip().lower()


_CURRENCY_ALIASES = {
    "$": "USD",
    "US$": "USD",
    "US $": "USD",
    "USD": "USD",
    "\u20ac": "EUR",
    "EUR": "EUR",
    "\u00a3": "GBP",
    "GBP": "GBP",
    "\u00a5": "JPY",
    "JPY": "JPY",
    "\u20bd": "RUB",
    "RUB": "RUB",
    "RUR": "RUB",
    "CAD": "CAD",
    "CA$": "CAD",
    "C$": "CAD",
    "AUD": "AUD",
    "AU$": "AUD",
    "A$": "AUD",
    "CHF": "CHF",
    "CNY": "CNY",
    "RMB": "CNY",
    "INR": "INR",
    "\u20b9": "INR",
    "KRW": "KRW",
    "\u20a9": "KRW",
    "MXN": "MXN",
    "MX$": "MXN",
    "BRL": "BRL",
    "R$": "BRL",
    "ZAR": "ZAR",
    "HKD": "HKD",
    "HK$": "HKD",
    "SGD": "SGD",
    "S$": "SGD",
    "TWD": "TWD",
    "NT$": "TWD",
    "AED": "AED",
    "SAR": "SAR",
    "QAR": "QAR",
    "TRY": "TRY",
    "NOK": "NOK",
    "SEK": "SEK",
    "DKK": "DKK",
    "PLN": "PLN",
    "CZK": "CZK",
    "HUF": "HUF",
    "ILS": "ILS",
}


def _normalize_currency_code(value: str | None) -> Optional[str]:
    # нормализуем валюту из символа/кода/строки к ISO-коду
    if not value:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    raw = re.sub(r"(?iu)\bруб(?:\.|ля|лей|ль)?\b", "RUB", raw)

    upper = raw.upper()
    if upper in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[upper]

    compact = upper.replace(" ", "")
    if compact in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[compact]

    for symbol, code in _CURRENCY_ALIASES.items():
        if len(symbol) == 1:
            if symbol in raw:
                return code
        else:
            if symbol in raw or symbol in upper or symbol in compact:
                return code

    match = re.search(r"[A-Z]{3}", compact)
    if match:
        return match.group(0)

    return None


class CurrencyProviderBase(ABC):

    @abstractmethod
    def fetch_rates(self, base_currency: str = "USD") -> Optional[Dict[str, float]]:
        pass

    @abstractmethod
    def get_name(self) -> str:
        pass


def _request_json(url: str, *, params: dict | None, timeout: int) -> tuple[int, str, dict | None]:
    response = requests.get(
        url,
        params=params,
        timeout=timeout,
        proxies=None,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    status = int(response.status_code)
    body = response.text or ""
    if status != 200:
        return status, body, None
    try:
        return status, body, response.json()
    except ValueError:
        return status, body, None


class ExchangeRateApiProvider(CurrencyProviderBase):

    # адрес api: https://api.exchangerate-api.com/v4/latest/{BASE}
    def __init__(self, api_url: str | None = None, timeout: int | None = None, max_retries: int | None = None):
        self.api_url = (api_url or CURRENCY_API_URL or "https://api.exchangerate-api.com/v4/latest").strip()
        self.timeout = int(timeout or CURRENCY_REQUEST_TIMEOUT or 10)
        self.max_retries = max(1, int(max_retries or CURRENCY_MAX_RETRIES or 1))

    def get_name(self) -> str:
        return "exchangerate-api.com"

    def _build_url(self, base_currency: str) -> str:
        # поддержка шаблона, query-параметра base и обычного /{base}
        if "{base}" in self.api_url:
            return self.api_url.format(base=base_currency)

        parsed = urlparse(self.api_url)
        if parsed.query:
            qs = parse_qs(parsed.query, keep_blank_values=True)
            qs["base"] = [base_currency]
            new_query = urlencode(qs, doseq=True)
            return urlunparse(parsed._replace(query=new_query))

        return f"{self.api_url.rstrip('/')}/{base_currency}"

    def fetch_rates(self, base_currency: str = "USD") -> Optional[Dict[str, float]]:
        url = self._build_url(base_currency)
        for attempt in range(self.max_retries):
            try:
                status, body, data = _request_json(url, params=None, timeout=self.timeout)
                if status != 200:
                    print(f"ERROR: ошибка запроса к API ({self.get_name()}), HTTP {status}, body: {body[:300]}")
                elif isinstance(data, dict):
                    rates = data.get("rates")
                    if isinstance(rates, dict) and rates:
                        return rates
                    print(f"WARNING: {self.get_name()} вернул ответ без блока rates")
                else:
                    print(f"ERROR: некорректный JSON от {self.get_name()}")
            except requests.exceptions.Timeout:
                print(f"WARNING: таймаут от {self.get_name()} ({attempt + 1}/{self.max_retries})")
            except requests.exceptions.RequestException as e:
                print(f"ERROR: ошибка запроса к API ({self.get_name()}): {e}")
            except Exception as e:
                print(f"WARNING: неожиданная ошибка от {self.get_name()}: {e}")

            if attempt < self.max_retries - 1:
                time.sleep(1)
        return None


class ExchangeRateApiV6Provider(CurrencyProviderBase):

    # адрес api: https://v6.exchangerate-api.com/v6/{API_KEY}/latest/{BASE}
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ):
        self.api_key = (api_key or EXCHANGE_RATE_API_KEY).strip()
        self.base_url = (base_url or EXCHANGE_RATE_API_URL or "https://v6.exchangerate-api.com/v6").strip()
        self.timeout = int(timeout or CURRENCY_REQUEST_TIMEOUT or 10)
        self.max_retries = max(1, int(max_retries or CURRENCY_MAX_RETRIES or 1))

    def get_name(self) -> str:
        return "exchangerate-api.com v6"

    def fetch_rates(self, base_currency: str = "USD") -> Optional[Dict[str, float]]:
        if not self.api_key:
            return None

        url = f"{self.base_url.rstrip('/')}/{self.api_key}/latest/{base_currency}"
        for attempt in range(self.max_retries):
            try:
                status, body, data = _request_json(url, params=None, timeout=self.timeout)
                if status != 200:
                    print(f"ERROR: ошибка запроса к API ({self.get_name()}), HTTP {status}, body: {body[:300]}")
                elif isinstance(data, dict):
                    rates = data.get("conversion_rates") or data.get("rates")
                    if isinstance(rates, dict) and rates:
                        return rates
                    print(f"WARNING: {self.get_name()} вернул ответ без блока rates")
                else:
                    print(f"ERROR: некорректный JSON от {self.get_name()}")
            except requests.exceptions.Timeout:
                print(f"WARNING: таймаут от {self.get_name()} ({attempt + 1}/{self.max_retries})")
            except requests.exceptions.RequestException as e:
                print(f"ERROR: ошибка запроса к API ({self.get_name()}): {e}")
            except Exception as e:
                print(f"WARNING: неожиданная ошибка от {self.get_name()}: {e}")

            if attempt < self.max_retries - 1:
                time.sleep(1)
        return None


class FreeCurrencyApiProvider(CurrencyProviderBase):

    # адрес api: https://api.freecurrencyapi.com/v1/latest?apikey=...&base_currency=USD
    def __init__(self, api_key: str | None = None, timeout: int | None = None, max_retries: int | None = None):
        self.api_key = (api_key or CURRENCY_API_KEY).strip()
        self.base_url = "https://api.freecurrencyapi.com/v1/latest"
        self.timeout = int(timeout or CURRENCY_REQUEST_TIMEOUT or 10)
        self.max_retries = max(1, int(max_retries or CURRENCY_MAX_RETRIES or 1))

    def get_name(self) -> str:
        return "freecurrencyapi.com"

    def fetch_rates(self, base_currency: str = "USD") -> Optional[Dict[str, float]]:
        if not self.api_key:
            return None

        params = {"apikey": self.api_key, "base_currency": base_currency}
        for attempt in range(self.max_retries):
            try:
                status, body, data = _request_json(self.base_url, params=params, timeout=self.timeout)
                if status != 200:
                    print(f"ERROR: ошибка запроса к API ({self.get_name()}), HTTP {status}, body: {body[:300]}")
                elif isinstance(data, dict):
                    rates = data.get("data")
                    if isinstance(rates, dict) and rates:
                        return rates
                    print(f"WARNING: {self.get_name()} вернул ответ без блока rates")
                else:
                    print(f"ERROR: некорректный JSON от {self.get_name()}")
            except requests.exceptions.Timeout:
                print(f"WARNING: таймаут от {self.get_name()} ({attempt + 1}/{self.max_retries})")
            except requests.exceptions.RequestException as e:
                print(f"ERROR: ошибка запроса к API ({self.get_name()}): {e}")
            except Exception as e:
                print(f"WARNING: неожиданная ошибка от {self.get_name()}: {e}")

            if attempt < self.max_retries - 1:
                time.sleep(1)
        return None


class ExchangeRateHostProvider(CurrencyProviderBase):

    # адрес api: https://api.exchangerate.host/latest?base=USD
    def __init__(self, base_url: str | None = None, timeout: int | None = None, max_retries: int | None = None):
        self.base_url = (base_url or "https://api.exchangerate.host/latest").strip()
        self.timeout = int(timeout or CURRENCY_REQUEST_TIMEOUT or 10)
        self.max_retries = max(1, int(max_retries or CURRENCY_MAX_RETRIES or 1))

    def get_name(self) -> str:
        return "exchangerate.host"

    def fetch_rates(self, base_currency: str = "USD") -> Optional[Dict[str, float]]:
        params = {"base": base_currency}
        for attempt in range(self.max_retries):
            try:
                status, body, data = _request_json(self.base_url, params=params, timeout=self.timeout)
                if status != 200:
                    print(f"ERROR: ошибка запроса к API ({self.get_name()}), HTTP {status}, body: {body[:300]}")
                elif isinstance(data, dict):
                    rates = data.get("rates")
                    if isinstance(rates, dict) and rates:
                        return rates
                    print(f"WARNING: {self.get_name()} вернул ответ без блока rates")
                else:
                    print(f"ERROR: некорректный JSON от {self.get_name()}")
            except requests.exceptions.Timeout:
                print(f"WARNING: таймаут от {self.get_name()} ({attempt + 1}/{self.max_retries})")
            except requests.exceptions.RequestException as e:
                print(f"ERROR: ошибка запроса к API ({self.get_name()}): {e}")
            except Exception as e:
                print(f"WARNING: неожиданная ошибка от {self.get_name()}: {e}")

            if attempt < self.max_retries - 1:
                time.sleep(1)
        return None


class CurrencyConverter:

    # объединяет несколько провайдеров и кэширует курсы
    def _build_default_providers(self) -> list:
        mode = CURRENCY_PROVIDER_MODE if CURRENCY_PROVIDER_MODE in {"v6_first", "v6_only"} else "v6_first"
        providers: list = []

        v6 = ExchangeRateApiV6Provider(api_key=EXCHANGE_RATE_API_KEY, base_url=EXCHANGE_RATE_API_URL or None)
        if v6.api_key:
            providers.append(v6)
        else:
            print("WARNING: EXCHANGE_RATE_API_KEY не задан, провайдер v6 пропущен")

        if mode != "v6_only" or not providers:
            providers.append(ExchangeRateApiProvider(api_url=CURRENCY_API_URL))
            providers.append(ExchangeRateHostProvider())
            if CURRENCY_API_KEY:
                providers.append(FreeCurrencyApiProvider(api_key=CURRENCY_API_KEY))

        return providers

    def __init__(self, providers: list | None = None, cache_ttl: int | None = None):
        self.providers = providers if providers is not None else self._build_default_providers()
        self.cache_ttl = int(cache_ttl or CURRENCY_CACHE_TTL or 3600)
        # структура: {base_currency: {"rates": {...}, "timestamp": float}}
        self._cache: Dict[str, dict] = {}

    def _is_cache_valid(self, base_currency: str) -> bool:
        if base_currency not in self._cache:
            return False
        cached = self._cache[base_currency]
        timestamp = float(cached.get("timestamp", 0))
        return (time.time() - timestamp) < self.cache_ttl

    def _get_rates(self, base_currency: str = "USD") -> Optional[Dict[str, float]]:
        if self._is_cache_valid(base_currency):
            return self._cache[base_currency]["rates"]

        for provider in self.providers:
            rates = provider.fetch_rates(base_currency)
            if rates:
                self._cache[base_currency] = {"rates": rates, "timestamp": time.time()}
                print(f"курсы валют получены от {provider.get_name()}")
                return rates

        print("WARNING: все провайдеры курсов завершились ошибкой")
        return None

    def convert(self, amount: float, from_currency: str, to_currency: str = "USD") -> Optional[float]:
        if amount is None:
            return None

        from_code = _normalize_currency_code(from_currency) or (from_currency.upper() if from_currency else None)
        to_code = _normalize_currency_code(to_currency) or (to_currency.upper() if to_currency else "USD")
        if not from_code or not to_code:
            return None

        if from_code == to_code:
            return round(float(amount), 2)

        # сохраняем текущее поведение:
        # берем курсы с базой to_code и считаем amount / rates[from_code]
        rates = self._get_rates(to_code)
        if not rates:
            return None
        if from_code not in rates:
            print(f"WARNING: валюта {from_code} не найдена в курсах")
            return None

        rate = rates[from_code]
        try:
            rate_f = float(rate)
        except Exception:
            return None
        if rate_f <= 0:
            return None

        converted = float(amount) / rate_f
        return round(converted, 2)

    def get_rate(self, from_currency: str, to_currency: str = "USD") -> Optional[float]:
        if not from_currency:
            return None

        from_code = _normalize_currency_code(from_currency) or (from_currency.upper() if from_currency else None)
        to_code = _normalize_currency_code(to_currency) or (to_currency.upper() if to_currency else "USD")
        if not from_code or not to_code:
            return None

        if from_code == to_code:
            return 1.0

        rates = self._get_rates(to_code)
        if not rates or from_code not in rates:
            return None

        rate = rates[from_code]
        try:
            rate_f = float(rate)
        except Exception:
            return None
        if rate_f <= 0:
            return None

        return 1.0 / rate_f

    def clear_cache(self):
        self._cache.clear()

_converter: Optional[CurrencyConverter] = None


def get_currency_converter() -> CurrencyConverter:
    # singleton-экземпляр конвертера для всего процесса
    global _converter
    if _converter is None:
        _converter = CurrencyConverter()
    return _converter


def convert_to_usd(amount: float, from_currency: str) -> Optional[float]:
    # конвертируем сумму в USD, возвращаем None при ошибке
    if amount is None:
        return None

    normalized = _normalize_currency_code(from_currency)
    if not normalized or normalized.upper() == "USD":
        return float(amount)

    converter = get_currency_converter()
    return converter.convert(amount, normalized, "USD")

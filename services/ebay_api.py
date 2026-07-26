"""вспомогательные функции для работы с eBay Browse API

oauth-токен кэшируется в памяти с учетом срока действия
весь вывод идет через utils.logger (без print)"""

from __future__ import annotations

import base64
import re
import time
from typing import Optional

import requests

from config import EBAY_API_ENV, EBAY_API_KEY, EBAY_API_SECRET
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)

_ITEM_ID_PATTERNS = (
    re.compile(r"/itm/(?:[^/?#]+/)?(\d+)(?:[/?#]|$)", re.I),
    re.compile(r"[?&]item=(\d+)(?:[&#]|$)", re.I),
)

_MARKETPLACE_BY_DOMAIN = {
    "ebay.com": "EBAY_US",
    "ebay.ca": "EBAY_CA",
    "ebay.co.uk": "EBAY_GB",
    "ebay.de": "EBAY_DE",
    "ebay.fr": "EBAY_FR",
    "ebay.it": "EBAY_IT",
    "ebay.es": "EBAY_ES",
    "ebay.com.au": "EBAY_AU",
    "ebay.at": "EBAY_AT",
    "ebay.be": "EBAY_BE",
    "ebay.ch": "EBAY_CH",
    "ebay.ie": "EBAY_IE",
    "ebay.nl": "EBAY_NL",
    "ebay.pl": "EBAY_PL",
}


def marketplace_from_domain(domain: str | None) -> str:
    """возвращает eBay marketplace id по домену, по умолчанию EBAY_US"""
    if not domain:
        return "EBAY_US"
    d = str(domain).strip().lower()
    return _MARKETPLACE_BY_DOMAIN.get(d, "EBAY_US")

# простой in-memory кэш oauth-токена
_OAUTH_TOKEN: str | None = None
_OAUTH_EXPIRES_AT: float = 0.0


def extract_ebay_item_id(url: str) -> str | None:
    """извлекает item id eBay из url, если получается"""
    if not url:
        return None
    try:
        for pattern in _ITEM_ID_PATTERNS:
            m = pattern.search(url)
            if m:
                return m.group(1)
        return None
    except Exception:
        return None


def _oauth_endpoint() -> str:
    env = (EBAY_API_ENV or "production").strip().lower()
    if env == "sandbox":
        return "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    return "https://api.ebay.com/identity/v1/oauth2/token"


def get_ebay_oauth_token() -> str | None:
    """получает oauth-токен для eBay API (client credentials)

    возвращает None, если credentials отсутствуют или запрос завершился ошибкой
"""
    global _OAUTH_TOKEN, _OAUTH_EXPIRES_AT

    if not EBAY_API_KEY:
        log_event(log, "ebay.oauth.missing_key", level="warning")
        return None

    # используем кэшированный токен, если он еще не истек
    now = time.time()
    if _OAUTH_TOKEN and _OAUTH_EXPIRES_AT and now < _OAUTH_EXPIRES_AT:
        return _OAUTH_TOKEN

    client_id = EBAY_API_KEY.strip()
    client_secret = (EBAY_API_SECRET or "").strip()

    if not client_secret:
        log_event(log, "ebay.oauth.missing_secret", level="warning")

    oauth_url = _oauth_endpoint()

    scopes_to_try = [
        "https://api.ebay.com/oauth/api_scope",
        "https://api.ebay.com/oauth/api_scope/buy.marketplace.shopping",
    ]

    # пробуем два варианта auth-заголовка:
    # 1) auth=(client_id, secret) через requests
    # 2) явный Authorization: Basic <base64(client:secret)>
    auth_variants = ["requests_auth", "explicit_base64"]

    for auth_method in auth_variants:
        for scope in scopes_to_try:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}

            auth = None
            if auth_method == "explicit_base64":
                creds = f"{client_id}:{client_secret}".encode("utf-8")
                headers["Authorization"] = "Basic " + base64.b64encode(creds).decode("utf-8")
            else:
                auth = (client_id, client_secret)

            data = {"grant_type": "client_credentials", "scope": scope}

            try:
                resp = requests.post(oauth_url, headers=headers, data=data, auth=auth, timeout=10)

                if resp.status_code == 200:
                    payload = resp.json() if resp.text else {}
                    token = payload.get("access_token")
                    expires_in = payload.get("expires_in")

                    if token:
                        _OAUTH_TOKEN = token
                        try:
                            ttl = int(expires_in) if expires_in is not None else 3600
                        except Exception:
                            ttl = 3600
                        # небольшой запас по времени
                        _OAUTH_EXPIRES_AT = time.time() + max(60, ttl - 30)

                        log_event(log, "ebay.oauth.ok", level="info", scope=scope, method=auth_method)
                        return token

                if resp.status_code == 401:
                    error_text = (resp.text or "")[:500]
                    log_event(
                        log,
                        "ebay.oauth.unauthorized",
                        level="warning",
                        status_code=resp.status_code,
                        scope=scope,
                        method=auth_method,
                        endpoint=oauth_url,
                        client_id=client_id[:20],
                        secret_present=bool(client_secret),
                        error=error_text,
                    )
                    continue

                # любые остальные ошибки логируем и продолжаем следующую попытку
                error_text = (resp.text or "")[:500]
                log_event(
                    log,
                    "ebay.oauth.error",
                    level="warning",
                    status_code=resp.status_code,
                    scope=scope,
                    method=auth_method,
                    endpoint=oauth_url,
                    error=error_text,
                )

                # для неожиданных кодов (не auth/scope), выходим раньше
                if resp.status_code not in (400, 401):
                    return None

            except Exception as e:
                log_exception(log, "ebay.oauth.request_error", e, level="warning", scope=scope, method=auth_method)
                continue

    return None


def fetch_ebay_item_via_api(item_id: str, include_meta: bool = False, marketplace_id: str | None = None):
    """получает данные карточки товара через eBay Browse API"""

    def _empty_result():
        return (None, None, None, None, None, None) if include_meta else (None, None, None, None)

    if not EBAY_API_KEY:
        return _empty_result()

    def _extract_error_message(response: requests.Response) -> str | None:
        try:
            error_data = response.json() if response.text else {}
            if isinstance(error_data, dict):
                errors = error_data.get("errors") or []
                if errors and isinstance(errors, list) and isinstance(errors[0], dict):
                    return errors[0].get("message")
        except Exception:
            return None
        return None

    def _read_money(money_obj: object) -> tuple[float | None, str | None]:
        if not isinstance(money_obj, dict):
            return None, None
        value = money_obj.get("value")
        cur = money_obj.get("currency")
        if value is None:
            return None, None
        try:
            parsed = float(value)
        except Exception:
            return None, None
        if parsed <= 0:
            return None, None
        return parsed, (str(cur).upper().strip() if cur else None)

    try:
        token = get_ebay_oauth_token()
        if not token:
            return _empty_result()

        effective_marketplace = (marketplace_id or "EBAY_US").strip().upper()

        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": effective_marketplace,
            "Content-Type": "application/json",
        }

        def _parse_api_payload(data: dict, endpoint: str):
            title = data.get("title")

            condition = None
            country = None
            if include_meta:
                condition = data.get("condition")
                try:
                    item_location = data.get("itemLocation") or {}
                    if isinstance(item_location, dict):
                        country = item_location.get("country") or item_location.get("countryName")
                except Exception:
                    country = None

            price = None
            currency = None
            money_candidates = [
                data.get("price"),
                data.get("currentBidPrice"),
                data.get("convertedCurrentBidPrice"),
                data.get("minimumBidPrice"),
                data.get("convertedMinimumBidPrice"),
                data.get("marketingPrice"),
            ]
            for candidate in money_candidates:
                parsed_price, parsed_currency = _read_money(candidate)
                if parsed_price is not None:
                    price = parsed_price
                    currency = parsed_currency or "USD"
                    break

            status = "Available"
            availability = data.get("availability") or {}
            if isinstance(availability, dict):
                availability_status = availability.get("availabilityStatus")
                if availability_status and "OUT_OF_STOCK" in str(availability_status).upper():
                    status = "Sold"

            item_end_date = data.get("itemEndDate")
            if item_end_date:
                try:
                    from datetime import datetime, timezone

                    end_date = datetime.fromisoformat(str(item_end_date).replace("Z", "+00:00"))
                    if end_date < datetime.now(timezone.utc):
                        status = "Sold"
                except Exception:
                    pass

            if price is not None:
                log_event(
                    log,
                    "ebay.api.ok",
                    level="info",
                    item_id=item_id,
                    endpoint=endpoint,
                    marketplace=effective_marketplace,
                    price=price,
                    currency=currency,
                    status=status,
                    condition=condition if include_meta else None,
                    country=country if include_meta else None,
                )

            return (
                title,
                price,
                currency,
                status,
                condition,
                country,
            ) if include_meta else (title, price, currency, status)

        api_requests = [("item", f"https://api.ebay.com/buy/browse/v1/item/{item_id}")]
        if str(item_id).isdigit():
            api_requests.append(
                (
                    "legacy_item",
                    f"https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id={item_id}",
                )
            )

        last_not_found_error = None
        for endpoint, api_url in api_requests:
            resp = requests.get(api_url, headers=headers, timeout=10)

            if resp.status_code == 200:
                data = resp.json() if resp.text else {}
                return _parse_api_payload(data, endpoint)

            if resp.status_code == 404:
                error_message = _extract_error_message(resp)
                if error_message:
                    last_not_found_error = error_message
                log_event(
                    log,
                    "ebay.api.not_found",
                    level="warning",
                    item_id=item_id,
                    endpoint=endpoint,
                    marketplace=effective_marketplace,
                    error=error_message,
                )
                continue

            error_text = (resp.text or "")[:500]
            log_event(
                log,
                "ebay.api.error",
                level="warning",
                item_id=item_id,
                endpoint=endpoint,
                marketplace=effective_marketplace,
                status_code=resp.status_code,
                error=error_text,
            )
            return _empty_result()

        # если не нашли в локальном marketplace — пробуем US
        if effective_marketplace != "EBAY_US":
            return fetch_ebay_item_via_api(item_id, include_meta=include_meta, marketplace_id="EBAY_US")

        log_event(
            log,
            "ebay.api.not_found.final",
            level="warning",
            item_id=item_id,
            marketplace=effective_marketplace,
            tried=[name for name, _ in api_requests],
            error=last_not_found_error,
        )
        return _empty_result()

    except Exception as e:
        log_exception(log, "ebay.api.unhandled", e, level="warning", item_id=item_id)
        return _empty_result()


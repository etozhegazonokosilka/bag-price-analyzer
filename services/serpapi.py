"""
сервис для работы с serpapi (google lens)"""

import base64
import mimetypes
import os
import time
import uuid

import requests
from requests.exceptions import RequestException

from config import (
    FILTER_SOLD_ITEMS,
    SERPAPI_KEY,
    SERPAPI_LENS_GL,
    SERPAPI_LENS_GOOGLE_DOMAIN,
    SERPAPI_LENS_HL,
    SERPAPI_LENS_LOCATION,
    SERPAPI_LENS_UULE,
)
from utils.domain import is_supported
from utils.logger import get_logger, log_event
from utils.price import parse_price_and_currency

log = get_logger(__name__)
_SERPAPI_URL = "https://serpapi.com/search.json"
_CATBOX_URL = "https://catbox.moe/user/api.php"
_IMGBB_URL = "https://api.imgbb.com/1/upload"
_URL_MODE_MAX_ATTEMPTS = 3
_DIRECT_UPLOAD_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_BASE_SEC = 0.6
_LENS_SEARCH_TYPE_PRIORITY = tuple(
    p.strip().lower()
    for p in (
        os.getenv(
            "SERPAPI_LENS_SEARCH_TYPE_PRIORITY",
            "exact_matches,visual_matches,all",
        )
        or ""
    ).split(",")
    if p and p.strip()
)
_LENS_BLEND_VISUAL = os.getenv("SERPAPI_LENS_BLEND_VISUAL", "1").strip().lower() in {"1", "true", "yes"}
_LENS_FALLBACK_ALL_MIN_SUPPORTED = max(
    0,
    int((os.getenv("SERPAPI_LENS_FALLBACK_ALL_MIN_SUPPORTED", "10") or "10").strip()),
)
_LENS_FALLBACK_ALL_MIN_TOTAL = max(
    0,
    int((os.getenv("SERPAPI_LENS_FALLBACK_ALL_MIN_TOTAL", "30") or "30").strip()),
)
_LENS_FALLBACK_ALL_MIN_VISUAL = max(
    0,
    int((os.getenv("SERPAPI_LENS_FALLBACK_ALL_MIN_VISUAL", "45") or "45").strip()),
)
_LENS_FALLBACK_ALL_MIN_VISUAL_SUPPORTED = max(
    0,
    int(
        (
            os.getenv(
                "SERPAPI_LENS_FALLBACK_ALL_MIN_VISUAL_SUPPORTED",
                str(max(5, _LENS_FALLBACK_ALL_MIN_SUPPORTED // 2)),
            )
            or str(max(5, _LENS_FALLBACK_ALL_MIN_SUPPORTED // 2))
        ).strip()
    ),
)
_DIRECT_DATA_URL_MAX_URL_CHARS = max(
    1024,
    int(
        (
            os.getenv(
                "SERPAPI_DIRECT_DATA_URL_MAX_URL_CHARS",
                os.getenv("SERPAPI_DIRECT_DATA_URI_MAX_URL_CHARS", "7600"),
            )
            or "7600"
        ).strip()
    ),
)
_IMGBB_API_KEY = (os.getenv("IMGBB_API_KEY", "") or "").strip()
_PUBLIC_IMAGE_UPLOAD_ORDER = [
    p.strip().lower()
    for p in (os.getenv("PUBLIC_IMAGE_UPLOAD_ORDER", "imgbb,catbox") or "").split(",")
    if p and p.strip()
]
_R2_ACCOUNT_ID = (os.getenv("R2_ACCOUNT_ID", "") or "").strip()
_R2_ACCESS_KEY_ID = (os.getenv("R2_ACCESS_KEY_ID", "") or "").strip()
_R2_SECRET_ACCESS_KEY = (os.getenv("R2_SECRET_ACCESS_KEY", "") or "").strip()
_R2_BUCKET = (os.getenv("R2_BUCKET", "") or "").strip()
_R2_S3_ENDPOINT = (os.getenv("R2_S3_ENDPOINT", "") or "").strip()
_R2_PUBLIC_BASE_URL = (os.getenv("R2_PUBLIC_BASE_URL", "") or "").strip()
_R2_URL_TTL_SEC = max(30, int((os.getenv("R2_URL_TTL_SEC", "120") or "120").strip()))
_R2_OBJECT_PREFIX = (os.getenv("R2_OBJECT_PREFIX", "tmp/") or "tmp/").strip()
_R2_MAX_ATTEMPTS = max(1, int((os.getenv("R2_MAX_ATTEMPTS", "2") or "2").strip()))


def _build_http_session() -> requests.Session:
    # отдельная сессия без системных proxy env, чтобы не упираться в локальные прокси
    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": None, "https": None}
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            # не держим keep-alive между вызовами, чтобы снизить риск SSLEOF на повторных запросах
            "Connection": "close",
        }
    )
    return session


def _is_retryable_status(status_code: int | None) -> bool:
    if status_code is None:
        return False
    return status_code == 429 or status_code >= 500


def _retry_sleep(attempt: int) -> float:
    delay_sec = min(2.0, _RETRY_BACKOFF_BASE_SEC * attempt)
    time.sleep(delay_sec)
    return delay_sec


def _log(prefix: str, message: str, **fields) -> None:
    # структурированный лог для serpapi
    level = "info"
    pfx = (prefix or "").strip().upper()
    if pfx in {"ERR", "ERROR"}:
        level = "warning"
    elif pfx in {"DEBUG"}:
        level = "debug"

    log_event(log, f"serpapi.{message}", level=level, **fields)


def _guess_content_type(file_path: str) -> str:
    content_type, _ = mimetypes.guess_type(file_path)
    if content_type and "/" in content_type:
        return content_type
    return "image/jpeg"


def _build_data_url_from_file(file_path: str) -> tuple[str, int]:
    # строит data-url для "квази direct-upload" режима
    content_type = _guess_content_type(file_path)
    with open(file_path, "rb") as f:
        payload = f.read()
    if not payload:
        raise RuntimeError("image payload is empty")
    payload_b64 = base64.b64encode(payload).decode("ascii")
    return f"data:{content_type};base64,{payload_b64}", len(payload)


def _build_google_lens_params(
    image_url: str,
    api_key: str,
    geo_params: dict[str, str] | None = None,
    search_type: str | None = None,
) -> dict[str, str]:
    # единая сборка query params для всех режимов SerpAPI Google Lens
    params = {
        "engine": "google_lens",
        "api_key": api_key,
        "url": image_url,
    }

    source_location = SERPAPI_LENS_LOCATION
    source_gl = SERPAPI_LENS_GL
    source_hl = SERPAPI_LENS_HL
    source_google_domain = SERPAPI_LENS_GOOGLE_DOMAIN
    source_uule = SERPAPI_LENS_UULE

    if geo_params:
        source_location = str(geo_params.get("location") or "").strip()
        source_gl = str(geo_params.get("gl") or "").strip()
        source_hl = str(geo_params.get("hl") or "").strip()
        source_google_domain = str(geo_params.get("google_domain") or "").strip()
        source_uule = str(geo_params.get("uule") or "").strip()

    if source_location:
        params["location"] = source_location
    if source_gl:
        params["gl"] = source_gl
    if source_hl:
        params["hl"] = source_hl
    if source_google_domain:
        params["google_domain"] = source_google_domain
    if source_uule:
        params["uule"] = source_uule
    if search_type:
        params["type"] = search_type
    return params


def _estimate_url_mode_request_len(
    image_url: str,
    api_key: str,
    geo_params: dict[str, str] | None = None,
    search_type: str | None = None,
) -> int:
    # оценивает длину итогового url запроса до отправки
    req = requests.Request(
        "GET",
        _SERPAPI_URL,
        params=_build_google_lens_params(
            image_url,
            api_key,
            geo_params=geo_params,
            search_type=search_type,
        ),
    )
    prepared = req.prepare()
    return len(prepared.url or "")


def _build_r2_client():
    # лениво создаем S3-клиент для cloudflare r2
    if not (_R2_ACCOUNT_ID and _R2_ACCESS_KEY_ID and _R2_SECRET_ACCESS_KEY and _R2_BUCKET):
        _log(
            "WARNING",
            "r2_not_configured",
            has_account_id=bool(_R2_ACCOUNT_ID),
            has_access_key=bool(_R2_ACCESS_KEY_ID),
            has_secret=bool(_R2_SECRET_ACCESS_KEY),
            has_bucket=bool(_R2_BUCKET),
        )
        return None

    endpoint_url = _R2_S3_ENDPOINT or f"https://{_R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    try:
        import boto3
        from botocore.client import Config
    except Exception as e:
        _log("WARNING", "r2_sdk_missing", error=str(e), hint="pip install boto3")
        return None

    try:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=_R2_ACCESS_KEY_ID,
            aws_secret_access_key=_R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        return client
    except Exception as e:
        _log("ERR", "r2_client_init_failed", error=str(e), endpoint=endpoint_url)
        return None


def _upload_image_to_r2(file_path: str, max_attempts: int = _R2_MAX_ATTEMPTS) -> dict | None:
    # грузим картинку в cloudflare r2 и возвращаем url + key
    client = _build_r2_client()
    if client is None:
        return None

    prefix = _R2_OBJECT_PREFIX.strip() or "tmp/"
    if not prefix.endswith("/"):
        prefix = f"{prefix}/"
    ext = os.path.splitext(file_path)[1].lower() or ".jpg"
    object_key = f"{prefix}{uuid.uuid4().hex}{ext}"
    content_type = _guess_content_type(file_path)

    for attempt in range(1, max_attempts + 1):
        try:
            _log(
                "INFO",
                "r2_upload_try",
                attempt=attempt,
                max_attempts=max_attempts,
                bucket=_R2_BUCKET,
                key=object_key,
                content_type=content_type,
            )
            client.upload_file(
                file_path,
                _R2_BUCKET,
                object_key,
                ExtraArgs={"ContentType": content_type},
            )

            if _R2_PUBLIC_BASE_URL:
                image_url = f"{_R2_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"
                url_mode = "public_base_url"
            else:
                image_url = client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": _R2_BUCKET, "Key": object_key},
                    ExpiresIn=_R2_URL_TTL_SEC,
                )
                url_mode = "presigned"

            _log(
                "INFO",
                "r2_upload_ok",
                attempt=attempt,
                bucket=_R2_BUCKET,
                key=object_key,
                url_mode=url_mode,
                ttl_sec=_R2_URL_TTL_SEC if url_mode == "presigned" else None,
            )
            return {"image_url": image_url, "object_key": object_key}

        except Exception as e:
            if attempt < max_attempts:
                delay = _retry_sleep(attempt)
                _log(
                    "WARNING",
                    "r2_upload_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(e),
                    exc_type=type(e).__name__,
                    delay_sec=delay,
                )
                continue
            _log(
                "ERR",
                "r2_upload_failed",
                attempt=attempt,
                max_attempts=max_attempts,
                error=str(e),
                exc_type=type(e).__name__,
                bucket=_R2_BUCKET,
                key=object_key,
            )

    return None


def _delete_image_from_r2(object_key: str, max_attempts: int = 2) -> None:
    # удаляем временный объект из r2 после запроса к serpapi
    if not object_key:
        return
    client = _build_r2_client()
    if client is None:
        return

    for attempt in range(1, max_attempts + 1):
        try:
            client.delete_object(Bucket=_R2_BUCKET, Key=object_key)
            _log(
                "INFO",
                "r2_delete_ok",
                attempt=attempt,
                bucket=_R2_BUCKET,
                key=object_key,
            )
            return
        except Exception as e:
            if attempt < max_attempts:
                delay = _retry_sleep(attempt)
                _log(
                    "WARNING",
                    "r2_delete_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(e),
                    exc_type=type(e).__name__,
                    delay_sec=delay,
                )
                continue
            _log(
                "WARNING",
                "r2_delete_failed",
                attempt=attempt,
                max_attempts=max_attempts,
                error=str(e),
                exc_type=type(e).__name__,
                bucket=_R2_BUCKET,
                key=object_key,
            )


def _upload_image_to_catbox(file_path: str, max_attempts: int = 2) -> str | None:
    # грузим картинку на catbox и получаем публичный url для url-mode
    payload = {"reqtype": "fileupload", "userhash": ""}
    for attempt in range(1, max_attempts + 1):
        session = _build_http_session()
        try:
            with open(file_path, "rb") as f:
                files = {"fileToUpload": f}
                _log("INFO", "catbox_upload_try", attempt=attempt, max_attempts=max_attempts, path=file_path)
                resp = session.post(_CATBOX_URL, data=payload, files=files, timeout=30)
                body = (resp.text or "").strip()
                _log(
                    "INFO",
                    "catbox_upload_resp",
                    attempt=attempt,
                    status_code=resp.status_code,
                    body_preview=body[:200],
                )
                if resp.status_code == 200 and body.startswith("http"):
                    return body
        except RequestException as e:
            _log(
                "WARNING",
                "catbox_upload_retry_transport",
                attempt=attempt,
                max_attempts=max_attempts,
                error=str(e),
                exc_type=type(e).__name__,
            )
        except Exception as e:
            _log("ERR", "catbox_upload_failed", attempt=attempt, max_attempts=max_attempts, error=str(e))
        finally:
            try:
                session.close()
            except Exception:
                pass

        if attempt < max_attempts:
            _retry_sleep(attempt)

    return None


def _upload_image_to_imgbb(file_path: str, max_attempts: int = 2) -> str | None:
    # грузим картинку на imgbb и получаем прямой публичный url для url-mode
    if not _IMGBB_API_KEY:
        _log("WARNING", "imgbb_key_missing")
        return None

    for attempt in range(1, max_attempts + 1):
        session = _build_http_session()
        resp = None
        try:
            with open(file_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode("ascii")

            payload = {
                "key": _IMGBB_API_KEY,
                "image": image_b64,
            }
            _log("INFO", "imgbb_upload_try", attempt=attempt, max_attempts=max_attempts, path=file_path)
            resp = session.post(_IMGBB_URL, data=payload, timeout=45)
            body_preview = (resp.text or "")[:300]
            _log("INFO", "imgbb_upload_resp", attempt=attempt, status_code=resp.status_code, body_preview=body_preview)

            if _is_retryable_status(getattr(resp, "status_code", None)) and attempt < max_attempts:
                delay = _retry_sleep(attempt)
                _log(
                    "WARNING",
                    "imgbb_upload_retry_status",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status_code=getattr(resp, "status_code", None),
                    delay_sec=delay,
                )
                continue

            resp.raise_for_status()
            payload_json = resp.json()
            data = payload_json.get("data") if isinstance(payload_json, dict) else None
            if not isinstance(data, dict):
                raise RuntimeError("imgbb response missing data object")

            image_obj = data.get("image") if isinstance(data.get("image"), dict) else {}
            thumb_obj = data.get("thumb") if isinstance(data.get("thumb"), dict) else {}
            candidates = [
                image_obj.get("url"),
                data.get("url"),
                data.get("display_url"),
                thumb_obj.get("url"),
            ]
            for candidate in candidates:
                if isinstance(candidate, str) and candidate.strip().startswith("http"):
                    return candidate.strip()

            raise RuntimeError("imgbb response has no valid url")

        except RequestException as e:
            if attempt < max_attempts:
                delay = _retry_sleep(attempt)
                _log(
                    "WARNING",
                    "imgbb_upload_retry_transport",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(e),
                    exc_type=type(e).__name__,
                    delay_sec=delay,
                )
                continue
            _log(
                "ERR",
                "imgbb_upload_failed",
                attempt=attempt,
                max_attempts=max_attempts,
                error=str(e),
                exc_type=type(e).__name__,
                body_preview=(resp.text[:300] if resp is not None else None),
            )
        except Exception as e:
            if attempt < max_attempts:
                delay = _retry_sleep(attempt)
                _log(
                    "WARNING",
                    "imgbb_upload_retry_error",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(e),
                    exc_type=type(e).__name__,
                    delay_sec=delay,
                )
                continue
            _log(
                "ERR",
                "imgbb_upload_failed",
                attempt=attempt,
                max_attempts=max_attempts,
                error=str(e),
                exc_type=type(e).__name__,
                body_preview=(resp.text[:300] if resp is not None else None),
            )
        finally:
            try:
                session.close()
            except Exception:
                pass

    return None


def _serpapi_google_lens_url_mode(
    image_url: str,
    api_key: str,
    max_attempts: int = _URL_MODE_MAX_ATTEMPTS,
    geo_params: dict[str, str] | None = None,
    search_type: str | None = None,
) -> dict | None:
    params = _build_google_lens_params(
        image_url,
        api_key,
        geo_params=geo_params,
        search_type=search_type,
    )
    _log(
        "INFO",
        "url_mode_start",
        image_url=image_url,
        max_attempts=max_attempts,
        location=params.get("location"),
        gl=params.get("gl"),
        hl=params.get("hl"),
        google_domain=params.get("google_domain"),
        has_uule=bool(params.get("uule")),
        search_type=params.get("type") or "all",
    )

    for attempt in range(1, max_attempts + 1):
        session = _build_http_session()
        resp = None
        try:
            resp = session.get(_SERPAPI_URL, params=params, timeout=60)

            if _is_retryable_status(getattr(resp, "status_code", None)) and attempt < max_attempts:
                delay = _retry_sleep(attempt)
                _log(
                    "WARNING",
                    "url_mode_retry_status",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    status_code=getattr(resp, "status_code", None),
                    delay_sec=delay,
                )
                continue

            resp.raise_for_status()

            try:
                result = resp.json()
                _log("INFO", "url_mode_ok", status_code=resp.status_code, attempt=attempt)
                return result
            except Exception as e:
                if attempt < max_attempts:
                    delay = _retry_sleep(attempt)
                    _log(
                        "WARNING",
                        "url_mode_retry_bad_json",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        status_code=getattr(resp, "status_code", None),
                        error=str(e),
                        body_preview=(resp.text[:300] if resp is not None else None),
                        delay_sec=delay,
                    )
                    continue

                _log(
                    "ERR",
                    "url_mode_failed",
                    status_code=getattr(resp, "status_code", None),
                    error=str(e),
                    body_preview=(resp.text[:300] if resp is not None else None),
                )
                return None

        except RequestException as e:
            if attempt < max_attempts:
                delay = _retry_sleep(attempt)
                _log(
                    "WARNING",
                    "url_mode_retry_transport",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(e),
                    exc_type=type(e).__name__,
                    delay_sec=delay,
                )
                continue

            _log(
                "ERR",
                "url_mode_failed",
                status_code=getattr(resp, "status_code", None),
                error=str(e),
                exc_type=type(e).__name__,
                body_preview=(resp.text[:300] if resp is not None else None),
            )
            return None
        finally:
            try:
                session.close()
            except Exception:
                pass

    return None


def _serpapi_section_count(payload: dict | None, section_name: str) -> int:
    if not isinstance(payload, dict):
        return 0
    section = payload.get(section_name)
    if isinstance(section, list):
        return len(section)
    return 0


def _serpapi_item_link(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("link") or item.get("source") or "").strip()


def _serpapi_payload_counts(payload: dict | None) -> tuple[int, int, int, int, int]:
    exact_count = _serpapi_section_count(payload, "exact_matches")
    visual_count = _serpapi_section_count(payload, "visual_matches")
    organic_count = _serpapi_section_count(payload, "organic_results")
    shopping_count = _serpapi_section_count(payload, "shopping_results")
    total_count = exact_count + visual_count + organic_count + shopping_count
    return exact_count, visual_count, organic_count, shopping_count, total_count


def _count_supported_links(payload: dict | None) -> int:
    if not isinstance(payload, dict):
        return 0
    supported_count = 0
    seen_links = set()
    for section_name in ("exact_matches", "visual_matches", "organic_results", "shopping_results"):
        section = payload.get(section_name) or []
        if not isinstance(section, list):
            continue
        for item in section:
            link = _serpapi_item_link(item)
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            if is_supported(link):
                supported_count += 1
    return supported_count


def _merge_serpapi_payloads(base_payload: dict, extra_payload: dict, search_type: str) -> dict:
    merged = dict(base_payload or {})
    for section_name in ("exact_matches", "visual_matches", "organic_results", "shopping_results"):
        merged_section = []
        seen_links = set()
        for src in (base_payload, extra_payload):
            section = (src or {}).get(section_name) or []
            if not isinstance(section, list):
                continue
            for item in section:
                if not isinstance(item, dict):
                    continue
                link = _serpapi_item_link(item)
                if link:
                    if link in seen_links:
                        continue
                    seen_links.add(link)
                merged_section.append(item)
        merged[section_name] = merged_section

    search_types_used = list(merged.get("_search_types_used") or [])
    if search_type and search_type not in search_types_used:
        search_types_used.append(search_type)
    merged["_search_types_used"] = search_types_used
    return merged


def _serpapi_google_lens_search_by_priority(
    image_url: str,
    api_key: str,
    max_attempts: int = _URL_MODE_MAX_ATTEMPTS,
    geo_params: dict[str, str] | None = None,
) -> dict | None:
    search_types = _LENS_SEARCH_TYPE_PRIORITY or ("exact_matches", "visual_matches", "all")
    normalized_types = []
    for raw_search_type in search_types:
        search_type = str(raw_search_type or "").strip().lower()
        if search_type not in {"exact_matches", "visual_matches", "all"}:
            continue
        if search_type == "visual_matches" and not _LENS_BLEND_VISUAL:
            continue
        if search_type not in normalized_types:
            normalized_types.append(search_type)
    if not normalized_types:
        normalized_types = ["exact_matches"]

    merged_result = None
    for search_type in normalized_types:
        if search_type == "all" and merged_result is not None:
            _, merged_visual, _, _, merged_total = _serpapi_payload_counts(merged_result)
            merged_supported = _count_supported_links(merged_result)
            has_enough_supported_total = (
                merged_supported >= _LENS_FALLBACK_ALL_MIN_SUPPORTED
                and merged_total >= _LENS_FALLBACK_ALL_MIN_TOTAL
            )
            has_strong_visual_signal = (
                _LENS_FALLBACK_ALL_MIN_VISUAL > 0
                and merged_visual >= _LENS_FALLBACK_ALL_MIN_VISUAL
                and merged_supported >= _LENS_FALLBACK_ALL_MIN_VISUAL_SUPPORTED
            )
            if (
                has_enough_supported_total
                or has_strong_visual_signal
            ):
                skip_reason = "fallback_not_needed"
                if has_strong_visual_signal and not has_enough_supported_total:
                    skip_reason = "fallback_not_needed_visual_enough"
                _log(
                    "INFO",
                    "search_type_skip",
                    search_type=search_type,
                    reason=skip_reason,
                    supported_links=merged_supported,
                    total_results=merged_total,
                    visual_matches=merged_visual,
                    min_supported=_LENS_FALLBACK_ALL_MIN_SUPPORTED,
                    min_total=_LENS_FALLBACK_ALL_MIN_TOTAL,
                    min_visual=_LENS_FALLBACK_ALL_MIN_VISUAL,
                    min_visual_supported=_LENS_FALLBACK_ALL_MIN_VISUAL_SUPPORTED,
                )
                continue

        result = _serpapi_google_lens_url_mode(
            image_url,
            api_key,
            max_attempts=max_attempts,
            geo_params=geo_params,
            search_type=search_type,
        )
        if result is None:
            continue

        exact_count, visual_count, organic_count, shopping_count, total_count = _serpapi_payload_counts(result)
        supported_links = _count_supported_links(result)

        _log(
            "INFO",
            "search_type_result",
            search_type=search_type,
            exact_matches=exact_count,
            visual_matches=visual_count,
            organic_results=organic_count,
            shopping_results=shopping_count,
            total_results=total_count,
            supported_links=supported_links,
        )

        if merged_result is None:
            merged_result = result
            merged_result["_search_types_used"] = [search_type]
        else:
            merged_result = _merge_serpapi_payloads(merged_result, result, search_type)

        (
            merged_exact,
            merged_visual,
            merged_organic,
            merged_shopping,
            merged_total,
        ) = _serpapi_payload_counts(merged_result)
        merged_supported = _count_supported_links(merged_result)
        _log(
            "INFO",
            "search_type_merged",
            search_type=search_type,
            exact_matches=merged_exact,
            visual_matches=merged_visual,
            organic_results=merged_organic,
            shopping_results=merged_shopping,
            total_results=merged_total,
            supported_links=merged_supported,
            search_types_used=",".join(merged_result.get("_search_types_used") or []),
        )

        if search_type == "exact_matches" and exact_count <= 0:
            _log("WARNING", "search_type_empty", search_type=search_type, reason="no_exact_matches")
        elif total_count <= 0:
            _log("WARNING", "search_type_empty", search_type=search_type, reason="no_results")

    return merged_result


def _serpapi_google_lens_direct_upload(
    image_path: str,
    api_key: str,
    max_attempts: int = _DIRECT_UPLOAD_MAX_ATTEMPTS,
    geo_params: dict[str, str] | None = None,
) -> dict:
    # у google_lens нет стабильного multipart upload endpoint,
    # поэтому "direct upload" делаем через data-url в url-mode
    _log("INFO", "direct_upload_start", attempt=1, max_attempts=max_attempts, mode="data_url")
    data_url, payload_size = _build_data_url_from_file(image_path)
    first_search_type = (_LENS_SEARCH_TYPE_PRIORITY[0] if _LENS_SEARCH_TYPE_PRIORITY else None)
    estimated_url_len = _estimate_url_mode_request_len(
        data_url,
        api_key,
        geo_params=geo_params,
        search_type=first_search_type,
    )
    _log(
        "INFO",
        "direct_upload_data_url_ready",
        payload_size_bytes=payload_size,
        estimated_url_len=estimated_url_len,
        max_url_chars=_DIRECT_DATA_URL_MAX_URL_CHARS,
    )

    if estimated_url_len > _DIRECT_DATA_URL_MAX_URL_CHARS:
        _log(
            "WARNING",
            "direct_upload_data_url_too_large",
            payload_size_bytes=payload_size,
            estimated_url_len=estimated_url_len,
            max_url_chars=_DIRECT_DATA_URL_MAX_URL_CHARS,
        )
        raise RuntimeError(
            f"serpapi data-url too large for direct mode: estimated_url_len={estimated_url_len}"
        )

    result = _serpapi_google_lens_search_by_priority(
        data_url,
        api_key,
        max_attempts=max_attempts,
        geo_params=geo_params,
    )
    if result is None:
        raise RuntimeError("serpapi direct data-url mode failed")

    _log(
        "INFO",
        "direct_upload_ok",
        mode="data_url",
        payload_size_bytes=payload_size,
        estimated_url_len=estimated_url_len,
    )
    return result


def serpapi_google_lens(image_path: str, geo_params: dict[str, str] | None = None) -> dict:
    # выполняет поиск через serpapi google lens
    if not SERPAPI_KEY:
        _log("ERR", "serpapi_key_missing", key="SERPAPI_KEY")
        raise RuntimeError("serpapi_key не задан. укажите SERPAPI_KEY в .env")

    file_size = None
    try:
        file_size = os.path.getsize(image_path)
    except Exception:
        pass
    _log("INFO", "serpapi_upload", path=image_path, size_bytes=file_size)

    # режим 1: cloudflare r2 -> url-mode (основной)
    r2_upload_result = _upload_image_to_r2(image_path)
    if r2_upload_result is not None:
        r2_url = (r2_upload_result.get("image_url") or "").strip()
        r2_object_key = (r2_upload_result.get("object_key") or "").strip()
        try:
            if r2_url:
                result = _serpapi_google_lens_search_by_priority(
                    r2_url,
                    SERPAPI_KEY,
                    max_attempts=_URL_MODE_MAX_ATTEMPTS,
                    geo_params=geo_params,
                )
                if result is not None:
                    _log("INFO", "r2_primary_ok", key=r2_object_key)
                    return result
                _log("WARNING", "r2_primary_failed", key=r2_object_key)
        finally:
            _delete_image_from_r2(r2_object_key)
    else:
        _log("WARNING", "r2_primary_unavailable")

    # режим 2: direct upload через data-url
    direct_upload_error: Exception | None = None
    try:
        return _serpapi_google_lens_direct_upload(
            image_path,
            SERPAPI_KEY,
            geo_params=geo_params,
        )
    except Exception as e:
        direct_upload_error = e
        _log(
            "WARNING",
            "direct_upload_secondary_failed",
            error=str(e),
            exc_type=type(e).__name__,
        )

    # режим 3: url-mode через публичные хостинги (по порядку в PUBLIC_IMAGE_UPLOAD_ORDER)
    uploader_registry = {
        "imgbb": _upload_image_to_imgbb,
        "catbox": _upload_image_to_catbox,
    }
    upload_order = _PUBLIC_IMAGE_UPLOAD_ORDER or ["imgbb", "catbox"]

    for provider in upload_order:
        uploader = uploader_registry.get(provider)
        if uploader is None:
            _log("WARNING", "public_upload_provider_unknown", provider=provider)
            continue

        image_url = uploader(image_path)
        if not image_url:
            _log("WARNING", "public_upload_provider_unavailable", provider=provider)
            continue

        result = _serpapi_google_lens_search_by_priority(
            image_url,
            SERPAPI_KEY,
            max_attempts=_URL_MODE_MAX_ATTEMPTS,
            geo_params=geo_params,
        )
        if result is not None:
            _log("INFO", "public_upload_provider_ok", provider=provider)
            return result

        _log("WARNING", "url_mode_provider_failed", provider=provider)

    _log("ERR", "all_modes_failed")
    if direct_upload_error is not None:
        raise direct_upload_error
    raise RuntimeError("не удалось получить результаты serpapi: r2, direct upload и публичные fallback режимы завершились ошибкой")


def _is_likely_sold_item(url: str, title: str = "") -> bool:
    """проверяет, является ли товар вероятно проданным по url и заголовку (до парсинга)"""
    if not url:
        return False

    url_lower = url.lower()
    title_lower = (title or "").lower()

    # паттерны url, указывающие на проданные товары
    sold_url_patterns = [
        "/sold/",
        "/completed/",
        "sold=true",
        "status=sold",
        "ended=",
        "sold-item",
        "completed-listing",
        "sold-listing",
    ]

    # ключевые слова в заголовке (только явные признаки)
    sold_title_keywords = [
        "sold listing",
        "ended listing",
        "completed listing",
        "no longer available",
    ]

    # проверяем url
    for pattern in sold_url_patterns:
        if pattern in url_lower:
            return True

    # проверяем заголовок (только если есть явные признаки)
    if title_lower:
        for keyword in sold_title_keywords:
            if keyword in title_lower:
                return True

    return False


def extract_results_from_serpapi(data: dict) -> list[dict]:
    # извлекаем ссылки и изображения из ответа google lens (serpapi)
    results = []
    if not isinstance(data, dict):
        _log("ERR", "serpapi_invalid_data", data_type=type(data).__name__)
        return results

    def _extract_price(item: dict, title: str) -> tuple[float | None, str | None]:
        price_text = (
            item.get("price")
            or item.get("extracted_price")
            or item.get("price_with_symbol")
            or item.get("price_str")
            or ""
        )
        price_val, price_cur = parse_price_and_currency(str(price_text))
        if price_val is None:
            try:
                if isinstance(item.get("extracted_price"), (int, float)):
                    price_val = float(item.get("extracted_price"))
                    price_cur = "USD"
            except Exception:
                pass
        if price_val is None and title:
            p, c = parse_price_and_currency(title)
            if p is not None:
                price_val = p
                price_cur = c or price_cur
        return price_val, price_cur

    def _upsert_result(
        link: str,
        thumb: str,
        title: str,
        price_val: float | None,
        price_cur: str | None,
        source: str,
    ) -> bool:
        if not link:
            return False
        existing = next((r for r in results if r.get("url") == link), None)
        if existing:
            sources = existing.get("serp_sources")
            if isinstance(sources, list):
                if source not in sources:
                    sources.append(source)
            else:
                existing["serp_sources"] = [existing.get("serp_source") or source, source]
            if existing.get("serp_price") is None and price_val is not None:
                existing["serp_price"] = price_val
                existing["serp_currency"] = price_cur
            if not existing.get("title") and title:
                existing["title"] = title
            if not existing.get("image") and thumb:
                existing["image"] = thumb
            return False
        results.append(
            {
                "url": link,
                "image": thumb,
                "title": title,
                "serp_price": price_val,
                "serp_currency": price_cur,
                "serp_source": source,
                "serp_sources": [source],
            }
        )
        return True

    # exact_matches обрабатываем первым приоритетом
    exact_matches = data.get("exact_matches") or []
    exact_added = 0
    exact_filtered = 0
    for item in exact_matches:
        link = item.get("link") or item.get("source") or ""
        thumb = item.get("thumbnail") or item.get("image") or ""
        title = item.get("title") or ""

        if FILTER_SOLD_ITEMS and _is_likely_sold_item(link, title):
            exact_filtered += 1
            continue

        price_val, price_cur = _extract_price(item, title)
        if _upsert_result(link, thumb, title, price_val, price_cur, "exact_matches"):
            exact_added += 1

    # visual_matches идет вторым
    visual_matches = data.get("visual_matches") or []
    visual_added = 0
    visual_filtered = 0
    for item in visual_matches:
        link = item.get("link") or item.get("source") or ""
        thumb = item.get("thumbnail") or item.get("image") or ""
        title = item.get("title") or ""

        if FILTER_SOLD_ITEMS and _is_likely_sold_item(link, title):
            visual_filtered += 1
            continue

        price_val, price_cur = _extract_price(item, title)
        if _upsert_result(link, thumb, title, price_val, price_cur, "visual_matches"):
            visual_added += 1

    # organic_results иногда содержат цены
    organic = data.get("organic_results") or []
    organic_added = 0
    organic_filtered = 0
    for item in organic:
        link = item.get("link") or ""
        if not (link and is_supported(link)):
            continue

        title = item.get("title") or ""
        thumb = item.get("thumbnail") or item.get("image") or ""

        if FILTER_SOLD_ITEMS and _is_likely_sold_item(link, title):
            organic_filtered += 1
            continue

        price_val, price_cur = _extract_price(item, title)
        if _upsert_result(link, thumb, title, price_val, price_cur, "organic_results"):
            organic_added += 1

    # shopping_results может быть хорошим источником цен
    shopping = data.get("shopping_results") or []
    shopping_added = 0
    shopping_filtered = 0
    for item in shopping:
        link = item.get("link") or ""
        thumb = item.get("thumbnail") or item.get("image") or ""
        title = item.get("title") or ""

        if FILTER_SOLD_ITEMS and _is_likely_sold_item(link, title):
            shopping_filtered += 1
            continue

        price_val, price_cur = _extract_price(item, title)
        if _upsert_result(link, thumb, title, price_val, price_cur, "shopping_results"):
            shopping_added += 1

    _log(
        "SUMMARY",
        "serpapi_results",
        total=len(results),
        exact_added=exact_added,
        visual_added=visual_added,
        organic_added=organic_added,
        shopping_added=shopping_added,
        exact_filtered=exact_filtered,
        visual_filtered=visual_filtered,
        organic_filtered=organic_filtered,
        shopping_filtered=shopping_filtered,
    )

    return results

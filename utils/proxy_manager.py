"""
централизованный менеджер прокси"""

import os
import random
import re
import threading
import time
from collections import defaultdict
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse, quote

import requests

from config import ROTATING_PROXY_URL, STATIC_PROXIES_FILE, STATIC_PROXY_SCHEME
from utils.domain import domain_of
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)

PROXY_BAD_COOLDOWN = int(os.getenv("PROXY_BAD_COOLDOWN", "600"))  # общий cooldown в сек
ROTATING_PROXY_BAD_COOLDOWN = int(os.getenv("ROTATING_PROXY_BAD_COOLDOWN", "180"))
STATIC_PROXY_BAD_COOLDOWN = int(os.getenv("STATIC_PROXY_BAD_COOLDOWN", str(PROXY_BAD_COOLDOWN)))
ROTATING_SPLIT_SCHEME = os.getenv("ROTATING_SPLIT_SCHEME", "0").strip().lower() in {"1", "true", "yes"}
ROTATING_PROXY_MAX_BAD_STREAK = max(1, int(os.getenv("ROTATING_PROXY_MAX_BAD_STREAK", "3")))
STATIC_PROXY_MAX_BAD_STREAK = max(1, int(os.getenv("STATIC_PROXY_MAX_BAD_STREAK", "2")))
ROTATING_PROXY_FORCE_HTTP = os.getenv("ROTATING_PROXY_FORCE_HTTP", "1").strip().lower() in {"1", "true", "yes"}
ROTATING_PROXY_URLS = os.getenv("ROTATING_PROXY_URLS", "")
ROTATING_PROXY_ROTATE_URLS = os.getenv("ROTATING_PROXY_ROTATE_URLS", "")
ROTATING_CHANGEIP_BEFORE_REQUEST = os.getenv(
    "ROTATING_CHANGEIP_BEFORE_REQUEST",
    "1",
).strip().lower() in {"1", "true", "yes"}
ROTATING_CHANGEIP_CONNECT_TIMEOUT_SEC = float(os.getenv("ROTATING_CHANGEIP_CONNECT_TIMEOUT_SEC", "10"))
ROTATING_CHANGEIP_TIMEOUT_SEC = float(os.getenv("ROTATING_CHANGEIP_TIMEOUT_SEC", "30"))
ROTATING_CHANGEIP_WAIT_SEC = float(os.getenv("ROTATING_CHANGEIP_WAIT_SEC", "1.2"))
ROTATING_CHANGEIP_MIN_INTERVAL_SEC = float(os.getenv("ROTATING_CHANGEIP_MIN_INTERVAL_SEC", "0"))
ROTATING_CHANGEIP_ENFORCE_INTERVAL = os.getenv(
    "ROTATING_CHANGEIP_ENFORCE_INTERVAL",
    "1",
).strip().lower() in {"1", "true", "yes"}
ROTATING_CHANGEIP_RETRY_WAIT_SEC = float(os.getenv("ROTATING_CHANGEIP_RETRY_WAIT_SEC", "2"))
ROTATING_CHANGEIP_MAX_ATTEMPTS = max(1, int(os.getenv("ROTATING_CHANGEIP_MAX_ATTEMPTS", "1")))
ROTATING_PROXY_QUARANTINE_SEC = int(os.getenv("ROTATING_PROXY_QUARANTINE_SEC", "240"))
ROTATING_PROXY_LEASE_TIMEOUT_SEC = float(os.getenv("ROTATING_PROXY_LEASE_TIMEOUT_SEC", "90"))
ROTATING_PROXY_MAX_IN_FLIGHT = max(1, int(os.getenv("ROTATING_PROXY_MAX_IN_FLIGHT", "2")))
ROTATING_PROXY_RECENT_BAD_COOLDOWN_SEC = max(0.0, float(os.getenv("ROTATING_PROXY_RECENT_BAD_COOLDOWN_SEC", "25")))
ROTATING_PROXY_DOMAIN_QUARANTINE_SEC = max(30, int(os.getenv("ROTATING_PROXY_DOMAIN_QUARANTINE_SEC", "600")))
ROTATING_PROXY_NETWORK_ERROR_DOMAIN_THRESHOLD = max(
    1,
    int(os.getenv("ROTATING_PROXY_NETWORK_ERROR_DOMAIN_THRESHOLD", "2")),
)
ROTATING_SOFTBLOCK_WINDOW_SEC = max(30, int(os.getenv("ROTATING_SOFTBLOCK_WINDOW_SEC", "600")))
ROTATING_SOFTBLOCK_DOMAIN_THRESHOLD = max(1, int(os.getenv("ROTATING_SOFTBLOCK_DOMAIN_THRESHOLD", "2")))
ROTATING_SOFTBLOCK_FORCE_ZENROWS_SEC = max(30, int(os.getenv("ROTATING_SOFTBLOCK_FORCE_ZENROWS_SEC", "600")))

_SOFT_BLOCK_TOKENS = (
    "proxy blocked",
    "playwright: blocked",
)


def _split_env_list(raw: str) -> List[str]:
    if not raw:
        return []
    items = re.split(r"[\r\n,;]+", raw)
    return [item.strip() for item in items if item and item.strip()]


def _normalize_proxy_uri(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if not s.startswith(("http://", "https://", "socks5://", "socks5h://")):
        s = f"http://{s}"
    if ROTATING_PROXY_FORCE_HTTP and s.startswith("https://"):
        s = "http://" + s[len("https://") :]
    return s


def _normalize_rotate_url(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if not s.startswith(("http://", "https://")):
        s = f"http://{s}"
    return s

EASY_DOMAINS = {
    "annsfabulousfinds.com",
    "aretrotale.com",
    "celebrityowned.com",
    "dallasdesignerhandbags.com",
    "designerexchange.com",
    "popchill.com",
    "shop.rebag.com",
    "rebag.com",
}

HARD_DOMAINS = {
    "ebay.com",
    "ebay.co.uk",
    "ebay.de",
    "ebay.fr",
    "ebay.it",
    "ebay.es",
    "ebay.ca",
    "ebay.com.au",
    "fashionphile.com",
    "therealreal.com",
    "theluxurycloset.com",
    "vestiairecollective.com",
    "yoogiscloset.com",
    "poshmark.com",
}

STRICT_DOMAINS = {
    "ebay.com",
    "ebay.co.uk",
    "ebay.de",
    "ebay.fr",
    "ebay.it",
    "ebay.es",
    "ebay.ca",
    "ebay.com.au",
    "jolicloset.com",
    "poshmark.com",
    "therealreal.com",
    "vestiairecollective.com",
}

_DIRECT_FIRST_ENV = os.getenv(
    "DIRECT_FIRST_DOMAINS",
    "theluxurycloset.com",
)
DIRECT_FIRST_DOMAINS = {
    item.strip().lower()
    for item in _DIRECT_FIRST_ENV.split(",")
    if item and item.strip()
}

_ROTATING_ONLY_ENV = os.getenv(
    "ROTATING_ONLY_DOMAINS",
    "therealreal.com",
)
ROTATING_ONLY_DOMAINS = {
    item.strip().lower()
    for item in _ROTATING_ONLY_ENV.split(",")
    if item and item.strip()
}


class ProxyInfo:
    """информация об одном прокси"""

    def __init__(self, proxy_uri: str, proxy_type: str = "static", rotate_url: str | None = None):
        self.proxy_uri = proxy_uri
        self.proxy_type = proxy_type  # режимы: static, rotating, direct
        self.rotate_url = (rotate_url or "").strip()
        self.is_bad = False
        self.fail_count = 0
        self.last_used = 0.0
        self.last_success = 0.0
        self.last_error: Optional[str] = None
        self.last_bad_time = 0.0
        self.last_rotate_ts = 0.0
        self.rotate_lock = threading.Lock()
        self.in_flight = 0
        self.lease_until = 0.0
        self.last_leased_ts = 0.0
        self.quarantine_until = 0.0

        # возможности прокси (поддерживает ли HTTPS CONNECT)
        self.supports_http: Optional[bool] = None
        self.supports_https_connect: Optional[bool] = None

        try:
            parsed = urlparse(proxy_uri)
            self.scheme = parsed.scheme
            self.hostname = parsed.hostname
            self.port = parsed.port
            self.username = parsed.username
            self.password = parsed.password
        except Exception:
            self.scheme = None
            self.hostname = None
            self.port = None
            self.username = None
            self.password = None

    def mark_bad(self, error: str | None = None) -> None:
        self.fail_count += 1
        self.last_error = error
        self.last_bad_time = time.time()

        max_bad_streak = (
            ROTATING_PROXY_MAX_BAD_STREAK
            if self.proxy_type == "rotating"
            else STATIC_PROXY_MAX_BAD_STREAK
        )
        if self.fail_count >= max_bad_streak:
            self.is_bad = True

    def mark_good(self) -> None:
        self.is_bad = False
        self.fail_count = 0
        self.last_success = time.time()
        self.last_error = None

    def is_quarantined(self) -> bool:
        return self.quarantine_until > time.time()

    def quarantine(self, seconds: float, reason: str | None = None) -> None:
        if seconds <= 0:
            return
        now = time.time()
        until = now + float(seconds)
        if until > self.quarantine_until:
            self.quarantine_until = until
        self.last_bad_time = now
        if reason:
            self.last_error = reason

    def to_requests_dict(self) -> Dict[str, str]:
        """преобразует в словарь для requests"""
        if not self.proxy_uri:
            return {}

        # для rotating часто нужен https:// вариант прокси на https-запросах
        if self.proxy_type == "rotating" and ROTATING_SPLIT_SCHEME:
            https_proxy_uri = self.proxy_uri
            if self.proxy_uri.startswith("http://"):
                https_proxy_uri = "https://" + self.proxy_uri[len("http://") :]
            return {
                "http": self.proxy_uri,
                "https": https_proxy_uri,
            }

        return {
            "http": self.proxy_uri,
            "https": self.proxy_uri,
        }

    def to_playwright_dict(self) -> Optional[Dict]:
        """преобразует в словарь для Playwright"""
        if not self.proxy_uri or not self.hostname or not self.port:
            return None

        scheme = self.scheme or "http"
        result: Dict[str, str] = {
            "server": f"{scheme}://{self.hostname}:{self.port}",
        }
        if self.username and self.password:
            result["username"] = self.username
            result["password"] = self.password
        return result


class ProxyManager:
    """централизованный менеджер прокси"""

    def __init__(self):
        self.static_proxies: List[ProxyInfo] = []
        self.rotating_proxy: Optional[ProxyInfo] = None
        self.rotating_proxies: List[ProxyInfo] = []
        self._rotating_pick_lock = threading.Lock()
        self._domain_soft_block_lock = threading.Lock()
        self._domain_soft_blocks: Dict[str, List[float]] = defaultdict(list)
        self._domain_force_zenrows_until: Dict[str, float] = {}
        self._rotating_domain_quarantine_until: Dict[Tuple[str, str], float] = {}
        self._rotating_domain_fail_streak: Dict[Tuple[str, str], int] = defaultdict(int)
        self.direct_proxy: Optional[ProxyInfo] = None

        self.stats: Dict[str, int] = defaultdict(int)

        self._load_proxies()

    @staticmethod
    def _mask_proxy_uri(proxy_uri: str) -> str:
        proxy_uri_masked = proxy_uri
        if "@" in proxy_uri_masked:
            parts = proxy_uri_masked.split("@")
            auth_part = parts[0].split(":")
            if len(auth_part) >= 2:
                proxy_uri_masked = f"{auth_part[0]}:{auth_part[1]}:****@{parts[1]}"
        return proxy_uri_masked

    def _release_stale_rotating_leases(self) -> None:
        now = time.time()
        with self._rotating_pick_lock:
            for proxy in self.rotating_proxies:
                if proxy.in_flight <= 0:
                    continue
                if proxy.lease_until > 0 and now < proxy.lease_until:
                    continue
                stale_for = max(0.0, now - (proxy.last_leased_ts or now))
                proxy.in_flight = 0
                proxy.lease_until = 0.0
                proxy.last_leased_ts = 0.0
                log_event(
                    log,
                    "proxy.rotating.lease.stale_release",
                    level="warning",
                    proxy=self._mask_proxy_uri(proxy.proxy_uri),
                    stale_for_sec=round(stale_for, 2),
                )

    def release_proxy(
        self,
        proxy_info: Optional[ProxyInfo],
        *,
        domain: str = "",
        url: str = "",
        reason: str = "",
    ) -> None:
        if not proxy_info or proxy_info.proxy_type != "rotating":
            return
        with self._rotating_pick_lock:
            if proxy_info.in_flight > 0:
                proxy_info.in_flight -= 1
            else:
                proxy_info.in_flight = 0
            if proxy_info.in_flight == 0:
                proxy_info.lease_until = 0.0
                proxy_info.last_leased_ts = 0.0
        if reason:
            log_event(
                log,
                "proxy.rotating.lease.release",
                level="debug",
                domain=domain or None,
                url=url or None,
                proxy=self._mask_proxy_uri(proxy_info.proxy_uri),
                reason=reason,
            )

    def _quarantine_rotating_proxy(
        self,
        proxy_info: Optional[ProxyInfo],
        *,
        reason: str,
        error_text: str = "",
    ) -> None:
        if not proxy_info or proxy_info.proxy_type != "rotating":
            return
        quarantine_sec = max(0, int(ROTATING_PROXY_QUARANTINE_SEC))
        if quarantine_sec <= 0:
            return

        old_until = proxy_info.quarantine_until
        proxy_info.quarantine(quarantine_sec, reason=reason)
        if proxy_info.quarantine_until <= old_until:
            return

        error_short = (error_text or reason or "")[:120]
        log_event(
            log,
            "proxy.rotating.quarantine",
            level="warning",
            proxy=self._mask_proxy_uri(proxy_info.proxy_uri),
            quarantine_sec=quarantine_sec,
            reason=reason,
            error=error_short or None,
        )
        self.stats["proxy_rotating_quarantine"] += 1

    @staticmethod
    def _normalize_domain_key(domain: str | None) -> str:
        return (domain or "").strip().lower()

    @staticmethod
    def _network_error_tokens() -> Tuple[str, ...]:
        return (
            "timeout",
            "timed out",
            "read timed out",
            "remote end closed",
            "connection reset",
            "connectionerror",
            "connection aborted",
            "proxyerror",
            "transport:",
        )

    @staticmethod
    def _hard_block_tokens() -> Tuple[str, ...]:
        return (
            "403",
            "429",
            "blocked",
            "access denied",
            "cloudflare",
            "captcha",
            "challenge",
        )

    def _proxy_domain_health_key(self, proxy_info: ProxyInfo, domain_key: str) -> Tuple[str, str]:
        return (proxy_info.proxy_uri, domain_key)

    def _is_rotating_proxy_domain_quarantined(self, proxy_info: ProxyInfo, domain_key: str, *, now: float) -> bool:
        if not domain_key:
            return False
        key = self._proxy_domain_health_key(proxy_info, domain_key)
        with self._domain_soft_block_lock:
            until = float(self._rotating_domain_quarantine_until.get(key, 0.0))
            if until <= now:
                if key in self._rotating_domain_quarantine_until:
                    self._rotating_domain_quarantine_until.pop(key, None)
                return False
            return True

    def _quarantine_rotating_proxy_for_domain(
        self,
        proxy_info: Optional[ProxyInfo],
        *,
        domain: str | None,
        reason: str,
        error_text: str = "",
        seconds: int | None = None,
    ) -> None:
        if not proxy_info or proxy_info.proxy_type != "rotating":
            return
        domain_key = self._normalize_domain_key(domain)
        if not domain_key:
            return

        quarantine_sec = max(30, int(seconds if seconds is not None else ROTATING_PROXY_DOMAIN_QUARANTINE_SEC))
        now = time.time()
        key = self._proxy_domain_health_key(proxy_info, domain_key)
        with self._domain_soft_block_lock:
            old_until = float(self._rotating_domain_quarantine_until.get(key, 0.0))
            new_until = now + float(quarantine_sec)
            if new_until <= old_until:
                return
            self._rotating_domain_quarantine_until[key] = new_until

        log_event(
            log,
            "proxy.rotating.domain_quarantine",
            level="warning",
            proxy=self._mask_proxy_uri(proxy_info.proxy_uri),
            domain=domain_key,
            reason=reason,
            quarantine_sec=quarantine_sec,
            error=(error_text or reason)[:120] or None,
        )
        self.stats["proxy_rotating_domain_quarantine"] += 1

    def _mark_rotating_proxy_domain_failure(self, proxy_info: Optional[ProxyInfo], *, domain: str | None) -> int:
        if not proxy_info or proxy_info.proxy_type != "rotating":
            return 0
        domain_key = self._normalize_domain_key(domain)
        if not domain_key:
            return 0
        key = self._proxy_domain_health_key(proxy_info, domain_key)
        with self._domain_soft_block_lock:
            self._rotating_domain_fail_streak[key] = int(self._rotating_domain_fail_streak.get(key, 0)) + 1
            return self._rotating_domain_fail_streak[key]

    def _clear_rotating_proxy_domain_health(self, proxy_info: Optional[ProxyInfo], *, domain: str | None) -> None:
        if not proxy_info or proxy_info.proxy_type != "rotating":
            return
        domain_key = self._normalize_domain_key(domain)
        if not domain_key:
            return
        key = self._proxy_domain_health_key(proxy_info, domain_key)
        with self._domain_soft_block_lock:
            self._rotating_domain_fail_streak.pop(key, None)
            self._rotating_domain_quarantine_until.pop(key, None)

    def _register_rotating_soft_block_for_domain(self, domain: str | None) -> None:
        domain_key = self._normalize_domain_key(domain)
        if not domain_key:
            return
        now = time.time()
        window_sec = max(30, int(ROTATING_SOFTBLOCK_WINDOW_SEC))
        threshold = max(1, int(ROTATING_SOFTBLOCK_DOMAIN_THRESHOLD))
        force_sec = max(30, int(ROTATING_SOFTBLOCK_FORCE_ZENROWS_SEC))

        with self._domain_soft_block_lock:
            events = self._domain_soft_blocks[domain_key]
            cutoff = now - float(window_sec)
            events[:] = [ts for ts in events if ts >= cutoff]
            events.append(now)
            hits = len(events)

            if hits < threshold:
                return

            new_until = now + float(force_sec)
            old_until = self._domain_force_zenrows_until.get(domain_key, 0.0)
            if new_until <= old_until:
                return

            self._domain_force_zenrows_until[domain_key] = new_until
            self.stats["proxy_domain_force_zenrows"] += 1
            log_event(
                log,
                "proxy.domain.force_zenrows",
                level="warning",
                domain=domain_key,
                soft_blocks_hits=hits,
                threshold=threshold,
                window_sec=window_sec,
                force_zenrows_sec=force_sec,
            )

    def should_force_zenrows_for_url(self, url: str = "", *, domain: str = "") -> bool:
        domain_key = self._normalize_domain_key(domain or domain_of(url))
        if not domain_key:
            return False

        now = time.time()
        with self._domain_soft_block_lock:
            until = float(self._domain_force_zenrows_until.get(domain_key, 0.0))
            if until <= now:
                if domain_key in self._domain_force_zenrows_until:
                    self._domain_force_zenrows_until.pop(domain_key, None)
                return False
            return True

    def _load_proxies(self) -> None:
        """загружает все типы прокси"""
        self._load_static_proxies()
        self._load_rotating_proxies()
        self.direct_proxy = ProxyInfo("", "direct")

    def _refresh_bad_proxies(self) -> None:
        """снимает флаг is_bad после cooldown"""
        now = time.time()
        self._release_stale_rotating_leases()

        with self._domain_soft_block_lock:
            stale_keys = [k for k, until in self._rotating_domain_quarantine_until.items() if float(until) <= now]
            for key in stale_keys:
                self._rotating_domain_quarantine_until.pop(key, None)

        for proxy in self.static_proxies:
            if proxy.is_bad and (now - proxy.last_bad_time) >= STATIC_PROXY_BAD_COOLDOWN:
                proxy.is_bad = False
                proxy.fail_count = 0
                proxy.last_error = None

        for proxy in self.rotating_proxies:
            if proxy.is_bad and (now - proxy.last_bad_time) >= ROTATING_PROXY_BAD_COOLDOWN:
                proxy.is_bad = False
                proxy.fail_count = 0
                proxy.last_error = None

    def _load_static_proxies(self) -> None:
        """загружает static-прокси из файла"""
        file_path = STATIC_PROXIES_FILE or "proxies_static.txt"
        requested_scheme = (STATIC_PROXY_SCHEME or "http").strip().lower()
        scheme = "http"
        if requested_scheme and requested_scheme != "http":
            log_event(
                log,
                "proxy.static.force_http",
                level="warning",
                requested_scheme=requested_scheme,
                forced_scheme=scheme,
            )

        if not os.path.exists(file_path):
            log_event(log, "proxy.static.missing_file", level="warning", file=file_path)
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.lstrip("\ufeff").strip()
                    if not line or line.startswith("#"):
                        continue

                    # ожидаемый формат: host:port:user:pass
                    parts = line.split(":")
                    if len(parts) < 4:
                        log_event(
                            log,
                            "proxy.static.invalid_line",
                            level="warning",
                            file=file_path,
                            line=line_num,
                            value=line,
                        )
                        continue

                    host = parts[0].strip()
                    port = parts[1].strip()
                    user = parts[2].strip()
                    password = parts[3].strip()

                    password = password.replace("\r", "").replace("\n", "").strip()

                    login_encoded = quote(user, safe="")
                    password_encoded = quote(password, safe="")

                    proxy_url = f"{scheme}://{login_encoded}:{password_encoded}@{host}:{port}"
                    proxy_info = ProxyInfo(proxy_url, "static")
                    self.static_proxies.append(proxy_info)
            log_event(
                log,
                "proxy.static.loaded",
                level="info",
                file=file_path,
                count=len(self.static_proxies),
                scheme=scheme,
            )
        except Exception as e:
            log_exception(log, "proxy.static.load_error", e, file=file_path)

    def _load_rotating_proxies(self) -> None:
        # загружаем rotating proxy из ROTATING_PROXY_URLS (пул) или ROTATING_PROXY_URL (один)
        raw_urls = _split_env_list(ROTATING_PROXY_URLS)
        if not raw_urls and ROTATING_PROXY_URL:
            raw_urls = [ROTATING_PROXY_URL]

        if not raw_urls:
            self.rotating_proxy = None
            self.rotating_proxies = []
            log_event(log, "proxy.rotating.missing", level="warning")
            return

        rotate_urls = _split_env_list(ROTATING_PROXY_ROTATE_URLS)
        loaded: List[ProxyInfo] = []
        for idx, raw_proxy in enumerate(raw_urls):
            proxy_uri = _normalize_proxy_uri(raw_proxy)
            if not proxy_uri:
                continue
            rotate_url = _normalize_rotate_url(rotate_urls[idx]) if idx < len(rotate_urls) else ""
            loaded.append(ProxyInfo(proxy_uri, "rotating", rotate_url=rotate_url))

        self.rotating_proxies = loaded
        self.rotating_proxy = loaded[0] if loaded else None

        if not loaded:
            log_event(log, "proxy.rotating.missing", level="warning")
            return

        log_event(
            log,
            "proxy.rotating.loaded",
            level="info",
            count=len(loaded),
            with_rotate_url=sum(1 for p in loaded if p.rotate_url),
            single_mode=(len(loaded) == 1),
        )

    def _pick_rotating_proxy(
        self,
        *,
        is_https: bool,
        domain: str = "",
        acquire_lease: bool = True,
    ) -> Optional[ProxyInfo]:
        now = time.time()
        domain_key = self._normalize_domain_key(domain)
        with self._rotating_pick_lock:
            candidates = [
                p
                for p in self.rotating_proxies
                if (not p.is_bad)
                and (not p.is_quarantined())
                and p.in_flight < ROTATING_PROXY_MAX_IN_FLIGHT
                and ((now - p.last_bad_time) >= ROTATING_PROXY_RECENT_BAD_COOLDOWN_SEC)
            ]
            if is_https:
                candidates = [p for p in candidates if p.supports_https_connect is not False]
            if domain_key:
                candidates = [
                    p for p in candidates if not self._is_rotating_proxy_domain_quarantined(p, domain_key, now=now)
                ]
            if not candidates:
                return None
            selected = min(candidates, key=lambda p: (p.in_flight, p.fail_count, p.last_used or 0.0))
            selected.last_used = now
            if acquire_lease:
                selected.in_flight += 1
                selected.last_leased_ts = now
                selected.lease_until = now + max(15.0, float(ROTATING_PROXY_LEASE_TIMEOUT_SEC))
            return selected

    def maybe_rotate_proxy(self, proxy_info: Optional[ProxyInfo], *, domain: str = "", url: str = "") -> bool:
        if (
            not ROTATING_CHANGEIP_BEFORE_REQUEST
            or not proxy_info
            or proxy_info.proxy_type != "rotating"
            or not proxy_info.rotate_url
        ):
            return False

        with proxy_info.rotate_lock:
            now = time.time()
            wait_left = ROTATING_CHANGEIP_MIN_INTERVAL_SEC - (now - proxy_info.last_rotate_ts)
            if wait_left > 0:
                if ROTATING_CHANGEIP_ENFORCE_INTERVAL:
                    log_event(
                        log,
                        "proxy.rotating.rotate.wait_interval",
                        level="info",
                        domain=domain or None,
                        url=url or None,
                        proxy=self._mask_proxy_uri(proxy_info.proxy_uri),
                        wait_left_sec=round(wait_left, 2),
                    )
                    time.sleep(wait_left)
                else:
                    log_event(
                        log,
                        "proxy.rotating.rotate.skip_interval",
                        level="debug",
                        domain=domain or None,
                        url=url or None,
                        proxy=self._mask_proxy_uri(proxy_info.proxy_uri),
                        wait_left_sec=round(wait_left, 2),
                    )
                    return False

            for attempt in range(ROTATING_CHANGEIP_MAX_ATTEMPTS):
                session = requests.Session()
                session.trust_env = False
                try:
                    response = session.get(
                        proxy_info.rotate_url,
                        timeout=(
                            max(1.0, ROTATING_CHANGEIP_CONNECT_TIMEOUT_SEC),
                            max(1.0, ROTATING_CHANGEIP_TIMEOUT_SEC),
                        ),
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    if 200 <= response.status_code < 400:
                        proxy_info.last_rotate_ts = time.time()
                        log_event(
                            log,
                            "proxy.rotating.rotate.ok",
                            level="info",
                            domain=domain or None,
                            url=url or None,
                            proxy=self._mask_proxy_uri(proxy_info.proxy_uri),
                            attempt=f"{attempt + 1}/{ROTATING_CHANGEIP_MAX_ATTEMPTS}",
                            status_code=response.status_code,
                        )
                        if ROTATING_CHANGEIP_WAIT_SEC > 0:
                            time.sleep(ROTATING_CHANGEIP_WAIT_SEC)
                        return True

                    if attempt < (ROTATING_CHANGEIP_MAX_ATTEMPTS - 1):
                        time.sleep(max(0.0, ROTATING_CHANGEIP_RETRY_WAIT_SEC))
                        continue

                    log_event(
                        log,
                        "proxy.rotating.rotate.bad_status",
                        level="warning",
                        domain=domain or None,
                        url=url or None,
                        proxy=self._mask_proxy_uri(proxy_info.proxy_uri),
                        attempt=f"{attempt + 1}/{ROTATING_CHANGEIP_MAX_ATTEMPTS}",
                        status_code=response.status_code,
                    )
                    proxy_info.mark_bad(f"rotate_status_{response.status_code}")
                    if response.status_code in {403, 407, 408, 423, 425, 429} or response.status_code >= 500:
                        self._quarantine_rotating_proxy(
                            proxy_info,
                            reason=f"rotate_status_{response.status_code}",
                            error_text=f"status_code={response.status_code}",
                        )
                except Exception as e:
                    if attempt < (ROTATING_CHANGEIP_MAX_ATTEMPTS - 1):
                        log_exception(
                            log,
                            "proxy.rotating.rotate.retry",
                            e,
                            level="warning",
                            wait_sec=ROTATING_CHANGEIP_RETRY_WAIT_SEC,
                            attempt=f"{attempt + 1}/{ROTATING_CHANGEIP_MAX_ATTEMPTS}",
                            domain=domain or None,
                            url=url or None,
                            proxy=self._mask_proxy_uri(proxy_info.proxy_uri),
                        )
                        time.sleep(max(0.0, ROTATING_CHANGEIP_RETRY_WAIT_SEC))
                        continue
                    log_exception(
                        log,
                        "proxy.rotating.rotate.error",
                        e,
                        level="warning",
                        domain=domain or None,
                        url=url or None,
                        proxy=self._mask_proxy_uri(proxy_info.proxy_uri),
                        attempt=f"{attempt + 1}/{ROTATING_CHANGEIP_MAX_ATTEMPTS}",
                    )
                    proxy_info.mark_bad(f"rotate_error: {str(e)[:80]}")
                    self._quarantine_rotating_proxy(
                        proxy_info,
                        reason="rotate_error",
                        error_text=str(e),
                    )
                finally:
                    session.close()
        return False

    def _load_rotating_proxy(self) -> None:
        """загружает rotating proxy из .env"""
        rotating_uri = ROTATING_PROXY_URL or ""
        if not rotating_uri:
            log_event(log, "proxy.rotating.missing", level="warning")
            return

        if not rotating_uri.startswith(("http://", "https://")):
            rotating_uri = f"http://{rotating_uri}"
        elif ROTATING_PROXY_FORCE_HTTP and rotating_uri.startswith("https://"):
            rotating_uri = "http://" + rotating_uri[len("https://") :]
            log_event(
                log,
                "proxy.rotating.force_http",
                level="warning",
                forced_scheme="http",
            )

        self.rotating_proxy = ProxyInfo(rotating_uri, "rotating")
        log_event(log, "proxy.rotating.loaded", level="info", proxy=rotating_uri)

    def _is_hard_domain(self, url: str) -> bool:
        domain = domain_of(url)
        if domain in HARD_DOMAINS:
            return True
        if domain.startswith("ebay."):
            return True
        for hard in HARD_DOMAINS:
            if domain.endswith(hard):
                return True
        return False

    def _is_strict_domain(self, url: str) -> bool:
        domain = domain_of(url)
        if domain in STRICT_DOMAINS:
            return True
        if domain.startswith("ebay."):
            return True
        for s in STRICT_DOMAINS:
            if domain.endswith(s):
                return True
        return False

    def _is_direct_first_domain(self, url: str) -> bool:
        domain = domain_of(url)
        if not domain:
            return False
        for item in DIRECT_FIRST_DOMAINS:
            if domain == item or domain.endswith(f".{item}"):
                return True
        return False

    def _is_rotating_only_domain(self, url: str) -> bool:
        domain = domain_of(url)
        if not domain:
            return False
        if domain == "ebay" or domain.startswith("ebay."):
            return True
        for item in ROTATING_ONLY_DOMAINS:
            if domain == item or domain.endswith(f".{item}"):
                return True
        return False

    def get_proxy_for_url(self, url: str, retry_count: int = 0, acquire_lease: bool = True) -> Optional[ProxyInfo]:
        """подбирает подходящий прокси под URL и номер попытки"""
        self._refresh_bad_proxies()
        domain = domain_of(url)
        is_strict = self._is_strict_domain(url)
        is_hard = self._is_hard_domain(url)
        is_https = url.startswith("https://")

        # для rotating-only доменов всегда пробуем rotating и не уходим в static
        if self._is_rotating_only_domain(url):
            picked = self._pick_rotating_proxy(is_https=is_https, domain=domain, acquire_lease=acquire_lease)
            if picked:
                return picked
            return self.direct_proxy

        # для капризных сайтов сначала пробуем direct, затем прокси
        if self._is_direct_first_domain(url):
            if retry_count == 0:
                return self.direct_proxy
            if retry_count == 1:
                picked = self._pick_rotating_proxy(is_https=is_https, domain=domain, acquire_lease=acquire_lease)
                if picked:
                    return picked
            if retry_count == 2:
                candidates = [p for p in self.static_proxies if not p.is_bad]
                if is_https:
                    candidates = [p for p in candidates if p.supports_https_connect is not False]
                if candidates:
                    return random.choice(candidates)
            return self.direct_proxy

        if is_strict:
            if retry_count == 0:
                picked = self._pick_rotating_proxy(is_https=is_https, domain=domain, acquire_lease=acquire_lease)
                if picked:
                    return picked
                candidates = [p for p in self.static_proxies if not p.is_bad]
                if is_https:
                    candidates = [p for p in candidates if p.supports_https_connect is not False]
                if candidates:
                    return random.choice(candidates)
                return self.direct_proxy
            if retry_count == 1:
                candidates = [p for p in self.static_proxies if not p.is_bad]
                if is_https:
                    candidates = [p for p in candidates if p.supports_https_connect is not False]
                if candidates:
                    return random.choice(candidates)
            return self.direct_proxy

        if is_hard:
            if retry_count == 0:
                picked = self._pick_rotating_proxy(is_https=is_https, domain=domain, acquire_lease=acquire_lease)
                if picked:
                    return picked
            if retry_count == 1:
                candidates = [p for p in self.static_proxies if not p.is_bad]
                if is_https:
                    candidates = [p for p in candidates if p.supports_https_connect is not False]
                if candidates:
                    return random.choice(candidates)
            return self.direct_proxy

        # простые домены
        if retry_count == 0:
            candidates = [p for p in self.static_proxies if not p.is_bad]
            if is_https:
                candidates = [p for p in candidates if p.supports_https_connect is not False]
            if candidates:
                return random.choice(candidates)
        return self.direct_proxy

    def get_proxy_dict_for_requests(self, url: str, retry_count: int = 0) -> Optional[Dict[str, str]]:
        proxy_info = self.get_proxy_for_url(url, retry_count, acquire_lease=False)
        if proxy_info:
            proxy_dict = proxy_info.to_requests_dict()
            if proxy_dict:
                return proxy_dict
        return None

    def get_proxy_dict_for_playwright(self, url: str, retry_count: int = 0) -> Optional[Dict]:
        proxy_info = self.get_proxy_for_url(url, retry_count, acquire_lease=False)
        if proxy_info and proxy_info.proxy_type != "direct":
            return proxy_info.to_playwright_dict()
        return None

    def mark_proxy_good(self, url: str, proxy_info: Optional[ProxyInfo]) -> None:
        if not proxy_info or proxy_info.proxy_type == "direct":
            return
        proxy_info.mark_good()
        if proxy_info.proxy_type == "rotating":
            self._clear_rotating_proxy_domain_health(proxy_info, domain=domain_of(url))

    def mark_proxy_bad(
        self,
        url: str,
        retry_count: int,
        error: str,
        transport: str = "requests",
        proxy_info: Optional[ProxyInfo] = None,
    ) -> None:
        if proxy_info is None:
            log_event(
                log,
                "proxy.mark_bad.skip_no_proxy",
                level="warning",
                domain=domain_of(url),
                transport=transport,
                attempt=retry_count + 1,
            )
            return
        if not proxy_info or proxy_info.proxy_type == "direct":
            return

        domain = domain_of(url)
        proxy_uri_masked = self._mask_proxy_uri(proxy_info.proxy_uri)
        log_event(
            log,
            'proxy.mark_bad',
            level='warning',
            domain=domain,
            proxy_type=proxy_info.proxy_type,
            transport=transport,
            attempt=retry_count + 1,
            fail_count=proxy_info.fail_count,
            proxy=proxy_uri_masked,
            error=(error[:120] if isinstance(error, str) else str(error)),
        )
        error_lower = (error or "").lower()
        domain_fail_streak = 0
        if proxy_info.proxy_type == "rotating":
            domain_fail_streak = self._mark_rotating_proxy_domain_failure(proxy_info, domain=domain)
        if any(token in error_lower for token in _SOFT_BLOCK_TOKENS):
            proxy_info.last_error = error
            proxy_info.last_bad_time = time.time()
            self._quarantine_rotating_proxy(
                proxy_info,
                reason="soft_block",
                error_text=error,
            )
            if proxy_info.proxy_type == "rotating":
                self._register_rotating_soft_block_for_domain(domain)
                self._quarantine_rotating_proxy_for_domain(
                    proxy_info,
                    domain=domain,
                    reason="soft_block",
                    error_text=error,
                )
            self.stats["proxy_soft_blocked"] += 1
            log_event(
                log,
                "proxy.mark_soft_block",
                level="info",
                domain=domain,
                proxy_type=proxy_info.proxy_type,
                transport=transport,
                proxy=proxy_uri_masked,
            )
            return

        force_bad = False
        if any(token in error_lower for token in ["407", "proxy_auth", "tunnel", "err_tunnel", "connection refused"]):
            proxy_info.supports_https_connect = False
        if proxy_info.proxy_type == "rotating":
            critical_tokens = [
                "407",
                "proxy_auth",
                "authentication",
                "err_tunnel",
                "tunnel connection",
                "connection refused",
            ]
            if any(token in error_lower for token in critical_tokens):
                force_bad = True

        proxy_info.mark_bad(error)
        if force_bad:
            proxy_info.is_bad = True
        if proxy_info.proxy_type == "rotating":
            hard_block_hit = any(token in error_lower for token in self._hard_block_tokens())
            network_error_hit = any(token in error_lower for token in self._network_error_tokens())
            if hard_block_hit:
                self._quarantine_rotating_proxy(
                    proxy_info,
                    reason="request_error",
                    error_text=error,
                )
                self._register_rotating_soft_block_for_domain(domain)
                self._quarantine_rotating_proxy_for_domain(
                    proxy_info,
                    domain=domain,
                    reason="hard_block",
                    error_text=error,
                )
            elif network_error_hit and domain_fail_streak >= ROTATING_PROXY_NETWORK_ERROR_DOMAIN_THRESHOLD:
                self._quarantine_rotating_proxy_for_domain(
                    proxy_info,
                    domain=domain,
                    reason="network_pressure",
                    error_text=error,
                    seconds=max(60, ROTATING_PROXY_DOMAIN_QUARANTINE_SEC // 2),
                )
                log_event(
                    log,
                    "proxy.rotating.domain_network_pressure",
                    level="warning",
                    domain=domain,
                    proxy=proxy_uri_masked,
                    fail_streak=domain_fail_streak,
                    threshold=ROTATING_PROXY_NETWORK_ERROR_DOMAIN_THRESHOLD,
                )
        if proxy_info.proxy_type == "rotating" and proxy_info.is_bad:
            # cooldown логика обрабатывается при следующем выборе
            pass

        self.stats["proxy_marked_bad"] += 1

    def get_stats(self) -> Dict:
        now = time.time()
        with self._domain_soft_block_lock:
            active_force_domains = [
                domain
                for domain, until in self._domain_force_zenrows_until.items()
                if float(until) > now
            ]
            active_domain_quarantines = [
                key for key, until in self._rotating_domain_quarantine_until.items() if float(until) > now
            ]
        return {
            "static_proxies_total": len(self.static_proxies),
            "static_proxies_bad": sum(1 for p in self.static_proxies if p.is_bad),
            "rotating_proxies_total": len(self.rotating_proxies),
            "rotating_proxies_bad": sum(1 for p in self.rotating_proxies if p.is_bad),
            "rotating_proxies_quarantined": sum(1 for p in self.rotating_proxies if p.is_quarantined()),
            "rotating_proxies_in_flight": sum(p.in_flight for p in self.rotating_proxies),
            "rotating_proxy_available": any(
                (not p.is_bad) and (not p.is_quarantined()) and p.in_flight < ROTATING_PROXY_MAX_IN_FLIGHT
                for p in self.rotating_proxies
            ),
            "rotating_domain_quarantines_active": len(active_domain_quarantines),
            "force_zenrows_domains_active": active_force_domains,
            "stats": dict(self.stats),
        }

_proxy_manager: Optional[ProxyManager] = None
_proxy_manager_lock = threading.Lock()


def get_proxy_manager() -> ProxyManager:
    global _proxy_manager
    if _proxy_manager is None:
        with _proxy_manager_lock:
            if _proxy_manager is None:
                _proxy_manager = ProxyManager()
    return _proxy_manager


def get_proxy(url: str) -> Optional[Dict[str, str]]:
    """обратная совместимость: прокси для requests"""
    manager = get_proxy_manager()
    return manager.get_proxy_dict_for_requests(url, retry_count=0)


def get_proxy_string(url: str) -> Optional[str]:
    """обратная совместимость: строка прокси для Playwright"""
    manager = get_proxy_manager()
    proxy_dict = manager.get_proxy_dict_for_playwright(url, retry_count=0)
    if not proxy_dict:
        return None

    server = proxy_dict.get("server", "")
    username = proxy_dict.get("username", "")
    password = proxy_dict.get("password", "")
    if username and password:
        return f"http://{username}:{password}@{server.replace('http://', '')}"
    return server

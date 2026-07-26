"""
простой структурированный логгер для консоли
цели:
- единый формат логов (успехи/ошибки/ретраи) для прокси, транспорта и парсеров
- корреляция событий через trace_id (contextvars)
- минимум зависимостей (только stdlib)"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import json
import logging
import os
import re
import sys
import threading
from typing import Any, Dict, Iterator, Mapping, Optional

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
_CTX_URL: contextvars.ContextVar[str | None] = contextvars.ContextVar("ctx_url", default=None)
_CTX_DOMAIN: contextvars.ContextVar[str | None] = contextvars.ContextVar("ctx_domain", default=None)

_INITIALIZED = False
_MOJIBAKE_RE = re.compile(
    r"(?:\u0440\u045f|\u0432\u045a|\u0432\u0459|\u0432\u2020|\u043f\u0451\u040f)"
)


class _TeeTextIO:
    """дублирует вывод в консоль и файл"""

    def __init__(self, primary, mirror, lock: threading.Lock):
        self._primary = primary
        self._mirror = mirror
        self._lock = lock

    @property
    def encoding(self):  # pragma: no cover - прокси-свойство
        return getattr(self._primary, "encoding", "utf-8")

    @property
    def errors(self):  # pragma: no cover - прокси-свойство
        return getattr(self._primary, "errors", "replace")

    def isatty(self):  # pragma: no cover - прокси-метод
        meth = getattr(self._primary, "isatty", None)
        return meth() if callable(meth) else False

    def fileno(self):  # pragma: no cover - прокси-метод
        meth = getattr(self._primary, "fileno", None)
        if callable(meth):
            return meth()
        raise OSError("fileno is not available")

    def write(self, data):
        text = data if isinstance(data, str) else str(data)
        with self._lock:
            written = self._primary.write(text)
            try:
                self._mirror.write(text)
            except Exception:
                pass
        return written if isinstance(written, int) else len(text)

    def flush(self):
        with self._lock:
            try:
                self._primary.flush()
            except Exception:
                pass
            try:
                self._mirror.flush()
            except Exception:
                pass

    def reconfigure(self, **kwargs):  # pragma: no cover - прокси-метод
        primary_reconfigure = getattr(self._primary, "reconfigure", None)
        if callable(primary_reconfigure):
            primary_reconfigure(**kwargs)
        mirror_reconfigure = getattr(self._mirror, "reconfigure", None)
        if callable(mirror_reconfigure):
            mirror_reconfigure(**kwargs)
_MOJIBAKE_BAD_CHARS = set(
    "\u0403\u0453\u201a\u2026\u2020\u2021\u20ac\u2030\u0409\u2039\u040a\u040b\u040f\u0452\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u2122\u0459\u203a\u045a\u045b\u045f\u040e\u045e\u0408\xa4\u0490\xa6\xa7\u0401\xa9\u0404\xab\xac\xae\u0407\xb0\xb1\u0406\u0456\u0491\xb5\xb6\xb7\u0451\u2116\u0454\xbb\u0458\u0405\u0455\u0457"
)


def _repair_mojibake_text(value: str) -> str:
    if not value:
        return value
    src = str(value)
    has_bad = any(ch in _MOJIBAKE_BAD_CHARS for ch in src)
    has_markers = bool(_MOJIBAKE_RE.search(src)) or ("�" in src)
    if not has_bad and not has_markers:
        return src

    candidates = [src]
    for enc in ("cp1251", "cp866", "latin1"):
        try:
            candidates.append(src.encode(enc).decode("utf-8"))
        except Exception:
            continue

    def score(text: str) -> int:
        if not text:
            return 0
        bad_chars = sum(1 for ch in text if ch in _MOJIBAKE_BAD_CHARS)
        bad_pairs = 0
        for i in range(len(text) - 1):
            if text[i] in {"Р", "С"} and text[i + 1] in _MOJIBAKE_BAD_CHARS:
                bad_pairs += 1
        return (
            len(_MOJIBAKE_RE.findall(text))
            + bad_chars * 3
            + bad_pairs * 4
            + text.count("�") * 6
        )

    best = src
    best_score = score(src)
    for candidate in candidates[1:]:
        candidate_score = score(candidate)
        if candidate_score < best_score:
            best = candidate
            best_score = candidate_score
    return best


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}


def _mask_url(url: str | None, max_len: int = 140) -> str | None:
    if not url:
        return None
    s = str(url).strip()
    if not s:
        return None
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "..."


def mask_proxy_url(proxy_url: str | None) -> str | None:
    """маскирует креды в proxy url, оставляя только хост:порт"""
    if not proxy_url:
        return None
    s = str(proxy_url).strip()
    if not s:
        return None

    # примеры:
    # пример: http://user:pass@1.2.3.4:8080
    # пример: user:pass@1.2.3.4:8080
    if "@" in s:
        return s.split("@", 1)[-1]

    # уже без кредов
    if s.startswith("http://") or s.startswith("https://"):
        return s.split("//", 1)[-1]

    return s


@contextlib.contextmanager
def log_context(
    *,
    trace_id: str | None = None,
    url: str | None = None,
    domain: str | None = None,
) -> Iterator[None]:
    """контекст для сквозной корреляции логов"""
    tokens = []
    try:
        if trace_id is not None:
            tokens.append((_TRACE_ID, _TRACE_ID.set(str(trace_id))))
        if url is not None:
            tokens.append((_CTX_URL, _CTX_URL.set(str(url))))
        if domain is not None:
            tokens.append((_CTX_DOMAIN, _CTX_DOMAIN.set(str(domain))))
        yield
    finally:
        for var, tok in reversed(tokens):
            try:
                var.reset(tok)
            except Exception:
                pass


def set_context_values(*, trace_id: str | None = None, url: str | None = None, domain: str | None = None) -> None:
    """устанавливает значения контекста без contextmanager
    удобно, когда нужно выставить поля один раз до обработки
"""
    if trace_id is not None:
        tid = str(trace_id).strip() if trace_id is not None else ''
        _TRACE_ID.set(tid or None)
    if url is not None:
        u = str(url).strip()
        _CTX_URL.set(u or None)
    if domain is not None:
        d = str(domain).strip()
        _CTX_DOMAIN.set(d or None)


def clear_context() -> None:
    """сбрасывает trace/url/domain в None"""
    _TRACE_ID.set(None)
    _CTX_URL.set(None)
    _CTX_DOMAIN.set(None)


def get_trace_id() -> str | None:
    return _TRACE_ID.get()


def ensure_trace_id(prefix: str = "t") -> str:
    """гарантирует наличие trace_id в текущем контексте"""
    cur = _TRACE_ID.get()
    if cur:
        return cur

    # короткий, но достаточно уникальный id
    import uuid

    tid = f"{prefix}{uuid.uuid4().hex[:8]}"
    _TRACE_ID.set(tid)
    return tid


def _format_value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, (list, tuple)):
        # ограничиваем шум
        if len(v) > 12:
            return f"[{', '.join(_format_value(x) for x in v[:12])}, ...]"
        return f"[{', '.join(_format_value(x) for x in v)}]"
    if isinstance(v, dict):
        try:
            return json.dumps(v, ensure_ascii=True, separators=(",", ":"))
        except Exception:
            return "{...}"

    s = _repair_mojibake_text(str(v))
    s = " ".join(s.split())
    if len(s) > 160:
        s = s[:159] + "..."
    # если есть пробелы/разделители, показываем в кавычках
    if any(ch.isspace() for ch in s) or any(ch in s for ch in ["|", "="]):
        return repr(s)
    return s


class _PrettyFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        ts = _dt.datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        level = record.levelname

        name = record.name
        if name.startswith("scrapers."):
            name = name[len("scrapers.") :]
        elif name.startswith("utils."):
            name = name[len("utils.") :]
        name = name[:18]

        trace_id = getattr(record, "trace_id", None) or _TRACE_ID.get()
        ctx_url = getattr(record, "url", None) or _CTX_URL.get()
        ctx_domain = getattr(record, "domain", None) or _CTX_DOMAIN.get()

        event = getattr(record, "event", None)
        if event is not None:
            event = _repair_mojibake_text(str(event))
        fields: Dict[str, Any] = {}
        try:
            extra_fields = getattr(record, "fields", None)
            if isinstance(extra_fields, Mapping):
                fields.update(dict(extra_fields))
        except Exception:
            pass

        # добавляем контекст как поля, чтобы их всегда было видно
        if trace_id and "trace" not in fields:
            fields["trace"] = trace_id
        if ctx_domain and "domain" not in fields:
            fields["domain"] = ctx_domain
        if ctx_url and "url" not in fields:
            fields["url"] = _mask_url(ctx_url)

        msg = record.getMessage().strip() if record.getMessage() else ""
        msg = _repair_mojibake_text(msg)

        # базовая строка
        parts = [
            f"{ts}",
            f"{level:5s}",
            f"{name:18s}",
        ]
        if event:
            parts.append(str(event))
        elif msg:
            parts.append(msg)
        else:
            parts.append("event")

        if msg and event:
            parts.append(msg)

        # формат: key=value
        if fields:
            # порядок ключей: сначала самые важные
            ordered_keys = []
            for k in ["trace", "domain", "url", "transport", "proxy", "attempt", "status", "status_code"]:
                if k in fields:
                    ordered_keys.append(k)
            for k in sorted(fields.keys()):
                if k not in ordered_keys:
                    ordered_keys.append(k)

            kv = " ".join(f"{k}={_format_value(fields.get(k))}" for k in ordered_keys if fields.get(k) is not None)
            if kv:
                parts.append(kv)

        return " | ".join(parts)


class _JsonFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
        }

        event = getattr(record, "event", None)
        if event:
            payload["event"] = _repair_mojibake_text(str(event))

        msg = record.getMessage().strip() if record.getMessage() else ""
        if msg:
            payload["msg"] = _repair_mojibake_text(msg)

        trace_id = getattr(record, "trace_id", None) or _TRACE_ID.get()
        if trace_id:
            payload["trace"] = trace_id

        domain = getattr(record, "domain", None) or _CTX_DOMAIN.get()
        if domain:
            payload["domain"] = domain

        url = getattr(record, "url", None) or _CTX_URL.get()
        if url:
            payload["url"] = url

        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            payload.update(dict(fields))

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def init_logging() -> None:
    """инициализация логов (один раз)"""
    global _INITIALIZED
    if _INITIALIZED:
        return

    # на windows-консоли включаем безопасную кодировку и моментальный flush
    try:
        console_encoding = (
            os.getenv("CONSOLE_ENCODING", "").strip()
            or (sys.stdout.encoding or "").strip()
            or "utf-8"
        )
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(
                encoding=console_encoding,
                errors='replace',
                line_buffering=True,
                write_through=True,
            )
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(
                encoding=console_encoding,
                errors='replace',
                line_buffering=True,
                write_through=True,
            )
    except Exception:
        pass

    log_to_file = _env_flag("LOG_TO_FILE", "1")
    log_file_path = (os.getenv("LOG_FILE", "logs.txt") or "").strip()
    if log_to_file and log_file_path:
        try:
            log_file_dir = os.path.dirname(log_file_path)
            if log_file_dir:
                os.makedirs(log_file_dir, exist_ok=True)

            mirror = open(
                log_file_path,
                mode="a",
                encoding=console_encoding,
                errors="replace",
                buffering=1,
            )
            lock = threading.Lock()
            sys.stdout = _TeeTextIO(sys.stdout, mirror, lock)
            sys.stderr = _TeeTextIO(sys.stderr, mirror, lock)
        except Exception:
            pass

    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    if _env_flag("LOG_JSON"):
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_PrettyFormatter())

    # заменяем все handlers, чтобы не было дублей
    root.handlers = [handler]

    # глушим слишком шумные зависимости по умолчанию
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: str = "info",
    msg: str | None = None,
    **fields: Any,
) -> None:
    """единая точка для структурированных событий"""
    lvl = getattr(logging, level.upper(), logging.INFO)
    event = _repair_mojibake_text(str(event)) if event else "event"

    # убираем None, и слегка нормализуем самые частые поля
    clean_fields: Dict[str, Any] = {}
    for k, v in fields.items():
        if v is None:
            continue
        if k == "proxy":
            clean_fields[k] = mask_proxy_url(str(v))
        elif k == "url":
            clean_fields[k] = _mask_url(str(v))
        elif isinstance(v, str):
            clean_fields[k] = _repair_mojibake_text(v)
        else:
            clean_fields[k] = v

    extra = {
        "event": event,
        "fields": clean_fields,
        "trace_id": _TRACE_ID.get(),
        "url": _CTX_URL.get(),
        "domain": _CTX_DOMAIN.get(),
    }

    if msg:
        msg = _repair_mojibake_text(str(msg))
        logger.log(lvl, msg, extra=extra)
    else:
        logger.log(lvl, "", extra=extra)


def log_exception(
    logger: logging.Logger,
    event: str,
    exc: BaseException,
    *,
    level: str = "error",
    msg: str | None = None,
    **fields: Any,
) -> None:
    clean = dict(fields)
    clean["exc_type"] = type(exc).__name__
    clean["error"] = _repair_mojibake_text(str(exc)[:300]) if str(exc) else type(exc).__name__

    # stacktrace только в DEBUG, иначе будет слишком много шума
    exc_info = logger.isEnabledFor(logging.DEBUG)

    lvl = getattr(logging, level.upper(), logging.ERROR)
    extra = {
        "event": event,
        "fields": {k: v for k, v in clean.items() if v is not None},
        "trace_id": _TRACE_ID.get(),
        "url": _CTX_URL.get(),
        "domain": _CTX_DOMAIN.get(),
    }

    logger.log(lvl, _repair_mojibake_text(msg or ""), extra=extra, exc_info=exc_info)

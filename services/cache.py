"""
сервис кэширования результатов"""

import os
import json
import time
import hashlib

from config import ENABLE_CACHE, CACHE_TTL, CACHE_DIR
from utils.logger import get_logger, log_event, log_exception

log = get_logger(__name__)


def get_image_hash(image_path: str) -> str:
    # вычисляем sha256 хеш изображения для использования в качестве ключа кэша
    # используем sha256 для надежности и уникальности
    sha256_hash = hashlib.sha256()
    with open(image_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def get_cache_path(image_hash: str) -> str:
    # получаем путь к файлу кэша для данного хеша изображения
    # используем первые 2 символа хеша для создания поддиректорий (ускоряет поиск)
    cache_subdir = os.path.join(CACHE_DIR, image_hash[:2])
    os.makedirs(cache_subdir, exist_ok=True)
    return os.path.join(cache_subdir, f"{image_hash}.json")


def load_from_cache(image_hash: str) -> dict | None:
    # загружаем результат из кэша, если он существует и не истек
    if not ENABLE_CACHE:
        return None

    cache_path = get_cache_path(image_hash)

    if not os.path.exists(cache_path):
        return None

    try:
        # проверяем время модификации файла
        file_mtime = os.path.getmtime(cache_path)
        current_time = time.time()

        # если кэш истек, удаляем файл и возвращаем none
        if current_time - file_mtime > CACHE_TTL:
            os.remove(cache_path)
            return None

        # загружаем данные из кэша
        with open(cache_path, "r", encoding="utf-8") as f:
            cached_data = json.load(f)

        # добавляем флаг что данные из кэша
        cached_data["cached"] = True
        cached_data["cache_age"] = int(current_time - file_mtime)
        log_event(
            log,
            'cache.hit',
            level='info',
            image_hash=image_hash[:16],
            cache_age_sec=int(current_time - file_mtime),
        )
        return cached_data

    except Exception as e:
        # если ошибка при чтении кэша, удаляем поврежденный файл
        log_exception(log, 'cache.read_error', e, level='warning', image_hash=image_hash[:16], cache_path=cache_path)
        try:
            os.remove(cache_path)
        except Exception:
            pass
        return None


def save_to_cache(image_hash: str, data: dict) -> None:
    # сохраняем результат в кэш
    if not ENABLE_CACHE:
        return

    try:
        cache_path = get_cache_path(image_hash)

        # создаем копию данных без флага cached (если есть)
        cache_data = {k: v for k, v in data.items() if k != "cached" and k != "cache_age"}

        # сохраняем в файл
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        log_event(log, 'cache.saved', level='debug', image_hash=image_hash[:16], cache_path=cache_path)
    except Exception as e:
        # тихо игнорируем ошибки сохранения кэша (не критично)
        log_exception(log, 'cache.save_error', e, level='warning', image_hash=image_hash[:16])

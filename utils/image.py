"""
утилиты для работы с изображениями"""

import io
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests
from PIL import Image

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_THUMB_FETCH_CONNECT_TIMEOUT_SEC = float(os.getenv("THUMB_FETCH_CONNECT_TIMEOUT_SEC", "2.0"))
_THUMB_FETCH_READ_TIMEOUT_SEC = float(os.getenv("THUMB_FETCH_READ_TIMEOUT_SEC", "4.0"))
_THUMB_MAX_WORKERS = max(1, int(os.getenv("THUMB_MAX_WORKERS", "10")))

_THUMB_SESSION = requests.Session()
_THUMB_SESSION.trust_env = False
_THUMB_SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
)


def _sanitize_filename(filename: str | None) -> str:
    # очищаем имя файла от невалидных символов и вложенных путей
    base = os.path.basename(str(filename or "").strip())
    if not base:
        return "upload.jpg"

    safe = _SAFE_FILENAME_RE.sub("_", base).strip("._")
    if not safe:
        return "upload.jpg"
    if "." not in safe:
        safe = f"{safe}.jpg"
    return safe


def _resolve_upload_tmp_root() -> str:
    # разрешаем переопределить корневую папку через env
    env_dir = os.getenv("UPLOAD_TMP_DIR", "").strip()
    if env_dir:
        return env_dir
    return os.path.join(os.getcwd(), ".tmp_uploads")


def save_temp_image(file_storage) -> str:
    # сохранение принятого файла на диск во временную директорию
    tmp_root = _resolve_upload_tmp_root()
    os.makedirs(tmp_root, exist_ok=True)

    temp_dir = os.path.join(tmp_root, f"img_{uuid.uuid4().hex[:10]}")
    os.makedirs(temp_dir, exist_ok=False)

    temp_path = os.path.join(temp_dir, _sanitize_filename(getattr(file_storage, "filename", None)))
    file_storage.save(temp_path)
    return temp_path


def load_pil_image_from_path(path: str) -> Image.Image:
    # загружает изображение с диска и конвертирует в rgb
    with Image.open(path) as im:
        return im.convert("RGB")


def load_pil_image_from_url(
    url: str,
    timeout_connect: float | None = None,
    timeout_read: float | None = None,
) -> Image.Image | None:
    # быстрая загрузка миниатюр с короткими timeout
    try:
        connect_timeout = float(timeout_connect) if timeout_connect is not None else _THUMB_FETCH_CONNECT_TIMEOUT_SEC
        read_timeout = float(timeout_read) if timeout_read is not None else _THUMB_FETCH_READ_TIMEOUT_SEC

        resp = _THUMB_SESSION.get(url, timeout=(connect_timeout, read_timeout), stream=True)
        resp.raise_for_status()

        # ограничиваем размер изображения для экономии памяти (макс 5mb)
        content_length = resp.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > 5 * 1024 * 1024:
                    return None
            except Exception:
                pass

        content = b""
        for chunk in resp.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > 5 * 1024 * 1024:
                return None
        return Image.open(io.BytesIO(content)).convert("RGB")
    except Exception:
        return None


def load_images_parallel(
    urls: list[str],
    max_workers: int | None = None,
    timeout_connect: float | None = None,
    timeout_read: float | None = None,
) -> list[Image.Image | None]:
    # параллельная загрузка изображений для ускорения
    workers = max_workers if max_workers is not None else _THUMB_MAX_WORKERS
    workers = max(1, int(workers))

    def load_one(idx_url):
        idx, url = idx_url
        return idx, load_pil_image_from_url(
            url,
            timeout_connect=timeout_connect,
            timeout_read=timeout_read,
        )

    results = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        # создаем задачи только для непустых url
        futures = {executor.submit(load_one, (i, url)): i for i, url in enumerate(urls) if url}
        for future in as_completed(futures):
            try:
                idx, img = future.result()
                results[idx] = img
            except Exception:
                pass
    return results


def _resize_with_padding_gray(gray: np.ndarray, size: int = 224) -> np.ndarray:
    # сохраняем пропорции и приводим к фиксированному квадрату
    try:
        import cv2
    except Exception:
        return gray

    h, w = gray.shape[:2]
    if h <= 0 or w <= 0:
        return gray

    scale = float(size) / float(max(h, w))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = cv2.resize(gray, (new_w, new_h), interpolation=cv2.INTER_AREA)

    canvas = np.full((size, size), 255, dtype=np.uint8)
    y0 = (size - new_h) // 2
    x0 = (size - new_w) // 2
    canvas[y0:y0 + new_h, x0:x0 + new_w] = resized
    return canvas


def _largest_contour_mask(binary: np.ndarray) -> np.ndarray | None:
    try:
        import cv2
    except Exception:
        return None

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    h, w = binary.shape[:2]
    img_area = float(max(1, h * w))

    # выбираем самый крупный разумный контур, избегая полностью залитого фона
    best = None
    best_area = 0.0
    for contour in contours:
        area = float(cv2.contourArea(contour))
        area_ratio = area / img_area
        if area_ratio < 0.01:
            continue
        if area_ratio > 0.95:
            continue
        if area > best_area:
            best = contour
            best_area = area

    if best is None:
        return None

    out = np.zeros_like(binary, dtype=np.uint8)
    cv2.drawContours(out, [best], -1, 255, thickness=cv2.FILLED)
    return out


def _extract_silhouette_mask(img: Image.Image, size: int = 224) -> np.ndarray | None:
    # извлекаем маску силуэта объекта без учета цвета
    try:
        import cv2
    except Exception:
        return None

    try:
        gray = np.array(img.convert("L"))
    except Exception:
        return None
    if gray.size == 0:
        return None

    gray = _resize_with_padding_gray(gray, size=size)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)

    _, bin1 = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bin2 = cv2.bitwise_not(bin1)

    kernel = np.ones((3, 3), np.uint8)
    candidates = []
    for b in (bin1, bin2):
        closed = cv2.morphologyEx(b, cv2.MORPH_CLOSE, kernel, iterations=2)
        opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = _largest_contour_mask(opened)
        if mask is None:
            continue
        area = int(np.count_nonzero(mask))
        candidates.append((area, mask))

    if not candidates:
        return None

    # берем маску с большей информативной площадью
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _hu_vector(mask: np.ndarray) -> np.ndarray | None:
    try:
        import cv2
    except Exception:
        return None
    try:
        moments = cv2.moments(mask)
        hu = cv2.HuMoments(moments).flatten()
    except Exception:
        return None
    hu = np.where(hu == 0.0, 1e-12, hu)
    hu = -np.sign(hu) * np.log10(np.abs(hu))
    return hu.astype(np.float32)


def silhouette_shape_similarity(src_img: Image.Image, target_img: Image.Image, size: int = 224) -> float | None:
    # оценка сходства формы/силуэта двух изображений (0..1), цвет не учитывается
    src_mask = _extract_silhouette_mask(src_img, size=size)
    tgt_mask = _extract_silhouette_mask(target_img, size=size)
    if src_mask is None or tgt_mask is None:
        return None

    src_hu = _hu_vector(src_mask)
    tgt_hu = _hu_vector(tgt_mask)
    if src_hu is None or tgt_hu is None:
        return None

    hu_dist = float(np.mean(np.abs(src_hu - tgt_hu)))
    hu_sim = float(np.exp(-0.35 * hu_dist))

    inter = float(np.count_nonzero((src_mask > 0) & (tgt_mask > 0)))
    union = float(np.count_nonzero((src_mask > 0) | (tgt_mask > 0)))
    iou = (inter / union) if union > 0 else 0.0

    area_src = float(np.count_nonzero(src_mask))
    area_tgt = float(np.count_nonzero(tgt_mask))
    area_ratio = (min(area_src, area_tgt) / max(area_src, area_tgt)) if max(area_src, area_tgt) > 0 else 0.0

    score = 0.55 * hu_sim + 0.30 * iou + 0.15 * area_ratio
    return max(0.0, min(1.0, float(score)))

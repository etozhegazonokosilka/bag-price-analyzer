"""
для работы с clip моделью
включает функции для получения embeddings, сравнения изображений,
анализа цветов и perceptual hash

требует torch и open_clip - устанавливаются отдельно через requirements-ml.txt"""

import numpy as np
import imagehash
from PIL import Image

from utils.logger import get_logger, log_event, log_exception

from config import (
    CLIP_MODEL_NAME,
    CLIP_PRETRAINED,
    SIMILARITY_THRESHOLD,
    ENABLE_COLOR_CHECK,
    COLOR_SIMILARITY_THRESHOLD,
    ENABLE_PHASH_CHECK,
    PHASH_THRESHOLD,
)

log = get_logger(__name__)

# глобальные переменные для модели clip
_clip_device = None  # будет установлен при первом использовании
_clip_model = None
_clip_preprocess = None
_clip_tokenizer = None


def _ensure_torch_imported():
    """проверяет доступность torch и open_clip, импортирует их"""
    try:
        import torch
        import torch.nn.functional as F
        import open_clip
        return torch, F, open_clip
    except ImportError as e:
        raise RuntimeError(
            "Для работы CLIP функций требуются torch и open_clip. "
            "Установите зависимости: pip install -r requirements-ml.txt"
        ) from e


def _get_clip_device() -> str:
    """возвращает устройство для CLIP (cuda/cpu), определяет лениво"""
    global _clip_device
    if _clip_device is None:
        torch, _, _ = _ensure_torch_imported()
        _clip_device = "cuda" if torch.cuda.is_available() else "cpu"
    return _clip_device


def init_clip_model():
    """инициализирует модель clip для сравнения изображений"""
    global _clip_model, _clip_preprocess, _clip_tokenizer
    if _clip_model is not None:
        return

    torch, _, open_clip = _ensure_torch_imported()
    device = _get_clip_device()

    try:
        model_name = CLIP_MODEL_NAME
        log_event(log, "clip.model.load_start", level="info", model_name=model_name, device=device)
        _clip_model, _, _clip_preprocess = open_clip.create_model_and_transforms(
            model_name,
            pretrained=CLIP_PRETRAINED,
            device=device,
        )
        _clip_tokenizer = open_clip.get_tokenizer(model_name)
        _clip_model.eval()
        for param in _clip_model.parameters():
            param.requires_grad = False
        log_event(log, "clip.model.load_ok", level="info", model_name=model_name, device=device)
    except Exception as e:
        log_exception(log, "clip.model.load_error", e, level="error", model_name=model_name, device=device)
        raise RuntimeError(f"Не удалось загрузить модель openclip: {e}")


def get_clip_device() -> str:
    """возвращает устройство на котором работает clip"""
    return _get_clip_device()


def get_clip_model():
    """возвращает загруженную модель clip"""
    return _clip_model


def preprocess_image_for_clip(img: Image.Image, min_size: int = 224) -> Image.Image:
    """увеличивает маленькие изображения до минимального размера для лучшей точности clip"""
    width, height = img.size
    max_dim = max(width, height)

    if max_dim < min_size:
        scale = min_size / max_dim
        new_width = int(width * scale)
        new_height = int(height * scale)
        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

    return img


def get_dominant_colors(img: Image.Image, k: int = 3) -> list[tuple[int, int, int]]:
    """извлекает доминирующие цвета изображения"""
    img_small = img.resize((150, 150), Image.Resampling.LANCZOS)
    pixels = np.array(img_small)
    h, w = pixels.shape[0], pixels.shape[1]

    colors = []
    step_y = max(1, h // 3)
    step_x = max(1, w // 3)

    for y in range(0, h, step_y):
        for x in range(0, w, step_x):
            y_end = min(y + step_y, h)
            x_end = min(x + step_x, w)
            region = pixels[y:y_end, x:x_end]
            if region.size > 0:
                avg_color = tuple(map(int, region.reshape(-1, 3).mean(axis=0)))
                colors.append(avg_color)
                if len(colors) >= k:
                    break
        if len(colors) >= k:
            break

    if len(colors) < k:
        avg_all = tuple(map(int, pixels.reshape(-1, 3).mean(axis=0)))
        colors.extend([avg_all] * (k - len(colors)))

    return colors[:k]


def compare_colors(colors_a: list[tuple[int, int, int]], colors_b: list[tuple[int, int, int]]) -> float:
    """сравнивает доминирующие цвета двух изображений, возвращает схожесть 0..1"""
    if not colors_a or not colors_b:
        return 0.0

    min_distances = []
    for color_a in colors_a:
        min_dist = float('inf')
        for color_b in colors_b:
            dist = np.sqrt(sum((a - b) ** 2 for a, b in zip(color_a, color_b)))
            min_dist = min(min_dist, dist)
        min_distances.append(min_dist)

    max_possible_dist = 441.0
    avg_distance = sum(min_distances) / len(min_distances)
    similarity = 1.0 - (avg_distance / max_possible_dist)
    return max(0.0, min(1.0, similarity))


def get_perceptual_hash(img: Image.Image) -> imagehash.ImageHash:
    """вычисляет perceptual hash изображения"""
    return imagehash.phash(img, hash_size=16)


def compare_phash(hash_a: imagehash.ImageHash, hash_b: imagehash.ImageHash, threshold: int = 8) -> bool:
    """сравнивает perceptual hashes, возвращает true если расстояние <= threshold"""
    if hash_a is None or hash_b is None:
        return False
    distance = hash_a - hash_b
    return distance <= threshold


def get_image_embedding(img: Image.Image):
    """получает embedding изображения через clip модель"""
    torch, F, _ = _ensure_torch_imported()
    init_clip_model()
    assert _clip_model is not None and _clip_preprocess is not None

    device = _get_clip_device()
    processed = preprocess_image_for_clip(img, min_size=224)
    img_tensor = _clip_preprocess(processed).unsqueeze(0).to(device)

    with torch.no_grad():
        features = _clip_model.encode_image(img_tensor)
        features = F.normalize(features, p=2, dim=-1)

    return features


def compute_image_similarity_batch(
    src_embedding,
    target_images: list[Image.Image],
    src_colors: list[tuple[int, int, int]] | None = None,
    src_phash: imagehash.ImageHash | None = None,
) -> list[dict]:
    # батчевое вычисление схожести с многоуровневой фильтрацией
    # возвращает список словарей с детальной информацией о схожести
    # многоуровневая фильтрация для точных копий
    # - clip схожесть должна быть >= similarity_threshold (обязательно)
    # - цветовая схожесть должна быть >= color_similarity_threshold (обязательно для точных копий)
    # - phash расстояние применяет мягкий штраф, если > phash_threshold
    # если все проверки пройдены - возвращаем итоговую схожесть (с учетом штрафов)

    torch, F, _ = _ensure_torch_imported()
    init_clip_model()
    assert _clip_model is not None and _clip_preprocess is not None

    device = _get_clip_device()

    if not target_images:
        return []

    try:
        # предобрабатываем все изображения
        processed_images = [preprocess_image_for_clip(img, min_size=224) for img in target_images]

        # применяем трансформации ко всем изображениям сразу
        img_tensors = torch.stack([_clip_preprocess(img) for img in processed_images]).to(device)

        with torch.no_grad():
            # получаем embeddings для всех изображений за один проход
            features_batch = _clip_model.encode_image(img_tensors)
            features_batch = F.normalize(features_batch, p=2, dim=-1)

            # вычисляем схожесть со всеми изображениями сразу
            similarities = (src_embedding @ features_batch.t()).squeeze(0)

            # приводим к [0, 1] и конвертируем в список
            similarities_01 = ((similarities + 1.0) / 2.0).cpu().tolist()

            # применяем многоуровневую фильтрацию для точных копий
            results = []
            for i, (clip_sim, img) in enumerate(zip(similarities_01, target_images)):
                # базовая информация о схожести
                result = {
                    "clip_similarity": float(clip_sim),
                    "final_similarity": float(clip_sim),  # по умолчанию = clip схожесть
                    "passed_color_check": True,  # по умолчанию проходит
                    "passed_phash_check": True,  # по умолчанию проходит
                    "color_similarity": None,
                    "phash_distance": None,
                    "filtered": False,  # был ли товар отфильтрован
                    "filter_reason": None,  # причина фильтрации
                }

                # быстрая проверка: если clip схожесть очень низкая, сразу отфильтровываем
                if clip_sim < 0.3:
                    result["final_similarity"] = 0.0
                    result["filtered"] = True
                    result["filter_reason"] = "clip_similarity_too_low"
                    results.append(result)
                    continue

                # проверка 1: clip схожесть (основной обязательный фильтр)
                # если не проходит - товар отфильтровывается
                if clip_sim < SIMILARITY_THRESHOLD:
                    result["final_similarity"] = 0.0
                    result["filtered"] = True
                    result["filter_reason"] = f"clip_similarity_below_threshold_{SIMILARITY_THRESHOLD:.2f}"
                    results.append(result)
                    continue

                # начинаем с оригинальной clip схожести
                final_sim = float(clip_sim)

                # проверка 2: цветовая схожесть (обязательная проверка для точных копий)
                # если не проходит - товар отфильтровывается (не точная копия по цвету)
                # для точных копий цвет должен совпадать!
                if ENABLE_COLOR_CHECK and src_colors:
                    try:
                        target_colors = get_dominant_colors(img, k=3)
                        color_sim = compare_colors(src_colors, target_colors)
                        result["color_similarity"] = float(color_sim)

                        if color_sim < COLOR_SIMILARITY_THRESHOLD:
                            # товар не проходит проверку цветов - отфильтровываем (не точная копия)
                            # цветовая схожесть < порога означает, что это товар другого цвета
                            result["final_similarity"] = 0.0
                            result["passed_color_check"] = False
                            result["filtered"] = True
                            result["filter_reason"] = (
                                "color_similarity_below_threshold_"
                                f"{COLOR_SIMILARITY_THRESHOLD:.2f}_different_color"
                            )
                            results.append(result)
                            continue
                        else:
                            result["passed_color_check"] = True
                    except Exception as e:
                        # если ошибка при проверке цветов - пропускаем проверку
                        log_exception(log, "clip.color_check.error", e, level="warning")
                        result["passed_color_check"] = True  # считаем, что прошло
                        pass

                # проверка 3: perceptual hash (мягкая проверка - применяет штраф)
                # если не проходит - применяется мягкий штраф, но товар не отфильтровывается
                # phash хорошо различает даже небольшие различия между изображениями
                # для 100% схожести (точных копий) phash должен быть очень близким
                # большие различия в phash означают разные формы/композиции
                if ENABLE_PHASH_CHECK and src_phash:
                    try:
                        target_phash = get_perceptual_hash(img)
                        phash_distance = src_phash - target_phash
                        result["phash_distance"] = int(phash_distance)

                        # для точных копий phash расстояние должно быть небольшим
                        # если phash расстояние большое - это разные формы/композиции
                        if phash_distance > PHASH_THRESHOLD:
                            result["passed_phash_check"] = False
                            # для 100% схожести применяем более строгий штраф
                            # если phash расстояние очень большое (> phash_threshold * 2) - это точно другой товар
                            if phash_distance > PHASH_THRESHOLD * 2:
                                # очень большой штраф - товар явно другой формы
                                final_sim *= 0.3  # снижаем схожесть на 70%
                            else:
                                # средний штраф для товаров с похожим, но не идентичным phash
                                normalized_distance = min(1.0, phash_distance / (PHASH_THRESHOLD * 2.0))
                                final_sim *= (1.0 - 0.4 * normalized_distance)  # максимальный штраф 40%
                        else:
                            result["passed_phash_check"] = True
                    except Exception as e:
                        # если ошибка при проверке phash - пропускаем проверку
                        log_exception(log, "clip.phash_check.error", e, level="warning")
                        result["passed_phash_check"] = True  # считаем, что прошло
                        pass

                # все проверки пройдены - товар проходит с итоговой схожестью
                # итоговая схожесть может быть немного ниже clip из-за штрафов
                result["final_similarity"] = max(0.0, float(final_sim))
                result["filtered"] = False
                results.append(result)

            return results

    except Exception as e:
        log_exception(log, 'clip.similarity.batch_error', e, level='error', target_count=len(target_images))
        # возвращаем список с нулевыми результатами
        return [{
            "clip_similarity": 0.0,
            "final_similarity": 0.0,
            "passed_color_check": False,
            "passed_phash_check": False,
            "color_similarity": None,
            "phash_distance": None,
            "filtered": True,
            "filter_reason": "error",
        }] * len(target_images)


def compute_image_similarity(img_a: Image.Image, img_b: Image.Image) -> float:
    # оставляем для обратной совместимости, но используем батчевую версию внутри
    results = compute_image_similarity_batch(get_image_embedding(img_a), [img_b])
    if results and len(results) > 0:
        return results[0].get("final_similarity", 0.0)
    return 0.0

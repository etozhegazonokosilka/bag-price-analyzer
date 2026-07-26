"""
точка входа приложения
запускает flask сервер для api"""

import os
import time

from utils.logger import get_logger, init_logging, log_event, log_exception

init_logging()

from config import CLIP_PREWARM_ON_START, PORT, print_config
from api import app
from services.clip import init_clip_model

log = get_logger(__name__)


def _prewarm_clip_model() -> None:
    # прогреваем clip на старте, чтобы первый /analyze не ловил cold start
    visual_similarity_enabled = os.getenv("VISUAL_SIMILARITY_ENABLED", "0").strip().lower() in {"1", "true", "yes"}
    if not visual_similarity_enabled:
        log_event(log, "startup.clip_prewarm.skip", level="info", enabled=False, reason="visual_similarity_disabled")
        return

    if not CLIP_PREWARM_ON_START:
        log_event(log, "startup.clip_prewarm.skip", level="info", enabled=False)
        return

    started_at = time.perf_counter()
    try:
        init_clip_model()
        log_event(
            log,
            "startup.clip_prewarm.ok",
            level="info",
            enabled=True,
            elapsed_sec=round(time.perf_counter() - started_at, 2),
        )
    except Exception as e:
        log_exception(
            log,
            "startup.clip_prewarm.error",
            e,
            level="warning",
            enabled=True,
            elapsed_sec=round(time.perf_counter() - started_at, 2),
        )

if __name__ == "__main__":
    print_config()
    _prewarm_clip_model()
    app.run(host="127.0.0.1", port=PORT)

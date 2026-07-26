"""
сервис очереди для фоновой генерации html-отчетов"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

try:
    from redis import Redis
    from rq import Queue, Worker
    from rq.exceptions import NoSuchJobError
    from rq.job import Job
    _RQ_AVAILABLE = True
except Exception:
    Redis = Any  # type: ignore[assignment]
    Queue = Any  # type: ignore[assignment]
    Worker = Any  # type: ignore[assignment]
    Job = Any  # type: ignore[assignment]

    class NoSuchJobError(Exception):
        # заглушка исключения при отсутствии rq
        pass

    _RQ_AVAILABLE = False

from config import (
    REPORT_QUEUE_ENABLED,
    REPORT_QUEUE_JOB_TIMEOUT_SEC,
    REPORT_QUEUE_NAME,
    REPORT_QUEUE_REDIS_URL,
    REPORT_QUEUE_RESULT_TTL_SEC,
)
from utils.logger import get_logger, log_event, log_exception
from utils.report import get_results_dir, save_html_report

log = get_logger(__name__)


def _get_redis_connection() -> Redis:
    # создает подключение к redis для очереди отчетов
    if not _RQ_AVAILABLE:
        raise RuntimeError("модули redis/rq не установлены")
    return Redis.from_url(REPORT_QUEUE_REDIS_URL)


def _result_json_path(project_root: str, artifact_ts: str) -> str:
    # возвращает путь к json-слепку результата
    results_dir = get_results_dir(project_root)
    return os.path.join(results_dir, f"result_{artifact_ts}.json")


def _update_result_snapshot(project_root: str, artifact_ts: str, patch_payload: dict) -> None:
    # дописывает поля отчета в json-слепок результата
    result_path = _result_json_path(project_root, artifact_ts)
    if not os.path.exists(result_path):
        return

    try:
        with open(result_path, "r", encoding="utf-8") as file_obj:
            snapshot = json.load(file_obj)
    except Exception as error:
        log_exception(
            log,
            "report_queue.snapshot.read_error",
            error,
            level="warning",
            result_path=result_path,
        )
        return

    snapshot.update(patch_payload or {})

    try:
        with open(result_path, "w", encoding="utf-8") as file_obj:
            json.dump(snapshot, file_obj, ensure_ascii=False, indent=2)
    except Exception as error:
        log_exception(
            log,
            "report_queue.snapshot.write_error",
            error,
            level="warning",
            result_path=result_path,
        )


def _normalize_task_status(raw_status: str | None) -> str:
    # приводит статус rq к api-статусу
    status = str(raw_status or "").strip().lower()
    if status in {"queued", "scheduled", "deferred"}:
        return "queued"
    if status in {"started"}:
        return "running"
    if status in {"finished"}:
        return "ready"
    if status in {"failed", "stopped", "canceled"}:
        return "failed"
    return "unknown"


def _extract_failed_job_error(job: Job) -> str:
    # извлекает краткую ошибку из failed-задачи
    exc_info = str(getattr(job, "exc_info", "") or "").strip()
    if not exc_info:
        return "задача завершилась с ошибкой"

    lines = [line.strip() for line in exc_info.splitlines() if line.strip()]
    if not lines:
        return "задача завершилась с ошибкой"
    return lines[-1][:400]


def _build_report_url(report_filename: str | None) -> str | None:
    # строит относительный url отчета по имени файла
    safe_name = os.path.basename(str(report_filename or "").strip())
    if not safe_name:
        return None
    return f"/results/{safe_name}"


def _build_expected_html_report_filename(job: Job) -> str | None:
    # формирует ожидаемое имя html-отчета по параметрам задачи
    kwargs = getattr(job, "kwargs", {}) or {}
    artifact_ts = str(kwargs.get("artifact_ts") or "").strip()
    if not artifact_ts:
        return None
    return f"report_{artifact_ts}.html"


def _build_expected_report_file(job: Job, report_filename: str | None) -> str | None:
    # формирует ожидаемый путь к html-отчету по параметрам задачи
    if not report_filename:
        return None

    kwargs = getattr(job, "kwargs", {}) or {}
    project_root = str(kwargs.get("project_root") or "").strip()
    if not project_root:
        return None
    return os.path.join(get_results_dir(project_root), report_filename)


def generate_html_report_job(
    payload: dict,
    artifact_ts: str,
    generated_at_iso: str,
    project_root: str,
) -> dict:
    # генерирует html-отчет в фоне и обновляет json-слепок
    generated_at = datetime.now()
    if generated_at_iso:
        try:
            generated_at = datetime.fromisoformat(generated_at_iso)
        except Exception:
            pass

    results_dir = get_results_dir(project_root)

    try:
        report_path, report_filename = save_html_report(
            payload,
            results_dir=results_dir,
            timestamp=artifact_ts,
            generated_at=generated_at,
        )
        result = {
            "report_file": report_path,
            "report_filename": report_filename,
            "report_format": "html",
            "report_ready": True,
            "report_status": "ready",
            "report_error": None,
        }
        _update_result_snapshot(project_root, artifact_ts, result)
        log_event(
            log,
            "report_queue.job.ready",
            level="info",
            artifact_ts=artifact_ts,
            report_filename=report_filename,
        )
        return result
    except Exception as error:
        patch_payload = {
            "report_ready": False,
            "report_status": "failed",
            "report_error": str(error),
        }
        _update_result_snapshot(project_root, artifact_ts, patch_payload)
        log_exception(
            log,
            "report_queue.job.failed",
            error,
            level="error",
            artifact_ts=artifact_ts,
        )
        raise


def enqueue_report_generation(
    payload: dict,
    artifact_ts: str,
    generated_at: datetime,
    project_root: str,
) -> dict:
    # ставит задачу генерации html в очередь
    if not REPORT_QUEUE_ENABLED:
        return {
            "enabled": False,
            "task_id": None,
            "status": "disabled",
            "error": "очередь отчетов выключена",
        }
    if not _RQ_AVAILABLE:
        return {
            "enabled": True,
            "task_id": None,
            "status": "failed",
            "error": "модули redis/rq не установлены",
        }

    try:
        redis_connection = _get_redis_connection()
        redis_connection.ping()
        queue = Queue(
            REPORT_QUEUE_NAME,
            connection=redis_connection,
            default_timeout=REPORT_QUEUE_JOB_TIMEOUT_SEC,
        )
        job = queue.enqueue(
            generate_html_report_job,
            payload=payload,
            artifact_ts=artifact_ts,
            generated_at_iso=generated_at.isoformat(),
            project_root=project_root,
            result_ttl=REPORT_QUEUE_RESULT_TTL_SEC,
            failure_ttl=REPORT_QUEUE_RESULT_TTL_SEC,
            job_timeout=REPORT_QUEUE_JOB_TIMEOUT_SEC,
        )
        log_event(
            log,
            "report_queue.enqueue.ok",
            level="info",
            queue_name=REPORT_QUEUE_NAME,
            task_id=job.get_id(),
            artifact_ts=artifact_ts,
        )
        return {
            "enabled": True,
            "task_id": job.get_id(),
            "status": "queued",
            "error": None,
        }
    except Exception as error:
        log_exception(
            log,
            "report_queue.enqueue.error",
            error,
            level="error",
            queue_name=REPORT_QUEUE_NAME,
            artifact_ts=artifact_ts,
        )
        return {
            "enabled": True,
            "task_id": None,
            "status": "failed",
            "error": str(error),
        }


def get_report_task_status(task_id: str) -> dict:
    # возвращает текущий статус задачи отчета
    safe_task_id = str(task_id or "").strip()
    if not safe_task_id:
        return {
            "task_id": None,
            "status": "not_found",
            "report_ready": False,
            "report_file": None,
            "report_filename": None,
            "report_format": "html",
            "report_url": None,
            "report_file_url": None,
            "report_error": "пустой task_id",
        }

    if not REPORT_QUEUE_ENABLED:
        return {
            "task_id": safe_task_id,
            "status": "disabled",
            "report_ready": False,
            "report_file": None,
            "report_filename": None,
            "report_format": "html",
            "report_url": None,
            "report_file_url": None,
            "report_error": "очередь отчетов выключена",
        }
    if not _RQ_AVAILABLE:
        return {
            "task_id": safe_task_id,
            "status": "error",
            "report_ready": False,
            "report_file": None,
            "report_filename": None,
            "report_format": "html",
            "report_url": None,
            "report_file_url": None,
            "report_error": "модули redis/rq не установлены",
        }

    try:
        redis_connection = _get_redis_connection()
        job = Job.fetch(safe_task_id, connection=redis_connection)
        raw_status = job.get_status(refresh=True)
        normalized_status = _normalize_task_status(raw_status)
        report_filename = _build_expected_html_report_filename(job)
        report_file = _build_expected_report_file(job, report_filename)
        report_url = _build_report_url(report_filename)

        payload = {
            "task_id": safe_task_id,
            "status": normalized_status,
            "rq_status": raw_status,
            "report_ready": normalized_status == "ready",
            "report_file": report_file,
            "report_filename": report_filename,
            "report_format": "html",
            "report_url": report_url,
            "report_file_url": report_url,
            "report_error": None,
        }

        if normalized_status == "ready":
            result = job.result if isinstance(job.result, dict) else {}
            if payload["report_filename"] is None:
                payload["report_filename"] = result.get("report_filename")
            if payload["report_file"] is None:
                payload["report_file"] = result.get("report_file")
            payload["report_url"] = _build_report_url(payload.get("report_filename"))
            payload["report_file_url"] = payload["report_url"]
            payload["report_format"] = "html"
            payload["report_ready"] = bool(result.get("report_ready", True))
            payload["report_error"] = result.get("report_error")
        elif normalized_status == "failed":
            payload["report_error"] = _extract_failed_job_error(job)

        return payload
    except NoSuchJobError:
        return {
            "task_id": safe_task_id,
            "status": "not_found",
            "report_ready": False,
            "report_file": None,
            "report_filename": None,
            "report_format": "html",
            "report_url": None,
            "report_file_url": None,
            "report_error": "задача не найдена",
        }
    except Exception as error:
        log_exception(
            log,
            "report_queue.status.error",
            error,
            level="error",
            task_id=safe_task_id,
        )
        return {
            "task_id": safe_task_id,
            "status": "error",
            "report_ready": False,
            "report_file": None,
            "report_filename": None,
            "report_format": "html",
            "report_url": None,
            "report_file_url": None,
            "report_error": str(error),
        }


def run_report_worker() -> None:
    # запускает rq-воркер очереди отчетов
    if not REPORT_QUEUE_ENABLED:
        log_event(
            log,
            "report_queue.worker.skip",
            level="warning",
            reason="queue_disabled",
            queue_name=REPORT_QUEUE_NAME,
        )
        return
    if not _RQ_AVAILABLE:
        raise RuntimeError("модули redis/rq не установлены")

    redis_connection = _get_redis_connection()
    redis_connection.ping()
    worker = Worker([REPORT_QUEUE_NAME], connection=redis_connection)
    log_event(log, "report_queue.worker.start", level="info", queue_name=REPORT_QUEUE_NAME)
    worker.work(with_scheduler=False)

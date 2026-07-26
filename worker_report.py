"""
точка входа воркера для генерации pdf-отчетов"""

from utils.logger import get_logger, init_logging, log_event, log_exception

init_logging()

from services.report_queue import run_report_worker

log = get_logger(__name__)

if __name__ == "__main__":
    try:
        log_event(log, "report_worker.start", level="info")
        run_report_worker()
    except Exception as error:
        log_exception(log, "report_worker.crash", error, level="error")
        raise

"""
Фоновый процесс, который раз в сутки запускает синхронизацию данных из WB API.
Это отдельный процесс (Railway-сервис), не связанный напрямую с дашбордом —
дашборд просто читает то, что этот процесс положил в базу.
"""
import logging
from apscheduler.schedulers.blocking import BlockingScheduler

from collector import sync
from ads_collector import sync_ads

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scheduler")


def job():
    log.info("Запуск плановой синхронизации с WB API")
    try:
        new_rows = sync()
        log.info("Синхронизация финансов завершена успешно, новых строк: %d", new_rows)
    except Exception:
        log.exception("Ошибка во время синхронизации финансового отчёта")

    try:
        ad_rows = sync_ads()
        log.info("Синхронизация рекламы завершена успешно, записей: %d", ad_rows)
    except Exception:
        log.exception("Ошибка во время синхронизации статистики рекламы")


if __name__ == "__main__":
    # Сразу синхронизируем при старте, чтобы не ждать первого срабатывания по расписанию
    job()

    scheduler = BlockingScheduler(timezone="Europe/Moscow")
    # Каждый день в 6:00 по Москве — финансовые данные WB обычно
    # обновляются по ночам, к утру свежие данные за прошлый день уже доступны
    scheduler.add_job(job, "cron", hour=6, minute=0)
    log.info("Планировщик запущен. Синхронизация — каждый день в 06:00 (Europe/Moscow).")
    scheduler.start()

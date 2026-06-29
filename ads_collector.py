"""
Сборщик расходов на рекламу из Wildberries API (раздел "Продвижение").

Логика в три шага:
1. Получаем список ID активных/завершённых рекламных кампаний.
2. Для каждой кампании запрашиваем подробную статистику за период
   (метод fullstats) — там расходы разбиты по дням и артикулам.
3. Сохраняем расход на рекламу по дням и nm_id (артикул WB) в таблицу AdSpend.

Эти данные потом связываются в дашборде с собственными артикулами продавца
(sa_name) через таблицу sale_records, где есть соответствие nm_id <-> sa_name.

Документация: https://dev.wildberries.ru/en/docs/openapi/promotion

⚠️ Метод POST /adv/v2/fullstats считается устаревшим и будет отключён.
Используем актуальный POST /api/advert/v3/fullstats.
Лимит: 3 запроса в минуту, максимум 31 день в одном запросе, максимум
50 ID кампаний в одном запросе.
"""
import os
import time
import logging
from datetime import datetime, date, timedelta

import requests
from dotenv import load_dotenv

from database import init_db, get_session, AdSpend

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ads_collector")

WB_API_TOKEN = os.environ.get("WB_API_TOKEN")

ADV_BASE_URL = "https://advert-api.wildberries.ru"
CAMPAIGNS_COUNT_URL = f"{ADV_BASE_URL}/adv/v1/promotion/count"
FULLSTATS_URL = f"{ADV_BASE_URL}/api/advert/v3/fullstats"

# WB разрешает 3 запроса в минуту к fullstats — берём паузу с запасом.
REQUEST_INTERVAL_SECONDS = 21
MAX_PERIOD_DAYS = 31  # ограничение самого API
MAX_IDS_PER_REQUEST = 50


def _headers():
    if not WB_API_TOKEN:
        raise RuntimeError(
            "Не задан WB_API_TOKEN. Проверь файл .env или переменные окружения."
        )
    return {"Authorization": WB_API_TOKEN}


def get_all_campaign_ids() -> list[int]:
    """
    Возвращает ID всех кампаний продавца (любого статуса), используя
    /adv/v1/promotion/count — этот метод группирует кампании по типу
    и статусу и отдаёт списки ID без лимита по времени жизни кампании.
    """
    resp = requests.get(CAMPAIGNS_COUNT_URL, headers=_headers(), timeout=30)

    if resp.status_code == 429:
        log.warning("Лимит запросов (429) при получении списка кампаний. Ждём.")
        time.sleep(REQUEST_INTERVAL_SECONDS)
        return get_all_campaign_ids()

    resp.raise_for_status()
    data = resp.json()

    ids = []
    for group in data.get("adverts", []):
        for item in group.get("advert_list", []):
            ids.append(item["advertId"])
    return ids


def fetch_fullstats(campaign_ids: list[int], date_from: str, date_to: str) -> list[dict]:
    """Запрашивает статистику пачки кампаний (до 50 штук) за период (до 31 дня)."""
    params = {
        "ids": ",".join(str(i) for i in campaign_ids),
        "beginDate": date_from,
        "endDate": date_to,
    }
    resp = requests.get(FULLSTATS_URL, headers=_headers(), params=params, timeout=60)

    if resp.status_code == 429:
        log.warning("Лимит запросов (429) при получении статистики. Ждём.")
        time.sleep(REQUEST_INTERVAL_SECONDS)
        return fetch_fullstats(campaign_ids, date_from, date_to)

    if resp.status_code == 400:
        # Часто означает "у этих кампаний нет статистики за этот период" — не страшно.
        log.info("Нет статистики для части кампаний за %s — %s (400, пропускаем)", date_from, date_to)
        return []

    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _date_chunks(date_from: date, date_to: date, max_days: int):
    """Разбивает большой период на куски не больше max_days, т.к. это лимит API."""
    current = date_from
    while current <= date_to:
        chunk_end = min(current + timedelta(days=max_days - 1), date_to)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def upsert_ad_spend(session, advert_id: int, day_stat: dict) -> int:
    """
    Раскладывает один день статистики кампании по артикулам (nm) и
    сохраняет/обновляет строки в AdSpend. day_stat — это один элемент
    из массива "days" в ответе fullstats.
    """
    spend_date_str = day_stat.get("date", "")[:10]
    if not spend_date_str:
        return 0
    spend_date = datetime.strptime(spend_date_str, "%Y-%m-%d").date()

    new_or_updated = 0
    for app in day_stat.get("apps", []):
        for nm in app.get("nms", []):
            nm_id = nm.get("nmId")
            if nm_id is None:
                continue

            existing = session.query(AdSpend).filter_by(
                advert_id=advert_id, nm_id=nm_id, spend_date=spend_date
            ).first()

            if existing:
                existing.sum_spent += nm.get("sum", 0) or 0
                existing.views += nm.get("views", 0) or 0
                existing.clicks += nm.get("clicks", 0) or 0
                existing.orders += nm.get("orders", 0) or 0
            else:
                session.add(AdSpend(
                    advert_id=advert_id,
                    nm_id=nm_id,
                    spend_date=spend_date,
                    sum_spent=nm.get("sum", 0) or 0,
                    views=nm.get("views", 0) or 0,
                    clicks=nm.get("clicks", 0) or 0,
                    orders=nm.get("orders", 0) or 0,
                ))
            new_or_updated += 1

    return new_or_updated


def sync_ads(date_from: str | None = None, date_to: str | None = None, history_start: str = "2024-02-01"):
    """
    Основная функция синхронизации расходов на рекламу.
    По умолчанию берёт последние 31 день (так проще: статистика рекламы
    меняется не так бурно, как продажи, и для дашборда достаточно
    регулярно обновлять недавний период; история глубже грузится один раз
    отдельным запуском с явным date_from).
    """
    init_db()
    session = get_session()

    if date_to is None:
        date_to_d = date.today()
    else:
        date_to_d = datetime.strptime(date_to, "%Y-%m-%d").date()

    if date_from is None:
        date_from_d = date_to_d - timedelta(days=31)
    else:
        date_from_d = datetime.strptime(date_from, "%Y-%m-%d").date()

    campaign_ids = get_all_campaign_ids()
    log.info("Найдено кампаний: %d", len(campaign_ids))

    if not campaign_ids:
        log.info("Нет рекламных кампаний — пропускаем синхронизацию рекламы.")
        session.close()
        return 0

    total_rows = 0
    id_batches = list(_chunked(campaign_ids, MAX_IDS_PER_REQUEST))
    date_ranges = list(_date_chunks(date_from_d, date_to_d, MAX_PERIOD_DAYS))

    for batch in id_batches:
        for d_from, d_to in date_ranges:
            stats = fetch_fullstats(batch, d_from.strftime("%Y-%m-%d"), d_to.strftime("%Y-%m-%d"))

            for campaign_stat in stats:
                advert_id = campaign_stat.get("advertId")
                for day_stat in campaign_stat.get("days", []):
                    total_rows += upsert_ad_spend(session, advert_id, day_stat)

            session.commit()
            time.sleep(REQUEST_INTERVAL_SECONDS)

    session.close()
    log.info("Синхронизация рекламы завершена. Обработано записей: %d", total_rows)
    return total_rows


if __name__ == "__main__":
    sync_ads()

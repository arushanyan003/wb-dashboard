"""
Сборщик финансовых данных из Wildberries API.

Используем метод GET /api/v5/supplier/reportDetailByPeriod —
"Отчёт о продажах по реализации". Это основной источник правды по деньгам:
продажи, комиссии, логистика, штрафы, хранение — всё в одном отчёте,
с детализацией по каждому артикулу.

⚠️ ВАЖНО НА БУДУЩЕЕ:
WB объявил, что отключит текущую версию этого метода (v5) ориентировочно
15 июля 2026 года и переводит всех на новые методы категории "Финансы":
  - POST /api/finance/v1/sales-reports/list
  - POST /api/finance/v1/sales-reports/detailed/{reportId}
На момент написания этого кода (конец июня 2026) новые методы ещё не везде
стабильны и полная схема полей официально не задокументирована, поэтому
здесь используется текущий работающий v5. Когда WB опубликует финальную
схему новых методов — нужно будет обновить функцию fetch_report_chunk()
ниже (она единственная знает про формат запроса/ответа, остальной код
не изменится). Подпишись на t.me/wb_api_notifications, чтобы не пропустить.

Документация: https://dev.wildberries.ru/openapi/financial-reports-and-accounting
"""
import os
import time
import logging
from datetime import datetime, date, timedelta

import requests
from dotenv import load_dotenv

from database import init_db, get_session, SaleRecord, SyncState

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collector")

WB_API_TOKEN = os.environ.get("WB_API_TOKEN")
REPORT_URL = "https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod"
HISTORY_START_DATE = os.environ.get("HISTORY_START_DATE", "2024-02-01")

# WB разрешает не больше 1 запроса в минуту на этот метод — соблюдаем лимит.
REQUEST_INTERVAL_SECONDS = 62


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _parse_date_only(value):
    dt = _parse_date(value)
    return dt.date() if dt else None


def fetch_report_chunk(date_from: str, date_to: str, rrdid: int = 0) -> list[dict]:
    """
    Один запрос к WB API. Возвращает список строк отчёта (может быть пустым).
    WB отдаёт максимум 100000 строк за раз; если строк больше — нужно
    повторять запрос с rrdid последней полученной строки (пагинация).
    """
    if not WB_API_TOKEN:
        raise RuntimeError(
            "Не задан WB_API_TOKEN. Проверь файл .env или переменные окружения."
        )

    headers = {"Authorization": WB_API_TOKEN}
    params = {
        "dateFrom": date_from,
        "dateTo": date_to,
        "limit": 100000,
        "rrdid": rrdid,
    }

    resp = requests.get(REPORT_URL, headers=headers, params=params, timeout=60)

    if resp.status_code == 429:
        log.warning("Превышен лимит запросов (429). Ждём минуту и повторяем.")
        time.sleep(REQUEST_INTERVAL_SECONDS)
        return fetch_report_chunk(date_from, date_to, rrdid)

    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def upsert_rows(session, rows: list[dict]) -> int:
    """Записывает строки в базу, пропуская те, что уже есть (по rrd_id)."""
    if not rows:
        return 0

    existing_ids = {
        r.rrd_id for r in session.query(SaleRecord.rrd_id).filter(
            SaleRecord.rrd_id.in_([row["rrd_id"] for row in rows])
        ).all()
    }

    new_count = 0
    for row in rows:
        if row["rrd_id"] in existing_ids:
            continue

        record = SaleRecord(
            rrd_id=row.get("rrd_id"),
            realization_report_id=row.get("realizationreport_id"),
            date_from=_parse_date_only(row.get("date_from")),
            date_to=_parse_date_only(row.get("date_to")),
            nm_id=row.get("nm_id"),
            sa_name=row.get("sa_name"),
            brand_name=row.get("brand_name"),
            subject_name=row.get("subject_name"),
            barcode=row.get("barcode"),
            doc_type_name=row.get("doc_type_name"),
            supplier_oper_name=row.get("supplier_oper_name"),
            order_dt=_parse_date(row.get("order_dt")),
            sale_dt=_parse_date(row.get("sale_dt")),
            rr_dt=_parse_date_only(row.get("rr_dt")),
            quantity=row.get("quantity") or 0,
            retail_price=row.get("retail_price") or 0,
            retail_amount=row.get("retail_amount") or 0,
            retail_price_withdisc_rub=row.get("retail_price_withdisc_rub") or 0,
            sale_percent=row.get("sale_percent") or 0,
            commission_percent=row.get("commission_percent") or 0,
            ppvz_for_pay=row.get("ppvz_for_pay") or 0,
            ppvz_sales_commission=row.get("ppvz_sales_commission") or 0,
            ppvz_reward=row.get("ppvz_reward") or 0,
            delivery_amount=row.get("delivery_amount") or 0,
            delivery_rub=row.get("delivery_rub") or 0,
            return_amount=row.get("return_amount") or 0,
            penalty=row.get("penalty") or 0,
            additional_payment=row.get("additional_payment") or 0,
            storage_fee=row.get("storage_fee") or 0,
            deduction=row.get("deduction") or 0,
            acceptance=row.get("acceptance") or 0,
            acquiring_fee=row.get("acquiring_fee") or 0,
            bonus_type_name=row.get("bonus_type_name"),
            office_name=row.get("office_name"),
            gi_box_type_name=row.get("gi_box_type_name"),
        )
        session.add(record)
        new_count += 1

    session.commit()
    return new_count


def get_last_synced_date(session) -> date:
    state = session.query(SyncState).filter_by(key="last_synced_date").first()
    if state and state.value:
        return datetime.strptime(state.value, "%Y-%m-%d").date()
    return datetime.strptime(HISTORY_START_DATE, "%Y-%m-%d").date()


def set_last_synced_date(session, d: date):
    state = session.query(SyncState).filter_by(key="last_synced_date").first()
    if not state:
        state = SyncState(key="last_synced_date")
        session.add(state)
    state.value = d.strftime("%Y-%m-%d")
    session.commit()


def sync(date_from: str | None = None, date_to: str | None = None):
    """
    Основная функция синхронизации.
    Без аргументов — забирает всё новое с последней синхронизации до сегодня.
    С аргументами — забирает конкретный период (полезно для разовой загрузки истории).
    """
    init_db()
    session = get_session()

    if date_from is None:
        date_from = get_last_synced_date(session).strftime("%Y-%m-%d")
    if date_to is None:
        date_to = date.today().strftime("%Y-%m-%d")

    log.info("Синхронизация периода %s — %s", date_from, date_to)

    rrdid = 0
    total_new = 0
    while True:
        rows = fetch_report_chunk(date_from, date_to, rrdid)
        if not rows:
            break

        new_count = upsert_rows(session, rows)
        total_new += new_count
        log.info("Получено строк: %d, новых записано: %d", len(rows), new_count)

        if len(rows) < 100000:
            # Это была последняя страница
            break

        rrdid = rows[-1]["rrd_id"]
        time.sleep(REQUEST_INTERVAL_SECONDS)  # уважаем лимит 1 запрос/минуту

    set_last_synced_date(session, date.today())
    session.close()
    log.info("Синхронизация завершена. Всего новых строк: %d", total_new)
    return total_new


if __name__ == "__main__":
    sync()

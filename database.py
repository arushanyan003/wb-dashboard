"""
Схема базы данных для дашборда WB.

Одна таблица sale_records хранит "плоские" строки финансового отчёта
(/api/v5/supplier/reportDetailByPeriod) — этого достаточно для всех
графиков и фильтров дашборда, не нужно усложнять нормализацией.
"""
import os
from datetime import date

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Float, Date, DateTime,
    UniqueConstraint, func
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class SaleRecord(Base):
    """
    Одна строка отчёта о реализации. Поля названы по смыслу (по-русски в
    комментариях), но имена колонок оставлены близкими к оригинальным полям
    WB API, чтобы было легко сверять с личным кабинетом при необходимости.
    """
    __tablename__ = "sale_records"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Уникальный ID строки отчёта у WB — по нему делаем дедупликацию,
    # т.к. WB может возвращать одну и ту же строку при пересечении периодов.
    rrd_id = Column(BigInteger, unique=True, nullable=False, index=True)

    realization_report_id = Column(BigInteger, index=True)
    date_from = Column(Date)
    date_to = Column(Date)

    # Товар
    nm_id = Column(BigInteger, index=True)          # артикул WB
    sa_name = Column(String)                         # артикул продавца
    brand_name = Column(String)
    subject_name = Column(String)                    # категория товара
    barcode = Column(String)

    # Тип операции и даты
    doc_type_name = Column(String)                   # "Продажа", "Возврат" и т.п.
    supplier_oper_name = Column(String)               # детальный тип операции
    order_dt = Column(DateTime)
    sale_dt = Column(DateTime, index=True)
    rr_dt = Column(Date, index=True)                  # дата отчёта о реализации — основная ось времени

    # Деньги
    quantity = Column(Integer, default=0)
    retail_price = Column(Float, default=0)           # цена розничная
    retail_amount = Column(Float, default=0)          # сумма продажи по рознице
    retail_price_withdisc_rub = Column(Float, default=0)
    sale_percent = Column(Float, default=0)
    commission_percent = Column(Float, default=0)

    ppvz_for_pay = Column(Float, default=0)           # к перечислению продавцу
    ppvz_sales_commission = Column(Float, default=0)  # комиссия WB
    ppvz_reward = Column(Float, default=0)

    delivery_amount = Column(Float, default=0)        # логистика, шт
    delivery_rub = Column(Float, default=0)           # логистика, руб
    return_amount = Column(Float, default=0)          # возвраты, шт

    penalty = Column(Float, default=0)                # штрафы
    additional_payment = Column(Float, default=0)
    storage_fee = Column(Float, default=0)             # платное хранение
    deduction = Column(Float, default=0)               # прочие удержания
    acceptance = Column(Float, default=0)               # приёмка
    acquiring_fee = Column(Float, default=0)            # эквайринг

    bonus_type_name = Column(String)                  # расшифровка типа штрафа/бонуса
    office_name = Column(String)
    gi_box_type_name = Column(String)

    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("rrd_id", name="uq_rrd_id"),
    )


class SyncState(Base):
    """Храним дату, до которой данные точно собраны, чтобы собирать только новое."""
    __tablename__ = "sync_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(String)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


def get_engine():
    db_url = os.environ.get("DATABASE_URL", "sqlite:///wb_dashboard.db")
    # Railway/Heroku иногда отдают строку с postgres://, SQLAlchemy 2.x хочет postgresql://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return create_engine(db_url, pool_pre_ping=True)


def get_session():
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


if __name__ == "__main__":
    init_db()
    print("База данных инициализирована.")

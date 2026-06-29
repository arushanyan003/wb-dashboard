"""
Финансовый дашборд продавца Wildberries.
Запуск: streamlit run dashboard.py
"""
import os
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import func
from dotenv import load_dotenv

from database import get_session, SaleRecord, init_db

load_dotenv()

st.set_page_config(
    page_title="WB Финансовый дашборд",
    page_icon="📊",
    layout="wide",
)

# ---------- Загрузка данных ----------

@st.cache_data(ttl=600)
def load_data(date_from: date, date_to: date) -> pd.DataFrame:
    init_db()
    session = get_session()
    query = session.query(SaleRecord).filter(
        SaleRecord.rr_dt >= date_from,
        SaleRecord.rr_dt <= date_to,
    )
    df = pd.read_sql(query.statement, session.bind)
    session.close()
    return df


@st.cache_data(ttl=600)
def get_date_bounds():
    init_db()
    session = get_session()
    min_d, max_d = session.query(
        func.min(SaleRecord.rr_dt), func.max(SaleRecord.rr_dt)
    ).first()
    session.close()
    return min_d, max_d


# ---------- Подготовка метрик ----------

def compute_kpis(df: pd.DataFrame) -> dict:
    if df.empty:
        return {k: 0 for k in [
            "revenue", "to_pay", "commission", "logistics",
            "penalty", "storage", "orders_qty", "returns_qty"
        ]}

    sales_mask = df["doc_type_name"] == "Продажа"

    return {
        "revenue": df.loc[sales_mask, "retail_amount"].sum(),
        "to_pay": df["ppvz_for_pay"].sum(),
        "commission": df["ppvz_sales_commission"].sum(),
        "logistics": df["delivery_rub"].sum(),
        "penalty": df["penalty"].sum(),
        "storage": df["storage_fee"].sum(),
        "orders_qty": int(df.loc[sales_mask, "quantity"].sum()),
        "returns_qty": int(df["return_amount"].sum()),
    }


def fmt_rub(value: float) -> str:
    return f"{value:,.0f} ₽".replace(",", " ")


# ---------- UI ----------

st.title("📊 Финансовый дашборд Wildberries")

min_date, max_date = get_date_bounds()

if min_date is None:
    st.warning(
        "В базе пока нет данных. Запусти `python collector.py` на сервере, "
        "чтобы загрузить историю из WB API, либо подожди первого "
        "автоматического запуска сборщика по расписанию."
    )
    st.stop()

# Боковая панель — фильтры
with st.sidebar:
    st.header("Фильтры")

    default_start = max(min_date, max_date - timedelta(days=30))
    date_range = st.date_input(
        "Период",
        value=(default_start, max_date),
        min_value=min_date,
        max_value=max_date,
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        period_from, period_to = date_range
    else:
        period_from, period_to = default_start, max_date

    st.caption(f"Данные доступны с {min_date} по {max_date}")

df = load_data(period_from, period_to)

if df.empty:
    st.info("Нет данных за выбранный период.")
    st.stop()

# Доп. фильтр по артикулу — после загрузки основного диапазона дат
with st.sidebar:
    articles = sorted(df["sa_name"].dropna().unique().tolist())
    selected_articles = st.multiselect("Артикул продавца (можно несколько)", articles)

if selected_articles:
    df = df[df["sa_name"].isin(selected_articles)]

kpis = compute_kpis(df)

# ---------- KPI-блок ----------

col1, col2, col3, col4 = st.columns(4)
col1.metric("Выручка (продажи)", fmt_rub(kpis["revenue"]))
col2.metric("К перечислению", fmt_rub(kpis["to_pay"]))
col3.metric("Комиссия WB", fmt_rub(kpis["commission"]))
col4.metric("Логистика", fmt_rub(kpis["logistics"]))

col5, col6, col7, col8 = st.columns(4)
col5.metric("Штрафы", fmt_rub(kpis["penalty"]))
col6.metric("Хранение", fmt_rub(kpis["storage"]))
col7.metric("Продано, шт", f"{kpis['orders_qty']:,}".replace(",", " "))
col8.metric("Возвраты, шт", f"{kpis['returns_qty']:,}".replace(",", " "))

st.divider()

# ---------- График динамики ----------

st.subheader("Динамика по дням")

daily = df.groupby("rr_dt").agg(
    revenue=("retail_amount", "sum"),
    to_pay=("ppvz_for_pay", "sum"),
    commission=("ppvz_sales_commission", "sum"),
    logistics=("delivery_rub", "sum"),
    penalty=("penalty", "sum"),
    storage=("storage_fee", "sum"),
).reset_index()

fig = go.Figure()
fig.add_trace(go.Scatter(x=daily["rr_dt"], y=daily["revenue"], name="Выручка", mode="lines+markers"))
fig.add_trace(go.Scatter(x=daily["rr_dt"], y=daily["to_pay"], name="К перечислению", mode="lines+markers"))
fig.update_layout(
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02),
    margin=dict(t=40, b=20),
)
st.plotly_chart(fig, use_container_width=True)

# ---------- Разбивка по статьям расходов ----------

st.subheader("Разбивка по статьям")

cost_breakdown = pd.DataFrame({
    "Статья": ["Комиссия WB", "Логистика", "Штрафы", "Хранение"],
    "Сумма": [kpis["commission"], kpis["logistics"], kpis["penalty"], kpis["storage"]],
})

col_left, col_right = st.columns([1, 1])

with col_left:
    fig_pie = px.pie(cost_breakdown, names="Статья", values="Сумма", hole=0.4)
    fig_pie.update_layout(margin=dict(t=10, b=10))
    st.plotly_chart(fig_pie, use_container_width=True)

with col_right:
    fig_bar = px.bar(daily, x="rr_dt", y=["commission", "logistics", "penalty", "storage"],
                      labels={"value": "₽", "rr_dt": "Дата", "variable": "Статья"})
    fig_bar.update_layout(legend_title_text="", margin=dict(t=10, b=10))
    st.plotly_chart(fig_bar, use_container_width=True)

st.divider()

# ---------- Детализация по артикулам ----------

st.subheader("Детализация по артикулам")

by_article = df.groupby(["sa_name", "nm_id", "brand_name"]).agg(
    revenue=("retail_amount", "sum"),
    to_pay=("ppvz_for_pay", "sum"),
    commission=("ppvz_sales_commission", "sum"),
    logistics=("delivery_rub", "sum"),
    penalty=("penalty", "sum"),
    storage=("storage_fee", "sum"),
    qty=("quantity", "sum"),
).reset_index().sort_values("revenue", ascending=False)

by_article.columns = [
    "Артикул продавца", "Артикул WB", "Бренд",
    "Выручка", "К перечислению", "Комиссия", "Логистика", "Штрафы", "Хранение", "Кол-во, шт"
]

st.dataframe(
    by_article.style.format({
        "Выручка": "{:,.0f} ₽",
        "К перечислению": "{:,.0f} ₽",
        "Комиссия": "{:,.0f} ₽",
        "Логистика": "{:,.0f} ₽",
        "Штрафы": "{:,.0f} ₽",
        "Хранение": "{:,.0f} ₽",
    }),
    use_container_width=True,
    height=400,
)

csv = by_article.to_csv(index=False).encode("utf-8-sig")
st.download_button("Скачать таблицу как CSV", csv, "wb_articles_report.csv", "text/csv")

st.divider()

# ---------- Штрафы детально ----------

penalties_df = df[df["penalty"] > 0]
if not penalties_df.empty:
    st.subheader("Штрафы — за что")
    penalty_breakdown = penalties_df.groupby("bonus_type_name", dropna=False).agg(
        sum_penalty=("penalty", "sum"),
        count=("penalty", "count"),
    ).reset_index().sort_values("sum_penalty", ascending=False)
    penalty_breakdown.columns = ["Причина", "Сумма, ₽", "Количество случаев"]
    st.dataframe(penalty_breakdown, use_container_width=True)

st.caption("Источник данных: Wildberries API, отчёт о продажах по реализации.")

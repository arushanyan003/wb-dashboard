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

from database import get_session, SaleRecord, ProductCost, AdSpend, init_db

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


@st.cache_data(ttl=600)
def load_ad_spend(date_from: date, date_to: date) -> pd.DataFrame:
    """
    Расходы на рекламу по дням и nm_id за период. Привязка к sa_name
    (артикулу продавца) делается позже, через join с sale_records,
    т.к. в самой рекламной статистике WB отдаёт только nm_id и название
    товара, но не sa_name.
    """
    init_db()
    session = get_session()
    query = session.query(AdSpend).filter(
        AdSpend.spend_date >= date_from,
        AdSpend.spend_date <= date_to,
    )
    df = pd.read_sql(query.statement, session.bind)
    session.close()
    return df


def load_product_costs() -> pd.DataFrame:
    """Себестоимость по артикулам — без кэша, чтобы изменения сразу применялись."""
    init_db()
    session = get_session()
    df = pd.read_sql(session.query(ProductCost).statement, session.bind)
    session.close()
    return df


def save_product_costs(edited_df: pd.DataFrame):
    """Сохраняет отредактированную таблицу себестоимости обратно в базу."""
    init_db()
    session = get_session()
    for _, row in edited_df.iterrows():
        sa_name = row["Артикул продавца"]
        cost = float(row["Себестоимость за 1 шт, ₽"] or 0)

        existing = session.query(ProductCost).filter_by(sa_name=sa_name).first()
        if existing:
            existing.cost_per_unit = cost
        else:
            session.add(ProductCost(sa_name=sa_name, cost_per_unit=cost))
    session.commit()
    session.close()


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

# ---------- Реклама: подтягиваем расход и привязываем к sa_name ----------

ad_spend_df = load_ad_spend(period_from, period_to)

# В sale_records есть соответствие nm_id -> sa_name (могут быть задвоения,
# берём последнее встретившееся имя для каждого nm_id — обычно оно не меняется).
nm_to_sa = df.drop_duplicates("nm_id").set_index("nm_id")["sa_name"].to_dict()

if not ad_spend_df.empty:
    ad_spend_df["sa_name"] = ad_spend_df["nm_id"].map(nm_to_sa)
    if selected_articles:
        ad_spend_df = ad_spend_df[ad_spend_df["sa_name"].isin(selected_articles)]
    total_ad_spend = ad_spend_df["sum_spent"].sum()
    ad_spend_by_article = ad_spend_df.groupby("sa_name")["sum_spent"].sum()
    ad_spend_by_day = ad_spend_df.groupby("spend_date")["sum_spent"].sum()
else:
    total_ad_spend = 0
    ad_spend_by_article = pd.Series(dtype=float)
    ad_spend_by_day = pd.Series(dtype=float)

# ---------- Себестоимость: редактируемая таблица ----------

costs_df = load_product_costs()
cost_by_article = (
    costs_df.set_index("sa_name")["cost_per_unit"].to_dict() if not costs_df.empty else {}
)

# Количество проданного по артикулу за период — нужно для расчёта общей себестоимости
qty_sold_by_article = df[df["doc_type_name"] == "Продажа"].groupby("sa_name")["quantity"].sum()

total_cost_of_goods = sum(
    qty_sold_by_article.get(article, 0) * cost_by_article.get(article, 0)
    for article in qty_sold_by_article.index
)

kpis = compute_kpis(df)
kpis["ad_spend"] = total_ad_spend
kpis["cost_of_goods"] = total_cost_of_goods
kpis["net_profit"] = kpis["to_pay"] - total_cost_of_goods - total_ad_spend

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

col9, col10, col11 = st.columns(3)
col9.metric("Реклама (авто из WB)", fmt_rub(kpis["ad_spend"]))
col10.metric("Себестоимость проданного", fmt_rub(kpis["cost_of_goods"]))
col11.metric("💰 Чистая прибыль", fmt_rub(kpis["net_profit"]))

if costs_df.empty or len(qty_sold_by_article.index.difference(costs_df["sa_name"])) > 0:
    st.caption(
        "⚠️ Чистая прибыль может быть неточной: не для всех проданных артикулов "
        "указана себестоимость. Заполни таблицу «Себестоимость по артикулам» ниже."
    )

st.divider()

# ---------- Себестоимость по артикулам (редактируемая таблица) ----------

st.subheader("💵 Себестоимость по артикулам")
st.caption(
    "Впиши себестоимость закупки за 1 штуку для каждого артикула — "
    "дашборд использует эти цифры для расчёта чистой прибыли. "
    "Расходы на рекламу подтягиваются из WB автоматически, вводить их не нужно."
)

# Собираем полный список артикулов, встречавшихся в продажах (не только в выбранном периоде),
# чтобы можно было один раз заполнить себестоимость и больше не возвращаться к этому экрану.
all_articles_df = pd.DataFrame({"Артикул продавца": sorted(df["sa_name"].dropna().unique())})
costs_editor_df = all_articles_df.merge(
    costs_df.rename(columns={"sa_name": "Артикул продавца", "cost_per_unit": "Себестоимость за 1 шт, ₽"})[
        ["Артикул продавца", "Себестоимость за 1 шт, ₽"]
    ] if not costs_df.empty else pd.DataFrame(columns=["Артикул продавца", "Себестоимость за 1 шт, ₽"]),
    on="Артикул продавца",
    how="left",
)
costs_editor_df["Себестоимость за 1 шт, ₽"] = costs_editor_df["Себестоимость за 1 шт, ₽"].fillna(0)

edited_costs = st.data_editor(
    costs_editor_df,
    use_container_width=True,
    hide_index=True,
    disabled=["Артикул продавца"],
    column_config={
        "Себестоимость за 1 шт, ₽": st.column_config.NumberColumn(
            min_value=0, step=1, format="%.2f ₽"
        )
    },
    key="cost_editor",
)

if st.button("💾 Сохранить себестоимость"):
    save_product_costs(edited_costs)
    st.cache_data.clear()
    st.success("Себестоимость сохранена. Цифры на дашборде обновлены.")
    st.rerun()

st.divider()

st.subheader("Динамика по дням")

daily = df.groupby("rr_dt").agg(
    revenue=("retail_amount", "sum"),
    to_pay=("ppvz_for_pay", "sum"),
    commission=("ppvz_sales_commission", "sum"),
    logistics=("delivery_rub", "sum"),
    penalty=("penalty", "sum"),
    storage=("storage_fee", "sum"),
).reset_index()

# Себестоимость по дням считаем через количество проданного в этот день
qty_by_day_article = df[df["doc_type_name"] == "Продажа"].groupby(["rr_dt", "sa_name"])["quantity"].sum().reset_index()
qty_by_day_article["cost_per_unit"] = qty_by_day_article["sa_name"].map(cost_by_article).fillna(0)
qty_by_day_article["cost_total"] = qty_by_day_article["quantity"] * qty_by_day_article["cost_per_unit"]
cost_by_day = qty_by_day_article.groupby("rr_dt")["cost_total"].sum()

daily["ad_spend"] = daily["rr_dt"].map(ad_spend_by_day).fillna(0)
daily["cost_of_goods"] = daily["rr_dt"].map(cost_by_day).fillna(0)
daily["net_profit"] = daily["to_pay"] - daily["cost_of_goods"] - daily["ad_spend"]

fig = go.Figure()
fig.add_trace(go.Scatter(x=daily["rr_dt"], y=daily["revenue"], name="Выручка", mode="lines+markers"))
fig.add_trace(go.Scatter(x=daily["rr_dt"], y=daily["to_pay"], name="К перечислению", mode="lines+markers"))
fig.add_trace(go.Scatter(
    x=daily["rr_dt"], y=daily["net_profit"], name="Чистая прибыль",
    mode="lines+markers", line=dict(width=3, dash="solid"),
))
fig.update_layout(
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02),
    margin=dict(t=40, b=20),
)
st.plotly_chart(fig, use_container_width=True)

# ---------- Разбивка по статьям расходов ----------

st.subheader("Разбивка по статьям")

cost_breakdown = pd.DataFrame({
    "Статья": ["Комиссия WB", "Логистика", "Штрафы", "Хранение", "Реклама", "Себестоимость"],
    "Сумма": [
        kpis["commission"], kpis["logistics"], kpis["penalty"], kpis["storage"],
        kpis["ad_spend"], kpis["cost_of_goods"],
    ],
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

# Подмешиваем рекламу и себестоимость, посчитанные выше по sa_name
by_article["ad_spend"] = by_article["sa_name"].map(ad_spend_by_article).fillna(0)
by_article["cost_per_unit"] = by_article["sa_name"].map(cost_by_article).fillna(0)
by_article["cost_of_goods"] = by_article["cost_per_unit"] * by_article["qty"]
by_article["net_profit"] = by_article["to_pay"] - by_article["cost_of_goods"] - by_article["ad_spend"]

by_article = by_article.drop(columns=["cost_per_unit"])

by_article.columns = [
    "Артикул продавца", "Артикул WB", "Бренд",
    "Выручка", "К перечислению", "Комиссия", "Логистика", "Штрафы", "Хранение", "Кол-во, шт",
    "Реклама", "Себестоимость", "Чистая прибыль",
]

by_article = by_article.sort_values("Чистая прибыль", ascending=False)

st.dataframe(
    by_article.style.format({
        "Выручка": "{:,.0f} ₽",
        "К перечислению": "{:,.0f} ₽",
        "Комиссия": "{:,.0f} ₽",
        "Логистика": "{:,.0f} ₽",
        "Штрафы": "{:,.0f} ₽",
        "Хранение": "{:,.0f} ₽",
        "Реклама": "{:,.0f} ₽",
        "Себестоимость": "{:,.0f} ₽",
        "Чистая прибыль": "{:,.0f} ₽",
    }).background_gradient(subset=["Чистая прибыль"], cmap="RdYlGn"),
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

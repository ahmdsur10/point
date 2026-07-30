"""
تطبيق إدارة نقاط - Point Manager (نسخة SQLAlchemy Async)
يتصل بجدول 'point' في قاعدة بيانات PostGIS على Neon باستخدام:
    - SQLAlchemy Async Engine
    - psycopg (v3) كـ driver
    - DATABASE_URL من ملف .env

طريقة التشغيل:
    1) pip install -r requirements.txt
    2) أنشئ ملف .env بجانب هذا الملف وحط فيه:
           DATABASE_URL=postgresql://neondb_owner:PASSWORD@ep-steep-bonus-ax7dker6.c-4.us-east-2.aws.neon.tech/point?sslmode=require
    3) streamlit run point_manager_app.py
"""

import re
import asyncio

import streamlit as st
import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# =========================================================
# 1) إعداد الاتصال (Async Engine مرة وحدة فقط، مخزن بالـ cache)
# =========================================================
TABLE_NAME = "point"
GEOM_COLUMN = "shape"      # اسم عمود الجيومتري الحقيقي عندك
PK_COLUMN = "gis_oid"      # عمود المفتاح الأساسي (Primary Key)
SRID = 4326

# الأعمدة الوصفية اللي تبي تظهر في النموذج (عدّلها/زد عليها حسب جدولك)
FORM_COLUMNS = [
    "اسم_الشارع",
    "الرقم_الموحد",
    "التصنيف",
    "وصف_الموقع",
    "وصف_المشكلة",
    "التصنيف_الاساسي",
    "مصدر_الشكوى",
    "درجة_الخطورة",
    "hay_n",
    "baladia",
]


@st.cache_resource
def get_engine():
    db_url = st.secrets.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("لم يتم العثور على DATABASE_URL في secrets.toml")
    # تحويل postgresql:// إلى postgresql+psycopg:// عشان يستخدم psycopg v3 async
    db_url = re.sub(r"^postgresql:", "postgresql+psycopg:", db_url)
    return create_async_engine(db_url, echo=False)


def run_async(coro):
    """يشغّل coroutine من كود Streamlit المتزامن (Sync)."""
    return asyncio.run(coro)


# =========================================================
# 2) دوال قاعدة البيانات (كلها async)
# =========================================================
async def _fetch_df(sql, params=None):
    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        rows = result.fetchall()
        cols = result.keys()
        return pd.DataFrame(rows, columns=cols)


async def _execute(sql, params=None):
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(text(sql), params or {})
        await conn.commit()


def load_data():
    cols = ", ".join([PK_COLUMN] + FORM_COLUMNS + ["lat", "long"])
    sql = f"SELECT {cols} FROM {TABLE_NAME} ORDER BY {PK_COLUMN} DESC LIMIT 200"
    return run_async(_fetch_df(sql))


def insert_point(lat, lng, values: dict):
    cols = [GEOM_COLUMN] + list(values.keys())
    col_list = ", ".join(f'"{c}"' for c in cols)
    val_list = ["ST_SetSRID(ST_MakePoint(:lng, :lat), :srid)"] + [f":{k}" for k in values.keys()]
    sql = f'INSERT INTO {TABLE_NAME} ({col_list}) VALUES ({", ".join(val_list)})'
    params = {"lng": lng, "lat": lat, "srid": SRID, **values}
    run_async(_execute(sql, params))


def update_point(pk_value, values: dict):
    set_clause = ", ".join(f'"{k}" = :{k}' for k in values.keys())
    sql = f'UPDATE {TABLE_NAME} SET {set_clause} WHERE {PK_COLUMN} = :pk_value'
    params = {**values, "pk_value": pk_value}
    run_async(_execute(sql, params))


def delete_point(pk_value):
    sql = f"DELETE FROM {TABLE_NAME} WHERE {PK_COLUMN} = :pk_value"
    run_async(_execute(sql, {"pk_value": pk_value}))


# =========================================================
# 3) واجهة التطبيق
# =========================================================
st.set_page_config(page_title="إدارة نقاط الخريطة", layout="wide")
st.title("🗺️ إدارة نقاط الخريطة - Point Manager")

tab_view, tab_add, tab_edit, tab_delete = st.tabs(
    ["📋 عرض البيانات", "➕ إضافة نقطة", "✏️ تعديل نقطة", "🗑️ حذف نقطة"]
)

# ---------------- تبويب العرض ----------------
with tab_view:
    st.subheader("آخر 200 نقطة مسجلة")
    try:
        df = load_data()
        st.dataframe(df, use_container_width=True)
    except Exception as e:
        st.error(f"خطأ في جلب البيانات: {e}")

# ---------------- تبويب الإضافة ----------------
with tab_add:
    st.subheader("إضافة نقطة جديدة")
    with st.form("add_form"):
        col1, col2 = st.columns(2)
        with col1:
            lat = st.number_input("Latitude (خط العرض)", format="%.6f", value=24.7136)
        with col2:
            lng = st.number_input("Longitude (خط الطول)", format="%.6f", value=46.6753)

        form_values = {}
        for col in FORM_COLUMNS:
            form_values[col] = st.text_input(col, key=f"add_{col}")

        submitted = st.form_submit_button("إضافة النقطة")
        if submitted:
            try:
                clean_values = {k: v for k, v in form_values.items() if v}
                insert_point(lat, lng, clean_values)
                st.success("✅ تمت إضافة النقطة بنجاح")
            except Exception as e:
                st.error(f"❌ فشل الإضافة: {e}")

# ---------------- تبويب التعديل ----------------
with tab_edit:
    st.subheader("تعديل نقطة موجودة")
    try:
        df_edit = load_data()
        if not df_edit.empty:
            selected_id = st.selectbox(
                f"اختر {PK_COLUMN}", df_edit[PK_COLUMN].tolist(), key="edit_select"
            )
            row = df_edit[df_edit[PK_COLUMN] == selected_id].iloc[0]

            with st.form("edit_form"):
                edit_values = {}
                for col in FORM_COLUMNS:
                    current_val = row[col] if pd.notna(row[col]) else ""
                    edit_values[col] = st.text_input(col, value=str(current_val), key=f"edit_{col}")

                update_submitted = st.form_submit_button("حفظ التعديلات")
                if update_submitted:
                    try:
                        update_point(selected_id, edit_values)
                        st.success("✅ تم التعديل بنجاح")
                    except Exception as e:
                        st.error(f"❌ فشل التعديل: {e}")
        else:
            st.info("ما فيه بيانات حاليًا")
    except Exception as e:
        st.error(f"خطأ: {e}")

# ---------------- تبويب الحذف ----------------
with tab_delete:
    st.subheader("حذف نقطة")
    try:
        df_del = load_data()
        if not df_del.empty:
            delete_id = st.selectbox(
                f"اختر {PK_COLUMN} للحذف", df_del[PK_COLUMN].tolist(), key="delete_select"
            )
            st.warning("⚠️ هذا الإجراء لا يمكن التراجع عنه")
            if st.button("تأكيد الحذف", type="primary"):
                try:
                    delete_point(delete_id)
                    st.success("✅ تم الحذف بنجاح")
                except Exception as e:
                    st.error(f"❌ فشل الحذف: {e}")
        else:
            st.info("ما فيه بيانات حاليًا")
    except Exception as e:
        st.error(f"خطأ: {e}")

"""
تطبيق إدارة نقاط - Point Manager (نسخة SQLAlchemy Async + خريطة تفاعلية)
يتصل بجدول 'point' في قاعدة بيانات PostGIS على Neon باستخدام:
    - SQLAlchemy Async Engine
    - psycopg (v3) كـ driver
    - st.secrets لبيانات الاتصال (.streamlit/secrets.toml)

المزايا:
    - خريطة تفاعلية تعرض كل النقاط
    - إضافة نقطة جديدة بالضغط على الخريطة مباشرة
    - إضافة / تعديل / حذف عبر نماذج تقليدية أيضًا

طريقة التشغيل:
    1) pip install -r requirements.txt
    2) أنشئ .streamlit/secrets.toml بجانب هذا الملف وحط فيه:
           DATABASE_URL = "postgresql://neondb_owner:PASSWORD@ep-steep-bonus-ax7dker6.c-4.us-east-2.aws.neon.tech/point?sslmode=require"
    3) streamlit run point_manager_app.py
"""

import re
import asyncio

import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# =========================================================
# 1) إعداد الاتصال (Async Engine مرة وحدة فقط، مخزن بالـ cache)
# =========================================================
TABLE_NAME = "point"
GEOM_COLUMN = "shape"      # اسم عمود الجيومتري الحقيقي عندك
PK_COLUMN = "gis_oid"      # عمود المفتاح الأساسي (Primary Key)
INPUT_SRID = 4326   # نظام الإحداثيات اللي يكتب فيه المستخدم (lat/lng عادي)
TABLE_SRID = 20438  # ⚠️ SRID الفعلي لعمود shape (تأكد بأمر: SELECT * FROM geometry_columns WHERE f_table_name = 'point';)

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


def load_map_data():
    """يجيب كل النقاط مع إحداثيات محولة لـ WGS84 (4326) عشان تُعرض على الخريطة بشكل صحيح."""
    cols = ", ".join([PK_COLUMN] + FORM_COLUMNS)
    sql = f"""
        SELECT {cols},
               ST_Y(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lat,
               ST_X(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lng
        FROM {TABLE_NAME}
        WHERE {GEOM_COLUMN} IS NOT NULL
        LIMIT 1000
    """
    return run_async(_fetch_df(sql))


def insert_point(lat, lng, values: dict):
    cols = [GEOM_COLUMN] + list(values.keys())
    col_list = ", ".join(f'"{c}"' for c in cols)
    val_list = ["ST_Transform(ST_SetSRID(ST_MakePoint(:lng, :lat), :input_srid), :table_srid)"] + [f":{k}" for k in values.keys()]
    sql = f'INSERT INTO {TABLE_NAME} ({col_list}) VALUES ({", ".join(val_list)})'
    params = {"lng": lng, "lat": lat, "input_srid": INPUT_SRID, "table_srid": TABLE_SRID, **values}
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

tab_map, tab_view, tab_add, tab_edit, tab_delete = st.tabs(
    ["🗺️ الخريطة", "📋 عرض البيانات", "➕ إضافة نقطة", "✏️ تعديل نقطة", "🗑️ حذف نقطة"]
)

# ---------------- تبويب الخريطة ----------------
with tab_map:
    st.subheader("كل النقاط على الخريطة")
    st.caption("اضغط على أي مكان بالخريطة لتحديد موقع نقطة جديدة، ثم عبّي البيانات تحت واحفظ.")

    try:
        map_df = load_map_data()
    except Exception as e:
        map_df = pd.DataFrame()
        st.error(f"خطأ في جلب بيانات الخريطة: {e}")

    # مركز الخريطة: لو فيه بيانات نتوسط عليها، ولو لا نستخدم الرياض كافتراضي
    if not map_df.empty:
        center_lat = map_df["map_lat"].mean()
        center_lng = map_df["map_lng"].mean()
    else:
        center_lat, center_lng = 24.7136, 46.6753

    m = folium.Map(location=[center_lat, center_lng], zoom_start=11)

    # عرض كل النقاط الموجودة كـ markers
    for _, row in map_df.iterrows():
        popup_lines = [f"<b>{col}:</b> {row[col]}" for col in FORM_COLUMNS if pd.notna(row[col]) and row[col] != ""]
        popup_html = "<br>".join(popup_lines) if popup_lines else f"{PK_COLUMN}: {row[PK_COLUMN]}"
        folium.Marker(
            location=[row["map_lat"], row["map_lng"]],
            popup=folium.Popup(popup_html, max_width=300),
            tooltip=str(row[PK_COLUMN]),
            icon=folium.Icon(color="blue", icon="map-marker", prefix="fa"),
        ).add_to(m)

    # لو فيه موقع جديد محدد (مو محفوظ بعد)، نعرضه بلون مختلف
    if st.session_state.get("new_point_location"):
        new_lat = st.session_state["new_point_location"]["lat"]
        new_lng = st.session_state["new_point_location"]["lng"]
        folium.Marker(
            location=[new_lat, new_lng],
            tooltip="نقطة جديدة (لم تُحفظ بعد)",
            icon=folium.Icon(color="red", icon="plus", prefix="fa"),
        ).add_to(m)

    map_output = st_folium(m, height=500, use_container_width=True, key="main_map")

    # التقاط ضغطة المستخدم على الخريطة
    if map_output and map_output.get("last_clicked"):
        clicked_lat = map_output["last_clicked"]["lat"]
        clicked_lng = map_output["last_clicked"]["lng"]
        st.session_state["new_point_location"] = {"lat": clicked_lat, "lng": clicked_lng}

    # لو فيه موقع محدد، اعرض نموذج تعبئة البيانات تحت الخريطة
    if st.session_state.get("new_point_location"):
        loc = st.session_state["new_point_location"]
        st.info(f"📍 الموقع المحدد: Lat = {loc['lat']:.6f}, Lng = {loc['lng']:.6f}")

        with st.form("map_add_form"):
            map_form_values = {}
            for col in FORM_COLUMNS:
                map_form_values[col] = st.text_input(col, key=f"map_add_{col}")

            col_save, col_cancel = st.columns(2)
            with col_save:
                map_submitted = st.form_submit_button("💾 حفظ النقطة", type="primary")
            with col_cancel:
                map_cancelled = st.form_submit_button("❌ إلغاء التحديد")

            if map_submitted:
                try:
                    clean_values = {k: v for k, v in map_form_values.items() if v}
                    insert_point(loc["lat"], loc["lng"], clean_values)
                    st.success("✅ تمت إضافة النقطة بنجاح")
                    del st.session_state["new_point_location"]
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ فشل الإضافة: {e}")

            if map_cancelled:
                del st.session_state["new_point_location"]
                st.rerun()

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

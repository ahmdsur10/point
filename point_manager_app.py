"""
تطبيق إدارة نقاط - Point Manager
يتصل بجدول 'point' في قاعدة بيانات PostGIS على Neon
يدعم: إضافة نقطة جديدة / تعديل نقطة موجودة / حذف نقطة / عرض جدول البيانات

طريقة التشغيل:
    1) pip install streamlit psycopg2-binary pandas
    2) عدّل بيانات الاتصال تحت (أو استخدم secrets.toml - موضح بالأسفل)
    3) streamlit run point_manager_app.py
"""

import streamlit as st
import psycopg2
import pandas as pd

# =========================================================
# 1) إعدادات الاتصال بقاعدة البيانات
# =========================================================
# الخيار الأول: تعبي القيم مباشرة هنا (أسهل للتجربة السريعة)
DB_CONFIG = {
    "host": "ep-steep-bonus-ax7dker6.c-4.us-east-2.aws.neon.tech",
    "dbname": "point",
    "user": "neondb_owner",
    "password": "ضع_كلمة_المرور_هنا",
    "sslmode": "require",
}

# الخيار الثاني (أفضل أمانًا): استخدم ملف .streamlit/secrets.toml بهذا الشكل:
# [postgres]
# host = "ep-steep-bonus-ax7dker6.c-4.us-east-2.aws.neon.tech"
# dbname = "point"
# user = "neondb_owner"
# password = "..."
# sslmode = "require"
#
# وبعدين استبدل DB_CONFIG أعلاه بـ:
# DB_CONFIG = st.secrets["postgres"]

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


# =========================================================
# 2) دوال الاتصال والاستعلامات
# =========================================================
@st.cache_resource
def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def run_query(sql, params=None, fetch=False):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if fetch:
            cols = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            conn.commit()
            return pd.DataFrame(rows, columns=cols)
        conn.commit()


def load_data():
    cols = ", ".join([PK_COLUMN] + FORM_COLUMNS + ["lat", "long"])
    sql = f'SELECT {cols} FROM {TABLE_NAME} ORDER BY {PK_COLUMN} DESC LIMIT 200;'
    return run_query(sql, fetch=True)


def insert_point(lat, lng, values: dict):
    cols = ["shape"] + list(values.keys())
    placeholders = ["ST_SetSRID(ST_MakePoint(%s, %s), %s)"] + ["%s"] * len(values)
    sql = f'''
        INSERT INTO {TABLE_NAME} ({", ".join(cols)})
        VALUES ({", ".join(placeholders)})
    '''
    params = [lng, lat, SRID] + list(values.values())
    run_query(sql, params)


def update_point(pk_value, values: dict):
    set_clause = ", ".join([f'"{k}" = %s' for k in values.keys()])
    sql = f'UPDATE {TABLE_NAME} SET {set_clause} WHERE {PK_COLUMN} = %s'
    params = list(values.values()) + [pk_value]
    run_query(sql, params)


def delete_point(pk_value):
    sql = f'DELETE FROM {TABLE_NAME} WHERE {PK_COLUMN} = %s'
    run_query(sql, [pk_value])


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
                # إزالة الحقول الفاضية لو تبي (اختياري)
                clean_values = {k: v for k, v in form_values.items() if v}
                insert_point(lat, lng, clean_values)
                st.success("✅ تمت إضافة النقطة بنجاح")
                st.cache_resource.clear()
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
                        st.cache_resource.clear()
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
                    st.cache_resource.clear()
                except Exception as e:
                    st.error(f"❌ فشل الحذف: {e}")
        else:
            st.info("ما فيه بيانات حاليًا")
    except Exception as e:
        st.error(f"خطأ: {e}")

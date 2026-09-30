"""
تطبيق إدارة نقاط - Point Manager (نسخة SQLAlchemy Sync + خريطة تفاعلية)
يتصل بجدول 'point' في قاعدة بيانات PostGIS على Neon باستخدام:
    - SQLAlchemy Engine عادي (Sync، بدون asyncio)
    - psycopg (v3) كـ driver
    - st.secrets لبيانات الاتصال (.streamlit/secrets.toml)

المزايا:
    - خريطة تفاعلية تعرض كل النقاط (بتقنية FastMarkerCluster للأداء العالي)
    - إضافة نقطة جديدة بالضغط على الخريطة مباشرة
    - إضافة / تعديل / حذف عبر نماذج تقليدية أيضًا

طريقة التشغيل:
    1) pip install -r requirements.txt
    2) أنشئ .streamlit/secrets.toml بجانب هذا الملف وحط فيه:
           DATABASE_URL = "postgresql://neondb_owner:PASSWORD@ep-steep-bonus-ax7dker6.c-4.us-east-2.aws.neon.tech/point?sslmode=require"
    3) streamlit run point_manager_app.py
"""

import re
import io
import zipfile

import streamlit as st
import pandas as pd
import folium
from folium.plugins import FastMarkerCluster, Draw
from streamlit_folium import st_folium
from sqlalchemy import text, create_engine

try:
    import shapefile  # مكتبة pyshp - قراءة شيب فايل بدون الحاجة لـ GDAL
    SHAPEFILE_AVAILABLE = True
except ImportError:
    SHAPEFILE_AVAILABLE = False

# =========================================================
# 1) إعداد الاتصال (Engine عادي متزامن، محفوظ بالـ cache مرة وحدة)
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
    "رابط_الموقع",
    "الحلول_المقترحة",
    "الاجراء_المتخذ",
]

# الحقول اللي تحتاج صندوق نص كبير (Text Area) بدل حقل نص عادي - نصوص طويلة
LONG_TEXT_COLUMNS = ["الحلول_المقترحة", "الاجراء_المتخذ"]


def render_field_input(col: str, current_value: str, key: str):
    """يعرض حقل الإدخال المناسب حسب نوع العمود: text_area للنصوص الطويلة، text_input لغيرها."""
    if col in LONG_TEXT_COLUMNS:
        return st.text_area(col, value=current_value, key=key, height=100)
    return st.text_input(col, value=current_value, key=key)


@st.cache_resource
def get_engine():
    db_url = st.secrets.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("لم يتم العثور على DATABASE_URL في secrets.toml")
    # تحويل postgresql:// إلى postgresql+psycopg:// عشان يستخدم psycopg v3 (sync)
    db_url = re.sub(r"^postgresql:", "postgresql+psycopg:", db_url)
    return create_engine(
        db_url,
        echo=False,
        pool_pre_ping=True,   # يتأكد الاتصال شغال قبل كل استعلام، ويفتح اتصال جديد تلقائيًا لو انقطع
        pool_recycle=180,     # يجدد الاتصال كل 3 دقايق عشان ما ينقطع بسبب خمول Neon (Scale to Zero)
    )


# =========================================================
# 2) دوال قاعدة البيانات (كلها sync عادية، بدون asyncio)
# =========================================================
def _fetch_df(sql, params=None):
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        rows = result.fetchall()
        cols = result.keys()
        return pd.DataFrame(rows, columns=cols)


def _execute(sql, params=None):
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text(sql), params or {})
        conn.commit()


def load_data():
    cols = ", ".join([PK_COLUMN] + FORM_COLUMNS + ["lat", "long"])
    sql = f"SELECT {cols} FROM {TABLE_NAME} ORDER BY {PK_COLUMN} DESC LIMIT 200"
    return _fetch_df(sql)


@st.cache_data(ttl=30)
def load_map_data():
    """يجيب كل النقاط مع إحداثيات محولة لـ WGS84 (4326) - تستخدم كـ fallback أول مرة قبل تحديد حدود الخريطة."""
    cols = ", ".join([PK_COLUMN] + FORM_COLUMNS)
    sql = f"""
        SELECT {cols},
               ST_Y(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lat,
               ST_X(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lng
        FROM {TABLE_NAME}
        WHERE {GEOM_COLUMN} IS NOT NULL
        ORDER BY {PK_COLUMN} DESC
        LIMIT 300
    """
    return _fetch_df(sql)


@st.cache_data(ttl=30)
def load_points_in_bounds(south, west, north, east, limit=300):
    """يجيب بس النقاط الموجودة داخل حدود الخريطة الظاهرة حاليًا (Viewport).
    يستخدم ST_Intersects مع Spatial Index (GiST) على عمود shape، فيكون سريع جدًا
    حتى مع آلاف النقاط - لازم ينفذ هذا الأمر مرة وحدة بـ pgAdmin أول شي:
        CREATE INDEX IF NOT EXISTS idx_point_shape ON point USING GIST (shape);
    """
    cols = ", ".join([PK_COLUMN] + FORM_COLUMNS)
    sql = f"""
        SELECT {cols},
               ST_Y(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lat,
               ST_X(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lng
        FROM {TABLE_NAME}
        WHERE {GEOM_COLUMN} IS NOT NULL
          AND ST_Intersects(
                {GEOM_COLUMN},
                ST_Transform(
                    ST_MakeEnvelope(:west, :south, :east, :north, :input_srid),
                    :table_srid
                )
              )
        LIMIT :limit
    """
    params = {
        "west": west, "south": south, "east": east, "north": north,
        "input_srid": INPUT_SRID, "table_srid": TABLE_SRID, "limit": limit,
    }
    return _fetch_df(sql, params)


ALL_COLUMNS_LABEL = "🔎 كل الأعمدة"
TEXT_TYPES = {"text", "character varying", "character", "citext", "name"}
DEFAULT_LAT, DEFAULT_LNG = 24.7136, 46.6753


@st.cache_data(ttl=300)
def get_table_columns():
    """يقرأ هيكل الجدول الفعلي (أسماء الأعمدة وأنواعها) من قاعدة البيانات نفسها،
    عشان أي عمود تضيفه للجدول مستقبلًا يظهر تلقائيًا بالتعديل والبحث بدون تعديل الكود."""
    try:
        return _fetch_df(
            """
            SELECT column_name, data_type, is_generated, identity_generation
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = :t
            ORDER BY ordinal_position
            """,
            {"t": TABLE_NAME},
        )
    except Exception:
        return pd.DataFrame()


def get_columns_info():
    """قائمة بكل أعمدة الجدول: الاسم، النوع، هل قابل للتعديل، هل قابل للبحث."""
    df = get_table_columns()
    if df.empty:  # احتياط لو تعذر قراءة هيكل الجدول
        return [
            {"name": c, "type": "text", "editable": c != PK_COLUMN, "searchable": True}
            for c in [PK_COLUMN] + FORM_COLUMNS
        ]
    infos = []
    for _, r in df.iterrows():
        name, dtype = r["column_name"], r["data_type"]
        is_geom = dtype == "USER-DEFINED" or name == GEOM_COLUMN
        read_only = (
            name == PK_COLUMN or is_geom or dtype == "ARRAY"
            or r["is_generated"] == "ALWAYS" or r["identity_generation"] == "ALWAYS"
        )
        infos.append({
            "name": name, "type": dtype,
            "editable": not read_only, "searchable": not is_geom,
        })
    return infos


def _to_text(v):
    if v is None or (isinstance(v, float) and v != v):
        return ""
    return str(v)


@st.cache_data(ttl=30)
def search_points(query_text, column, limit=50):
    """بحث جزئي (غير حساس لحالة الأحرف) داخل عمود محدد، أو داخل كل الأعمدة."""
    if not query_text or not query_text.strip():
        return pd.DataFrame()
    searchable = [c["name"] for c in get_columns_info() if c["searchable"]]
    if column == ALL_COLUMNS_LABEL:
        targets = searchable
    elif column in searchable:  # نتأكد إن اسم العمود من أعمدة الجدول الفعلية (حماية)
        targets = [column]
    else:
        raise ValueError("عمود البحث غير موجود بالجدول")
    like_conditions = " OR ".join([f'"{c}"::text ILIKE :q' for c in targets])
    extra = [column] if column in searchable else []
    cols = ", ".join(f'"{c}"' for c in dict.fromkeys([PK_COLUMN] + FORM_COLUMNS + extra))
    sql = f"""
        SELECT {cols},
               ST_Y(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lat,
               ST_X(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lng
        FROM {TABLE_NAME}
        WHERE {GEOM_COLUMN} IS NOT NULL AND ({like_conditions})
        LIMIT :limit
    """
    return _fetch_df(sql, {"q": f"%{query_text.strip()}%", "limit": limit})


def get_point_full(pk_value):
    """يجيب كل الحقول القابلة للتعديل لنقطة واحدة + إحداثياتها (WGS84)."""
    editable = [c["name"] for c in get_columns_info() if c["editable"]]
    col_sql = ", ".join(f'"{c}"' for c in editable)
    sql = f"""
        SELECT {col_sql},
               ST_Y(ST_Transform("{GEOM_COLUMN}", {INPUT_SRID})) AS "__lat",
               ST_X(ST_Transform("{GEOM_COLUMN}", {INPUT_SRID})) AS "__lng"
        FROM {TABLE_NAME} WHERE "{PK_COLUMN}" = :pk_value
    """
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"pk_value": pk_value}).mappings().first()
    return dict(row) if row else None


def update_point_full(pk_value, changes: dict, col_types: dict, new_coords=None):
    """يحدّث الحقول المتغيرة فقط + (اختياريًا) موقع النقطة.
    القيمة None تعني NULL. الأنواع غير النصية تُحوَّل بـ CAST حسب نوع العمود بالقاعدة."""
    sets, params = [], {"pk_value": pk_value}
    for i, (col, val) in enumerate(changes.items()):
        if val is None:
            sets.append(f'"{col}" = NULL')
        elif col_types.get(col, "text") in TEXT_TYPES:
            sets.append(f'"{col}" = :v{i}')
            params[f"v{i}"] = val
        else:
            sets.append(f'"{col}" = CAST(:v{i} AS {col_types[col]})')
            params[f"v{i}"] = val
    if new_coords:
        sets.append(
            f'"{GEOM_COLUMN}" = ST_Transform(ST_SetSRID(ST_MakePoint(:new_lng, :new_lat), {INPUT_SRID}), {TABLE_SRID})'
        )
        params["new_lat"], params["new_lng"] = new_coords
    if not sets:
        return
    _execute(f'UPDATE {TABLE_NAME} SET {", ".join(sets)} WHERE "{PK_COLUMN}" = :pk_value', params)


def insert_point(lat, lng, values: dict):
    cols = [GEOM_COLUMN] + list(values.keys())
    col_list = ", ".join(f'"{c}"' for c in cols)
    val_list = ["ST_Transform(ST_SetSRID(ST_MakePoint(:lng, :lat), :input_srid), :table_srid)"] + [f":{k}" for k in values.keys()]
    sql = f'INSERT INTO {TABLE_NAME} ({col_list}) VALUES ({", ".join(val_list)})'
    params = {"lng": lng, "lat": lat, "input_srid": INPUT_SRID, "table_srid": TABLE_SRID, **values}
    _execute(sql, params)


def bulk_insert_points(records: list, source_srid: int):
    """إدخال جماعي لعدة نقاط دفعة وحدة - يستخدم للاستيراد من Excel/Shapefile.
    records: قائمة قواميس، كل واحد فيها لازم يحتوي 'x' و 'y' (إحداثيات بنظام source_srid)
             + أي أعمدة إضافية من FORM_COLUMNS (اختياري).
    يرجع (عدد النجاح, قائمة الأخطاء [(رقم الصف, رسالة الخطأ), ...])"""
    engine = get_engine()
    success_count = 0
    errors = []
    with engine.connect() as conn:
        for i, rec in enumerate(records):
            try:
                extra_cols = [k for k in rec.keys() if k not in ("x", "y")]
                cols = [GEOM_COLUMN] + extra_cols
                col_list = ", ".join(f'"{c}"' for c in cols)
                val_list = ["ST_Transform(ST_SetSRID(ST_MakePoint(:x, :y), :src_srid), :table_srid)"] + [f":{k}" for k in extra_cols]
                sql = f'INSERT INTO {TABLE_NAME} ({col_list}) VALUES ({", ".join(val_list)})'
                params = {"x": rec["x"], "y": rec["y"], "src_srid": source_srid, "table_srid": TABLE_SRID}
                for k in extra_cols:
                    params[k] = rec[k]
                conn.execute(text(sql), params)
                success_count += 1
            except Exception as e:
                errors.append((i + 1, str(e)))
        conn.commit()
    return success_count, errors


def get_point_by_id(pk_value):
    """يجيب نقطة واحدة بالضبط برقم gis_oid من كامل الجدول (بدون حد أقصى للبحث،
    بعكس load_data اللي تجيب بس آخر 200 نقطة). يستخدم بتبويب التعديل عشان تقدر
    تعدل أي نقطة بالجدول كامل مو بس النقاط الأخيرة."""
    cols = ", ".join([PK_COLUMN] + FORM_COLUMNS)
    sql = f"SELECT {cols} FROM {TABLE_NAME} WHERE {PK_COLUMN} = :pk_value"
    return _fetch_df(sql, {"pk_value": pk_value})


def update_point(pk_value, values: dict):
    set_clause = ", ".join(f'"{k}" = :{k}' for k in values.keys())
    sql = f'UPDATE {TABLE_NAME} SET {set_clause} WHERE {PK_COLUMN} = :pk_value'
    params = {**values, "pk_value": pk_value}
    _execute(sql, params)


def delete_point(pk_value):
    sql = f"DELETE FROM {TABLE_NAME} WHERE {PK_COLUMN} = :pk_value"
    _execute(sql, {"pk_value": pk_value})


def delete_points(pk_values: list):
    """حذف عدة نقاط دفعة وحدة برقم gis_oid. يرجع (عدد النجاح, قائمة الأخطاء)."""
    engine = get_engine()
    success_count = 0
    errors = []
    with engine.connect() as conn:
        for pk_value in pk_values:
            try:
                sql = f"DELETE FROM {TABLE_NAME} WHERE {PK_COLUMN} = :pk_value"
                conn.execute(text(sql), {"pk_value": pk_value})
                success_count += 1
            except Exception as e:
                errors.append((pk_value, str(e)))
        conn.commit()
    return success_count, errors


# =========================================================
# 3) واجهة التطبيق
# =========================================================
st.set_page_config(page_title="إدارة نقاط الخريطة", layout="wide")

# ---------------------------------------------------------
# إخفاء شريط أدوات Streamlit العلوي بالكامل (Share / ⭐ / ✏️ / GitHub / ⋮)
# ملاحظة أمنية مهمة: هذا يخفي الواجهة بس، ما يمنع الوصول للكود الفعلي
# لو ريبو GitHub المرتبط بالتطبيق عام (Public). لازم كمان تخلي الريبو Private
# من إعدادات GitHub، وإلا أي حد يقدر يوصل للكود عن طريق رابط الريبو مباشرة.
# ---------------------------------------------------------
st.markdown("""
    <style>
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stToolbarActions"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"],
        #MainMenu,
        footer,
        footer *,
        .stToolbar,
        button[title="View app fullscreen"],
        a[href*="streamlit.io"],
        a[href*="github.com"],
        [data-testid="baseButton-headerNoPadding"]
        {
            display: none !important;
            visibility: hidden !important;
            height: 0 !important;
            overflow: hidden !important;
            pointer-events: none !important;
        }

        /* نبضة متحركة لعلامة النقطة الجديدة غير المحفوظة على الخريطة */
        @keyframes pulse-ring {
            0%   { transform: scale(0.6); opacity: 0.6; }
            70%  { transform: scale(1.8); opacity: 0; }
            100% { transform: scale(1.8); opacity: 0; }
        }
    </style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------
# تنسيق التطبيق: واجهة عربية من اليمين لليسار (RTL) + خط عربي + مظهر أنظف
# (الخريطة والأكواد والأرقام تبقى LTR لأن هذا هو الأنسب لها)
# ---------------------------------------------------------
st.markdown("""
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700&display=swap');

        /* اتجاه الصفحة كاملة من اليمين لليسار */
        html, body, .stApp,
        [data-testid="stAppViewContainer"],
        [data-testid="stMain"],
        [data-testid="stMainBlockContainer"] {
            direction: rtl;
            text-align: right;
        }

        /* الخط العربي (ما نطبقه على span عشان ما نكسر أيقونات Streamlit) */
        .stApp, .stApp p, .stApp label, .stApp li, .stApp input, .stApp textarea,
        .stApp button, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5,
        .stApp [data-baseweb="tab"], .stApp [data-baseweb="select"] div,
        .stApp [data-testid="stCaptionContainer"] {
            font-family: 'Cairo', 'Segoe UI', Tahoma, sans-serif !important;
        }

        h1, h2, h3, h4, h5, p, label,
        [data-testid="stMarkdownContainer"],
        [data-testid="stCaptionContainer"],
        [data-testid="stWidgetLabel"] {
            text-align: right !important;
        }

        /* حقول الإدخال */
        input, textarea { direction: rtl; text-align: right; }
        [data-testid="stNumberInput"] input { direction: ltr; text-align: left; }
        [data-baseweb="select"] { direction: rtl; text-align: right; }

        /* عناصر لازم تبقى LTR: الخريطة، الأكواد، محرر SQL، الجداول */
        pre, code, [data-testid="stCode"], textarea[aria-label="SQL:"],
        [data-testid="stCustomComponentV1"], iframe,
        [data-testid="stDataFrame"] {
            direction: ltr !important;
            text-align: left !important;
        }

        /* مظهر عام */
        .block-container { padding-top: 2rem; max-width: 1400px; }
        h1 { color: #0b5cad; font-weight: 700; }
        h2, h3, h4 { color: #1f3a5f; font-weight: 600; }

        [data-baseweb="tab-list"] { gap: 6px; border-bottom: 2px solid #e3e8ef; }
        [data-baseweb="tab"] {
            border-radius: 10px 10px 0 0; padding: 10px 16px; font-weight: 600;
        }

        .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
            border-radius: 10px; font-weight: 600;
        }
        [data-testid="stForm"] {
            border: 1px solid #e3e8ef; border-radius: 14px; padding: 1.2rem;
            background: rgba(120, 140, 170, 0.05);
        }
        [data-testid="stAlert"] { border-radius: 12px; }
        [data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }
    </style>
""", unsafe_allow_html=True)

st.title("🗺️ إدارة نقاط الخريطة - Point Manager")

# =========================================================
# الخريطة: توضع خارج st.tabs تمامًا (مهم جدًا)
# لو الخريطة تكون جوا tab غير نشط وقت أول رسم، Leaflet يحسب
# حجمها صفر وتطلع بيضاء/فاضية حتى لو رجعت تفتح نفس التبويب.
# لذلك نخليها دايمًا ظاهرة بأعلى الصفحة مباشرة.
# =========================================================
st.subheader("🗺️ خريطة النقاط")
st.caption("حرّك/كبّر الخريطة عشان تشوف النقاط بمنطقتك، ابحث عن نقطة محددة، أو اضغط على أيقونة الماركر 📍 بأعلى يسار الخريطة ثم حدد مكان النقطة الجديدة. تقدر تبدّل بين خريطة الشوارع والصورة الجوية من أيقونة الطبقات بأعلى يمين الخريطة.")

# ---------------------------------------------------------
# مربع البحث: أولًا نختار اسم العمود، وبعدين نكتب الكلمة اللي نبحث عنها فيه
# ---------------------------------------------------------
search_column_options = [ALL_COLUMNS_LABEL] + [c["name"] for c in get_columns_info() if c["searchable"]]

with st.form("search_form"):
    sc_col, sc_query, sc_btn = st.columns([2, 3, 1])
    with sc_col:
        search_column = st.selectbox("📂 اختر العمود", search_column_options, key="search_column")
    with sc_query:
        search_query = st.text_input(
            "🔍 الكلمة المراد البحث عنها",
            key="search_box",
            placeholder="مثال: شارع الملك فهد",
        )
    with sc_btn:
        st.markdown("<div style='height:1.85rem'></div>", unsafe_allow_html=True)  # محاذاة الزر
        do_search = st.form_submit_button("🔍 بحث", use_container_width=True, type="primary")

if do_search:
    if not search_query.strip():
        st.warning("اكتب الكلمة اللي تبي تبحث عنها")
    else:
        try:
            st.session_state["search_results"] = search_points(search_query, search_column)
            st.session_state["search_meta"] = {"column": search_column, "query": search_query.strip()}
            st.session_state["search_seq"] = st.session_state.get("search_seq", 0) + 1
            st.session_state.pop("focus_location", None)
            st.session_state.pop("_focus_token", None)
        except Exception as e:
            st.error(f"خطأ بالبحث: {e}")

search_results = st.session_state.get("search_results")
if search_results is not None:
    meta = st.session_state.get("search_meta", {})
    if search_results.empty:
        st.warning(f"ما فيه نتائج لكلمة «{meta.get('query', '')}» في {meta.get('column', '')}")
    else:
        head_col, clear_col = st.columns([4, 1])
        with head_col:
            st.success(f"لقيت {len(search_results)} نتيجة لكلمة «{meta['query']}» في: {meta['column']}")
        with clear_col:
            if st.button("🧹 مسح النتائج", use_container_width=True):
                for k in ("search_results", "search_meta", "focus_location", "_focus_token"):
                    st.session_state.pop(k, None)
                st.rerun()

        shown_extra = meta["column"] if meta["column"] in search_results.columns else None

        def _result_label(row):
            parts = [str(row[c]) for c in FORM_COLUMNS[:3] if pd.notna(row[c]) and row[c] != ""]
            if shown_extra and shown_extra not in FORM_COLUMNS[:3] and pd.notna(row[shown_extra]):
                parts.append(f"{shown_extra}: {row[shown_extra]}")
            label = f"[{row[PK_COLUMN]}] " + (" - ".join(parts) if parts else "بدون بيانات")
            return label if len(label) <= 130 else label[:127] + "..."

        result_labels = {idx: _result_label(row) for idx, row in search_results.iterrows()}
        seq = st.session_state.get("search_seq", 0)
        selected_idx = st.selectbox(
            "اختر نتيجة للتوسط عليها بالخريطة:",
            options=list(result_labels.keys()),
            format_func=lambda i: result_labels[i],
            key=f"search_result_select_{seq}",
        )
        # نحدّث موقع التركيز فقط لما يتغير الاختيار فعليًا (عشان ما يتعارض مع زر «إظهار النقاط»)
        token = (seq, selected_idx)
        if st.session_state.get("_focus_token") != token:
            selected_row = search_results.loc[selected_idx]
            st.session_state["focus_location"] = {
                "lat": selected_row["map_lat"], "lng": selected_row["map_lng"]
            }
            st.session_state["_focus_token"] = token

# ---------------------------------------------------------
# تنبيه بارز أعلى الخريطة لو فيه نقطة جديدة لسا ما انحفظت
# (يفضل ظاهر مهما تنقلت بالخريطة، عشان ما تنسى تكمل حفظها)
# ---------------------------------------------------------
if st.session_state.get("new_point_location"):
    pending = st.session_state["new_point_location"]
    st.warning(
        f"⚠️ **عندك نقطة جديدة لسا ما انحفظت!** "
        f"(الموقع: {pending['lat']:.5f}, {pending['lng']:.5f}) — "
        f"بانها بالخريطة بعلامة 🔴 كبيرة. عبّي البيانات بالنموذج تحت الخريطة واضغط «حفظ النقطة»، "
        f"أو اضغط «إلغاء التحديد» لو ضغطت بالغلط."
    )

# ---------------------------------------------------------
# تحديد مركز/تكبير الخريطة:
# 1) لو فيه نتيجة بحث محددة: نتوسط عليها بزوم قريب
# 2) لو المستخدم ضغط "إظهار النقاط حسب العرض الحالي": نستخدم آخر موقع محفوظ وقتها
# 3) غير كذا: قيمة افتراضية (الرياض) - ونخلي الخريطة نفسها (المتصفح) يتحكم بالزوم
#    والتنقل بعد كذا بدون ما نتدخل، عشان ما نسبب اهتزاز/تصارع مع حركة المستخدم
# ---------------------------------------------------------
if st.session_state.get("focus_location"):
    center_lat = st.session_state["focus_location"]["lat"]
    center_lng = st.session_state["focus_location"]["lng"]
    zoom_level = 17
elif st.session_state.get("last_map_center"):
    center_lat = st.session_state["last_map_center"]["lat"]
    center_lng = st.session_state["last_map_center"]["lng"]
    zoom_level = 15
else:
    center_lat, center_lng = 24.7136, 46.6753
    zoom_level = 12

# ---------------------------------------------------------
# جلب النقاط: نعرض الخريطة فاضية أول ما تفتح، ولا نجيب أي نقاط
# إلا بعد ما المستخدم يكبّر/يحرّك للمنطقة اللي تبيه ويضغط زر
# "إظهار النقاط حسب العرض الحالي فقط" تحت الخريطة (اللي يحدد last_map_bounds).
# ---------------------------------------------------------
last_bounds = st.session_state.get("last_map_bounds")
map_df = pd.DataFrame()
if last_bounds:
    try:
        map_df = load_points_in_bounds(
            last_bounds["south"], last_bounds["west"],
            last_bounds["north"], last_bounds["east"],
        )
    except Exception as e:
        map_df = pd.DataFrame()
        st.error(f"خطأ في جلب بيانات الخريطة: {e}")

if last_bounds:
    st.caption(f"📍 عدد النقاط الظاهرة حاليًا: {len(map_df)}")
else:
    st.info(
        "🔍 الخريطة فاضية حاليًا. كبّر/حرّك للمنطقة اللي تبيها، "
        "ثم اضغط زر «👁️ إظهار النقاط حسب العرض الحالي فقط» تحت الخريطة عشان تظهر النقاط."
    )

# نبني الخريطة بدون طبقة تايل افتراضية، ونضيف طبقتين يدويًا (شارع + قمر صناعي)
# عشان يقدر المستخدم يبدل بينهم من أداة الطبقات (أيقونة أعلى يمين الخريطة)
m = folium.Map(location=[center_lat, center_lng], zoom_start=zoom_level, prefer_canvas=True, tiles=None)

folium.TileLayer(
    tiles="OpenStreetMap", name="🗺️ خريطة الشوارع", overlay=False, control=True,
).add_to(m)
folium.TileLayer(
    tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attr="Esri World Imagery",
    name="🛰️ صورة جوية (قمر صناعي)",
    overlay=False,
    control=True,
).add_to(m)

# نستخدم FastMarkerCluster بدل MarkerCluster العادي: يبني الماركرات عن طريق
# كود JS مضغوط جدًا بدل ما ينشئ عنصر HTML/DOM كامل لكل نقطة بشكل منفصل.
# هذا أسرع بشكل ملحوظ مع مئات/آلاف النقاط.
if not map_df.empty:
    # نبني للـ popup: رقم gis_oid دايمًا بالأعلى (يسهّل نسخه للاستعلام/التعديل/الحذف)
    # + أهم عمودين/ثلاثة إضافيين بس (بدل كل الأعمدة) عشان يفضل حجم البيانات المرسلة صغير وسريع
    popup_cols = FORM_COLUMNS[:3]

    def build_row(row):
        parts = [f"<b>{PK_COLUMN}:</b> {row[PK_COLUMN]}"]
        parts += [f"<b>{c}:</b> {row[c]}" for c in popup_cols if pd.notna(row[c]) and row[c] != ""]
        popup_text = "<br>".join(parts)
        return [row["map_lat"], row["map_lng"], popup_text]

    cluster_data = [build_row(row) for _, row in map_df.iterrows()]

    callback = """
    function (row) {
        var marker = L.circleMarker(new L.LatLng(row[0], row[1]), {
            radius: 7, color: '#1a73e8', fillColor: '#1a73e8',
            fillOpacity: 0.85, weight: 1
        });
        marker.bindPopup(row[2], {autoPan: false});
        return marker;
    }
    """

    FastMarkerCluster(
        data=cluster_data,
        callback=callback,
        disableClusteringAtZoom=17,
    ).add_to(m)

# ماركر بارز لنتيجة البحث المختارة
if st.session_state.get("focus_location"):
    floc = st.session_state["focus_location"]
    folium.Marker(
        location=[floc["lat"], floc["lng"]],
        tooltip="نتيجة البحث",
        icon=folium.Icon(color="green", icon="star", prefix="fa"),
    ).add_to(m)

# لو فيه موقع جديد محدد (مو محفوظ بعد)، نعرضه بعلامة نابضة كبيرة وواضحة جدًا
if st.session_state.get("new_point_location"):
    new_lat = st.session_state["new_point_location"]["lat"]
    new_lng = st.session_state["new_point_location"]["lng"]
    pulse_html = """
    <div style="position:relative; width:30px; height:30px;">
        <div style="position:absolute; top:0; left:0; width:30px; height:30px;
                    background:#e53935; border-radius:50%; opacity:0.55;
                    animation: pulse-ring 1.4s ease-out infinite;"></div>
        <div style="position:absolute; top:8px; left:8px; width:14px; height:14px;
                    background:#e53935; border:2px solid white; border-radius:50%;
                    box-shadow:0 0 6px rgba(0,0,0,0.6);"></div>
    </div>
    """
    folium.Marker(
        location=[new_lat, new_lng],
        tooltip="🔴 نقطة جديدة - لم تُحفظ بعد",
        icon=folium.DivIcon(html=pulse_html, icon_size=(30, 30), icon_anchor=(15, 15)),
    ).add_to(m)

# ---------------------------------------------------------
# أداة إضافة نقطة عن طريق أيقونة مخصصة (Draw plugin):
# تظهر أيقونة ماركر بأعلى يسار الخريطة، تضغطها أول، وبعدين تضغط
# على المكان اللي تبيه بالخريطة عشان تحدد نقطة جديدة - بدل ما أي
# ضغطة عشوائية بالخريطة تضيف نقطة بالغلط.
# ---------------------------------------------------------
Draw(
    export=False,
    draw_options={
        "polyline": False, "polygon": False, "rectangle": False,
        "circle": False, "circlemarker": False,
        "marker": True,
    },
    edit_options={"edit": False, "remove": False},
).add_to(m)

folium.LayerControl(position="topright", collapsed=False).add_to(m)

try:
    map_output = st_folium(
        m, width="100%", height=550,
        returned_objects=["all_drawings", "bounds"],
        key="main_map",
    )
except Exception as e:
    map_output = None
    st.error(f"خطأ في عرض الخريطة: {e}")
    st.info("جرب تحدّث المكتبة: pip install --upgrade streamlit-folium folium")

# حفظ حدود الخريطة الحالية (Bounds) للاستخدام لاحقًا - بدون rerun تلقائي ولا إجبار
# على زوم/مركز معين. المستخدم يضغط "إظهار النقاط..." متى ما يحتاج يحدّث النقاط
# المعروضة حسب المنطقة الحالية. ملاحظة مهمة: ما نتابع zoom/center من الخريطة
# ولا نفرضهم مرة ثانية على الخريطة - هذا كان يسبب اهتزاز/تصارع مع تنقل المستخدم
# الفعلي (كل حركة زوم بسيطة تسبب rerun يعيد فرض زوم قديم يتعارض مع حركته الحالية).
if map_output and map_output.get("bounds"):
    b = map_output["bounds"]
    st.session_state["pending_bounds"] = {
        "south": b["_southWest"]["lat"], "west": b["_southWest"]["lng"],
        "north": b["_northEast"]["lat"], "east": b["_northEast"]["lng"],
    }

refresh_col1, refresh_col2 = st.columns([1, 4])
with refresh_col1:
    if st.button("👁️ إظهار النقاط حسب العرض الحالي فقط", use_container_width=True):
        if st.session_state.get("pending_bounds"):
            pb = st.session_state["pending_bounds"]
            st.session_state["last_map_bounds"] = pb
            # نحسب مركز تقريبي من حدود العرض الحالي بس ما نغيّر مستوى الزوم -
            # يفضل الزوم زي ما هو بدل ما نفرض رقم قديم يسبب قفزة
            st.session_state["last_map_center"] = {
                "lat": (pb["south"] + pb["north"]) / 2,
                "lng": (pb["west"] + pb["east"]) / 2,
            }
        st.session_state.pop("focus_location", None)
        st.rerun()

# التقاط نقطة جديدة تمت إضافتها عن طريق أيقونة الماركر (Draw tool) بأعلى يسار الخريطة
# - بدل الاعتماد على أي ضغطة عشوائية بالخريطة، الحين لازم تضغط الأيقونة أول قصدًا
#
# ملاحظة مهمة: نعتمد على "عدد" الرسومات الكلي (all_drawings) بدل آخر رسمة لحالها
# (last_active_drawing) - لأن الأخيرة ممكن "تترجع" من جديد بذاكرة المكوّن حتى بعد
# ما نعالجها، حتى لو كان السبب مجرد فتح popup لنقطة موجودة. بمقارنة العدد فقط،
# نتأكد 100% إن فيه رسمة جديدة فعلية صارت (العدد زاد)، مو مجرد إعادة إرسال قديمة.
if map_output and map_output.get("all_drawings"):
    drawings = map_output["all_drawings"]
    last_count = st.session_state.get("drawings_count", 0)
    if len(drawings) > last_count:
        newest = drawings[-1]
        if newest.get("geometry", {}).get("type") == "Point":
            # GeoJSON يخزن الإحداثيات بترتيب [lng, lat]
            drawn_lng, drawn_lat = newest["geometry"]["coordinates"]
            st.session_state["new_point_location"] = {"lat": drawn_lat, "lng": drawn_lng}
            st.session_state.pop("focus_location", None)
        st.session_state["drawings_count"] = len(drawings)

# نموذج تعبئة بيانات النقطة الجديدة (التنبيه الرئيسي بمكانه فوق الخريطة)
if st.session_state.get("new_point_location"):
    loc = st.session_state["new_point_location"]
    st.markdown("### 📝 عبّي بيانات النقطة الجديدة")

    with st.form("map_add_form"):
        map_form_values = {}
        for col in FORM_COLUMNS:
            map_form_values[col] = render_field_input(col, "", key=f"map_add_{col}")

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
                load_map_data.clear()
                load_points_in_bounds.clear()
                search_points.clear()
                st.rerun()
            except Exception as e:
                st.error(f"❌ فشل الإضافة: {e}")

        if map_cancelled:
            del st.session_state["new_point_location"]
            st.rerun()

st.divider()

# =========================================================
# باقي الوظائف تحت بالتبويبات (عرض / إضافة يدوي / تعديل / حذف)
# =========================================================
tab_view, tab_sql, tab_import, tab_add, tab_edit, tab_delete = st.tabs(
    ["📋 عرض البيانات", "🧮 استعلام SQL", "📤 استيراد جماعي", "➕ إضافة نقطة (يدوي)", "✏️ تعديل نقطة", "🗑️ حذف نقطة"]
)

# ---------------- تبويب العرض ----------------
with tab_view:
    st.subheader("آخر 200 نقطة مسجلة")
    try:
        df = load_data()
        st.dataframe(
            df,
            use_container_width=True,
            column_config={
                "رابط_الموقع": st.column_config.LinkColumn("رابط الموقع", display_text="📍 فتح بقوقل ماب")
            },
        )
    except Exception as e:
        st.error(f"خطأ في جلب البيانات: {e}")

# ---------------- تبويب استعلام SQL ----------------
with tab_sql:
    st.subheader("🧮 Select By Attributes")

    # ---- Input Table (ثابت) ----
    st.text_input("Input Table", value=TABLE_NAME, disabled=True, key="sql_input_table")

    # ---- Selection Type ----
    st.selectbox(
        "Selection Type",
        ["New selection"],
        disabled=True,
        key="sql_selection_type",
        help="حاليًا مدعوم بس 'New selection' (استعلام جديد في كل مرة)",
    )

    st.markdown("**Expression**")

    sql_editor_mode = st.toggle("🔧 SQL Editor (كتابة يدوية)", value=False, key="sql_editor_toggle")

    OPERATORS = {
        "is equal to": "=",
        "is not equal to": "!=",
        "is greater than": ">",
        "is greater than or equal to": ">=",
        "is less than": "<",
        "is less than or equal to": "<=",
        "contains": "CONTAINS",
        "starts with": "STARTS_WITH",
        "is null": "IS NULL",
        "is not null": "IS NOT NULL",
    }
    ALL_FIELDS = [PK_COLUMN] + FORM_COLUMNS

    if not sql_editor_mode:
        # =========================================================
        # وضع البناء التفاعلي (Builder) - شبيه بـ ArcGIS Select By Attributes
        # =========================================================
        if "sql_clauses" not in st.session_state:
            st.session_state["sql_clauses"] = [{"field": ALL_FIELDS[0], "operator": "is equal to", "value": "", "bool_op": "And"}]

        clauses = st.session_state["sql_clauses"]

        for i, clause in enumerate(clauses):
            row = st.columns([1, 2.5, 2.5, 3, 0.6])
            with row[0]:
                if i == 0:
                    st.markdown("<div style='padding-top:8px'><b>Where</b></div>", unsafe_allow_html=True)
                else:
                    clause["bool_op"] = st.selectbox(
                        " ", ["And", "Or"], index=["And", "Or"].index(clause.get("bool_op", "And")),
                        key=f"bool_{i}", label_visibility="collapsed",
                    )
            with row[1]:
                clause["field"] = st.selectbox(
                    " ", ALL_FIELDS, index=ALL_FIELDS.index(clause["field"]) if clause["field"] in ALL_FIELDS else 0,
                    key=f"field_{i}", label_visibility="collapsed",
                )
            with row[2]:
                op_names = list(OPERATORS.keys())
                clause["operator"] = st.selectbox(
                    " ", op_names, index=op_names.index(clause.get("operator", "is equal to")),
                    key=f"op_{i}", label_visibility="collapsed",
                )
            with row[3]:
                if OPERATORS[clause["operator"]] not in ("IS NULL", "IS NOT NULL"):
                    clause["value"] = st.text_input(
                        " ", value=clause.get("value", ""), key=f"val_{i}", label_visibility="collapsed",
                    )
                else:
                    st.write("")
            with row[4]:
                st.write("")
                if len(clauses) > 1 and st.button("✖", key=f"remove_{i}"):
                    clauses.pop(i)
                    st.rerun()

        if st.button("➕ Add Clause"):
            clauses.append({"field": ALL_FIELDS[0], "operator": "is equal to", "value": "", "bool_op": "And"})
            st.rerun()

        invert = st.checkbox("Invert Where Clause", key="sql_invert")

        # ---- بناء SQL من الشروط ----
        where_parts = []
        params = {}
        for i, clause in enumerate(clauses):
            op_key = OPERATORS[clause["operator"]]
            field = clause["field"]
            prefix = "" if i == 0 else f' {clause.get("bool_op", "And").upper()} '
            if op_key in ("IS NULL", "IS NOT NULL"):
                part = f'"{field}" {op_key}'
            elif op_key == "CONTAINS":
                pname = f"val{i}"
                part = f'"{field}"::text ILIKE :{pname}'
                params[pname] = f"%{clause['value']}%"
            elif op_key == "STARTS_WITH":
                pname = f"val{i}"
                part = f'"{field}"::text ILIKE :{pname}'
                params[pname] = f"{clause['value']}%"
            else:
                pname = f"val{i}"
                part = f'"{field}" {op_key} :{pname}'
                params[pname] = clause["value"]
            where_parts.append(prefix + part)

        where_sql = "".join(where_parts) if where_parts else "1=1"
        if invert:
            where_sql = f"NOT ({where_sql})"

        final_sql = f"SELECT * FROM {TABLE_NAME} WHERE {where_sql} LIMIT 500"
        st.code(final_sql, language="sql")

    else:
        # =========================================================
        # وضع الكتابة اليدوية (SQL Editor)
        # =========================================================
        default_sql = f"SELECT * FROM {TABLE_NAME} ORDER BY {PK_COLUMN} DESC LIMIT 100"
        final_sql = st.text_area("SQL:", value=default_sql, height=150, key="custom_sql")
        params = {}

    st.divider()
    col_apply, col_ok = st.columns(2)
    with col_apply:
        run_sql = st.button("▶️ Apply", type="primary", use_container_width=True)
    with col_ok:
        run_sql_ok = st.button("✅ OK (تنفيذ وإغلاق النتيجة السابقة)", use_container_width=True)

    if run_sql or run_sql_ok:
        cleaned = final_sql.strip().strip(";")
        if not cleaned.lower().startswith("select"):
            st.error("❌ مسموح بس بأوامر SELECT من هذا التبويب (حماية من التعديل غير المقصود بالبيانات).")
        else:
            try:
                result_df = _fetch_df(cleaned, params)
                st.session_state["sql_last_result"] = result_df
                st.success(f"✅ تم التنفيذ - {len(result_df)} صف")
            except Exception as e:
                st.session_state.pop("sql_last_result", None)
                st.error(f"❌ خطأ بتنفيذ الاستعلام: {e}")

    if st.session_state.get("sql_last_result") is not None:
        result_df = st.session_state["sql_last_result"]
        st.dataframe(result_df, use_container_width=True)
        csv_bytes = result_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ Export Selection (CSV)", data=csv_bytes,
            file_name="query_result.csv", mime="text/csv",
        )

# ---------------- تبويب الاستيراد الجماعي ----------------
with tab_import:
    st.subheader("📤 استيراد عدة نقاط دفعة وحدة")
    st.caption("ارفع ملف Excel/CSV أو Shapefile (كملف ZIP) يحتوي عدة نقاط، وحدد نظام الإحداثيات وربط الأعمدة، ثم استورد الكل مرة وحدة.")

    import_source = st.radio(
        "مصدر البيانات:", ["📊 Excel / CSV", "🗺️ Shapefile (ملف ZIP)"],
        horizontal=True, key="import_source_type",
    )

    SRID_OPTIONS = {
        "WGS84 (4326) - إحداثيات lat/long عادية بالدرجات": 4326,
        "Ain el Abd 1970 UTM Zone 38N (20438) - نفس نظام الجدول": 20438,
    }

    # =====================================================
    # الاستيراد من Excel / CSV
    # =====================================================
    if import_source == "📊 Excel / CSV":
        uploaded_file = st.file_uploader(
            "ارفع ملف Excel (.xlsx) أو CSV", type=["xlsx", "xls", "csv"], key="excel_uploader"
        )

        if uploaded_file:
            try:
                if uploaded_file.name.lower().endswith(".csv"):
                    raw_df = pd.read_csv(uploaded_file)
                else:
                    raw_df = pd.read_excel(uploaded_file)

                st.success(f"✅ تم تحميل الملف - {len(raw_df)} صف")
                st.dataframe(raw_df.head(20), use_container_width=True)

                available_cols = raw_df.columns.tolist()

                st.markdown("#### 1️⃣ حدد أعمدة الإحداثيات ونظامها")
                c1, c2, c3 = st.columns(3)
                with c1:
                    x_col = st.selectbox("عمود X / Longitude", available_cols, key="excel_x_col")
                with c2:
                    y_col = st.selectbox("عمود Y / Latitude", available_cols, key="excel_y_col")
                with c3:
                    srid_label = st.selectbox("نظام إحداثيات الملف", list(SRID_OPTIONS.keys()), key="excel_srid")
                source_srid = SRID_OPTIONS[srid_label]

                st.markdown("#### 2️⃣ اربط أعمدة الملف بحقول الجدول (اختياري)")
                st.caption("لو أسماء الأعمدة بالملف مطابقة لأسماء الحقول، بيتم ربطها تلقائيًا.")
                field_mapping = {}
                map_cols = st.columns(3)
                for i, form_col in enumerate(FORM_COLUMNS):
                    options = ["-- تجاهل --"] + available_cols
                    default_idx = options.index(form_col) if form_col in available_cols else 0
                    with map_cols[i % 3]:
                        field_mapping[form_col] = st.selectbox(
                            form_col, options, index=default_idx, key=f"excel_map_{form_col}"
                        )

                st.divider()
                if st.button("🚀 استيراد كل النقاط", type="primary", key="excel_import_btn"):
                    records = []
                    for _, row in raw_df.iterrows():
                        rec = {}
                        try:
                            rec["x"] = float(row[x_col])
                            rec["y"] = float(row[y_col])
                        except (ValueError, TypeError):
                            continue  # نتجاهل صفوف بدون إحداثيات صحيحة
                        for form_col, src_col in field_mapping.items():
                            if src_col != "-- تجاهل --" and pd.notna(row[src_col]):
                                rec[form_col] = str(row[src_col])
                        records.append(rec)

                    if not records:
                        st.error("❌ ما فيه صفوف تحتوي إحداثيات صالحة للاستيراد")
                    else:
                        with st.spinner(f"جاري استيراد {len(records)} نقطة..."):
                            success_count, errors = bulk_insert_points(records, source_srid)

                        st.success(f"✅ تم استيراد {success_count} من {len(records)} نقطة بنجاح")
                        if errors:
                            st.warning(f"⚠️ فشل استيراد {len(errors)} صف")
                            with st.expander("عرض تفاصيل الأخطاء"):
                                for row_num, err in errors:
                                    st.text(f"صف {row_num}: {err}")

                        load_map_data.clear()
                        load_points_in_bounds.clear()
                        search_points.clear()

            except Exception as e:
                st.error(f"❌ خطأ بقراءة الملف: {e}")

    # =====================================================
    # الاستيراد من Shapefile (ZIP)
    # =====================================================
    else:
        if not SHAPEFILE_AVAILABLE:
            st.error("❌ مكتبة قراءة الشيب فايل (pyshp) مو مثبتة. أضف السطر التالي لملف requirements.txt:\n\n`pyshp`\n\nثم أعد نشر التطبيق.")
        else:
            st.caption("ارفع ملف ZIP يحتوي أطراف الشيب فايل: .shp و .shx و .dbf على الأقل (نفس الاسم لكل الملفات). مدعوم بس نوع Point.")
            uploaded_zip = st.file_uploader("ارفع ملف ZIP", type=["zip"], key="shp_uploader")

            if uploaded_zip:
                try:
                    zf = zipfile.ZipFile(uploaded_zip)
                    names = zf.namelist()

                    shp_name = next((n for n in names if n.lower().endswith(".shp")), None)
                    shx_name = next((n for n in names if n.lower().endswith(".shx")), None)
                    dbf_name = next((n for n in names if n.lower().endswith(".dbf")), None)

                    if not (shp_name and dbf_name):
                        st.error("❌ الملف ما يحتوي .shp و .dbf المطلوبين")
                    else:
                        shp_io = io.BytesIO(zf.read(shp_name))
                        dbf_io = io.BytesIO(zf.read(dbf_name))
                        shx_io = io.BytesIO(zf.read(shx_name)) if shx_name else None

                        reader = shapefile.Reader(shp=shp_io, shx=shx_io, dbf=dbf_io)

                        if reader.shapeType not in (shapefile.POINT, shapefile.POINTZ, shapefile.POINTM):
                            st.error(f"❌ نوع الشيب فايل غير مدعوم حاليًا (لازم يكون Point). النوع الحالي: {reader.shapeTypeName}")
                        else:
                            field_names = [f[0] for f in reader.fields[1:]]  # أول عنصر هو DeletionFlag، نتجاهله
                            records_raw = []
                            for shape_rec in reader.iterShapeRecords():
                                geom = shape_rec.shape
                                if not geom.points:
                                    continue
                                x, y = geom.points[0]
                                row = dict(zip(field_names, shape_rec.record))
                                row["_x"] = x
                                row["_y"] = y
                                records_raw.append(row)

                            shp_df = pd.DataFrame(records_raw)
                            st.success(f"✅ تم تحميل الشيب فايل - {len(shp_df)} نقطة")
                            st.dataframe(shp_df.head(20), use_container_width=True)

                            st.markdown("#### 1️⃣ حدد نظام إحداثيات الشيب فايل")
                            srid_label = st.selectbox("نظام الإحداثيات", list(SRID_OPTIONS.keys()), key="shp_srid")
                            source_srid = SRID_OPTIONS[srid_label]
                            st.caption("💡 تأكد من نظام الإحداثيات الصحيح للشيب فايل (تقدر تشوفه بملف .prj لو موجود) عشان النقاط ما تنزل بمكان غلط.")

                            st.markdown("#### 2️⃣ اربط حقول الشيب فايل بحقول الجدول (اختياري)")
                            field_mapping = {}
                            map_cols = st.columns(3)
                            for i, form_col in enumerate(FORM_COLUMNS):
                                options = ["-- تجاهل --"] + field_names
                                default_idx = options.index(form_col) if form_col in field_names else 0
                                with map_cols[i % 3]:
                                    field_mapping[form_col] = st.selectbox(
                                        form_col, options, index=default_idx, key=f"shp_map_{form_col}"
                                    )

                            st.divider()
                            if st.button("🚀 استيراد كل النقاط", type="primary", key="shp_import_btn"):
                                records = []
                                for row in records_raw:
                                    rec = {"x": row["_x"], "y": row["_y"]}
                                    for form_col, src_field in field_mapping.items():
                                        if src_field != "-- تجاهل --" and row.get(src_field) not in (None, ""):
                                            rec[form_col] = str(row[src_field])
                                    records.append(rec)

                                with st.spinner(f"جاري استيراد {len(records)} نقطة..."):
                                    success_count, errors = bulk_insert_points(records, source_srid)

                                st.success(f"✅ تم استيراد {success_count} من {len(records)} نقطة بنجاح")
                                if errors:
                                    st.warning(f"⚠️ فشل استيراد {len(errors)} صف")
                                    with st.expander("عرض تفاصيل الأخطاء"):
                                        for row_num, err in errors:
                                            st.text(f"صف {row_num}: {err}")

                                load_map_data.clear()
                                load_points_in_bounds.clear()
                                search_points.clear()

                except Exception as e:
                    st.error(f"❌ خطأ بقراءة ملف الشيب فايل: {e}")

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
            form_values[col] = render_field_input(col, "", key=f"add_{col}")

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
    st.subheader("✏️ تعديل نقطة (من كامل الجدول)")
    st.caption(f"أدخل رقم {PK_COLUMN} للنقطة اللي تبي تعدلها - يشتغل مع أي نقطة بالجدول، وتظهر لك كل حقول الجدول للتعديل + الموقع (الإحداثيات).")

    flash_msg = st.session_state.pop("edit_flash", None)
    if flash_msg:
        st.success(flash_msg)

    id_col, btn_col = st.columns([2, 1])
    with id_col:
        selected_id = st.number_input(
            f"رقم {PK_COLUMN}", min_value=0, step=1, value=0, key="edit_id_input"
        )
    with btn_col:
        st.write("")
        st.write("")
        load_clicked = st.button("🔍 تحميل بيانات النقطة", use_container_width=True)

    if load_clicked:
        if selected_id <= 0:
            st.warning("⚠️ أدخل رقم صحيح أكبر من صفر")
            st.session_state.pop("edit_loaded_row", None)
        else:
            try:
                result = get_point_full(int(selected_id))
                if result is None:
                    st.error(f"❌ ما فيه نقطة بالرقم {selected_id}")
                    st.session_state.pop("edit_loaded_row", None)
                else:
                    st.session_state["edit_loaded_row"] = result
                    st.session_state["edit_loaded_id"] = int(selected_id)
                    # رقم إصدار يتغير مع كل تحميل عشان مفاتيح الحقول تتجدد ولا تحتفظ بقيم قديمة
                    st.session_state["edit_ver"] = st.session_state.get("edit_ver", 0) + 1
            except Exception as e:
                st.error(f"❌ خطأ: {e}")
                st.session_state.pop("edit_loaded_row", None)

    # نعرض نموذج التعديل بس لو فيه نقطة محمّلة فعليًا وتطابق الرقم المدخل حاليًا
    if st.session_state.get("edit_loaded_row") and st.session_state.get("edit_loaded_id") == int(selected_id):
        row = st.session_state["edit_loaded_row"]
        loaded_id = st.session_state["edit_loaded_id"]
        ver = st.session_state.get("edit_ver", 0)
        kp = f"edit_{loaded_id}_{ver}"  # بادئة مفاتيح الحقول

        editable_cols = [c for c in get_columns_info() if c["editable"]]
        col_types = {c["name"]: c["type"] for c in editable_cols}

        st.info(f"📝 بيانات النقطة رقم {loaded_id} - عدّل أي حقل تبيه، والحقول اللي ما تغيّرها تبقى كما هي")

        # ---------- 1) الموقع (الإحداثيات) ----------
        st.markdown("#### 📍 الموقع (الإحداثيات)")
        has_geom = row.get("__lat") is not None and row.get("__lng") is not None
        orig_lat = float(row["__lat"]) if has_geom else DEFAULT_LAT
        orig_lng = float(row["__lng"]) if has_geom else DEFAULT_LNG

        set_geom = False
        if not has_geom:
            st.warning("⚠️ هذي النقطة ما لها موقع مسجّل. فعّل الخيار تحت لو تبي تحدد لها موقع.")
            set_geom = st.checkbox("تحديد موقع لهذه النقطة", key=f"{kp}_setgeom")

        lat_c, lng_c = st.columns(2)
        with lat_c:
            new_lat = st.number_input(
                "خط العرض (Latitude)", value=orig_lat, format="%.6f", step=0.0001,
                min_value=-90.0, max_value=90.0, key=f"{kp}_coord_lat",
            )
        with lng_c:
            new_lng = st.number_input(
                "خط الطول (Longitude)", value=orig_lng, format="%.6f", step=0.0001,
                min_value=-180.0, max_value=180.0, key=f"{kp}_coord_lng",
            )

        coords_changed = (
            (has_geom and (abs(new_lat - orig_lat) > 5e-7 or abs(new_lng - orig_lng) > 5e-7))
            or (not has_geom and set_geom)
        )

        sync_latlong = False
        if "lat" in col_types and "long" in col_types:
            sync_latlong = st.checkbox(
                "تحديث عمودي lat و long تلقائيًا لو تغيّر الموقع", value=True, key=f"{kp}_sync",
            )

        # معاينة الموقع على خريطة صغيرة (الأزرق = الحالي المسجّل، الأحمر = الجديد)
        preview = folium.Map(location=[new_lat, new_lng], zoom_start=17, prefer_canvas=True, tiles=None)
        folium.TileLayer("OpenStreetMap", name="🗺️ شوارع", overlay=False).add_to(preview)
        folium.TileLayer(
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Esri World Imagery", name="🛰️ صورة جوية", overlay=False,
        ).add_to(preview)
        if has_geom:
            folium.CircleMarker(
                [orig_lat, orig_lng], radius=8, color="#1a73e8", fill=True,
                fill_opacity=0.9, tooltip="الموقع الحالي المسجّل",
            ).add_to(preview)
        if coords_changed:
            folium.Marker(
                [new_lat, new_lng], tooltip="الموقع الجديد",
                icon=folium.Icon(color="red", icon="map-marker", prefix="fa"),
            ).add_to(preview)
        folium.LayerControl(position="topright").add_to(preview)
        st_folium(preview, width="100%", height=300, returned_objects=[], key=f"{kp}_preview")

        if coords_changed:
            st.caption("🔴 تم تغيير الموقع - سيتم حفظه عند الضغط على «حفظ التعديلات».")
        else:
            st.caption("🔵 الموقع الحالي المسجّل. غيّر الإحداثيات فوق لو تبي تصحح الموقع.")

        # ---------- 2) كل حقول الجدول ----------
        # مهم: مفتاح كل حقل مربوط برقم النقطة ورقم الإصدار، عشان ما تظهر قيم نقطة سابقة.
        with st.form(f"edit_form_{loaded_id}_{ver}"):
            st.markdown("#### 🗂️ بيانات النقطة (كل حقول الجدول)")
            edit_values = {}

            short_cols, long_cols = [], []
            for c in editable_cols:
                val = _to_text(row.get(c["name"]))
                if c["name"] in LONG_TEXT_COLUMNS or len(val) > 80 or "\n" in val:
                    long_cols.append(c)
                else:
                    short_cols.append(c)

            grid = st.columns(2)
            for i, c in enumerate(short_cols):
                with grid[i % 2]:
                    edit_values[c["name"]] = st.text_input(
                        c["name"], value=_to_text(row.get(c["name"])),
                        key=f"{kp}_field_{c['name']}", help=f"نوع الحقل: {c['type']}",
                    )
            for c in long_cols:
                edit_values[c["name"]] = st.text_area(
                    c["name"], value=_to_text(row.get(c["name"])),
                    key=f"{kp}_field_{c['name']}", height=110, help=f"نوع الحقل: {c['type']}",
                )

            update_submitted = st.form_submit_button("💾 حفظ التعديلات", type="primary")

        if update_submitted:
            # نرسل للقاعدة الحقول اللي تغيّرت فقط (أسلم وأسرع)
            changes = {}
            for name, new_val in edit_values.items():
                if new_val != _to_text(row.get(name)):
                    changes[name] = new_val if new_val.strip() != "" else None

            if coords_changed and sync_latlong:
                changes.setdefault("lat", f"{new_lat:.6f}")
                changes.setdefault("long", f"{new_lng:.6f}")

            if not changes and not coords_changed:
                st.info("ما فيه أي تغييرات للحفظ")
            else:
                try:
                    update_point_full(
                        loaded_id, changes, col_types,
                        new_coords=(new_lat, new_lng) if coords_changed else None,
                    )
                    load_map_data.clear()
                    load_points_in_bounds.clear()
                    search_points.clear()
                    st.session_state["edit_loaded_row"] = get_point_full(loaded_id)
                    st.session_state["edit_ver"] = ver + 1
                    parts = []
                    if changes:
                        parts.append(f"{len(changes)} حقل")
                    if coords_changed:
                        parts.append("الموقع")
                    st.session_state["edit_flash"] = f"✅ تم حفظ التعديلات بنجاح ({' + '.join(parts)})"
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ فشل التعديل: {e}")

# ---------------- تبويب الحذف ----------------
with tab_delete:
    st.subheader("🗑️ حذف نقطة أو عدة نقاط")

    st.markdown("#### 1️⃣ اختر من آخر 200 نقطة (اختياري)")
    try:
        df_del = load_data()
        multiselect_ids = []
        if not df_del.empty:
            multiselect_ids = st.multiselect(
                f"اختر عدة أرقام {PK_COLUMN} للحذف",
                df_del[PK_COLUMN].tolist(),
                key="delete_multiselect",
            )
        else:
            st.info("ما فيه بيانات حاليًا")
    except Exception as e:
        multiselect_ids = []
        st.error(f"خطأ بجلب البيانات: {e}")

    st.markdown("#### 2️⃣ أو اكتب أرقام يدويًا (يشتغل مع أي نقطة بكامل الجدول)")
    manual_ids_text = st.text_input(
        f"أرقام {PK_COLUMN} مفصولة بفاصلة (مثال: 5, 102, 900)", key="delete_manual_ids"
    )

    # ندمج الأرقام من الطريقتين (المختارة + المكتوبة يدويًا) بدون تكرار
    manual_ids = []
    if manual_ids_text.strip():
        for part in manual_ids_text.split(","):
            part = part.strip()
            if part.isdigit():
                manual_ids.append(int(part))

    all_ids_to_delete = sorted(set(multiselect_ids) | set(manual_ids))

    if all_ids_to_delete:
        st.markdown("#### 3️⃣ تأكيد الحذف")
        st.warning(f"⚠️ راح يتم حذف **{len(all_ids_to_delete)}** نقطة: {', '.join(map(str, all_ids_to_delete))}")
        st.caption("هذا الإجراء لا يمكن التراجع عنه")

        confirm = st.checkbox("أنا متأكد إني أبي أحذف هذي النقاط", key="delete_confirm_checkbox")

        if st.button("🗑️ تأكيد الحذف النهائي", type="primary", disabled=not confirm):
            try:
                success_count, errors = delete_points(all_ids_to_delete)
                st.success(f"✅ تم حذف {success_count} من {len(all_ids_to_delete)} نقطة بنجاح")
                if errors:
                    st.warning(f"⚠️ فشل حذف {len(errors)} نقطة")
                    with st.expander("عرض تفاصيل الأخطاء"):
                        for pk_val, err in errors:
                            st.text(f"{PK_COLUMN} = {pk_val}: {err}")

                load_map_data.clear()
                load_points_in_bounds.clear()
                search_points.clear()
                st.rerun()
            except Exception as e:
                st.error(f"❌ فشل الحذف: {e}")
    else:
        st.info("اختر أو اكتب رقم/أرقام النقاط اللي تبي تحذفها")

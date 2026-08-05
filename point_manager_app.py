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

import streamlit as st
import pandas as pd
import folium
from folium.plugins import FastMarkerCluster
from streamlit_folium import st_folium
from sqlalchemy import text, create_engine

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


@st.cache_data(ttl=30)
def search_points(query_text, limit=30):
    """يدور على نص معين بأي عمود من أعمدة النموذج (بحث جزئي غير حساس لحالة الأحرف)."""
    if not query_text or not query_text.strip():
        return pd.DataFrame()
    like_conditions = " OR ".join([f'"{col}"::text ILIKE :q' for col in FORM_COLUMNS])
    cols = ", ".join([PK_COLUMN] + FORM_COLUMNS)
    sql = f"""
        SELECT {cols},
               ST_Y(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lat,
               ST_X(ST_Transform({GEOM_COLUMN}, {INPUT_SRID})) AS map_lng
        FROM {TABLE_NAME}
        WHERE {GEOM_COLUMN} IS NOT NULL AND ({like_conditions})
        LIMIT :limit
    """
    params = {"q": f"%{query_text.strip()}%", "limit": limit}
    return _fetch_df(sql, params)


def insert_point(lat, lng, values: dict):
    cols = [GEOM_COLUMN] + list(values.keys())
    col_list = ", ".join(f'"{c}"' for c in cols)
    val_list = ["ST_Transform(ST_SetSRID(ST_MakePoint(:lng, :lat), :input_srid), :table_srid)"] + [f":{k}" for k in values.keys()]
    sql = f'INSERT INTO {TABLE_NAME} ({col_list}) VALUES ({", ".join(val_list)})'
    params = {"lng": lng, "lat": lat, "input_srid": INPUT_SRID, "table_srid": TABLE_SRID, **values}
    _execute(sql, params)


def update_point(pk_value, values: dict):
    set_clause = ", ".join(f'"{k}" = :{k}' for k in values.keys())
    sql = f'UPDATE {TABLE_NAME} SET {set_clause} WHERE {PK_COLUMN} = :pk_value'
    params = {**values, "pk_value": pk_value}
    _execute(sql, params)


def delete_point(pk_value):
    sql = f"DELETE FROM {TABLE_NAME} WHERE {PK_COLUMN} = :pk_value"
    _execute(sql, {"pk_value": pk_value})


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
st.caption("حرّك/كبّر الخريطة عشان تشوف النقاط بمنطقتك، ابحث عن نقطة محددة، أو اضغط على الخريطة لإضافة نقطة جديدة.")

# ---------------------------------------------------------
# مربع البحث
# ---------------------------------------------------------
search_col1, search_col2 = st.columns([4, 1])
with search_col1:
    search_query = st.text_input(
        "🔍 ابحث عن نقطة (بالاسم، الرقم الموحد، الحي...)",
        key="search_box",
        placeholder="مثال: شارع الملك فهد",
    )
with search_col2:
    st.write("")  # محاذاة
    do_search = st.button("بحث", use_container_width=True)

search_results = pd.DataFrame()
if do_search and search_query:
    try:
        search_results = search_points(search_query)
        if search_results.empty:
            st.warning("ما فيه نتائج مطابقة")
        else:
            st.success(f"لقيت {len(search_results)} نتيجة")
    except Exception as e:
        st.error(f"خطأ بالبحث: {e}")

# لو فيه نتيجة بحث، خلي المستخدم يختار وحدة يتوسط عليها الخريطة
if not search_results.empty:
    result_labels = {
        idx: " - ".join(str(row[c]) for c in FORM_COLUMNS if pd.notna(row[c]) and row[c] != "") or f"نقطة {row[PK_COLUMN]}"
        for idx, row in search_results.iterrows()
    }
    selected_idx = st.selectbox(
        "اختر نتيجة للتوسط عليها بالخريطة:",
        options=list(result_labels.keys()),
        format_func=lambda i: result_labels[i],
        key="search_result_select",
    )
    selected_row = search_results.loc[selected_idx]
    st.session_state["focus_location"] = {
        "lat": selected_row["map_lat"], "lng": selected_row["map_lng"]
    }

# ---------------------------------------------------------
# تحديد مركز/تكبير الخريطة:
# 1) لو فيه نتيجة بحث محددة: نتوسط عليها بزوم قريب
# 2) لو المستخدم سوى "تحديث حسب العرض الحالي": نحافظ على نفس مركز وتكبير الخريطة كما هو
# 3) غير كذا: نستخدم قيمة افتراضية (الرياض)
# ---------------------------------------------------------
if st.session_state.get("focus_location"):
    center_lat = st.session_state["focus_location"]["lat"]
    center_lng = st.session_state["focus_location"]["lng"]
    zoom_level = 17
elif st.session_state.get("last_map_center") and st.session_state.get("last_map_zoom") is not None:
    center_lat = st.session_state["last_map_center"]["lat"]
    center_lng = st.session_state["last_map_center"]["lng"]
    zoom_level = st.session_state["last_map_zoom"]
else:
    center_lat, center_lng = 24.7136, 46.6753
    zoom_level = 12

# ---------------------------------------------------------
# جلب النقاط: حسب حدود الخريطة الحالية (Viewport) لو متوفرة، وإلا نستخدم مجموعة افتراضية
# ---------------------------------------------------------
last_bounds = st.session_state.get("last_map_bounds")
try:
    if last_bounds:
        map_df = load_points_in_bounds(
            last_bounds["south"], last_bounds["west"],
            last_bounds["north"], last_bounds["east"],
        )
    else:
        map_df = load_map_data()
except Exception as e:
    map_df = pd.DataFrame()
    st.error(f"خطأ في جلب بيانات الخريطة: {e}")

st.caption(f"📍 عدد النقاط الظاهرة حاليًا: {len(map_df)}")

m = folium.Map(location=[center_lat, center_lng], zoom_start=zoom_level, prefer_canvas=True)

# نستخدم FastMarkerCluster بدل MarkerCluster العادي: يبني الماركرات عن طريق
# كود JS مضغوط جدًا بدل ما ينشئ عنصر HTML/DOM كامل لكل نقطة بشكل منفصل.
# هذا أسرع بشكل ملحوظ مع مئات/آلاف النقاط.
if not map_df.empty:
    # نبني للـ popup أهم عمودين/ثلاثة بس (بدل كل الأعمدة) عشان يفضل حجم البيانات المرسلة صغير وسريع
    popup_cols = FORM_COLUMNS[:3]

    def build_row(row):
        parts = [f"<b>{c}:</b> {row[c]}" for c in popup_cols if pd.notna(row[c]) and row[c] != ""]
        popup_text = "<br>".join(parts) if parts else f"{PK_COLUMN}: {row[PK_COLUMN]}"
        return [row["map_lat"], row["map_lng"], popup_text]

    cluster_data = [build_row(row) for _, row in map_df.iterrows()]

    callback = """
    function (row) {
        var marker = L.circleMarker(new L.LatLng(row[0], row[1]), {
            radius: 7, color: '#1a73e8', fillColor: '#1a73e8',
            fillOpacity: 0.85, weight: 1
        });
        marker.bindPopup(row[2]);
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

# لو فيه موقع جديد محدد (مو محفوظ بعد)، نعرضه بلون مختلف
if st.session_state.get("new_point_location"):
    new_lat = st.session_state["new_point_location"]["lat"]
    new_lng = st.session_state["new_point_location"]["lng"]
    folium.Marker(
        location=[new_lat, new_lng],
        tooltip="نقطة جديدة (لم تُحفظ بعد)",
        icon=folium.Icon(color="red", icon="plus", prefix="fa"),
    ).add_to(m)

try:
    map_output = st_folium(
        m, width="100%", height=550,
        returned_objects=["last_clicked", "bounds", "zoom", "center"],
        key="main_map",
    )
except Exception as e:
    map_output = None
    st.error(f"خطأ في عرض الخريطة: {e}")
    st.info("جرب تحدّث المكتبة: pip install --upgrade streamlit-folium folium")

# حفظ حدود/مركز/تكبير الخريطة الحالية للاستخدام لاحقًا - بدون rerun تلقائي
# (rerun تلقائي مع كل حركة بسيطة كان يسبب ثقل ملحوظ). المستخدم يضغط "تحديث حسب العرض"
# متى ما يحتاج يشوف نقاط منطقة جديدة بعد ما يحرّك/يكبّر الخريطة - ونحافظ على نفس مستوى التكبير.
if map_output:
    if map_output.get("bounds"):
        b = map_output["bounds"]
        st.session_state["pending_bounds"] = {
            "south": b["_southWest"]["lat"], "west": b["_southWest"]["lng"],
            "north": b["_northEast"]["lat"], "east": b["_northEast"]["lng"],
        }
    if map_output.get("zoom") is not None:
        st.session_state["pending_zoom"] = map_output["zoom"]
    if map_output.get("center"):
        st.session_state["pending_center"] = map_output["center"]

refresh_col1, refresh_col2 = st.columns([1, 4])
with refresh_col1:
    if st.button("🔄 تحديث حسب العرض الحالي", use_container_width=True):
        if st.session_state.get("pending_bounds"):
            st.session_state["last_map_bounds"] = st.session_state["pending_bounds"]
        if st.session_state.get("pending_zoom") is not None:
            st.session_state["last_map_zoom"] = st.session_state["pending_zoom"]
        if st.session_state.get("pending_center"):
            st.session_state["last_map_center"] = st.session_state["pending_center"]
        # الضغط على تحديث يعني المستخدم يبي يفضل بنفس منطقة/تكبير الخريطة، مو نتيجة بحث قديمة
        st.session_state.pop("focus_location", None)
        st.rerun()

# التقاط ضغطة المستخدم على الخريطة لإضافة نقطة جديدة
if map_output and map_output.get("last_clicked"):
    clicked_lat = map_output["last_clicked"]["lat"]
    clicked_lng = map_output["last_clicked"]["lng"]
    st.session_state["new_point_location"] = {"lat": clicked_lat, "lng": clicked_lng}
    st.session_state.pop("focus_location", None)

# لو فيه موقع محدد، اعرض نموذج تعبئة البيانات تحت الخريطة
if st.session_state.get("new_point_location"):
    loc = st.session_state["new_point_location"]
    st.info(f"📍 الموقع المحدد: Lat = {loc['lat']:.6f}, Lng = {loc['lng']:.6f}")

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
tab_view, tab_sql, tab_add, tab_edit, tab_delete = st.tabs(
    ["📋 عرض البيانات", "🧮 استعلام SQL", "➕ إضافة نقطة (يدوي)", "✏️ تعديل نقطة", "🗑️ حذف نقطة"]
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
    st.subheader("تعديل نقطة موجودة")
    try:
        df_edit = load_data()
        if not df_edit.empty:
            selected_id = st.selectbox(
                f"اختر {PK_COLUMN}", df_edit[PK_COLUMN].tolist(), key="edit_select"
            )
            row = df_edit[df_edit[PK_COLUMN] == selected_id].iloc[0]

            st.info(f"📝 البيانات الحالية للنقطة رقم {selected_id} - عدّل الحقل اللي تبيه بس واترك الباقي كما هو")

            # مهم: نربط مفتاح كل حقل برقم النقطة نفسها (selected_id)، مو بس باسم العمود.
            # لو المفتاح ثابت بين كل النقاط، Streamlit يحتفظ بالقيمة القديمة اللي كتبتها
            # لنقطة سابقة وما يحدّثها للنقطة الجديدة المختارة.
            with st.form(f"edit_form_{selected_id}"):
                edit_values = {}
                for col in FORM_COLUMNS:
                    current_val = row[col] if pd.notna(row[col]) else ""
                    edit_values[col] = render_field_input(
                        col, str(current_val), key=f"edit_{selected_id}_{col}"
                    )

                update_submitted = st.form_submit_button("حفظ التعديلات")
                if update_submitted:
                    try:
                        update_point(selected_id, edit_values)
                        st.success("✅ تم التعديل بنجاح")
                        load_map_data.clear()
                        load_points_in_bounds.clear()
                        search_points.clear()
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
                    load_map_data.clear()
                    load_points_in_bounds.clear()
                    search_points.clear()
                except Exception as e:
                    st.error(f"❌ فشل الحذف: {e}")
        else:
            st.info("ما فيه بيانات حاليًا")
    except Exception as e:
        st.error(f"خطأ: {e}")

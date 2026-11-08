"""
point_report.py — وحدة تقرير النقطة + تحليل علاقتها بشبكة تصريف السيول
(منطق فقط بدون Streamlit، عشان تنختبر وتنعاد استخدامها بسهولة)

المزايا:
  - قراءة شبكة السيول من: ZIP (Shapefile) / GeoJSON / KML / KMZ / CSV-Excel (عمود WKT)
    + (GPKG / GML / DXF وغيرها لو geopandas مثبتة)
  - تحليل علاقة النقطة بالخطوط: «على خط» / «بجوار خط» / «بعيدة» لكل حالة
    (قائم / تحت التنفيذ / مستقبلي أو مقترح / غير محدد) مع المسافة والاتجاه
  - خريطة تفصيلية بالصورة الجوية كخلفية + خريطة سياق
  - تقرير HTML مستقل قابل للطباعة / الحفظ PDF
"""
import io
import re
import json
import math
import html
import zipfile
import tempfile
import datetime
import os
import xml.etree.ElementTree as ET
from collections import Counter

import pandas as pd
from pyproj import CRS, Transformer
from shapely import wkt as shp_wkt, force_2d
from shapely.geometry import (
    Point, LineString, Polygon, GeometryCollection, shape as shp_shape,
    mapping,
)
from shapely.ops import transform as shp_transform, nearest_points

try:
    import shapefile  # pyshp
    PYSHP_OK = True
except Exception:
    PYSHP_OK = False

try:
    import geopandas as gpd
    GPD_OK = True
except Exception:
    GPD_OK = False

# =========================================================
# ثوابت الحالات
# =========================================================
ST_EXISTING = "قائم"
ST_UNDER = "تحت التنفيذ"
ST_FUTURE = "مستقبلي / مقترح"
ST_UNKNOWN = "غير محدد"
STATUS_ORDER = [ST_EXISTING, ST_UNDER, ST_FUTURE, ST_UNKNOWN]
STATUS_COLORS = {
    ST_EXISTING: "#00b0ff",
    ST_UNDER: "#ff9100",
    ST_FUTURE: "#d500f9",
    ST_UNKNOWN: "#cfd8dc",
}
STATUS_NOUN = {
    ST_EXISTING: "خط سيول قائم",
    ST_UNDER: "خط سيول تحت التنفيذ",
    ST_FUTURE: "خط سيول مستقبلي / مقترح",
    ST_UNKNOWN: "عنصر شبكة سيول (حالته غير محددة)",
}

_KEYWORDS = [
    (ST_UNDER, ["تحت التنفيذ", "قيد التنفيذ", "جاري", "under construction", "construction", "ongoing"]),
    (ST_FUTURE, ["مستقبل", "مقترح", "مخطط", "مستهدف", "future", "propos", "planned", "plan"]),
    (ST_EXISTING, ["قائم", "موجود", "منفذ", "مشغل", "existing", "exist", "built", "operational", "active"]),
]

SUPPORTED_TYPES = ["zip", "geojson", "json"]


def normalize_status(value):
    """يحوّل أي نص حالة (عربي/إنجليزي) لإحدى الحالات الأربع."""
    if value is None:
        return ST_UNKNOWN
    v = str(value).strip().lower()
    if not v or v in ("nan", "none", "null"):
        return ST_UNKNOWN
    if v.startswith("غير") or v.startswith("non") or v.startswith("not "):
        return ST_UNKNOWN
    for status, kws in _KEYWORDS:
        if any(k in v for k in kws):
            return status
    return ST_UNKNOWN


# =========================================================
# قراءة الملفات
# =========================================================
def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, (int, float, str, bool)):
        return v
    if hasattr(v, "item"):
        try:
            return _clean(v.item())
        except Exception:
            pass
    s = str(v)
    return None if s in ("NaT", "nan", "None") else s


def _crs_from_geojson(obj):
    name = ((obj.get("crs") or {}).get("properties") or {}).get("name") if isinstance(obj, dict) else None
    if not name:
        return CRS.from_epsg(4326)
    if "CRS84" in name:
        return CRS.from_epsg(4326)
    m = re.search(r"EPSG\D*(\d{4,6})", name)
    return CRS.from_epsg(int(m.group(1))) if m else CRS.from_epsg(4326)


def _read_geojson(data: bytes):
    obj = json.loads(data.decode("utf-8-sig"))
    t = obj.get("type")
    if t == "FeatureCollection":
        items = obj.get("features", [])
    elif t == "Feature":
        items = [obj]
    else:
        items = [{"type": "Feature", "geometry": obj, "properties": {}}]
    feats = []
    for it in items:
        g = it.get("geometry")
        if not g:
            continue
        geom = force_2d(shp_shape(g))
        if geom.is_empty:
            continue
        props = {k: _clean(v) for k, v in (it.get("properties") or {}).items()}
        feats.append({"geom": geom, "props": props})
    return feats, _crs_from_geojson(obj)


def _read_kml(data: bytes):
    root = ET.fromstring(data)

    def ln(el):
        return el.tag.split("}")[-1]

    def coords(text):
        pts = []
        for tok in (text or "").split():
            p = tok.split(",")
            if len(p) >= 2:
                pts.append((float(p[0]), float(p[1])))
        return pts

    def geom_of(el):
        n = ln(el)
        if n == "Point":
            return Point(coords(el.find(".//{*}coordinates").text)[0])
        if n == "LineString":
            return LineString(coords(el.find(".//{*}coordinates").text))
        if n == "Polygon":
            c = el.find("{*}outerBoundaryIs//{*}coordinates")
            return Polygon(coords(c.text))
        if n == "MultiGeometry":
            gs = [geom_of(c) for c in el if ln(c) in ("Point", "LineString", "Polygon", "MultiGeometry")]
            return GeometryCollection([g for g in gs if g is not None])
        return None

    feats = []
    for pm in root.iter():
        if ln(pm) != "Placemark":
            continue
        props = {}
        nm = pm.find("{*}name")
        if nm is not None and nm.text:
            props["name"] = nm.text.strip()
        ds = pm.find("{*}description")
        if ds is not None and ds.text:
            props["description"] = re.sub(r"<[^>]+>", " ", ds.text).strip()[:150]
        for el in pm.iter():
            if ln(el) == "Data" and el.get("name"):
                props[el.get("name")] = el.findtext("{*}value")
            elif ln(el) == "SimpleData" and el.get("name"):
                props[el.get("name")] = el.text
        for child in pm:
            if ln(child) in ("Point", "LineString", "Polygon", "MultiGeometry"):
                try:
                    g = geom_of(child)
                except Exception:
                    g = None
                if g is not None and not g.is_empty:
                    feats.append({"geom": force_2d(g), "props": props})
    return feats, CRS.from_epsg(4326)


def _read_shp_zip(zf: zipfile.ZipFile):
    names = zf.namelist()
    shp = next(n for n in names if n.lower().endswith(".shp"))
    stem = shp[:-4]

    def find(ext):
        return next((n for n in names if n.lower() == (stem + ext).lower()), None)

    dbf, shx, prj = find(".dbf"), find(".shx"), find(".prj")
    crs = None
    if prj:
        try:
            crs = CRS.from_wkt(zf.read(prj).decode("utf-8", "ignore"))
        except Exception:
            crs = None

    if PYSHP_OK and dbf:
        for enc in ("utf-8", "cp1256", "latin-1"):
            try:
                r = shapefile.Reader(
                    shp=io.BytesIO(zf.read(shp)),
                    shx=io.BytesIO(zf.read(shx)) if shx else None,
                    dbf=io.BytesIO(zf.read(dbf)), encoding=enc,
                )
                fields = [f[0] for f in r.fields[1:]]
                feats = []
                for sr in r.iterShapeRecords():
                    if sr.shape.shapeType == 0:
                        continue
                    geom = force_2d(shp_shape(sr.shape.__geo_interface__))
                    if geom.is_empty:
                        continue
                    feats.append({"geom": geom, "props": dict(zip(fields, [_clean(v) for v in sr.record]))})
                return feats, crs
            except UnicodeDecodeError:
                continue
    if GPD_OK:
        with tempfile.TemporaryDirectory() as td:
            zf.extractall(td)
            return _read_with_gpd(os.path.join(td, shp), shp_encoding="utf-8")
    raise ValueError("لقراءة الشيب فايل ثبّت المكتبة: pyshp")


def _read_with_gpd(path, shp_encoding=None):
    kw = {"encoding": shp_encoding} if shp_encoding else {}
    gdf = gpd.read_file(path, **kw)
    gname = gdf.geometry.name
    feats = []
    for _, row in gdf.iterrows():
        g = row[gname]
        if g is None or g.is_empty:
            continue
        feats.append({"geom": force_2d(g), "props": {k: _clean(v) for k, v in row.items() if k != gname}})
    return feats, gdf.crs


def _read_table(filename, data: bytes):
    df = pd.read_csv(io.BytesIO(data)) if filename.lower().endswith(".csv") else pd.read_excel(io.BytesIO(data))
    lower = {str(c).lower(): c for c in df.columns}
    wkt_col = next((lower[k] for k in ("wkt", "geometry", "geom", "the_geom", "shape") if k in lower), None)
    feats = []
    if wkt_col is not None:
        for _, row in df.iterrows():
            try:
                g = shp_wkt.loads(str(row[wkt_col]))
            except Exception:
                continue
            if g.is_empty:
                continue
            feats.append({"geom": force_2d(g),
                          "props": {k: _clean(v) for k, v in row.items() if k != wkt_col}})
    else:
        xc = next((lower[k] for k in ("x", "lon", "lng", "long", "longitude") if k in lower), None)
        yc = next((lower[k] for k in ("y", "lat", "latitude") if k in lower), None)
        if xc is None or yc is None:
            raise ValueError("ملف الجدول لازم يحتوي عمود WKT (للخطوط) أو أعمدة X/Y (للنقاط)")
        for _, row in df.iterrows():
            try:
                g = Point(float(row[xc]), float(row[yc]))
            except Exception:
                continue
            feats.append({"geom": g, "props": {k: _clean(v) for k, v in row.items()}})
    return feats, None  # النظام غير معروف: المستخدم يحدده


def read_network_file(filename: str, data: bytes):
    """يقرأ ملف شبكة سيول ويرجع dict: features, crs (أو None), fields, geom_types."""
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext in ("geojson", "json"):
        feats, crs = _read_geojson(data)
    elif ext == "kml":
        feats, crs = _read_kml(data)
    elif ext == "kmz":
        zf = zipfile.ZipFile(io.BytesIO(data))
        kml = next((n for n in zf.namelist() if n.lower().endswith(".kml")), None)
        if not kml:
            raise ValueError("ملف KMZ ما يحتوي KML")
        feats, crs = _read_kml(zf.read(kml))
    elif ext == "zip":
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        if any(n.lower().endswith(".shp") for n in names):
            feats, crs = _read_shp_zip(zf)
        else:
            inner = next((n for n in names if n.lower().rsplit(".", 1)[-1] in
                          ("geojson", "json", "kml", "gpkg", "gml", "dxf")), None)
            if not inner:
                raise ValueError("ما لقيت داخل ZIP ملف شيب فايل (.shp) ولا أي صيغة مدعومة")
            return read_network_file(inner, zf.read(inner))
    elif ext in ("csv", "xlsx", "xls"):
        feats, crs = _read_table(filename, data)
    elif ext in ("gpkg", "gml", "dxf"):
        if not GPD_OK:
            raise ValueError(f"صيغة .{ext} تحتاج مكتبة geopandas — أضفها لـ requirements.txt أو حوّل الملف لـ GeoJSON/Shapefile")
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "in." + ext)
            with open(p, "wb") as f:
                f.write(data)
            feats, crs = _read_with_gpd(p)
    else:
        raise ValueError(f"صيغة غير مدعومة: .{ext}")

    if not feats:
        raise ValueError("الملف ما يحتوي أي عناصر جيومترية صالحة")
    fields = []
    for f in feats[:2000]:
        for k in f["props"]:
            if k not in fields:
                fields.append(k)
    return {
        "features": feats, "crs": crs, "fields": fields,
        "geom_types": dict(Counter(f["geom"].geom_type for f in feats)),
    }


# =========================================================
# تجهيز الطبقة والتحليل (مبسّط: الحالة = قيمة حقل case)
# =========================================================
import textwrap
import concurrent.futures as _cf
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from PIL import Image, ImageEnhance
from matplotlib import patheffects as pe
import arabic_reshaper
from bidi.algorithm import get_display

NO_CASE = "غير محدد"
PALETTE = ["#00b0ff", "#ff9100", "#d500f9", "#00e676", "#ffea00", "#ff4081", "#8d6e63", "#b0bec5"]


def find_case_field(fields):
    return next((f for f in fields if str(f).strip().lower() == "case"), None)


def prepare_layer(raw, crs, case_field, metric_epsg=20438):
    crs = CRS.from_user_input(crs)
    to_m = Transformer.from_crs(crs, metric_epsg, always_xy=True).transform
    to_w = Transformer.from_crs(crs, 4326, always_xy=True).transform
    out = []
    for f in raw["features"]:
        try:
            gm = shp_transform(to_m, f["geom"])
            gw = shp_transform(to_w, f["geom"])
            if not (math.isfinite(gm.bounds[0]) and math.isfinite(gw.bounds[0])):
                continue
        except Exception:
            continue
        v = f["props"].get(case_field) if case_field else None
        case = str(v).strip() if v not in (None, "") else NO_CASE
        out.append({"geom_m": gm, "geom_w": gw, "case": case})
    return out


def case_colors(features):
    cases = sorted({f["case"] for f in features})
    colors, used = {}, set()
    for c in cases:
        st = normalize_status(c)
        pref = {"قائم": "#00b0ff", "تحت التنفيذ": "#ff9100", "مستقبلي / مقترح": "#d500f9"}.get(st)
        if pref and pref not in used:
            colors[c] = pref
            used.add(pref)
    free = [p for p in PALETTE if p not in used]
    for c in cases:
        if c not in colors:
            colors[c] = free.pop(0) if free else "#90a4ae"
    return colors


def _bearing_ar(dx, dy):
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return "—"
    ang = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    names = ["شمال", "شمال شرق", "شرق", "جنوب شرق", "جنوب", "جنوب غرب", "غرب", "شمال غرب"]
    return names[int((ang + 22.5) // 45) % 8]


def analyze_point(lat, lng, features, on_tol=3.0, near_tol=25.0, metric_epsg=20438):
    to_m = Transformer.from_crs(4326, metric_epsg, always_xy=True)
    px, py = to_m.transform(lng, lat)
    pt = Point(px, py)
    best = {}
    for f in features:
        d = f["geom_m"].distance(pt)
        f["dist"] = d
        if f["case"] not in best or d < best[f["case"]][0]:
            best[f["case"]] = (d, f)
    lines, rows = [], []
    for case, (d, f) in sorted(best.items(), key=lambda kv: kv[1][0]):
        n = nearest_points(f["geom_m"], pt)[0]
        direction = _bearing_ar(n.x - px, n.y - py)
        rows.append({"case": case, "dist": d, "dir": direction})
        if d <= on_tol:
            lines.append(f"على شبكة سيول ({case}) — المسافة {d:.1f} م")
        elif d <= near_tol:
            lines.append(f"بجوار شبكة سيول ({case}) — تبعد {d:.1f} م باتجاه {direction}")
    if not features:
        headline, lines = "لم تُحمَّل بيانات شبكة السيول", []
    elif lines:
        headline = "علاقة النقطة بشبكة السيول"
    else:
        r = rows[0]
        headline = "لا توجد شبكة سيول بجوار النقطة"
        lines = [f"أقرب شبكة ({r['case']}) على بعد {r['dist']:.0f} م باتجاه {r['dir']}"]
    return {"lat": lat, "lng": lng, "headline": headline, "lines": lines, "rows": rows,
            "features": features, "on_tol": on_tol, "near_tol": near_tol,
            "nearest": rows[0]["dist"] if rows else None}


# =========================================================
# الخريطة الثابتة (صورة جوية) + صفحة التقرير PNG/PDF
# =========================================================
ESRI_IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


try:
    from matplotlib import ft2font as _ft
    _RAQM = bool(getattr(_ft, "__libraqm_version__", None))  # نسخ matplotlib الحديثة تشكّل العربي وتعالج الاتجاه بنفسها
except Exception:
    _RAQM = False


def ar(text):
    text = str(text)
    return text if _RAQM else get_display(arabic_reshaper.reshape(text))


def _merc(lon, lat, z):
    n = 256 * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def _fetch_tile(url, z, x, y):
    try:
        r = requests.get(url.format(z=z, x=x, y=y), timeout=10, headers={"User-Agent": "point-manager/1.0"})
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None


def _tile_crop(url, lat, lng, z, half_w, half_h):
    """يجمّع التايلات ويقص صورة مركزها (lat,lng) بنصف عرض/ارتفاع بالبكسل. يرجع (صورة, (x0,y0), نجاح)"""
    cx, cy = _merc(lng, lat, z)
    x0, x1, y0, y1 = cx - half_w, cx + half_w, cy - half_h, cy + half_h
    n = 2 ** z
    tx0, tx1 = int(x0 // 256), int(x1 // 256)
    ty0, ty1 = max(int(y0 // 256), 0), min(int(y1 // 256), n - 1)
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (205, 210, 215))
    jobs = [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)]
    ok = 0
    with _cf.ThreadPoolExecutor(8) as ex:
        for (tx, ty), img in zip(jobs, ex.map(lambda t: _fetch_tile(url, z, t[0] % n, t[1]), jobs)):
            if img is not None:
                canvas.paste(img, ((tx - tx0) * 256, (ty - ty0) * 256))
                ok += 1
    crop = canvas.crop((int(x0 - tx0 * 256), int(y0 - max(ty0, 0) * 256), int(x1 - tx0 * 256), int(y1 - max(ty0, 0) * 256)))
    return crop, (x0, y0), ok > 0


def _aerial_background(lat, lng, radius_m, aspect, opacity=1.0, streets=False):
    """خلفية الخريطة: صورة جوية فوق خريطة شوارع (OSM) أو فوق الأبيض.
    opacity = عتامة الصورة الجوية (1 = معتمة تمامًا، 0 = شفافة تمامًا فتظهر الشوارع فقط)."""
    z = int(math.floor(math.log2(156543.03 * math.cos(math.radians(lat)) * 1500 / (2 * radius_m))))
    z = max(1, min(19, z))
    mpp = 156543.03 * math.cos(math.radians(lat)) / 2 ** z
    hw = radius_m / mpp
    crop, origin, ok = _tile_crop(ESRI_IMAGERY, lat, lng, z, hw, hw / aspect)
    # لمسة باهتة قليلًا (مثل خرائط ArcGIS) عشان الخطوط تبان أوضح
    crop = ImageEnhance.Color(crop).enhance(0.75)
    crop = Image.blend(crop, Image.new("RGB", crop.size, (255, 255, 255)), 0.16)
    opacity = max(0.0, min(1.0, float(opacity)))
    if opacity < 0.999:
        base = None
        if streets:
            base, _, ok_osm = _tile_crop(OSM_TILES, lat, lng, z, hw, hw / aspect)
            if not ok_osm:
                base = None
            elif base.size != crop.size:
                base = base.resize(crop.size)
        if base is None:
            base = Image.new("RGB", crop.size, (255, 255, 255))
        crop = Image.blend(base, crop, opacity)
    return crop, origin, z, mpp, ok


def _attribution(streets, opacity):
    t = "Esri World Imagery"
    if streets and opacity < 0.999:
        t += "  |  © OpenStreetMap contributors"
    return t


def _parts(g):
    if hasattr(g, "geoms"):
        for sub in g.geoms:
            yield from _parts(sub)
    else:
        yield g


def _panel(fig, left, bottom, width, height):
    ax = fig.add_axes([left, bottom, width, height], facecolor="white")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_linestyle((0, (4, 3)))
        sp.set_edgecolor("#8a8a8a")
        sp.set_linewidth(1.1)
    return ax


def _hex_ok(c):
    return isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c) is not None


def _legend_maps(feats, legend):
    """يدمج الألوان/النصوص المخصصة (legend: {case: {"label":..., "color":"#rrggbb"}}) مع الافتراضي."""
    colors = case_colors(feats)
    labels = {c: f"شبكة سيول: {c}" for c in colors}
    for c, cfg in (legend or {}).items():
        if not isinstance(cfg, dict):
            continue
        if _hex_ok(cfg.get("color")):
            colors[c] = cfg["color"]
        if (cfg.get("label") or "").strip():
            labels[c] = cfg["label"].strip()
    return colors, labels


def _draw_features(ax, feats, colors, xy, clat, clng, dlat_v, dlng_v, scale=1.0):
    in_view = set()
    for f in feats:
        bx0, by0, bx1, by1 = f["geom_w"].bounds
        if bx1 < clng - dlng_v or bx0 > clng + dlng_v or by1 < clat - dlat_v or by0 > clat + dlat_v:
            continue
        in_view.add(f["case"])
        col = colors[f["case"]]
        for g in _parts(f["geom_w"]):
            t = g.geom_type
            if t == "LineString":
                pts = [xy(*c[:2]) for c in g.coords]
                ax.plot(*zip(*pts), color="white", lw=4.2 * scale, alpha=0.55, solid_capstyle="round", zorder=3)
                ax.plot(*zip(*pts), color=col, lw=2.3 * scale, solid_capstyle="round", zorder=4)
            elif t == "Polygon":
                pts = [xy(*c[:2]) for c in g.exterior.coords]
                ax.plot(*zip(*pts), color=col, lw=2.2 * scale, zorder=4)
                ax.fill(*zip(*pts), color=col, alpha=0.2, zorder=3)
            elif t == "Point":
                px_, py_ = xy(g.x, g.y)
                ax.scatter([px_], [py_], s=45 * scale, color=col, edgecolor="white", linewidth=1, zorder=5)
    return in_view


def _view_params(analysis, view):
    lat, lng = analysis["lat"], analysis["lng"]
    near = analysis.get("nearest")
    if view:
        vs, vw, vn, ve = view
        clat, clng = (vs + vn) / 2, (vw + ve) / 2
        radius = max(15.0, (ve - vw) * 111320 * math.cos(math.radians(clat)) / 2)
    else:
        clat, clng = lat, lng
        radius = max(120.0, min((near or 0) * 1.3, 600.0), analysis["near_tol"] * 3)
    return clat, clng, radius


def _fig_to_png_pdf(fig, dpi):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
    png = buf.getvalue()
    pdf_buf = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(pdf_buf, "PDF", resolution=dpi)
    return png, pdf_buf.getvalue()


def _draw_map_legend(fig, ax, point_label, items, fontsize=11.5):
    """مفتاح خريطة داخل الخريطة (أسفل اليمين): نقطة التجمع + أنواع الشبكة. items = [(label, color)]"""
    h_in = fig.get_size_inches()[1]
    step = (fontsize * 2.15 / 72) / h_in
    pad_y, pad_x = 0.016, 0.010
    sw_w = 0.04
    right = 0.985
    box_h = (1 + len(items)) * step + 2 * pad_y
    bottom = 0.03
    top_y = bottom + box_h - pad_y - step / 2
    text_right = right - pad_x - sw_w - 0.008
    rows = [("point", point_label, "#e60000")] + [("line", lb, col) for lb, col in items]
    texts, y = [], top_y
    for kind, lb, col in rows:
        t = ax.text(text_right, y, ar(lb), transform=ax.transAxes, ha="right", va="center",
                    fontsize=fontsize, color="#222", zorder=13)
        texts.append(t)
        x0, x1 = right - pad_x - sw_w, right - pad_x
        if kind == "point":
            ax.scatter([(x0 + x1) / 2], [y], transform=ax.transAxes, s=95, color=col,
                       edgecolor="white", linewidth=1.4, zorder=13)
        else:
            ax.plot([x0, x1], [y, y], transform=ax.transAxes, color=col, lw=4, solid_capstyle="round", zorder=13)
        y -= step
    renderer = fig.canvas.get_renderer()
    inv = ax.transAxes.inverted()
    wmax = max(t.get_window_extent(renderer).transformed(inv).width for t in texts)
    left = text_right - wmax - pad_x * 1.4
    ax.add_patch(Rectangle((left, bottom), right - left, box_h, transform=ax.transAxes, facecolor="white",
                           alpha=0.93, edgecolor="#777777", linewidth=1.1, linestyle=(0, (4, 3)), zorder=12))


def render_full_map(analysis, dpi=200, view=None, legend=None, point_label="نقطة التجمع",
                    show_north=False, show_scale=False, aerial_opacity=1.0, streets=False):
    """خريطة بالصورة الجوية تملأ كامل الصورة (A4 أفقي) + مفتاح خريطة فقط. يرجع (png, pdf)."""
    lat, lng = analysis["lat"], analysis["lng"]
    feats = analysis["features"]
    colors, labels = _legend_maps(feats, legend)
    W_IN, H_IN = 11.69, 8.27
    clat, clng, radius = _view_params(analysis, view)
    bg, (ox, oy), z, mpp, tiles_ok = _aerial_background(clat, clng, radius, W_IN / H_IN, aerial_opacity, streets)
    dlat_v = radius * 2 / 111320.0
    dlng_v = dlat_v / max(math.cos(math.radians(clat)), 0.2)

    plt.rcParams["font.family"] = "DejaVu Sans"
    fig = plt.figure(figsize=(W_IN, H_IN), dpi=dpi, facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.imshow(bg, extent=(0, bg.width, bg.height, 0), interpolation="bilinear", aspect="auto")
    ax.set_xlim(0, bg.width)
    ax.set_ylim(bg.height, 0)
    ax.axis("off")
    if not tiles_ok:
        ax.text(0.5, 0.55, ar("تعذر تحميل الصورة الجوية"), transform=ax.transAxes, ha="center", color="#555", fontsize=16)

    def xy(lon, la):
        px, py = _merc(lon, la, z)
        return px - ox, py - oy

    in_view = _draw_features(ax, feats, colors, xy, clat, clng, dlat_v, dlng_v, scale=1.25)
    cx, cy = xy(lng, lat)
    ax.scatter([cx], [cy], s=130, color="#e60000", edgecolor="white", linewidth=1.8, zorder=8)

    ax.text(0.006, 0.006, _attribution(streets, aerial_opacity), transform=ax.transAxes, ha="left", va="bottom", fontsize=6.5,
            color="white", path_effects=[pe.withStroke(linewidth=2, foreground="#444444")], zorder=10)

    if show_north:
        ax.scatter([0.965], [0.9], s=1700, color="white", alpha=0.85, transform=ax.transAxes, zorder=9)
        ax.annotate("", xy=(0.965, 0.945), xytext=(0.965, 0.885), xycoords="axes fraction",
                    arrowprops=dict(arrowstyle="-|>", color="black", lw=2), zorder=10)
        ax.text(0.965, 0.86, "N", transform=ax.transAxes, ha="center", va="center", fontsize=10,
                fontweight="bold", zorder=10)
    if show_scale:
        m_per_in = (2 * radius) / W_IN
        nice = max((v for v in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000)
                    if v / m_per_in <= 2.2), default=1)
        bar = nice / m_per_in / W_IN
        x0, y0, hh = 0.02, 0.045, 0.012
        ax.add_patch(Rectangle((x0 - 0.008, y0 - 0.03), bar + 0.016, 0.075, transform=ax.transAxes,
                               color="white", alpha=0.85, zorder=9))
        for k in range(4):
            ax.add_patch(Rectangle((x0 + k * bar / 4, y0), bar / 4, hh, transform=ax.transAxes,
                                   facecolor="black" if k % 2 == 0 else "white", edgecolor="black",
                                   linewidth=0.8, zorder=10))
        unit_km = nice >= 1000
        fmt = (lambda v: f"{v / 1000:g}") if unit_km else (lambda v: f"{v:g}")
        for frac in (0, 0.5, 1):
            ax.text(x0 + frac * bar, y0 - 0.006, fmt(nice * frac), transform=ax.transAxes, ha="center",
                    va="top", fontsize=8, zorder=10)
        ax.text(x0 + bar / 2, y0 + hh + 0.006, ar("كيلومتر" if unit_km else "متر"), transform=ax.transAxes,
                ha="center", va="bottom", fontsize=8.5, zorder=10)

    items = [(labels[c], colors[c]) for c in sorted(in_view)]
    _draw_map_legend(fig, ax, (point_label or "").strip() or "نقطة التجمع", items)
    return _fig_to_png_pdf(fig, dpi)



def render_report(analysis, point_id, description, dpi=200, view=None, legend=None, point_label="نقطة التجمع",
                  aerial_opacity=1.0, streets=False):
    """يرجع (png_bytes, pdf_bytes): صفحة A4 أفقية على نمط خرائط ArcGIS:
    خريطة جوية كبيرة + عمود جانبي (خريطة موقع عامة، المصطلحات، الموضوع، علاقة الشبكة، مقياس الرسم)."""
    lat, lng = analysis["lat"], analysis["lng"]
    feats = analysis["features"]
    colors, labels = _legend_maps(feats, legend)
    near = analysis["nearest"]
    W_IN, H_IN = 11.69, 8.27
    MAP_L, MAP_B, MAP_W, MAP_H = 0.022, 0.03, 0.76, 0.94
    ax_w_in, ax_h_in = MAP_W * W_IN, MAP_H * H_IN
    aspect = ax_w_in / ax_h_in

    if view:
        vs, vw, vn, ve = view
        clat, clng = (vs + vn) / 2, (vw + ve) / 2
        radius = max(15.0, (ve - vw) * 111320 * math.cos(math.radians(clat)) / 2)
    else:
        clat, clng = lat, lng
        radius = max(120.0, min((near or 0) * 1.3, 600.0), analysis["near_tol"] * 3)
    bg, (ox, oy), z, mpp, tiles_ok = _aerial_background(clat, clng, radius, aspect, aerial_opacity, streets)
    dlat_v = radius * 2 / 111320.0
    dlng_v = dlat_v / max(math.cos(math.radians(clat)), 0.2)

    plt.rcParams["font.family"] = "DejaVu Sans"
    fig = plt.figure(figsize=(W_IN, H_IN), dpi=dpi, facecolor="#6aa84f")
    fig.add_artist(Rectangle((0.006, 0.009), 0.988, 0.982, transform=fig.transFigure,
                             facecolor="white", edgecolor="#3d7a1f", linewidth=1.3, zorder=-10))
    fig.add_artist(Rectangle((0.012, 0.017), 0.976, 0.966, transform=fig.transFigure,
                             facecolor="white", edgecolor="#9ccc65", linewidth=2.2, zorder=-9))

    # ================= الخريطة الرئيسية =================
    ax = fig.add_axes([MAP_L, MAP_B, MAP_W, MAP_H])
    ax.imshow(bg, extent=(0, bg.width, bg.height, 0), interpolation="bilinear", aspect="auto")
    ax.set_xlim(0, bg.width)
    ax.set_ylim(bg.height, 0)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_edgecolor("#333333")
        sp.set_linewidth(1.2)
    if not tiles_ok:
        ax.text(0.5, 0.55, ar("تعذر تحميل الصورة الجوية"), transform=ax.transAxes, ha="center", color="#555", fontsize=14)

    def xy(lon, la):
        px, py = _merc(lon, la, z)
        return px - ox, py - oy

    in_view = _draw_features(ax, feats, colors, xy, clat, clng, dlat_v, dlng_v)

    cx, cy = xy(lng, lat)
    ax.add_patch(plt.Circle((cx, cy), analysis["near_tol"] / mpp, fill=False, edgecolor="#ffd600",
                            lw=1.4, ls="--", zorder=6))
    ax.scatter([cx], [cy], s=70, color="#e60000", edgecolor="white", linewidth=1.3, zorder=8)
    halo = [pe.withStroke(linewidth=3.2, foreground="white")]
    ax.text(cx + bg.width * 0.011, cy - bg.height * 0.004, ar("موقع التجمع"), fontsize=11, fontweight="bold",
            color="black", ha="left", va="center", zorder=9, path_effects=halo)

    # اتجاه الشمال + مصدر الصورة
    ax.scatter([0.955], [0.885], s=1500, color="white", alpha=0.85, transform=ax.transAxes, zorder=9)
    ax.annotate("", xy=(0.955, 0.935), xytext=(0.955, 0.865), xycoords="axes fraction",
                arrowprops=dict(arrowstyle="-|>", color="black", lw=2), zorder=10)
    ax.text(0.955, 0.84, "N", transform=ax.transAxes, ha="center", va="center", fontsize=10, fontweight="bold", zorder=10)
    ax.text(0.99, 0.012, _attribution(streets, aerial_opacity), transform=ax.transAxes, ha="right", fontsize=6.5, color="white",
            path_effects=[pe.withStroke(linewidth=2, foreground="#444444")], zorder=10)

    # ================= العمود الجانبي =================
    PL, PW = 0.79, 0.19
    gap = 0.008
    heights = [0.30, 0.223, 0.15, 0.15, 0.085]
    tops, y = [], 0.97
    for h in heights:
        tops.append(y)
        y -= h + gap
    panel_w_in = PW * W_IN

    def pan(idx):
        return _panel(fig, PL, tops[idx] - heights[idx], PW, heights[idx])

    # 1) خريطة الموقع العامة
    a1 = pan(0)
    ph_in = heights[0] * H_IN
    half_w = 260
    ov, _, ov_ok = _tile_crop(OSM_TILES, lat, lng, 11, half_w, half_w * ph_in / panel_w_in)
    a1.imshow(ov, extent=(0, 1, 0, 1), aspect="auto", interpolation="bilinear")
    a1.scatter([0.5], [0.5], s=26, color="#e60000", edgecolor="white", linewidth=0.8, zorder=5)
    a1.text(0.5, 0.012, "© OpenStreetMap contributors", ha="center", va="bottom", fontsize=5.5, color="#333",
            bbox=dict(facecolor="white", alpha=0.75, edgecolor="none", pad=1.2))

    # 2) المصطلحات
    a2 = pan(1)
    a2.text(0.95, 0.9, ar("المصطلحات"), ha="right", va="center", fontsize=15, color="#222")
    cases = sorted(in_view)
    n_items = len(cases) + 1
    step = min(0.115, 0.66 / max(n_items, 1))
    yy = 0.74
    a2.scatter([0.905], [yy], s=45, color="#e60000", edgecolor="white", linewidth=0.8, zorder=5)
    _pl = (point_label or "").strip() or "نقطة التجمع"
    a2.text(0.82, yy, ar(_pl), ha="right", va="center", fontsize=min(9.5, max(6.0, 9.5 * 20 / max(len(_pl), 1))), color="#333")
    for c in cases:
        yy -= step
        a2.plot([0.86, 0.96], [yy, yy], color=colors[c], lw=3, solid_capstyle="round")
        a2.text(0.82, yy, ar(labels[c]), ha="right", va="center", fontsize=min(9.5, max(6.0, 9.5 * 20 / max(len(labels[c]), 1))), color="#333")

    # 3) الموضوع (وصف الموقع)
    a3 = pan(2)
    a3.text(0.95, 0.83, ar("الموضوع"), ha="right", va="center", fontsize=15, color="#222")
    desc = (description or "").strip() or "—"
    ly = 0.62
    for ln_ in textwrap.wrap(desc, 27)[:4]:
        a3.text(0.95, ly, ar(ln_), ha="right", va="center", fontsize=9.5, color="#333")
        ly -= 0.14

    # 4) علاقة النقطة بالشبكة
    a4 = pan(3)
    a4.text(0.95, 0.83, ar(analysis["headline"]), ha="right", va="center", fontsize=9.5, fontweight="bold", color="#222")
    ly = 0.62
    for item in analysis["lines"][:3]:
        for ln_ in textwrap.wrap(item, 30)[:2]:
            a4.text(0.95, ly, ar(ln_), ha="right", va="center", fontsize=8.5, color="#333")
            ly -= 0.125

    # 5) مقياس الرسم (مرتبط بمقياس الخريطة الفعلي)
    a5 = pan(4)
    m_per_in = (2 * radius) / ax_w_in
    nice = max((v for v in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000) if v / m_per_in <= panel_w_in * 0.62), default=1)
    bar_in = nice / m_per_in
    bx0 = (panel_w_in - bar_in) / 2 / panel_w_in
    seg = bar_in / panel_w_in / 4
    for k in range(4):
        a5.add_patch(Rectangle((bx0 + k * seg, 0.50), seg, 0.16, facecolor="black" if k % 2 == 0 else "white",
                               edgecolor="black", linewidth=0.8))
    unit_km = nice >= 1000
    fmt = (lambda v: f"{v / 1000:g}") if unit_km else (lambda v: f"{v:g}")
    for frac in (0, 0.5, 1):
        a5.text(bx0 + frac * seg * 4, 0.40, fmt(nice * frac), ha="center", va="top", fontsize=8)
    a5.text(0.5, 0.06, ar("كيلومتر" if unit_km else "متر"), ha="center", va="bottom", fontsize=9)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
    png = buf.getvalue()
    pdf_buf = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(pdf_buf, "PDF", resolution=dpi)
    return png, pdf_buf.getvalue()

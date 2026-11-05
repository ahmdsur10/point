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
from PIL import Image
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


def _fetch_tile(z, x, y):
    try:
        r = requests.get(ESRI_IMAGERY.format(z=z, x=x, y=y), timeout=10, headers={"User-Agent": "point-manager/1.0"})
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None


def _aerial_background(lat, lng, radius_m, aspect):
    z = int(math.floor(math.log2(156543.03 * math.cos(math.radians(lat)) * 1000 / (2 * radius_m))))
    z = max(1, min(19, z))
    cx, cy = _merc(lng, lat, z)
    mpp = 156543.03 * math.cos(math.radians(lat)) / 2 ** z
    hw = radius_m / mpp
    hh = hw / aspect
    x0, x1, y0, y1 = cx - hw, cx + hw, cy - hh, cy + hh
    tx0, tx1, ty0, ty1 = int(x0 // 256), int(x1 // 256), int(y0 // 256), int(y1 // 256)
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256), (205, 210, 215))
    jobs = [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)]
    ok = 0
    with _cf.ThreadPoolExecutor(8) as ex:
        for (tx, ty), img in zip(jobs, ex.map(lambda t: _fetch_tile(z, t[0], t[1]), jobs)):
            if img is not None:
                canvas.paste(img, ((tx - tx0) * 256, (ty - ty0) * 256))
                ok += 1
    crop = canvas.crop((int(x0 - tx0 * 256), int(y0 - ty0 * 256), int(x1 - tx0 * 256), int(y1 - ty0 * 256)))
    return crop, (x0, y0), z, mpp, ok > 0


def _parts(g):
    if hasattr(g, "geoms"):
        for sub in g.geoms:
            yield from _parts(sub)
    else:
        yield g


def render_report(analysis, point_id, description, dpi=200, view=None):
    """يرجع (png_bytes, pdf_bytes) لصفحة A4 فيها: العنوان، وصف الموقع، علاقة الشبكة، الخريطة بالصورة الجوية."""
    lat, lng = analysis["lat"], analysis["lng"]
    feats = analysis["features"]
    colors = case_colors(feats)
    near = analysis["nearest"]
    if view:  # (south, west, north, east) من خريطة المعاينة: نفس المركز ونفس عرض الخريطة
        vs, vw, vn, ve = view
        clat, clng = (vs + vn) / 2, (vw + ve) / 2
        radius = max(15.0, (ve - vw) * 111320 * math.cos(math.radians(clat)) / 2)
    else:
        clat, clng = lat, lng
        radius = max(120.0, min((near or 0) * 1.3, 600.0), analysis["near_tol"] * 3)
    aspect = 7.3 / 6.6
    bg, (ox, oy), z, mpp, tiles_ok = _aerial_background(clat, clng, radius, aspect)
    dlat_v = radius * 2 / 111320.0
    dlng_v = dlat_v / max(math.cos(math.radians(clat)), 0.2)

    plt.rcParams["font.family"] = "DejaVu Sans"
    fig = plt.figure(figsize=(8.27, 11.69), dpi=dpi, facecolor="white")
    W = 8.27

    # ---- الترويسة ----
    fig.add_artist(Rectangle((0, 0.925), 1, 0.075, transform=fig.transFigure, color="#0b4f9e"))
    fig.text(0.5, 0.963, ar("تقرير نقطة التجمع"), color="white", fontsize=24, fontweight="bold", ha="center", va="center")

    # ---- وصف الموقع ----
    fig.text(0.95, 0.895, ar("وصف الموقع"), color="#0b4f9e", fontsize=13, fontweight="bold", ha="right", va="center")
    desc = (description or "").strip() or "—"
    wrapped = textwrap.wrap(desc, 75)[:5]
    y = 0.872
    for ln_ in wrapped:
        fig.text(0.95, y, ar(ln_), color="#1b2a3b", fontsize=11, ha="right", va="center")
        y -= 0.0185

    # ---- علاقة الشبكة ----
    y -= 0.008
    box_h = 0.026 + 0.021 * max(len(analysis["lines"]), 1)
    fig.add_artist(Rectangle((0.05, y - box_h), 0.9, box_h, transform=fig.transFigure,
                             facecolor="#eef5fc", edgecolor="#b9d3ee", linewidth=1))
    fig.text(0.94, y - 0.014, ar(analysis["headline"]), color="#0b4f9e", fontsize=12, fontweight="bold", ha="right", va="center")
    yy = y - 0.014 - 0.021
    for ln_ in analysis["lines"]:
        fig.text(0.94, yy, ar("• " + ln_), color="#1b2a3b", fontsize=10.5, ha="right", va="center")
        yy -= 0.021
    top = y - box_h - 0.012

    # ---- الخريطة ----
    ax_h = (7.3 / aspect) / 11.69
    ax = fig.add_axes([0.05, max(top - ax_h, 0.04), 0.9, ax_h])
    ax.imshow(bg, extent=(0, bg.width, bg.height, 0), interpolation="bilinear")
    ax.set_xlim(0, bg.width)
    ax.set_ylim(bg.height, 0)
    ax.set_xticks([])
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_edgecolor("#0b4f9e")
        sp.set_linewidth(1.5)
    if not tiles_ok:
        ax.text(0.5, 0.5, ar("تعذر تحميل الصورة الجوية"), transform=ax.transAxes, ha="center", color="#555", fontsize=14)

    def xy(lon, la):
        px, py = _merc(lon, la, z)
        return px - ox, py - oy

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
                ax.plot(*zip(*pts), color="white", lw=5.5, alpha=0.6, solid_capstyle="round", zorder=3)
                ax.plot(*zip(*pts), color=col, lw=3, solid_capstyle="round", zorder=4)
            elif t == "Polygon":
                pts = [xy(*c[:2]) for c in g.exterior.coords]
                ax.plot(*zip(*pts), color=col, lw=2.5, zorder=4)
                ax.fill(*zip(*pts), color=col, alpha=0.2, zorder=3)
            elif t == "Point":
                px_, py_ = xy(g.x, g.y)
                ax.scatter([px_], [py_], s=60, color=col, edgecolor="white", linewidth=1.2, zorder=5)

    # دوائر النطاق ثم نقطة التجمع
    cx, cy = xy(lng, lat)
    for r_m, ls in ((analysis["near_tol"], "--"),):
        ax.add_patch(plt.Circle((cx, cy), r_m / mpp, fill=False, edgecolor="#ffd600", lw=1.6, ls=ls, zorder=6))
    ax.scatter([cx], [cy], s=520, color="#ff1744", alpha=0.25, zorder=7)
    ax.scatter([cx], [cy], s=140, color="#ff1744", edgecolor="white", linewidth=2.2, zorder=8)

    # مقياس الرسم
    nice = next((v for v in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000) if v / mpp > bg.width * 0.12), 1000)
    bar = nice / mpp
    bx, by = bg.width * 0.04, bg.height * 0.95
    pad, bh = bg.width * 0.012, bg.height * 0.055
    ax.add_patch(Rectangle((bx - pad, by - bh * 0.85), bar + 2 * pad, bh, color="white", alpha=0.78, zorder=9))
    ax.plot([bx, bx + bar], [by, by], color="black", lw=3, zorder=10)
    ax.text(bx + bar / 2, by - bh * 0.15, f"{nice} m", ha="center", va="bottom", fontsize=8, fontweight="bold", zorder=10)
    # اتجاه الشمال
    ax.annotate("N", xy=(bg.width * 0.95, bg.height * 0.05), xytext=(bg.width * 0.95, bg.height * 0.15),
                ha="center", va="center", fontsize=11, fontweight="bold", color="white",
                arrowprops=dict(arrowstyle="-|>", color="white", lw=2.2), zorder=10)

    # مفتاح الخريطة: نقطة التجمع + الشبكة حسب case
    handles = [Line2D([0], [0], marker="o", color="none", markerfacecolor="#ff1744", markeredgecolor="white",
                      markersize=11, label=ar("نقطة التجمع"))]
    present = sorted(in_view)
    for c in present:
        handles.append(Line2D([0], [0], color=colors[c], lw=4, label=ar(f"شبكة سيول: {c}")))
    leg = ax.legend(handles=handles, loc="lower right", fontsize=9.5, framealpha=0.93, title=ar("مفتاح الخريطة"),
                    title_fontsize=10, borderpad=0.8)
    leg.get_frame().set_edgecolor("#0b4f9e")
    leg.set_zorder(20)

    fig.text(0.05, 0.022, f"WGS84: {lat:.6f}, {lng:.6f}", fontsize=8.5, color="#6b7a8c", ha="left")
    fig.text(0.95, 0.022, ar("تاريخ التقرير: ") + datetime.datetime.now().strftime("%Y-%m-%d"),
             fontsize=8.5, color="#6b7a8c", ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="white")
    plt.close(fig)
    png = buf.getvalue()
    pdf_buf = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(pdf_buf, "PDF", resolution=dpi)
    return png, pdf_buf.getvalue()

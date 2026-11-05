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

import folium
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

SUPPORTED_TYPES = ["zip", "geojson", "json", "kml", "kmz", "csv", "xlsx", "xls", "gpkg", "gml", "dxf"]


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
# تجهيز الطبقات والتحليل
# =========================================================
def prepare_layer(name, raw, crs, status_mode, status_value=None, status_field=None, metric_epsg=20438):
    """يحوّل عناصر الطبقة لنظام قياس بالمتر (metric) و WGS84 ويحدد حالة كل عنصر."""
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
        if status_mode == "field" and status_field:
            st = normalize_status(f["props"].get(status_field))
        else:
            st = status_value or ST_UNKNOWN
        out.append({"geom_m": gm, "geom_w": gw, "props": f["props"], "status": st, "layer": name})
    return out


def _bearing_ar(dx, dy):
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return "—"
    ang = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    names = ["شمال", "شمال شرق", "شرق", "جنوب شرق", "جنوب", "جنوب غرب", "غرب", "شمال غرب"]
    return names[int((ang + 22.5) // 45) % 8]


def _relation(d, on_tol, near_tol):
    return "on" if d <= on_tol else ("near" if d <= near_tol else "far")


REL_TEXT = {"on": "على الخط", "near": "بجوار الخط", "far": "خارج نطاق التجاور"}


def analyze_point(lat, lng, features, on_tol=3.0, near_tol=25.0, search_radius=300.0, metric_epsg=20438):
    to_m = Transformer.from_crs(4326, metric_epsg, always_xy=True)
    to_w = Transformer.from_crs(metric_epsg, 4326, always_xy=True)
    px, py = to_m.transform(lng, lat)
    pt = Point(px, py)

    for f in features:
        f["dist"] = f["geom_m"].distance(pt)

    def describe(f):
        n = nearest_points(f["geom_m"], pt)[0]
        nlng, nlat = to_w.transform(n.x, n.y)
        return {
            "feature": f, "dist": f["dist"], "relation": _relation(f["dist"], on_tol, near_tol),
            "bearing": _bearing_ar(n.x - px, n.y - py), "near_latlng": (nlat, nlng),
        }

    per_status = {}
    for st in STATUS_ORDER:
        cands = [f for f in features if f["status"] == st]
        if cands:
            per_status[st] = describe(min(cands, key=lambda f: f["dist"]))

    in_radius = sorted([f for f in features if f["dist"] <= search_radius], key=lambda f: f["dist"])
    nearest_list = [describe(f) for f in in_radius[:10]]

    # ---- الحكم النهائي ----
    verdict = None
    for st in STATUS_ORDER:
        for rel in ("on", "near"):
            r = per_status.get(st)
            if r and r["relation"] == rel:
                verdict = (st, rel, r)
                break
        if verdict:
            break

    notes = []
    if verdict:
        st, rel, r = verdict
        verb = "تقع على" if rel == "on" else "تقع بجوار"
        headline = f"النقطة {verb} {STATUS_NOUN[st]}"
        if rel == "on":
            detail = f"الطبقة: {r['feature']['layer']} — المسافة عن محور الخط {r['dist']:.1f} م"
        else:
            detail = f"الطبقة: {r['feature']['layer']} — يبعد {r['dist']:.1f} م باتجاه {r['bearing']}"
        level = st
        for st2, r2 in per_status.items():
            if (st2, r2["relation"]) != (st, rel) and r2["relation"] in ("on", "near"):
                w = "على" if r2["relation"] == "on" else "بجوار"
                notes.append(f"يوجد أيضًا {STATUS_NOUN[st2]} {w} النقطة ({r2['dist']:.1f} م باتجاه {r2['bearing']}) — الطبقة: {r2['feature']['layer']}")
    elif per_status:
        best = min(per_status.items(), key=lambda kv: kv[1]["dist"])
        headline = "لا يوجد خط سيول ضمن نطاق التجاور"
        detail = f"أقرب عنصر: {STATUS_NOUN[best[0]]} ({best[1]['feature']['layer']}) يبعد {best[1]['dist']:.0f} م باتجاه {best[1]['bearing']}"
        level = "far"
    else:
        headline = "لم تُحمَّل بيانات شبكة سيول"
        detail = "ارفع ملف الشبكة لإظهار علاقة النقطة بالخطوط القائمة والمستقبلية."
        level = "nodata"

    return {
        "lat": lat, "lng": lng, "x_m": px, "y_m": py, "metric_epsg": metric_epsg,
        "on_tol": on_tol, "near_tol": near_tol, "search_radius": search_radius,
        "per_status": per_status, "nearest_list": nearest_list, "n_in_radius": len(in_radius),
        "headline": headline, "detail": detail, "level": level, "notes": notes,
        "features": features,
    }


# =========================================================
# الخرائط
# =========================================================
ESRI_IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ESRI_LABELS = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"

_PULSE_CSS = """<style>
@keyframes pulse-ring {0%{transform:scale(.6);opacity:.7}70%,100%{transform:scale(2);opacity:0}}
.legend{position:fixed;bottom:22px;right:10px;z-index:9999;background:rgba(255,255,255,.94);
 padding:9px 12px;border-radius:10px;font:12px Tahoma,Arial,sans-serif;direction:rtl;
 box-shadow:0 2px 8px rgba(0,0,0,.35);line-height:1.9}
.legend i{display:inline-block;width:26px;height:0;border-top:4px solid;margin-left:7px;vertical-align:middle}
.northarrow{position:fixed;top:12px;left:52px;z-index:9999;background:rgba(255,255,255,.92);
 border-radius:50%;width:38px;height:38px;text-align:center;font:700 11px Arial;line-height:1.1;
 padding-top:4px;box-shadow:0 1px 5px rgba(0,0,0,.4)}
</style>"""


def _bounds_for(lat, lng, radius_m):
    dlat = radius_m / 111320.0
    dlng = radius_m / (111320.0 * max(math.cos(math.radians(lat)), 0.2))
    return [[lat - dlat, lng - dlng], [lat + dlat, lng + dlng]]


def build_map(analysis, radius_m, basemap="sat", max_features=500):
    lat, lng = analysis["lat"], analysis["lng"]
    m = folium.Map(location=[lat, lng], tiles=None, zoom_control=True, control_scale=True, prefer_canvas=True)
    sat = folium.TileLayer(ESRI_IMAGERY, attr="Esri World Imagery", name="🛰️ صورة جوية", overlay=False, max_zoom=19)
    osm = folium.TileLayer("OpenStreetMap", name="🗺️ شوارع", overlay=False)
    (sat if basemap == "sat" else osm).add_to(m)
    (osm if basemap == "sat" else sat).add_to(m)
    folium.TileLayer(ESRI_LABELS, attr="Esri", name="🏷️ أسماء الأماكن", overlay=True, control=True,
                     show=(basemap == "sat")).add_to(m)

    m.get_root().header.add_child(folium.Element(_PULSE_CSS))

    # ---- عناصر الشبكة ----
    shown = [f for f in analysis["features"] if f["dist"] <= radius_m * 1.6]
    shown.sort(key=lambda f: f["dist"])
    shown = shown[:max_features]
    groups = {}
    for f in shown:
        st = f["status"]
        if st not in groups:
            groups[st] = folium.FeatureGroup(name=f"💧 {st}", show=True)
        col = STATUS_COLORS[st]
        geom = f["geom_w"]
        is_pt = geom.geom_type in ("Point", "MultiPoint")
        if not is_pt:
            folium.GeoJson(
                mapping(geom),
                style_function=lambda _x: {"color": "#ffffff", "weight": 9, "opacity": 0.55, "fill": False},
            ).add_to(groups[st])
        tip = f"{f['layer']} — {st} — {f['dist']:.1f} م"
        gj = folium.GeoJson(
            mapping(geom),
            style_function=lambda _x, c=col, s=st: {
                "color": c, "weight": 5, "opacity": 0.98,
                "dashArray": "9 8" if s == ST_FUTURE else None,
                "fillColor": c, "fillOpacity": 0.25,
            },
            marker=folium.CircleMarker(radius=6, color="#fff", weight=1.5, fill=True, fill_color=col, fill_opacity=1),
        )
        gj.add_child(folium.Tooltip(tip))
        gj.add_to(groups[st])
    for g in groups.values():
        g.add_to(m)

    # ---- دوائر النطاق ----
    folium.Circle([lat, lng], radius=analysis["near_tol"], color="#ffd600", weight=2, fill=True,
                  fill_opacity=0.08, dash_array="6 6",
                  tooltip=f"نطاق «بجوار» ({analysis['near_tol']:.0f} م)").add_to(m)
    folium.Circle([lat, lng], radius=max(analysis["on_tol"], 0.5), color="#ff1744", weight=2, fill=True,
                  fill_opacity=0.25, tooltip=f"نطاق «على الخط» ({analysis['on_tol']:.0f} م)").add_to(m)

    # ---- خطوط الربط لأقرب عنصر من كل حالة ----
    for st, r in analysis["per_status"].items():
        if r["dist"] <= radius_m * 1.6 and r["dist"] > 0.3:
            folium.PolyLine([[lat, lng], list(r["near_latlng"])], color=STATUS_COLORS[st], weight=2.5,
                            dash_array="3 6", tooltip=f"{r['dist']:.1f} م — {r['bearing']}").add_to(m)

    # ---- علامة النقطة ----
    pulse = """<div style="position:relative;width:34px;height:34px">
      <div style="position:absolute;inset:0;background:#ff1744;border-radius:50%;opacity:.6;animation:pulse-ring 1.5s ease-out infinite"></div>
      <div style="position:absolute;top:9px;left:9px;width:16px;height:16px;background:#ff1744;border:3px solid #fff;border-radius:50%;box-shadow:0 0 8px rgba(0,0,0,.8)"></div></div>"""
    folium.Marker([lat, lng], tooltip="موقع النقطة",
                  icon=folium.DivIcon(html=pulse, icon_size=(34, 34), icon_anchor=(17, 17))).add_to(m)

    # ---- مفتاح الخريطة + السهم ----
    rows = "".join(f"<div><i style='border-color:{STATUS_COLORS[s]};{'border-top-style:dashed;' if s == ST_FUTURE else ''}'></i>{s}</div>"
                   for s in STATUS_ORDER if s in groups)
    legend = f"<div class='legend'><b>مفتاح الخريطة</b><div><span style='color:#ff1744'>●</span> موقع النقطة</div>{rows}</div>"
    m.get_root().html.add_child(folium.Element(legend + "<div class='northarrow'>▲<br>N</div>"))

    folium.LayerControl(position="topright", collapsed=True).add_to(m)
    m.fit_bounds(_bounds_for(lat, lng, radius_m))
    return m


def _map_iframe(m, height):
    doc = m.get_root().render()
    return f'<iframe class="map" style="height:{height}px" srcdoc="{html.escape(doc, quote=True)}"></iframe>'


# =========================================================
# تقرير HTML
# =========================================================
_LEVEL_STYLE = {
    ST_EXISTING: ("#0277bd", "#e1f5fe", "🔵"),
    ST_UNDER: ("#e65100", "#fff3e0", "🟠"),
    ST_FUTURE: ("#8e24aa", "#f3e5f5", "🟣"),
    ST_UNKNOWN: ("#546e7a", "#eceff1", "⚪"),
    "far": ("#2e7d32", "#e8f5e9", "✅"),
    "nodata": ("#757575", "#f5f5f5", "ℹ️"),
}

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800&display=swap');
*{box-sizing:border-box}
body{margin:0;background:#eef2f7;font-family:'Cairo','Segoe UI',Tahoma,sans-serif;color:#1b2a3b;direction:rtl}
.page{max-width:980px;margin:18px auto;background:#fff;border-radius:18px;overflow:hidden;box-shadow:0 8px 30px rgba(20,40,80,.15)}
.hero{background:linear-gradient(135deg,#084c91,#0b6bcb 60%,#2fa0e0);color:#fff;padding:26px 32px;display:flex;justify-content:space-between;align-items:center;gap:16px}
.hero h1{margin:0;font-size:26px;font-weight:800}.hero .sub{opacity:.9;font-size:14px;margin-top:4px}
.idbox{background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.4);padding:10px 18px;border-radius:14px;text-align:center}
.idbox b{display:block;font-size:26px}.idbox span{font-size:12px;opacity:.9}
.body{padding:24px 32px 30px}
.verdict{border-radius:16px;padding:18px 22px;margin-bottom:20px;border-right:8px solid var(--c);background:var(--bg)}
.verdict .h{font-size:22px;font-weight:800;color:var(--c)}.verdict .d{margin-top:4px;font-size:15px}
.verdict ul{margin:8px 0 0;padding-right:20px;font-size:14px}
h2{font-size:18px;margin:26px 0 10px;border-right:5px solid #0b6bcb;padding-right:10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{border:1px solid #e3e8ef;border-radius:14px;padding:12px 16px;background:#fafcff}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{padding:7px 10px;border-bottom:1px solid #e8edf3;text-align:right;vertical-align:top}
th{background:#f1f5fa;font-weight:700;white-space:nowrap}
td.k{color:#5b6b80;white-space:nowrap;width:34%}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:700;color:#fff}
.map{width:100%;border:1px solid #cfd8e3;border-radius:14px;display:block}
.cap{font-size:12px;color:#6b7a8c;margin-top:6px}
.long{background:#f7f9fc;border:1px solid #e3e8ef;border-radius:10px;padding:8px 12px;margin-top:8px;font-size:13.5px;white-space:pre-wrap}
.long b{display:block;color:#0b6bcb;margin-bottom:2px}
.foot{font-size:12px;color:#7b8794;padding:14px 32px 22px;border-top:1px dashed #d7dee8}
.printbtn{background:#fff;color:#0b6bcb;border:0;border-radius:10px;padding:8px 16px;font:700 14px Cairo,Tahoma;cursor:pointer}
a{color:#0b6bcb}
@media print{body{background:#fff}.page{box-shadow:none;margin:0;border-radius:0}.printbtn{display:none}
 .card,.verdict,table,.map{break-inside:avoid}}
"""


def _fmt_props(props, limit=5):
    items = [(k, v) for k, v in props.items() if v not in (None, "") and str(k).lower() not in ("geometry", "shape")]
    return "، ".join(f"{html.escape(str(k))}: {html.escape(str(v))}" for k, v in items[:limit]) or "—"


def build_report_html(analysis, point_attrs, layers_info, point_id, long_cols=(), preparer="", notes=""):
    esc = html.escape
    lat, lng = analysis["lat"], analysis["lng"]
    color, bg, icon = _LEVEL_STYLE.get(analysis["level"], _LEVEL_STYLE["nodata"])

    nearest_any = min([r["dist"] for r in analysis["per_status"].values()], default=None)
    zoom_r = analysis["near_tol"] * 3
    if nearest_any is not None:
        zoom_r = max(zoom_r, min(nearest_any * 1.4, analysis["search_radius"]))
    zoom_r = max(zoom_r, 60)
    ctx_r = max(analysis["search_radius"] * 2, 300)

    map1 = _map_iframe(build_map(analysis, zoom_r, "sat"), 520)
    map2 = _map_iframe(build_map(analysis, ctx_r, "osm", max_features=700), 380)

    # ---- بيانات النقطة ----
    short_rows, long_blocks = "", ""
    for k, v in point_attrs.items():
        if v in (None, "") or str(k).startswith("__"):
            continue
        sv = str(v)
        if k in long_cols or len(sv) > 90 or "\n" in sv:
            long_blocks += f"<div class='long'><b>{esc(str(k))}</b>{esc(sv)}</div>"
        elif sv.startswith("http"):
            short_rows += f"<tr><td class='k'>{esc(str(k))}</td><td><a href='{esc(sv)}' target='_blank'>فتح الرابط 🔗</a></td></tr>"
        else:
            short_rows += f"<tr><td class='k'>{esc(str(k))}</td><td>{esc(sv)}</td></tr>"

    gmaps = f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}"
    coords_card = f"""
    <table>
      <tr><td class='k'>خط العرض (WGS84)</td><td dir='ltr' style='text-align:right'>{lat:.6f}</td></tr>
      <tr><td class='k'>خط الطول (WGS84)</td><td dir='ltr' style='text-align:right'>{lng:.6f}</td></tr>
      <tr><td class='k'>X (EPSG:{analysis['metric_epsg']})</td><td dir='ltr' style='text-align:right'>{analysis['x_m']:.2f}</td></tr>
      <tr><td class='k'>Y (EPSG:{analysis['metric_epsg']})</td><td dir='ltr' style='text-align:right'>{analysis['y_m']:.2f}</td></tr>
      <tr><td class='k'>الموقع</td><td><a href='{gmaps}' target='_blank'>فتح بقوقل ماب 📍</a></td></tr>
    </table>"""

    # ---- جدول العلاقة بكل حالة ----
    rel_rows = ""
    for st in STATUS_ORDER:
        r = analysis["per_status"].get(st)
        if r is None:
            if layers_info:
                rel_rows += f"<tr><td><span class='badge' style='background:{STATUS_COLORS[st]}'>{st}</span></td><td colspan='4' style='color:#8a97a6'>لا توجد عناصر بهذه الحالة في البيانات المحمّلة</td></tr>"
            continue
        rel = r["relation"]
        rt = {"on": f"<b style='color:#c62828'>على الخط</b>", "near": "<b style='color:#ef6c00'>بجوار الخط</b>",
              "far": "<span style='color:#5f6f81'>خارج نطاق التجاور</span>"}[rel]
        rel_rows += (f"<tr><td><span class='badge' style='background:{STATUS_COLORS[st]}'>{st}</span></td>"
                     f"<td>{rt}</td><td>{r['dist']:.1f} م</td><td>{esc(r['bearing'])}</td>"
                     f"<td>{esc(r['feature']['layer'])}</td></tr>")
    rel_table = (f"<table><tr><th>الحالة</th><th>العلاقة بالنقطة</th><th>أقرب مسافة</th><th>الاتجاه</th><th>الطبقة</th></tr>{rel_rows}</table>"
                 if rel_rows else "<div class='cap'>لا توجد بيانات شبكة.</div>")

    # ---- أقرب العناصر ----
    near_rows = ""
    for i, r in enumerate(analysis["nearest_list"], 1):
        f = r["feature"]
        near_rows += (f"<tr><td>{i}</td><td>{esc(f['layer'])}</td>"
                      f"<td><span class='badge' style='background:{STATUS_COLORS[f['status']]}'>{f['status']}</span></td>"
                      f"<td>{esc(f['geom_m'].geom_type)}</td><td>{r['dist']:.1f} م</td><td>{esc(r['bearing'])}</td>"
                      f"<td style='font-size:12px'>{_fmt_props(f['props'])}</td></tr>")
    near_table = (f"<table><tr><th>#</th><th>الطبقة</th><th>الحالة</th><th>النوع</th><th>المسافة</th><th>الاتجاه</th><th>أهم الخصائص</th></tr>{near_rows}</table>"
                  if near_rows else f"<div class='cap'>لا توجد عناصر شبكة ضمن نطاق البحث ({analysis['search_radius']:.0f} م).</div>")

    notes_html = "".join(f"<li>{esc(n)}</li>" for n in analysis["notes"])
    notes_ul = f"<ul>{notes_html}</ul>" if notes_html else ""

    layers_rows = "".join(
        f"<tr><td>{esc(li['name'])}</td><td>{esc(li['file'])}</td><td>{esc(li['status_desc'])}</td>"
        f"<td>{li['count']}</td><td>EPSG:{esc(str(li['epsg']))}</td></tr>" for li in layers_info)
    layers_table = (f"<table><tr><th>الطبقة</th><th>الملف</th><th>الحالة</th><th>عدد العناصر</th><th>النظام</th></tr>{layers_rows}</table>"
                    if layers_rows else "<div class='cap'>لم يتم تحميل بيانات شبكة.</div>")

    user_notes = f"<h2>📝 ملاحظات</h2><div class='long'>{esc(notes)}</div>" if notes.strip() else ""
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>تقرير النقطة {esc(str(point_id))}</title>
<style>{_CSS}</style></head><body><div class="page">
<div class="hero"><div><h1>🗺️ تقرير موقع نقطة</h1>
<div class="sub">علاقة النقطة بشبكة تصريف السيول — {today}{(' — إعداد: ' + esc(preparer)) if preparer.strip() else ''}</div>
<button class="printbtn" onclick="window.print()" style="margin-top:12px">🖨️ طباعة / حفظ PDF</button></div>
<div class="idbox"><span>رقم النقطة</span><b>{esc(str(point_id))}</b></div></div>
<div class="body">
<div class="verdict" style="--c:{color};--bg:{bg}"><div class="h">{icon} {esc(analysis['headline'])}</div>
<div class="d">{esc(analysis['detail'])}</div>{notes_ul}</div>

<h2>🛰️ موقع النقطة على الصورة الجوية</h2>{map1}
<div class="cap">الدائرة الحمراء: نطاق «على الخط» ({analysis['on_tol']:.0f} م) — الدائرة الصفراء: نطاق «بجوار» ({analysis['near_tol']:.0f} م) — الخطوط المنقطة تربط النقطة بأقرب عنصر من كل حالة.</div>

<h2>💧 العلاقة بشبكة السيول</h2>{rel_table}
<h2>📍 أقرب العناصر (ضمن {analysis['search_radius']:.0f} م — {analysis['n_in_radius']} عنصر)</h2>{near_table}

<div class="grid"><div><h2>🧭 الإحداثيات</h2><div class="card">{coords_card}</div></div>
<div><h2>🗂️ بيانات النقطة</h2><div class="card"><table>{short_rows or "<tr><td>—</td></tr>"}</table></div></div></div>
{long_blocks}

<h2>🗺️ خريطة السياق</h2>{map2}
{user_notes}
<h2>📦 البيانات المستخدمة</h2>{layers_table}
</div>
<div class="foot">أُنشئ هذا التقرير آليًا من تطبيق إدارة النقاط. التحليل يعتمد على دقة بيانات الشبكة المرفوعة ونظام إحداثياتها؛
ويُعد استرشاديًا ولا يغني عن المعاينة الميدانية. المقاييس بنظام EPSG:{analysis['metric_epsg']}.</div>
</div></body></html>"""

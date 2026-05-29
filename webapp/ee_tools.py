"""Earth Engine tool functions for the chat-driven web app.

Each public function corresponds to a tool Claude can call. The contract:
- Inputs are simple JSON-serializable types (strings, ints, dicts).
- Each layer-producing function returns a dict describing a Leaflet tile layer
  the frontend can render: tile_url template, vis label, bounds, etc.
- Stats functions return numeric dicts.

EE init is lazy on first call — we share state with the MCP server (same
EE_PROJECT env var, same user OAuth credential).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import ee
from dotenv import load_dotenv

# .env lives in the gee-mcp root, one level up from webapp/
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

EE_PROJECT = os.environ.get("EE_PROJECT")
EE_SERVICE_ACCOUNT_KEY = os.environ.get("EE_SERVICE_ACCOUNT_KEY")

_initialized = False


def ensure_ee() -> None:
    global _initialized
    if _initialized:
        return
    if not EE_PROJECT:
        raise RuntimeError("EE_PROJECT env var is not set in gee-mcp\\.env")
    if EE_SERVICE_ACCOUNT_KEY:
        key_path = Path(EE_SERVICE_ACCOUNT_KEY).expanduser()
        with open(key_path) as f:
            sa_email = json.load(f)["client_email"]
        creds = ee.ServiceAccountCredentials(sa_email, str(key_path))
        ee.Initialize(creds, project=EE_PROJECT)
    else:
        ee.Initialize(project=EE_PROJECT)
    _initialized = True


# ---------- AOI resolution ----------

# In-memory cache of geometries the user uploaded via /upload-asset. The keys
# are the leaf asset names they gave the upload ("Testing", "Pune_test", etc).
# The values are GeoJSON dicts. This lets `resolve_aoi("Testing")` work *before*
# the EE ingestion task finishes — we already parsed the geometry server-side
# so EE doesn't need to have the asset to use it for `reduceRegion` etc.
_UPLOADED_AOIS: dict[str, dict] = {}


def register_uploaded_aoi(name: str, geojson: dict) -> None:
    """Cache an uploaded GeoJSON so future `resolve_aoi(name)` calls find it."""
    _UPLOADED_AOIS[name] = geojson


def recent_upload_names() -> list[str]:
    """Return the names of currently-cached uploaded AOIs (for system-prompt injection)."""
    return list(_UPLOADED_AOIS.keys())


# Hand-curated synonyms — extend as needed.
_AOI_ALIASES = {
    "bangalore": "Bangalore Urban",
    "bengaluru": "Bangalore Urban",
    "bengaluru urban": "Bangalore Urban",
    "mumbai": "Mumbai",
    "delhi": "Delhi",
    "chennai": "Chennai",
    "hyderabad": "Hyderabad",
    "pune": "Pune",
    "kolkata": "Kolkata",
}


def resolve_aoi(aoi: dict | str) -> tuple[ee.Geometry, dict]:
    """Resolve a user-supplied AOI to (ee.Geometry, info_dict).

    Accepts:
      - a string name → looked up in FAO GAUL admin levels 2 → 1 → 0
      - a GeoJSON Geometry / Feature / FeatureCollection dict
      - {"bbox": [w, s, e, n]} for a rectangular box
    """
    if isinstance(aoi, dict):
        if "bbox" in aoi:
            w, s, e, n = aoi["bbox"]
            geom = ee.Geometry.Rectangle([w, s, e, n])
            return geom, {"source": "bbox", "bbox": [w, s, e, n]}
        t = aoi.get("type")
        if t == "Feature":
            return ee.Geometry(aoi["geometry"]), {"source": "geojson_feature"}
        if t == "FeatureCollection":
            return ee.FeatureCollection(aoi).geometry(), {"source": "geojson_fc"}
        return ee.Geometry(aoi), {"source": "geojson_geometry"}

    name = aoi.strip()
    # Full EE asset path (e.g. "projects/ee-foo/assets/my_aoi") — load it
    # directly. Works once the EE ingestion task is DONE; survives server
    # restarts (unlike the in-memory upload cache below).
    if name.startswith("projects/") and "/assets/" in name:
        try:
            info = ee.data.getAsset(name)
            t = info.get("type", "").upper()
            if t in ("TABLE", "FEATURECOLLECTION"):
                return ee.FeatureCollection(name).geometry(), {"source": "ee_asset", "asset_id": name}
            if t in ("IMAGE", "IMAGECOLLECTION"):
                base = ee.Image(name) if t == "IMAGE" else ee.ImageCollection(name).mosaic()
                return base.geometry(), {"source": "ee_asset", "asset_id": name}
        except Exception as e:
            raise ValueError(f"Asset '{name}' not found or not yet ingested: {e}")
    # Check the uploaded-AOI cache (case-insensitive). This makes
    # "Testing" usable immediately after upload, regardless of whether the EE
    # ingestion task has finished.
    for uploaded_name, geojson in _UPLOADED_AOIS.items():
        if uploaded_name.lower() == name.lower():
            t = geojson.get("type")
            if t == "FeatureCollection":
                geom = ee.FeatureCollection(geojson).geometry()
            elif t == "Feature":
                geom = ee.Geometry(geojson["geometry"])
            else:
                geom = ee.Geometry(geojson)
            return geom, {"source": "uploaded", "name": uploaded_name}
    lookup = _AOI_ALIASES.get(name.lower(), name)
    gaul2 = ee.FeatureCollection("FAO/GAUL/2015/level2").filter(ee.Filter.eq("ADM2_NAME", lookup))
    if gaul2.size().getInfo() > 0:
        return ee.Feature(gaul2.first()).geometry(), {"source": "gaul2", "match": lookup}
    gaul1 = ee.FeatureCollection("FAO/GAUL/2015/level1").filter(ee.Filter.eq("ADM1_NAME", lookup))
    if gaul1.size().getInfo() > 0:
        return ee.Feature(gaul1.first()).geometry(), {"source": "gaul1", "match": lookup}
    gaul0 = ee.FeatureCollection("FAO/GAUL/2015/level0").filter(ee.Filter.eq("ADM0_NAME", lookup))
    if gaul0.size().getInfo() > 0:
        return ee.Feature(gaul0.first()).geometry(), {"source": "gaul0", "match": lookup}
    raise ValueError(
        f"Couldn't resolve AOI '{aoi}'. Try a city or country name, or pass a GeoJSON dict / bbox."
    )


def _geom_to_leaflet_bounds(geom: ee.Geometry) -> list[list[float]]:
    """Return [[south, west], [north, east]] for Leaflet fitBounds."""
    rect = geom.bounds().coordinates().get(0).getInfo()
    lons = [p[0] for p in rect]
    lats = [p[1] for p in rect]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


# ---------- collections & helpers ----------

def _landsat_sr(start: str, end: str, geom: ee.Geometry) -> ee.ImageCollection:
    """Merged Landsat 8 + 9 Collection 2 L2 surface reflectance, cloud-masked, scaled."""
    def fix(img):
        sr = (img.select(["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"])
              .multiply(2.75e-5).add(-0.2)
              .rename(["BLUE", "GREEN", "RED", "NIR", "SWIR1", "SWIR2"]))
        qa = img.select("QA_PIXEL")
        return (sr.updateMask(qa.bitwiseAnd(1 << 3).eq(0))
                  .updateMask(qa.bitwiseAnd(1 << 4).eq(0))
                  .copyProperties(img, ["system:time_start"]))

    def coll(cid):
        return (ee.ImageCollection(cid).filterBounds(geom).filterDate(start, end)
                .filter(ee.Filter.lt("CLOUD_COVER", 50)).map(fix))

    return coll("LANDSAT/LC08/C02/T1_L2").merge(coll("LANDSAT/LC09/C02/T1_L2"))


def _landsat_lst(start: str, end: str, geom: ee.Geometry) -> ee.ImageCollection:
    """Landsat 8 + 9 LST (°C), cloud-masked."""
    def to_c(img):
        lst = (img.select("ST_B10").multiply(0.00341802).add(149.0)
               .subtract(273.15).rename("LST"))
        qa = img.select("QA_PIXEL")
        return (lst.updateMask(qa.bitwiseAnd(1 << 3).eq(0))
                   .updateMask(qa.bitwiseAnd(1 << 4).eq(0))
                   .copyProperties(img, ["system:time_start"]))

    def coll(cid):
        return (ee.ImageCollection(cid).filterBounds(geom).filterDate(start, end)
                .filter(ee.Filter.lt("CLOUD_COVER", 50)).map(to_c))

    return coll("LANDSAT/LC08/C02/T1_L2").merge(coll("LANDSAT/LC09/C02/T1_L2"))


def _sentinel2(start: str, end: str, geom: ee.Geometry) -> ee.ImageCollection:
    """Sentinel-2 SR Harmonized, cloud-masked via SCL, scaled to reflectance."""
    def fix(img):
        scl = img.select("SCL")
        # SCL classes: 3=cloud shadow, 8=cloud medium, 9=cloud high, 10=cirrus, 11=snow
        cloud_mask = (scl.eq(3).Or(scl.eq(8)).Or(scl.eq(9)).Or(scl.eq(10))).Not()
        sr = (img.select(["B2", "B3", "B4", "B8", "B11", "B12"]).divide(10000)
              .rename(["BLUE", "GREEN", "RED", "NIR", "SWIR1", "SWIR2"]))
        return sr.updateMask(cloud_mask).copyProperties(img, ["system:time_start"])

    return (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(geom).filterDate(start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 50)).map(fix))


_PAL_LST = [
    "040274", "0502a3", "235cb1", "30c8e2", "3ae237", "b5e22e", "fff705",
    "ffd611", "ffb613", "ff8b13", "ff0000", "a71001",
]
_PAL_NDVI = ["8b0000", "d73027", "fdae61", "fee08b", "d9ef8b", "a6d96a", "1a9850", "006837"]
_PAL_NDBI = ["1a9850", "a6d96a", "ffffbf", "fdae61", "d73027"]


# ---------- public tool functions ----------

def add_ee_layer(
    source: str,
    visualize: str,
    aoi: dict | str,
    year: int,
    months: list[int] | None = None,
    name: str | None = None,
) -> dict:
    """Build an EE layer over an AOI/date range and return its Leaflet tile URL.

    Args:
        source: "landsat" or "sentinel2".
        visualize: one of "rgb", "false_color", "ndvi", "ndbi", "lst".
                   "lst" requires source="landsat" (S2 has no thermal band).
        aoi: AOI string name or GeoJSON dict (see resolve_aoi).
        year: e.g. 2024.
        months: list of months 1-12 (default [3, 4, 5] = pre-monsoon).
        name: optional layer display name; auto-generated if omitted.
    """
    ensure_ee()
    if visualize == "lst" and source != "landsat":
        raise ValueError("LST visualization requires source='landsat' (Sentinel-2 has no thermal band).")

    months = months or [3, 4, 5]
    start = f"{year}-{min(months):02d}-01"
    end_month = max(months)
    # End-exclusive: first day of (max_month + 1) or next year
    if end_month == 12:
        end = f"{year + 1}-01-01"
    else:
        end = f"{year}-{end_month + 1:02d}-01"

    geom, aoi_info = resolve_aoi(aoi)

    if visualize == "lst":
        coll = _landsat_lst(start, end, geom)
        img = coll.median().clip(geom)
        vis = {"min": 25, "max": 50, "palette": _PAL_LST, "bands": ["LST"]}
        label = "°C"
    else:
        coll = _landsat_sr(start, end, geom) if source == "landsat" else _sentinel2(start, end, geom)
        median = coll.median().clip(geom)
        if visualize == "rgb":
            img = median.select(["RED", "GREEN", "BLUE"])
            vis = {"min": 0.0, "max": 0.30, "gamma": 1.1, "bands": ["RED", "GREEN", "BLUE"]}
            label = "true color"
        elif visualize == "false_color":
            img = median.select(["NIR", "RED", "GREEN"])
            vis = {"min": 0.0, "max": 0.35, "gamma": 1.1, "bands": ["NIR", "RED", "GREEN"]}
            label = "false color (NIR/R/G)"
        elif visualize == "ndvi":
            img = median.normalizedDifference(["NIR", "RED"]).rename("NDVI")
            vis = {"min": -0.2, "max": 0.8, "palette": _PAL_NDVI, "bands": ["NDVI"]}
            label = "NDVI"
        elif visualize == "ndbi":
            img = median.normalizedDifference(["SWIR1", "NIR"]).rename("NDBI")
            vis = {"min": -0.3, "max": 0.4, "palette": _PAL_NDBI, "bands": ["NDBI"]}
            label = "NDBI"
        else:
            raise ValueError(f"Unknown visualize='{visualize}'. Use rgb/false_color/ndvi/ndbi/lst.")

    scene_count = coll.size().getInfo()
    if scene_count == 0:
        raise ValueError(
            f"No {source} scenes found for {start}..{end} over the AOI. Try a wider date range."
        )

    map_id = img.getMapId(vis)
    tile_url = map_id["tile_fetcher"].url_format
    bounds = _geom_to_leaflet_bounds(geom)
    auto_name = name or f"{source.title()} {visualize.upper()} {year} m{min(months)}-{max(months)}"

    return {
        "name": auto_name,
        "tile_url": tile_url,
        "bounds": bounds,
        "vis": {**vis, "label": label},
        "meta": {
            "source": source,
            "visualize": visualize,
            "year": year,
            "months": months,
            "start": start,
            "end": end,
            "scene_count": scene_count,
            "aoi": aoi_info,
            "aoi_input": aoi if isinstance(aoi, str) else None,
        },
    }


def add_admin_outline(aoi: dict | str, name: str | None = None) -> dict:
    """Add the AOI's outline polygon as a vector overlay (no fill)."""
    ensure_ee()
    geom, aoi_info = resolve_aoi(aoi)
    geojson = geom.getInfo()
    bounds = _geom_to_leaflet_bounds(geom)
    label = name or (aoi if isinstance(aoi, str) else aoi_info.get("source", "AOI"))
    return {
        "name": f"Outline: {label}",
        "geojson": geojson,
        "bounds": bounds,
        "meta": {"aoi": aoi_info, "kind": "vector"},
    }


def compute_zonal_stats(
    source: str,
    visualize: str,
    aoi: dict | str,
    year: int,
    months: list[int] | None = None,
    scale: int | None = None,
) -> dict:
    """Return mean/min/max/stdDev for an index over an AOI/date range, without adding a layer."""
    ensure_ee()
    if visualize == "lst" and source != "landsat":
        raise ValueError("LST requires source='landsat'.")

    months = months or [3, 4, 5]
    start = f"{year}-{min(months):02d}-01"
    end_month = max(months)
    end = f"{year + 1}-01-01" if end_month == 12 else f"{year}-{end_month + 1:02d}-01"

    geom, aoi_info = resolve_aoi(aoi)

    if visualize == "lst":
        img = _landsat_lst(start, end, geom).median().rename("v")
        default_scale = 30
    else:
        coll = _landsat_sr(start, end, geom) if source == "landsat" else _sentinel2(start, end, geom)
        median = coll.median()
        if visualize == "rgb" or visualize == "false_color":
            raise ValueError("Stats are only meaningful for ndvi/ndbi/lst, not raw RGB composites.")
        if visualize == "ndvi":
            img = median.normalizedDifference(["NIR", "RED"]).rename("v")
        elif visualize == "ndbi":
            img = median.normalizedDifference(["SWIR1", "NIR"]).rename("v")
        else:
            raise ValueError(f"Unknown visualize='{visualize}'.")
        default_scale = 30 if source == "landsat" else 10

    reducer = (ee.Reducer.mean()
               .combine(ee.Reducer.minMax(), sharedInputs=True)
               .combine(ee.Reducer.stdDev(), sharedInputs=True)
               .combine(ee.Reducer.count(), sharedInputs=True))
    stats = img.reduceRegion(
        reducer=reducer,
        geometry=geom,
        scale=scale or default_scale,
        maxPixels=1e10,
        bestEffort=True,
    ).getInfo()
    return {
        "source": source,
        "visualize": visualize,
        "year": year,
        "months": months,
        "scale_m": scale or default_scale,
        "stats": stats,
        "aoi": aoi_info,
    }


def list_supported_aoi_aliases() -> dict:
    """Return the known city-name shortcuts."""
    return {"aliases": _AOI_ALIASES, "note": "Any FAO GAUL ADM2/ADM1/ADM0 name also works."}


# ---------- internal helpers shared by export tools ----------

def _build_image_for_params(
    source: str, visualize: str, geom: ee.Geometry, start: str, end: str
) -> tuple[ee.Image, dict]:
    """Build the same image add_ee_layer would, returning (image, vis_params)."""
    if visualize == "lst":
        if source != "landsat":
            raise ValueError("LST requires source='landsat'.")
        coll = _landsat_lst(start, end, geom)
        img = coll.median().clip(geom)
        return img, {"min": 25, "max": 50, "palette": _PAL_LST, "bands": ["LST"]}
    coll = _landsat_sr(start, end, geom) if source == "landsat" else _sentinel2(start, end, geom)
    median = coll.median().clip(geom)
    if visualize == "rgb":
        return median.select(["RED", "GREEN", "BLUE"]), {"min": 0.0, "max": 0.30, "gamma": 1.1}
    if visualize == "false_color":
        return median.select(["NIR", "RED", "GREEN"]), {"min": 0.0, "max": 0.35, "gamma": 1.1}
    if visualize == "ndvi":
        return (median.normalizedDifference(["NIR", "RED"]).rename("NDVI"),
                {"min": -0.2, "max": 0.8, "palette": _PAL_NDVI})
    if visualize == "ndbi":
        return (median.normalizedDifference(["SWIR1", "NIR"]).rename("NDBI"),
                {"min": -0.3, "max": 0.4, "palette": _PAL_NDBI})
    raise ValueError(f"Unknown visualize='{visualize}'.")


def _window_dates(year: int, months: list[int]) -> tuple[str, str]:
    start = f"{year}-{min(months):02d}-01"
    end_m = max(months)
    end = f"{year + 1}-01-01" if end_m == 12 else f"{year}-{end_m + 1:02d}-01"
    return start, end


# ---------- GIS asset management ----------

def list_my_assets(parent: str | None = None) -> dict:
    """List Earth Engine assets owned by the authenticated account.

    Args:
        parent: An asset path to list. Defaults to your project's root assets:
                `projects/<EE_PROJECT>/assets`. Pass an asset folder path to
                drill in.

    Returns a dict with `parent` (the path queried) and `assets` (a list of
    {id, type, updateTime} entries).
    """
    ensure_ee()
    if not parent:
        parent = f"projects/{EE_PROJECT}/assets"
    try:
        res = ee.data.listAssets({"parent": parent})
    except ee.EEException as e:
        raise ValueError(f"Could not list assets under '{parent}': {e}")
    assets = []
    for a in (res.get("assets") or []):
        assets.append({
            "id": a.get("id") or a.get("name"),
            "type": a.get("type"),
            "updateTime": a.get("updateTime"),
            "sizeBytes": a.get("sizeBytes"),
        })
    return {"parent": parent, "asset_count": len(assets), "assets": assets}


def add_asset_layer(
    asset_id: str,
    name: str | None = None,
    bands: list[str] | None = None,
    vis_min: float | None = None,
    vis_max: float | None = None,
    palette: list[str] | None = None,
    color: str = "#1d4ed8",
) -> dict:
    """Add a user's EE asset to the map.

    Supports Image, ImageCollection (uses mean composite), and Table
    (FeatureCollection — returned as GeoJSON for vector rendering).

    Args:
        asset_id: Full asset path, e.g. "projects/ee-foo/assets/my_image".
        name: Optional display name. Defaults to the asset's leaf name.
        bands: For images, which bands to display (1 for grayscale, 3 for RGB).
        vis_min, vis_max, palette: standard EE vis params for images.
        color: stroke color for FeatureCollection assets.
    """
    ensure_ee()
    info = ee.data.getAsset(asset_id)
    asset_type = info.get("type", "").upper()
    leaf = asset_id.rsplit("/", 1)[-1]
    display = name or leaf

    if asset_type in ("IMAGE", "IMAGECOLLECTION"):
        if asset_type == "IMAGECOLLECTION":
            img = ee.ImageCollection(asset_id).mean()
        else:
            img = ee.Image(asset_id)
        # Build vis params from whatever the caller provided; if nothing, pick sane defaults.
        vis: dict[str, Any] = {}
        if bands: vis["bands"] = bands
        if vis_min is not None: vis["min"] = vis_min
        if vis_max is not None: vis["max"] = vis_max
        if palette: vis["palette"] = palette
        if not vis:
            # Discover available bands and stretch the first one.
            band_names = img.bandNames().getInfo()
            if not band_names:
                raise ValueError(f"Asset '{asset_id}' has no bands.")
            if len(band_names) >= 3:
                vis = {"bands": band_names[:3], "min": 0, "max": 3000}
            else:
                vis = {"bands": [band_names[0]], "min": 0, "max": 1, "palette": _PAL_NDVI}
        map_id = img.getMapId(vis)
        try:
            bounds = _geom_to_leaflet_bounds(img.geometry())
        except Exception:
            bounds = None
        return {
            "name": f"Asset: {display}",
            "tile_url": map_id["tile_fetcher"].url_format,
            "bounds": bounds,
            "vis": {**vis, "label": f"asset ({asset_type.lower()})"},
            "meta": {"asset_id": asset_id, "asset_type": asset_type},
        }

    if asset_type in ("TABLE", "FEATURECOLLECTION"):
        fc = ee.FeatureCollection(asset_id)
        # Cap at ~5000 features to keep the response manageable.
        size = fc.size().getInfo()
        if size > 5000:
            fc = fc.limit(5000)
        geojson = fc.getInfo()
        try:
            bounds = _geom_to_leaflet_bounds(ee.FeatureCollection(asset_id).geometry())
        except Exception:
            bounds = None
        return {
            "name": f"Asset: {display}",
            "geojson": geojson,
            "bounds": bounds,
            "meta": {
                "asset_id": asset_id,
                "asset_type": asset_type,
                "feature_count": size,
                "displayed": min(size, 5000),
                "color": color,
            },
        }

    raise ValueError(f"Unsupported asset type '{asset_type}' for asset {asset_id}.")


# ---------- exports ----------

def export_layer_to_drive(
    source: str,
    visualize: str,
    aoi: dict | str,
    year: int,
    months: list[int] | None = None,
    folder: str = "EarthEngine",
    file_name: str | None = None,
    scale: int = 30,
    crs: str = "EPSG:4326",
) -> dict:
    """Start a Drive export task for the same image add_ee_layer would build."""
    ensure_ee()
    months = months or [3, 4, 5]
    start, end = _window_dates(year, months)
    geom, _ = resolve_aoi(aoi)
    img, _vis = _build_image_for_params(source, visualize, geom, start, end)
    fname = file_name or f"{source}_{visualize}_{year}_m{min(months)}-{max(months)}"
    task = ee.batch.Export.image.toDrive(
        image=img, description=fname, folder=folder, fileNamePrefix=fname,
        region=geom, scale=scale, crs=crs, maxPixels=1e10,
    )
    task.start()
    return {
        "ok": True,
        "task_id": task.id,
        "state": "SUBMITTED",
        "description": fname,
        "folder": folder,
        "note": "Track progress under code.earthengine.google.com → Tasks. File lands in your Google Drive.",
    }


def get_geotiff_url(
    source: str,
    visualize: str,
    aoi: dict | str,
    year: int,
    months: list[int] | None = None,
    scale: int = 30,
    crs: str = "EPSG:4326",
) -> dict:
    """Return a short-lived direct download URL for a GeoTIFF of the layer.

    EE caps this at ~32 MB per call. For larger AOIs use export_layer_to_drive.
    """
    ensure_ee()
    months = months or [3, 4, 5]
    start, end = _window_dates(year, months)
    geom, _ = resolve_aoi(aoi)
    img, _vis = _build_image_for_params(source, visualize, geom, start, end)
    url = img.getDownloadURL({
        "scale": scale, "crs": crs, "format": "GEO_TIFF",
        "region": geom.getInfo(),
    })
    return {"ok": True, "download_url": url, "note": "Link valid for ~24h. EE caps direct downloads at ~32 MB."}


def shapefile_zip_to_geojson(zip_bytes: bytes) -> dict:
    """Parse a zipped Shapefile and return a GeoJSON FeatureCollection in WGS84.

    Expects a `.zip` containing one shapefile (`.shp` + `.shx` + `.dbf`; `.prj`
    optional but recommended for CRS detection). Non-WGS84 coordinates are
    reprojected to EPSG:4326 so the resulting GeoJSON is EE-ready.
    """
    import datetime
    import io
    import zipfile

    import shapefile  # pyshp
    from pyproj import CRS, Transformer

    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = z.namelist()

    # Find the shapefile basename — there may be a single top-level folder in the zip.
    shp_files = [n for n in names if n.lower().endswith(".shp") and not n.endswith("/")]
    if not shp_files:
        raise ValueError("Zip does not contain a .shp file. Make sure you zipped the .shp + .shx + .dbf together.")
    if len(shp_files) > 1:
        raise ValueError(f"Multiple .shp files in zip: {shp_files}. Provide one shapefile per upload.")
    shp_name = shp_files[0]
    base = shp_name[:-4]

    def _read(part: str) -> bytes:
        # Match case-insensitively because Windows sometimes capitalizes extensions.
        for n in names:
            if n.lower() == part.lower():
                return z.read(n)
        raise ValueError(f"Zip is missing required Shapefile component: {part}")

    shp_bytes = _read(f"{base}.shp")
    shx_bytes = _read(f"{base}.shx")
    dbf_bytes = _read(f"{base}.dbf")
    prj_text = None
    for n in names:
        if n.lower() == f"{base}.prj".lower():
            prj_text = z.read(n).decode("utf-8", errors="ignore")
            break

    sf = shapefile.Reader(
        shp=io.BytesIO(shp_bytes),
        shx=io.BytesIO(shx_bytes),
        dbf=io.BytesIO(dbf_bytes),
    )
    field_names = [f[0] for f in sf.fields[1:]]  # skip the deletion-flag pseudo-field

    def _jsonify(v):
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", errors="replace")
        if isinstance(v, (datetime.date, datetime.datetime)):
            return v.isoformat()
        return v

    features = []
    skipped = 0
    for sr in sf.shapeRecords():
        if sr.shape.shapeType == shapefile.NULL:
            skipped += 1
            continue
        try:
            geom = sr.shape.__geo_interface__
        except Exception:
            skipped += 1
            continue
        if geom is None or geom.get("type") == "GeometryCollection" and not geom.get("geometries"):
            skipped += 1
            continue
        props = {k: _jsonify(v) for k, v in zip(field_names, sr.record)}
        features.append({"type": "Feature", "geometry": geom, "properties": props})

    if not features:
        raise ValueError("Shapefile contained no valid geometries.")

    # Reproject to WGS84 if needed.
    if prj_text:
        try:
            src_crs = CRS.from_wkt(prj_text)
        except Exception:
            src_crs = None
        if src_crs is not None and not src_crs.equals(CRS.from_epsg(4326)):
            transformer = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)

            def _xform(coords):
                if not coords:
                    return coords
                first = coords[0]
                if isinstance(first, (int, float)):
                    lon, lat = transformer.transform(coords[0], coords[1])
                    return [lon, lat] + list(coords[2:])
                return [_xform(c) for c in coords]

            for feat in features:
                g = feat["geometry"]
                if g and "coordinates" in g:
                    g["coordinates"] = _xform(g["coordinates"])

    return {"type": "FeatureCollection", "features": features, "_skipped": skipped}


def upload_geojson_as_asset(geojson: dict, asset_name: str, description: str = "") -> dict:
    """Save a GeoJSON FeatureCollection / Feature / Geometry as a new EE asset.

    Uses Export.table.toAsset, which does NOT need a GCS bucket — EE ingests the
    inline features directly. The asset lands at
    `projects/<EE_PROJECT>/assets/<asset_name>` after the task completes.

    Args:
        geojson: parsed GeoJSON dict (FeatureCollection, Feature, or Geometry).
        asset_name: leaf name for the new asset (no slashes; letters/digits/_/- only).
        description: optional task description.
    """
    ensure_ee()
    if not asset_name or "/" in asset_name or " " in asset_name:
        raise ValueError("asset_name must be a non-empty leaf name without slashes or spaces.")

    t = geojson.get("type")
    if t == "FeatureCollection":
        fc = ee.FeatureCollection(geojson)
    elif t == "Feature":
        fc = ee.FeatureCollection([ee.Feature(geojson.get("geometry"), geojson.get("properties") or {})])
    elif t in ("Polygon", "MultiPolygon", "Point", "MultiPoint", "LineString", "MultiLineString", "GeometryCollection"):
        fc = ee.FeatureCollection([ee.Feature(geojson)])
    else:
        raise ValueError(f"Unsupported GeoJSON type: {t!r}. Expected Feature, FeatureCollection, or a Geometry.")

    asset_id = f"projects/{EE_PROJECT}/assets/{asset_name}"
    task = ee.batch.Export.table.toAsset(
        collection=fc,
        description=description or f"upload_{asset_name}",
        assetId=asset_id,
    )
    task.start()
    return {
        "ok": True,
        "task_id": task.id,
        "asset_id": asset_id,
        "state": "SUBMITTED",
        "note": "Once the task is DONE you can call add_asset_layer with this asset_id.",
    }


def list_export_tasks(limit: int = 10) -> dict:
    """List recent EE export tasks for the authenticated account."""
    ensure_ee()
    out = []
    for t in ee.batch.Task.list()[:limit]:
        s = t.status()
        out.append({
            "id": s.get("id"),
            "description": s.get("description"),
            "state": s.get("state"),
            "type": s.get("task_type"),
            "creation_timestamp_ms": s.get("creation_timestamp_ms"),
            "destination_uris": s.get("destination_uris"),
        })
    return {"task_count": len(out), "tasks": out}

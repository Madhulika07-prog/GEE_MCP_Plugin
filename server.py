"""Google Earth Engine MCP server.

Exposes a set of narrow, LLM-friendly tools plus a generic `run_ee_code`
escape hatch so an MCP client (Claude Code, Claude Desktop) can drive a
user's Earth Engine account in natural language.

Auth model:
- Default: user OAuth from `earthengine authenticate` (credentials in
  ~/.config/earthengine/credentials).
- If EE_SERVICE_ACCOUNT_KEY env var is set to a JSON key path, switch to a
  service-account credential transparently.
- EE_PROJECT env var supplies the Cloud project (required by modern EE).
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import ee
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env sitting next to this file, regardless of cwd.
load_dotenv(Path(__file__).with_name(".env"))

EE_PROJECT = os.environ.get("EE_PROJECT")
EE_SERVICE_ACCOUNT_KEY = os.environ.get("EE_SERVICE_ACCOUNT_KEY")

mcp = FastMCP("earth-engine")

_initialized = False


def _ensure_ee() -> None:
    """Lazily initialize Earth Engine on the first tool call.

    Lazy because (a) we want server import to never fail just because EE
    auth isn't ready yet, and (b) it lets us surface a clear error message
    via a tool result instead of a startup crash the client can't see.
    """
    global _initialized
    if _initialized:
        return
    if not EE_PROJECT:
        raise RuntimeError(
            "EE_PROJECT env var is not set. Put your Google Cloud project ID "
            "in gee-mcp\\.env, e.g.  EE_PROJECT=ee-yourproject"
        )
    if EE_SERVICE_ACCOUNT_KEY:
        key_path = Path(EE_SERVICE_ACCOUNT_KEY).expanduser()
        with open(key_path) as f:
            sa_email = json.load(f)["client_email"]
        creds = ee.ServiceAccountCredentials(sa_email, str(key_path))
        ee.Initialize(creds, project=EE_PROJECT)
    else:
        ee.Initialize(project=EE_PROJECT)
    _initialized = True


def _aoi(geojson: dict | str) -> ee.Geometry:
    """Accept GeoJSON Geometry, Feature, or FeatureCollection and return ee.Geometry."""
    if isinstance(geojson, str):
        geojson = json.loads(geojson)
    t = geojson.get("type")
    if t == "Feature":
        geojson = geojson["geometry"]
    elif t == "FeatureCollection":
        # Union all feature geometries.
        return ee.FeatureCollection(geojson).geometry()
    return ee.Geometry(geojson)


# ---------- diagnostics ----------

@mcp.tool()
def authenticate_status() -> dict:
    """Report whether Earth Engine is reachable and which project/credential is in use.

    Call this first when something looks wrong. Returns a dict with `ok`,
    `project`, `auth_mode`, and on failure an `error` string.
    """
    try:
        _ensure_ee()
        # Tiny round-trip to confirm the credential actually works.
        _ = ee.Number(1).getInfo()
        return {
            "ok": True,
            "project": EE_PROJECT,
            "auth_mode": "service_account" if EE_SERVICE_ACCOUNT_KEY else "user_oauth",
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------- image search & metadata ----------

@mcp.tool()
def search_image_collection(
    collection_id: str,
    aoi_geojson: dict | str,
    start_date: str,
    end_date: str,
    max_cloud_cover: float | None = None,
    cloud_property: str = "CLOUDY_PIXEL_PERCENTAGE",
    limit: int = 20,
) -> list[dict]:
    """Search an ImageCollection for scenes intersecting an AOI in a date range.

    Args:
        collection_id: e.g. "COPERNICUS/S2_SR_HARMONIZED", "LANDSAT/LC09/C02/T1_L2".
        aoi_geojson: GeoJSON Geometry/Feature/FeatureCollection (dict or string).
        start_date: ISO date, e.g. "2024-05-01".
        end_date: ISO date, exclusive.
        max_cloud_cover: optional cloud-cover ceiling (0-100).
        cloud_property: name of the cloud metadata property. Default suits S2.
            Use "CLOUD_COVER" for Landsat C2.
        limit: max scenes to return.

    Returns list of dicts with id, date, cloud cover, and footprint centroid.
    """
    _ensure_ee()
    coll = ee.ImageCollection(collection_id).filterBounds(_aoi(aoi_geojson)).filterDate(start_date, end_date)
    if max_cloud_cover is not None:
        coll = coll.filter(ee.Filter.lt(cloud_property, max_cloud_cover))
    coll = coll.limit(limit)

    def feat(img):
        img = ee.Image(img)
        return ee.Feature(img.geometry().centroid(1), {
            "id": img.get("system:id"),
            "date": ee.Date(img.get("system:time_start")).format("YYYY-MM-dd'T'HH:mm:ss"),
            "cloud": img.get(cloud_property),
        })

    fc = coll.map(feat)
    return fc.getInfo().get("features", [])


@mcp.tool()
def image_metadata(image_id: str) -> dict:
    """Return bands, acquisition date, footprint, and all properties of a single image."""
    _ensure_ee()
    img = ee.Image(image_id)
    info = img.getInfo()
    # Trim band info to the useful bits to keep the response small.
    bands = [
        {"id": b.get("id"), "data_type": b.get("data_type"), "crs": b.get("crs")}
        for b in info.get("bands", [])
    ]
    return {
        "id": info.get("id"),
        "bands": bands,
        "properties": info.get("properties", {}),
    }


# ---------- common indices ----------

_INDEX_FORMULAS = {
    # Sentinel-2 band names by default; override via band_map.
    "NDVI": ("NIR", "RED"),
    "NDWI": ("GREEN", "NIR"),
    "NBR":  ("NIR", "SWIR2"),
    "EVI":  None,  # special-cased below
}

_S2_BAND_MAP = {"BLUE": "B2", "GREEN": "B3", "RED": "B4", "NIR": "B8", "SWIR1": "B11", "SWIR2": "B12"}
_LS_BAND_MAP = {"BLUE": "SR_B2", "GREEN": "SR_B3", "RED": "SR_B4", "NIR": "SR_B5", "SWIR1": "SR_B6", "SWIR2": "SR_B7"}


def _band_map_for(collection_id: str, override: dict | None) -> dict:
    if override:
        return override
    cid = collection_id.upper()
    if "LANDSAT" in cid:
        return _LS_BAND_MAP
    return _S2_BAND_MAP  # default to Sentinel-2 SR


def _compute_index_image(img: ee.Image, index: str, bands: dict) -> ee.Image:
    if index == "EVI":
        return img.expression(
            "2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))",
            {"NIR": img.select(bands["NIR"]), "RED": img.select(bands["RED"]), "BLUE": img.select(bands["BLUE"])},
        ).rename("EVI")
    b1, b2 = _INDEX_FORMULAS[index]
    return img.normalizedDifference([bands[b1], bands[b2]]).rename(index)


@mcp.tool()
def compute_index(
    index: str,
    collection_id: str,
    aoi_geojson: dict | str,
    start_date: str,
    end_date: str,
    reducer: str = "median",
    scale: int = 30,
    band_map: dict | None = None,
) -> dict:
    """Compute a spectral index (NDVI, NDWI, NBR, EVI) over an AOI/date range.

    Returns a dict with `mean`, `min`, `max`, `stdDev`, and `count` of pixels
    sampled. Use `reducer` to control how the time series is collapsed before
    spatial stats: "median" (default), "mean", "max", "min".
    """
    _ensure_ee()
    index = index.upper()
    if index not in _INDEX_FORMULAS:
        raise ValueError(f"Unknown index '{index}'. Supported: {list(_INDEX_FORMULAS)}")
    bands = _band_map_for(collection_id, band_map)
    geom = _aoi(aoi_geojson)

    coll = ee.ImageCollection(collection_id).filterBounds(geom).filterDate(start_date, end_date)
    indexed = coll.map(lambda img: _compute_index_image(ee.Image(img), index, bands))

    reducers = {"median": ee.Reducer.median(), "mean": ee.Reducer.mean(),
                "max": ee.Reducer.max(), "min": ee.Reducer.min()}
    if reducer not in reducers:
        raise ValueError(f"Unknown reducer '{reducer}'. Use: {list(reducers)}")
    composite = indexed.reduce(reducers[reducer]).rename(index)

    stats = composite.reduceRegion(
        reducer=ee.Reducer.mean().combine(ee.Reducer.minMax(), sharedInputs=True)
                              .combine(ee.Reducer.stdDev(), sharedInputs=True)
                              .combine(ee.Reducer.count(), sharedInputs=True),
        geometry=geom, scale=scale, maxPixels=1e10, bestEffort=True,
    ).getInfo()
    return {"index": index, "reducer": reducer, "scale": scale, "stats": stats}


# ---------- thumbnails (cheap preview, no export task) ----------

@mcp.tool()
def get_thumbnail_url(
    image_id: str,
    aoi_geojson: dict | str | None = None,
    bands: list[str] | None = None,
    min: float | None = None,
    max: float | None = None,
    gamma: float | None = None,
    palette: list[str] | None = None,
    dimensions: int = 512,
) -> dict:
    """Return a temporary PNG URL for an image with the given visualization params.

    Cheap, synchronous, and no export task required. Good for "show me what
    this looks like" before kicking off a real export.
    """
    _ensure_ee()
    img = ee.Image(image_id)
    vis: dict[str, Any] = {"dimensions": dimensions}
    if bands: vis["bands"] = bands
    if min is not None: vis["min"] = min
    if max is not None: vis["max"] = max
    if gamma is not None: vis["gamma"] = gamma
    if palette: vis["palette"] = palette
    if aoi_geojson is not None:
        vis["region"] = _aoi(aoi_geojson).getInfo()
    return {"url": img.getThumbURL(vis)}


# ---------- exports ----------

@mcp.tool()
def export_image_to_drive(
    image_id: str,
    aoi_geojson: dict | str,
    description: str,
    folder: str = "EarthEngine",
    file_name_prefix: str | None = None,
    scale: int = 10,
    crs: str = "EPSG:4326",
    max_pixels: float = 1e10,
) -> dict:
    """Start an Earth Engine -> Google Drive export task. Returns the task id.

    Poll progress with `list_tasks`. The image lands in the chosen Drive folder
    under the user account that authenticated EE.
    """
    _ensure_ee()
    task = ee.batch.Export.image.toDrive(
        image=ee.Image(image_id),
        description=description,
        folder=folder,
        fileNamePrefix=file_name_prefix or description,
        region=_aoi(aoi_geojson),
        scale=scale,
        crs=crs,
        maxPixels=max_pixels,
    )
    task.start()
    return {"task_id": task.id, "description": description, "state": "SUBMITTED"}


@mcp.tool()
def export_image_to_gcs(
    image_id: str,
    aoi_geojson: dict | str,
    description: str,
    bucket: str,
    file_name_prefix: str | None = None,
    scale: int = 10,
    crs: str = "EPSG:4326",
    max_pixels: float = 1e10,
) -> dict:
    """Start an Earth Engine -> Google Cloud Storage export task. Returns the task id."""
    _ensure_ee()
    task = ee.batch.Export.image.toCloudStorage(
        image=ee.Image(image_id),
        description=description,
        bucket=bucket,
        fileNamePrefix=file_name_prefix or description,
        region=_aoi(aoi_geojson),
        scale=scale,
        crs=crs,
        maxPixels=max_pixels,
    )
    task.start()
    return {"task_id": task.id, "description": description, "state": "SUBMITTED", "bucket": bucket}


@mcp.tool()
def get_download_url(
    image_id: str,
    aoi_geojson: dict | str,
    scale: int = 10,
    crs: str = "EPSG:4326",
    file_format: str = "GeoTIFF",
) -> dict:
    """Return a short-lived URL to download a GeoTIFF directly (no Drive/GCS).

    EE caps this at ~32 MB per call; for larger AOIs use export_image_to_drive
    or export_image_to_gcs instead.
    """
    _ensure_ee()
    url = ee.Image(image_id).getDownloadURL({
        "scale": scale, "crs": crs, "format": file_format,
        "region": _aoi(aoi_geojson).getInfo(),
    })
    return {"url": url}


@mcp.tool()
def list_tasks(limit: int = 20) -> list[dict]:
    """List recent EE export tasks for the authenticated account."""
    _ensure_ee()
    out = []
    for t in ee.batch.Task.list()[:limit]:
        s = t.status()
        out.append({
            "id": s.get("id"), "description": s.get("description"),
            "state": s.get("state"), "task_type": s.get("task_type"),
            "creation_timestamp_ms": s.get("creation_timestamp_ms"),
        })
    return out


# ---------- asset management ----------

@mcp.tool()
def list_assets(parent: str) -> list[dict]:
    """List child assets under an EE asset path.

    `parent` examples:
      - "projects/earthengine-legacy/assets/users/<username>"
      - "projects/<your-cloud-project>/assets"
    """
    _ensure_ee()
    res = ee.data.listAssets({"parent": parent})
    return res.get("assets", [])


@mcp.tool()
def describe_asset(asset_id: str) -> dict:
    """Return type, owner, ACL, properties of a single EE asset."""
    _ensure_ee()
    return ee.data.getAsset(asset_id)


@mcp.tool()
def delete_asset(asset_id: str, confirm: bool = False) -> dict:
    """Delete an EE asset. Requires confirm=True as a safety gate.

    Irreversible — once deleted the asset and its contents are gone.
    """
    if not confirm:
        return {"ok": False, "error": "Refusing to delete without confirm=True"}
    _ensure_ee()
    ee.data.deleteAsset(asset_id)
    return {"ok": True, "deleted": asset_id}


# ---------- generic escape hatch ----------

@mcp.tool()
def run_ee_code(code: str) -> dict:
    """Run arbitrary Python code with the `ee` module pre-imported and initialized.

    Captures stdout. If the code ends with an expression (no trailing
    assignment), its value is returned in `result`; if that value is an EE
    object, `.getInfo()` is called on it. Use this when the narrow tools
    above don't cover what you need.

    Use only for code you understand — anything you can do here, the user's
    EE account can do (including starting paid compute or modifying assets).
    """
    _ensure_ee()
    buf = io.StringIO()
    local_ns: dict[str, Any] = {"ee": ee}
    try:
        with redirect_stdout(buf):
            # Try eval first (expression); fall back to exec (statements).
            try:
                value = eval(code, local_ns)
            except SyntaxError:
                exec(code, local_ns)
                value = local_ns.get("_")
        if hasattr(value, "getInfo"):
            value = value.getInfo()
        return {"ok": True, "stdout": buf.getvalue(), "result": value}
    except Exception as e:
        return {
            "ok": False,
            "stdout": buf.getvalue(),
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }


if __name__ == "__main__":
    mcp.run()

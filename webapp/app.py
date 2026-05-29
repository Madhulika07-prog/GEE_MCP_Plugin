"""GEE chat-map web app.

Single-page UI: chat panel on the left, Leaflet map on the right. The browser
posts user messages to /chat; the backend runs a user-selected LLM (Gemini,
Claude, GPT, Groq Llama, Mistral, OpenRouter) with EE tools that produce map
tile URLs, statistics, and exports.

Single-user, runs on localhost. Do not expose to the network.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import ee_tools
import llm_providers

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")

DEFAULT_MODEL = "gemini-2.5-flash"

app = FastAPI(title="GEE Chat Map")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Per-session state: {session_id: {"history": [...], "model_id": str}}
SESSIONS: dict[str, dict] = {}


SYSTEM_PROMPT = """You are an Earth Engine map assistant. The user sees a live Leaflet map on the right and chats with you on the left. Your job is to translate natural-language requests into Earth Engine layers and statistics by calling the provided tools.

Tools you have:
- add_ee_layer: build a tile URL for an EE-derived layer and add it to the map. (Composite from Landsat/Sentinel-2.)
- add_admin_outline: overlay an administrative boundary as a vector outline.
- compute_zonal_stats: return numeric statistics for an index over an AOI without adding a layer.
- clear_map_layers: wipe all user-added layers from the map.
- list_my_assets: list the user's own EE assets at a given path. Pass an empty string to list project root.
- add_asset_layer: visualize one of the user's EE assets (Image or FeatureCollection) on the map.
- export_layer_to_drive: kick off an Earth Engine -> Google Drive export task for a layer.
- get_geotiff_url: return a short-lived direct GeoTIFF download URL (capped ~32 MB).
- list_export_tasks: list recent export tasks.

Sensors and supported visualizations:
- source="landsat" (Landsat 8 + 9 Collection 2 Level-2, 30 m) supports rgb, false_color, ndvi, ndbi, lst.
- source="sentinel2" (Sentinel-2 SR Harmonized, 10 m) supports rgb, false_color, ndvi, ndbi. No thermal band - use Landsat for LST.

AOI handling: pass a string place name (e.g. "Bangalore", "Mumbai", "Karnataka", "India"). Backend resolves through FAO/GAUL.

Date defaults: if user does not specify months, use [3,4,5] (pre-monsoon). State the window you used.

Style:
- Be concise. After tool calls, 1-3 sentences describing what you did. The map shows the result.
- "LST from Sentinel-2" -> politely correct and use Landsat.
- Multiple layers in one request -> call add_ee_layer multiple times in the same turn.
- Ambiguous request -> pick a sensible default and call the tool. Don't ping-pong with clarifying questions.
- When the user asks to see their own data, call list_my_assets first; suggest add_asset_layer for promising ones.
- For exports: use export_layer_to_drive for full city/region scale; use get_geotiff_url only when the user wants a quick direct download for a small AOI.
"""


# ---- tool declarations (provider-agnostic; adapters convert to native schemas) ----

TOOLS = [
    {
        "name": "add_ee_layer",
        "description": "Build an Earth Engine layer over an AOI/date range and add it to the live map.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["landsat", "sentinel2"]},
                "visualize": {"type": "string", "enum": ["rgb", "false_color", "ndvi", "ndbi", "lst"]},
                "aoi": {"type": "string", "description": "Place name (city/district/country)."},
                "year": {"type": "integer"},
                "months": {"type": "array", "items": {"type": "integer"}, "description": "Defaults to [3,4,5]."},
                "name": {"type": "string", "description": "Optional layer label."},
            },
            "required": ["source", "visualize", "aoi", "year"],
        },
    },
    {
        "name": "add_admin_outline",
        "description": "Overlay a city/district/country administrative boundary as a vector outline.",
        "parameters": {
            "type": "object",
            "properties": {
                "aoi": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["aoi"],
        },
    },
    {
        "name": "compute_zonal_stats",
        "description": "Return mean/min/max/stdDev of NDVI, NDBI, or LST over an AOI/date range. Does NOT add a layer.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["landsat", "sentinel2"]},
                "visualize": {"type": "string", "enum": ["ndvi", "ndbi", "lst"]},
                "aoi": {"type": "string"},
                "year": {"type": "integer"},
                "months": {"type": "array", "items": {"type": "integer"}},
                "scale": {"type": "integer"},
            },
            "required": ["source", "visualize", "aoi", "year"],
        },
    },
    {
        "name": "clear_map_layers",
        "description": "Remove all user-added layers from the map.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_my_assets",
        "description": "List the user's Earth Engine assets. Returns id, type, and update time for each. Pass parent='' to list the project root.",
        "parameters": {
            "type": "object",
            "properties": {
                "parent": {"type": "string", "description": "Asset path to list (e.g. 'projects/ee-foo/assets'). Empty for project root."},
            },
        },
    },
    {
        "name": "add_asset_layer",
        "description": "Add one of the user's Earth Engine assets to the map. Supports Image, ImageCollection, and FeatureCollection (Table) assets.",
        "parameters": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string", "description": "Full asset path, e.g. 'projects/ee-foo/assets/my_image'."},
                "name": {"type": "string", "description": "Optional display name."},
                "bands": {"type": "array", "items": {"type": "string"}, "description": "Bands to display (1 for grayscale, 3 for RGB)."},
                "vis_min": {"type": "number"},
                "vis_max": {"type": "number"},
                "palette": {"type": "array", "items": {"type": "string"}},
                "color": {"type": "string", "description": "Stroke color for FeatureCollection assets (hex)."},
            },
            "required": ["asset_id"],
        },
    },
    {
        "name": "export_layer_to_drive",
        "description": "Start an Earth Engine -> Google Drive export task for an EE layer. Returns a task ID. Output file appears in the user's Drive once complete.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["landsat", "sentinel2"]},
                "visualize": {"type": "string", "enum": ["rgb", "false_color", "ndvi", "ndbi", "lst"]},
                "aoi": {"type": "string"},
                "year": {"type": "integer"},
                "months": {"type": "array", "items": {"type": "integer"}},
                "folder": {"type": "string", "description": "Drive folder name. Default 'EarthEngine'."},
                "file_name": {"type": "string", "description": "Output file name prefix."},
                "scale": {"type": "integer", "description": "Pixel scale in meters (default 30)."},
            },
            "required": ["source", "visualize", "aoi", "year"],
        },
    },
    {
        "name": "get_geotiff_url",
        "description": "Get a short-lived direct download URL for a GeoTIFF of an EE layer. EE caps this at ~32 MB so use it only for small AOIs.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["landsat", "sentinel2"]},
                "visualize": {"type": "string", "enum": ["rgb", "false_color", "ndvi", "ndbi", "lst"]},
                "aoi": {"type": "string"},
                "year": {"type": "integer"},
                "months": {"type": "array", "items": {"type": "integer"}},
                "scale": {"type": "integer"},
            },
            "required": ["source", "visualize", "aoi", "year"],
        },
    },
    {
        "name": "list_export_tasks",
        "description": "List recent Earth Engine export tasks for the authenticated account.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max tasks to return. Default 10."},
            },
        },
    },
]


def _execute_tool(name: str, args: dict) -> tuple[dict, dict | None]:
    """Run an EE tool. Returns (model_visible_result, frontend_op_or_None)."""
    if name == "add_ee_layer":
        layer = ee_tools.add_ee_layer(**args)
        return (
            {"ok": True, "name": layer["name"], "scene_count": layer["meta"]["scene_count"],
             "window": f'{layer["meta"]["start"]}..{layer["meta"]["end"]}'},
            {"op": "add_layer", "layer": layer},
        )
    if name == "add_admin_outline":
        outline = ee_tools.add_admin_outline(**args)
        return ({"ok": True, "name": outline["name"]}, {"op": "add_outline", "outline": outline})
    if name == "compute_zonal_stats":
        stats = ee_tools.compute_zonal_stats(**args)
        return stats, {"op": "show_stats", "stats": stats}
    if name == "clear_map_layers":
        return {"ok": True, "cleared": True}, {"op": "clear_layers"}
    if name == "list_my_assets":
        result = ee_tools.list_my_assets(args.get("parent") or None)
        return result, {"op": "show_assets", "assets": result}
    if name == "add_asset_layer":
        layer = ee_tools.add_asset_layer(**args)
        # Distinguish raster asset vs feature-collection asset by what's in the dict.
        if "tile_url" in layer:
            return ({"ok": True, "name": layer["name"], "asset_type": layer["meta"]["asset_type"]},
                    {"op": "add_layer", "layer": layer})
        return ({"ok": True, "name": layer["name"], "asset_type": layer["meta"]["asset_type"],
                 "feature_count": layer["meta"]["feature_count"]},
                {"op": "add_outline", "outline": layer})
    if name == "export_layer_to_drive":
        result = ee_tools.export_layer_to_drive(**args)
        return result, {"op": "export_started", "task": result}
    if name == "get_geotiff_url":
        result = ee_tools.get_geotiff_url(**args)
        return result, {"op": "download_url", "url": result["download_url"], "note": result.get("note", "")}
    if name == "list_export_tasks":
        result = ee_tools.list_export_tasks(**args)
        return result, {"op": "show_tasks", "tasks": result["tasks"]}
    raise ValueError(f"Unknown tool: {name}")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    model_id: str | None = None


def _build_system_prompt() -> str:
    """Re-render the system prompt each turn, injecting any recently-uploaded AOIs.

    This is how the LLM finds out that the user uploaded "Testing" via the
    modal — we don't pollute the chat history with synthetic messages.
    """
    uploads = ee_tools.recent_upload_names()
    if not uploads:
        return SYSTEM_PROMPT
    upload_list = ", ".join(f'"{u}"' for u in uploads)
    addendum = (
        "\n\nRecent uploads available as AOI names: " + upload_list + ". "
        "If the user says 'this shapefile', 'my upload', or refers to an AOI "
        "without naming it, prefer the most recently uploaded name. You can "
        "pass it as `aoi` to any tool just like a place name."
    )
    return SYSTEM_PROMPT + addendum


def _run_chat_sync(session: dict, user_message: str) -> tuple[str, list[dict]]:
    """Run one user turn through the selected LLM provider."""
    return llm_providers.run_chat(
        model_id=session["model_id"],
        history=session["history"],
        user_message=user_message,
        tools=TOOLS,
        system=_build_system_prompt(),
        execute_tool=_execute_tool,
    )


@app.post("/chat")
async def chat(req: ChatRequest):
    session_id = req.session_id or secrets.token_urlsafe(8)
    session = SESSIONS.setdefault(session_id, {"history": [], "model_id": DEFAULT_MODEL})
    # Allow the client to switch model mid-conversation. The internal history
    # is provider-agnostic, so the new provider can pick up where the old one left off.
    if req.model_id and req.model_id != session["model_id"]:
        if llm_providers.get_spec(req.model_id) is None:
            raise HTTPException(400, f"Unknown model id: {req.model_id}")
        session["model_id"] = req.model_id
    try:
        assistant_text, ops = await run_in_threadpool(_run_chat_sync, session, req.message)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"LLM error: {type(e).__name__}: {e}")
    return JSONResponse({
        "session_id": session_id,
        "model_id": session["model_id"],
        "text": assistant_text,
        "operations": ops,
    })


@app.post("/reset")
async def reset(payload: dict):
    sid = payload.get("session_id")
    if sid and sid in SESSIONS:
        SESSIONS.pop(sid)
    return {"ok": True}


@app.post("/upload-asset")
async def upload_asset(
    file: UploadFile = File(...),
    asset_name: str = Form(...),
    description: str = Form(""),
):
    """Upload a GeoJSON file as a new Earth Engine asset.

    Uses Export.table.toAsset — no GCS bucket needed. Returns the task ID;
    once the task completes the asset is available at
    `projects/<EE_PROJECT>/assets/<asset_name>`.
    """
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "File too large (20 MB max). Split it or use the EE Code Editor for large uploads.")

    # Detect format from filename / magic bytes.
    fname = (file.filename or "").lower()
    is_zip = fname.endswith(".zip") or raw[:4] == b"PK\x03\x04"

    if is_zip:
        # Parse the Shapefile zip into GeoJSON, then go through the same EE path.
        try:
            geojson = await run_in_threadpool(ee_tools.shapefile_zip_to_geojson, raw)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"Shapefile parse failed: {type(e).__name__}: {e}")
        skipped = geojson.pop("_skipped", 0)
    else:
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as e:
            raise HTTPException(400, f"File is not valid UTF-8 text: {e}")
        try:
            geojson = json.loads(text)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"Not valid JSON: {e}")
        if not isinstance(geojson, dict) or "type" not in geojson:
            raise HTTPException(400, "File does not look like GeoJSON (missing top-level 'type').")
        skipped = 0

    try:
        result = await run_in_threadpool(
            ee_tools.upload_geojson_as_asset, geojson, asset_name, description
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {type(e).__name__}: {e}")
    # Cache the parsed geometry so the chat can use it as an AOI immediately,
    # without waiting on the EE ingestion task.
    ee_tools.register_uploaded_aoi(asset_name, geojson)
    if skipped:
        result["skipped_geometries"] = skipped
    result["feature_count"] = len(geojson.get("features", []))
    result["input_format"] = "shapefile_zip" if is_zip else "geojson"
    return result


@app.post("/export-layer")
async def export_layer(payload: dict):
    """Export a layer the user clicked Export on.

    Payload shape: {format: "drive" | "geotiff_url", meta: {source, visualize,
    aoi_input, year, months}, scale?, folder?, file_name?}
    """
    fmt = payload.get("format")
    meta = payload.get("meta") or {}
    aoi = meta.get("aoi_input") or meta.get("aoi")
    if not aoi:
        raise HTTPException(400, "Cannot export this layer: missing AOI metadata. Re-create the layer via chat.")
    common = dict(
        source=meta["source"], visualize=meta["visualize"],
        aoi=aoi, year=meta["year"], months=meta.get("months"),
    )
    try:
        if fmt == "drive":
            result = await run_in_threadpool(
                ee_tools.export_layer_to_drive,
                **common,
                folder=payload.get("folder", "EarthEngine"),
                file_name=payload.get("file_name"),
                scale=payload.get("scale", 30),
            )
        elif fmt == "geotiff_url":
            result = await run_in_threadpool(
                ee_tools.get_geotiff_url,
                **common,
                scale=payload.get("scale", 30),
            )
        else:
            raise HTTPException(400, f"Unknown format: {fmt!r}. Use 'drive' or 'geotiff_url'.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Export failed: {type(e).__name__}: {e}")
    return result


@app.get("/models")
async def models():
    return {"models": llm_providers.list_available_models(), "default": DEFAULT_MODEL}


@app.get("/health")
async def health():
    try:
        ee_tools.ensure_ee()
        ee_ok = True
        ee_err = None
    except Exception as e:
        ee_ok = False
        ee_err = f"{type(e).__name__}: {e}"
    # Report which provider keys are present.
    keys = {
        p: llm_providers.get_provider_key(p) is not None
        for p in llm_providers.PROVIDER_CONFIG
    }
    return {
        "ee_ok": ee_ok,
        "ee_error": ee_err,
        "default_model": DEFAULT_MODEL,
        "provider_keys_set": keys,
        "active_sessions": len(SESSIONS),
    }


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)

# GEE_MCP_Plugin

Natural-language control of Google Earth Engine, two ways:

1. **MCP server** (`server.py`) — exposes EE as tools to any MCP client (Claude Code, Claude Desktop). Chat with Claude, it calls EE on your behalf.
2. **Chat-map web app** (`webapp/`) — a single-page browser app: chat panel on the left, live Leaflet map on the right. Pick from Gemini (free), Claude, GPT, Llama-on-Groq, Mistral, or OpenRouter as the model. Upload GeoJSON or Shapefile vector data, kick off Drive exports, get GeoTIFF downloads.

Built on top of your own Google Earth Engine account using your own auth — no third-party servers in the data path.

## What it can do

- **Imagery composites** from Landsat 8/9 and Sentinel-2 SR Harmonized over any AOI / date range
- **Spectral indices**: NDVI, NDBI, false-color, and Land Surface Temperature (LST, Landsat only — S2 has no thermal band)
- **Admin boundary AOIs** by name (FAO/GAUL — *"Bangalore"*, *"Pune"*, *"Karnataka"*, *"India"* all just work)
- **Your own EE assets** — list, visualize on the map, export
- **Vector data upload** — GeoJSON or zipped Shapefile (.shp + .shx + .dbf + .prj). Non-WGS84 CRS gets reprojected automatically via `pyproj`. Goes to EE via `Export.table.toAsset` so no Google Cloud Storage bucket is needed.
- **Exports** — per-layer button: send to Google Drive as a task, or get a short-lived direct GeoTIFF download URL
- **Zonal statistics** — mean / min / max / stdDev for indices over any AOI

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Browser (Leaflet map + chat panel)                                 │
│   • Layers panel with toggles, remove, export                       │
│   • Upload modal for GeoJSON / Shapefile                            │
│   • Model selector (free + paid)                                    │
└────────────────┬─────────────────────────────────────────────────────┘
                 │ HTTP POST /chat, /upload-asset, /export-layer
                 ▼
┌──────────────────────────────────────────────────────────────────────┐
│  FastAPI backend (webapp/app.py)                                    │
│   • llm_providers.py — Gemini / Claude / GPT / Groq / Mistral /     │
│     OpenRouter unified behind one interface                          │
│   • ee_tools.py — EE layer building, asset management, exports      │
│   • Per-session chat state, system-prompt augmentation              │
└────────────────┬─────────────────────────────────────────────────────┘
                 │  Earth Engine Python API
                 ▼
            ┌────────────┐
            │ Earth Engine │  (your project, your auth)
            └────────────┘

──────────────────────────────────────────────────────────────────────
Independently, the same EE-tools layer is also exposed as an MCP server
(server.py) for use directly from Claude Code / Claude Desktop.
──────────────────────────────────────────────────────────────────────
```

## Quick start

### 1. Prereqs

- Python 3.11+
- A Google Earth Engine account with a registered Cloud project — see https://signup.earthengine.google.com

### 2. Install

```bash
git clone https://github.com/Madhulika07-prog/GEE_MCP_Plugin.git gee-mcp
cd gee-mcp
python -m pip install -r requirements.txt
```

### 3. Authenticate to Earth Engine (one-time, opens your browser)

```bash
python -m ee.cli.eecli authenticate
# or the script directly:
earthengine authenticate
```

Sign in with the Google account that owns your EE project. The credential goes to `%USERPROFILE%\.config\earthengine\credentials` on Windows or `~/.config/earthengine/credentials` elsewhere.

### 4. Configure `.env`

Copy `.env.example` to `.env` and fill in:

```dotenv
# Required: your Earth Engine Cloud project ID
EE_PROJECT=ee-yourproject

# At least one LLM provider key. Gemini's is free at https://aistudio.google.com/apikey
GEMINI_API_KEY=AIzaSy...

# Optional: enable other providers
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# GROQ_API_KEY=gsk_...              # free, fast, Llama 3.3 70B
# MISTRAL_API_KEY=...                # free tier on Mistral Small
# OPENROUTER_API_KEY=sk-or-...       # proxies free + paid models
```

### 5. Run the web app

```bash
cd webapp
python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765. Try:

- *"Show NDVI over Bangalore for May 2024"*
- *"List my Earth Engine assets"*
- *"Compare 2015 vs 2024 LST over Pune"*

Then click 📁 Upload to push a shapefile to EE, or ⬇ on any layer to export it.

### 6. (Optional) Use the MCP server with Claude Code or Claude Desktop

Add to `~/.claude.json` (Claude Code) or `claude_desktop_config.json` (Claude Desktop):

```json
{
  "mcpServers": {
    "earth-engine": {
      "type": "stdio",
      "command": "C:\\Python314\\python.exe",
      "args": ["C:\\path\\to\\gee-mcp\\server.py"]
    }
  }
}
```

Restart the client. Then in chat you can do *"using the earth-engine MCP, compute mean NDVI for Bangalore in May 2024"* and Claude will drive EE for you.

## Supported models in the web app

The dropdown in the chat header shows every model the catalog knows about, with a 🆓 / 💳 tag and per-provider availability based on which API keys you've set:

| Provider | Key env var | Free? | Notes |
|---|---|---|---|
| Google Gemini | `GEMINI_API_KEY` | Yes (free tier) | Default. `gemini-2.5-flash` is the recommended starting point. |
| Groq | `GROQ_API_KEY` | Yes (free tier) | Llama 3.3 70B — fastest inference, very good tool use |
| Mistral | `MISTRAL_API_KEY` | Yes (free for small) | `mistral-small-latest` is free |
| OpenRouter | `OPENROUTER_API_KEY` | Some `:free` models | Proxies many providers |
| Anthropic Claude | `ANTHROPIC_API_KEY` | No (paid) | Opus / Sonnet / Haiku |
| OpenAI | `OPENAI_API_KEY` | No (paid) | GPT-4o family |

Switching model mid-conversation is supported — history is provider-agnostic internally and converts to each model's native format on demand.

## File layout

```
gee-mcp/
├── server.py                  # MCP server (Claude Code / Desktop integration)
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md                  # this file
├── claude_desktop_config.snippet.json   # paste into Claude Desktop config
├── docs/
│   └── bangalore-uhi.md       # worked example: 2015 vs 2024 Bangalore UHI
└── webapp/
    ├── app.py                 # FastAPI backend
    ├── ee_tools.py            # EE layer building, asset mgmt, exports
    ├── llm_providers.py       # unified Gemini/Claude/GPT/Groq/Mistral adapter
    ├── templates/
    │   └── index.html
    └── static/
        ├── app.js
        └── style.css
```

## Status / limitations

- **Vector upload only.** Raster image upload to EE requires a Google Cloud Storage bucket (EE's `startIngestion` workflow). Not yet wired in.
- **KML upload not yet supported.** Easy to add with `fastkml` — open an issue if you want it.
- **20 MB upload cap** in the modal — use the EE Code Editor's native uploader for larger files.
- **Single-user, runs on localhost.** Don't expose port 8765 to the network without adding auth + CORS handling.
- **In-memory session state.** Restarting the web app clears chat history and the uploaded-AOI cache (uploaded assets in EE persist; only the immediately-usable client-side cache is lost).

## Built with

- [Google Earth Engine](https://earthengine.google.com) Python API
- [FastAPI](https://fastapi.tiangolo.com) + [Uvicorn](https://www.uvicorn.org)
- [Leaflet](https://leafletjs.com) + [CARTO](https://carto.com/) basemap
- [google-genai](https://pypi.org/project/google-genai/) · [anthropic](https://pypi.org/project/anthropic/) · [openai](https://pypi.org/project/openai/)
- [pyshp](https://pypi.org/project/pyshp/) + [pyproj](https://pypi.org/project/pyproj/) for Shapefile parsing and reprojection
- [Model Context Protocol](https://modelcontextprotocol.io) for the Claude Code / Desktop bridge

import os
import sys
import signal
import time
import gc
import json
import glob
import requests
import mimetypes
import hashlib
import secrets
from urllib.parse import quote, unquote
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Response, Cookie, Query
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Ensure parent directory (app root) is in Python path for importing shared
GATEWAY_DIR = os.path.abspath(os.path.dirname(__file__))
BASE_DIR = os.path.abspath(os.path.join(GATEWAY_DIR, ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from shared import api as wangp_api

# ═══════════════════════════ SageAttention SM89 Patch ═══════════════════════════

try:
    import shared.sage2_core as sage2_core
    if hasattr(sage2_core, "sageattn") and not getattr(sage2_core, "SM89_ENABLED", False):
        _orig_sageattn = sage2_core.sageattn
        def _patched_sageattn(qkv_list, tensor_layout="HND", is_causal=False, sm_scale=None, return_lse=False, recycle_q=False, **kwargs):
            try:
                arch = sage2_core._get_cuda_arch(qkv_list[0].device)
                if arch == "sm89" and not sage2_core.SM89_ENABLED:
                    return sage2_core.sageattn_qk_int8_pv_fp16_triton(
                        qkv_list,
                        tensor_layout=tensor_layout,
                        is_causal=is_causal,
                        sm_scale=sm_scale,
                        return_lse=return_lse,
                        **kwargs
                    )
            except Exception as e:
                print(f"[Gateway Patch Warning] Fallback failed: {e}")
            return _orig_sageattn(qkv_list, tensor_layout=tensor_layout, is_causal=is_causal, sm_scale=sm_scale, return_lse=return_lse, recycle_q=recycle_q, **kwargs)
        sage2_core.sageattn = _patched_sageattn
        print("[Gateway Patch] Applied in-memory SM89 Triton fallback patch to SageAttention.")
except Exception as e:
    print(f"[Gateway Patch] Failed to apply patch: {e}")


# ═══════════════════════════ Config ═══════════════════════════

SECRET_TOKEN = os.environ.get("GATEWAY_TOKEN", "my_super_secret_cookcalai_token_999")
GATEWAY_CONFIG_FILE = os.path.join(GATEWAY_DIR, "gateway_config.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ═══════════════════════════ Auth ═══════════════════════════

# Session tokens for authenticated browser sessions
_active_sessions: set[str] = set()

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def _create_session() -> str:
    session_id = secrets.token_urlsafe(32)
    _active_sessions.add(session_id)
    return session_id

def _verify_session(session_id: str | None) -> bool:
    return session_id is not None and session_id in _active_sessions

def _is_localhost(request: Request) -> bool:
    """Check if request comes from localhost or docker host interface"""
    host_header = (request.headers.get("host") or "").split(":")[0]
    if host_header in ("localhost", "127.0.0.1", "::1"):
        return True
    client = request.client
    if client is None:
        return False
    return client.host in ("127.0.0.1", "::1", "localhost")

def _verify_token_from_request(request: Request) -> bool:
    """Check token from localhost, cookie, query param, or Authorization header"""
    # 0. Localhost = always trusted (no token needed)
    if _is_localhost(request):
        return True
    # 1. Cookie session
    session_id = request.cookies.get("gw_session")
    if _verify_session(session_id):
        return True
    # 2. Query parameter
    token = request.query_params.get("token")
    if token == SECRET_TOKEN:
        return True
    # 3. Authorization header
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == SECRET_TOKEN:
        return True
    return False

# ═══════════════════════════ Gateway Config Management ═══════════════════════════

DEFAULT_GATEWAY_CONFIG = {
    "model_type": "flux2_klein_9b",
    "resolution": "1024x1024",
    "steps": 4,
    "seed": -1,
    "guidance_scale": 3.5,
    "negative_prompt": "",
    "callback_url": "https://service.cookcalai.com/n8n/webhook/201054d7-d04d-46bd-9b29-9854803bff61",
}

def load_gateway_config():
    if os.path.isfile(GATEWAY_CONFIG_FILE):
        try:
            with open(GATEWAY_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            merged = {**DEFAULT_GATEWAY_CONFIG, **saved}
            return merged
        except Exception as e:
            print(f"Warning: failed to load gateway config: {e}")
    return dict(DEFAULT_GATEWAY_CONFIG)

def save_gateway_config(config):
    with open(GATEWAY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

def get_available_models():
    """Scan defaults/ and finetunes/ for model definitions"""
    models = []
    for folder in ["defaults", "finetunes"]:
        folder_path = os.path.join(BASE_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        for file_path in sorted(glob.glob(os.path.join(folder_path, "*.json"))):
            model_id = os.path.basename(file_path)[:-5]
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                model_def = data.get("model", {})
                name = model_def.get("name", model_id)
                if not model_def.get("visible", True):
                    continue
                architecture = model_def.get("architecture", "")
                description = model_def.get("description", "")
                default_steps = data.get("num_inference_steps", 20)
                default_resolution = data.get("resolution", "")
                default_guidance = data.get("guidance_scale", 3.5)
                models.append({
                    "id": model_id,
                    "name": name,
                    "architecture": architecture,
                    "description": description,
                    "default_steps": default_steps,
                    "default_resolution": default_resolution,
                    "default_guidance": default_guidance,
                    "folder": folder,
                })
            except Exception:
                continue
    return models

def resolve_model_id(requested_model: str | None) -> str:
    """Resolve a requested model ID or Name to a valid internal model_id, falling back to config default."""
    cfg = load_gateway_config()
    default_model = cfg.get("model_type", "flux2_klein_9b")
    if not requested_model or not str(requested_model).strip():
        return default_model

    req = str(requested_model).strip()
    available = get_available_models()
    
    # 1. Exact match on model ID
    for m in available:
        if m["id"] == req:
            return m["id"]
            
    # 2. Exact match on model Name
    for m in available:
        if m["name"] == req:
            return m["id"]

    # 3. Case-insensitive match on model ID or Name
    req_lower = req.lower()
    for m in available:
        if m["id"].lower() == req_lower or m["name"].lower() == req_lower:
            return m["id"]

    # 4. Partial match
    for m in available:
        if req_lower in m["id"].lower() or req_lower in m["name"].lower() or m["id"].lower() in req_lower:
            return m["id"]

    print(f"[Model Resolver] Unknown model '{requested_model}', falling back to default '{default_model}'")
    return default_model

RESOLUTION_PRESETS = [
    {"group": "1080p", "options": [
        {"label": "1920x1088 (16:9)", "value": "1920x1088"},
        {"label": "1088x1920 (9:16)", "value": "1088x1920"},
        {"label": "1920x832 (21:9)", "value": "1920x832"},
    ]},
    {"group": "720p", "options": [
        {"label": "1280x720 (16:9)", "value": "1280x720"},
        {"label": "720x1280 (9:16)", "value": "720x1280"},
        {"label": "1024x1024 (1:1)", "value": "1024x1024"},
        {"label": "1280x544 (21:9)", "value": "1280x544"},
        {"label": "1104x832 (4:3)", "value": "1104x832"},
        {"label": "832x1104 (3:4)", "value": "832x1104"},
        {"label": "960x960 (1:1)", "value": "960x960"},
    ]},
    {"group": "540p", "options": [
        {"label": "960x544 (16:9)", "value": "960x544"},
        {"label": "544x960 (9:16)", "value": "544x960"},
    ]},
    {"group": "480p", "options": [
        {"label": "832x480 (16:9)", "value": "832x480"},
        {"label": "480x832 (9:16)", "value": "480x832"},
        {"label": "832x624 (4:3)", "value": "832x624"},
        {"label": "720x720 (1:1)", "value": "720x720"},
    ]},
    {"group": "384p", "options": [
        {"label": "672x384 (16:9)", "value": "672x384"},
        {"label": "512x512 (1:1)", "value": "512x512"},
    ]},
]

# ═══════════════════════════ Session Lifecycle ═══════════════════════════

session = None

def _cleanup_gpu():
    global session
    if session is not None:
        try:
            print("Releasing GPU model memory...")
            session.close()
            session = None
        except Exception as e:
            print(f"Warning: error releasing model: {e}")
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass
    gc.collect()
    print("GPU memory released.")

@asynccontextmanager
async def lifespan(app):
    global session
    print("Initializing Wan2GP engine...")
    session = wangp_api.init(output_dir=OUTPUT_DIR)
    try:
        from discovery import start_discovery_listener
        start_discovery_listener()
    except Exception as e:
        print(f"Warning: failed to start discovery listener: {e}")
    print("Engine ready!")
    yield
    _cleanup_gpu()

# ═══════════════════════════ Signal Handlers ═══════════════════════════

def _signal_handler(signum, frame):
    sig_name = signal.Signals(signum).name
    print(f"\nReceived {sig_name}, shutting down gracefully...")
    sys.exit(0)

# ═══════════════════════════ FastAPI App ═══════════════════════════

app = FastAPI(title="API", docs_url=None, redoc_url=None, lifespan=lifespan)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "app_gateway"}

# ═══════════════════════════ Auth Middleware ═══════════════════════════

from starlette.middleware.base import BaseHTTPMiddleware

class SettingsAuthMiddleware(BaseHTTPMiddleware):
    """Protect /api/settings/* endpoints with token auth"""
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/settings/"):
            if not _verify_token_from_request(request):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized. Provide token via cookie, ?token= query, or Authorization: Bearer header."}
                )
        return await call_next(request)

app.add_middleware(SettingsAuthMiddleware)

# ═══════════════════════════ Request Model ═══════════════════════════

class GenerateRequest(BaseModel):
    prompt: str = Field(..., description="Prompt text")
    negative_prompt: str = Field(default=None, description="Negative prompt")
    model_type: str = Field(default=None, description="Model ID (uses config default if not set)")
    seed: int = Field(default=None, description="Random seed (-1 for random)")
    resolution: str = Field(default=None, description="Resolution e.g. '1024x1024' (uses config default if not set)")
    steps: int = Field(default=None, description="Inference steps")
    guidance_scale: float = Field(default=None, description="Guidance scale (CFG)")
    callback_url: str = Field(default=None, description="Webhook URL")
    other_data: str = Field(default="", description="Extra data for n8n")
    token: str = Field(default="", description="Auth token")

# ═══════════════════════════ Network API ═══════════════════════════

class IPUpdateRequest(BaseModel):
    ip: str

@app.get("/api/network/status")
async def get_network_status():
    import discovery
    avail = os.getenv("AVAILABLE_IPS", "").split(",")
    current = discovery.get_local_ip()
    if current not in avail and current:
        avail.append(current)
    avail = [ip.strip() for ip in avail if ip.strip()]
    return {
        "current_ip": current,
        "available_ips": list(set(avail)),
        "is_managed_by_hub": discovery.is_managed_by_hub
    }

@app.post("/api/network/ip")
async def set_network_ip(req: IPUpdateRequest):
    import discovery
    if discovery.is_managed_by_hub:
        return JSONResponse(status_code=403, content={"detail": "Cannot switch IP while managed by Service_LM Hub."})
    discovery.set_active_ip(req.ip)
    return {"status": "success", "message": f"Active IP switched to {req.ip}"}

# ═══════════════════════════ Settings UI ═══════════════════════════

SETTINGS_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wan2GP Gateway Settings</title>
<link rel="icon" href="/favicon.png" type="image/png">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

  :root {
    --bg: #0a0a0f;
    --bg-card: #12121a;
    --bg-input: #1a1a26;
    --bg-hover: #222233;
    --border: #2a2a3e;
    --border-focus: #6366f1;
    --text: #e2e8f0;
    --text-dim: #94a3b8;
    --text-muted: #64748b;
    --accent: #6366f1;
    --accent-glow: rgba(99, 102, 241, 0.3);
    --success: #22c55e;
    --success-bg: rgba(34, 197, 94, 0.1);
    --warning: #f59e0b;
    --danger: #ef4444;
    --radius: 12px;
    --radius-sm: 8px;
  }

  * { margin:0; padding:0; box-sizing:border-box; }

  body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.6;
  }

  .container {
    max-width: 720px;
    margin: 0 auto;
    padding: 32px 20px;
  }

  /* Header */
  .header {
    text-align: center;
    margin-bottom: 40px;
    position: relative;
  }
  .network-badge {
    position: absolute;
    top: 0;
    right: 0;
    background: rgba(99, 102, 241, 0.1);
    border: 1px solid rgba(99, 102, 241, 0.3);
    padding: 6px 12px;
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--accent);
  }
  .network-badge select {
    background: rgba(0, 0, 0, 0.2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 11px;
    outline: none;
    width: auto;
  }
  .network-badge select:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  
  .header h1 {
    font-size: 28px;
    font-weight: 700;
    background: linear-gradient(135deg, #6366f1, #a78bfa, #6366f1);
    background-size: 200% 200%;
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    animation: shimmer 3s ease infinite;
    margin-bottom: 6px;
  }
  @keyframes shimmer {
    0%, 100% { background-position: 0% 50%; }
    50% { background-position: 100% 50%; }
  }
  .header p { color: var(--text-dim); font-size: 14px; }

  /* Cards */
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin-bottom: 20px;
    transition: border-color 0.2s;
  }
  .card:hover { border-color: #3a3a52; }
  .card-header-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
  }
  .card-header-row .card-title {
    margin-bottom: 0;
  }
  .card-title {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: var(--text-muted);
    margin-bottom: 16px;
  }

  .btn-icon {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 5px 10px;
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--text-dim);
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    font-family: inherit;
  }
  .btn-icon:hover {
    border-color: var(--accent);
    color: var(--text);
    background: var(--bg-hover);
  }
  .btn-icon.spinning .spin-icon {
    display: inline-block;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  /* Form */
  .form-group { margin-bottom: 16px; }
  .form-group:last-child { margin-bottom: 0; }
  label {
    display: block;
    font-size: 13px;
    font-weight: 500;
    color: var(--text-dim);
    margin-bottom: 6px;
  }

  select, input[type="text"], input[type="number"] {
    width: 100%;
    padding: 10px 14px;
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: 14px;
    font-family: inherit;
    transition: all 0.2s;
    outline: none;
  }
  select:focus, input:focus {
    border-color: var(--border-focus);
    box-shadow: 0 0 0 3px var(--accent-glow);
  }
  select option { background: var(--bg-input); color: var(--text); }

  /* Model search */
  .model-search {
    position: relative;
    margin-bottom: 10px;
  }
  .model-search input {
    padding-left: 36px;
  }
  .model-search::before {
    content: "\\1F50D";
    position: absolute;
    left: 12px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 14px;
    pointer-events: none;
  }

  /* Model select with info */
  .model-info {
    font-size: 12px;
    color: var(--text-muted);
    margin-top: 6px;
    padding: 8px 12px;
    background: rgba(99, 102, 241, 0.05);
    border-radius: var(--radius-sm);
    border-left: 3px solid var(--accent);
    display: none;
    line-height: 1.5;
  }
  .model-info.visible { display: block; }

  /* Resolution grid */
  .resolution-groups { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
  .res-group-btn {
    padding: 6px 14px;
    border: 1px solid var(--border);
    border-radius: 20px;
    background: transparent;
    color: var(--text-dim);
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    font-family: inherit;
  }
  .res-group-btn:hover { border-color: var(--accent); color: var(--text); }
  .res-group-btn.active {
    background: var(--accent);
    border-color: var(--accent);
    color: white;
  }

  .resolution-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 8px;
  }
  .res-btn {
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg-input);
    color: var(--text-dim);
    font-size: 13px;
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
    font-family: inherit;
  }
  .res-btn:hover { border-color: var(--accent); color: var(--text); background: var(--bg-hover); }
  .res-btn.active {
    border-color: var(--accent);
    background: rgba(99, 102, 241, 0.12);
    color: white;
    box-shadow: 0 0 0 1px var(--accent);
  }

  /* Steps slider */
  .steps-row {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .steps-row input[type="range"] {
    flex: 1;
    -webkit-appearance: none;
    height: 6px;
    border-radius: 3px;
    background: var(--border);
    outline: none;
  }
  .steps-row input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: var(--accent);
    cursor: pointer;
    box-shadow: 0 0 8px var(--accent-glow);
  }
  .steps-value {
    min-width: 40px;
    text-align: center;
    font-weight: 600;
    font-size: 18px;
    color: var(--accent);
  }

  /* Two columns */
  .two-col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  /* Buttons */
  .btn-row {
    display: flex;
    gap: 12px;
    margin-top: 24px;
  }
  .btn {
    flex: 1;
    padding: 14px 20px;
    border: none;
    border-radius: var(--radius-sm);
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    font-family: inherit;
  }
  .btn-primary {
    background: linear-gradient(135deg, #6366f1, #4f46e5);
    color: white;
  }
  .btn-primary:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 16px var(--accent-glow);
  }
  .btn-secondary {
    background: var(--bg-input);
    color: var(--text-dim);
    border: 1px solid var(--border);
  }
  .btn-secondary:hover { border-color: var(--accent); color: var(--text); }

  /* Toast */
  .toast {
    position: fixed;
    top: 20px;
    right: 20px;
    padding: 14px 20px;
    border-radius: var(--radius-sm);
    font-size: 14px;
    font-weight: 500;
    z-index: 1000;
    transform: translateX(120%);
    transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    max-width: 360px;
  }
  .toast.show { transform: translateX(0); }
  .toast.success {
    background: var(--success-bg);
    border: 1px solid var(--success);
    color: var(--success);
  }
  .toast.error {
    background: rgba(239, 68, 68, 0.1);
    border: 1px solid var(--danger);
    color: var(--danger);
  }

  /* Config path badge */
  .config-path {
    text-align: center;
    margin-top: 16px;
    font-size: 11px;
    color: var(--text-muted);
    word-break: break-all;
  }

  /* Test Section */
  .test-prompt {
    width: 100%;
    min-height: 80px;
    padding: 12px 14px;
    background: var(--bg-input);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    color: var(--text);
    font-size: 14px;
    font-family: inherit;
    resize: vertical;
    outline: none;
    transition: all 0.2s;
    line-height: 1.5;
  }
  .test-prompt:focus {
    border-color: var(--border-focus);
    box-shadow: 0 0 0 3px var(--accent-glow);
  }
  .test-prompt::placeholder { color: var(--text-muted); }

  .btn-generate {
    width: 100%;
    padding: 14px;
    border: none;
    border-radius: var(--radius-sm);
    background: linear-gradient(135deg, #22c55e, #16a34a);
    color: white;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    font-family: inherit;
    transition: all 0.2s;
    margin-top: 12px;
  }
  .btn-generate:hover:not(:disabled) {
    transform: translateY(-1px);
    box-shadow: 0 4px 16px rgba(34, 197, 94, 0.3);
  }
  .btn-generate:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  .btn-generate.running {
    background: linear-gradient(135deg, #f59e0b, #d97706);
    animation: pulse-btn 1.5s ease-in-out infinite;
  }
  @keyframes pulse-btn {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.75; }
  }

  .test-result {
    margin-top: 16px;
    display: none;
  }
  .test-result.visible { display: block; }
  .test-result img {
    width: 100%;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
  }
  .test-result-info {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 8px;
    font-size: 12px;
    color: var(--text-muted);
  }
  .test-result-info a {
    color: var(--accent);
    text-decoration: none;
  }
  .test-result-info a:hover { text-decoration: underline; }

  .test-status {
    margin-top: 12px;
    padding: 10px 14px;
    border-radius: var(--radius-sm);
    font-size: 13px;
    display: none;
  }
  .test-status.visible { display: block; }
  .test-status.generating {
    background: rgba(245, 158, 11, 0.1);
    border: 1px solid var(--warning);
    color: var(--warning);
  }
  .test-status.error {
    background: rgba(239, 68, 68, 0.1);
    border: 1px solid var(--danger);
    color: var(--danger);
  }

  .timer { font-variant-numeric: tabular-nums; }

  /* Responsive */
  @media (max-width: 480px) {
    .two-col { grid-template-columns: 1fr; }
    .resolution-grid { grid-template-columns: repeat(2, 1fr); }
  }
</style>
</head>
<body>

<div class="container">
  <div class="header">
    <div class="network-badge" id="networkBadge">
      <span id="networkStatusIcon">💻</span>
      <select id="ipSelector" onchange="changeActiveIp(this.value)">
        <option value="">載入中...</option>
      </select>
    </div>
    <h1>Wan2GP Gateway</h1>
    <p>API Settings Dashboard</p>
  </div>

  <!-- Model Selection -->
  <div class="card">
    <div class="card-header-row">
      <div class="card-title">Model</div>
      <button type="button" class="btn-icon" id="btnRefreshModels" onclick="refreshModels()" title="重新掃描並更新模型清單">
        <span class="spin-icon">🔄</span> 更新模型清單
      </button>
    </div>
    <div class="form-group">
      <div class="model-search">
        <input type="text" id="modelSearch" placeholder="Search models..." autocomplete="off">
      </div>
      <select id="modelSelect" size="6" style="height: 180px;"></select>
      <div class="model-info" id="modelInfo"></div>
    </div>
  </div>

  <!-- Resolution -->
  <div class="card">
    <div class="card-title">Resolution</div>
    <div class="resolution-groups" id="resGroups"></div>
    <div class="resolution-grid" id="resGrid"></div>
  </div>

  <!-- Parameters -->
  <div class="card">
    <div class="card-title">Parameters</div>
    <div class="form-group">
      <label>Inference Steps</label>
      <div class="steps-row">
        <input type="range" id="stepsRange" min="1" max="100" value="4">
        <div class="steps-value" id="stepsValue">4</div>
      </div>
    </div>
    <div class="form-group">
      <label>Guidance Scale (CFG)</label>
      <div class="steps-row">
        <input type="range" id="guidanceRange" min="0" max="20" step="0.1" value="3.5">
        <div class="steps-value" id="guidanceValue">3.5</div>
      </div>
    </div>
    <div class="form-group">
      <label>Default Negative Prompt</label>
      <input type="text" id="negativePromptInput" placeholder="Optional negative prompt...">
    </div>
    <div class="two-col">
      <div class="form-group">
        <label>Seed (-1 = random)</label>
        <input type="number" id="seedInput" value="-1">
      </div>
      <div class="form-group">
        <label>Callback URL</label>
        <input type="text" id="callbackUrl" placeholder="https://...">
      </div>
    </div>
  </div>

  <!-- Actions -->
  <div class="btn-row">
    <button class="btn btn-secondary" onclick="resetDefaults()">Reset Defaults</button>
    <button class="btn btn-primary" onclick="saveConfig()">Save Settings</button>
  </div>

  <div class="config-path" id="configPath"></div>

  <!-- Test Generation -->
  <div class="card" style="margin-top: 32px; border-color: #1e3a2f;">
    <div class="card-title" style="color: #22c55e;">&#9889; Test Generation</div>
    <div class="form-group">
      <label>Prompt</label>
      <textarea class="test-prompt" id="testPrompt" placeholder="Enter a prompt to test generation...">a glass greenhouse filled with lush tropical plants, misty air, and dappled light</textarea>
    </div>
    <div class="form-group">
      <label>Negative Prompt</label>
      <input type="text" id="testNegativePrompt" placeholder="Optional negative prompt for test generation...">
    </div>
    <button class="btn-generate" id="btnGenerate" onclick="runTest()">&#128640; Generate</button>
    <div class="test-status" id="testStatus"></div>
    <div class="test-result" id="testResult">
      <img id="testImage" src="" alt="Generated image">
      <div class="test-result-info">
        <span id="testMeta"></span>
        <a id="testDownload" href="" download>Download JPG</a>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let allModels = [];
let currentConfig = {};
let currentResGroup = '720p';

async function init() {
  const [modelsRes, configRes] = await Promise.all([
    fetch('/api/settings/models'),
    fetch('/api/settings/config'),
  ]);
  allModels = await modelsRes.json();
  const data = await configRes.json();
  currentConfig = data.config;
  document.getElementById('configPath').textContent = data.config_path || '';

  renderModels(allModels);
  applyConfig(currentConfig);
  
  pollNetworkStatus();
  setInterval(pollNetworkStatus, 3000);
}

async function pollNetworkStatus() {
  try {
    const res = await fetch('/api/network/status');
    const data = await res.json();
    const sel = document.getElementById('ipSelector');
    
    // Remember current selection if any
    const currVal = sel.value;
    sel.innerHTML = '';
    
    data.available_ips.forEach(ip => {
      const opt = document.createElement('option');
      opt.value = ip;
      opt.textContent = ip;
      sel.appendChild(opt);
    });
    
    if (data.current_ip) {
      sel.value = data.current_ip;
    }
    
    const badge = document.getElementById('networkBadge');
    const icon = document.getElementById('networkStatusIcon');
    if (data.is_managed_by_hub) {
      sel.disabled = true;
      icon.textContent = '🔒';
      icon.title = '由 Service_LM 納管中，無法手動切換 IP';
      badge.style.borderColor = 'rgba(34, 197, 94, 0.4)';
      badge.style.background = 'rgba(34, 197, 94, 0.1)';
      badge.style.color = '#22c55e';
    } else {
      sel.disabled = false;
      icon.textContent = '💻';
      icon.title = '獨立運行模式';
      badge.style.borderColor = 'rgba(99, 102, 241, 0.3)';
      badge.style.background = 'rgba(99, 102, 241, 0.1)';
      badge.style.color = 'var(--accent)';
    }
  } catch (err) {
    console.error('Failed to poll network status', err);
  }
}

async function changeActiveIp(newIp) {
  if (!newIp) return;
  try {
    const res = await fetch('/api/network/ip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip: newIp })
    });
    if (!res.ok) throw new Error(await res.text());
    showToast('IP 切換成功', 'success');
  } catch (err) {
    showToast('切換失敗: ' + err.message, 'error');
  }
}

function renderModels(models) {
  const sel = document.getElementById('modelSelect');
  sel.innerHTML = '';
  models.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m.id;
    const tag = m.folder === 'finetunes' ? ' [finetune]' : '';
    opt.textContent = m.name + tag;
    opt.dataset.description = m.description || '';
    opt.dataset.steps = m.default_steps || 20;
    opt.dataset.resolution = m.default_resolution || '';
    opt.dataset.guidance = m.default_guidance ?? 3.5;
    sel.appendChild(opt);
  });
  sel.onchange = () => {
    const opt = sel.options[sel.selectedIndex];
    showModelInfo(opt);
    if (opt.dataset.steps) {
      document.getElementById('stepsRange').value = opt.dataset.steps;
      document.getElementById('stepsValue').textContent = opt.dataset.steps;
    }
    if (opt.dataset.guidance) {
      document.getElementById('guidanceRange').value = opt.dataset.guidance;
      document.getElementById('guidanceValue').textContent = opt.dataset.guidance;
    }
  };
}

function showModelInfo(opt) {
  const info = document.getElementById('modelInfo');
  if (opt && opt.dataset.description) {
    info.textContent = opt.dataset.description;
    info.classList.add('visible');
  } else {
    info.classList.remove('visible');
  }
}

async function refreshModels() {
  const btn = document.getElementById('btnRefreshModels');
  if (btn) btn.classList.add('spinning');

  try {
    const sel = document.getElementById('modelSelect');
    const selectedValue = sel ? sel.value : null;

    const modelsRes = await fetch('/api/settings/models');
    allModels = await modelsRes.json();
    renderModels(allModels);

    if (selectedValue && sel) {
      sel.value = selectedValue;
      if (sel.selectedIndex !== -1) {
        showModelInfo(sel.options[sel.selectedIndex]);
      }
    }

    const searchInput = document.getElementById('modelSearch');
    if (searchInput && searchInput.value) {
      const q = searchInput.value.toLowerCase();
      const filtered = allModels.filter(m =>
        m.name.toLowerCase().includes(q) || m.id.toLowerCase().includes(q)
      );
      renderModels(filtered);
      if (selectedValue && sel) sel.value = selectedValue;
    }

    showToast('模型清單已成功更新！', 'success');
  } catch (err) {
    showToast('更新模型清單失敗：' + err.message, 'error');
  } finally {
    if (btn) btn.classList.remove('spinning');
  }
}

function applyConfig(cfg) {
  // Model
  const sel = document.getElementById('modelSelect');
  sel.value = cfg.model_type || 'flux2_klein_9b';
  if (sel.selectedIndex >= 0) showModelInfo(sel.options[sel.selectedIndex]);

  // Steps
  const steps = cfg.steps || 4;
  document.getElementById('stepsRange').value = steps;
  document.getElementById('stepsValue').textContent = steps;

  // Guidance Scale
  const guidance = cfg.guidance_scale ?? 3.5;
  document.getElementById('guidanceRange').value = guidance;
  document.getElementById('guidanceValue').textContent = guidance;

  // Negative Prompt
  document.getElementById('negativePromptInput').value = cfg.negative_prompt || '';

  // Seed
  document.getElementById('seedInput').value = cfg.seed ?? -1;

  // Callback
  document.getElementById('callbackUrl').value = cfg.callback_url || '';

  // Resolution
  const res = cfg.resolution || '1024x1024';
  selectResGroup(guessResGroup(res));
  setTimeout(() => selectResolution(res), 50);
}

function guessResGroup(res) {
  const [w, h] = res.split('x').map(Number);
  const maxDim = Math.max(w, h);
  if (maxDim >= 1920) return '1080p';
  if (maxDim >= 960) return '720p';
  if (maxDim >= 800) return '540p';
  if (maxDim >= 672) return '480p';
  return '384p';
}

// Resolution groups
const RES_PRESETS = RESOLUTION_PRESETS_JSON;

function renderResGroups() {
  const container = document.getElementById('resGroups');
  container.innerHTML = '';
  RES_PRESETS.forEach(g => {
    const btn = document.createElement('button');
    btn.className = 'res-group-btn' + (g.group === currentResGroup ? ' active' : '');
    btn.textContent = g.group;
    btn.onclick = () => selectResGroup(g.group);
    container.appendChild(btn);
  });
}

function selectResGroup(group) {
  currentResGroup = group;
  renderResGroups();
  const preset = RES_PRESETS.find(g => g.group === group);
  const grid = document.getElementById('resGrid');
  grid.innerHTML = '';
  if (!preset) return;
  preset.options.forEach(opt => {
    const btn = document.createElement('button');
    btn.className = 'res-btn';
    btn.textContent = opt.label;
    btn.dataset.value = opt.value;
    btn.onclick = () => selectResolution(opt.value);
    grid.appendChild(btn);
  });
}

function selectResolution(val) {
  document.querySelectorAll('.res-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.value === val);
  });
  currentConfig.resolution = val;
}

// Steps slider
document.getElementById('stepsRange').oninput = function() {
  document.getElementById('stepsValue').textContent = this.value;
};

// Guidance slider
document.getElementById('guidanceRange').oninput = function() {
  document.getElementById('guidanceValue').textContent = this.value;
};

// Model search
document.getElementById('modelSearch').oninput = function() {
  const q = this.value.toLowerCase();
  const filtered = allModels.filter(m =>
    m.name.toLowerCase().includes(q) || m.id.toLowerCase().includes(q)
  );
  renderModels(filtered);
};

async function saveConfig() {
  const sel = document.getElementById('modelSelect');
  const res = currentConfig.resolution || '1024x1024';
  const config = {
    model_type: sel.value,
    resolution: res,
    steps: parseInt(document.getElementById('stepsRange').value),
    guidance_scale: parseFloat(document.getElementById('guidanceRange').value),
    negative_prompt: document.getElementById('negativePromptInput').value.trim(),
    seed: parseInt(document.getElementById('seedInput').value) || -1,
    callback_url: document.getElementById('callbackUrl').value.trim(),
  };
  try {
    const resp = await fetch('/api/settings/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(config),
    });
    if (resp.ok) {
      currentConfig = config;
      showToast('Settings saved!', 'success');
    } else {
      showToast('Save failed', 'error');
    }
  } catch(e) {
    showToast('Network error', 'error');
  }
}

function resetDefaults() {
  const defaults = {
    model_type: 'flux2_klein_9b',
    resolution: '1024x1024',
    steps: 4,
    guidance_scale: 3.5,
    negative_prompt: '',
    seed: -1,
    callback_url: 'https://service.cookcalai.com/n8n/webhook/201054d7-d04d-46bd-9b29-9854803bff61',
  };
  applyConfig(defaults);
  showToast('Reset to defaults (not saved yet)', 'success');
}

function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type + ' show';
  setTimeout(() => t.classList.remove('show'), 2500);
}

// ═══════════════════════ Test Generation ═══════════════════════

let testRunning = false;
let testTimer = null;
let testStartTime = 0;

function setTestStatus(msg, type) {
  const el = document.getElementById('testStatus');
  el.textContent = msg;
  el.className = 'test-status visible ' + type;
}
function clearTestStatus() {
  document.getElementById('testStatus').className = 'test-status';
}

function startTimer() {
  testStartTime = Date.now();
  testTimer = setInterval(() => {
    const elapsed = Math.floor((Date.now() - testStartTime) / 1000);
    const min = Math.floor(elapsed / 60);
    const sec = elapsed % 60;
    const timeStr = min > 0 ? `${min}m ${sec}s` : `${sec}s`;
    setTestStatus(`Generating... ${timeStr}`, 'generating');
  }, 1000);
}
function stopTimer() {
  if (testTimer) { clearInterval(testTimer); testTimer = null; }
}

async function runTest() {
  if (testRunning) return;
  const prompt = document.getElementById('testPrompt').value.trim();
  if (!prompt) { showToast('Please enter a prompt', 'error'); return; }
  const negative_prompt = document.getElementById('testNegativePrompt').value.trim();
  const model_type = document.getElementById('modelSelect').value;
  const steps = parseInt(document.getElementById('stepsRange').value);
  const guidance_scale = parseFloat(document.getElementById('guidanceRange').value);
  const seed = parseInt(document.getElementById('seedInput').value) || -1;

  testRunning = true;
  const btn = document.getElementById('btnGenerate');
  btn.disabled = true;
  btn.textContent = 'Generating...';
  btn.classList.add('running');
  document.getElementById('testResult').classList.remove('visible');
  setTestStatus('Starting generation...', 'generating');
  startTimer();

  try {
    const resp = await fetch('/api/test/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ prompt, negative_prompt, model_type, steps, guidance_scale, seed }),
    });
    stopTimer();
    const data = await resp.json();
    if (resp.ok && data.success) {
      const img = document.getElementById('testImage');
      // Add cache-buster to force reload
      img.src = data.image_url + '?t=' + Date.now();
      const dl = document.getElementById('testDownload');
      dl.href = data.image_url;
      if (data.filename) dl.download = data.filename;
      const elapsed = ((Date.now() - testStartTime) / 1000).toFixed(1);
      document.getElementById('testMeta').textContent =
        `${data.model_type} | ${data.resolution} | ${data.steps} steps | ${elapsed}s`;
      document.getElementById('testResult').classList.add('visible');
      clearTestStatus();
      showToast('Generation complete!', 'success');
    } else {
      setTestStatus('Error: ' + (data.detail || data.error || 'Unknown error'), 'error');
    }
  } catch(e) {
    stopTimer();
    setTestStatus('Network error: ' + e.message, 'error');
  } finally {
    testRunning = false;
    btn.disabled = false;
    btn.textContent = String.fromCodePoint(0x1F680) + ' Generate';
    btn.classList.remove('running');
  }
}

renderResGroups();
init();
</script>
</body>
</html>"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wan2GP Gateway - Login</title>
<link rel="icon" href="/favicon.png" type="image/png">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
  :root {
    --bg: #0a0a0f; --bg-card: #12121a; --bg-input: #1a1a26;
    --border: #2a2a3e; --border-focus: #6366f1; --text: #e2e8f0;
    --text-dim: #94a3b8; --accent: #6366f1; --accent-glow: rgba(99,102,241,0.3);
    --danger: #ef4444; --radius: 12px; --radius-sm: 8px;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text);
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
  }
  .login-card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 40px; width: 380px; max-width: 90vw; text-align: center;
  }
  .login-card h1 {
    font-size: 24px; font-weight: 700; margin-bottom: 6px;
    background: linear-gradient(135deg, #6366f1, #a78bfa);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .login-card p { color: var(--text-dim); font-size: 13px; margin-bottom: 24px; }
  .login-card input {
    width: 100%; padding: 12px 16px; background: var(--bg-input); border: 1px solid var(--border);
    border-radius: var(--radius-sm); color: var(--text); font-size: 14px; font-family: inherit;
    outline: none; transition: all 0.2s; margin-bottom: 16px;
  }
  .login-card input:focus { border-color: var(--border-focus); box-shadow: 0 0 0 3px var(--accent-glow); }
  .login-card button {
    width: 100%; padding: 12px; border: none; border-radius: var(--radius-sm);
    background: linear-gradient(135deg, #6366f1, #4f46e5); color: white;
    font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit;
    transition: all 0.2s;
  }
  .login-card button:hover { transform: translateY(-1px); box-shadow: 0 4px 16px var(--accent-glow); }
  .error { color: var(--danger); font-size: 13px; margin-bottom: 12px; }
  .lock-icon { font-size: 48px; margin-bottom: 16px; }
</style>
</head>
<body>
<div class="login-card">
  <div class="lock-icon">🔒</div>
  <h1>Wan2GP Gateway</h1>
  <p>Enter your token to access settings</p>
  ERROR_PLACEHOLDER
  <form id="loginForm" onsubmit="doLogin(event)">
    <input type="password" id="tokenInput" placeholder="Enter token..." autofocus>
    <button type="submit">Login</button>
  </form>
</div>
<script>
async function doLogin(e) {
  e.preventDefault();
  const token = document.getElementById('tokenInput').value;
  const resp = await fetch('/login', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token}),
  });
  if (resp.redirected) {
    window.location.href = resp.url;
  } else if (resp.ok) {
    window.location.href = '/';
  } else {
    document.getElementById('tokenInput').style.borderColor = '#ef4444';
    const errDiv = document.querySelector('.error');
    if (!errDiv) {
      const d = document.createElement('div');
      d.className = 'error';
      d.textContent = 'Invalid token';
      document.getElementById('loginForm').before(d);
    }
  }
}
</script>
</body>
</html>"""

def get_login_html(error=False):
    error_html = '<div class="error">Invalid token</div>' if error else ''
    return LOGIN_HTML.replace("ERROR_PLACEHOLDER", error_html)

def get_settings_html():
    """Inject resolution presets and logout button into HTML template"""
    html = SETTINGS_HTML.replace(
        "RESOLUTION_PRESETS_JSON",
        json.dumps(RESOLUTION_PRESETS, ensure_ascii=False)
    )
    # Add logout button to header
    html = html.replace(
        '<p>API Settings Dashboard</p>',
        '<p>API Settings Dashboard &nbsp;·&nbsp; <a href="/logout" style="color:#94a3b8;text-decoration:none;font-size:12px;">Logout 🔓</a></p>'
    )
    return html

# ═══════════════════════════ API Routes ═══════════════════════════

@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return "User-agent: *\nDisallow: /"

@app.get("/")
async def settings_page(request: Request):
    # Localhost always gets access without login
    if _is_localhost(request):
        return HTMLResponse(content=get_settings_html())
    session_id = request.cookies.get("gw_session")
    if _verify_session(session_id):
        return HTMLResponse(content=get_settings_html())
    return HTMLResponse(content=get_login_html())

@app.post("/login")
async def login(request: Request):
    try:
        body = await request.json()
        token = body.get("token", "")
    except Exception:
        token = ""
    if token == SECRET_TOKEN:
        session_id = _create_session()
        response = JSONResponse(content={"success": True})
        response.set_cookie("gw_session", session_id, httponly=True, samesite="strict", max_age=86400)
        return response
    return JSONResponse(status_code=401, content={"success": False, "detail": "Invalid token"})

@app.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("gw_session")
    if session_id:
        _active_sessions.discard(session_id)
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("gw_session")
    return response

@app.get("/favicon.png")
async def favicon():
    favicon_path = os.path.join(BASE_DIR, "favicon.png")
    if os.path.isfile(favicon_path):
        from fastapi.responses import FileResponse
        return FileResponse(favicon_path, media_type="image/png")
    return PlainTextResponse("", status_code=404)

@app.get("/api/settings/models")
async def get_models():
    return get_available_models()

@app.get("/api/settings/config")
async def get_config():
    return {"config": load_gateway_config(), "config_path": GATEWAY_CONFIG_FILE}

@app.post("/api/settings/config")
async def update_config(config: dict):
    save_gateway_config(config)
    return {"success": True}

# ═══════════════════════════ Test Generation (Sync) ═══════════════════════════

@app.post("/api/test/generate")
async def test_generate(request: Request, body: dict):
    if not _verify_token_from_request(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")
    
    negative_prompt = body.get("negative_prompt", "").strip()
    
    cfg = load_gateway_config()
    model_type = resolve_model_id(body.get("model_type"))
    resolution = body.get("resolution") or cfg.get("resolution", "1024x1024")
    steps = body.get("steps") if body.get("steps") is not None else cfg.get("steps", 4)
    seed = body.get("seed") if body.get("seed") is not None else cfg.get("seed", -1)
    guidance_scale = body.get("guidance_scale") if body.get("guidance_scale") is not None else cfg.get("guidance_scale", 3.5)
    
    if seed == -1:
        seed = int(time.time())
    
    task_settings = {
        "prompt": prompt,
        "negative_prompt": negative_prompt or cfg.get("negative_prompt", ""),
        "model_type": model_type,
        "seed": seed,
        "resolution": resolution,
        "num_inference_steps": steps,
        "guidance_scale": guidance_scale,
        "num_images": 1,
        "image_mode": 1,  # Output image instead of video
    }
    
    print(f"[Test] Generating: {prompt[:50]}...")
    
    try:
        import asyncio
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: session.run_task(task_settings)
        )
        
        if result.success and result.generated_files:
            file_path = result.generated_files[0]
            filename = os.path.basename(file_path)
            quoted_filename = quote(filename)
            print(f"[Test] Generated: {file_path}")
            return {
                "success": True,
                "image_url": f"/outputs/{quoted_filename}",
                "filename": filename,
                "file_path": file_path,
                "model_type": model_type,
                "resolution": resolution,
                "steps": steps,
            }
        else:
            errors = [str(e) for e in (result.errors or [])]
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "; ".join(errors) or "Generation failed"}
            )
    except Exception as e:
        print(f"[Test] Error: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )

# Serve output files
from fastapi.responses import FileResponse

@app.get("/outputs/{filename}")
async def serve_output(filename: str):
    raw_name = unquote(filename)
    safe_name = os.path.basename(raw_name)  # prevent path traversal
    file_path = os.path.join(OUTPUT_DIR, safe_name)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    encoded_filename = quote(safe_name)
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}"
    }
    return FileResponse(file_path, headers=headers)

# ═══════════════════════════ OpenAI Compatible API ═══════════════════════════

@app.get("/v1/models")
@app.get("/models")
async def openai_list_models():
    models = get_available_models()
    data = []
    for m in models:
        data.append({
            "id": m["id"],
            "name": m["name"],
            "object": "model",
            "created": 1700000000,
            "owned_by": "wan2gp",
        })
    return {"object": "list", "data": data}

class OpenAIImageGenerationRequest(BaseModel):
    prompt: str = Field(..., description="Prompt text for image generation")
    model: str | None = Field(default=None, description="Model ID")
    n: int | None = Field(default=1, description="Number of images to generate")
    size: str | None = Field(default=None, description="Size e.g. '1024x1024'")
    response_format: str | None = Field(default="url", description="Response format: 'url' or 'b64_json'")
    user: str | None = Field(default=None, description="Optional user identifier")

async def _handle_openai_images_generations(request: Request, body: OpenAIImageGenerationRequest):
    if not _verify_token_from_request(request):
        raise HTTPException(status_code=401, detail="Unauthorized - Invalid API Token")

    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    import discovery
    discovery.active_tasks += 1
    try:
        cfg = load_gateway_config()
        model_type = resolve_model_id(body.model)
        resolution = body.size or cfg.get("resolution", "1024x1024")
        steps = cfg.get("steps", 4)
        seed = cfg.get("seed", -1)
        if seed == -1:
            seed = int(time.time())
        guidance_scale = cfg.get("guidance_scale", 3.5)
    
        task_settings = {
            "prompt": prompt,
            "negative_prompt": cfg.get("negative_prompt", ""),
            "model_type": model_type,
            "seed": seed,
            "resolution": resolution,
            "num_inference_steps": steps,
            "guidance_scale": guidance_scale,
            "num_images": body.n or 1,
            "image_mode": 1,  # Output image mode
        }
    
        print(f"[OpenAI API] Generating image: {prompt[:60]}... ({resolution}, model={model_type})")
    
        try:
            import asyncio
            import base64
    
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: session.run_task(task_settings)
            )
    
            if result.success and result.generated_files:
                host_header = request.headers.get("host") or "localhost:58080"
                scheme = request.url.scheme or "http"
                
                data_items = []
                for file_path in result.generated_files:
                    filename = os.path.basename(file_path)
                    quoted_filename = quote(filename)
                    image_url = f"{scheme}://{host_header}/outputs/{quoted_filename}?token={SECRET_TOKEN}"
                    
                    if body.response_format == "url":
                        item = {"url": image_url}
                    else:
                        item = {}
                        try:
                            with open(file_path, "rb") as image_file:
                                item["b64_json"] = base64.b64encode(image_file.read()).decode("utf-8")
                        except Exception as b64_err:
                            print(f"Warning: b64encode failed: {b64_err}")
                            item["url"] = image_url
    
                    data_items.append(item)
    
                return {
                    "created": int(time.time()),
                    "data": data_items
                }
            else:
                errors = [str(e) for e in (result.errors or [])]
                raise HTTPException(
                    status_code=500,
                    detail=f"Image generation failed: {'; '.join(errors)}"
                )
        except HTTPException:
            raise
        except Exception as e:
            print(f"[OpenAI API] Error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    finally:
        discovery.active_tasks = max(0, discovery.active_tasks - 1)

@app.post("/v1/images/generations")
@app.post("/images/generations")
async def openai_images_generations(request: Request, body: OpenAIImageGenerationRequest):
    return await _handle_openai_images_generations(request, body)

# ═══════════════════════════ Generation (Async + Webhook) ═══════════════════════════

def process_and_webhook(req: GenerateRequest):
    import discovery
    discovery.active_tasks += 1
    try:
        cfg = load_gateway_config()
        
        model_type = resolve_model_id(req.model_type)
        resolution = cfg.get("resolution", "1024x1024")
    
        print(f"Processing task: {req.prompt[:50]}...")
        
        task_settings = {
            "prompt": req.prompt,
            "negative_prompt": req.negative_prompt if req.negative_prompt is not None else cfg.get("negative_prompt", ""),
            "model_type": model_type,
            "seed": (req.seed if req.seed is not None else cfg.get("seed", -1)),
            "resolution": resolution,
            "num_inference_steps": req.steps or cfg.get("steps", 4),
            "guidance_scale": req.guidance_scale if req.guidance_scale is not None else cfg.get("guidance_scale", 3.5),
            "num_images": 1,
            "image_mode": 1,  # Output image instead of video
        }
        
        if task_settings["seed"] == -1:
            task_settings["seed"] = int(time.time())
        
        try:
            result = session.run_task(task_settings)
            
            if result.success and result.generated_files:
                file_path = result.generated_files[0]
                print(f"Generated: {file_path}")
                
                callback = req.callback_url or cfg.get("callback_url", "")
                if callback:
                    file_name = os.path.basename(file_path)
                    mime_type, _ = mimetypes.guess_type(file_path)
                    mime_type = mime_type or 'application/octet-stream'
                    with open(file_path, 'rb') as f:
                        file_data = f.read()
                    
                    files = {'image': (file_name, file_data, mime_type)}
                    data = {'other_data': req.other_data}
                    
                    resp = requests.post(callback, files=files, data=data)
                    print(f"Webhook status: {resp.status_code}")
                    
            else:
                print(f"Generation failed: {result.errors}")
                
        except Exception as e:
            print(f"Error: {e}")
    finally:
        discovery.active_tasks = max(0, discovery.active_tasks - 1)

@app.post("/api/generate")
async def trigger_generation(req: GenerateRequest, background_tasks: BackgroundTasks):
    if req.token != SECRET_TOKEN:
        print("Unauthorized request blocked.")
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    background_tasks.add_task(process_and_webhook, req)
    return {"success": True, "message": "Task queued"}

# ═══════════════════════════ Entry Point ═══════════════════════════

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    
    import uvicorn
    port = int(os.environ.get("GATEWAY_PORT", 58080))
    uvicorn.run(app, host="0.0.0.0", port=port)

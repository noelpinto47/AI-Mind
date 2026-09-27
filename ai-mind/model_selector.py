"""
model_selector.py
-----------------
Runs on ai-mind startup. Finds the best free model per provider
using the same scoring logic as healthcheck.py, caches the result
to disk for 24 hours, and returns a dict of provider → model_id.

Usage (in ai_router.py):
    from model_selector import get_best_free_models
    best = get_best_free_models()   # {"groq": "...", "openrouter": "...", ...}
"""

import os
import json
import math
import time
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

CACHE_FILE   = os.path.join(os.path.dirname(__file__), ".model_cache.json")
CACHE_TTL    = 60 * 60 * 24          # 24 hours
ACCOUNT_ID   = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

SCAN_SKIP = [
    "whisper", "guard", "orpheus", "tts", "speech", "embed", "rerank",
    "fill-mask", "translation", "summarization", "moderation", "ocr",
    "imagen", "veo", "aqa", "retrieval", "text-embedding", "embedding",
    "realtime", "transcribe", "audio", "image-gen", "diffusion",
]

OR_ROUTER_SLUGS = {
    "openrouter/auto", "openrouter/auto-beta", "openrouter/free",
    "openrouter/fusion", "openrouter/bodybuilder", "openrouter/pareto-code",
    "typesafe/jev-router",
}

PROVIDERS_CONFIG = [
    {
        "name": "groq",
        "key":  os.getenv("GROQ_API_KEY"),
        "base": "https://api.groq.com/openai/v1",
    },
    {
        "name": "gemini",
        "key":  os.getenv("GEMINI_API_KEY"),
        "base": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    {
        "name": "cloudflare",
        "key":  os.getenv("CLOUDFLARE_API_KEY"),
        "base": f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1",
    },
    {
        "name": "mistral",
        "key":  os.getenv("MISTRAL_API_KEY"),
        "base": "https://api.mistral.ai/v1",
    },
    {
        "name": "huggingface",
        "key":  os.getenv("HUGGINGFACE_API_KEY"),
        "base": "https://router.huggingface.co/v1",
    },
    {
        "name": "openrouter",
        "key":  os.getenv("OPENROUTER_API_KEY"),
        "base": "https://openrouter.ai/api/v1",
    },
]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _load_cache() -> dict | None:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("timestamp", 0) < CACHE_TTL:
            return data.get("models")
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None


def _save_cache(models: dict):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time(), "models": models}, f, indent=2)
    except Exception as e:
        print(f"[ModelSelector] Cache write failed: {e}")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _is_chat_model(model_id: str) -> bool:
    lid = model_id.lower()
    return not any(s in lid for s in SCAN_SKIP)


def _is_free(m: dict) -> bool:
    """Return True only if the model is confirmed free (pricing_input == 0)."""
    pi = m.get("pricing_input")
    return pi is not None and pi == 0


def _has_any_capability(m: dict) -> bool:
    return bool(m.get("tool_support") or m.get("vision_support") or m.get("reasoning_support"))


def _is_active(raw: dict) -> bool:
    active = raw.get("active")
    return True if active is None else bool(active)


def _is_available_groq(raw: dict) -> bool:
    return _is_active(raw) and not raw.get("shutdown_date")


def _is_available_mistral(raw: dict) -> bool:
    if raw.get("deprecation"):
        return False
    caps = raw.get("capabilities") or {}
    return not (caps.get("audio_transcription_realtime") and not caps.get("completion_chat"))


def _is_available_huggingface(raw: dict) -> bool:
    return any(p.get("status") == "live" for p in (raw.get("providers") or []))


def _is_available_openrouter(raw: dict) -> bool:
    if raw.get("id", "") in OR_ROUTER_SLUGS:
        return False
    try:
        if float((raw.get("pricing") or {}).get("prompt", 0)) < 0:
            return False
    except (TypeError, ValueError):
        pass
    return not raw.get("expiration_date")


# ---------------------------------------------------------------------------
# Metadata extraction  (mirrors healthcheck.py exactly)
# ---------------------------------------------------------------------------

def _extract_meta(provider_name: str, raw: dict) -> dict:
    m = {
        "id": raw.get("id", ""),
        "context_length": None,
        "tool_support": False,
        "vision_support": False,
        "reasoning_support": False,
        "pricing_input": None,
        "hf_latency_ms": None,
    }

    if provider_name == "groq":
        m["context_length"]    = raw.get("context_window") or raw.get("context_length")
        feats                  = raw.get("supported_features") or []
        m["tool_support"]      = "tools" in feats
        m["reasoning_support"] = "reasoning" in feats
        m["vision_support"]    = "image" in (raw.get("input_modalities") or [])
        p = raw.get("pricing") or {}
        if p.get("prompt"): m["pricing_input"] = float(p["prompt"])

    elif provider_name == "gemini":
        m["context_length"] = raw.get("inputTokenLimit") or raw.get("context_length")
        m["tool_support"]   = "generateContent" in (raw.get("supportedGenerationMethods") or [])

    elif provider_name == "cloudflare":
        m["id"]          = raw.get("name", raw.get("id", ""))
        m["tool_support"] = True   # CF text-gen models support tools at runtime
        for prop in (raw.get("properties") or []):
            pid = prop.get("property_id")
            if pid == "context_window":
                try: m["context_length"] = int(prop["value"])
                except: pass
            elif pid == "price":
                for entry in (prop.get("value") or []):
                    if "input" in entry.get("unit", ""):
                        m["pricing_input"] = entry.get("price", 0) / 1_000_000

    elif provider_name == "mistral":
        m["context_length"]    = raw.get("max_context_length")
        caps                   = raw.get("capabilities") or {}
        m["tool_support"]      = caps.get("function_calling", False)
        m["vision_support"]    = caps.get("vision", False)
        m["reasoning_support"] = caps.get("reasoning", False)

    elif provider_name == "huggingface":
        m["vision_support"] = "image" in (raw.get("architecture") or {}).get("input_modalities", [])
        live = [p for p in (raw.get("providers") or []) if p.get("status") == "live"]
        if live:
            ctx = [p["context_length"] for p in live if p.get("context_length")]
            if ctx: m["context_length"] = max(ctx)
            m["tool_support"] = any(p.get("supports_tools") for p in live)
            prices = [p["pricing"]["input"] for p in live if (p.get("pricing") or {}).get("input")]
            if prices: m["pricing_input"] = min(prices) / 1_000_000
            lats = [p["first_token_latency_ms"] for p in live if p.get("first_token_latency_ms")]
            if lats: m["hf_latency_ms"] = min(lats)

    elif provider_name == "openrouter":
        m["context_length"]    = raw.get("context_length") or (raw.get("top_provider") or {}).get("context_length")
        m["vision_support"]    = "image" in (raw.get("architecture") or {}).get("input_modalities", [])
        m["tool_support"]      = "tools" in (raw.get("supported_parameters") or [])
        m["reasoning_support"] = bool(raw.get("reasoning"))
        p = raw.get("pricing") or {}
        try:
            pi = float(p.get("prompt", 0) or 0)
            if pi >= 0: m["pricing_input"] = pi
        except (TypeError, ValueError):
            pass

    return m


# ---------------------------------------------------------------------------
# Scoring  (same formula as healthcheck.py)
# ---------------------------------------------------------------------------

def _score(m: dict) -> float:
    score = 0.0
    ctx = m.get("context_length") or 0
    if ctx > 0:
        score += 25 * min(math.log10(ctx) / math.log10(1_000_000), 1.0)
    if m.get("tool_support"):      score += 25
    if m.get("vision_support"):    score += 10
    if m.get("reasoning_support"): score += 10
    pi = m.get("pricing_input")
    if pi is None:   score += 10
    elif pi == 0:    score += 20
    else:            score += 20 * max(0.0, 1.0 - (pi / 0.000010))
    lat = m.get("hf_latency_ms")
    if lat and lat > 0:
        score += 10 * max(0.0, 1.0 - (lat / 5000))
    return round(score, 1)


# ---------------------------------------------------------------------------
# Model listing per provider
# ---------------------------------------------------------------------------

def _list_gemini(key: str) -> list[dict]:
    url, all_raw, page_token = "https://generativelanguage.googleapis.com/v1beta/models", [], None
    while True:
        params = {"pageSize": 1000, "key": key}
        if page_token: params["pageToken"] = page_token
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"[ModelSelector] gemini list error: {e}")
            break
        data = resp.json()
        all_raw.extend(data.get("models", []))
        page_token = data.get("nextPageToken")
        if not page_token: break
    out = []
    for m in all_raw:
        mid = m.get("name", "")
        if "generateContent" not in (m.get("supportedGenerationMethods") or []): continue
        if not _is_chat_model(mid): continue
        out.append(_extract_meta("gemini", {**m, "id": mid}))
    return out


def _list_cloudflare(key: str) -> list[dict]:
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/models/search"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ModelSelector] cloudflare list error: {e}")
        return []
    out = []
    for m in resp.json().get("result", []):
        task = m.get("task") or {}
        task_name = task.get("name", "") if isinstance(task, dict) else str(task)
        if not ("text generation" in task_name.lower() or "chat" in task_name.lower()): continue
        model_name = m.get("name", "")
        if not model_name or not _is_chat_model(model_name): continue
        out.append(_extract_meta("cloudflare", m))
    return out


def _list_openai_compat(provider_name: str, key: str, base: str) -> list[dict]:
    avail_fn = {
        "groq":       _is_available_groq,
        "mistral":    _is_available_mistral,
        "openrouter": _is_available_openrouter,
    }.get(provider_name, _is_active)

    try:
        client = OpenAI(api_key=key, base_url=base)
        models = client.models.list().data
    except Exception as e:
        print(f"[ModelSelector] {provider_name} list error: {e}")
        return []

    out = []
    for m in models:
        if not _is_chat_model(m.id): continue
        raw = m.model_dump() if hasattr(m, "model_dump") else vars(m)
        if not avail_fn(raw): continue
        out.append(_extract_meta(provider_name, raw))
    return out


def _list_huggingface(key: str, base: str) -> list[dict]:
    try:
        client = OpenAI(api_key=key, base_url=base)
        models = client.models.list().data
    except Exception as e:
        print(f"[ModelSelector] huggingface list error: {e}")
        return []
    out = []
    for m in models:
        if not _is_chat_model(m.id): continue
        raw = m.model_dump() if hasattr(m, "model_dump") else vars(m)
        if not _is_available_huggingface(raw): continue
        out.append(_extract_meta("huggingface", raw))
    return out


def _list_models(p: dict) -> list[dict]:
    name, key, base = p["name"], p["key"], p["base"]
    if name == "gemini":      return _list_gemini(key)
    if name == "cloudflare":  return _list_cloudflare(key)
    if name == "huggingface": return _list_huggingface(key, base)
    return _list_openai_compat(name, key, base)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_best_free_models(force_refresh: bool = False) -> dict[str, str]:
    """
    Return {provider_name: best_free_model_id} for every configured provider.

    Result is cached to .model_cache.json for 24 hours.
    Pass force_refresh=True to bypass the cache (e.g. from a CLI flag).
    """

    if not force_refresh:
        cached = _load_cache()
        if cached:
            print("[ModelSelector] Using cached model selection.")
            return cached

    print("[ModelSelector] Scanning providers for best free models ...")
    result = {}

    for p in PROVIDERS_CONFIG:
        name = p["name"]
        key  = p["key"]

        if not key:
            print(f"[ModelSelector]   {name}: no API key, skipped")
            continue

        if name == "cloudflare" and not ACCOUNT_ID:
            print(f"[ModelSelector]   cloudflare: no account ID, skipped")
            continue

        models = _list_models(p)

        # Keep only free models with at least one capability
        free_capable = [
            m for m in models
            if _is_free(m) and _has_any_capability(m)
        ]

        if not free_capable:
            print(f"[ModelSelector]   {name}: no free capable models found")
            continue

        # Score and pick the best
        for m in free_capable:
            m["score"] = _score(m)

        best = max(free_capable, key=lambda m: m["score"])
        result[name] = best["id"]
        print(
            f"[ModelSelector]   {name}: {best['id']}  "
            f"(score={best['score']}, ctx={best.get('context_length')}, "
            f"tools={'✓' if best.get('tool_support') else '✗'}, "
            f"vision={'✓' if best.get('vision_support') else '✗'}, "
            f"reason={'✓' if best.get('reasoning_support') else '✗'})"
        )

    _save_cache(result)
    print(f"[ModelSelector] Done. {len(result)} provider(s) selected.")
    return result

import os, sys, time, math, requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

PROVIDERS = [
    {"name": "groq",        "key": os.getenv("GROQ_API_KEY"),        "base": "https://api.groq.com/openai/v1",                                              "model": os.getenv("GROQ_MODEL")        or "openai/gpt-oss-120b"},
    {"name": "gemini",      "key": os.getenv("GEMINI_API_KEY"),      "base": "https://generativelanguage.googleapis.com/v1beta/openai/",                    "model": os.getenv("GEMINI_MODEL")      or "models/gemini-flash-lite-latest"},
    {"name": "cloudflare",  "key": os.getenv("CLOUDFLARE_API_KEY"),  "base": f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1",           "model": os.getenv("CLOUDFLARE_MODEL")  or "@cf/meta/llama-3.1-8b-instruct-fp8"},
    {"name": "mistral",     "key": os.getenv("MISTRAL_API_KEY"),     "base": "https://api.mistral.ai/v1",                                                   "model": os.getenv("MISTRAL_MODEL")     or "mistral-code-latest"},
    {"name": "huggingface", "key": os.getenv("HUGGINGFACE_API_KEY"), "base": "https://router.huggingface.co/v1",                                            "model": os.getenv("HUGGINGFACE_MODEL") or "zai-org/GLM-5.3-Flash"},
    {"name": "openrouter",  "key": os.getenv("OPENROUTER_API_KEY"),  "base": "https://openrouter.ai/api/v1",                                                "model": os.getenv("OPENROUTER_MODEL")  or "qwen/qwen3.8-27b:free"},
]

PROVIDERS = [
    p for p in PROVIDERS
    if not (p["name"] == "cloudflare" and (not ACCOUNT_ID or ACCOUNT_ID == "your_account_id"))
]

TOOL_TEST = [{"type": "function", "function": {"name": "get_time", "description": "Get current time", "parameters": {"type": "object", "properties": {}}}}]
COL       = {"provider": 14, "model": 36, "status": 8, "latency": 10, "tools": 12}
LOG_FILE  = "healthcheck_log.txt"

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


# ---------------------------------------------------------------------------
# Availability filters
# ---------------------------------------------------------------------------

def _is_active(raw: dict) -> bool:
    active = raw.get("active")
    if active is None:
        return True
    return bool(active)


def _is_chat_model(model_id: str) -> bool:
    lid = model_id.lower()
    return not any(s in lid for s in SCAN_SKIP)


def _has_any_capability(m: dict) -> bool:
    """Return False if model has no tools, vision, or reasoning — nothing useful to offer."""
    return bool(m.get("tool_support") or m.get("vision_support") or m.get("reasoning_support"))


def _is_available_groq(raw: dict) -> bool:
    if not _is_active(raw):          return False
    if raw.get("shutdown_date"):     return False
    return True


def _is_available_mistral(raw: dict) -> bool:
    if raw.get("deprecation"):       return False
    caps = raw.get("capabilities") or {}
    if caps.get("audio_transcription_realtime") and not caps.get("completion_chat"):
        return False
    return True


def _is_available_huggingface(raw: dict) -> bool:
    providers = raw.get("providers") or []
    return any(p.get("status") == "live" for p in providers)


def _is_available_openrouter(raw: dict) -> bool:
    model_id = raw.get("id", "")
    if model_id in OR_ROUTER_SLUGS:  return False
    try:
        if float((raw.get("pricing") or {}).get("prompt", 0)) < 0:
            return False
    except (TypeError, ValueError):
        pass
    if raw.get("expiration_date"):   return False
    return True


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def _extract_meta(provider_name: str, raw: dict) -> dict:
    m = {
        "id": raw.get("id", ""),
        "name": "",
        "description": "",
        "context_length": None,
        "max_output_tokens": None,
        "owned_by": "",
        "tool_support": False,
        "vision_support": False,
        "reasoning_support": False,
        "pricing_input": None,
        "pricing_output": None,
        "aliases": [],
        "knowledge_cutoff": None,
        "hf_latency_ms": None,
        "hf_throughput": None,
        "raw": raw,
    }

    if provider_name == "groq":
        m["name"]              = raw.get("name", "")
        m["owned_by"]          = raw.get("owned_by", "")
        m["context_length"]    = raw.get("context_window") or raw.get("context_length")
        m["max_output_tokens"] = raw.get("max_completion_tokens") or raw.get("max_output_length")
        feats                  = raw.get("supported_features") or []
        m["tool_support"]      = "tools" in feats
        m["reasoning_support"] = "reasoning" in feats
        m["vision_support"]    = "image" in (raw.get("input_modalities") or [])
        p = raw.get("pricing") or {}
        if p.get("prompt"):     m["pricing_input"]  = float(p["prompt"])
        if p.get("completion"): m["pricing_output"] = float(p["completion"])

    elif provider_name == "gemini":
        m["name"]              = raw.get("displayName", raw.get("display_name", raw.get("id", "")))
        m["owned_by"]          = "Google"
        m["description"]       = raw.get("description", "")
        m["context_length"]    = raw.get("inputTokenLimit") or raw.get("context_length")
        m["max_output_tokens"] = raw.get("outputTokenLimit") or raw.get("max_output_tokens")
        methods                = raw.get("supportedGenerationMethods") or []
        m["tool_support"]      = "generateContent" in methods

    elif provider_name == "cloudflare":
        m["id"]          = raw.get("name", raw.get("id", ""))
        m["name"]        = raw.get("name", "")
        m["description"] = raw.get("description", "")
        m["owned_by"]    = "Cloudflare"
        for prop in (raw.get("properties") or []):
            pid = prop.get("property_id")
            if pid == "context_window":
                try: m["context_length"] = int(prop["value"])
                except: pass
            elif pid == "price":
                for entry in (prop.get("value") or []):
                    unit  = entry.get("unit", "")
                    price = entry.get("price", 0)
                    if "input" in unit:  m["pricing_input"]  = price / 1_000_000
                    if "output" in unit: m["pricing_output"] = price / 1_000_000

    elif provider_name == "mistral":
        m["name"]              = raw.get("name", "")
        m["description"]       = raw.get("description", "")
        m["owned_by"]          = raw.get("owned_by", "mistralai")
        m["context_length"]    = raw.get("max_context_length")
        caps                   = raw.get("capabilities") or {}
        m["tool_support"]      = caps.get("function_calling", False)
        m["vision_support"]    = caps.get("vision", False)
        m["reasoning_support"] = caps.get("reasoning", False)
        m["aliases"]           = raw.get("aliases") or []

    elif provider_name == "huggingface":
        m["name"]         = raw.get("id", "")
        m["owned_by"]     = raw.get("owned_by", "")
        arch              = raw.get("architecture") or {}
        m["vision_support"] = "image" in arch.get("input_modalities", [])
        live = [p for p in (raw.get("providers") or []) if p.get("status") == "live"]
        if live:
            ctx_vals = [p["context_length"] for p in live if p.get("context_length")]
            if ctx_vals: m["context_length"] = max(ctx_vals)
            m["tool_support"] = any(p.get("supports_tools") for p in live)
            prices_in  = [p["pricing"]["input"]  for p in live if (p.get("pricing") or {}).get("input")]
            prices_out = [p["pricing"]["output"] for p in live if (p.get("pricing") or {}).get("output")]
            if prices_in:  m["pricing_input"]  = min(prices_in)  / 1_000_000
            if prices_out: m["pricing_output"] = min(prices_out) / 1_000_000
            lats = [p["first_token_latency_ms"] for p in live if p.get("first_token_latency_ms")]
            thru = [p["throughput"]             for p in live if p.get("throughput")]
            if lats: m["hf_latency_ms"] = min(lats)
            if thru: m["hf_throughput"] = max(thru)

    elif provider_name == "openrouter":
        m["name"]              = raw.get("name", "")
        m["description"]       = raw.get("description", "")
        m["context_length"]    = raw.get("context_length") or (raw.get("top_provider") or {}).get("context_length")
        m["max_output_tokens"] = (raw.get("top_provider") or {}).get("max_completion_tokens")
        arch                   = raw.get("architecture") or {}
        m["vision_support"]    = "image" in arch.get("input_modalities", [])
        m["tool_support"]      = "tools" in (raw.get("supported_parameters") or [])
        m["reasoning_support"] = bool(raw.get("reasoning"))
        m["knowledge_cutoff"]  = raw.get("knowledge_cutoff")
        p = raw.get("pricing") or {}
        try:
            pi = float(p.get("prompt", 0) or 0)
            po = float(p.get("completion", 0) or 0)
            if pi >= 0: m["pricing_input"]  = pi
            if po >= 0: m["pricing_output"] = po
        except (TypeError, ValueError):
            pass

    return m


# ---------------------------------------------------------------------------
# Model listing
# ---------------------------------------------------------------------------

def _list_models_gemini(key: str) -> list[dict]:
    url, all_raw, page_token = "https://generativelanguage.googleapis.com/v1beta/models", [], None
    while True:
        params = {"pageSize": 1000, "key": key}
        if page_token: params["pageToken"] = page_token
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        all_raw.extend(data.get("models", []))
        page_token = data.get("nextPageToken")
        if not page_token: break
    out = []
    for m in all_raw:
        model_id = m.get("name", "")
        if "generateContent" not in (m.get("supportedGenerationMethods") or []): continue
        if not _is_chat_model(model_id): continue
        out.append(_extract_meta("gemini", {**m, "id": model_id}))
    return out


def _list_models_cloudflare(key: str) -> list[dict]:
    url  = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/models/search"
    resp = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=15)
    resp.raise_for_status()
    out = []
    for m in resp.json().get("result", []):
        task      = m.get("task") or {}
        task_name = task.get("name", "") if isinstance(task, dict) else str(task)
        if not ("text generation" in task_name.lower() or "chat" in task_name.lower()): continue
        model_name = m.get("name", "")
        if not model_name or not _is_chat_model(model_name): continue
        out.append(_extract_meta("cloudflare", m))
    return out


def _list_models_openai(provider_name: str, client: OpenAI) -> list[dict]:
    avail_fn = {
        "groq":       _is_available_groq,
        "mistral":    _is_available_mistral,
        "openrouter": _is_available_openrouter,
    }.get(provider_name, _is_active)
    out = []
    for m in client.models.list().data:
        if not _is_chat_model(m.id): continue
        raw = m.model_dump() if hasattr(m, "model_dump") else vars(m)
        if not avail_fn(raw): continue
        out.append(_extract_meta(provider_name, raw))
    return out


def _list_models_huggingface(client: OpenAI) -> list[dict]:
    out = []
    for m in client.models.list().data:
        if not _is_chat_model(m.id): continue
        raw = m.model_dump() if hasattr(m, "model_dump") else vars(m)
        if not _is_available_huggingface(raw): continue
        out.append(_extract_meta("huggingface", raw))
    return out


def _list_models(provider_name: str, key: str, client: OpenAI) -> list[dict]:
    """Fetch, filter, and return only models with at least one capability."""
    if provider_name == "cloudflare":    models = _list_models_cloudflare(key)
    elif provider_name == "gemini":      models = _list_models_gemini(key)
    elif provider_name == "huggingface": models = _list_models_huggingface(client)
    else:                                models = _list_models_openai(provider_name, client)

    before = len(models)
    # Remove models with no capabilities at all (✗ tools  ✗ vision  ✗ reasoning)
    models = [m for m in models if _has_any_capability(m)]
    dropped = before - len(models)
    if dropped:
        print(f"  [{provider_name}] dropped {dropped} model(s) with no capabilities")

    return models


# ---------------------------------------------------------------------------
# Scoring
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
    lat = m.get("hf_latency_ms") or m.get("latency_ms")
    if lat and lat > 0:
        score += 10 * max(0.0, 1.0 - (lat / 5000))
    return round(score, 1)


# ---------------------------------------------------------------------------
# Live testing
# ---------------------------------------------------------------------------

def _test_chat(client: OpenAI, model_id: str) -> tuple[str, int, str, dict]:
    start, rate_limits = time.perf_counter(), {}
    try:
        resp    = client.chat.completions.with_raw_response.create(
            model=model_id,
            messages=[{"role": "user", "content": "Say: ok"}],
            max_tokens=5,
        )
        latency = round((time.perf_counter() - start) * 1000)
        for header, key in [
            ("x-ratelimit-limit-requests",         "rpm_limit"),
            ("x-ratelimit-limit-tokens",            "tpm_limit"),
            ("x-ratelimit-remaining-requests",      "rpm_remaining"),
            ("x-ratelimit-remaining-tokens",        "tpm_remaining"),
            ("x-ratelimit-reset-requests",          "rpm_reset"),
            ("x-ratelimit-reset-tokens",            "tpm_reset"),
            ("x-ratelimit-limit-req-minute",        "rpm_limit"),
            ("x-ratelimit-limit-tokens-minute",     "tpm_limit"),
            ("x-ratelimit-remaining-req-minute",    "rpm_remaining"),
            ("x-ratelimit-remaining-tokens-minute", "tpm_remaining"),
        ]:
            val = resp.headers.get(header)
            if val and key not in rate_limits:
                rate_limits[key] = val
        parsed  = resp.parse()
        content = parsed.choices[0].message.content
        return ("✓ ok" if content else "✗ empty", latency, repr(content), rate_limits)
    except Exception as e:
        latency = round((time.perf_counter() - start) * 1000)
        return ("✗ error", latency, str(e), rate_limits)


def _test_tools(client: OpenAI, model_id: str) -> str:
    try:
        tresp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "What time is it?"}],
            tools=TOOL_TEST, tool_choice="auto", max_tokens=20,
        )
        return "✓ supported" if tresp.choices[0].message.tool_calls else "✗ no call"
    except Exception as e:
        return f"✗ {str(e)[:20]}"


# ---------------------------------------------------------------------------
# --rank
# ---------------------------------------------------------------------------

def rank_provider(provider_name: str):
    p = next((p for p in PROVIDERS if p["name"] == provider_name), None)
    if not p:
        print(f"Unknown provider: {provider_name}")
        print(f"Available: {', '.join(x['name'] for x in PROVIDERS)}")
        return
    if not p["key"]:
        print(f"No API key for {provider_name}")
        return

    print(f"\nFetching models for {provider_name} ...")
    client = OpenAI(api_key=p["key"], base_url=p["base"])

    try:
        models = _list_models(provider_name, p["key"], client)
    except Exception as e:
        print(f"  ERROR listing models: {e}")
        return

    for m in models:
        m["score"] = _score(m)
    models.sort(key=lambda m: m["score"], reverse=True)

    W = {"rank": 4, "model": 46, "ctx": 10, "tools": 6, "vision": 6, "reason": 7, "price_in": 12, "score": 7}
    header = (
        f"{'#':<{W['rank']}} {'Model':<{W['model']}} {'Context':>{W['ctx']}} "
        f"{'Tools':<{W['tools']}} {'Vision':<{W['vision']}} {'Reason':<{W['reason']}} "
        f"{'$/M in':>{W['price_in']}} {'Score':>{W['score']}}"
    )
    print(f"\n  {provider_name.upper()} — {len(models)} capable text models ranked\n")
    print(f"  {header}")
    print("  " + "─" * (sum(W.values()) + len(W)))

    for i, m in enumerate(models, 1):
        model_id = m["id"]
        display  = model_id if len(model_id) <= W["model"] else model_id[:W["model"]-1] + "…"
        ctx      = f"{m['context_length']//1000}K" if m.get("context_length") else "—"
        tools    = "✓" if m.get("tool_support")      else "✗"
        vision   = "✓" if m.get("vision_support")    else "✗"
        reason   = "✓" if m.get("reasoning_support") else "✗"
        pi       = m.get("pricing_input")
        price    = "free" if pi == 0 else (f"${pi*1_000_000:.3f}" if pi is not None else "—")
        print(
            f"  {i:<{W['rank']}} {display:<{W['model']}} {ctx:>{W['ctx']}} "
            f"{tools:<{W['tools']}} {vision:<{W['vision']}} {reason:<{W['reason']}} "
            f"{price:>{W['price_in']}} {m['score']:>{W['score']}}"
        )
    print()


def rank_all():
    for p in PROVIDERS:
        if not p["key"]:
            print(f"\n  [{p['name']}] skipped — no API key")
            continue
        rank_provider(p["name"])


# ---------------------------------------------------------------------------
# --scan / --scan-all
# ---------------------------------------------------------------------------

def scan_provider(provider_name: str, silent: bool = False) -> dict:
    p = next((p for p in PROVIDERS if p["name"] == provider_name), None)
    if not p:
        return {"provider": provider_name, "base": "", "models": [], "error": "Unknown provider"}
    if not p["key"]:
        return {"provider": provider_name, "base": p["base"], "models": [], "error": "No API key"}

    if not silent: print(f"\nScanning {provider_name} ({p['base']}) ...\n")
    client = OpenAI(api_key=p["key"], base_url=p["base"])

    try:
        models = _list_models(provider_name, p["key"], client)
    except Exception as e:
        return {"provider": provider_name, "base": p["base"], "models": [], "error": str(e)}

    if not silent:
        print(f"Found {len(models)} capable chat models to test:\n")
        print(f"  {'Model':<48} {'Chat':<10} {'Latency':>8}  Tools")
        print("  " + "─" * 78)

    for m in models:
        model_id = m["id"]
        status, latency, err, rate_limits = _test_chat(client, model_id)
        m["chat_status"] = status
        m["latency_ms"]  = latency
        m["rate_limits"] = rate_limits
        m["score"]       = _score(m)

        if "error" in status:
            m["tool_status"] = "—"
            if not silent:
                display = model_id[:47] + "…" if len(model_id) > 48 else model_id
                print(f"  {display:<48} {'✗ error':<10} {f'{latency}ms':>8}  — {err[:35]}")
        else:
            tool_status = _test_tools(client, model_id)
            m["tool_status"] = tool_status
            if not silent:
                display = model_id[:47] + "…" if len(model_id) > 48 else model_id
                print(f"  {display:<48} {status:<10} {f'{latency}ms':>8}  {tool_status}")

    if not silent: print()
    return {"provider": provider_name, "base": p["base"], "models": models}


def scan_all():
    print(f"\n{'='*60}\n  Full scan of all {len(PROVIDERS)} providers\n{'='*60}")
    all_results = []
    for p in PROVIDERS:
        name = p["name"]
        if not p["key"]:
            print(f"\n  [{name}] skipped — no API key")
            all_results.append({"provider": name, "base": p["base"], "models": [], "error": "No API key configured"})
            continue
        print(f"\n  [{name}] scanning...")
        all_results.append(scan_provider(name, silent=False))

    _write_log(all_results, LOG_FILE)

    print(f"\n{'─'*60}\n  SUMMARY\n{'─'*60}")
    for r in all_results:
        pname  = r["provider"]
        models = r["models"]
        error  = r.get("error")
        if error:
            print(f"  {pname:<14}  ERROR: {error}")
        else:
            ok    = sum(1 for m in models if "✓" in m.get("chat_status", ""))
            tools = sum(1 for m in models if "✓ supported" in m.get("tool_status", ""))
            top   = sorted(models, key=lambda m: m.get("score", 0), reverse=True)
            best  = top[0]["id"] if top else "—"
            print(f"  {pname:<14}  {len(models)} models  |  {ok} chat ok  |  {tools} tool-capable  |  best: {best}")
    print(f"\n  Full log: {LOG_FILE}\n")


# ---------------------------------------------------------------------------
# Default healthcheck
# ---------------------------------------------------------------------------

def run_healthcheck():
    header = (
        f"{'Provider':<{COL['provider']}} {'Model':<{COL['model']}} "
        f"{'Status':<{COL['status']}} {'Latency':>{COL['latency']}}  {'Tool calls':<{COL['tools']}}"
    )
    print(f"\n{header}")
    print("─" * (sum(COL.values()) + 6))

    for p in PROVIDERS:
        name  = p["name"]
        model = p["model"]
        model_display = model if len(model) <= COL["model"] else model[:COL["model"]-1] + "…"

        if not p["key"]:
            print(f"{name:<{COL['provider']}} {model_display:<{COL['model']}} {'skip':<{COL['status']}} {'—':>{COL['latency']}}  {'—':<{COL['tools']}}  No API key")
            continue

        client = OpenAI(api_key=p["key"], base_url=p["base"])
        status, latency, err, _ = _test_chat(client, model)

        if status == "✗ error":
            print(f"{name:<{COL['provider']}} {model_display:<{COL['model']}} {'✗ error':<{COL['status']}} {f'{latency}ms':>{COL['latency']}}  {'—':<{COL['tools']}}  {err[:55]}")
            continue

        tool_status = _test_tools(client, model)
        print(f"{name:<{COL['provider']}} {model_display:<{COL['model']}} {status:<{COL['status']}} {f'{latency}ms':>{COL['latency']}}  {tool_status:<{COL['tools']}}")
    print()


# ---------------------------------------------------------------------------
# --try
# ---------------------------------------------------------------------------

def try_model(provider_name: str, model_id: str):
    p = next((p for p in PROVIDERS if p["name"] == provider_name), None)
    if not p:
        print(f"Unknown provider: {provider_name}")
        return
    if not p["key"]:
        print(f"No API key for {provider_name}")
        return
    print(f"\nTesting {provider_name} / {model_id} ...")
    client = OpenAI(api_key=p["key"], base_url=p["base"])
    status, latency, content, rl = _test_chat(client, model_id)
    print(f"  Chat:  {status}  ({latency}ms)  → {content}")
    if rl.get("rpm_limit"): print(f"  Requests remaining : {rl.get('rpm_remaining')} / {rl['rpm_limit']}")
    if rl.get("tpm_limit"): print(f"  Tokens remaining   : {rl.get('tpm_remaining')} / {rl['tpm_limit']}")
    if "error" not in status:
        print(f"  Tools: {_test_tools(client, model_id)}")


# ---------------------------------------------------------------------------
# Log writer
# ---------------------------------------------------------------------------

def _write_log(results: list[dict], path: str):
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = ["=" * 80, "  AI PROVIDER HEALTHCHECK SCAN", f"  Generated: {now}", "=" * 80]

    for r in results:
        pname  = r["provider"]
        base   = r["base"]
        models = r["models"]
        error  = r.get("error")

        lines += ["", "─" * 80, f"  PROVIDER : {pname.upper()}", f"  BASE URL : {base}", "─" * 80]

        if error:
            lines += [f"  ERROR: {error}", ""]
            continue

        ranked = sorted(models, key=lambda m: m.get("score", 0), reverse=True)
        lines += [f"  Total capable chat models: {len(ranked)}", ""]

        for rank, m in enumerate(ranked, 1):
            lines.append(f"  ┌─ #{rank}  {m['id']}")
            if m.get("name") and m["name"] != m["id"]:
                lines.append(f"  │  Name             : {m['name']}")
            if m.get("owned_by"):
                lines.append(f"  │  Owned by         : {m['owned_by']}")
            if m.get("description"):
                desc = m["description"][:120] + ("…" if len(m["description"]) > 120 else "")
                lines.append(f"  │  Description      : {desc}")
            if m.get("context_length"):
                lines.append(f"  │  Context length   : {m['context_length']:,} tokens")
            if m.get("max_output_tokens"):
                lines.append(f"  │  Max output       : {m['max_output_tokens']:,} tokens")
            lines.append(f"  │  Tool support     : {'✓' if m.get('tool_support') else '✗'}")
            lines.append(f"  │  Vision support   : {'✓' if m.get('vision_support') else '✗'}")
            lines.append(f"  │  Reasoning        : {'✓' if m.get('reasoning_support') else '✗'}")
            if m.get("aliases"):
                lines.append(f"  │  Aliases          : {', '.join(m['aliases'])}")
            if m.get("knowledge_cutoff"):
                lines.append(f"  │  Knowledge cutoff : {m['knowledge_cutoff']}")
            pi, po = m.get("pricing_input"), m.get("pricing_output")
            if pi is not None:
                p_str = "free" if pi == 0 else f"${pi*1e6:.4f}/M"
                o_str = "free" if po == 0 else (f"${po*1e6:.4f}/M" if po else "—")
                lines.append(f"  │  Pricing          : input={p_str}  output={o_str}")
            if m.get("hf_latency_ms"):
                lines.append(f"  │  HF best latency  : {m['hf_latency_ms']:.0f}ms first-token")
            if m.get("hf_throughput"):
                lines.append(f"  │  HF throughput    : {m['hf_throughput']:.1f} tok/s")
            lines.append(f"  │  Score            : {m.get('score', 0)}")
            lines.append(f"  │  Chat test        : {m.get('chat_status', '—')}  ({m.get('latency_ms', '—')}ms)")
            lines.append(f"  │  Tool calling     : {m.get('tool_status', '—')}")
            rl = m.get("rate_limits") or {}
            if rl:
                if rl.get("rpm_limit"):
                    lines.append(f"  │  Req rate limit   : {rl.get('rpm_remaining')} / {rl['rpm_limit']} remaining  (resets {rl.get('rpm_reset', '?')})")
                if rl.get("tpm_limit"):
                    lines.append(f"  │  Tok rate limit   : {rl.get('tpm_remaining')} / {rl['tpm_limit']} remaining  (resets {rl.get('tpm_reset', '?')})")
            lines += [f"  └{'─' * 60}", ""]

    lines += ["=" * 80, "  END OF REPORT", "=" * 80]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n  Log written to: {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--try" in sys.argv:
        idx = sys.argv.index("--try")
        try: try_model(sys.argv[idx + 1], sys.argv[idx + 2])
        except IndexError: print("Usage: python healthcheck.py --try <provider> <model_id>")

    elif "--rank-all" in sys.argv:
        rank_all()

    elif "--rank" in sys.argv:
        idx = sys.argv.index("--rank")
        try: rank_provider(sys.argv[idx + 1])
        except IndexError:
            print("Usage: python healthcheck.py --rank <provider>")
            print(f"Available: {', '.join(p['name'] for p in PROVIDERS)}")

    elif "--scan-all" in sys.argv:
        scan_all()

    elif "--scan" in sys.argv:
        idx = sys.argv.index("--scan")
        try: scan_provider(sys.argv[idx + 1])
        except IndexError:
            print("Usage: python healthcheck.py --scan <provider>")
            print(f"Available: {', '.join(p['name'] for p in PROVIDERS)}")

    else:
        run_healthcheck()

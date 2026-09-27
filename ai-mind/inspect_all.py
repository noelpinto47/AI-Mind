# inspect_all.py  —  run once, dumps everything to inspect_dump.txt
import os, json, requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

PROVIDERS = [
    {"name": "groq",        "key": os.getenv("GROQ_API_KEY"),        "base": "https://api.groq.com/openai/v1",                                                           "model": os.getenv("GROQ_MODEL")        or "openai/gpt-oss-120b"},
    {"name": "gemini",      "key": os.getenv("GEMINI_API_KEY"),      "base": "https://generativelanguage.googleapis.com/v1beta/openai/",                                 "model": os.getenv("GEMINI_MODEL")      or "models/gemini-flash-lite-latest"},
    {"name": "cloudflare",  "key": os.getenv("CLOUDFLARE_API_KEY"),  "base": f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1",                        "model": os.getenv("CLOUDFLARE_MODEL")  or "@cf/meta/llama-3.1-8b-instruct-fp8"},
    {"name": "mistral",     "key": os.getenv("MISTRAL_API_KEY"),     "base": "https://api.mistral.ai/v1",                                                                "model": os.getenv("MISTRAL_MODEL")     or "mistral-code-latest"},
    {"name": "huggingface", "key": os.getenv("HUGGINGFACE_API_KEY"), "base": "https://router.huggingface.co/v1",                                                         "model": os.getenv("HUGGINGFACE_MODEL") or "zai-org/GLM-5.3-Flash"},
    {"name": "openrouter",  "key": os.getenv("OPENROUTER_API_KEY"),  "base": "https://openrouter.ai/api/v1",                                                             "model": os.getenv("OPENROUTER_MODEL")  or "stealth/space-bunny-alpha"},
]

OUT = "inspect_dump.txt"
SEP = "=" * 80
SEP2 = "─" * 60

def dump(lines, text):
    lines.append(text)
    print(text)

def inspect(p, lines):
    name  = p["name"]
    model = p["model"]
    key   = p["key"]
    base  = p["base"]

    dump(lines, f"\n{SEP}")
    dump(lines, f"  PROVIDER : {name.upper()}")
    dump(lines, f"  MODEL    : {model}")
    dump(lines, SEP)

    if not key:
        dump(lines, "  SKIPPED — no API key\n")
        return

    client = OpenAI(api_key=key, base_url=base)

    # ── 1. Model object from /v1/models (or CF search) ──────────
    dump(lines, f"\n{SEP2}")
    dump(lines, "  [1] MODEL OBJECT")
    dump(lines, SEP2)
    try:
        if name == "cloudflare":
            url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/models/search"
            r = requests.get(url, headers={"Authorization": f"Bearer {key}"},
                             params={"search": model.replace("@cf/", "")}, timeout=15)
            results = r.json().get("result", [])
            match = next((m for m in results if m.get("name") == model), results[0] if results else {})
            dump(lines, json.dumps(match, indent=2, default=str))
        else:
            all_models = client.models.list().data
            match = next((m for m in all_models if m.id == model), None)
            if match:
                raw = match.model_dump() if hasattr(match, "model_dump") else vars(match)
                dump(lines, json.dumps(raw, indent=2, default=str))
            else:
                dump(lines, f"  '{model}' not found in /v1/models")
    except Exception as e:
        dump(lines, f"  ERROR: {e}")

    # ── 2. All response headers ──────────────────────────────────
    dump(lines, f"\n{SEP2}")
    dump(lines, "  [2] RESPONSE HEADERS")
    dump(lines, SEP2)
    raw_resp = None
    try:
        raw_resp = client.chat.completions.with_raw_response.create(
            model=model,
            messages=[{"role": "user", "content": "Say: ok"}],
            max_tokens=5,
        )
        for k, v in sorted(dict(raw_resp.headers).items()):
            dump(lines, f"  {k:<50} : {v}")
    except Exception as e:
        dump(lines, f"  ERROR: {e}")

    # ── 3. Full completion object ────────────────────────────────
    dump(lines, f"\n{SEP2}")
    dump(lines, "  [3] COMPLETION OBJECT")
    dump(lines, SEP2)
    try:
        if raw_resp:
            parsed = raw_resp.parse()
            dump(lines, json.dumps(parsed.model_dump(), indent=2, default=str))
    except Exception as e:
        dump(lines, f"  ERROR: {e}")

    dump(lines, "")


lines = []
for p in PROVIDERS:
    inspect(p, lines)

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"\n\nWritten to {OUT}")
# AI Mind

AI Mind is a local-first personal AI workspace built with Flask, SQLite, and a browser-based chat interface. It provides a single chat experience over multiple AI providers while automatically building useful conversation context and long-term memory in the background.

The application is designed for people who want:

- A private, self-hosted chat interface
- Automatic conversation summaries and long-term memory
- Reliable fallback across free or individually configured AI providers
- Local persistence without requiring a hosted database
- Practical controls for prompt size, provider health, quotas, and model output

## Features

### Multi-provider AI routing

AI Mind can use the following providers when their credentials are configured:

- Groq
- Google Gemini
- Cloudflare Workers AI
- Mistral
- Hugging Face
- OpenRouter

Providers are discovered and live-tested during startup. The router:

- Selects models from provider catalogs where available
- Tests candidate models before publishing them for chat
- Falls back through providers in configured priority order
- Skips unavailable, unauthorized, deprecated, or empty-response models
- Remembers rejected models locally for eight hours
- Persists recent rate-limit failures across restarts
- Supports a separate provider order for background work
- Keeps the server available while slower provider discovery continues

### Automatic memory and context

Memory is built automatically after chat requests. No manual approval is required for normal operation.

The memory system:

- Extracts durable user preferences, projects, decisions, and constraints
- Creates rolling conversation summaries in the background
- Retrieves only relevant memories for each request
- Uses local ranking before optional model-based reranking
- Deduplicates memories using normalized content hashes
- Tracks confidence, importance, provenance, and access frequency
- Preserves previous versions when a fact is changed
- Marks outdated memories as superseded instead of silently deleting them
- Avoids storing credentials, secrets, and low-value temporary details

### Context optimization

Requests are compacted before being sent to external providers. The router:

- Keeps system instructions and the newest relevant messages
- Uses rolling summaries instead of replaying entire conversations
- Limits completion size for small free-tier models
- Accounts for tool definitions when fitting prompts
- Retries context-length failures with a minimal prompt
- Records token and latency telemetry when providers return usage data

### Chat interface

The web interface includes:

- Conversation history and search
- Projects with project-specific instructions
- Markdown and syntax-highlighted code
- Message copy, feedback, speech, and regenerate actions
- Provider/model badges
- Input and output token counts
- Light and dark themes
- File and text attachments
- Automatic background memory processing

## Architecture

```text
Browser
  |
  | HTTP/JSON
  v
Flask application (ai-mind/app.py)
  |
  +--> AI router
  |      +--> Model discovery and live probes
  |      +--> Provider priority and fallback
  |      +--> Prompt fitting and retry logic
  |      +--> Usage and failure telemetry
  |
  +--> Memory engine
  |      +--> Automatic memory extraction
  |      +--> Rolling summaries
  |      +--> Deduplication and supersession
  |
  +--> Retrieval
  |      +--> Local memory ranking
  |      +--> Previous conversation search
  |      +--> Relevant context assembly
  |
  +--> SQLite database
         +--> Conversations and messages
         +--> Memories and memory versions
         +--> Conversation summaries
         +--> AI provider usage
```

## Repository layout

```text
/server
├── README.md
└── ai-mind/
    ├── app.py                  Flask application and API routes
    ├── ai_router.py            Provider routing, prompt budgets, fallback
    ├── model_selector.py       Model discovery, probing, and model cache
    ├── database.py             SQLite schema and persistence helpers
    ├── memory_engine.py        Automatic memory and summary processing
    ├── memory_retrieval.py     Relevant long-term memory retrieval
    ├── conversation_retrieval.py
    ├── healthcheck.py          Provider/model diagnostics
    ├── templates/              HTML templates
    ├── static/                 Browser JavaScript and CSS
    ├── requirements.txt         Python dependencies
    └── .env.example             Configuration template
```

## Requirements

- Windows, Linux, or macOS
- Python 3.10 or newer
- At least one supported provider API key
- Internet access for configured external providers

SQLite is included with Python. No separate database server is required.

## Quick start

From the repository root:

### 1. Create a virtual environment

Windows PowerShell:

```powershell
Set-Location .\ai-mind
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Linux or macOS:

```bash
cd ai-mind
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

Windows PowerShell:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Linux or macOS:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Configure providers

Copy the example configuration:

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Linux or macOS:

```bash
cp .env.example .env
```

Add at least one provider key to `ai-mind/.env`. The application only initializes providers whose required credentials are present.

Example:

```env
GROQ_API_KEY=your-groq-key
GEMINI_API_KEY=your-gemini-key
OPENROUTER_API_KEY=your-openrouter-key
```

Never commit `.env`, API keys, or provider tokens.

### 4. Start the server

```powershell
python app.py
```

The application listens on:

```text
http://127.0.0.1:8081
```

Provider discovery continues in the background. The first chat request waits briefly for an available provider according to `AI_STARTUP_PROVIDER_WAIT`.

## Configuration

The following settings are available in `ai-mind/.env`.

### Provider configuration

```env
GROQ_API_KEY=
GEMINI_API_KEY=
CLOUDFLARE_API_KEY=
CLOUDFLARE_ACCOUNT_ID=
MISTRAL_API_KEY=
HUGGINGFACE_API_KEY=
OPENROUTER_API_KEY=
```

Provider priority can be customized:

```env
AI_PROVIDER_ORDER=groq,gemini,cloudflare,mistral,huggingface,openrouter
```

Background memory, retrieval, and summary calls can use a different order:

```env
AI_BACKGROUND_PROVIDER_ORDER=mistral,gemini,openrouter
```

### Prompt and startup behavior

```env
AI_MAX_TOKENS=800
AI_STARTUP_PROVIDER_WAIT=30
```

### Automatic memory

```env
AI_AUTO_MEMORY=true
AI_MEMORY_MIN_CONFIDENCE=0.88
AI_MEMORY_MIN_IMPORTANCE=0.70
AI_MEMORY_RECENT_MESSAGES=12
AI_SUMMARY_EVERY_MESSAGES=8
```

### Provider failure memory

```env
AI_QUOTA_COOLDOWN_MINUTES=15
```

Rejected model candidates are stored in `ai-mind/.model_cache.json`. This file is runtime state and should normally not be edited manually.

## API overview

The main endpoints include:

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/chat` | Send a chat message |
| `GET` | `/api/conversations` | List and search conversations |
| `GET` | `/api/conversation/<id>` | Load a conversation |
| `GET` | `/api/ai-router/status` | Inspect configured provider/model health |
| `GET` | `/api/memories` | List active memories |
| `DELETE` | `/api/memories/<id>` | Delete a memory |
| `PUT` | `/api/memories/<id>` | Edit a memory |
| `GET` | `/memories` | Open the memory management page |
| `GET` | `/api/projects` | List projects |
| `POST` | `/api/projects` | Create a project |

Example chat request:

```powershell
Invoke-RestMethod `
  -Uri http://127.0.0.1:8081/api/chat `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"message":"What is the current project status?"}'
```

The response includes the assistant text, provider, model, latency, fallback information, and usage data when available.

## Memory model

AI Mind separates short-term context from long-term memory:

```text
Raw messages
    ↓
Recent message window
    ↓
Rolling conversation summary
    ↓
Relevant long-term memories
    ↓
Current prompt
```

Memory records include lifecycle and provenance fields such as:

- Memory type
- Confidence
- Importance
- Source conversation
- Validity timestamps
- Active or superseded status
- Access count and last access time

When a user explicitly changes a durable preference or project decision, the newer memory becomes active and the older version remains available as superseded history.

## Provider health and troubleshooting

Check provider state while the server is running:

```powershell
Invoke-RestMethod http://127.0.0.1:8081/api/ai-router/status
```

Common behaviors:

- `404` or `model_not_found`: the candidate model is rejected and cached temporarily.
- `401` or `403`: credentials or provider access are unavailable.
- `429` or `rate_limited`: the provider enters cooldown and is skipped temporarily.
- Empty responses: the candidate fails the live probe and is not selected.
- Context-length errors: the router compacts and retries with a smaller prompt.

If a provider becomes available again before its cached rejection expires, force a fresh model scan by removing the intended runtime cache entry from `ai-mind/.model_cache.json` while the server is stopped.

## Development and validation

Run Python compilation checks:

```powershell
python -m py_compile app.py ai_router.py database.py memory_engine.py memory_retrieval.py conversation_retrieval.py model_selector.py
```

Run the provider diagnostics script when investigating model availability:

```powershell
python healthcheck.py
```

The project currently relies on focused script checks and local integration checks. When changing routing, memory, or prompt assembly, validate at least:

1. Python compilation.
2. SQLite initialization and migration.
3. Provider status endpoint.
4. A short `/api/chat` request.
5. Memory creation and retrieval.
6. Fallback behavior after a simulated provider failure.

## Privacy and security

- API keys are loaded from environment variables and should never be committed.
- Conversation history and memories are stored locally in SQLite.
- Configured providers receive the prompt context required for their requests.
- Do not place passwords, API tokens, private keys, or other secrets in chat messages.
- Review memories periodically and delete anything that should not persist.
- Do not expose the Flask development server directly to the public internet without adding authentication, TLS, and production deployment controls.

## Current limitations

- Provider model catalogs and free-tier limits change over time.
- Some providers do not return token usage metadata.
- Token counts may be estimated by the browser when the provider does not report them.
- Background memory processing uses configured AI providers and may consume free-tier quota.
- The default SQLite retrieval is intentionally lightweight; a vector index can be added later if measured evaluation shows that hybrid local retrieval is insufficient.
- The bundled Flask server is intended for local or trusted-network use, not as a hardened public production server.

## License

MIT License

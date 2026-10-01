import os
import time
import threading
import json
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from model_selector import get_best_free_models
from database import get_recent_provider_failures, record_ai_usage

load_dotenv()


@dataclass
class ProviderState:
    name: str
    model: str
    available: bool = True
    failures: int = 0
    cooldown_until: float = 0
    last_latency_ms: float | None = None
    last_error: str | None = None


class AIRouter:
    """
    Multi-provider AI router.

    Internal AI Mind message metadata is deliberately removed before
    messages are sent to external providers.

    Providers are tried in configured priority order:

        groq -> gemini -> cloudflare -> mistral -> huggingface -> openrouter

    That default order (see _build_providers) is chosen by how far each
    provider's free tier actually goes for a single-user assistant:

      1. groq       - fastest, and by far the most generous free daily
                      request/token allowance of the group.
      2. gemini     - excellent quality, still a solid daily allowance.
      3. cloudflare - usable, but the free Workers AI tier is a tiny
                      10k-neuron/day budget (roughly 15-25 replies).
      4. mistral    - ~1B tokens/month, but an unpublished, very low
                      requests-per-second ceiling.
      5. huggingface - smallest free allowance of all (~$0.10/mo in
                      routing credit), kept as a late fallback.
      6. openrouter - "openrouter/free", OpenRouter's own free-model
                      router; a broad catch-all last resort.

    Set AI_PROVIDER_ORDER (comma-separated provider names, e.g.
    "gemini,groq,openrouter") to override this order without touching
    code. Providers you don't mention keep their default relative order,
    appended after the ones you did mention.

    Failed providers are temporarily put into cooldown.
    Successful providers recover automatically.

    Callers (see chat()) can also pass preferred_provider to bump one
    provider to the front of the list for a single request - e.g. to
    honor a model the user explicitly picked in the UI - while still
    falling back through the rest of the list automatically if it's
    unavailable.
    """

    COOLDOWN_SECONDS = 60
    RATE_LIMIT_COOLDOWN_SECONDS = 15 * 60
    INVALID_MODEL_COOLDOWN_SECONDS = 60 * 60
    # Keep requests compatible with small free-tier models such as
    # allam-2-7b, whose effective context limit may be lower than catalog
    # metadata suggests.
    MAX_CONTEXT_TOKENS = 2048

    def __init__(self):
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.provider_ready = threading.Event()
        self.initializing = True
        self.max_tokens = int(os.getenv("AI_MAX_TOKENS", "4096"))
        self.test_fail_providers = { ... }
        self._best_models = {}
        self.providers = []

        # Do not block Flask startup on slow provider catalog scans.
        threading.Thread(
            target=self._initialize_providers,
            name="ai-provider-initializer",
            daemon=True,
        ).start()

    def _initialize_providers(self):
        try:
            self._best_models = get_best_free_models(
                on_model_selected=self._publish_selected_provider,
            )
            # The callback publishes providers as soon as each live probe
            # succeeds. This final pass applies any configured ordering.
            with self.lock:
                self.providers = self._apply_custom_order(self.providers)
        except Exception as error:
            print(f"[AI Router] Provider initialization failed: {error}")
        finally:
            self.initializing = False
            self.ready.set()

    def _publish_selected_provider(self, name: str, model: str):
        with self.lock:
            if any(provider["state"].name == name for provider in self.providers):
                return

            provider = self._create_provider_for_model(name, model)
            if provider:
                self.providers.append(provider)
                self.providers = self._apply_custom_order(self.providers)
                self.provider_ready.set()
                print(f"[AI Router] {name} is ready with {model}")

    def _create_provider_for_model(self, name: str, model: str):
        configs = {
            "groq": (
                os.getenv("GROQ_API_KEY"),
                "https://api.groq.com/openai/v1",
                None,
            ),
            "gemini": (
                os.getenv("GEMINI_API_KEY"),
                "https://generativelanguage.googleapis.com/v1beta/openai/",
                None,
            ),
            "cloudflare": (
                os.getenv("CLOUDFLARE_API_KEY"),
                f"https://api.cloudflare.com/client/v4/accounts/{os.getenv('CLOUDFLARE_ACCOUNT_ID')}/ai/v1",
                None,
            ),
            "mistral": (
                os.getenv("MISTRAL_API_KEY"),
                "https://api.mistral.ai/v1",
                None,
            ),
            "huggingface": (
                os.getenv("HUGGINGFACE_API_KEY"),
                "https://router.huggingface.co/v1",
                None,
            ),
            "openrouter": (
                os.getenv("OPENROUTER_API_KEY"),
                "https://openrouter.ai/api/v1",
                {"X-OpenRouter-Title": "Noel AI Mind"},
            ),
        }
        config = configs.get(name)
        if not config or not config[0]:
            return None
        return self._create_provider(
            name=name,
            api_key=config[0],
            base_url=config[1],
            model=model.removeprefix("models/") if name == "gemini" else model,
            headers=config[2],
        )

    # ---------------------------------------------------------
    # Message sanitization
    # ---------------------------------------------------------

    @staticmethod
    def sanitize_messages(
        messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Remove AI Mind internal fields such as `metadata` before
        sending conversation history to external model APIs.

        External providers generally accept only the standard
        OpenAI-compatible message properties.
        """

        cleaned = []

        allowed_keys = {
            "role",
            "content",
            "name",
            "tool_calls",
            "tool_call_id",
        }

        for message in messages:
            if not isinstance(message, dict):
                continue

            clean_message = {
                key: value
                for key, value in message.items()
                if key in allowed_keys
            }

            # Role is required for chat messages.
            if not clean_message.get("role"):
                continue

            # Avoid accidentally sending None as content.
            if "content" not in clean_message:
                clean_message["content"] = ""

            cleaned.append(clean_message)

        return cleaned

    @classmethod
    def _fit_messages(
        cls,
        messages: list[dict[str, Any]],
        requested_max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Keep the system prompt and newest messages within a safe budget."""
        if not messages:
            return messages, requested_max_tokens

        system_messages = [
            message for message in messages
            if message.get("role") == "system"
        ]
        other_messages = [
            message for message in messages
            if message.get("role") != "system"
        ]

        requested_completion = min(requested_max_tokens, 512)
        tool_chars = len(json.dumps(tools, separators=(",", ":"))) if tools else 0
        prompt_budget_chars = (
            cls.MAX_CONTEXT_TOKENS - requested_completion
        ) * 4 - tool_chars
        system_chars = sum(len(str(message.get("content", ""))) for message in system_messages)
        remaining_chars = max(600, prompt_budget_chars - system_chars)
        selected = []
        used_chars = 0

        for message in reversed(other_messages):
            message_chars = len(str(message.get("content", "")))
            if selected and used_chars + message_chars > remaining_chars:
                break
            if not selected and message_chars > remaining_chars:
                message = {
                    **message,
                    "content": str(message.get("content", ""))[-remaining_chars:],
                }
                message_chars = remaining_chars
            selected.append(message)
            used_chars += message_chars

        fitted_system = []
        system_budget = max(600, prompt_budget_chars // 2)
        for message in system_messages:
            content = str(message.get("content", ""))
            if len(content) > system_budget:
                message = {**message, "content": content[:system_budget]}
            fitted_system.append(message)

        fitted = fitted_system + list(reversed(selected))
        prompt_chars = sum(len(str(message.get("content", ""))) for message in fitted)
        estimated_prompt_tokens = max(1, prompt_chars // 4)
        available_completion = max(
            128,
            cls.MAX_CONTEXT_TOKENS - estimated_prompt_tokens,
        )
        return fitted, min(requested_max_tokens, available_completion)

    @staticmethod
    def _is_context_error(error: Exception) -> bool:
        text = str(error).lower()
        return (
            "reduce the length" in text
            or "maximum context" in text
            or "context length" in text
            or "too many tokens" in text
        )

    @staticmethod
    def _failure_category(error: Exception) -> str:
        text = str(error).lower()
        if "reduce the length" in text or "context" in text or "tokens" in text:
            return "context"
        if "429" in text or "rate limit" in text or "rate_limited" in text:
            return "rate_limit"
        if "404" in text or "model_not_found" in text:
            return "model_unavailable"
        if "403" in text or "401" in text:
            return "authorization"
        if "empty response" in text:
            return "empty_response"
        return "request_error"

    @staticmethod
    def _response_usage(response):
        usage = getattr(response, "usage", None)
        if not usage:
            return None, None, None
        return (
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            getattr(usage, "total_tokens", None),
        )

    def _build_providers(self):
        providers = []

        def selected_model(provider_name: str) -> str | None:
            # The selector probes configured overrides first, so prefer its
            # result here. Never bypass a failed probe with a stale override.
            model = self._best_models.get(provider_name)
            if not model:
                print(
                    f"[AI Router] {provider_name}: no discovered usable model, skipped"
                )
                return None
            # Gemini's REST model catalog returns "models/foo", while the
            # OpenAI-compatible endpoint expects "foo".
            if provider_name == "gemini":
                model = model.removeprefix("models/")
            return model

        groq_model = selected_model("groq") if os.getenv("GROQ_API_KEY") else None
        if groq_model:
            providers.append(self._create_provider(
                name="groq",
                api_key=os.getenv("GROQ_API_KEY"),
                base_url="https://api.groq.com/openai/v1",          # ← must be here
                model=groq_model
            ))

        gemini_model = selected_model("gemini") if os.getenv("GEMINI_API_KEY") else None
        if gemini_model:
            providers.append(self._create_provider(
                name="gemini",
                api_key=os.getenv("GEMINI_API_KEY"),
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                model=gemini_model
            ))

        cloudflare_model = (
            selected_model("cloudflare")
            if os.getenv("CLOUDFLARE_API_KEY") and os.getenv("CLOUDFLARE_ACCOUNT_ID")
            else None
        )
        if cloudflare_model:
            account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
            providers.append(self._create_provider(
                name="cloudflare",
                api_key=os.getenv("CLOUDFLARE_API_KEY"),
                base_url=f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
                model=cloudflare_model
            ))

        mistral_model = selected_model("mistral") if os.getenv("MISTRAL_API_KEY") else None
        if mistral_model:
            providers.append(self._create_provider(
                name="mistral",
                api_key=os.getenv("MISTRAL_API_KEY"),
                base_url="https://api.mistral.ai/v1",
                model=mistral_model
            ))

        huggingface_model = selected_model("huggingface") if os.getenv("HUGGINGFACE_API_KEY") else None
        if huggingface_model:
            providers.append(self._create_provider(
                name="huggingface",
                api_key=os.getenv("HUGGINGFACE_API_KEY"),
                base_url="https://router.huggingface.co/v1",
                model=huggingface_model
            ))

        openrouter_model = selected_model("openrouter") if os.getenv("OPENROUTER_API_KEY") else None
        if openrouter_model:
            providers.append(self._create_provider(
                name="openrouter",
                api_key=os.getenv("OPENROUTER_API_KEY"),
                base_url="https://openrouter.ai/api/v1",
                model=openrouter_model,
                headers={"X-OpenRouter-Title": "Noel AI Mind"}
            ))

        return self._apply_custom_order(providers)

    def _apply_custom_order(self, providers):
        """
        Optionally reorder providers via AI_PROVIDER_ORDER, a
        comma-separated list of provider names (e.g. "gemini,groq").

        Providers named in the env var are moved to the front in the
        order given. Any configured provider NOT named keeps its
        default relative order and is appended after them. Unknown
        names (typos, providers without an API key) are ignored.
        """

        order_env = os.getenv("AI_PROVIDER_ORDER", "").strip()

        if not order_env:
            return providers

        requested_order = [
            name.strip().lower()
            for name in order_env.split(",")
            if name.strip()
        ]

        by_name = {
            provider["state"].name: provider
            for provider in providers
        }

        ordered = []

        for name in requested_order:
            provider = by_name.pop(name, None)
            if provider is not None:
                ordered.append(provider)

        # Anything left over keeps its original relative order.
        ordered.extend(
            provider
            for provider in providers
            if provider["state"].name in by_name
        )

        return ordered

    def _create_provider(
        self,
        name: str,
        api_key: str,
        base_url: str,
        model: str,
        headers: dict[str, str] | None = None
    ):
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=headers or {}
        )

        return {
            "client": client,
            "state": ProviderState(
                name=name,
                model=model
            )
        }

    # ---------------------------------------------------------
    # Provider state
    # ---------------------------------------------------------

    def _is_available(self, state: ProviderState) -> bool:
        return time.time() >= state.cooldown_until

    @staticmethod
    def _apply_request_kind_order(providers, request_kind):
        """Apply an optional provider chain for non-foreground work."""
        if request_kind == "foreground":
            return providers

        configured = os.getenv("AI_BACKGROUND_PROVIDER_ORDER", "").strip()
        if not configured:
            return providers

        order = [
            name.strip().lower()
            for name in configured.split(",")
            if name.strip()
        ]
        if not order:
            return providers

        by_name = {
            provider["state"].name.lower(): provider
            for provider in providers
        }
        selected = [
            by_name[name]
            for name in order
            if name in by_name
        ]
        selected_names = {provider["state"].name.lower() for provider in selected}
        return selected + [
            provider for provider in providers
            if provider["state"].name.lower() not in selected_names
        ]

    @staticmethod
    def _has_persisted_quota_failure(state: ProviderState) -> bool:
        """Avoid retrying a provider that recently reported an exhausted quota."""
        minutes = int(os.getenv("AI_QUOTA_COOLDOWN_MINUTES", "15"))
        failures = get_recent_provider_failures(
            state.name,
            state.model,
            max(1, minutes),
        )
        return failures.get("rate_limit", 0) > 0

    def _mark_failure(
        self,
        state: ProviderState,
        error: Exception
    ):
        with self.lock:
            state.failures += 1
            state.available = False
            state.last_error = str(error)

            error_text = str(error).lower()
            status_code = getattr(error, "status_code", None)
            if status_code == 429 or "rate limit" in error_text or "rate_limited" in error_text:
                cooldown = self.RATE_LIMIT_COOLDOWN_SECONDS
            elif status_code == 404 or "model_not_found" in error_text:
                cooldown = self.INVALID_MODEL_COOLDOWN_SECONDS
            else:
                cooldown = self.COOLDOWN_SECONDS

            state.cooldown_until = time.time() + cooldown

    def _mark_success(
        self,
        state: ProviderState,
        latency_ms: float
    ):
        with self.lock:
            state.failures = 0
            state.available = True
            state.cooldown_until = 0
            state.last_error = None
            state.last_latency_ms = latency_ms

    # ---------------------------------------------------------
    # Chat
    # ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        preferred_provider: str | None = None,
        tools=None,
        request_kind: str = "foreground",
    ):
        """
        Send a chat request through the provider pool.

        Internal message metadata is stripped before the request
        is sent to any external provider.

        preferred_provider (optional): a provider name (e.g. "groq",
        "gemini") to try first for this one request, such as a model
        the user explicitly picked in the UI. If that provider is
        unavailable or fails, the router still falls back through
        the rest of the providers in their normal order - a
        preference never turns into a hard failure by itself. An
        unrecognized name is ignored and the default order is used.

        Returns:

        {
            "content": "...",
            "provider": "...",
            "model": "...",
            "latency_ms": ...,
            "fallback_used": bool,
            "attempts": int
        }
        """

        if not self.providers and self.initializing:
            self.provider_ready.wait(
                timeout=float(os.getenv("AI_STARTUP_PROVIDER_WAIT", "30"))
            )

        if not self.providers:
            raise RuntimeError(
                "No AI providers configured. "
                "Add at least one provider API key to .env."
            )

        # IMPORTANT:
        # AI Mind stores internal metadata on messages for its own
        # database/UI. Providers such as Groq, Hugging Face and
        # Mistral reject that property. Sanitize once at the router
        # boundary so every provider gets the same clean payload.
        api_messages = self.sanitize_messages(messages)

        if not api_messages:
            raise RuntimeError(
                "No valid messages were supplied to the AI router."
            )

        max_tokens = (
            max_tokens
            if max_tokens is not None
            else self.max_tokens
        )

        attempts = 0
        fallback_used = False
        errors = []

        with self.lock:
            providers = list(self.providers)

        if preferred_provider:
            preferred_provider = preferred_provider.strip().lower()

            preferred = [
                provider
                for provider in providers
                if provider["state"].name == preferred_provider
            ]

            if preferred:
                rest = [
                    provider
                    for provider in providers
                    if provider["state"].name != preferred_provider
                ]
                providers = preferred + rest

        providers = self._apply_request_kind_order(
            providers,
            request_kind,
        )

        for provider in providers:
            client = provider["client"]
            state = provider["state"]

            # Skip providers currently in cooldown.
            if not self._is_available(state):
                continue

            # Cooldowns normally live in memory. The usage table preserves
            # rate-limit knowledge across restarts so background jobs cannot
            # immediately consume the same exhausted provider again.
            if self._has_persisted_quota_failure(state):
                print(
                    f"[AI Router] Skipping {state.name}: "
                    "recent rate limit recorded"
                )
                continue

            # ------------------------------------------------
            # Test failure simulation
            # ------------------------------------------------
            if state.name.lower() in self.test_fail_providers:
                print(
                    f"[AI Router] TEST: Simulating failure "
                    f"for {state.name}"
                )

                error = RuntimeError(
                    f"Simulated failure for provider: {state.name}"
                )

                self._mark_failure(state, error)

                errors.append(
                    f"{state.name}: {error}"
                )

                attempts += 1
                fallback_used = True
                continue

            attempts += 1
            start = time.perf_counter()

            try:
                request_messages, request_max_tokens = self._fit_messages(
                    api_messages,
                    max_tokens,
                    tools,
                )
                request_args: dict[str, Any] = {
                    "model": state.model,
                    "messages": request_messages,
                    "max_tokens": request_max_tokens,
                }
                if tools:
                    request_args["tools"] = tools

                response = client.chat.completions.create(
                    **request_args
                )
                prompt_tokens, completion_tokens, total_tokens = (
                    self._response_usage(response)
                )

                tool_calls = None
                choice = response.choices[0]
                if choice.message.tool_calls:
                    tool_calls = [
                        {"name": tc.function.name, "arguments": json.loads(tc.function.arguments), "id": tc.id}
                        for tc in choice.message.tool_calls
                    ]

                latency_ms = round(
                    (time.perf_counter() - start) * 1000,
                    2
                )

                content = response.choices[0].message.content

                if not content:
                    raise RuntimeError(
                        f"{state.name} returned an empty response"
                    )

                self._mark_success(
                    state,
                    latency_ms
                )
                record_ai_usage(
                    state.name,
                    state.model,
                    request_kind,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    latency_ms,
                    "success",
                )

                return {
                    "content": content,
                    "provider": state.name,
                    "model": state.model,
                    "latency_ms": latency_ms,
                    "fallback_used": fallback_used,
                    "attempts": attempts,
                    "tool_calls": tool_calls,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }

            except Exception as error:
                if self._is_context_error(error):
                    # Retry once with only the system instruction and the
                    # newest user turn. A context rejection is request-
                    # specific and should not quarantine a healthy model.
                    compact_messages = [
                        message for message in api_messages
                        if message.get("role") == "system"
                    ]
                    latest_user = next(
                        (
                            message for message in reversed(api_messages)
                            if message.get("role") == "user"
                        ),
                        None,
                    )
                    if latest_user:
                        compact_messages.append(latest_user)
                    compact_messages, compact_max_tokens = self._fit_messages(
                        compact_messages,
                        256,
                        None,
                    )
                    try:
                        retry_response = client.chat.completions.create(
                            model=state.model,
                            messages=compact_messages,
                            max_tokens=compact_max_tokens,
                        )
                        retry_content = retry_response.choices[0].message.content
                        if retry_content and retry_content.strip():
                            latency_ms = round(
                                (time.perf_counter() - start) * 1000,
                                2,
                            )
                            self._mark_success(state, latency_ms)
                            record_ai_usage(
                                state.name,
                                state.model,
                                request_kind,
                                None,
                                None,
                                None,
                                latency_ms,
                                "success_retry",
                            )
                            return {
                                "content": retry_content,
                                "provider": state.name,
                                "model": state.model,
                                "latency_ms": latency_ms,
                                "fallback_used": fallback_used,
                                "attempts": attempts,
                                "tool_calls": None,
                                "prompt_tokens": None,
                                "completion_tokens": None,
                                "total_tokens": None,
                            }
                    except Exception as retry_error:
                        error = retry_error

                latency_ms = round(
                    (time.perf_counter() - start) * 1000,
                    2
                )

                state.last_latency_ms = latency_ms

                self._mark_failure(
                    state,
                    error
                )
                record_ai_usage(
                    state.name,
                    state.model,
                    request_kind,
                    None,
                    None,
                    None,
                    latency_ms,
                    "failure",
                    self._failure_category(error),
                )

                errors.append(
                    f"{state.name}: {error}"
                )

                fallback_used = True

                print(
                    f"[AI Router] {state.name} failed "
                    f"after {latency_ms}ms: {error}"
                )

        raise RuntimeError(
            "All AI providers failed.\n" +
            "\n".join(errors)
        )

    # ---------------------------------------------------------
    # Status
    # ---------------------------------------------------------

    def get_status(self):
        result = []

        for provider in self.providers:
            state = provider["state"]

            available = self._is_available(state)

            result.append({
                "provider": state.name,
                "model": state.model,
                "available": available,
                "failures": state.failures,
                "cooldown_until": (
                    state.cooldown_until
                    if state.cooldown_until > time.time()
                    else None
                ),
                "last_latency_ms": state.last_latency_ms,
                "last_error": state.last_error
            })

        return result


# Global router instance
ai_router = AIRouter()
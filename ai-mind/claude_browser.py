"""Claude browser provider for the Chromium CDP session."""

from __future__ import annotations

import os
import re
import time
from typing import Any

from playwright.sync_api import sync_playwright


FREE_MODELS = (
    "Sonnet 5",
    "Sonnet 4.6",
)
EFFORT_OPTIONS = ("Low", "Medium", "High", "Max", "Thinking")
USAGE_LIMIT_MESSAGE = (
    "Claude has reached its usage limit. "
    "The five-hour Claude limit has been reached. "
    "Wait for the limit to reset or select another provider."
)


class ClaudeUsageLimitError(RuntimeError):
    """Raised when Claude displays its rolling usage-limit message."""


class ClaudeBrowserClient:
    """Adapter with the same chat-shaped boundary as API providers."""

    def __init__(
        self,
        cdp_url: str,
        model: str,
        effort: str = "High",
        claude_url: str = "https://claude.ai/new",
    ):
        self.cdp_url = cdp_url
        self.model = model
        self.effort = effort if effort in EFFORT_OPTIONS else "High"
        self.claude_url = claude_url

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        effort: str | None = None,
    ):
        requested_model = self._canonical_model(model or self.model)
        requested_effort = effort or self.effort
        if requested_effort not in EFFORT_OPTIONS:
            raise RuntimeError(
                f"Unsupported Claude effort '{requested_effort}'. "
                f"Choose one of: {', '.join(EFFORT_OPTIONS)}."
            )
        prompt = self._prompt_from_messages(messages)
        started = time.perf_counter()
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(self.cdp_url)
            page = self._find_or_open_page(browser)
            self._raise_if_usage_limited(page)
            self._select_model(page, requested_model)
            self._select_effort(page, requested_effort)
            composer = self._find_composer(page)
            before = page.locator("body").inner_text()
            composer.fill(prompt)
            composer.press("Enter")
            content = self._wait_for_response(page, before)
        return {
            "content": content,
            "model": requested_model,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "effort": requested_effort,
        }

    def _find_or_open_page(self, browser):
        pages = [
            page
            for context in browser.contexts
            for page in context.pages
            if "claude.ai" in page.url
        ]
        if pages:
            return pages[0]

        if not browser.contexts:
            raise RuntimeError(
                "The configured Chromium CDP session has no browser context "
                "available for opening Claude."
            )

        page = browser.contexts[0].new_page()
        try:
            page.goto(
                self.claude_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except Exception as error:
            raise RuntimeError(
                f"Could not open Claude at {self.claude_url}: {error}"
            ) from error
        return page

    @staticmethod
    def _canonical_model(model):
        normalized = str(model).strip().casefold()
        for candidate in FREE_MODELS:
            if candidate.casefold() == normalized:
                return candidate
        raise RuntimeError(
            f"Claude model '{model}' is not in the free-model allowlist."
        )

    @staticmethod
    def _prompt_from_messages(messages):
        usable = [
            message
            for message in messages
            if message.get("content") and message.get("role") != "system"
        ]
        if not usable:
            raise RuntimeError("No usable messages were supplied to Claude.")
        latest = usable[-1]
        context = usable[:-1]
        if not context:
            return str(latest["content"])
        history = "\n\n".join(
            f"[{message['role'].upper()} MESSAGE]\n{message['content']}"
            for message in context[-8:]
        )
        return (
            "Background from the earlier conversation is quoted below. "
            "Use it only when it helps answer the final message.\n\n"
            "<conversation_history>\n"
            f"{history}\n"
            "</conversation_history>\n\n"
            "[CURRENT USER MESSAGE]\n"
            f"{latest['content']}"
        )

    @staticmethod
    def _find_composer(page):
        for selector in (
            'textarea[placeholder*="Message"]',
            'textarea[aria-label*="Message"]',
            '[contenteditable="true"][role="textbox"]',
            '[contenteditable="true"]',
        ):
            candidate = page.locator(selector).last
            if candidate.count() and candidate.is_visible():
                return candidate
        raise RuntimeError("Claude's message box was not found.")

    @staticmethod
    def _raise_if_usage_limited(page):
        message = ClaudeBrowserClient._usage_limit_message(page)
        if message:
            raise ClaudeUsageLimitError(message)

    @staticmethod
    def _usage_limit_message(page):
        body = page.locator("body").inner_text()
        lowered = body.lower()
        indicators = (
            "five-hour limit",
            "5-hour limit",
            "usage limit",
            "limit reached",
            "reached your limit",
            "out of free messages",
            "hit your limit for claude messages",
        )
        if not any(indicator in lowered for indicator in indicators):
            return None

        reset_match = re.search(
            r"(?:reset|until)\s+(?:at\s+)?(\d{1,2}:\d{2}\s*(?:AM|PM))",
            body,
            re.IGNORECASE,
        )
        if reset_match:
            return (
                "Claude has reached its usage limit. "
                f"Free messages reset at {reset_match.group(1)}. "
                "Wait for the reset or select another provider."
            )
        return USAGE_LIMIT_MESSAGE

    def usage_limit_message(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(self.cdp_url)
            pages = [
                page
                for context in browser.contexts
                for page in context.pages
                if "claude.ai" in page.url
            ]
            if not pages:
                return None
            return self._usage_limit_message(pages[0])

    @staticmethod
    def _select_model(page, model):
        picker = page.locator('button[aria-label^="Model:"]').last
        if not picker.count():
            raise RuntimeError("Claude's model picker was not found.")
        if model.lower() in picker.inner_text().lower():
            return
        picker.click()
        page.wait_for_timeout(250)
        more = page.get_by_text("More models", exact=True)
        if more.count():
            more.click()
            page.wait_for_timeout(400)
        menu = page.locator('[role="menu"]').last
        option = menu.get_by_text(model, exact=True)
        if not option.count():
            page.keyboard.press("Escape")
            raise RuntimeError(
                f"Claude model '{model}' was not available in the current account."
            )
        option.click()
        page.wait_for_timeout(400)

    @staticmethod
    def _select_effort(page, effort):
        controls = page.locator("button").filter(has_text=effort)
        visible_controls = [
            controls.nth(index)
            for index in range(controls.count())
            if controls.nth(index).is_visible()
        ]
        if not visible_controls:
            return
        visible_controls[-1].click()
        page.wait_for_timeout(250)
        option = page.get_by_text(effort, exact=True).last
        if option.count() and option.is_visible():
            option.click()
            page.wait_for_timeout(250)

    @staticmethod
    def _wait_for_response(page, before):
        transient = {
            "contemplating", "running", "claude is thinking",
            "thinking", "generating", "searching the web",
            "searching", "searched the web", "browsing the web",
            "web search", "reading",
        }
        last_text = None
        stable_since = None
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            ClaudeBrowserClient._raise_if_usage_limited(page)
            candidate_text = None
            for selector in ('[data-testid="assistant-message"]', "main article"):
                messages = page.locator(selector)
                if not messages.count():
                    continue
                text = messages.last.inner_text().strip()
                normalized = " ".join(text.split()).lower()
                if text and text != before and normalized not in transient:
                    candidate_text = text
                    break
            if candidate_text:
                if candidate_text == last_text:
                    if stable_since is None:
                        stable_since = time.monotonic()
                    elif time.monotonic() - stable_since >= 2:
                        return candidate_text
                else:
                    last_text = candidate_text
                    stable_since = time.monotonic()
            else:
                last_text = None
                stable_since = None
            time.sleep(1)
        ClaudeBrowserClient._raise_if_usage_limited(page)
        raise RuntimeError("Timed out waiting for Claude's response.")


def configured_claude():
    cdp_url = os.getenv("CLAUDE_CDP_URL", "http://127.0.0.1:9222").strip()
    claude_url = os.getenv("CLAUDE_URL", "https://claude.ai/new").strip()
    model = os.getenv("CLAUDE_DEFAULT_MODEL", "Sonnet 4.6").strip()
    if model not in FREE_MODELS:
        model = FREE_MODELS[0]
    return ClaudeBrowserClient(
        cdp_url=cdp_url,
        model=model,
        effort=os.getenv("CLAUDE_EFFORT", "High").strip(),
        claude_url=claude_url,
    )

import os
import json
import re
import threading

from ai_router import ai_router

from database import (
    get_messages,
    get_memories,
    get_ignored_memories,
    add_memory,
    memory_exists,
    is_memory_ignored,
    get_conversation_summary,
    replace_memory,
    save_conversation_summary,
)


# ============================================================
# Configuration
# ============================================================

AUTO_MEMORY_ENABLED = (
    os.getenv("AI_AUTO_MEMORY", "true").strip().lower()
    in {"1", "true", "yes", "on"}
)

MIN_CONFIDENCE = float(
    os.getenv("AI_MEMORY_MIN_CONFIDENCE", "0.88")
)

MIN_IMPORTANCE = float(
    os.getenv("AI_MEMORY_MIN_IMPORTANCE", "0.70")
)

MAX_RECENT_MESSAGES = int(
    os.getenv("AI_MEMORY_RECENT_MESSAGES", "12")
)
SUMMARY_EVERY_MESSAGES = int(
    os.getenv("AI_SUMMARY_EVERY_MESSAGES", "8")
)


# Prevent multiple memory jobs for the same conversation
_active_jobs = set()
_pending_jobs = set()
_jobs_lock = threading.Lock()


# ============================================================
# Helpers
# ============================================================

def _clean_json_response(content):
    """Remove markdown code fences and parse JSON safely."""

    if not content:
        return None

    text = content.strip()
    fenced = re.search(
        r"```(?:json)?\s*([\s\S]*?)\s*```",
        text,
        re.IGNORECASE,
    )
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(text)
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _build_conversation_text(messages):
    """Convert recent messages into compact text for analysis."""

    recent = messages[-MAX_RECENT_MESSAGES:]

    return "\n".join(
        f"{message['role']}: {message['content']}"
        for message in recent
    )


def _build_memory_text(memories):
    """Represent existing memories for the AI."""

    if not memories:
        return "No existing memories."

    return "\n".join(
        (
            f"[ID {memory['id']}] "
            f"[{memory['memory_type']}] "
            f"{memory['content']} "
            f"(confidence={memory['confidence']})"
        )
        for memory in memories
    )


def _build_prompt(messages, memories, ignored_memories):
    conversation_text = _build_conversation_text(messages)
    memory_text = _build_memory_text(memories)

    ignored_text = (
        "\n".join(f"- {item}" for item in ignored_memories)
        if ignored_memories
        else "No ignored memories."
    )

    return f"""
You are the memory manager for a personal AI assistant.

Your job is NOT to summarize the conversation.

Your job is to determine whether the conversation contains
stable, useful information about the USER that should persist
across future conversations.

Only create or update a memory when the information is genuinely
useful beyond the current conversation.

GOOD memory candidates:
- Stable user preferences
- Stable technical preferences
- Long-term projects
- Important ongoing goals
- Recurring preferences
- User facts that are useful in future conversations
- Explicit corrections to an existing memory

DO NOT store:
- One-off questions
- Temporary thoughts
- One-time tasks
- Facts about the assistant
- Information inferred without evidence
- Conversation details that have no future value
- Secrets, passwords, API keys, credentials or tokens
- Precise sensitive personal information
- Information that is obviously temporary unless it represents
  an important ongoing project or goal

IMPORTANT:
- Prefer concise, generalized memories.
- Do not copy the user's entire sentence.
- Never create a duplicate of an existing memory.
- If an existing memory describes the same concept and the user
  provides an updated/corrected version, UPDATE that memory.
  The previous version will be retained as superseded history.
- Do not delete memories automatically.
- Only create/update memories when confidence is high.
- Maximum 2 memory actions.
- If nothing deserves to be remembered, return an empty actions array.

Existing memories:
{memory_text}

Previously ignored memories:
{ignored_text}

Recent conversation:
{conversation_text}

Return ONLY valid JSON in exactly this format:

{{
  "actions": [
    {{
      "action": "create",
      "content": "Concise memory statement",
      "type": "long_term_project",
      "confidence": 0.95,
      "importance": 0.90
    }}
  ]
}}

For an update:

{{
  "actions": [
    {{
      "action": "update",
      "memory_id": 12,
      "content": "Updated memory statement",
      "type": "technical_preference",
      "confidence": 0.96,
      "importance": 0.85
    }}
  ]
}}

Valid memory types:
- fact
- personal_fact
- communication_preference
- technical_preference
- long_term_project
- recurring_preference
- general

If nothing should be remembered:

{{
  "actions": []
}}
"""


def update_conversation_summary(conversation_id, messages):
    """Refresh a compact rolling summary only after a batch of new turns."""
    if len(messages) < SUMMARY_EVERY_MESSAGES:
        return False

    existing = get_conversation_summary(conversation_id)
    if existing and len(messages) - existing["message_count"] < SUMMARY_EVERY_MESSAGES:
        return False

    previous = existing["summary"] if existing else "No previous summary."
    recent = _build_conversation_text(messages[-16:])
    prompt = f"""
Update a compact conversation summary for a personal AI assistant.
Preserve durable decisions, active projects, constraints, unresolved
questions, and user preferences. Remove greetings, repetition, and
temporary tool output. Do not invent facts.

Previous summary:
{previous}

Recent messages:
{recent}

Return only the updated summary in plain text, no headings or markdown.
Keep it under 1200 characters.
"""

    try:
        result = ai_router.chat(
            [{"role": "user", "content": prompt}],
            max_tokens=300,
            request_kind="background_summary",
        )
        summary = (result.get("content") or "").strip()
        if not summary:
            return False
        save_conversation_summary(conversation_id, summary[:4800], len(messages))
        return True
    except Exception as error:
        print(f"[Memory Engine] Summary update failed: {error}")
        return False


# ============================================================
# AI analysis
# ============================================================

def analyze_memory(conversation_id):
    """
    Analyze a conversation and determine whether memories
    should be created or updated.
    """

    messages = get_messages(conversation_id)

    if not messages:
        return {
            "created": [],
            "updated": [],
            "skipped": []
        }

    memories = get_memories()
    ignored_memories = get_ignored_memories()

    prompt = _build_prompt(
        messages,
        memories,
        ignored_memories
    )

    try:
        result = ai_router.chat(
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=700,
            request_kind="background_memory",
        )

        parsed = _clean_json_response(
            result.get("content")
        )

        if not parsed:
            print(
                "[Memory Engine] AI returned invalid JSON."
            )
            return {
                "created": [],
                "updated": [],
                "skipped": []
            }

        actions = parsed.get("actions", [])

        if not isinstance(actions, list):
            return {
                "created": [],
                "updated": [],
                "skipped": []
            }

        created = []
        updated = []
        skipped = []

        # Existing memories indexed by ID
        existing_by_id = {
            memory["id"]: memory
            for memory in memories
        }

        for action in actions[:2]:

            if not isinstance(action, dict):
                continue

            action_type = (
                str(action.get("action", ""))
                .strip()
                .lower()
            )

            content = (
                str(action.get("content", ""))
                .strip()
            )

            memory_type = (
                str(action.get("type", "general"))
                .strip()
                .lower()
            )

            try:
                confidence = float(
                    action.get("confidence", 0)
                )
            except (TypeError, ValueError):
                confidence = 0

            try:
                importance = float(
                    action.get("importance", 0)
                )
            except (TypeError, ValueError):
                importance = 0

            # --------------------------------------------
            # Basic validation
            # --------------------------------------------

            if not content:
                skipped.append({
                    "reason": "empty_content"
                })
                continue

            if not (
                MIN_CONFIDENCE <= confidence <= 1
            ):
                skipped.append({
                    "content": content,
                    "reason": "low_confidence"
                })
                continue

            if importance < MIN_IMPORTANCE:
                skipped.append({
                    "content": content,
                    "reason": "low_importance"
                })
                continue

            if is_memory_ignored(content):
                skipped.append({
                    "content": content,
                    "reason": "ignored"
                })
                continue

            # --------------------------------------------
            # CREATE
            # --------------------------------------------

            if action_type == "create":

                existing_id = memory_exists(content)

                if existing_id:
                    skipped.append({
                        "content": content,
                        "reason": "duplicate"
                    })
                    continue

                memory_id = add_memory(
                    content,
                    memory_type,
                    confidence,
                    importance,
                    conversation_id,
                    None,
                )

                created.append({
                    "id": memory_id,
                    "content": content,
                    "type": memory_type,
                    "confidence": confidence,
                    "importance": importance
                })

            # --------------------------------------------
            # UPDATE
            # --------------------------------------------

            elif action_type == "update":

                try:
                    memory_id = int(
                        action.get("memory_id")
                    )
                except (TypeError, ValueError):
                    skipped.append({
                        "content": content,
                        "reason": "invalid_memory_id"
                    })
                    continue

                existing = existing_by_id.get(memory_id)

                if not existing:
                    skipped.append({
                        "content": content,
                        "reason": "memory_not_found"
                    })
                    continue

                # Don't perform pointless updates
                if (
                    existing["content"].strip().lower()
                    == content.lower()
                ):
                    skipped.append({
                        "content": content,
                        "reason": "unchanged"
                    })
                    continue

                new_memory_id = replace_memory(
                    memory_id,
                    content,
                    memory_type,
                    confidence,
                    importance,
                    conversation_id,
                    None,
                )

                updated.append({
                    "id": new_memory_id,
                    "superseded_id": memory_id,
                    "content": content,
                    "type": memory_type,
                    "confidence": confidence,
                    "importance": importance
                })

            else:
                skipped.append({
                    "content": content,
                    "reason": "unknown_action"
                })

        if created:
            print(
                f"[Memory Engine] Created "
                f"{len(created)} memory(s)"
            )

        if updated:
            print(
                f"[Memory Engine] Updated "
                f"{len(updated)} memory(s)"
            )

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped
        }

    except Exception as error:
        print(
            f"[Memory Engine] Analysis failed: {error}"
        )

        return {
            "created": [],
            "updated": [],
            "skipped": []
        }


# ============================================================
# Background processing
# ============================================================

def _memory_job(conversation_id):
    try:
        messages = get_messages(conversation_id)
        update_conversation_summary(conversation_id, messages)
        analyze_memory(conversation_id)

    except Exception as error:
        print(
            f"[Memory Engine] Background job failed: {error}"
        )

    finally:
        rerun = False
        with _jobs_lock:
            _active_jobs.discard(conversation_id)
            if conversation_id in _pending_jobs:
                _pending_jobs.discard(conversation_id)
                _active_jobs.add(conversation_id)
                rerun = True

        if rerun:
            thread = threading.Thread(
                target=_memory_job,
                args=(conversation_id,),
                daemon=True,
            )
            thread.start()


def schedule_memory_processing(conversation_id):
    """
    Start memory processing in the background.

    Multiple requests for the same conversation will not create
    duplicate jobs.
    """

    if not AUTO_MEMORY_ENABLED:
        return

    with _jobs_lock:
        if conversation_id in _active_jobs:
            _pending_jobs.add(conversation_id)
            return

        _active_jobs.add(conversation_id)

    thread = threading.Thread(
        target=_memory_job,
        args=(conversation_id,),
        daemon=True
    )

    thread.start()
import json
import re
import re

from ai_router import ai_router
from database import get_memories


DEFAULT_LIMIT = 5


def _normalize_text(text):
    """Normalize text for basic matching."""
    return re.sub(r"\s+", " ", text.strip().lower())


def _keyword_overlap(query, content):
    """
    Calculate a simple keyword-overlap score.

    This is only used as a fallback when the retrieval model
    fails or returns invalid JSON.
    """
    query_words = set(re.findall(r"\b[a-zA-Z0-9]{3,}\b", _normalize_text(query)))
    content_words = set(re.findall(r"\b[a-zA-Z0-9]{3,}\b", _normalize_text(content)))

    if not query_words or not content_words:
        return 0.0

    return len(query_words & content_words) / len(query_words)


def _fallback_retrieval(user_message, memories, limit):
    """
    Basic keyword-based fallback.

    This ensures retrieval still works if the AI retrieval call
    fails because of a provider error or malformed response.
    """
    scored = []

    for memory in memories:
        score = _keyword_overlap(
            user_message,
            memory["content"]
        )

        if score > 0:
            scored.append((score, memory))

    scored.sort(
        key=lambda item: (
            item[0],
            item[1].get("confidence", 0)
        ),
        reverse=True
    )

    return [memory for _, memory in scored[:limit]]


def _build_retrieval_prompt(user_message, memories, limit):
    """Build the prompt used by the retrieval model."""

    memory_lines = []

    for memory in memories:
        memory_lines.append(
            f"Memory ID: {memory['id']}\n"
            f"Type: {memory['memory_type']}\n"
            f"Confidence: {memory['confidence']}\n"
            f"Content: {memory['content']}"
        )

    memory_context = "\n\n".join(memory_lines)

    return f"""
You are the memory retrieval component of a personal AI assistant.

Your job is ONLY to identify which existing memories are relevant
to the user's current message.

Do NOT create memories.
Do NOT modify memories.
Do NOT rewrite memories.
Do NOT infer facts that are not explicitly present in the memories.

Return ONLY the IDs of memories that are genuinely useful for
answering the user's current message.

User message:
{user_message}

Available memories:

{memory_context}

Rules:
- Return at most {limit} memory IDs.
- Only select memories that have meaningful relevance to the user's message.
- Do not select memories merely because they are generally about the user.
- A communication preference may be selected when it affects how the response should be written.
- Personal facts should only be selected when relevant.
- Project memories should only be selected when the user's message relates to that project.
- If no memories are relevant, return an empty list.
- Never invent memory IDs.

Return EXACTLY this JSON format:

{{
  "memory_ids": [3, 4]
}}

or, when nothing is relevant:

{{
  "memory_ids": []
}}
""".strip()


def _parse_memory_ids(response, valid_ids, limit):
    """
    Safely parse the retrieval model response.

    Only IDs that actually exist in the database are accepted.
    """

    text = (response or "").strip()
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

    data = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            break
        except (json.JSONDecodeError, TypeError):
            continue

    if not isinstance(data, dict):
        return []

    memory_ids = data.get("memory_ids", [])

    if not isinstance(memory_ids, list):
        return []

    result = []

    for memory_id in memory_ids:
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            continue

        if memory_id in valid_ids and memory_id not in result:
            result.append(memory_id)

        if len(result) >= limit:
            break

    return result


def retrieve_relevant_memories(user_message, limit=DEFAULT_LIMIT):
    """
    Retrieve memories relevant to the current user message.

    Returns a list of memory dictionaries.

    Example:

        [
            {
                "id": 3,
                "content": "...",
                "memory_type": "long_term_project",
                "confidence": 0.96
            }
        ]
    """

    if not user_message or not user_message.strip():
        return []

    memories = get_memories()

    if not memories:
        return []

    limit = max(1, int(limit))

    # Don't send an unnecessarily large number of memories
    # to the retrieval model.
    candidate_memories = memories[:50]

    prompt = _build_retrieval_prompt(
        user_message,
        candidate_memories,
        limit
    )

    try:
        result = ai_router.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a precise memory retrieval system. "
                        "Return valid JSON only."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=300,
            request_kind="retrieval",
        )

        response = result.get("content", "")

        valid_ids = {
            memory["id"]
            for memory in candidate_memories
        }

        selected_ids = _parse_memory_ids(
            response,
            valid_ids,
            limit
        )

        if selected_ids:
            memory_by_id = {
                memory["id"]: memory
                for memory in candidate_memories
            }

            return [
                memory_by_id[memory_id]
                for memory_id in selected_ids
            ]

        # An empty list can be a legitimate AI answer.
        # However, if the model returned malformed JSON,
        # _parse_memory_ids also returns [].
        #
        # Use the lightweight fallback only when the response
        # clearly isn't valid JSON.
        try:
            json.loads(response)
            return []
        except (json.JSONDecodeError, TypeError):
            pass

    except Exception as error:
        print(f"[Memory Retrieval] AI retrieval failed: {error}")

    # Final fallback: simple keyword matching.
    return _fallback_retrieval(
        user_message,
        candidate_memories,
        limit
    )


def format_memories_for_prompt(memories):
    """
    Convert retrieved memories into text suitable for the
    main AI system prompt.
    """

    if not memories:
        return ""

    lines = []

    for memory in memories:
        lines.append(
            f"- {memory['content']}"
        )

    return "\n".join(lines)
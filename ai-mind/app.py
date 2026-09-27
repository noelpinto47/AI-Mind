import os
import json
import time

from flask import Flask, render_template, request, jsonify
from ai_router import ai_router
from dotenv import load_dotenv
from memory_engine import schedule_memory_processing
from memory_retrieval import retrieve_relevant_memories
from conversation_retrieval import retrieve_relevant_conversations
from conversation_retrieval import format_conversations_for_prompt

from database import (
    init_database,
    create_conversation,
    add_message,
    get_messages,
    get_conversation,
    list_conversations,
    rename_conversation,
    delete_conversation,
    set_conversation_project,
    create_project,
    list_projects,
    get_project,
    update_project,
    delete_project,
    add_memory,
    get_memories,
    add_ignored_memory,
    is_memory_ignored,
    get_ignored_memories,
    delete_memory,
    update_memory,
    memory_exists,
    get_ai_status,
    save_ai_status,
    get_communication_style,
    update_communication_style,
)


# ============================================================
# Configuration
# ============================================================

load_dotenv()

app = Flask(__name__)

init_database()

MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "800"))

AI_STATUS = get_ai_status()


# ============================================================
# Legacy AI status handling
# ============================================================

def update_ai_rate_limit_status(error):
    """
    Store AI rate-limit information.

    This keeps compatibility with the existing ai_status database
    table. Provider-specific health information is handled by
    ai_router.
    """

    global AI_STATUS

    try:
        error_text = str(error)

        if "429" not in error_text:
            return

        AI_STATUS["available"] = False
        AI_STATUS["status"] = "Rate Limited"
        AI_STATUS["last_error"] = error_text

        # Try to extract reset timestamp if the provider exposes it.
        marker = "X-RateLimit-Reset"

        if marker in error_text:
            reset_part = error_text.split(marker, 1)[1]

            import re

            match = re.search(r"\d{10,}", reset_part)

            if match:
                reset_ms = int(match.group(0))
                AI_STATUS["reset_at"] = reset_ms / 1000

        # Extract request limit
        marker = "X-RateLimit-Limit"

        if marker in error_text:
            import re

            limit_part = error_text.split(marker, 1)[1]
            match = re.search(r"\d+", limit_part)

            if match:
                AI_STATUS["limit"] = int(match.group(0))

        # Extract remaining requests
        marker = "X-RateLimit-Remaining"

        if marker in error_text:
            import re

            remaining_part = error_text.split(marker, 1)[1]
            match = re.search(r"\d+", remaining_part)

            if match:
                AI_STATUS["remaining"] = int(match.group(0))

        save_ai_status(
            AI_STATUS["available"],
            AI_STATUS["status"],
            AI_STATUS["limit"],
            AI_STATUS["remaining"],
            AI_STATUS["reset_at"],
            AI_STATUS["last_error"]
        )

    except Exception as e:
        print("AI status tracking error:", e)


# ============================================================
# Memory extraction
# ============================================================

def extract_memory_candidates(messages):
    """
    Ask the AI to identify useful long-term memories
    from the conversation.
    """

    conversation_text = "\n".join(
        f"{message['role']}: {message['content']}"
        for message in messages
    )

    prompt = f"""
Analyze the following conversation and identify information
that would be useful to remember about the user in future conversations.

Only suggest information that is reasonably stable or useful long-term.

Good examples:
- Communication preferences
- Stable technical preferences
- Long-term projects
- Important decisions
- Recurring preferences
- Facts the user explicitly states about themselves

Do NOT suggest:
- Temporary thoughts
- One-time questions
- Things the AI said
- Information merely inferred about the user
- Sensitive personal information

Return ONLY valid JSON in this format:

{{
    "memories": [
        {{
            "content": "Short description of the memory",
            "type": "communication_preference",
            "confidence": 0.0
        }}
    ]
}}

If there are no useful memories, return:

{{
    "memories": []
}}

Conversation:

{conversation_text}
"""

    try:
        result = ai_router.chat(
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=500
        )

        content = result["content"]

        if not content:
            return []

        # Remove markdown code fences if the model adds them
        content = content.strip()

        if content.startswith("```"):
            content = content.replace("```json", "", 1)
            content = content.replace("```", "", 1)
            content = content.strip()

        # Convert JSON string into a Python object
        result_json = json.loads(content)

        candidates = result_json.get("memories", [])

        # Remove memories that have already been ignored
        filtered_candidates = []

        for candidate in candidates:
            candidate_content = candidate.get("content", "").strip()

            if not candidate_content:
                continue

            if is_memory_ignored(candidate_content):
                continue

            # Also don't suggest something that is already saved
            if memory_exists(candidate_content):
                continue

            filtered_candidates.append(candidate)

        return filtered_candidates

    except Exception as e:
        print("Memory extraction error:", e)
        return []


# ============================================================
# Communication style extraction
# ============================================================

def extract_communication_style(messages, current_profile):
    """Extract repeated communication preferences, never factual memories."""

    conversation_text = "\n".join(
        f"{message['role']}: {message['content']}"
        for message in messages
    )

    prompt = f"""
Analyze this conversation only for the user's communication preferences.
Return preferences about how the assistant should respond, not facts about
the user, their projects, identity, or life.

Only return a preference when it is explicitly stated or supported by at
least two conversational signals. Ignore one-off requests and preferences
that are uncertain. Existing preferences may be confirmed or changed only
when the conversation provides strong repeated evidence.

Use stable keys such as response_length, technical_detail, tone,
prefers_step_by_step, prefers_examples, and avoids_unnecessary_questions.

Return ONLY valid JSON:

{{
  "observations": [
    {{"key": "response_length", "value": "concise", "confidence": 0.9,
     "evidence_count": 2, "explicit": false}}
  ]
}}

Existing style profile:
{json.dumps(current_profile)}

Conversation:
{conversation_text}
"""

    try:
        result = ai_router.chat(
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=400
        )

        content = result["content"]

        if not content:
            return []

        content = content.strip()

        if content.startswith("```"):
            content = content.replace("```json", "", 1)
            content = content.replace("```", "", 1).strip()

        observations = json.loads(content).get("observations", [])

        valid = []

        for observation in observations:
            key = observation.get("key", "").strip()
            value = observation.get("value")
            confidence = float(observation.get("confidence", 0))
            evidence_count = int(observation.get("evidence_count", 0))
            explicit = bool(observation.get("explicit", False))

            if (
                key
                and value is not None
                and confidence >= 0.75
                and (explicit or evidence_count >= 2)
            ):
                valid.append({
                    "key": key,
                    "value": value
                })

        return valid

    except Exception as e:
        print("Communication style extraction error:", e)
        return []


# ============================================================
# Communication style learning
# ============================================================

def learn_communication_style(messages):
    """Update style only periodically, after enough user messages exist."""

    user_message_count = sum(
        message["role"] == "user"
        for message in messages
    )

    if user_message_count < 3 or user_message_count % 3 != 0:
        return

    style = get_communication_style()

    observations = extract_communication_style(
        messages,
        style["profile"]
    )

    if not observations:
        return

    profile = dict(style["profile"])

    for observation in observations:
        profile[observation["key"]] = observation["value"]

    update_communication_style(
        profile,
        style["observation_count"] + len(observations)
    )


# ============================================================
# Main page
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


# ============================================================
# Chat
# ============================================================

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}

    conversation_id = data.get("conversation_id")
    message = data.get("message", "").strip()
    options = data.get("options") or {}

    if not message:
        return jsonify({
            "error": "Message cannot be empty"
        }), 400

    # Create a conversation if one doesn't exist. A project_id may be
    # supplied to file the brand-new conversation under a project (e.g.
    # "New chat" was started from inside that project's view). It is
    # only honored here, at creation time - moving an existing
    # conversation between projects goes through
    # PATCH /api/conversations/<id> instead.
    if not conversation_id:
        requested_project_id = data.get("project_id")
        project_id = None

        if requested_project_id is not None:
            try:
                requested_project_id = int(requested_project_id)
            except (TypeError, ValueError):
                requested_project_id = None

            if (
                requested_project_id is not None
                and get_project(requested_project_id)
            ):
                project_id = requested_project_id

        conversation_id = create_conversation(project_id=project_id)

    elif not get_conversation(conversation_id):
        return jsonify({
            "error": "Conversation not found"
        }), 404

    try:
        # ----------------------------------------------------
        # Save user's message
        # ----------------------------------------------------

        add_message(
            conversation_id,
            "user",
            message
        )

        # ----------------------------------------------------
        # Get conversation history
        # ----------------------------------------------------

        messages = get_messages(conversation_id)

        # ----------------------------------------------------
        # Get approved long-term memories
        # ----------------------------------------------------

        memories = retrieve_relevant_memories(message)

        # ----------------------------------------------------
        # Retrieve relevant context from previous conversations
        # ----------------------------------------------------
        previous_conversations = retrieve_relevant_conversations(
            message,
            current_conversation_id=conversation_id,
            limit=3
        )

        conversation_text = format_conversations_for_prompt(
            previous_conversations
        )

        communication_style = get_communication_style()["profile"]

        # ----------------------------------------------------
        # Project context (if this conversation belongs to one)
        # ----------------------------------------------------

        conversation_data = get_conversation(conversation_id)
        project = None

        if conversation_data and conversation_data.get("project_id"):
            project = get_project(conversation_data["project_id"])

        project_text = ""

        if project and project.get("instructions"):
            project_text = (
                "This chat is part of the project \"{name}\". "
                "Follow these project-specific instructions:\n"
                "{instructions}"
            ).format(
                name=project["name"],
                instructions=project["instructions"]
            )

        # ----------------------------------------------------
        # Build memory context
        # ----------------------------------------------------

        memory_text = ""

        if memories:
            memory_text = "\n".join(
                f"- {memory['content']}"
                for memory in memories
            )

        # ----------------------------------------------------
        # Build communication style context
        # ----------------------------------------------------

        style_text = ""

        if communication_style:
            style_text = "\n".join(
                f"- {key.replace('_', ' ').capitalize()}: {value}"
                for key, value in communication_style.items()
            )

        # ----------------------------------------------------
        # System message
        # ----------------------------------------------------
        system_message = """
You are Noel's personal AI assistant.

You should be helpful, natural, and conversational.

{project_text}

The following information has been explicitly saved as
long-term memory about the user:

{memory_text}

Use these memories when they are relevant to the user's question.

The following are relevant excerpts from previous conversations.
They are historical context, not necessarily permanent facts.

{conversation_text}

Use previous conversation context when it is relevant.
Do not assume that every previous conversation is still current.
If previous context conflicts with something the user says now,
prefer the user's current statement.

The following communication style preferences were learned from repeated
conversation patterns. Use them when appropriate, but do not mention them
or treat them as factual memories:

{style_text}

Do not mention the memory system or say that you are retrieving
memories unless the user explicitly asks about it.

Do not invent additional facts about the user.
        """.format(
            project_text=project_text,
            memory_text=(
                memory_text
                if memory_text
                else "No saved memories yet."
            ),
            conversation_text=(
                conversation_text
                if conversation_text
                else "No relevant previous conversations found."
            ),
            style_text=(
                style_text
                if style_text
                else "No learned communication preferences yet."
            )
        )

        # ----------------------------------------------------
        # Send request through AI router
        # ----------------------------------------------------

        messages_for_ai = [
            {
                "role": "system",
                "content": system_message
            }
        ] + messages

        # A model of "auto" (or nothing) means: use the router's normal
        # priority order. Anything else is a provider name the user
        # explicitly picked in the UI, which the router will try first
        # and still fall back from automatically if it's unavailable.
        requested_model = options.get("model")
        preferred_provider = (
            str(requested_model).strip().lower()
            if requested_model and requested_model != "auto"
            else None
        )

        ai_result = ai_router.chat(
            messages=messages_for_ai,
            max_tokens=MAX_TOKENS,
            preferred_provider=preferred_provider
        )

        content = ai_result["content"]

        if content is None:
            return jsonify({
                "error": "The model returned no text response."
            }), 500

        # ----------------------------------------------------
        # Save AI response
        # ----------------------------------------------------

        add_message(
            conversation_id,
            "assistant",
            content,
            metadata={
                "provider": ai_result["provider"],
                "model": ai_result["model"],
                "latency_ms": ai_result["latency_ms"],
                "fallback_used": ai_result["fallback_used"],
                "attempts": ai_result["attempts"]
            }
        )

        # Automatically analyze the conversation for long-term memory.
        # This runs in the background and does not delay the response.
        schedule_memory_processing(conversation_id)

        # ----------------------------------------------------
        # Learn communication style
        # ----------------------------------------------------

        learn_communication_style(
            messages + [
                {
                    "role": "assistant",
                    "content": content
                }
            ]
        )

        # ----------------------------------------------------
        # Return response
        # ----------------------------------------------------

        return jsonify({
            "conversation_id": conversation_id,
            "response": content,

            # Project this conversation belongs to, if any
            "project_id": (
                conversation_data.get("project_id")
                if conversation_data
                else None
            ),
            "project_name": (
                conversation_data.get("project_name")
                if conversation_data
                else None
            ),

            # Router metadata
            "provider": ai_result["provider"],
            "model": ai_result["model"],
            "latency_ms": ai_result["latency_ms"],
            "fallback_used": ai_result["fallback_used"],
            "attempts": ai_result["attempts"]
        })

    except Exception as e:
        update_ai_rate_limit_status(e)

        return jsonify({
            "error": str(e)
        }), 500


# ============================================================
# Router status
# ============================================================

@app.route("/api/ai-router/status", methods=["GET"])
def ai_router_status():
    """
    Return the health/status of every configured AI provider.
    """

    return jsonify({
        "providers": ai_router.get_status()
    })


# ============================================================
# Conversation
# ============================================================

@app.route(
    "/api/conversation/<int:conversation_id>",
    methods=["GET"]
)
def conversation(conversation_id):

    conversation_data = get_conversation(conversation_id)

    if not conversation_data:
        return jsonify({
            "error": "Conversation not found"
        }), 404

    messages = get_messages(conversation_id)

    return jsonify({
        "conversation_id": conversation_id,
        "title": conversation_data["title"],
        "created_at": conversation_data["created_at"],
        "updated_at": conversation_data["updated_at"],
        "project_id": conversation_data.get("project_id"),
        "project_name": conversation_data.get("project_name"),
        "messages": messages
    })


@app.route("/api/conversations", methods=["GET"])
def conversations():

    search = request.args.get("search", "")

    project_id = request.args.get("project_id")
    if project_id is not None:
        try:
            project_id = int(project_id)
        except ValueError:
            project_id = None

    return jsonify({
        "conversations": list_conversations(
            search=search,
            project_id=project_id
        )
    })


@app.route(
    "/api/conversations/<int:conversation_id>",
    methods=["PATCH"]
)
def rename_conversation_route(conversation_id):

    if not get_conversation(conversation_id):
        return jsonify({
            "error": "Conversation not found"
        }), 404

    data = request.get_json() or {}
    updated_something = False

    # Renaming (unchanged behavior when only "title" is sent).
    if "title" in data:
        title = " ".join(
            str(data.get("title", "")).split()
        ).strip()

        if not title:
            return jsonify({
                "error": "Conversation title cannot be empty"
            }), 400

        if len(title) > 80:
            return jsonify({
                "error": "Conversation title cannot exceed 80 characters"
            }), 400

        rename_conversation(conversation_id, title)
        updated_something = True

    # Moving into/out of a project. project_id: null unfiles the chat.
    if "project_id" in data:
        project_id = data.get("project_id")

        if project_id is not None:
            try:
                project_id = int(project_id)
            except (TypeError, ValueError):
                return jsonify({
                    "error": "project_id must be a number or null"
                }), 400

            if not get_project(project_id):
                return jsonify({
                    "error": "Project not found"
                }), 404

        set_conversation_project(conversation_id, project_id)
        updated_something = True

    if not updated_something:
        return jsonify({
            "error": "Nothing to update"
        }), 400

    return jsonify({
        "conversation": get_conversation(conversation_id)
    })


@app.route(
    "/api/conversations/<int:conversation_id>",
    methods=["DELETE"]
)
def delete_conversation_route(conversation_id):

    if not delete_conversation(conversation_id):
        return jsonify({
            "error": "Conversation not found"
        }), 404

    return jsonify({
        "success": True,
        "conversation_id": conversation_id
    })


# ============================================================
# Projects
# ============================================================

def _validate_project_name(name):
    name = " ".join(str(name or "").split()).strip()

    if not name:
        return None, ("Project name cannot be empty", 400)

    if len(name) > 80:
        return None, ("Project name cannot exceed 80 characters", 400)

    return name, None


@app.route("/api/projects", methods=["GET"])
def projects_route():
    return jsonify({
        "projects": list_projects()
    })


@app.route("/api/projects", methods=["POST"])
def create_project_route():
    data = request.get_json() or {}

    name, error = _validate_project_name(data.get("name"))
    if error:
        message, status = error
        return jsonify({"error": message}), status

    description = str(data.get("description", "")).strip()
    instructions = str(data.get("instructions", "")).strip()

    project_id = create_project(name, description, instructions)

    return jsonify({
        "project": get_project(project_id)
    })


@app.route(
    "/api/projects/<int:project_id>",
    methods=["GET"]
)
def project_detail_route(project_id):
    project = get_project(project_id)

    if not project:
        return jsonify({
            "error": "Project not found"
        }), 404

    return jsonify({
        "project": project,
        "conversations": list_conversations(project_id=project_id)
    })


@app.route(
    "/api/projects/<int:project_id>",
    methods=["PATCH"]
)
def update_project_route(project_id):
    if not get_project(project_id):
        return jsonify({
            "error": "Project not found"
        }), 404

    data = request.get_json() or {}

    name = None
    if "name" in data:
        name, error = _validate_project_name(data.get("name"))
        if error:
            message, status = error
            return jsonify({"error": message}), status

    description = (
        str(data.get("description", "")).strip()
        if "description" in data
        else None
    )
    instructions = (
        str(data.get("instructions", "")).strip()
        if "instructions" in data
        else None
    )

    update_project(
        project_id,
        name=name,
        description=description,
        instructions=instructions
    )

    return jsonify({
        "project": get_project(project_id)
    })


@app.route(
    "/api/projects/<int:project_id>",
    methods=["DELETE"]
)
def delete_project_route(project_id):
    if not delete_project(project_id):
        return jsonify({
            "error": "Project not found"
        }), 404

    return jsonify({
        "success": True,
        "project_id": project_id
    })


# ============================================================
# Memories
# ============================================================

@app.route("/api/memories", methods=["POST"])
def save_memory():

    data = request.get_json() or {}

    content = data.get("content", "").strip()
    memory_type = data.get("type", "general")
    confidence = float(data.get("confidence", 0.5))

    if not content:
        return jsonify({
            "error": "Memory content cannot be empty"
        }), 400

    # Prevent exact duplicate memories
    existing_id = memory_exists(content)

    if existing_id:
        return jsonify({
            "success": False,
            "duplicate": True,
            "id": existing_id,
            "message": "This memory already exists."
        })

    memory_id = add_memory(
        content,
        memory_type,
        confidence
    )

    return jsonify({
        "success": True,
        "id": memory_id
    })


@app.route("/api/memories", methods=["GET"])
def get_all_memories():

    return jsonify({
        "memories": get_memories()
    })


@app.route("/memories")
def memory_page():

    return render_template("memories.html")


# ============================================================
# Communication style
# ============================================================

@app.route("/api/communication-style", methods=["GET"])
def communication_style():

    return jsonify(
        get_communication_style()
    )


@app.route("/api/communication-style", methods=["PUT"])
def save_communication_style():

    data = request.get_json() or {}

    profile = data.get("profile")

    if not isinstance(profile, dict):
        return jsonify({
            "error": "Style profile must be an object."
        }), 400

    cleaned_profile = {
        str(key).strip(): value
        for key, value in profile.items()
        if (
            str(key).strip()
            and value is not None
            and value != ""
        )
    }

    current = get_communication_style()

    update_communication_style(
        cleaned_profile,
        current["observation_count"]
    )

    return jsonify(
        get_communication_style()
    )


@app.route("/api/communication-style", methods=["DELETE"])
def reset_communication_style():

    update_communication_style({}, 0)

    return jsonify(
        get_communication_style()
    )


# ============================================================
# Memory candidates
# ============================================================

@app.route(
    "/api/memory-candidates/<int:conversation_id>",
    methods=["GET"]
)
def memory_candidates(conversation_id):

    messages = get_messages(conversation_id)

    if not messages:
        return jsonify({
            "error": "Conversation not found"
        }), 404

    candidates = extract_memory_candidates(messages)

    return jsonify({
        "candidates": candidates
    })


# ============================================================
# Memory management
# ============================================================

@app.route(
    "/api/memories/<int:memory_id>",
    methods=["DELETE"]
)
def delete_memory_route(memory_id):

    delete_memory(memory_id)

    return jsonify({
        "success": True
    })


@app.route(
    "/api/memories/<int:memory_id>",
    methods=["PUT"]
)
def update_memory_route(memory_id):

    data = request.get_json() or {}

    content = data.get("content", "").strip()
    memory_type = data.get("type", "general")
    confidence = float(data.get("confidence", 0.5))

    if not content:
        return jsonify({
            "error": "Memory content cannot be empty"
        }), 400

    update_memory(
        memory_id,
        content,
        memory_type,
        confidence
    )

    return jsonify({
        "success": True
    })


@app.route("/api/memories/ignore", methods=["POST"])
def ignore_memory():

    data = request.get_json() or {}

    content = data.get("content", "").strip()

    if not content:
        return jsonify({
            "error": "Memory content cannot be empty"
        }), 400

    add_ignored_memory(content)

    return jsonify({
        "success": True
    })


# ============================================================
# Legacy AI status
# ============================================================

@app.route("/api/ai-status", methods=["GET"])
def ai_status():

    # Automatically become available again after reset.
    if (
        not AI_STATUS["available"]
        and AI_STATUS["reset_at"]
        and time.time() >= AI_STATUS["reset_at"]
    ):
        AI_STATUS["available"] = True
        AI_STATUS["status"] = "Available"
        AI_STATUS["last_error"] = None
        AI_STATUS["remaining"] = None

    save_ai_status(
        AI_STATUS["available"],
        AI_STATUS["status"],
        AI_STATUS["limit"],
        AI_STATUS["remaining"],
        AI_STATUS["reset_at"],
        AI_STATUS["last_error"]
    )

    return jsonify(AI_STATUS)

@app.route("/api/chat/agent", methods=["POST"])
def chat_agent():
    data = request.get_json() or {}
    session_id = data.get("session_id")
    messages = data.get("messages", [])
    tools = data.get("tools", [])

    if not messages:
        return jsonify({"error": "messages required"}), 400

    # Resolve session_id to a real integer conversation_id.
    # noel-code sends a random string tag on first call — treat that
    # as "no conversation yet" and create one.
    conversation_id = None

    if session_id is not None:
        try:
            candidate = int(session_id)
            if get_conversation(candidate):
                conversation_id = candidate
        except (TypeError, ValueError):
            pass  # string tag like "a3f9b2c1" — create a new conversation

    if conversation_id is None:
        conversation_id = create_conversation()  # returns int

    # Save user messages to history
    for msg in messages:
        if msg.get("role") == "user":
            add_message(conversation_id, "user", msg.get("content", ""))
            break  # only save the latest user turn

    system_message = {
        "role": "system",
        "content": "You are Noel's coding assistant. Use the provided tools to read, write, and execute code. Think step by step."
    }

    messages_for_ai = [system_message] + messages

    try:
        result = ai_router.chat(
            messages=messages_for_ai,
            max_tokens=2000,
            tools=tools if tools else None
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    content = result.get("content") or ""
    tool_calls = result.get("tool_calls")

    # Only save to DB if there's actual content (tool-call-only turns have no content)
    if content:
        add_message(conversation_id, "assistant", content, metadata={
            "provider": result["provider"],
            "tool_calls": tool_calls
        })

    return jsonify({
        "session_id": str(conversation_id),  # real DB id, noel-code persists this
        "content": content,
        "tool_calls": tool_calls,
        "provider": result["provider"],
        "model": result["model"]
    })

# ============================================================
# Application entry point
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8081,
        debug=False
    )
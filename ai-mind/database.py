import json
import sqlite3
import hashlib
import re

DATABASE = "ai_mind.db"


def get_connection():
    connection = sqlite3.connect(DATABASE, timeout=10) 
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode=WAL") 
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _column_exists(connection, table, column):
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def init_database():
    connection = get_connection()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT 'New conversation',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (conversation_id)
                REFERENCES conversations(id)
                ON DELETE CASCADE
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            instructions TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            memory_type TEXT NOT NULL DEFAULT 'general',
            confidence REAL NOT NULL DEFAULT 0.5,
            importance REAL NOT NULL DEFAULT 0.5,
            content_hash TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            source_conversation_id INTEGER,
            source_message_id INTEGER,
            valid_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            valid_until TIMESTAMP,
            superseded_by INTEGER,
            last_accessed_at TIMESTAMP,
            access_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS ignored_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS ai_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            available INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'Available',
            rate_limit INTEGER,
            remaining INTEGER,
            reset_at REAL,
            last_error TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS communication_style (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            profile TEXT NOT NULL DEFAULT '{}',
            observation_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS ai_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            request_kind TEXT NOT NULL DEFAULT 'foreground',
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            latency_ms REAL,
            status TEXT NOT NULL,
            failure_category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            conversation_id INTEGER PRIMARY KEY,
            summary TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id)
                REFERENCES conversations(id)
                ON DELETE CASCADE
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS prompt_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            source_name TEXT,
            source_url TEXT,
            rationale TEXT,
            is_curated INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migration: add metadata column for pre-existing databases created
    # before this column was introduced. Safe to run on every startup.
    if not _column_exists(connection, "messages", "metadata"):
        connection.execute("ALTER TABLE messages ADD COLUMN metadata TEXT")

    # Migration: add project_id column for pre-existing databases created
    # before Projects existed. Conversations keep their history if a project
    # is deleted; they just fall back to being unfiled (ON DELETE SET NULL).
    if not _column_exists(connection, "conversations", "project_id"):
        connection.execute(
            "ALTER TABLE conversations ADD COLUMN project_id "
            "INTEGER REFERENCES projects(id) ON DELETE SET NULL"
        )

    memory_columns = {
        "importance": "REAL NOT NULL DEFAULT 0.5",
        "content_hash": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "source_conversation_id": "INTEGER",
        "source_message_id": "INTEGER",
        "valid_from": "TIMESTAMP",
        "valid_until": "TIMESTAMP",
        "superseded_by": "INTEGER",
        "last_accessed_at": "TIMESTAMP",
        "access_count": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in memory_columns.items():
        if not _column_exists(connection, "memories", column):
            connection.execute(
                f"ALTER TABLE memories ADD COLUMN {column} {definition}"
            )

    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_prompt_library_category "
        "ON prompt_library(category)"
    )

    curated_prompts = [
        (
            "structured-task",
            "Structured task brief",
            "Turn an ambiguous request into a clear, executable brief.",
            "general",
            """<task>
Describe the task you want completed.
</task>
<context>
Add the relevant background, constraints, and existing work.
</context>
<requirements>
List must-have outcomes, exclusions, and quality criteria.
</requirements>
<output>
Return: (1) assumptions, (2) a concise plan, (3) the first concrete step.
</output>""",
            "OpenAI Prompt Engineering",
            "https://developers.openai.com/api/docs/guides/prompt-engineering",
            "Separates the objective, context, constraints, and output contract so the model has less ambiguity and produces an actionable response.",
        ),
        (
            "code-review",
            "Evidence-based code review",
            "Review code with prioritized, actionable findings.",
            "coding",
            """Review the code below.
Focus only on correctness, security, maintainability, and performance risks.
For each finding, provide:
- severity: critical, high, medium, or low
- exact location
- why it matters
- a minimal fix
If you find no issue, say so explicitly. Do not invent problems.

<code>
Paste code here.
</code>""",
            "Google Gemini Prompt Design Strategies",
            "https://ai.google.dev/gemini-api/docs/prompting-strategies",
            "Defines evaluation criteria and an explicit output shape, which improves consistency and discourages speculative findings.",
        ),
        (
            "debug",
            "Reproducible debugging",
            "Turn an error report into a focused diagnosis and test plan.",
            "coding",
            """Help debug this issue.

<error>
Paste the exact error and stack trace.
</error>
<environment>
Include runtime, versions, platform, and recent changes.
</environment>
<expected>
What should happen?
</expected>
<actual>
What happens instead?
</actual>

Reason from the evidence. Return:
1. most likely cause and confidence
2. two alternative hypotheses
3. smallest diagnostic test
4. minimal fix
5. regression test to add""",
            "Anthropic Prompting Best Practices",
            "https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/overview",
            "Uses clear delimiters and asks for evidence, hypotheses, and verification rather than jumping straight to an unsupported fix.",
        ),
        (
            "summarize-decisions",
            "Decision-focused summary",
            "Compress long notes without losing decisions or open work.",
            "writing",
            """Summarize the material below for someone who must continue the work.
Preserve only:
- goals and constraints
- decisions and their reasons
- unresolved questions
- risks and dependencies
- next actions with owners, if present
Separate confirmed facts from assumptions. Do not invent missing details.
Use concise headings and bullets.

<material>
Paste notes, transcript, or document here.
</material>""",
            "Anthropic: Prompting long context",
            "https://www.anthropic.com/news/prompting-long-context",
            "Prioritizes relevant excerpts and durable information, which reduces context noise while preserving the information needed for follow-up work.",
        ),
        (
            "rewrite",
            "Audience-aware rewrite",
            "Rewrite content while preserving intent and controlling tone.",
            "writing",
            """Rewrite the text below for <audience>describe the audience</audience>.
Goal: <goal>describe the desired outcome</goal>
Tone: <tone>professional, warm, direct, or other</tone>
Constraints: preserve factual claims, names, links, and required terminology.
Return only the revised text, followed by a short note listing any ambiguity
that prevented an exact rewrite.

<text>
Paste text here.
</text>""",
            "OpenAI Prompt Engineering",
            "https://developers.openai.com/api/docs/guides/prompt-engineering",
            "Makes audience, goal, tone, constraints, and output format explicit instead of relying on vague style instructions.",
        ),
        (
            "logic",
            "Stepwise logic check",
            "Analyze a difficult question without skipping assumptions or edge cases.",
            "logic",
            """Analyze this problem carefully.
First identify the known facts, unknowns, constraints, and definitions.
Then work through the solution in explicit, verifiable steps.
Check the result against the original requirements and list any uncertainty.
Do not invent missing information.

<problem>
Describe the problem here.
</problem>

Return:
1. assumptions
2. reasoning summary
3. conclusion
4. edge cases or confidence limits""",
            "Chain-of-Thought Prompting",
            "https://arxiv.org/abs/2201.11903",
            "Decomposes a complex task into intermediate, checkable steps and asks for a conclusion plus uncertainty instead of an unsupported answer.",
        ),
        (
            "creative-ideation",
            "Divergent creative ideation",
            "Generate varied ideas before selecting the strongest direction.",
            "creativity",
            """Generate creative directions for the brief below.
Create 8 genuinely different concepts, not minor variations.
For each, include: name, central idea, audience value, and one risk.
Then compare the concepts against the stated constraints and recommend
the best two with a short reason. Avoid clichés and explain unusual choices.

<brief>
Describe the creative challenge, audience, tone, and constraints.
</brief>""",
            "Tree of Thoughts",
            "https://arxiv.org/abs/2305.10601",
            "Separates exploration from evaluation: multiple candidate directions are generated first, then assessed against explicit constraints.",
        ),
        (
            "design-brief",
            "Human-centered design brief",
            "Turn a vague product idea into a usable design direction.",
            "design",
            """Create a design brief from the information below.
Cover: target user, core problem, desired behavior, content hierarchy,
interaction states, accessibility needs, visual direction, and success metrics.
Call out assumptions and ask no more than three high-impact questions.
Prefer simple, calm, usable solutions over decorative complexity.

<idea>
Describe the product, feature, or interface.
</idea>""",
            "Prompt Design and Engineering",
            "https://arxiv.org/abs/2401.14423",
            "Uses a role- and outcome-oriented structure to translate an open-ended request into requirements, constraints, and measurable success criteria.",
        ),
        (
            "concise-answer",
            "Concise answer",
            "Get the shortest useful answer without losing essential caveats.",
            "concise",
            """Answer the question below in the minimum length needed to be
correct and useful. Start with the direct answer in one sentence, then add
at most three bullets for essential detail. Include a caveat only if omitting
it would make the answer misleading. Do not repeat the question.

<question>
Ask your question here.
</question>""",
            "Systematic Survey of Prompt Engineering",
            "https://arxiv.org/abs/2402.07927",
            "Sets a measurable length and output contract while preserving correctness and caveats, reducing unnecessary context and verbosity.",
        ),
        (
            "research-synthesis",
            "Research synthesis",
            "Compare sources and separate evidence from interpretation.",
            "research",
            """Synthesize the sources below for the stated question.
For each major claim, distinguish direct evidence, reasonable inference,
and unresolved uncertainty. Compare disagreements rather than hiding them.
End with a concise conclusion and the most useful follow-up question.
Do not cite a source for a claim it does not support.

<question>
State the research question.
</question>
<sources>
Paste source excerpts or links here.
</sources>""",
            "ReAct: Synergizing Reasoning and Acting",
            "https://arxiv.org/abs/2210.03629",
            "Separates evidence gathering, interpretation, and conclusion, reducing unsupported synthesis and making uncertainty visible.",
        ),
    ]
    connection.executemany(
        """
        INSERT OR IGNORE INTO prompt_library (
            slug, title, description, category, prompt,
            source_name, source_url, rationale, is_curated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        curated_prompts,
    )
    connection.execute(
        """
        UPDATE prompt_library
        SET category = CASE slug
            WHEN 'structured-task' THEN 'planning'
            WHEN 'code-review' THEN 'programming'
            WHEN 'debug' THEN 'programming'
            WHEN 'summarize-decisions' THEN 'concise'
            WHEN 'rewrite' THEN 'writing'
            ELSE category
        END
        WHERE is_curated = 1
        """
    )

    connection.commit()
    connection.close()


def list_prompt_library():
    connection = get_connection()
    rows = connection.execute(
        """
        SELECT id, slug, title, description, prompt, category,
               source_name, source_url, rationale, is_curated,
               created_at, updated_at
        FROM prompt_library
        ORDER BY is_curated DESC, category ASC, title ASC, id ASC
        """
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


def create_prompt(
    title,
    prompt,
    description="",
    category="general",
    source_name=None,
    source_url=None,
    rationale=None,
):
    connection = get_connection()
    cursor = connection.execute(
        """
        INSERT INTO prompt_library (
            title, description, prompt, category,
            source_name, source_url, rationale, is_curated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            title.strip(),
            description.strip(),
            prompt.strip(),
            category.strip() or "general",
            source_name.strip() if source_name else None,
            source_url.strip() if source_url else None,
            rationale.strip() if rationale else None,
        ),
    )
    connection.commit()
    prompt_id = cursor.lastrowid
    connection.close()
    return prompt_id


def delete_prompt(prompt_id):
    connection = get_connection()
    cursor = connection.execute(
        "DELETE FROM prompt_library WHERE id = ? AND is_curated = 0",
        (prompt_id,),
    )
    connection.commit()
    connection.close()
    return cursor.rowcount > 0


def record_ai_usage(
    provider,
    model,
    request_kind,
    prompt_tokens,
    completion_tokens,
    total_tokens,
    latency_ms,
    status,
    failure_category=None,
):
    connection = get_connection()
    connection.execute(
        """
        INSERT INTO ai_usage (
            provider, model, request_kind, prompt_tokens,
            completion_tokens, total_tokens, latency_ms,
            status, failure_category
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            provider,
            model,
            request_kind,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            latency_ms,
            status,
            failure_category,
        ),
    )
    connection.commit()
    connection.close()


def get_recent_provider_failures(provider, model, minutes=15):
    connection = get_connection()
    rows = connection.execute(
        """
        SELECT failure_category, COUNT(*) AS count
        FROM ai_usage
        WHERE provider = ?
          AND model = ?
          AND status LIKE 'failure%'
          AND created_at >= datetime('now', ?)
        GROUP BY failure_category
        """,
        (provider, model, f"-{int(minutes)} minutes"),
    ).fetchall()
    connection.close()
    return {row["failure_category"]: row["count"] for row in rows}


def create_conversation(title="New conversation", project_id=None):
    connection = get_connection()

    cursor = connection.execute(
        """
        INSERT INTO conversations (title, project_id)
        VALUES (?, ?)
        """,
        (title, project_id)
    )

    conversation_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return conversation_id


def add_message(conversation_id, role, content, metadata=None):
    connection = get_connection()

    metadata_json = json.dumps(metadata) if metadata is not None else None

    connection.execute(
        """
        INSERT INTO messages (
            conversation_id,
            role,
            content,
            metadata
        )
        VALUES (?, ?, ?, ?)
        """,
        (conversation_id, role, content, metadata_json)
    )

    connection.execute(
        """
        UPDATE conversations
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (conversation_id,)
    )

    connection.commit()
    connection.close()


def get_messages(conversation_id):
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT role, content, metadata
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id ASC
        """,
        (conversation_id,)
    ).fetchall()

    connection.close()

    return [
        {
            "role": row["role"],
            "content": row["content"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else None
        }
        for row in rows
    ]


def get_conversation_summary(conversation_id):
    connection = get_connection()
    row = connection.execute(
        """
        SELECT summary, message_count, updated_at
        FROM conversation_summaries
        WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    connection.close()
    return dict(row) if row else None


def save_conversation_summary(conversation_id, summary, message_count):
    connection = get_connection()
    connection.execute(
        """
        INSERT INTO conversation_summaries (
            conversation_id, summary, message_count, updated_at
        )
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(conversation_id) DO UPDATE SET
            summary = excluded.summary,
            message_count = excluded.message_count,
            updated_at = CURRENT_TIMESTAMP
        """,
        (conversation_id, summary, message_count),
    )
    connection.commit()
    connection.close()


def list_conversations(search="", limit=100, project_id=None):
    """
    List conversations, most recently updated first.

    When project_id is given, only conversations belonging to that
    project are returned. Otherwise every conversation is returned
    (unfiled and project-owned alike), each tagged with its project_id
    / project_name so callers can render a project badge without a
    second lookup.
    """

    search_term = f"%{search.strip()}%"
    connection = get_connection()

    params = [search.strip(), search_term, search_term]
    project_filter_sql = ""

    if project_id is not None:
        project_filter_sql = "AND c.project_id = ?"
        params.append(project_id)

    params.append(limit)

    rows = connection.execute(
        f"""
        SELECT
            c.id,
            CASE
                WHEN c.title = 'New conversation' THEN COALESCE(
                    (
                        SELECT SUBSTR(TRIM(content), 1, 80)
                        FROM messages first_title_message
                        WHERE first_title_message.conversation_id = c.id
                          AND first_title_message.role = 'user'
                        ORDER BY first_title_message.id ASC
                        LIMIT 1
                    ),
                    c.title
                )
                ELSE c.title
            END AS title,
            c.created_at,
            c.updated_at,
            c.project_id AS project_id,
            p.name AS project_name,
            COUNT(m.id) AS message_count,
            COALESCE(
                (
                    SELECT content
                    FROM messages first_message
                    WHERE first_message.conversation_id = c.id
                      AND first_message.role = 'user'
                    ORDER BY first_message.id ASC
                    LIMIT 1
                ),
                ''
            ) AS preview
        FROM conversations c
        LEFT JOIN messages m ON m.conversation_id = c.id
        LEFT JOIN projects p ON p.id = c.project_id
        WHERE (? = '' OR c.title LIKE ? OR EXISTS (
            SELECT 1
            FROM messages matching_message
            WHERE matching_message.conversation_id = c.id
              AND matching_message.content LIKE ?
        ))
        {project_filter_sql}
        GROUP BY c.id
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT ?
        """,
        params
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


def get_conversation(conversation_id):
    connection = get_connection()
    row = connection.execute(
        """
        SELECT
            conversations.id,
            CASE
                WHEN title = 'New conversation' THEN COALESCE(
                    (
                        SELECT SUBSTR(TRIM(content), 1, 80)
                        FROM messages first_title_message
                        WHERE first_title_message.conversation_id = conversations.id
                          AND first_title_message.role = 'user'
                        ORDER BY first_title_message.id ASC
                        LIMIT 1
                    ),
                    title
                )
                ELSE title
            END AS title,
            created_at,
            updated_at,
            project_id,
            (
                SELECT name FROM projects
                WHERE projects.id = conversations.project_id
            ) AS project_name
        FROM conversations
        WHERE id = ?
        """,
        (conversation_id,)
    ).fetchone()
    connection.close()
    return dict(row) if row else None


def get_conversation_raw_title(conversation_id):
    connection = get_connection()
    row = connection.execute(
        "SELECT title FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    connection.close()
    return row["title"] if row else None


def set_conversation_project(conversation_id, project_id):
    """Attach (or detach, when project_id is None) a conversation to a project."""
    connection = get_connection()
    cursor = connection.execute(
        """
        UPDATE conversations
        SET project_id = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (project_id, conversation_id)
    )
    connection.commit()
    connection.close()
    return cursor.rowcount > 0


def rename_conversation(conversation_id, title):
    connection = get_connection()
    cursor = connection.execute(
        """
        UPDATE conversations
        SET title = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (title, conversation_id)
    )
    connection.commit()
    connection.close()
    return cursor.rowcount > 0


def delete_conversation(conversation_id):
    connection = get_connection()
    cursor = connection.execute(
        "DELETE FROM conversations WHERE id = ?",
        (conversation_id,)
    )
    connection.commit()
    connection.close()
    return cursor.rowcount > 0


# ============================================================
# Projects
# ============================================================

def create_project(name, description="", instructions=""):
    connection = get_connection()

    cursor = connection.execute(
        """
        INSERT INTO projects (name, description, instructions)
        VALUES (?, ?, ?)
        """,
        (name, description, instructions)
    )

    project_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return project_id


def list_projects():
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT
            p.id,
            p.name,
            p.description,
            p.instructions,
            p.created_at,
            p.updated_at,
            COUNT(c.id) AS conversation_count
        FROM projects p
        LEFT JOIN conversations c ON c.project_id = p.id
        GROUP BY p.id
        ORDER BY p.updated_at DESC, p.id DESC
        """
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


def get_project(project_id):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT id, name, description, instructions, created_at, updated_at
        FROM projects
        WHERE id = ?
        """,
        (project_id,)
    ).fetchone()

    connection.close()

    return dict(row) if row else None


def update_project(project_id, name=None, description=None, instructions=None):
    """
    Update only the fields that are not None, so callers can send a
    partial payload (e.g. just new instructions) without clobbering
    the rest.
    """

    connection = get_connection()

    current = connection.execute(
        "SELECT name, description, instructions FROM projects WHERE id = ?",
        (project_id,)
    ).fetchone()

    if not current:
        connection.close()
        return False

    connection.execute(
        """
        UPDATE projects
        SET name = ?,
            description = ?,
            instructions = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            name if name is not None else current["name"],
            description if description is not None else current["description"],
            instructions if instructions is not None else current["instructions"],
            project_id
        )
    )

    connection.commit()
    connection.close()

    return True


def delete_project(project_id):
    """
    Delete a project. Conversations that belonged to it are kept and
    simply become unfiled (project_id is set to NULL by the
    ON DELETE SET NULL foreign key on conversations.project_id).
    """
    connection = get_connection()
    cursor = connection.execute(
        "DELETE FROM projects WHERE id = ?",
        (project_id,)
    )
    connection.commit()
    connection.close()
    return cursor.rowcount > 0


def _memory_content_hash(content):
    normalized = re.sub(r"\s+", " ", str(content).strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def add_memory(
    content,
    memory_type="general",
    confidence=0.5,
    importance=0.5,
    source_conversation_id=None,
    source_message_id=None,
):
    connection = get_connection()

    cursor = connection.execute(
        """
        INSERT INTO memories (
            content,
            memory_type,
            confidence,
            importance,
            content_hash,
            source_conversation_id,
            source_message_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            content,
            memory_type,
            confidence,
            importance,
            _memory_content_hash(content),
            source_conversation_id,
            source_message_id,
        )
    )

    memory_id = cursor.lastrowid

    connection.commit()
    connection.close()

    return memory_id


def get_memories():
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT id, content, memory_type, confidence, importance,
               status, source_conversation_id, source_message_id,
               valid_from, valid_until, superseded_by,
               last_accessed_at, access_count
        FROM memories
        WHERE status = 'active'
        ORDER BY updated_at DESC
        """
    ).fetchall()

    connection.close()

    return [
        {
            "id": row["id"],
            "content": row["content"],
            "memory_type": row["memory_type"],
            "confidence": row["confidence"]
            ,"importance": row["importance"]
            ,"status": row["status"]
            ,"source_conversation_id": row["source_conversation_id"]
            ,"source_message_id": row["source_message_id"]
            ,"valid_from": row["valid_from"]
            ,"valid_until": row["valid_until"]
            ,"superseded_by": row["superseded_by"]
            ,"last_accessed_at": row["last_accessed_at"]
            ,"access_count": row["access_count"]
        }
        for row in rows
    ]

def delete_memory(memory_id):
    connection = get_connection()

    connection.execute(
        """
        DELETE FROM memories
        WHERE id = ?
        """,
        (memory_id,)
    )

    connection.commit()
    connection.close()

def update_memory(
    memory_id,
    content,
    memory_type,
    confidence,
    importance=0.5,
    source_conversation_id=None,
    source_message_id=None,
):
    connection = get_connection()

    connection.execute(
        """
        UPDATE memories
        SET content = ?,
            memory_type = ?,
            confidence = ?,
            importance = ?,
            content_hash = ?,
            source_conversation_id = COALESCE(?, source_conversation_id),
            source_message_id = COALESCE(?, source_message_id),
            status = 'active',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            content,
            memory_type,
            confidence,
            importance,
            _memory_content_hash(content),
            source_conversation_id,
            source_message_id,
            memory_id,
        )
    )

    connection.commit()
    connection.close()


def replace_memory(
    memory_id,
    content,
    memory_type,
    confidence,
    importance=0.5,
    source_conversation_id=None,
    source_message_id=None,
):
    """Create a new version while retaining the previous fact for audit."""
    connection = get_connection()
    cursor = connection.execute(
        """
        INSERT INTO memories (
            content, memory_type, confidence, importance, content_hash,
            source_conversation_id, source_message_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            content,
            memory_type,
            confidence,
            importance,
            _memory_content_hash(content),
            source_conversation_id,
            source_message_id,
        ),
    )
    new_id = cursor.lastrowid
    connection.execute(
        """
        UPDATE memories
        SET status = 'superseded',
            valid_until = CURRENT_TIMESTAMP,
            superseded_by = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'active'
        """,
        (new_id, memory_id),
    )
    connection.commit()
    connection.close()
    return new_id


def memory_exists(content):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT id
        FROM memories
        WHERE status = 'active'
          AND (
            content_hash = ?
            OR LOWER(TRIM(content)) = LOWER(TRIM(?))
          )
        LIMIT 1
        """,
        (_memory_content_hash(content), content)
    ).fetchone()

    connection.close()

    return row["id"] if row else None


def mark_memories_accessed(memory_ids):
    ids = sorted({int(memory_id) for memory_id in memory_ids})
    if not ids:
        return
    connection = get_connection()
    placeholders = ",".join("?" for _ in ids)
    connection.execute(
        f"""
        UPDATE memories
        SET last_accessed_at = CURRENT_TIMESTAMP,
            access_count = access_count + 1
        WHERE id IN ({placeholders}) AND status = 'active'
        """,
        ids,
    )
    connection.commit()
    connection.close()

def add_ignored_memory(content):
    connection = get_connection()

    connection.execute(
        """
        INSERT OR IGNORE INTO ignored_memories (content)
        VALUES (?)
        """,
        (content.strip(),)
    )

    connection.commit()
    connection.close()


def is_memory_ignored(content):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT id
        FROM ignored_memories
        WHERE LOWER(TRIM(content)) = LOWER(TRIM(?))
        LIMIT 1
        """,
        (content,)
    ).fetchone()

    connection.close()

    return row is not None


def get_ignored_memories():
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT content
        FROM ignored_memories
        ORDER BY created_at DESC
        """
    ).fetchall()

    connection.close()

    return [
        row["content"]
        for row in rows
    ]

def get_ai_status():
    connection = get_connection()

    row = connection.execute(
        """
        SELECT
            available,
            status,
            rate_limit,
            remaining,
            reset_at,
            last_error
        FROM ai_status
        WHERE id = 1
        """
    ).fetchone()

    connection.close()

    if not row:
        return {
            "available": True,
            "status": "Available",
            "limit": None,
            "remaining": None,
            "reset_at": None,
            "last_error": None
        }

    return {
        "available": bool(row["available"]),
        "status": row["status"],
        "limit": row["rate_limit"],
        "remaining": row["remaining"],
        "reset_at": row["reset_at"],
        "last_error": row["last_error"]
    }


def save_ai_status(
    available,
    status,
    limit,
    remaining,
    reset_at,
    last_error
):
    connection = get_connection()

    connection.execute(
        """
        INSERT INTO ai_status (
            id,
            available,
            status,
            rate_limit,
            remaining,
            reset_at,
            last_error,
            updated_at
        )
        VALUES (1, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)

        ON CONFLICT(id) DO UPDATE SET
            available = excluded.available,
            status = excluded.status,
            rate_limit = excluded.rate_limit,
            remaining = excluded.remaining,
            reset_at = excluded.reset_at,
            last_error = excluded.last_error,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            int(available),
            status,
            limit,
            remaining,
            reset_at,
            last_error
        )
    )

    connection.commit()
    connection.close()


def get_communication_style():
    connection = get_connection()

    row = connection.execute(
        """
        SELECT profile, observation_count, updated_at
        FROM communication_style
        WHERE id = 1
        """
    ).fetchone()

    connection.close()

    if not row:
        return {
            "profile": {},
            "observation_count": 0,
            "updated_at": None
        }

    try:
        profile = json.loads(row["profile"])
    except (TypeError, json.JSONDecodeError):
        profile = {}

    return {
        "profile": profile if isinstance(profile, dict) else {},
        "observation_count": row["observation_count"],
        "updated_at": row["updated_at"]
    }


def update_communication_style(profile, observation_count=None):
    connection = get_connection()

    if observation_count is None:
        existing = connection.execute(
            "SELECT observation_count FROM communication_style WHERE id = 1"
        ).fetchone()
        observation_count = existing["observation_count"] if existing else 0

    connection.execute(
        """
        INSERT INTO communication_style (id, profile, observation_count, updated_at)
        VALUES (1, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            profile = excluded.profile,
            observation_count = excluded.observation_count,
            updated_at = CURRENT_TIMESTAMP
        """,
        (json.dumps(profile), observation_count)
    )

    connection.commit()
    connection.close()
from tools import TOOL_REGISTRY, TOOL_SCHEMA
from client import agent_chat
import json

def run_agent(task: str, session_id: str = None, cwd: str = ".", auto: bool = False, max_iter: int = 20):
    messages = [{"role": "user", "content": task}]
    iteration = 0

    print(f"\n🤖 Task: {task}\n")

    while iteration < max_iter:
        iteration += 1
        response = agent_chat(session_id, messages, TOOL_SCHEMA)
        session_id = response["session_id"]

        content = response.get("content")
        tool_calls = response.get("tool_calls")

        # No tool calls → final answer
        if not tool_calls:
            print(f"\n✅ Done:\n{content}")
            break

        # Append assistant message (content may be None)
        messages.append({
            "role": "assistant",
            "content": content,   # keep as None if None, don't coerce to ""
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"])
                    }
                }
                for tc in tool_calls
            ]
        })

        # Execute each tool and append results
        for tc in tool_calls:
            name = tc["name"]
            args = tc["arguments"]
            print(f"\n🔧 {name}({', '.join(f'{k}={repr(v)[:60]}' for k, v in args.items())})")

            fn = TOOL_REGISTRY.get(name)
            if not fn:
                result = f"Unknown tool: {name}"
            else:
                try:
                    if name in ("write_file", "edit_file") and not auto:
                        result = fn(**args, confirm=True)
                    else:
                        # strip confirm kwarg if not applicable
                        safe_args = {k: v for k, v in args.items() if k != "confirm"}
                        result = fn(**safe_args)
                except Exception as e:
                    result = f"ERROR: {e}"

            print(f"   → {str(result)[:200]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": str(result)
            })

    else:
        print("\n⚠️  Max iterations reached.")

    return session_id
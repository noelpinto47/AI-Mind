import requests
import tomllib

with open("config.toml", "rb") as f:
    CONFIG = tomllib.load(f)

BASE_URL = CONFIG["server"]["url"]

def agent_chat(session_id, messages, tools):
    resp = requests.post(f"{BASE_URL}/api/chat/agent", json={
        "session_id": session_id,
        "messages": messages,
        "tools": tools
    }, timeout=60)
    resp.raise_for_status()
    return resp.json()
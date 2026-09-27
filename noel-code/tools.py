import os, subprocess, fnmatch

def read_file(path: str) -> str:
    """Read the entire contents of a file at the given path.

    Args:
        path: The file path to read from.

    Returns:
        The file contents as a string.
    """
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_file(path: str, content: str, confirm=True) -> str:
    if confirm:
        ans = input(f"  Write to {path}? [y/N] ")
        if ans.lower() != "y":
            return "Write cancelled by user."
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Written: {path}"

def edit_file(path: str, old: str, new: str, confirm=True) -> str:
    content = read_file(path)
    if old not in content:
        return f"ERROR: string not found in {path}"
    updated = content.replace(old, new, 1)
    return write_file(path, updated, confirm=confirm)

def run_shell(cmd: str, cwd: str = ".") -> str:
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=30)
    out = result.stdout + result.stderr
    return out[:4000] or "(no output)"

def list_dir(path: str = ".") -> str:
    lines = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
        level = root.replace(path, "").count(os.sep)
        indent = "  " * level
        lines.append(f"{indent}{os.path.basename(root)}/")
        for f in files:
            lines.append(f"{indent}  {f}")
        if len(lines) > 200:
            lines.append("  ... (truncated)")
            break
    return "\n".join(lines)

def search_code(pattern: str, path: str = ".") -> str:
    results = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__"}]
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.lower() in line.lower():
                            results.append(f"{fpath}:{i}: {line.rstrip()}")
                            if len(results) >= 50:
                                return "\n".join(results)
            except Exception:
                pass
    return "\n".join(results) or "No matches found."

TOOL_REGISTRY = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "run_shell": run_shell,
    "list_dir": list_dir,
    "search_code": search_code,
}

# OpenAI-compatible schema to send to ai-mind
TOOL_SCHEMA = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write content to a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace a string in a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {"name": "run_shell", "description": "Run a shell command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}, "cwd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "list_dir", "description": "List directory tree", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "search_code", "description": "Search for a pattern in code files", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
]
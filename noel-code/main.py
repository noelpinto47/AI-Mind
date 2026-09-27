import argparse, os, uuid
from agent import run_agent

def main():
    parser = argparse.ArgumentParser(prog="noel-code")
    parser.add_argument("task", nargs="?", help="Task to perform")
    parser.add_argument("--session", default=None, help="Resume a session")
    parser.add_argument("--cwd", default=os.getcwd(), help="Project root")
    parser.add_argument("--auto", action="store_true", help="Skip confirmations")
    args = parser.parse_args()

    task = args.task or input("Task: ").strip()
    if not task:
        print("No task given.")
        return

    os.chdir(args.cwd)
    session_id = args.session or str(uuid.uuid4())[:8]
    run_agent(task, session_id=session_id, cwd=args.cwd, auto=args.auto)

if __name__ == "__main__":
    main()
"""Entry point.

  python run.py web                      bots + daily scheduler + settings page on http://127.0.0.1:8000
  python run.py preview [YYYY-MM-DD]     print the morning message for a game night
  python run.py send                     build and send the morning message right now
  python run.py export > settings.json   save your settings to a file
  python run.py import settings.json     load settings from a file (e.g. on a new server)

Environment: HOST (default 127.0.0.1; 0.0.0.0 in Docker) and PORT (default 8000).
"""
import os
import sys
from datetime import date
from pathlib import Path


def load_dotenv() -> None:
    """Read KEY=VALUE lines from .env (next to this file) into the environment."""
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    load_dotenv()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "web"
    if cmd == "web":
        import uvicorn
        # Listening on a network is safe: the settings page answers only on this machine
        # or with NBA_AGENT_ADMIN_PASSWORD (see the guard in nba_agent/web.py).
        uvicorn.run("nba_agent.web:app", host=os.environ.get("HOST", "127.0.0.1"),
                    port=int(os.environ.get("PORT", "8000")))
    elif cmd == "export":
        from nba_agent import store
        print(store.get_settings().model_dump_json(indent=2))
    elif cmd == "import" and len(sys.argv) > 2:
        from nba_agent import store
        s = store.Settings.model_validate_json(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
        store.save_settings(s)
        print("Settings imported.")
    elif cmd in ("preview", "send"):
        from nba_agent import digest, senders, store
        s = store.get_settings()
        night = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else digest.game_night()
        text, _ = digest.build(night, store.as_dict(s))
        if cmd == "preview":
            print(text)
        else:
            print(senders.send(s, text))
            store.log_send(1, night.isoformat(), "manual", True, text)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()

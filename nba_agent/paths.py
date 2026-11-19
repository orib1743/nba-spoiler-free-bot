"""Where the app keeps its files. Set NBA_AGENT_DATA_DIR to put them on a
persistent disk (in the cloud, the volume mount, e.g. /data)."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("NBA_AGENT_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")

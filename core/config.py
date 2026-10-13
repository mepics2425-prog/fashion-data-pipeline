import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = Path(
    os.environ.get("DATA_DIR", PROJECT_ROOT / "data")
)

RAW_DIR = Path(
    os.environ.get("RAW_DIR", DATA_DIR / "raw")
)

LOG_DIR = Path(
    os.environ.get("LOG_DIR", DATA_DIR / "logs")
)

STATE_DIR = Path(
    os.environ.get("STATE_DIR", DATA_DIR / "state")
)

QUEUE_DIR = Path(
    os.environ.get("QUEUE_DIR", DATA_DIR / "queues")
)

ENV_FILE = Path(
    os.environ.get("FASHION_ENV", PROJECT_ROOT / ".env")
)

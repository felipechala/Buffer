"""Runtime settings. Secrets come from the environment only, never from source."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(os.getenv("TREASURYTWIN_FIXTURES", REPO_ROOT / "fixtures"))
DEFAULT_FIXTURE = os.getenv("TREASURYTWIN_DEFAULT_FIXTURE", "demo_company")

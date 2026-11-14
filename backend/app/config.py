"""Runtime settings. Secrets come from the environment only, never from source."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(os.getenv("TREASURYTWIN_FIXTURES", REPO_ROOT / "fixtures"))
ENGINE_FIXTURES_DIR = FIXTURES_DIR / "engine"
DEMO_COMPANY_FIXTURE = FIXTURES_DIR / "demo_company.json"
DATABASE_PATH = os.getenv("DATABASE_PATH", str(REPO_ROOT / "backend" / "treasurytwin.db"))
SIMULATION_DEFAULT_SEED = int(os.getenv("SIMULATION_DEFAULT_SEED", "42"))
SANDBOX_WRITES_ENABLED = os.getenv("SANDBOX_WRITES_ENABLED", "false").lower() == "true"

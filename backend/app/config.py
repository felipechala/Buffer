"""Runtime settings. Secrets come from the environment only, never from source."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines; real environment variables win.
    Values are never logged."""
    if not path.is_file() or os.getenv("TREASURYTWIN_SKIP_DOTENV"):
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(REPO_ROOT / ".env")
FIXTURES_DIR = Path(os.getenv("TREASURYTWIN_FIXTURES", REPO_ROOT / "fixtures"))
ENGINE_FIXTURES_DIR = FIXTURES_DIR / "engine"
NORTHSTAR_FIXTURE = FIXTURES_DIR / "demo_company.json"      # engine/workflow tests
CASHGUARD_FIXTURE = FIXTURES_DIR / "cashguard_demo.json"     # CashGuard demo company
DEMO_COMPANY_FIXTURE = Path(os.getenv("DEMO_COMPANY_FIXTURE", CASHGUARD_FIXTURE))
DATABASE_PATH = os.getenv("DATABASE_PATH", str(REPO_ROOT / "backend" / "treasurytwin.db"))
SIMULATION_DEFAULT_SEED = int(os.getenv("SIMULATION_DEFAULT_SEED", "42"))
SANDBOX_WRITES_ENABLED = os.getenv("SANDBOX_WRITES_ENABLED", "false").lower() == "true"
# Demo tenant token for the public /v1 API. Not a secret: it only guards a sandbox demo.
CASHGUARD_API_KEY = os.getenv("CASHGUARD_API_KEY", "cg_test_demo_key")
NESSIE_API_KEY = os.getenv("NESSIE_API_KEY", "")
NESSIE_BASE_URL = os.getenv("NESSIE_BASE_URL", "")

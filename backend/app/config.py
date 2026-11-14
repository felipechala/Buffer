"""Runtime settings. Secrets come from the environment only, never from source."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, never overrides variables already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(REPO_ROOT / ".env")
FIXTURES_DIR = Path(os.getenv("TREASURYTWIN_FIXTURES", REPO_ROOT / "fixtures"))
ENGINE_FIXTURES_DIR = FIXTURES_DIR / "engine"
CASHGUARD_DEMO_FIXTURE = FIXTURES_DIR / "cashguard_demo.json"
# Northstar (3 accounts, 7 days) is the engine/workflow test company; Acme Co (6 accounts,
# 14 days) is the CashGuard demo company. CASHGUARD_DEMO=1 makes the app boot with Acme Co.
DEMO_COMPANY_FIXTURE = Path(os.getenv(
    "DEMO_COMPANY_FIXTURE",
    CASHGUARD_DEMO_FIXTURE if os.getenv("CASHGUARD_DEMO", "0") == "1" else FIXTURES_DIR / "demo_company.json"))
DATABASE_PATH = os.getenv("DATABASE_PATH", str(REPO_ROOT / "backend" / "treasurytwin.db"))
SIMULATION_DEFAULT_SEED = int(os.getenv("SIMULATION_DEFAULT_SEED", "42"))
SANDBOX_WRITES_ENABLED = os.getenv("SANDBOX_WRITES_ENABLED", "false").lower() == "true"
# Public /v1 API demo tenant token. One company per deployment in the hackathon build.
CASHGUARD_API_KEY = os.getenv("CASHGUARD_API_KEY", "cg_test_demo_key")
NESSIE_BASE_URL = os.getenv("NESSIE_BASE_URL", "https://api.nessieisreal.com")
NESSIE_API_KEY = os.getenv("NESSIE_API_KEY", "")
NESSIE_CUSTOMER_ID = os.getenv("NESSIE_CUSTOMER_ID", "")

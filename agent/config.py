"""Settings from environment variables (and a local .env file, never committed)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    for p in (ROOT / ".env", ROOT.parent / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

API_BASE = os.environ.get("CLAIMS_API_BASE", "http://localhost:8080")
KIE_API_KEY = os.environ.get("KIE_API_KEY", "")
KIE_BASE = os.environ.get("KIE_BASE", "https://api.kie.ai")
# Ordered model chain: first is primary, the rest are fallbacks.
LLM_MODELS = [m.strip() for m in os.environ.get("LLM_MODELS", "deepseek-v4-1-flash,gpt-5-6-luna,gemini-3-7-flash").split(",") if m.strip()]
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "60"))
LLM_CONCURRENCY = int(os.environ.get("LLM_CONCURRENCY", "4"))   # KIE allows 20 requests / 10 s
CLAIMS_DIR = Path(os.environ.get("CLAIMS_DIR", str(ROOT / "claims")))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(ROOT / "output")))
CACHE_DIR = ROOT / ".cache"
# "Today" for the run: the case data is from early November 2026.
TODAY = os.environ.get("CLAIMS_TODAY", "2026-11-02")

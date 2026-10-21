"""실행: .venv/bin/python sh/build_runbook_index.py (변경 문서에 OpenAI 비용 발생)."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
from agent.runbook_search import build_index

if __name__ == "__main__":
    print(json.dumps(build_index(), ensure_ascii=False, indent=2))

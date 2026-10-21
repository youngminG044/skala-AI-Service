#!/usr/bin/env bash
# 로컬 프런트엔드 실행. 다른 프로세스를 종료하지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
    echo "ERROR: 먼저 bash sh/setup_venv.sh로 가상환경을 준비하세요."
    exit 1
fi
if ! curl -fsS --max-time 2 http://127.0.0.1:8100/health >/dev/null 2>&1; then
    echo "ERROR: 백엔드가 응답하지 않습니다. 다른 터미널에서 bash sh/run_agent_openai.sh를 실행하세요."
    exit 1
fi
if lsof -tiTCP:8600 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: 8600 포트가 사용 중입니다. 해당 서버를 직접 확인해주세요."
    exit 1
fi
echo "채팅 화면: http://localhost:8600 · 종료: Ctrl+C"
exec .venv/bin/python web_demo_server.py

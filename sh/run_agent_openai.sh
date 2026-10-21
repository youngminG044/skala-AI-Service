#!/usr/bin/env bash
# OpenAI 실행. 기존 DB를 보존하며 다른 프로세스를 종료하지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x ".venv/bin/python3" ] && export PATH="$PWD/.venv/bin:$PATH"
if [ ! -f .env ]; then
    echo "ERROR: .env.example을 .env로 복사하고 OPENAI_API_KEY를 설정하세요."
    exit 1
fi
set -a
source .env
set +a
if [ -z "${OPENAI_API_KEY:-}" ] || [ "$OPENAI_API_KEY" = "sk-..." ]; then
    echo "ERROR: 유효한 OPENAI_API_KEY가 필요합니다."
    exit 1
fi
if lsof -tiTCP:8100 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: 8100 포트가 사용 중입니다. 해당 서버를 직접 확인해주세요."
    exit 1
fi
export LLM_BACKEND=openai SUPERVISOR_VERSION=v2 RUNBOOK_EMBEDDING_BACKEND=openai
export LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false
echo "OpenAI: fast=${OPENAI_FAST_MODEL:-gpt-4o-mini}, quality=${OPENAI_QUALITY_MODEL:-gpt-4o}"
echo "검색 인덱스가 없다면 먼저 python3 sh/build_runbook_index.py를 실행하세요."
echo "메시지마다 OpenAI API 비용이 발생합니다. session.db는 유지합니다. 종료: Ctrl+C"
exec python3 server.py

"""Docker 역할별 사전 확인·실행. 키/인덱스를 네트워크 호출 없이 검사한다."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class SetupError(ValueError):
    pass


def check_openai():
    key = os.getenv('OPENAI_API_KEY', '').strip()
    if not key or key in ('sk-...', 'your_api_key_here'):
        raise SetupError('.env에 유효한 OPENAI_API_KEY를 설정하세요. 키 값은 출력하지 않습니다.')
    os.environ['OPENAI_API_KEY'] = key
    os.environ.update(LLM_BACKEND='openai', SUPERVISOR_VERSION='v2', RUNBOOK_EMBEDDING_BACKEND='openai',
                      LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false')


def check_index(path, directory=None):
    from agent.runbook_search import OpenAIEmbedder, _documents, _unit
    try:
        embedder = OpenAIEmbedder()
        data = json.loads(Path(path).read_text())
        current = _documents(Path(directory or ROOT / 'data/runbooks'))
        rows = data['documents']
        if data['version'] != 2 or data['model'] != embedder.model or data['dimensions'] != embedder.dimensions:
            raise ValueError('index configuration mismatch')
        if len(rows) != len(current) or {d['document_id']: d['sha256'] for d in rows} != {k: d['sha256'] for k, d in current.items()}:
            raise ValueError('index documents mismatch')
        for row in rows:
            _unit(row['embedding'], embedder.dimensions)
    except Exception:
        raise SetupError('런북 인덱스가 없거나 문서·모델·차원이 다릅니다. docker compose run --rm indexer로 생성·갱신하세요.') from None


def check_backend_url():
    value = os.getenv('BACKEND_URL', 'http://backend:8100')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError()
        _ = parsed.port
    except ValueError:
        raise SetupError('BACKEND_URL은 인증 정보 없는 HTTP(S) 서버 주소여야 합니다.') from None


def prepare(role):
    if role not in ('backend', 'frontend', 'indexer', 'tests'):
        raise SetupError('실행 역할은 backend / frontend / indexer / tests 중 하나여야 합니다.')
    if role in ('backend', 'indexer'):
        check_openai()
        index = Path(os.getenv('RUNBOOK_INDEX_PATH', '/state/index/runbook_index.json'))
        if role == 'backend':
            check_index(index)
            db = Path(os.getenv('SESSION_DB_PATH', '/state/sessions/session.db'))
            db.parent.mkdir(parents=True, exist_ok=True)
            if not os.access(db.parent, os.W_OK):
                raise SetupError('세션 볼륨의 쓰기 권한을 확인하세요.')
        else:
            index.parent.mkdir(parents=True, exist_ok=True)
            if not os.access(index.parent, os.W_OK):
                raise SetupError('인덱스 볼륨의 쓰기 권한을 확인하세요.')
    elif role == 'frontend':
        check_backend_url()
    return {
        'backend': ['-m', 'uvicorn', 'server:app', '--host', '0.0.0.0', '--port', '8100', '--workers', '1', '--log-level', 'warning'],
        'frontend': ['-m', 'uvicorn', 'web_demo_server:app', '--host', '0.0.0.0', '--port', '8600', '--workers', '1', '--log-level', 'warning'],
        'indexer': ['sh/build_runbook_index.py'],
        'tests': ['-m', 'unittest', 'discover', '-s', 'tests', '-v'],
    }[role]


def main():
    try:
        role = sys.argv[1] if len(sys.argv) == 2 else ''
        args = prepare(role)
    except (SetupError, OSError) as exc:
        # 환경 값이나 인증 정보가 포함될 수 있는 내부 예외를 출력하지 않는다.
        message = str(exc) if isinstance(exc, SetupError) else '저장 경로의 설정·권한을 확인하세요.'
        print('설정 오류: ' + message, file=sys.stderr)
        return 2
    os.execv(sys.executable, [sys.executable, *args])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

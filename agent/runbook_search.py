"""짧은 런북의 OpenAI 임베딩 검색. 인덱스 생성은 명시적으로 실행한다."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

import httpx

from .contracts import RunbookCandidate, RunbookSearchResult

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_INDEX = DATA_DIR / ".cache" / "runbook_index.json"
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIMENSIONS = 1536
DEFAULT_THRESHOLD = 0.42
DEFAULT_SCORE_GAP = 0.06


class SearchError(RuntimeError):
    """상세 API 오류/인증 정보 대신 호출자에게 전달할 검색 실패 사유."""


def document_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_service_query(target: str, monitoring: dict) -> str | None:
    """다른 서비스의 사용자 문구를 섞지 않고 이 서비스의 관측 원문만 조합한다.

    정상·무알림은 검색 불필요로 분리한다. 원인 추정이나 증상→원인 변환은 하지 않는다.
    """
    if monitoring.get("system_name") != target:
        raise SearchError("검색 대상과 모니터링 대상 불일치")
    alerts = [a for a in monitoring.get("alerts", []) if a.strip()]
    if monitoring.get("status") == "healthy" and not alerts:
        return None
    # 정상 범위 수치까지 늘어놓으면 CPU·메모리 문서가 모든 질문에 섞일 수 있다.
    # 알림을 우선 사용하고 무알림 비정상 상태에서만 상태와 수치를 사용한다.
    observed = "\n".join(alerts) if alerts else (
        f"상태: {monitoring['status']}; CPU: {monitoring['cpu_percent']}%; 메모리: {monitoring['memory_percent']}%")
    return f"서비스: {target}\n관측 알림:\n{observed}\n이 증상의 점검 및 대응 절차"


def _unit(vector, dimensions):
    if not isinstance(vector, list) or len(vector) != dimensions or any(
        type(v) not in (int, float) or not math.isfinite(v) for v in vector
    ):
        raise SearchError("임베딩 벡터 형식 또는 차원 불일치")
    norm = math.sqrt(sum(v * v for v in vector))
    if not math.isfinite(norm) or norm == 0:
        raise SearchError("유효하지 않은 임베딩 벡터")
    return [v / norm for v in vector]


class OpenAIEmbedder:
    def __init__(self, model=None, dimensions=None):
        self.model = model or os.getenv("OPENAI_EMBEDDING_MODEL", DEFAULT_MODEL)
        self.dimensions = dimensions or int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", str(DEFAULT_DIMENSIONS)))

    def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key or not texts or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise SearchError("임베딩 키 또는 입력을 확인하세요")
        try:
            response = httpx.post("https://api.openai.com/v1/embeddings", headers={"Authorization": f"Bearer {key}"},
                                  json={"model": self.model, "input": texts, "dimensions": self.dimensions,
                                        "encoding_format": "float"}, timeout=30)
            response.raise_for_status()
            body = response.json()
            rows = sorted(body["data"], key=lambda row: row["index"])
            if [r["index"] for r in rows] != list(range(len(texts))) or body.get("model") != self.model:
                raise SearchError("임베딩 응답의 모델 또는 항목 불일치")
            vectors = [_unit(r["embedding"], self.dimensions) for r in rows]
            return vectors, int(body["usage"]["total_tokens"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise SearchError(f"임베딩 API 처리 실패 ({type(exc).__name__})") from None


def _documents(directory: Path) -> dict[str, dict]:
    result = {}
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise SearchError("빈 런북 문서")
        result[path.stem] = {"document_id": path.stem, "text": text, "sha256": document_hash(text)}
    if not result:
        raise SearchError("런북 문서 없음")
    return result


def build_index(embedder=None, *, directory=None, index_path=None) -> dict:
    """동일 모델·차원·본문이면 벡터 재사용. 변경 문서만 API로 다시 처리한다."""
    embedder = embedder or OpenAIEmbedder()
    directory, index_path = Path(directory or DATA_DIR / "runbooks"), Path(index_path or os.getenv("RUNBOOK_INDEX_PATH", DEFAULT_INDEX))
    documents = _documents(directory)
    old = {}
    try:
        cached = json.loads(index_path.read_text())
        if cached.get("version") == 2 and cached.get("model") == embedder.model and cached.get("dimensions") == embedder.dimensions:
            old = {d["document_id"]: d for d in cached["documents"]}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    changed = []
    for key, doc in documents.items():
        previous = old.get(key, {})
        if previous.get("sha256") == doc["sha256"]:
            try:
                doc["embedding"] = _unit(previous.get("embedding"), embedder.dimensions)
                continue
            except SearchError:
                pass
        changed.append(key)
    tokens = 0
    if changed:
        vectors, tokens = embedder.embed([key + "\n" + documents[key]["text"] for key in changed])
        if len(vectors) != len(changed):
            raise SearchError("임베딩 개수 불일치")
        for key, vector in zip(changed, vectors):
            documents[key]["embedding"] = _unit(vector, embedder.dimensions)
    payload = {"version": 2, "model": embedder.model, "dimensions": embedder.dimensions,
               "documents": [{k: v for k, v in d.items() if k != "text"} for d in documents.values()]}
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=index_path.parent, delete=False, encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False)
        temporary = output.name
    os.replace(temporary, index_path)
    return {"model": embedder.model, "dimensions": embedder.dimensions,
            "document_count": len(documents), "embedded_documents": len(changed),
            "reused_documents": len(documents) - len(changed), "embedding_tokens": tokens}


def select_candidates(candidates, threshold, score_gap):
    if not -1 <= threshold <= 1 or not 0 <= score_gap <= 2:
        raise SearchError("검색 점수 설정 범위 오류")
    if not candidates:
        return ()
    best = candidates[0].score
    return tuple(c.document_id for c in candidates if c.score >= threshold and best - c.score <= score_gap)


def search_runbooks(query: str, top_k: int = 2, *, embedder=None, directory=None,
                    index_path=None, threshold=None, score_gap=None, rerank=None) -> RunbookSearchResult:
    if type(top_k) is not int or not 1 <= top_k <= 2 or not isinstance(query, str) or not query.strip():
        raise SearchError("검색 질문과 top_k(1~2)를 확인하세요")
    started = time.perf_counter()
    embedder = embedder or OpenAIEmbedder()
    index_path = Path(index_path or os.getenv("RUNBOOK_INDEX_PATH", DEFAULT_INDEX))
    try:
        index = json.loads(index_path.read_text())
        if index["version"] != 2 or index["model"] != embedder.model or index["dimensions"] != embedder.dimensions:
            raise SearchError("임베딩 모델·차원 변경: 인덱스를 다시 생성하세요")
        current = _documents(Path(directory or DATA_DIR / "runbooks"))
        rows = index["documents"]
        if len(rows) != len(current) or {r["document_id"]: r["sha256"] for r in rows} != {k: d["sha256"] for k, d in current.items()}:
            raise SearchError("런북 변경: 인덱스를 다시 생성하세요")
        vectors = [_unit(r["embedding"], embedder.dimensions) for r in rows]
    except (OSError, ValueError, KeyError, TypeError):
        raise SearchError("검색 인덱스를 읽을 수 없습니다. 인덱스를 생성하세요") from None
    query_vectors, tokens = embedder.embed([query])
    vector = _unit(query_vectors[0], embedder.dimensions)
    candidates = sorted((RunbookCandidate(document_id=row["document_id"], document_sha256=row["sha256"],
                        reference=f"runbook://incident-response/{row['document_id']}",
                        score=max(-1, min(1, sum(a * b for a, b in zip(vector, embedding)))))
                         for row, embedding in zip(rows, vectors)), key=lambda c: (-c.score, c.document_id))[:top_k]
    selected = select_candidates(candidates,
        float(os.getenv("RUNBOOK_SCORE_THRESHOLD", str(DEFAULT_THRESHOLD))) if threshold is None else threshold,
        float(os.getenv("RUNBOOK_SCORE_GAP", str(DEFAULT_SCORE_GAP))) if score_gap is None else score_gap)
    reranking = None
    use_reranking = rerank if rerank is not None else os.getenv("RUNBOOK_RERANK_ENABLED", "true").lower() == "true"
    if use_reranking:
        from .runbook_rerank import rerank_runbooks
        reranking = rerank_runbooks(query, candidates, directory=directory)
        selected = reranking.ranked_ids
    return RunbookSearchResult(query=query, status="matched" if selected else "no_match",
                              embedding_model=embedder.model, dimensions=embedder.dimensions,
                              candidates=tuple(candidates), selected_ids=selected, embedding_tokens=tokens,
                              reranking=reranking,
                              elapsed_seconds=round(time.perf_counter() - started, 4))

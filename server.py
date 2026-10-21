"""all_total 통합 실습 — FastAPI 백엔드 (Wave 1·3·12·14 축 + 전체 조립).

- Wave 1  : lifespan에서 Agent 번들(MCP 연결·LLM·그래프)을 프로세스당 정확히 1회 생성,
            app.state로 모든 요청이 공유 (creation_count/instance_id로 증명)
- Wave 2  : mcp_server.py를 subprocess(stdio)로 기동해 연결 유지
- Wave 3  : GET /chat/stream — 그래프 실행 과정을 SSE 이벤트로 스트리밍
- Wave 12 : 대화 이력을 SQLite(session.db)에 저장 — 재시작해도 유지
- Wave 14 : SUPERVISOR_VERSION(v1/v2) 환경변수로 legacy/신형 파이프라인 즉시 전환
- LangSmith: 요청당 chat_request 루트 run 아래로 노드·LLM·MCP 호출이 계층 기록됨
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import sqlite3
from typing import Annotated

from dotenv import load_dotenv

load_dotenv()   # LANGCHAIN_TRACING_V2 / API_KEY / PROJECT — import보다 먼저

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel, StringConstraints
from agent.context import next_context

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.grounding import VALIDATION_SCOPE
from agent.mcp_client import MCPClient
from agent.session_store import SqliteSessionStore
from agent.supervisor_v1 import run_v1

CREATION_COUNT = 0   # Wave 1 — 프로세스 내 Agent 번들 생성 횟수 (항상 1이어야 정상)


ThreadId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,128}$")]


class ChatRequest(BaseModel):   # Wave 1 — Pydantic 요청 검증
    message: str
    thread_id: ThreadId = "default"


def _build_llms():
    """LLM_BACKEND=fake(기본, 결정론) | ollama(로컬 실모델) | openai(실제 OpenAI API)."""
    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend == "ollama":
        from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
        fast = OllamaFastLLM(os.environ.get("OLLAMA_FAST_MODEL", "qwen3:0.6b"),
                             usd_per_1k_tokens=0.00015)
        quality = OllamaQualityLLM(os.environ.get("OLLAMA_QUALITY_MODEL", "qwen3:8b"),
                                   usd_per_1k_tokens=0.005)
        return backend, fast, quality
    if backend == "openai":
        from agent.openai_llm import OpenAIFastLLM, OpenAIQualityLLM
        fast = OpenAIFastLLM(os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini"),
                             usd_per_1k_tokens=0.00015)
        quality = OpenAIQualityLLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o"),
                                   usd_per_1k_tokens=0.005)
        return backend, fast, quality
    fast = FastLLM("fast-model-stub", usd_per_1k_tokens=0.00015)
    quality = QualityLLM("quality-model-stub", usd_per_1k_tokens=0.005)
    return backend, fast, quality


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CREATION_COUNT
    CREATION_COUNT += 1
    mcp = MCPClient()
    await mcp.start()   # Wave 2 — MCP subprocess는 서버 수명 동안 딱 1개
    backend, fast_llm, quality_llm = _build_llms()
    app.state.llm_backend = backend
    deps = SupervisorDeps(
        fast_llm=fast_llm,
        quality_llm=quality_llm,
        mcp=mcp,
        session_store=SqliteSessionStore(),
        version=os.environ.get("SUPERVISOR_VERSION", "v2"),
    )
    app.state.deps = deps
    app.state.graph = build_graph(deps) if deps.version == "v2" else None
    app.state.instance_id = id(deps)
    yield
    await mcp.stop()
    deps.session_store.close()
    try:   # 종료 전 LangSmith 배치 전송 플러시 — kill 시 run이 pending으로 남는 것 방지
        from langchain_core.tracers.langchain import wait_for_all_tracers
        wait_for_all_tracers()
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "instance_id": app.state.instance_id,
            "creation_count": CREATION_COUNT,
            "supervisor_version": app.state.deps.version,
            "llm_backend": app.state.llm_backend,
            "fast_llm": app.state.deps.fast_llm.name,
            "quality_llm": app.state.deps.quality_llm.name}


@app.get("/version")
async def version():
    return {"supervisor_version": app.state.deps.version}


def _metadata(result: dict, run_id: str | None) -> dict:
    return {
        "route": result.get("route"),
        "task_count": len(result.get("tasks", [])),
        "gate_removed": result.get("gate_removed", 0),
        "replan_rounds": result.get("replan_rounds", 0),
        "retry_count": result.get("retry_count", 0),
        "parallel_elapsed": result.get("parallel_elapsed"),
        "validation_attempts": result.get("validation_attempts", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "cost_basis": "legacy_simulated_rate_not_billing",
        "llm_backend": app.state.llm_backend,
        "supervisor_version": app.state.deps.version,
        "instance_id": app.state.instance_id,
        "langsmith_run_id": run_id,
        "processing_status": result.get("response_state", {}).get("status"),
        "validation_status": result.get("response_state", {}).get("validation", {}).get("status"),
        "target_resolution": result.get("target_resolution"),
        "monitoring_coverage": result.get("monitoring_coverage"),
        "runbook_searches": result.get("runbook_searches", []),
        "runbook_coverage": result.get("runbook_coverage"),
        "response_state": result.get("response_state"),
        "validation_scope": VALIDATION_SCOPE if result.get("validation_result", {}).get("status") in ("passed", "failed") else None,
        "validation_history": result.get("validation_history", []),
        "context_resolution": result.get("context_resolution"),
        "completed_at": result.get("completed_at"),
    }


def _request_state(deps, message, thread_id):
    try:
        history = deps.session_store.load(thread_id)
        context = deps.session_store.load_context(thread_id) if deps.version == "v2" else None
    except (ValueError, sqlite3.Error):
        raise HTTPException(503, "대화 이력 또는 문맥을 읽지 못했습니다. 저장소를 확인해주세요.") from None
    return {"query": message, "thread_id": thread_id, "history": history, "events": [], "cost_usd": 0.0,
            "conversation_context": context.model_dump(mode="json") if context else None}, context


def _save_result(deps, message, thread_id, result, context, run_id):
    completed = datetime.now(timezone.utc)
    result["completed_at"] = completed.isoformat()
    updated = next_context(context, result, message, now=completed) if context else None
    try:
        deps.session_store.save_turn(thread_id, message, result["final"], updated, _metadata(result, run_id))
    except (ValueError, sqlite3.Error):
        raise HTTPException(503, "결과를 저장하지 못했습니다. 대화 이력을 복원한 뒤 다시 확인해주세요.") from None


@app.get("/sessions/{thread_id}")
async def session_history(thread_id: ThreadId, response: Response):
    response.headers["Cache-Control"] = "no-store"
    store = app.state.deps.session_store
    async with store.turn_lock(thread_id):
        try:
            return {"thread_id": thread_id, "messages": store.history(thread_id),
                    "context": store.load_context(thread_id).model_dump(mode="json")}
        except (ValueError, sqlite3.Error):
            raise HTTPException(503, "대화 이력을 복원하지 못했습니다. 저장소를 확인해주세요.") from None


@traceable(name="chat_request")
async def _handle_chat(message: str, thread_id: str) -> tuple[dict, str | None]:
    deps = app.state.deps
    async with deps.session_store.turn_lock(thread_id):
        initial, context = _request_state(deps, message, thread_id)
        if deps.version == "v2":
            result = await app.state.graph.ainvoke(initial)
        else:
            result = await run_v1(deps, message, thread_id, initial["history"])
        rt = get_current_run_tree()
        run_id = str(rt.id) if rt else None
        _save_result(deps, message, thread_id, result, context, run_id)
        return result, run_id


@app.post("/chat")
async def chat(req: ChatRequest):
    result, run_id = await _handle_chat(req.message, req.thread_id)
    return {"reply": result["final"], "events": result.get("events", []),
            "metadata": _metadata(result, run_id)}


@app.get("/chat/stream")
async def chat_stream(message: str, thread_id: ThreadId = "default"):
    """POST와 같은 문맥·원자 저장 정책을 적용한다. 완료한 결과만 저장한다."""
    deps = app.state.deps

    async def gen():
        async with deps.session_store.turn_lock(thread_id):
            try:
                initial, context = _request_state(deps, message, thread_id)
                if deps.version != "v2":
                    result = await run_v1(deps, message, thread_id, initial["history"])
                    for ev in result["events"]:
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                else:
                    seen, result = 0, {}
                    async for mode, payload in app.state.graph.astream(initial, stream_mode=["updates", "custom"]):
                        if mode == "custom":
                            if payload.get("type") == "AnswerToken" and result.get("route") == "incident":
                                continue
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                            continue
                        for node_name, delta in payload.items():
                            result.update(delta)
                            for ev in (delta.get("events") or [])[seen:]:
                                yield f"data: {json.dumps({**ev, 'node': node_name}, ensure_ascii=False)}\n\n"
                            if delta.get("events"):
                                seen = len(delta["events"])
                _save_result(deps, message, thread_id, result, context, None)
                final = {"type": "FinalAnswer", "reply": result["final"],
                         "prompt": result.get("last_prompt") if result.get("route") == "chat" else None,
                         "metadata": _metadata(result, None)}
                yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
            except HTTPException as exc:
                yield f"data: {json.dumps({'type': 'Error', 'message': exc.detail}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")

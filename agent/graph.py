"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

route → (chat 직행 | incident 파이프라인)
incident: resolve_targets → (clarify_target | plan) → gate → research(가드+병렬+재시도)
          → replan_check(부족 시 gap task 후 재조사) → analysis(needs_analysis=True일 때만
          실제 실행) → write → validate(반려 시 재작성) → finalize

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다(Wave 13).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
import asyncio
from datetime import datetime, timezone
import math
import time
from functools import partial
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable
from pydantic import ValidationError

from .deps import SupervisorDeps
from .contracts import (ConversationContext, DraftReport, EvidenceRecord, ResponseState,
                        RunbookEvidence, RunbookSearchResult, TargetResolution, ValidationIssue, ValidationResult)
from .grounding import (MAX_DRAFT_ATTEMPTS, prepare_report, render_report, validate_report)
from .retry import with_retry
from .runbook_search import build_service_query, document_hash
from .context import interpret_query
from .targets import (
    RegistryError, is_operational_request, normalize_plan,
    resolve_targets,
)


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    needs_analysis: bool
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft_report: dict | None
    draft_error: str | None
    draft_required: bool
    validation_result: dict
    validation_history: list[dict]
    analysis_text: str
    validation_attempts: int
    validation_feedback: str
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]
    target_resolution: dict
    target_error: str | None
    clarification_question: str | None
    pending_clarification: dict | None
    response_state: dict
    collection_results: list[dict]
    evidence: list[dict]
    monitoring_repair_rounds: int
    monitoring_coverage: dict
    runbook_searches: list[dict]
    runbook_coverage: dict
    conversation_context: dict
    context_resolution: dict
    effective_query: str


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 — 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다."""
    events = list(state.get("events", []))
    try:
        result, usage = deps.fast_llm.route(state["query"])
    except Exception as exc:
        result, usage = {}, {"usd": 0.0}
        events.append({"type": "RouteFallback", "reason": type(exc).__name__})
    result = result if isinstance(result, dict) else {}
    route = "incident" if is_operational_request(state["query"]) else result.get("route", "chat")
    if state.get("context_resolution", {}).get("force_incident"):
        route = "incident"
    route = route if route in ("chat", "incident") else "chat"
    event = {"type": "RouteDecided", "route": route}
    return {"route": route, "needs_analysis": result.get("needs_analysis") is True,
            "cost_usd": _cost(state, usage),
            "events": events + [event]}


@traceable(name="resolve_targets_node")
async def resolve_targets_node(state: AgentState, deps: SupervisorDeps) -> dict:
    decision = state["context_resolution"]
    resolution = TargetResolution.model_validate(decision["target_resolution"])
    error = state.get("target_error")
    question = decision.get("clarification_question") if not error else None
    pending = decision.get("pending_clarification")
    return {"target_resolution": resolution.model_dump(mode="json"),
            "target_error": error, "clarification_question": question,
            "pending_clarification": pending,
            "events": state["events"] + [{"type": "TargetsResolved",
                **resolution.model_dump(mode="json"), "error": error}]}


async def context_node(state: AgentState, deps: SupervisorDeps) -> dict:
    context = ConversationContext.model_validate(state.get("conversation_context") or {
        "thread_id": state.get("thread_id", "default")})
    try:
        decision = interpret_query(state["query"], context, resolver=resolve_targets)
        error = None
    except RegistryError as exc:
        error = str(exc)
        decision = {"mode": "registry_error", "force_incident": False,
                    "target_resolution": TargetResolution().model_dump(mode="json"), "effective_query": state["query"]}
    return {"context_resolution": decision, "effective_query": decision["effective_query"], "target_error": error,
            "events": state.get("events", []) + [{"type": "ContextResolved", "mode": decision["mode"]}]}


@traceable(name="clarify_target_node")
async def clarify_target_node(state: AgentState, deps: SupervisorDeps) -> dict:
    question = state.get("clarification_question")
    if state.get("target_error"):
        reply = state["target_error"] + ". 설정을 확인한 후 다시 요청해주세요. 조사는 실행하지 않았습니다."
        response = ResponseState(status="partial")
    else:
        reply = question
        response = ResponseState(status="needs_clarification", clarification_question=question)
    return {"final": reply, "response_state": response.model_dump(mode="json"),
            "events": state["events"] + [{"type": "ClarificationRequested", "question": reply},
                {"type": "Finalize", "pipeline": "target-check", "status": response.status}]}


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "response_state": ResponseState(status="completed").model_dump(mode="json"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 4 — Plan/Task 분해. Wave 2의 MCP Prompt가 표준 절차를 깔아준다.

    resource와 마찬가지로 이름을 어느 서버가 들고 있는지 미리 가정하지 않고,
    find_prompt()로 연결된 서버들을 먼저 조회해 찾은 뒤에만 get_prompt를 보낸다."""
    resolution = TargetResolution.model_validate(state["target_resolution"])
    events = list(state["events"])
    try:
        prompt_server = await deps.mcp.find_prompt("incident_analysis_prompt")
        procedure = await deps.mcp.get_prompt(
            "incident_analysis_prompt",
            {"system_name": ", ".join(resolution.resolved_targets), "symptom": state.get("effective_query", state["query"])},
            server=prompt_server,
        )
        raw, usage = deps.fast_llm.plan(state.get("effective_query", state["query"]), procedure, resolution=resolution)
    except Exception as exc:
        raw, usage = {}, {"usd": 0.0}
        events.append({"type": "PlanFallback", "reason": type(exc).__name__})
    result = normalize_plan(raw, resolution)
    adjustments = result["plan_adjustments"]
    if isinstance(raw, dict) and not any(adjustments.values()):
        adjustments = raw.get("plan_adjustments", adjustments)
    targets = [t["target"] for t in result["tasks"]
               if t["agent"] == "research" and t.get("target")]
    event = {"type": "PlanCreated", "task_count": len(result["tasks"]),
             "targets": targets, "adjustments": adjustments}
    return {"tasks": result["tasks"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage), "events": events + [event]}


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 권위 게이트. needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}


def _valid_monitoring(data: object, target: str) -> bool:
    if not isinstance(data, dict) or data.get("system_name") != target:
        return False
    if data.get("status") not in ("healthy", "degraded", "down"):
        return False
    if not isinstance(data.get("alerts"), list) or not all(isinstance(a, str) for a in data["alerts"]):
        return False
    return all(type(data.get(key)) in (int, float) and math.isfinite(data[key])
               and 0 <= data[key] <= 100 for key in ("cpu_percent", "memory_percent"))


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """대상별 수집 결과를 격리한다. 실패·미확인은 writer의 관측 자료에 넣지 않는다."""
    tasks = state["tasks"]
    indices = [i for i, task in enumerate(tasks)
               if task["agent"] == "research" and task["status"] == "pending"]
    if not indices:
        return {"events": state["events"] + [{"type": "ResearchSkipped", "reason": "pending research task 0개"}]}
    runbook_server = runbook_template = None
    discovery_error = None
    if any(tasks[i]["kind"] == "runbook" for i in indices):
        try:
            runbook_server, runbook_template = await deps.mcp.find_resource_template("get_incident_runbook")
        except Exception as exc:
            discovery_error = type(exc).__name__

    async def run_one(index: int) -> dict:
        task = tasks[index]
        source = "monitoring" if task["kind"] == "monitoring" else "runbook"
        reference = "get_monitoring_status" if source == "monitoring" else "get_incident_runbook"
        if task.get("retrieval"):
            reference = task["retrieval"]["candidate"]["reference"]
        data, retries, attempts = None, 0, 0
        status, reason = "ok", None
        try:
            await asyncio.sleep(0.35)
            if source == "monitoring":
                async def call():
                    nonlocal attempts
                    attempts += 1
                    return await deps.mcp.call_tool("get_monitoring_status", {"system_name": task["target"]})
                data, retries = await with_retry(call)
                if isinstance(data, dict) and data.get("status") == "unknown":
                    status, reason = "unknown", "모니터링 도구에서 대상을 찾지 못했습니다"
                elif not _valid_monitoring(data, task["target"]):
                    status, reason = "error", "요청한 서비스와 모니터링 응답 또는 필수 지표가 일치하지 않습니다"
            else:
                if discovery_error:
                    raise RuntimeError("런북 제공 서버를 찾지 못했습니다")
                uri = runbook_template.format(incident_type=task["target"])
                if task.get("retrieval") and uri != task["retrieval"]["candidate"]["reference"]:
                    raise ValueError("검색 후보와 원문 템플릿 URI 불일치")
                reference = uri
                data = await deps.mcp.read_resource(uri, server=runbook_server)
                if not isinstance(data, str) or not data.strip() or data.startswith("# Runbook Not Found"):
                    status, reason = "unknown", "대응 런북 원문을 확보하지 못했습니다"
                elif task.get("retrieval") and document_hash(data) != task["retrieval"]["candidate"]["document_sha256"]:
                    status, reason = "error", "검색 인덱스와 조회 원문이 변경되어 일치하지 않습니다"
        except Exception as exc:
            status, reason = "error", f"자료 수집 실패 ({type(exc).__name__})"
            retries = max(0, attempts - 1)
        return {"index": index, "target": task["target"], "source": source,
                "status": status, "reason": reason, "data": data, "retries": retries,
                "reference": reference}

    started = time.perf_counter()
    results = await asyncio.gather(*(run_one(i) for i in indices))
    new_tasks = [dict(task) for task in tasks]
    collected = list(state.get("collected", []))
    collection_results = list(state.get("collection_results", []))
    evidence = list(state.get("evidence", []))
    events = list(state["events"])
    retry_total = state.get("retry_count", 0)
    for result in results:
        task = new_tasks[result.pop("index")]
        result["task_id"] = task["id"]
        result["source_id"] = "source-" + uuid4().hex
        if result["source"] == "monitoring":
            fields = dict(
                source_id=result["source_id"], target=result["target"], source_type="monitoring",
                reference="get_monitoring_status", observed_at=datetime.now(timezone.utc),
                status=result["status"], reason=result["reason"], raw_data=result["data"],
            )
            try:
                item = EvidenceRecord(**fields)
            except ValidationError:
                result.update(status="error", reason="수집 응답을 유효한 JSON 근거로 저장할 수 없습니다", data=None)
                item = EvidenceRecord(**{**fields, "status": result["status"],
                                         "reason": result["reason"], "raw_data": None})
            evidence.append(item.model_dump(mode="json"))
        else:
            item = RunbookEvidence(
                source_id=result["source_id"], reference=result["reference"],
                observed_at=datetime.now(timezone.utc), status=result["status"],
                raw_data=result["data"] if isinstance(result["data"], str) else None,
                reason=result["reason"], applicable_targets=task.get("applicable_targets", ()),
                retrieval=task.get("retrieval"),
            )
            evidence.append(item.model_dump(mode="json"))
        task["status"] = "done" if result["status"] == "ok" else "failed"
        collection_results.append(result)
        if result["status"] == "ok":
            collected.append({"source": result["source"], "target": result["target"],
                              "source_id": result["source_id"], "data": result["data"]})
        retry_total += result["retries"]
        events.append({"type": "TaskCompleted" if result["status"] == "ok" else "TaskFailed",
                       "id": task["id"], "source": result["source"], "target": result["target"],
                       "status": result["status"], "reason": result["reason"], "retries": result["retries"]})
    return {"tasks": new_tasks, "collected": collected, "collection_results": collection_results,
            "evidence": evidence, "retry_count": retry_total,
            "parallel_elapsed": round(time.perf_counter() - started, 2), "events": events}


async def _search_per_service(state, deps):
    """정상 수집된 서비스마다 한 번 검색한다. 검색 실패는 다른 서비스와 격리한다."""
    latest = {e["target"]: e for e in state.get("evidence", [])
              if e["source_type"] == "monitoring" and e["status"] == "ok"}
    tasks, searches = [], []
    tool_server, discovery_error = None, None
    for target in state["target_resolution"]["resolved_targets"]:
        source = latest.get(target)
        if source is None:
            continue
        query = build_service_query(target, source["raw_data"])
        record = {"target": target, "monitoring_source_id": source["source_id"], "query": query}
        if query is None:
            searches.append({**record, "status": "not_needed", "reason": "정상 상태이며 활성 알림 없음"})
            continue
        try:
            if tool_server is None and discovery_error is None:
                try:
                    tool_server = await deps.mcp.find_tool("search_runbooks")
                except Exception as exc:
                    discovery_error = type(exc).__name__
            if discovery_error:
                raise ValueError("런북 검색 도구를 찾지 못했습니다")
            raw = await deps.mcp.call_tool("search_runbooks", {"query": query, "top_k": 2}, server=tool_server)
            if raw.get("status") == "error":
                searches.append({**record, "status": "error", "reason": raw.get("reason", "검색 오류")})
                continue
            found = RunbookSearchResult.model_validate(raw)
            if found.query != query:
                raise ValueError("검색 질문 불일치")
            searches.append({**found.model_dump(mode="json"), **record})
            for candidate in found.candidates:
                if candidate.document_id not in found.selected_ids:
                    continue
                tasks.append({"id": f"runbook-{target}-{candidate.document_id}", "agent": "research",
                    "kind": "runbook", "target": candidate.document_id, "status": "pending",
                    "applicable_targets": [target], "retrieval": {"target": target,
                        "monitoring_source_id": source["source_id"], "query": query,
                        "embedding_model": found.embedding_model, "candidate": candidate.model_dump(mode="json")}})
        except Exception as exc:
            searches.append({**record, "status": "error", "reason": f"런북 검색 실패 ({type(exc).__name__})"})
    return tasks, searches


def _runbook_coverage(state, searches):
    fetched = {}
    for e in state.get("evidence", []):
        if e["source_type"] == "runbook" and e["status"] == "ok":
            for target in e.get("applicable_targets", []):
                fetched.setdefault(target, []).append(e["reference"])
    return {"services": [{"target": s["target"], "search_status": s["status"],
              "status": "not_needed" if s["status"] == "not_needed" else (
                  "fetched" if s.get("selected_ids") and len(fetched.get(s["target"], [])) == len(s["selected_ids"]) else
                  (s["status"] if s["status"] in ("error", "no_match") else "fetch_failed")),
              "references": fetched.get(s["target"], [])} for s in searches]}


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    targets = state["target_resolution"]["resolved_targets"]
    collected = state.get("collected", [])
    observed = {item["target"] for item in collected if item["source"] == "monitoring"}
    missing = [target for target in targets if target not in observed]
    coverage = {"expected_targets": targets, "successful_targets": [t for t in targets if t in observed],
                "missing_targets": missing}
    events = list(state["events"])
    repair_rounds = state.get("monitoring_repair_rounds", 0)
    if missing and repair_rounds < 1:
        tasks = [dict(task) for task in state["tasks"]]
        for task in tasks:
            if task.get("kind") == "monitoring" and task.get("target") in missing:
                task["status"] = "pending"
        events.append({"type": "ReplanCheck", "is_sufficient": False, "needs_research": True,
                       "reason": "monitoring_missing", "missing_targets": missing})
        return {"tasks": tasks, "monitoring_repair_rounds": repair_rounds + 1,
                "monitoring_coverage": coverage, "events": events}
    rounds = state.get("replan_rounds", 0)
    searches = state.get("runbook_searches", [])
    if observed and rounds < 1:
        tasks, searches = await _search_per_service(state, deps)
        events.extend({"type": "RunbookSearchCompleted", **s} for s in searches)
        if tasks:
            events.append({"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
                           "needs_research": True, "reason": "runbook_search", "task_count": len(tasks)})
            return {"tasks": state["tasks"] + tasks, "replan_rounds": rounds + 1,
                    "runbook_searches": searches, "monitoring_coverage": coverage, "events": events}
    runbook_coverage = _runbook_coverage(state, searches)
    complete = all(s["status"] in ("fetched", "not_needed") for s in runbook_coverage["services"])
    events.extend([{"type": "MonitoringCoverage", **coverage},
                   {"type": "ReplanCheck", "is_sufficient": not missing and complete,
                    "needs_research": False, "reason": "finished"}])
    return {"monitoring_coverage": coverage, "runbook_searches": searches, "replan_rounds": max(rounds, 1),
            "runbook_coverage": runbook_coverage, "events": events}


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 보완 — needs_analysis=True가 정당한 경우(모델의 자기모순이 아니라
    실제로 패턴 비교가 필요하다고 판단한 경우)를 위한 실행 경로. gate가 살려둔
    agent=='analysis' task가 있을 때만 수집된 monitoring 결과들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending or not any(c["source"] == "monitoring" for c in state.get("collected", [])):
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    summary, usage = deps.fast_llm.analyze(state.get("collected", []))
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "analysis_text": summary,
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """검증 전 모델 초안은 스트리밍하지 않는다. 관측 사실은 코드가 구성한다."""
    attempt = state.get("validation_attempts", 0) + 1
    if not state["monitoring_coverage"]["successful_targets"]:
        return {"draft_required": False, "draft_report": None, "draft_error": None,
                "events": state["events"] + [{"type": "DraftSkipped", "reason": "no_observations"}]}
    get_stream_writer()({"type": "DraftStarted", "attempt": attempt})
    usage = {"usd": 0.0}
    candidate, error = None, None
    try:
        query = state.get("effective_query", state["query"])
        if state.get("analysis_text"):
            query += "\n미검증 모델 분석 (관측 사실이 아님): " + state["analysis_text"]
        raw, usage = await deps.quality_llm.write(
            query, state.get("evidence", []), attempt, state.get("validation_feedback"))
        candidate = prepare_report(raw, state.get("evidence", []),
                                   state["monitoring_coverage"]["successful_targets"]).model_dump(mode="json")
    except Exception as exc:
        error = "draft_schema" if isinstance(exc, (ValidationError, ValueError, TypeError)) else "draft_generation"
    return {"draft_required": True, "draft_report": candidate, "draft_error": error,
            "last_prompt": usage.get("prompt"), "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    if not state.get("draft_required"):
        verdict = ValidationResult()
        return {"validation_result": verdict.model_dump(mode="json"), "validation_feedback": None}
    attempt = state.get("validation_attempts", 0) + 1
    if state.get("draft_error"):
        verdict = ValidationResult(status="failed", attempts=attempt,
            issues=(ValidationIssue(code=state["draft_error"], message="구조화된 초안을 생성하거나 해석하지 못했습니다"),))
    else:
        verdict = validate_report(state.get("draft_report"), state.get("evidence", []),
                                  state["monitoring_coverage"]["successful_targets"], attempt)
    feedback = None if verdict.status == "passed" else "\n".join(f"{i.code}: {i.message}" for i in verdict.issues)
    result = verdict.model_dump(mode="json")
    return {"validation_result": result, "validation_attempts": attempt,
            "validation_history": state.get("validation_history", []) + [result],
            "validation_feedback": feedback,
            "events": state["events"] + [{"type": "Validation", "attempt": attempt,
                "status": verdict.status, "issue_codes": [i.code for i in verdict.issues],
                "will_retry": verdict.status == "failed" and attempt < MAX_DRAFT_ATTEMPTS}]}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    resolution = TargetResolution.model_validate(state["target_resolution"])
    coverage = state["monitoring_coverage"]
    runbooks = state.get("runbook_coverage", {}).get("services", [])
    partial = bool(resolution.unresolved_targets or coverage["missing_targets"]
                   or any(s["status"] not in ("fetched", "not_needed") for s in runbooks))
    validation = ValidationResult.model_validate(state["validation_result"])
    status = "validation_failed" if validation.status == "failed" else ("partial" if partial else "completed")
    response = ResponseState(status=status, validation=validation,
                             clarification_question=state.get("clarification_question"))
    report = (DraftReport.model_validate(state["draft_report"]) if validation.status == "passed" else DraftReport())
    body = render_report(report, state.get("evidence", []), coverage["successful_targets"],
                         limited=validation.status == "failed")
    lines = ["조사 대상: " + ", ".join(resolution.resolved_targets),
             "모니터링 수집 완료: " + (", ".join(coverage["successful_targets"]) or "없음")]
    if coverage["missing_targets"]:
        lines.append("모니터링 미수집: " + ", ".join(coverage["missing_targets"]))
    if resolution.unresolved_targets:
        lines.append("미확인 서비스: " + ", ".join(resolution.unresolved_targets))
    labels = {"fetched": "원문 확보", "not_needed": "정상·무알림으로 검색 생략",
              "no_match": "관련 문서 없음", "error": "검색 오류", "fetch_failed": "원문 확보 실패"}
    for service in runbooks:
        lines.append(f"대응 런북 ({service['target']}): {labels[service['status']]}")
    if state.get("clarification_question"):
        lines.append(state["clarification_question"])
    return {"final": "\n".join(lines) + "\n\n" + body,
            "response_state": response.model_dump(mode="json"),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "incident-v2", "status": response.status}]}


def _after_route(state: AgentState) -> str:
    return "chat_answer" if state["route"] == "chat" else "resolve_targets"


def _after_target_resolution(state: AgentState) -> str:
    return "plan" if state["target_resolution"]["resolved_targets"] and not state.get("target_error") else "clarify_target"


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "research" if last.get("needs_research") else "analysis"


def _after_validate(state: AgentState) -> str:
    if state.get("validation_feedback") is None or state.get("validation_attempts", 0) >= MAX_DRAFT_ATTEMPTS:
        return "finalize"
    return "write"


def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("context", node(context_node))
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("resolve_targets", node(resolve_targets_node))
    g.add_node("clarify_target", node(clarify_target_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("context")
    g.add_edge("context", "route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "resolve_targets": "resolve_targets"})
    g.add_edge("chat_answer", END)
    g.add_conditional_edges("resolve_targets", _after_target_resolution,
                            {"plan": "plan", "clarify_target": "clarify_target"})
    g.add_edge("clarify_target", END)
    g.add_edge("plan", "gate")
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()

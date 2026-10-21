"""사용자가 확인한 대상·요청만 이용하는 후속 질문 해석. LLM/API 호출 없음."""
from datetime import datetime, timezone
import re

from .contracts import ConversationContext, PendingClarification, TargetResolution
from .targets import clarification_question, has_target_mentions, is_operational_request, resolve_targets

_REFERENCE = re.compile(r"(?:그|해당)\s*(?:서비스|시스템|서버)|그중|이어서|다시|정상화|복구됐|나아졌|상태는|대응\s*절차|원인은|원인이")
_CURRENT = re.compile(r"(?:지금|현재).*(?:상태|정상|어때|괜찮|확인|복구)")
_ALL = re.compile(r"둘\s*다|두\s*서비스\s*모두|모두\s*다|전부")
_NOT_REFERENCE = re.compile(r"(?:그|해당)\s*(?:서비스|시스템|서버)\s*(?:는|은)?\s*(?:말고|제외|빼고)")
_SOCIAL = re.compile(r"^(?:다시\s*)?(?:안녕(?:하세요)?|고마워(?:요)?|감사합니다|감사해요|반가워(?:요)?)[.!?\s]*$")


def _target_only(query, resolution):
    text = query
    for target in sorted(resolution.requested_targets, key=len, reverse=True):
        text = text.replace(target, "")
    return bool(resolution.requested_targets) and bool(re.fullmatch(
        r"[\s,./`'\"!?]*(?:(?:와|과|및|하고|랑|이랑|그리고|and|만|이요|요|야|입니다|로|맞아|맞아요)[\s,./`'\"!?]*)*", text, re.I))


def interpret_query(query: str, context: ConversationContext, *, resolver=resolve_targets) -> dict:
    explicit = resolver(query)
    mentioned = bool(explicit.requested_targets) or has_target_mentions(query)
    pending = context.pending_clarification
    followup = not _SOCIAL.fullmatch(query.strip()) and bool(_REFERENCE.search(query) or _CURRENT.search(query))
    mode, resolution, original = "explicit", explicit, query
    question = None
    force_incident = False
    if mentioned:
        if pending and _target_only(query, explicit):
            mode, original, force_incident = "clarification_answer", pending.original_query, True
    elif pending and (followup or _ALL.search(query)):
        if pending.reason == "ambiguous_reference" and _ALL.search(query) and not _NOT_REFERENCE.search(query):
            resolution = resolver(" ".join(pending.candidate_targets))
            mode, original, force_incident = "clarification_answer", pending.original_query, True
        else:
            mode, question, force_incident = "pending_clarification", pending.question, True
    elif followup or (_ALL.search(query) and is_operational_request(query)):
        force_incident = True
        previous = context.previous_targets
        if _NOT_REFERENCE.search(query):
            mode = "ambiguous_reference"
            question = "대신 조사할 정확한 서비스명을 알려주세요."
        elif len(previous) == 1 or (previous and _ALL.search(query)):
            resolution = resolver(" ".join(previous))
            mode = "previous_target"
        elif len(previous) > 1:
            mode = "ambiguous_reference"
            question = f"이전 조사 대상은 {', '.join(previous)}입니다. 어느 서비스를 확인할까요? 정확한 서비스명을 알려주세요."
        else:
            mode = "missing_target"
    if question is None:
        question = clarification_question(resolution)
    waiting = None
    if question:
        if mode == "pending_clarification":
            waiting = pending
        else:
            reason = "ambiguous_reference" if mode == "ambiguous_reference" else (
                "unknown_target" if resolution.unresolved_targets else "missing_target")
            waiting = PendingClarification(original_query=original, question=question, reason=reason,
                candidate_targets=context.previous_targets if reason == "ambiguous_reference" else ())
    reference = original if mode == "clarification_answer" else (
        context.last_request if mode == "previous_target" else None)
    effective = query
    if mode in ("previous_target", "clarification_answer"):
        effective = (f"현재 사용자 메시지: {query}\n확인된 조사 대상: {', '.join(resolution.resolved_targets) or '없음'}\n"
                     f"사용자의 이전 요청(증상 참고, 과거 관측 사실이 아님): {reference or '(없음)'}\n"
                     "현재 상태는 이번에 새로 수집한 관측만으로 판단하세요.")
    return {"mode": mode, "force_incident": force_incident,
            "target_resolution": resolution.model_dump(mode="json"), "effective_query": effective,
            "clarification_question": question,
            "pending_clarification": waiting.model_dump(mode="json") if waiting else None,
            "request_reference": reference}


def next_context(previous: ConversationContext, result: dict, query: str, *, now=None) -> ConversationContext:
    now = now or datetime.now(timezone.utc)
    if result.get("route") != "incident" or result.get("target_error"):
        return previous.model_copy(update={"updated_at": now})
    decision = result.get("context_resolution", {})
    resolution = TargetResolution.model_validate(result["target_resolution"])
    mode = decision.get("mode")
    targets = (previous.previous_targets if mode in ("ambiguous_reference", "pending_clarification")
               else resolution.resolved_targets)
    reference = query
    if mode in ("previous_target", "clarification_answer"):
        reference = decision.get("request_reference") or previous.last_request or query
    return ConversationContext(thread_id=previous.thread_id, previous_targets=targets,
        symptoms=(reference,), last_request=reference,
        pending_clarification=result.get("pending_clarification"), updated_at=now,
        last_investigation_at=now if result.get("monitoring_coverage", {}).get("successful_targets")
                                   else previous.last_investigation_at)

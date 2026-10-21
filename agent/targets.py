"""등록 목록 기반 대상 식별과 모델 계획 정규화. 대화 문맥 추론은 수행하지 않는다."""
import json
from pathlib import Path
import re

from .contracts import TargetResolution

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "monitoring.json"
_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9_-]*")
_SERVICE_ID = re.compile(r"(?:[-_]service(?:[-_][A-Za-z0-9]+)*|[-_]\d+)$")
_EXCLUDED = re.compile(
    r"^\s*[\"'`]?\s*(?:은|는|이|가|을|를)?\s*"
    r"(?:말고|빼고|빼주세요|빼줘|제외하고|제외해주세요|제외해줘|제외(?:\s|$)|대신)"
)
_JOIN = re.compile(r"^\s*(?:(?:,|와|과|및|하고|랑|이랑|그리고|/|&|and)\s*)+$", re.I)
_OPERATIONAL = re.compile(
    r"장애|진단|조사|조회|상태|성능|접속|오류|응답|모니터링|느려|느리|지연|급증|다운|타임아웃|메모리|디스크|"
    r"\b(?:cpu|memory|latency|incident|timeout|monitoring)\b", re.I
)


class RegistryError(ValueError):
    """대상 목록을 신뢰할 수 없어 조사를 시작할 수 없음."""


def load_registry(path: Path | None = None) -> frozenset[str]:
    try:
        data = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError("서비스 등록 목록을 읽을 수 없습니다") from exc
    if (not isinstance(data, dict) or not data
            or any(not isinstance(name, str) or _TOKEN.fullmatch(name) is None
                   or not isinstance(item, dict) for name, item in data.items())):
        raise RegistryError("서비스 등록 목록의 형식이 올바르지 않습니다")
    return frozenset(data)


def is_operational_request(query: str) -> bool:
    """명백한 운영 요청이 소형 모델의 chat 오분류로 조사를 우회하지 않도록 한다."""
    return bool(_OPERATIONAL.search(query))


def has_target_mentions(query: str) -> bool:
    """모두 제외한 명시적 요청을 '대상 미언급'으로 취급해 문맥을 붙이지 않는다."""
    known = load_registry()
    return any(m.group() in known or _SERVICE_ID.search(m.group()) for m in _TOKEN.finditer(query))


def resolve_targets(query: str, known_systems: frozenset[str] | None = None) -> TargetResolution:
    known = load_registry() if known_systems is None else known_systems
    mentions = [m for m in _TOKEN.finditer(query)
                if m.group() in known or _SERVICE_ID.search(m.group())]
    excluded = set()
    for i, mention in enumerate(mentions):
        end = mentions[i + 1].start() if i + 1 < len(mentions) else len(query)
        if not _EXCLUDED.search(query[mention.end():end]):
            continue
        excluded.add(i)
        # "auth-service와 cache-service는 제외하고 ..."처럼 연결된 제외 목록.
        j = i
        while j > 0 and _JOIN.fullmatch(query[mentions[j - 1].end():mentions[j].start()]):
            j -= 1
            excluded.add(j)
    requested = tuple(dict.fromkeys(m.group() for i, m in enumerate(mentions) if i not in excluded))
    return TargetResolution(
        requested_targets=requested,
        resolved_targets=tuple(name for name in requested if name in known),
        unresolved_targets=tuple(name for name in requested if name not in known),
    )


def clarification_question(resolution: TargetResolution) -> str | None:
    if resolution.unresolved_targets:
        return (f"등록 목록에서 {', '.join(resolution.unresolved_targets)}를 찾지 못했습니다. "
                "정확한 서비스명을 알려주시겠습니까?")
    if not resolution.resolved_targets:
        return "어느 서비스의 상태를 확인할까요? 정확한 서비스명을 알려주세요."
    return None


def normalize_plan(raw: object, resolution: TargetResolution) -> dict:
    """모델의 계획과 무관하게 확인된 대상마다 pending 모니터링 작업을 하나씩 만든다."""
    raw = raw if isinstance(raw, dict) else {}
    candidates = raw.get("tasks", [])
    candidates = candidates if isinstance(candidates, list) else []
    candidates = [t for t in candidates if isinstance(t, dict)]
    planned = [t.get("target") for t in candidates if t.get("agent") == "research"
               and isinstance(t.get("target"), str)]
    targets = resolution.resolved_targets
    tasks = [{"id": f"task-monitoring-{i}", "agent": "research", "kind": "monitoring",
              "target": target, "status": "pending"} for i, target in enumerate(targets)]
    if targets and any(t.get("agent") == "analysis" for t in candidates):
        tasks.append({"id": "task-analysis", "agent": "analysis", "kind": "pattern-analysis",
                      "status": "pending"})
    return {
        "tasks": tasks,
        "needs_analysis": raw.get("needs_analysis") is True,
        "plan_adjustments": {
            "added_targets": [t for t in targets if t not in planned],
            "removed_targets": list(dict.fromkeys(t for t in planned if t not in targets)),
            "duplicate_targets": list(dict.fromkeys(t for t in planned if planned.count(t) > 1)),
        },
    }

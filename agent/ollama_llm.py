"""LLM_BACKEND=ollama — 로컬 Ollama 실모델 백엔드 (fake_llm과 동일 인터페이스).

Wave 10a 구도를 실물로: fast(기본 qwen3:0.6b)는 분류·계획·단순 대화, quality(기본
qwen3:8b)는 최종 답변 작성 전담. Ollama의 구조화 출력(format=<JSON Schema>)과
temperature=0으로 변동을 줄이지만 실모델이므로 결정론은 아니다 — 그래서 gate(W6)·
replan(W7)·validator(W8)가 연출이 아니라 실제 방어선으로 동작한다.
fc
비용(cost_usd)은 '가상 단가 × 실측 토큰 수(prompt_eval_count+eval_count)'다 — 로컬
추론이라 실제 과금은 0이지만, W10a의 모델 분리 효과를 같은 잣대로 비교하기 위함이다.

사전 조건: `ollama serve` 실행 중 + `ollama pull qwen3:0.6b qwen3:8b`.
"""
import json
import re

import httpx
from langsmith import traceable

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


from .targets import normalize_plan, resolve_targets


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ["chat", "incident"]},
        "needs_analysis": {"type": "boolean"},
    },
    "required": ["route", "needs_analysis"],
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_analysis": {"type": "boolean"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string",
                              "enum": ["research", "analysis"]},
                    "target": {"type": "string"},
                },
                "required": ["agent"],
            },
        },
    },
    "required": ["needs_analysis", "tasks"],
}


class OllamaLLM:
    def __init__(self, model: str, usd_per_1k_tokens: float):
        self.name = f"ollama:{model}"
        self.model = model
        self.usd_per_1k = usd_per_1k_tokens

    def _chat(self, system: str, user: str, schema: dict | None = None) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": 0.0},
        }
        if schema is not None:
            payload["format"] = schema
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=300.0)
        resp.raise_for_status()
        data = resp.json()
        content = data["message"]["content"]
        tokens = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
        usage = {"model": self.name, "tokens": tokens,
                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        return content, usage

    async def _chat_stream(self, system: str, user: str, on_token=None) -> tuple[str, dict]:
        """사용자 대면 텍스트(최종 답변류) 전용 — Ollama NDJSON 스트림을 토큰 단위로
        받아 on_token(조각)을 즉시 호출해 SSE로 실시간 중계할 수 있게 한다. 분류/계획
        등 JSON 스키마 출력(route/plan/analyze)은 부분 출력이 의미가 없어 그대로 _chat을
        쓴다."""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": True,
            "options": {"temperature": 0.0},
        }
        chunks: list[str] = []
        usage = {"model": self.name, "tokens": 0, "usd": 0.0}
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("POST", OLLAMA_URL, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    piece = data.get("message", {}).get("content", "")
                    if piece:
                        chunks.append(piece)
                        if on_token is not None:
                            on_token(piece)
                    if data.get("done"):
                        tokens = ((data.get("prompt_eval_count") or 0)
                                  + (data.get("eval_count") or 0))
                        usage = {"model": self.name, "tokens": tokens,
                                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        return "".join(chunks), usage


class OllamaFastLLM(OllamaLLM):
    """분류·계획·단순 대화 전담 (저사양 소형 모델)."""

    @traceable(run_type="llm", name="ollama_fast.route")
    def route(self, query: str) -> tuple[dict, dict]:
        system = ("당신은 ICT 운영 지원 라우터다. 사용자 요청을 분류하라. "
                  "장애·성능 저하·지연·진단·모니터링·티켓 등 운영 이슈면 route=incident, "
                  "인사·잡담·일반 질문이면 route=chat. "
                  "needs_analysis는 패턴 비교/통계 분석이 꼭 필요할 때만 true.")
        content, usage = self._chat(system, query, schema=ROUTE_SCHEMA)
        try:
            raw = json.loads(content)
            route = raw.get("route") if raw.get("route") in ("chat", "incident") else None
            result = {"route": route or "chat",
                      "needs_analysis": bool(raw.get("needs_analysis", False))}
        except (json.JSONDecodeError, AttributeError):
            # 소형 모델의 형식 붕괴 대비 — 코드 레벨 fallback (W6 정신)
            incident = bool(re.search(r"장애|진단|다운|지연|급증|incident|티켓", query))
            result = {"route": "incident" if incident else "chat",
                      "needs_analysis": False}
        return result, usage

    @traceable(run_type="llm", name="ollama_fast.plan")
    def plan(self, query: str, procedure_prompt: str) -> tuple[dict, dict]:
        resolution = resolve_targets(query)
        if not resolution.resolved_targets:
            return normalize_plan({}, resolution), {"model": self.name, "tokens": 0, "usd": 0.0}
        system = ("당신은 ICT 운영 Supervisor의 planning 노드다. 표준 진단 절차에 따라 "
                  "실행 계획(tasks)을 JSON으로 세워라. "
                  f"확인된 조사 대상은 {', '.join(resolution.resolved_targets)}뿐이다. "
                  "각 대상마다 agent=research 작업을 하나씩 만들고 다른 대상을 추가하지 마라. "
                  "패턴 비교가 꼭 필요할 때만 analysis 작업을 추가하라.")
        user = f"## 표준 절차\n{procedure_prompt}\n\n## 사용자 요청\n{query}"
        content, usage = self._chat(system, user, schema=PLAN_SCHEMA)
        try:
            raw = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            raw = {}
        return normalize_plan(raw, resolution), usage

    @traceable(run_type="llm", name="ollama_fast.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 패턴 분석가다. 여러 시스템의 모니터링 결과를 비교해 "
                  "공통 원인이 의심되는 패턴이 있는지 한두 문장으로 짧게 답하라. "
                  "패턴이 없으면 '특이 패턴 없음'이라고만 답하라.")
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        facts = "\n".join(
            f"- {d['system_name']}: 상태={d['status']}, CPU {d['cpu_percent']}%, "
            f"알림 {d['alerts']}" for d in mon) or "(모니터링 데이터 없음)"
        return self._chat(system, facts)

    @traceable(run_type="llm", name="ollama_fast.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        system = ("당신은 간결한 한국어 비서다. 한두 문장으로 답하라. "
                  "대화 이력에서 사용자가 밝힌 이름이 있으면 정확히 기억해 답하라.")
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in history[-10:])
        user = (f"## 지금까지의 대화\n{rendered or '(없음)'}\n\n"
                f"## 사용자의 새 메시지\n{query}")
        return await self._chat_stream(system, user, on_token=on_token)


class OllamaQualityLLM(OllamaLLM):
    """최종 답변 작성 전담 (상대적으로 큰 모델)."""

    @traceable(run_type="llm", name="ollama_quality.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
              feedback: str | None, on_token=None) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 보고 작성자다. 수집된 사실만 사용해 한국어로 "
                  "간결한 대응 보고를 작성하라. 반드시 '1) ... 2) ... 3) ...' 형식의 "
                  "번호 절차로 답하고, 수치는 수집된 값을 그대로 인용하라.")
        facts = []
        for c in collected:
            if c["source"] == "monitoring":
                d = c["data"]
                facts.append(f"- 모니터링 {d['system_name']}: 상태={d['status']}, "
                             f"CPU {d['cpu_percent']}%, 메모리 {d['memory_percent']}%, "
                             f"알림 {len(d['alerts'])}건 {d['alerts']}")
            elif c["source"] == "runbook":
                facts.append(f"- runbook 발췌:\n{c['data'].strip()}")
            elif c["source"] == "analysis":
                facts.append(f"- 패턴 분석: {c['data'].strip()}")
        user = f"## 사용자 요청\n{query}\n\n## 수집된 사실\n" + "\n".join(facts)
        if feedback:
            user += f"\n\n## 이전 초안에 대한 검증 반려 사유 (반드시 반영)\n{feedback}"
        return await self._chat_stream(system, user, on_token=on_token)

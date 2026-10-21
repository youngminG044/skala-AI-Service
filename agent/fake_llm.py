"""Wave 4/10a — 결정론적 Fake LLM 2종 (fast/quality) + 실측 비용 계산.

실제 LLM 호출 없이도 파이프라인 전체가 결정론적으로 재현되도록, 프롬프트 내용에 따라
정해진 응답을 돌려준다. 각 호출은 LangSmith에 run_type="llm"으로 기록되고, 문자열
길이 기반 토큰 추정치로 비용을 계산해 상태에 누적한다 (Wave 10a의 모델 분리 근거).
"""
import json
import re

from langsmith import traceable

from .targets import normalize_plan, resolve_targets


class FakeLLM:
    def __init__(self, name: str, usd_per_1k_tokens: float):
        self.name = name
        self.usd_per_1k = usd_per_1k_tokens

    def _usage(self, prompt: str, completion: str) -> dict:
        tokens = (len(prompt) + len(completion)) // 4 + 1
        return {"model": self.name, "tokens": tokens,
                "usd": round(tokens / 1000 * self.usd_per_1k, 6)}


class FastLLM(FakeLLM):
    """분류·계획 등 짧고 정형적인 단계 전담 (저비용)."""

    @traceable(run_type="llm", name="fast_llm.route")
    def route(self, query: str) -> tuple[dict, dict]:
        """Wave 5 — 쿼리 라우팅. 장애 관련이면 incident 파이프라인, 아니면 chat 직행."""
        incident = bool(re.search(r"장애|진단|다운|지연|급증|incident|티켓", query))
        result = {"route": "incident" if incident else "chat", "needs_analysis": False}
        return result, self._usage(query, str(result))

    @traceable(run_type="llm", name="fast_llm.plan")
    def plan(self, query: str, procedure_prompt: str) -> tuple[dict, dict]:
        """Wave 4 — Plan/Task 분해. MCP Prompt가 준 표준 절차를 따라 계획을 세운다.

        Wave 6 재현: needs_analysis=False라고 스스로 판단해 놓고도 analysis task를
        끼워 넣는 '모델의 자기모순'을 결정론적으로 재현한다 → gate 노드가 걸러낸다.
        """
        raw = {"needs_analysis": False,
               "tasks": [{"agent": "analysis"}]}  # gate의 불필요 분석 작업 제거 확인용
        result = normalize_plan(raw, resolve_targets(query))
        return result, self._usage(query + procedure_prompt, str(result))

    @traceable(run_type="llm", name="fast_llm.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        """Wave 6 보완 — 여러 시스템의 monitoring 결과 사이에 공통 패턴이 있는지 비교."""
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        degraded = [d["system_name"] for d in mon if d.get("status") != "healthy"]
        if len(degraded) >= 2:
            summary = (f"{', '.join(degraded)} 복수 시스템이 동시에 저하됨 — "
                       f"공유 의존성 등 공통 원인이 의심됨")
        elif degraded:
            summary = f"{degraded[0]}만 단독 저하 — 연관 시스템은 정상, 국소 이슈로 추정"
        else:
            summary = "모니터링된 시스템 모두 healthy — 패턴상 이상 징후 없음"
        return summary, self._usage(str(collected), summary)

    @traceable(run_type="llm", name="fast_llm.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        """Wave 5의 direct 경로 + Wave 12 증명용 — 히스토리에서 이름을 기억해낸다.

        fake 백엔드는 실제 네트워크 호출이 없어 토큰 단위로 쪼갤 대상이 없다 —
        ollama_llm.py와 인터페이스(on_token 콜백)만 맞추고 결과를 한 번에 반환한다."""
        for msg in reversed(history):
            m = re.search(r"제 이름은 (\S+?)입니다", msg.get("content", ""))
            if m and re.search(r"이름이 뭐", query):
                reply = f"{m.group(1)}님이라고 말씀하셨어요. 무엇을 도와드릴까요?"
                if on_token is not None:
                    on_token(reply)
                return reply, self._usage(query, reply)
        m = re.search(r"제 이름은 (\S+?)입니다", query)
        if m:
            reply = f"안녕하세요, {m.group(1)}님! 무엇을 도와드릴까요?"
        else:
            reply = "안녕하세요! 무엇을 도와드릴까요?"
        if on_token is not None:
            on_token(reply)
        return reply, self._usage(query, reply)


class QualityLLM(FakeLLM):
    """최종 답변 작성 전담 (고품질·고비용)."""

    @traceable(run_type="llm", name="quality_llm.write")
    async def write(self, query: str, evidence: list[dict], attempt: int,
                    feedback: str | None, on_token=None) -> tuple[dict, dict]:
        """정상 주석을 반환한다. 실패 초안은 테스트에서 명시적으로 주입한다."""
        result = {"hypotheses": [], "missing_checks": ["원인 확정을 위한 추가 자료 확인 필요"],
                  "recommendations": []}
        return result, self._usage(query, json.dumps(result, ensure_ascii=False))

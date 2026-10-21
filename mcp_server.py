"""all_total 통합 실습 — MCP Server (Wave 2 축).

Tool / Resource / Prompt 3대 capability를 모두 노출하는 실제 fastmcp 서버.
FastAPI 백엔드(server.py)가 lifespan에서 이 파일을 subprocess(stdio)로 1회 기동해
프로세스가 살아 있는 동안 재사용한다 (Wave 1 싱글턴 + Wave 2 표준화의 결합).

- Tool    get_monitoring_status : 호출마다 값이 달라지는 동적 조회 → Tool이 맞다
- Resource runbook://incident-response/{incident_type} : URI로 식별되는 정적 문서
- Prompt  incident_analysis_prompt : 여러 Tool을 조합하는 표준 진단 절차를 강제
"""
import asyncio
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from agent.runbook_search import SearchError, search_runbooks as search_local_runbooks

mcp = FastMCP(name="capstone-all-total-ops")

DATA_DIR = Path(__file__).parent / "data"


class SystemStatus(BaseModel):
    system_name: str = Field(description="시스템 이름")
    status: str = Field(description="상태: healthy / degraded / down / unknown")
    cpu_percent: float | None = Field(description="CPU 사용률 (%), 미확인 시 null")
    memory_percent: float | None = Field(description="메모리 사용률 (%), 미확인 시 null")
    last_checked: str = Field(description="마지막 체크 시각 (ISO 8601)")
    alerts: list[str] = Field(default_factory=list, description="활성 알림 목록")


@mcp.tool()
def get_monitoring_status(system_name: str) -> SystemStatus:
    """지정된 시스템의 현재 모니터링 상태를 조회합니다 (호출마다 값이 달라지는 동적 Tool)."""
    monitoring_data = json.loads((DATA_DIR / "monitoring.json").read_text())
    if system_name not in monitoring_data:
        return SystemStatus(
            system_name=system_name, status="unknown",
            cpu_percent=None, memory_percent=None,
            last_checked=datetime.now(timezone.utc).isoformat(),
            alerts=[f"System '{system_name}' not found in monitoring"],
        )
    data = monitoring_data[system_name]
    jitter = random.uniform(-3.0, 8.0)
    return SystemStatus(
        system_name=system_name,
        status=data["status"],
        cpu_percent=round(data["cpu_base"] + jitter, 1),
        memory_percent=round(data["memory_base"] + jitter / 2, 1),
        last_checked=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        alerts=data.get("alerts", []),
    )


@mcp.resource("runbook://incident-response/{incident_type}")
def get_incident_runbook(incident_type: str) -> str:
    """특정 incident 유형의 runbook(정적 문서)을 반환합니다."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", incident_type):
        return "# Runbook Not Found\n\nInvalid document ID"
    runbook_path = DATA_DIR / "runbooks" / f"{incident_type}.md"
    if not runbook_path.exists():
        return f"# Runbook Not Found\n\nNo runbook for incident type: {incident_type}"
    return runbook_path.read_text()


@mcp.tool()
async def search_runbooks(query: str, top_k: int = 2) -> dict:
    """관측 증상의 관련 런북 후보와 선택 결과를 반환한다. 원문 조회는 Resource를 사용한다."""
    if os.getenv("RUNBOOK_EMBEDDING_BACKEND", os.getenv("LLM_BACKEND", "fake")) != "openai":
        return {"status": "error", "reason": "OpenAI 런북 검색 백엔드가 설정되지 않았습니다"}
    try:
        result = await asyncio.to_thread(search_local_runbooks, query, top_k)
        return result.model_dump(mode="json")
    except (SearchError, ValueError) as exc:
        return {"status": "error", "reason": str(exc)}


@mcp.prompt()
def incident_analysis_prompt(system_name: str, symptom: str) -> str:
    """장애 진단 표준 절차 프롬프트 — planning 노드가 계획을 세울 때 이 절차를 따른다."""
    return f"""다음 장애에 대해 표준 진단 절차를 수행하세요.

## 장애 정보
- 시스템: {system_name}
- 증상: {symptom}

## 진단 절차 (반드시 순서대로 수행)
1. get_monitoring_status Tool로 관련 시스템의 현재 상태를 조회하세요.
2. 증상에 해당하는 runbook(runbook://incident-response/...)을 확인하세요.
3. 위 정보를 종합해 가능한 원인과 권장 조치를 제시하세요.
"""


if __name__ == "__main__":
    try:
        mcp.run(show_banner=False)
    except TypeError:   # 구버전 fastmcp 호환
        mcp.run()

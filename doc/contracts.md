# 공통 데이터 구조 사용 가이드

`agent/contracts.py`는 조사 대상, 수집 자료, 답변 초안, 대화 문맥, 처리·검증 결과를
Pydantic 모델로 정의합니다. Ollama와 OpenAI 중 어느 제공자를 사용하더라도 같은
구조를 사용합니다. 제공자 호출이나 환경변수 읽기, 파일·DB 접근은 수행하지 않습니다.

## 구조와 책임

| 모델 | 주요 필드 | 구조 검증 범위 |
|---|---|---|
| `TargetResolution` | `requested_targets`, `resolved_targets`, `unresolved_targets` | 누락·추가·중복·중복 분류 거부 |
| `EvidenceRecord` | `source_id`, `target`, `source_type`, `reference`, `observed_at`, `status`, `raw_data`, `reason` | 출처 필수 정보와 수집 결과 구분 |
| `Fact` | `target`, `field`, `value`, `unit`, `source_id` | 검증할 사실의 필수 정보와 값 타입 |
| `RunbookEvidence` | `source_id`, `reference`, `observed_at`, `status`, `raw_data`, `applicable_targets` | 문서 원문과 확인된 서비스 연결 |
| `RunbookSelection` | `source_id`, `step`, `target` | 모델이 선택한 문서 단계 |
| `RunbookCitation` | `source_id`, `step`, `target`, `quote` | 코드가 원문에서 구성한 인용 |
| `DraftAnnotations` | `hypotheses`, `missing_checks`, `recommendations` | 모델이 생성할 주석과 단계 선택 |
| `DraftReport` | `facts`, `hypotheses`, `missing_checks`, `recommendations` | 사실·추정·확인 사항·조치 분리 |
| `PendingClarification` | `original_query`, `question`, `reason`, `candidate_targets` | 원래 요청과 확인 질문 보관 |
| `ConversationContext` | `thread_id`, `previous_targets`, `symptoms`, `pending_clarification` | 세션별 문맥의 JSON 저장·복원 형식 |
| `ValidationIssue` | `code`, `message`, `target`, `source_id`, `field` | 실패 사유 표현 |
| `ValidationResult` | `status`, `attempts`, `issues` | 미검증·통과·실패의 일관성 |
| `ResponseState` | `status`, `validation`, `clarification_question` | 처리 상태와 검증 상태의 일관성 |

`TargetResolution`은 **대상 식별이 끝난 결과**를 입력받습니다. `resolved_targets`의
실제 서비스 등록 여부 확인, 사용자 표현 해석, 제외 대상 처리, 조사 태스크 생성은
대상 식별 기능의 책임입니다. 이름에 기본 서비스나 제공자별 규칙을 넣지 않습니다.

## 대상 목록 구성

프로젝트 폴더에서 실행하는 Python 코드 예시입니다.

```python
from agent.contracts import TargetResolution

targets = TargetResolution(
    requested_targets=["payments-service", "billing-service"],
    resolved_targets=["payments-service"],
    unresolved_targets=["billing-service"],
)
assert targets.needs_clarification
```

요청 대상은 확인된 대상과 미확인 대상 중 정확히 한 곳에 포함해야 합니다.
목록 순서는 보존하며, 중복은 조용히 제거하지 않고 검증 오류로 처리합니다.
아무 대상도 없는 `TargetResolution()`은 임의 서비스를 추가하지 않으며 확인 질문이
필요한 상태로 표현합니다.

## 출처와 초안

- `source_type`: `monitoring`, `runbook`, `analysis`. 분석 결과는 별도 종류로 기록합니다.
- `target`: 조사 대상 서비스명입니다. 런북 문서 유형을 서비스명 대신 넣지 않습니다.
- `reference`: 수집 도구 이름 또는 원문 URI입니다.
- `observed_at`: 시간대가 포함된 수집 시각입니다. 도구 원문의 측정 시각은 `raw_data`에 보존합니다.
  기존 도구의 시간대 없는 시각은 수집 기능에서 명시적으로 처리해야 합니다.
- `status`: `ok`, `unknown`, `error`. 미확인·오류에는 `reason`이 필요합니다.
- `raw_data`: JSON으로 표현 가능한 원본 자료입니다. 런북 본문의 줄바꿈도 그대로 유지합니다.

`Fact.value`는 문자열·정수·실수·불리언을 구별합니다. 예를 들어 `"91.0"`을
자동으로 숫자 `91.0`으로 바꾸지 않습니다. 비어 있는 값, NaN, 무한대는 거부합니다.

**구조 검증 통과는 사실 검증 통과가 아닙니다.** `source_id`가 실제 존재하는지,
서비스·수치·단위가 원본과 일치하는지, 미확인·오류·분석 자료를 사실로 인용하는지와
자유 서술에 근거 없는 주장이 있는지는 별도 검증 기능에서 판단합니다.
[사실·근거 검증](grounding.md)은 실행 흐름에 연결되어 있으며 모델이 사실·인용문을 재작성하지 않도록 구성합니다.

## 저장과 복원

```python
from agent.contracts import ConversationContext

context = ConversationContext(
    thread_id="session-a",
    previous_targets=["payments-service"],
    symptoms=["CPU 급증"],
)
payload = context.model_dump_json()
restored = ConversationContext.model_validate_json(payload)
assert restored == context
```

신뢰할 수 없는 Python 사전은 `model_validate()`, JSON 문자열은
`model_validate_json()`으로 검증합니다. 필드 오타와 알 수 없는 필드는 거부합니다.
목록은 내부에서 tuple로 보관하고 필드 재할당을 막습니다. 새 문맥은 검증을 거쳐
새 인스턴스로 만듭니다. `EvidenceRecord.raw_data` 안의 사전·목록을 독립된 작업에
복사할 때는 `model_copy(deep=True)`를 사용합니다.

이 구조만으로 세션을 자동 저장하거나 브라우저를 복원하지 않습니다. 저장 계층은
`thread_id`별 조회와 접근 분리, 원자적 저장을 별도로 구현해야 합니다.

## 처리 상태

- `needs_clarification`: 확인 질문 필요. `clarification_question`을 반드시 지정합니다.
- `completed`: 요청 처리 완료. 이것만으로 사실 검증 통과를 의미하지 않습니다.
- `partial`: 일부 처리 완료. 확인된 사실의 검증 결과와 추가 질문을 함께 담을 수 있습니다.
- `validation_failed`: 답변 검증 실패. `ValidationResult.status`도 `failed`여야 합니다.

검증 상태는 `not_run`, `passed`, `failed`입니다. 통과·실패에는 1회 이상의 시도 기록이
필요하고, 실패에는 구체적인 오류가 있어야 합니다. 오류가 남아 있는 통과 판정은
거부합니다. 재작성 제한과 장애 답변 공개 여부는 실행 기능에서 적용합니다.

## 기능 테스트 실행

서비스 폴더(`improved_ai_service`)에서 실행합니다.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

테스트는 공통 구조, 임시 JSON 파일 복원, 대상 식별, 실제 그래프와 API의 근거 검증을 확인합니다.
모델·MCP는 테스트 구현을 주입하고 임시 DB만 사용합니다. LLM 호출, 실제 서버 기동,
기존 대화 DB 접근은 수행하지 않습니다. 실모델 품질과 실제 세션 복원은 별도로 확인합니다.

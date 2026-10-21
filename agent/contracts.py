"""조사 대상·근거·초안·대화 문맥·처리 상태의 공통 데이터 구조.

모델 제공자나 실행 환경에 의존하지 않는다. 이 모듈은 입력 구조만 검증하며,
대상 식별, 근거와 사실의 대조, 검색, 세션 저장은 각 기능에서 수행한다.
대상·모니터링 근거·처리 상태는 그래프에서 사용하며 나머지는 후속 기능에서 연결한다.
"""
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)

NonBlankString = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)
]


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError("목록에 중복된 항목이 있습니다")
    return values


UniqueStrings = Annotated[tuple[NonBlankString, ...], AfterValidator(_unique)]
SourceType = Literal["monitoring", "runbook", "analysis"]
EvidenceStatus = Literal["ok", "unknown", "error"]
ProcessingStatus = Literal[
    "needs_clarification", "completed", "partial", "validation_failed"
]
ValidationStatus = Literal["not_run", "passed", "failed"]
FactValue = NonBlankString | StrictBool | StrictInt | StrictFloat


class ContractModel(BaseModel):
    """필드 오타·비정상 숫자를 거부하고 생성 후 필드 재할당을 막는다.

    목록은 tuple로 보관한다. raw_data 내부의 JSON 사전/목록은 변경 가능하므로
    독립된 작업에 복사할 때는 model_copy(deep=True)를 사용한다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TargetResolution(ContractModel):
    """대상 식별 결과. 모든 요청 대상은 확인됨/미확인 중 하나에 속해야 한다.

    requested_targets는 명시적 요청 또는 유효한 대화 문맥에서 정한 조사 범위다.
    실제 등록 여부 확인과 제외 표현 처리는 resolver에서 수행한다.
    """

    requested_targets: UniqueStrings = ()
    resolved_targets: UniqueStrings = ()
    unresolved_targets: UniqueStrings = ()

    @model_validator(mode="after")
    def check_partition(self) -> "TargetResolution":
        resolved = set(self.resolved_targets)
        unresolved = set(self.unresolved_targets)
        if resolved & unresolved:
            raise ValueError("같은 대상을 확인됨과 미확인으로 동시에 분류할 수 없습니다")
        if set(self.requested_targets) != resolved | unresolved:
            raise ValueError("모든 요청 대상을 빠짐없이 분류하고 추가 대상은 포함하지 않아야 합니다")
        return self

    @property
    def needs_clarification(self) -> bool:
        return bool(self.unresolved_targets) or not self.resolved_targets


class EvidenceRecord(ContractModel):
    """수집 자료의 출처와 서비스 연결. analysis는 관측 사실과 별도 종류다.

    runbook의 target도 문서 유형이 아닌 조사 대상 서비스다. reference에는
    호출한 도구 이름 또는 원문 URI를, observed_at에는 수집 시각을 기록한다.
    """

    source_id: NonBlankString
    target: NonBlankString
    source_type: SourceType
    reference: NonBlankString
    observed_at: AwareDatetime
    status: EvidenceStatus
    raw_data: JsonValue
    reason: NonBlankString | None = None

    @model_validator(mode="after")
    def check_collection_result(self) -> "EvidenceRecord":
        if self.status == "ok" and self.raw_data is None:
            raise ValueError("수집 성공에는 원본 데이터가 필요합니다")
        if self.status != "ok" and self.reason is None:
            raise ValueError("미확인 또는 수집 실패 사유가 필요합니다")
        return self


class Fact(ContractModel):
    """검증 대상 사실. source_id가 있다는 것만으로 사실 검증을 통과하지 않는다."""

    target: NonBlankString
    field: NonBlankString
    value: FactValue
    unit: NonBlankString | None = None
    source_id: NonBlankString


class DraftReport(ContractModel):
    """사실과 자유 서술을 분리한 초안. 빈 초안도 작성 중 상태로 허용한다."""

    facts: tuple[Fact, ...] = ()
    hypotheses: tuple[NonBlankString, ...] = ()
    missing_checks: tuple[NonBlankString, ...] = ()
    recommendations: tuple[NonBlankString, ...] = ()


class PendingClarification(ContractModel):
    """짧은 후속 답변을 원래 요청에 연결하기 위한 확인 대기 정보."""

    original_query: NonBlankString
    question: NonBlankString
    reason: Literal["missing_target", "unknown_target", "ambiguous_reference"]
    candidate_targets: UniqueStrings = ()


class ConversationContext(ContractModel):
    """세션별 구조화 문맥. 저장·조회 기능과 최근 관측값의 재사용은 포함하지 않는다."""

    thread_id: NonBlankString
    previous_targets: UniqueStrings = ()
    symptoms: UniqueStrings = ()
    pending_clarification: PendingClarification | None = None


class ValidationIssue(ContractModel):
    code: NonBlankString
    message: NonBlankString
    target: NonBlankString | None = None
    source_id: NonBlankString | None = None
    field: NonBlankString | None = None


class ValidationResult(ContractModel):
    """미검증·통과·실패를 구분한다. 재작성 횟수 정책은 실행부에서 적용한다."""

    status: ValidationStatus = "not_run"
    attempts: int = Field(default=0, ge=0, strict=True)
    issues: tuple[ValidationIssue, ...] = ()

    @model_validator(mode="after")
    def check_verdict(self) -> "ValidationResult":
        if self.status == "not_run":
            if self.attempts != 0 or self.issues:
                raise ValueError("미검증 상태에는 시도 횟수와 검증 오류가 없어야 합니다")
        elif self.attempts == 0:
            raise ValueError("검증 결과에는 1회 이상의 시도 기록이 필요합니다")
        elif self.status == "passed" and self.issues:
            raise ValueError("검증 오류가 남아 있으면 통과할 수 없습니다")
        elif self.status == "failed" and not self.issues:
            raise ValueError("검증 실패에는 구체적인 오류가 필요합니다")
        return self


class ResponseState(ContractModel):
    """요청 처리 결과와 답변 검증 결과를 별개로 표현한다.

    completed 자체는 사실 검증 통과를 뜻하지 않는다(예: 일반 대화).
    검증 실패가 있으면 처리 결과도 validation_failed로 명시해야 한다.
    """

    status: ProcessingStatus
    validation: ValidationResult = Field(default_factory=ValidationResult)
    clarification_question: NonBlankString | None = None

    @model_validator(mode="after")
    def check_response(self) -> "ResponseState":
        if self.status == "needs_clarification" and self.clarification_question is None:
            raise ValueError("확인 대기 상태에는 사용자에게 물을 질문이 필요합니다")
        if (self.status == "validation_failed") != (self.validation.status == "failed"):
            raise ValueError("검증 실패와 처리 상태가 일치해야 합니다")
        return self

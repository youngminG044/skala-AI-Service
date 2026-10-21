# 조사 대상 확인과 기능 검증 가이드

신형 파이프라인(`SUPERVISOR_VERSION=v2`)은 조사 전에 실제 서비스 등록 목록을 확인합니다.
모델 제공자별 API 호출은 유지하면서 대상 식별과 계획 보정 규칙은 공통으로 사용합니다.

## 처리 순서

운영 요청 분류 → 대상 식별 → 확인 질문 또는 계획 → 계획 보정 → 자료 수집 →
누락 확인·제한된 추가 조회 → 보고서 작성 → 처리 상태 표시.

- 서비스 등록 목록은 `data/monitoring.json`의 키입니다. 파일을 읽지 못하면 조사하지 않고 설정 오류를 알립니다.
- 등록된 ID를 독립된 식별자로 확인합니다. `payments-service-3`을 `payments-service`로 축약하지 않습니다.
- 미등록 ID는 `billing-service`, `payments-service-3`, `web-02` 같은 서비스·호스트 표기를 인식합니다.
  임의의 자연어 이름이나 별칭을 자동으로 실제 서비스에 연결하지 않습니다.
- 한글 조사와 함께 쓴 ID도 인식합니다. `auth-service 말고 cache-service만`,
  `auth-service와 payments-service는 제외하고 cache-service` 같은 명시적인 후행 제외 표현을 지원합니다.
  복잡한 부정문·영문 제외 표현 전반을 해석하는 자연어 이해 기능은 포함하지 않습니다.
- 확인된 서비스마다 모니터링 작업은 1개씩 생성합니다. 모델 계획의 누락·중복·요청 외 대상은 보정합니다.
- 미등록 대상만 있거나 대상이 없으면 도구 조회와 보고서 작성을 건너뛰고 질문합니다.
- 등록·미등록 대상이 섞이면 등록 대상만 조사하고 미등록 대상의 정확한 이름을 묻습니다.

## 수집 결과와 상태

도구 호출 성공뿐 아니라 응답 서비스명, 정상 상태 종류, 필수 지표의 유효성도 확인합니다.
정상 수집 자료만 작성 모델에 전달하고 미확인·실패는 별도 기록합니다.
도구에서 미등록 서비스를 반환할 때 CPU·메모리는 `0` 대신 `null`입니다.

모니터링 누락 보완은 **최대 1회**입니다. 기존 일시적 시간 초과 재시도는 각 수집
라운드에서 최대 3번의 도구 호출이므로, 지속적인 시간 초과는 대상당 최대 6번 호출할 수 있습니다.
일반 오류는 라운드당 1번, 총 2번까지 조회합니다. 성공한 서비스는 다시 조회하지 않습니다.

| `metadata.processing_status` | 의미 |
|---|---|
| `needs_clarification` | 조사할 서비스 확인 필요 |
| `completed` | 확인된 요청 범위의 처리 완료 |
| `partial` | 미등록 대상·수집 실패·필수 자료 미확보 또는 등록 목록 오류 |

`completed`는 **사실 검증 통과를 의미하지 않습니다**. `metadata.validation_status`는
현재 `not_run`입니다. 기존 답변 형식 검사와 원본 자료에 대한 사실 검증은 구별합니다.
`validation_failed`는 향후 사실 검증 기능에서 사용합니다.

## 실행

서비스 폴더에서 설치된 가상환경과 로컬 Ollama를 사용합니다.
`qwen3:0.6b`와 `qwen3:8b`가 설치된 상태에서 다음과 같이 실행할 수 있습니다.
이는 기존 세션 DB를 삭제하지 않는 직접 실행 방법입니다.

```bash
ollama serve
```

다른 터미널에서 백엔드를 실행합니다.

```bash
LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python server.py
```

또 다른 터미널에서 프론트엔드를 실행하고 `http://localhost:8600`에 접속합니다.

```bash
.venv/bin/python web_demo_server.py
```

## 기능 확인

```bash
.venv/bin/python -m unittest discover -s tests -v
```

자동 테스트의 모델과 MCP는 주입한 테스트 구현입니다. 공통 규칙·실제 그래프·POST/SSE
응답을 확인하며 실제 모델 성능 측정은 아닙니다. 임시 DB만 사용하고 `.env`는 읽지 않습니다.
OpenAI 어댑터의 공통 계획 규칙 테스트도 API 호출 없이 수행합니다.

실제 Ollama에서는 다음 질문을 확인합니다.

| 질문 | 확인 사항 |
|---|---|
| `payments-service와 cache-service 상태를 확인해줘` | 두 대상의 모니터링 수집 표시 |
| `billing-service 상태를 확인해줘` | 다른 서비스 조회 없이 정확한 이름 질문 |
| `서비스가 느려요. 상태를 확인해줘` | 조사할 서비스 질문 |
| `payments-service-3 상태를 확인해줘` | 상위 서비스로 대체하지 않고 질문 |
| `auth-service 말고 cache-service만 진단해줘` | cache-service만 조사 |
| `payments-service와 billing-service 상태를 확인해줘` | payments 조사와 billing 확인 질문, 일부 완료 |

POST `/chat`의 `metadata`와 SSE `/chat/stream`의 `FinalAnswer.metadata`는 같은 구조입니다.
`target_resolution`에는 요청·확인·미확인 대상을, `monitoring_coverage`에는
조사 예정·성공·미수집 대상을 반환합니다. 화면에도 확인 질문과 일부 완료 상태를 표시합니다.

후속 지칭 해석, 확인 질문의 짧은 답변 연결, 구조화 문맥 영구 저장과 브라우저 복원은
아직 연결하지 않았습니다. 런북 선택은 기존 규칙을 사용하며 사실·근거 검증도 후속 기능입니다.

# OpenAI 서비스별 런북 검색 실행 가이드

v2는 서비스별 관측 알림으로 검색어를 구성한다. OpenAI 임베딩 검색의 상위 두 후보를 OpenAI 빠른 모델이 원문과 함께 재평가한다. 선택 문서를 MCP Resource로 읽은 뒤 서비스·관측 출처를 연결하고, 답변의 사실·인용을 별도로 검증한다.

## 1. 설정

서비스 폴더에서 `.env.example`을 참고해 `.env`에 키를 설정한다. 키를 코드·로그·화면에 붙여 넣지 않는다.

```dotenv
LLM_BACKEND=openai
OPENAI_API_KEY=본인의_API_키
OPENAI_FAST_MODEL=gpt-4o-mini
OPENAI_QUALITY_MODEL=gpt-4o
RUNBOOK_EMBEDDING_BACKEND=openai
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_EMBEDDING_DIMENSIONS=1536
RUNBOOK_RERANK_ENABLED=true
OPENAI_RERANK_MODEL=gpt-4o-mini
LANGCHAIN_TRACING_V2=false
LANGSMITH_TRACING=false
```

## 2. 인덱스 생성

```bash
.venv/bin/python sh/build_runbook_index.py
```

짧은 `data/runbooks/*.md` 문서 하나를 검색 단위 하나로 사용한다. 문서 ID와 본문을 임베딩하고 `data/.cache/runbook_index.json`에 저장한다. 생성 파일은 Git 추적에서 제외한다. `RUNBOOK_INDEX_PATH`로 별도 경로를 지정할 수 있다.

- 첫 생성과 변경 문서 처리에는 OpenAI 비용이 발생한다.
- 동일 문서·모델·차원은 재사용한다. 모델·차원·인덱스 형식 변경 시 전체를 갱신한다.
- 문서 변경이나 인덱스 누락 시 검색 요청이 인덱스를 자동 생성하지 않는다. 위 명령으로 갱신한다.
- 문서와 검색 질문에는 동일한 임베딩 모델·차원을 사용한다.

## 3. 실행

```bash
bash sh/run_agent_openai.sh
```

다른 터미널에서:

```bash
.venv/bin/python web_demo_server.py
```

`http://localhost:8600`에서 `payments-service와 cache-service를 각각 진단해줘`, `web-01 현재 상태를 확인해줘`, `billing-service 상태를 확인해줘` 등을 확인한다. 메시지마다 실제 API 비용이 발생한다.

실행 스크립트는 OpenAI/v2를 선택하고 외부 추적을 끈다. 기존 session.db를 보존하며, 다른 프로세스를 종료하지 않는다. 화면 새로고침·후속 대화 처리는 [세션 유지 가이드](session_continuity.md)를 참고한다.

## 4. 처리 결과 확인

POST `/chat`과 SSE `/chat/stream`의 `metadata.runbook_searches`는 서비스, 검색어, 모니터링 출처 ID, 후보·유사도, 선택 ID, 임베딩 토큰과 재순위화 모델·토큰·시간을 제공한다. `runbook_coverage.services`는 서비스별 원문 확보 상태를 제공한다.

| 상태 | 의미 |
|---|---|
| fetched | 선택한 원문을 모두 확보 |
| not_needed | 정상 상태이고 활성 알림이 없어 검색 생략 |
| no_match | 후보 중 관련 문서를 선택하지 않음 |
| error | API·설정·인덱스·응답 검증 등 검색 실패 |
| fetch_failed | 선택 이후 원문 확보 실패 또는 본문 변경 |

검색어는 해당 서비스의 알림 원문을 우선 사용한다. 무알림 비정상 상태에서는 상태·CPU·메모리 관측을 사용한다. 다른 서비스의 질문·모델 원인 추정은 합치지 않는다. 알림에 포함된 의존 서비스 이름은 원문에 남지만 조사 대상을 추가하지 않는다.

재순위화는 전용 Cross Encoder가 아닌 OpenAI LLM의 후보 적합성 평가다. 후보에 없는 ID·중복 ID·잘못된 응답을 거부하고, 실패하면 해당 서비스의 검색 오류를 표시한다. 검색 후보에 없는 정답은 복구할 수 없다. 문서 관련성은 원인 확정이나 조치 실행을 의미하지 않는다.

`RUNBOOK_RERANK_ENABLED=false`는 임베딩 점수만으로 선택한다. 기본 `RUNBOOK_SCORE_THRESHOLD=0.42`, `RUNBOOK_SCORE_GAP=0.06`이며 유사도는 확률이 아니다. 재순위화 사용 시 두 후보 모두를 평가하므로 이 점수 필터는 최종 선택에 적용하지 않는다. 문서·모델 변경 후에는 재검증한다.

검색 실패·관련 문서 없음은 일부 완료로 표시하되 관측 사실을 유지한다. 정상·무알림 검색 생략은 오류가 아니다. 서비스별 인용 누락, 다른 서비스 인용, 원문·관측값 불일치는 별도 작성 검증에서 거부한다. 최대 세 번 실패하면 관측 사실만 제공한다.

`cost_usd`는 기존 가상 단가 계산값이며 실제 청구액이 아니다. 실제 사용량은 API의 입력·출력 토큰과 검색 메타데이터를 확인한다.

## 5. 기능 테스트

```bash
.venv/bin/python -m unittest discover -s tests -v
```

테스트는 모델과 MCP를 주입하므로 유료 API를 호출하지 않는다. 실제 API 확인 시 전용 테스트 DB와 샘플 데이터를 사용한다.

공식 기준: [임베딩](https://developers.openai.com/api/docs/guides/embeddings), [JSON 모드와 구조 검증](https://developers.openai.com/api/docs/guides/structured-outputs#json-mode).

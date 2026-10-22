# Docker Compose 실행 가이드

상시 서비스는 frontend와 backend 두 개다. 백엔드는 OpenAI/v2·단일 worker로 실행하고 ops/docs MCP는 같은 컨테이너의 stdio 하위 프로세스로 유지한다. 기존 로컬 실행도 사용할 수 있다.

## 1. 최초 준비

Docker Desktop을 실행하고 서비스 폴더에서 작업한다. `.env`가 없으면 `.env.example`을 복사하고 본인 OpenAI 키를 설정한다. 기존 `.env`를 덮어쓰지 않는다. Docker Compose는 이 파일에서 필요한 설정만 읽으며 키·파일을 이미지에 복사하지 않는다.

```bash
cp -n .env.example .env
docker compose config --quiet
docker compose build backend tests
```

`requirements-docker.lock`은 검증한 Linux Python 3.11 의존성의 직접·전이 버전을 고정한다. Dockerfile의 공식 Python 이미지 digest도 고정한다. 호스트 `.venv`를 복사하지 않는다. 의존성 변경 시 이미지를 재빌드하고 테스트한다.

키 값을 출력할 수 있는 `docker compose config` 전체 출력 대신 `--quiet`로 설정 구문만 검사한다. 프런트엔드에는 API 키를 전달하지 않는다.

## 2. 인덱스 최초 생성·갱신

```bash
docker compose run --rm indexer
```

`tools` 프로필의 일회성 서비스이며 명시적으로 실행할 때만 동작한다. 처음에는 런북 6개를 OpenAI 임베딩 API로 처리한다. 변경하지 않은 문서·모델·차원은 재사용한다. 서버 시작·재시작은 인덱스를 생성하지 않는다.

인덱스가 없거나 문서·모델·차원과 맞지 않으면 백엔드 시작이 중단되고 위 명령을 안내한다. 인덱서 실행과 실제 채팅에는 API 비용이 발생한다. 키 존재·로컬 인덱스 사전 검사는 네트워크를 호출하지 않는다.

## 3. 함께 실행·관리

```bash
docker compose up -d --wait
docker compose ps
docker compose logs -f
```

브라우저에서 `http://localhost:8600`을 연다. 다른 호스트 프로그램이 8600을 쓰는 경우 `.env`에 `FRONTEND_PORT=다른_포트`를 지정하고 다시 실행한다. 포트 점유 프로세스를 자동 종료하지 않는다.

- frontend: `127.0.0.1:8600` → 컨테이너 8600, 화면·프록시 제공.
- backend: 호스트 포트를 공개하지 않고 Compose 네트워크의 `backend:8100`으로 연결.
- 두 서비스 모두 UID/GID 10001의 비관리자 사용자로 실행.
- `init: true`와 정상 종료 유예 시간으로 MCP 하위 프로세스 종료·회수를 지원.
- 비정상 종료 시 `unless-stopped` 정책을 사용. unhealthy만으로 자동 재시작되는 것은 아니다.
- 상태 확인은 로컬 `/health` 응답을 검사한다. OpenAI 키의 실제 유효성·할당량·네트워크 정상 여부까지 보장하지 않는다. 상태 확인에 유료 API를 사용하지 않는다.

로그 확인만 종료하려면 Ctrl+C를 누른다. 컨테이너는 계속 실행된다.

```bash
# 두 서비스 재시작
docker compose restart

# 소스 변경 후 이미지 갱신·재생성
docker compose build backend
docker compose up -d --wait

# 컨테이너를 재생성하되 데이터 유지
docker compose up -d --force-recreate --wait

# 함께 종료·컨테이너 및 프로젝트 네트워크 제거, 데이터 볼륨 유지
docker compose down
```

브라우저 연결이 끊긴 뒤에는 대화를 다시 불러와 저장 여부를 확인한다. 요청을 자동으로 재전송하지 않는다.

## 4. 데이터 보존

| 볼륨 | 컨테이너 경로 | 접근 |
|---|---|---|
| sessions | `/state/sessions/session.db` | 백엔드 읽기·쓰기 |
| runbook_index | `/state/index/runbook_index.json` | 백엔드 읽기 전용, 인덱서 읽기·쓰기 |

기존 개인 `session.db`·호스트 검색 캐시를 자동 가져오지 않는다. Docker 전용 새 DB를 사용한다. `.env`의 호스트 SESSION_DB_PATH·RUNBOOK_INDEX_PATH는 Compose가 고정한 컨테이너 경로로 대체한다. 기존 파일은 삭제하지 않는다.

Compose 프로젝트 이름에 따라 볼륨이 분리된다. 같은 데이터를 다시 사용하려면 같은 폴더·프로젝트 이름으로 시작한다. 다른 프로젝트 이름은 별도 대화 저장소다. 일반 종료에는 볼륨 삭제 옵션을 사용하지 않는다.

백업은 SQLite backup API로 일관된 사본을 만든 다음 복사한다.

```bash
mkdir -p .backups
docker compose exec -T backend python -c "import sqlite3; s=sqlite3.connect('/state/sessions/session.db'); d=sqlite3.connect('/tmp/session-backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp backend:/tmp/session-backup.db .backups/session-backup.db
```

반복 백업은 호스트 파일 이름을 바꿔 보관한다. `.backups/`는 Git과 Docker 빌드에서 제외한다. 기존 DB 복원·이관은 서버를 중지하고 백업을 확보한 상태에서 별도 작업으로 수행한다.

## 5. 자동 테스트

```bash
docker compose build tests
docker compose run --rm tests
```

테스트 컨테이너에는 API 키나 운영 볼륨을 전달하지 않는다. 모델·MCP 주입 테스트이므로 유료 호출이 없다. 실제 OpenAI 확인에는 샘플 데이터와 별도 Compose 프로젝트 이름을 사용하고 생성한 사용자 요청 수·토큰을 구분해 기록한다.

기존 방식도 유지한다.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 6. 문제 대응

| 증상 | 확인·조치 |
|---|---|
| Docker daemon 연결 실패 | Docker Desktop 기동 확인 |
| 이미지 다운로드 지연 | Docker Hub 연결·Docker 자격 증명 도구 확인. 프로젝트 소스 오류와 구분 |
| 설정 오류: API 키 | `.env`의 키 누락·예제 값 확인. 키를 로그·화면에 출력하지 않음 |
| 인덱스 설정 오류 | `docker compose run --rm indexer` 후 다시 기동 |
| 볼륨 쓰기 권한 오류 | 이미지의 UID/GID와 볼륨 소유권 확인. 데이터 삭제로 해결하지 않음 |
| frontend 대기·unhealthy | `docker compose logs backend frontend`로 기동·통신 확인 |
| 대화가 비어 있음 | Compose 프로젝트 이름과 볼륨 연결 확인 |
| partial·validation_failed | 기존 [실행 가이드](execution_guide.md)의 응답 상태 해석 적용 |

현재 범위는 로컬 단일 백엔드다. Docker 적용이 인증·다중 서버 동시 처리·공개 배포를 추가하는 것은 아니다. Vue 화면의 CDN 로드와 OpenAI API 사용에는 인터넷 연결이 필요하다.

공식 참고: [Compose 기동 순서](https://docs.docker.com/compose/how-tos/startup-order/), [일회성 프로필](https://docs.docker.com/reference/compose-file/profiles/).

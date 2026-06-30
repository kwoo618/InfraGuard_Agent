# Contributing to InfraGuard Agent

InfraGuard Agent에 기여해주셔서 감사합니다.
이 문서는 4인 팀의 협업 규칙과 개발 절차를 설명합니다.

---

## 팀 역할 분담

| 팀원 | 역할 | 담당 파일 |
|---|---|---|
| 이하은 | 부하 생성 | `tools/run_load_test.py`, `infra/locust/` |
| 박정기 | 진단 에이전트 | `app/agent/`, `tools/generate_plan.py` |
| 최강우 | 스케일링 & 인프라 | `tools/get_metrics.py`, `tools/scale_service.py`, `infra/`, `docker-compose.yml` |
| 최소명 | API & UI | `app/api/`, `app/static/`, `app/main.py` |

> 담당 파일 외 영역을 수정할 때는 해당 담당자에게 먼저 확인하세요.

---

## 브랜치 전략

이 프로젝트는 **GitHub Flow**를 사용합니다.

- `main` 브랜치는 항상 동작 가능한 상태를 유지합니다.
- 모든 작업은 `main`에서 브랜치를 따서 진행하고, PR을 통해 `main`으로 병합합니다.
- `main`에 직접 push하는 것은 금지되어 있습니다.

---

## 브랜치 네이밍

```
<타입>/issue-번호-짧은-설명
```

| 타입 | 용도 |
|---|---|
| `feature` | 새 기능 |
| `fix` | 버그 수정 |
| `chore` | 설정, 인프라, 의존성 |
| `docs` | 문서 |
| `refactor` | 리팩토링 |

**예시**

```
feature/12-locust-tool-runner          # 이하은
feature/23-react-loop-engine           # 박정기
feature/34-prometheus-metrics-tool     # 최강우
feature/45-sse-streaming-api           # 최소명
fix/41-locust-result-parsing-error     # 이하은
chore/3-docker-compose-infra           # 최강우
```

---

## 이슈

새 작업을 시작하기 전에 이슈를 먼저 생성하세요.

**라벨**: `feature` / `bug` / `chore` / `docs` / `refactor`

**제목 형식**: 동사 원형으로 시작하는 간결한 설명

```
# 좋은 예
Locust Tool 실행 및 결과 파싱 구현
Prometheus 쿼리 타임아웃 수정
Docker Compose 인프라 구성
ReAct Loop MAX_LOOP 가드레일 추가
SSE 스트리밍 엔드포인트 구현

# 나쁜 예
에이전트
버그
작업중
```

---

## PR(Pull Request)

### 기본 규칙

- PR 제목은 이슈 제목과 동일하게 작성합니다.
- 본문에 `closes #이슈번호`를 포함해 이슈와 연결합니다.
- 팀원 **1명 이상**의 승인을 받아야 merge할 수 있습니다.
- 로컬에서 `pytest` 통과 확인 후 PR을 올립니다.

### merge 전략

- 모든 브랜치 → main: **Squash merge** (커밋 히스토리 정리)

### PR 체크리스트

```markdown
## 변경 내용
<!-- 무엇을 왜 바꿨는지 -->

## 관련 이슈
closes #

## 변경 타입
- [ ] feature (새 기능)
- [ ] fix (버그 수정)
- [ ] refactor (리팩토링)
- [ ] chore (설정, 인프라, 의존성)
- [ ] docs (문서)

## 체크리스트
- [ ] 브랜치 네이밍 규칙을 따랐다
- [ ] 커밋 메시지 규칙을 따랐다
- [ ] 로컬에서 pytest가 통과한다
- [ ] docker compose up -d 후 기본 동작을 확인했다
- [ ] 불필요한 코드(print, 임시 주석 등)를 제거했다
- [ ] MAX_LOOP, 타임아웃 등 가드레일이 유지된다
- [ ] HITL 지점(스케일링 실행 전 승인)이 우회되지 않는다
- [ ] 담당 파일 외 영역 수정 시 해당 담당자에게 확인했다
```

---

## 개발 환경 설정

```bash
# 저장소 클론
git clone https://github.com/jogeulling/UpStage_Project.git
cd UpStage_Project

# Python 의존성 설치
pip install -r backend/requirements.txt

# 환경변수 설정
cp .env.example .env
# .env에 UPSTAGE_API_KEY 입력 (필수)

# 인프라 구동
docker compose up -d

# 테스트 실행
pytest tests/

# 백엔드 서버 실행
cd backend && uvicorn app.main:app --reload --port 8000
```

---

## 커밋 메시지

```
<타입>: <변경 내용을 동사 원형으로>

# 예시
feat: Locust Tool 실행 및 결과 파싱 구현
feat: ReAct Loop 엔진 MAX_LOOP 가드레일 추가
feat: Prometheus 메트릭 수집 Tool 구현
feat: SSE 스트리밍 /start 엔드포인트 구현
fix: Locust 결과 파싱 오류 수정
chore: Docker Compose 인프라 구성
docs: API 명세 업데이트
refactor: AgentState dataclass 구조 개선
```

---

## 인터페이스 계약 (Interface Contract)

역할 간 데이터를 주고받는 구조체입니다. **변경 시 반드시 전체 팀에 공유하세요.**

```python
# 이하은 → 박정기
@dataclass
class LoadTestResult:
    tps: float
    latency_p95: float   # ms
    error_rate: float    # 0.0 ~ 1.0
    duration: int        # 초

# 최강우 → 박정기
@dataclass
class SystemMetrics:
    cpu_pct: float
    mem_pct: float
    connection_count: int
    timestamp: str

# 박정기 → 최소명
@dataclass
class BottleneckReport:
    cause: str
    severity: str        # low | medium | high
    recommendation: str
    confidence: float    # 0.0 ~ 1.0

# 최강우 → 최소명
@dataclass
class ScalingResult:
    before_replicas: int
    after_replicas: int
    success: bool
```

---

## 로컬 부하 테스트 시 주의사항

하나의 PC에서 Locust + 대상 서버 + Prometheus + 에이전트 백엔드를 동시에 구동하므로 **CPU 고갈**에 주의합니다.

- 기본 목표 TPS는 **30~50 TPS**로 설정합니다.
- Locust 실행 전 `docker stats`로 현재 리소스 사용량을 확인합니다.
- 테스트 후 `docker compose down`으로 컨테이너를 정리합니다.

---

## 면책 조항

InfraGuard Agent는 인프라 진단 및 최적화 제안 도구이지만,
자동 스케일링 결과에 대한 법적·운영적 책임을 보장하지 않습니다.
프로덕션 환경 적용 전 반드시 전문가 검토를 받으시기 바랍니다.
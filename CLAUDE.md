# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

**InfraGuard Agent** — Locust로 실제 부하를 직접 만들어 시스템 한계를 측정하고,
AI 에이전트가 TPS·Latency 데이터를 해석해 스스로 인프라를 최적화하는 자율 진단 파이프라인.

사후 알림이 아닌 **사전 진단 + 자율 판단 + HITL 승인** 후 실행이 핵심 가치다.

Claude Code 응답 언어: 한국어

## 팀 구성

- 원 개발: 대구대 부트캠프 2팀 4인 (이하은 부하 생성, 박정기 진단 에이전트, 최강우 스케일링·인프라, 최소명 API·UI)
- 현재 (2026 우수작품경진대회 출품): 최강우, 최소명 2인

| 팀원 | 역할 |
|---|---|
| 최강우 | 추가 개발 전체 (백엔드·에이전트·인프라·UI) |
| 최소명 | PR 리뷰, 발표 자료 |

> 파일별 소유권 규칙은 없다. 누구든 수정할 수 있으나, 인터페이스(schemas.py/state.py) 변경은 PR에 명시한다.

## 커맨드

> **요구사항: Python 3.11+ 필수** — `engine.py`가 `asyncio.timeout`(3.11 신규 API)을 사용한다. 3.10 이하에서는 engine 테스트가 전부 실패한다.

```bash
# 의존성 설치
pip install -r backend/requirements.txt

# 전체 테스트
pytest tests/

# 단위 테스트
pytest tests/unit/test_load_runner.py -v
pytest tests/unit/test_agent.py -v
pytest tests/unit/test_metrics.py -v
pytest tests/unit/test_prompts.py -v
pytest tests/unit/test_api.py -v
pytest tests/unit/test_run_history.py -v
pytest tests/unit/test_replica_reset.py -v
pytest tests/unit/test_run_gate.py -v
pytest tests/unit/test_grafana_config.py -v
pytest tests/unit/test_saved_results.py -v

# FastAPI 백엔드 서버 실행
cd backend && uvicorn app.main:app --reload --port 8000

# Docker Compose로 전체 인프라 구동
docker compose up -d

# 개별 서비스 확인
open http://localhost:8000   # FastAPI + UI
open http://localhost:8089   # Locust 대시보드
open http://localhost:9090   # Prometheus
open http://localhost:3000   # Grafana
```

## 파일 구조

```
InfraGuard_Agent
├── backend
│   ├── app
│   │   ├── agent
│   │   │   ├── engine.py          # ReAct Loop 오케스트레이터
│   │   │   ├── state.py           # Agent 런타임 상태 정의
│   │   │   ├── prompts.py         # 시스템 프롬프트
│   │   │   └── nodes.py           # LLM reasoning node
│   │   ├── tools
│   │   │   ├── run_load_test.py   # Locust 실행 + 결과 파싱 (헤드라인은 종료 시 통계, + 그래프용 초 단위 시계열·엔드포인트 통계)
│   │   │   ├── get_metrics.py     # Prometheus 쿼리 (활성 연결 수, 서버별 요청 수)
│   │   │   ├── scale_service.py   # Docker replica 조정
│   │   │   └── generate_plan.py   # 최적화 플랜 생성
│   │   ├── api
│   │   │   └── v1
│   │   │       ├── agent.py       # /start /approve /report /replicas
│   │   │       ├── run_history.py # 라운드별 측정 이력·그래프 데이터 + results/ 결과 파일 저장
│   │   │       └── saved_results.py # 저장된 결과 파일(docs/evidence) 읽기 전용 조회 + /report 모양 변환 (#93)
│   │   ├── static                 # SSE UI + HITL 버튼 + 결과 패널
│   │   │   ├── index.html
│   │   │   ├── main.js
│   │   │   ├── result_panel.js    # 결과 패널 공용 계산 (자릿수·SLO 판정·요약·결론 규칙, #75 #93)
│   │   │   ├── toolbar.css        # 상단 도구 막대 (보기 전환·저장 기록 불러오기, #93)
│   │   │   ├── view_switch.js     # 기본 보기 / 상세 보기 전환 ?view=simple|detail, 기본 simple (#93)
│   │   │   ├── saved_results.js   # 저장된 실행 기록 불러오기 (#93)
│   │   │   └── refresh            # 결과 패널 보기 (#93)
│   │   │       ├── common.css / common.js   # 두 보기 공용 구조·뷰모델(결론 한 줄·용어 풀이)·차트
│   │   │       ├── c.css / c.js             # 기본 보기 (C 미니멀형, 한눈에)
│   │   │       └── a.css / a.js             # 상세 보기 (A 관제 콘솔형, 전체 정보)
│   │   └── main.py                # FastAPI 앱 진입점
│   ├── Dockerfile
│   └── requirements.txt
├── infra
│   ├── locust
│   │   ├── locustfile.py          # 부하 시나리오 + 요청별 원시 기록 훅 (--csv prefix 옆 _requests.csv) + 종료 시 통계 (_final_stats.json, #85)
│   │   └── locust.conf            # 부하 기본 설정 (동시 가상 사용자 기본값 50)
│   ├── nginx
│   │   └── nginx.conf             # target-server 로드밸런서 (localhost:8080)
│   ├── prometheus
│   │   └── prometheus.yml
│   ├── grafana
│   │   ├── dashboard.json         # 대시보드 원본 (홈 대시보드, 익명 보기 전용)
│   │   └── provisioning           # 데이터소스(uid "prometheus")·대시보드 자동 로드 (#74)
│   │       ├── datasources/prometheus.yml
│   │       └── dashboards/infraguard.yml
│   └── target-server              # 부하 받을 샘플 앱
│       ├── main.py                # latency 히스토그램 버킷 LATENCY_BUCKETS (#74)
│       ├── requirements.txt       # 재빌드 재현용 버전 고정 (#74)
│       └── Dockerfile
├── tests
│   ├── unit
│   │   ├── test_load_runner.py
│   │   ├── test_metrics.py
│   │   ├── test_prompts.py
│   │   ├── test_agent.py
│   │   ├── test_api.py
│   │   ├── test_run_history.py
│   │   ├── test_replica_reset.py
│   │   ├── test_run_gate.py       # 동시 실행 방지 (#87)
│   │   ├── test_grafana_config.py # Grafana 설정·버킷 일관성 (#74)
│   │   └── test_saved_results.py  # 저장된 결과 불러오기 API·경로 검증 (#93)
│   └── integration
│       └── test_e2e.py
├── results                        # 실행 결과 JSON (gitignore). 발표 증빙은 골라서 docs/evidence/로 옮긴다
├── docker-compose.yml
├── .env.example
├── CLAUDE.md
├── CONTRIBUTING.md
└── README.md
```

## 아키텍처

계층 구조는 단방향 의존성을 따른다:
`static(SSE UI)` → `api(FastAPI)` → `agent(ReAct Loop)` → `tools` → `infra`

### 핵심 데이터 흐름

```
run_load_test    →  LoadTestResult(tps, latency_p95, error_rate, duration)
get_metrics      →  SystemMetrics(cpu_pct, mem_pct, connection_count, timestamp)
nodes.py(LLM)   →  BottleneckReport(cause, severity, recommendation, confidence)
scale_service    →  ScalingResult(before_replicas, after_replicas, success)
```

### State 구조 (state.py)

```python
from typing import Literal, TypedDict

from app.schemas import (
    BottleneckReport,
    LoadTestResult,
    ScalingResult,
    SystemMetrics,
)


AgentOutcome = Literal[
    "pending",
    "diagnosed",
    "awaiting_approval",
    "scaled",
    "failed",
]


class AgentRuntimeState(TypedDict):
    task_id: str
    target_tps: int
    duration: int

    load_test_result: LoadTestResult | None
    system_metrics: SystemMetrics | None
    bottleneck_report: BottleneckReport | None

    agent_outcome: AgentOutcome

    scaling_plan: dict[str, object] | None
    optimization_plan: list[str]
    scaling_required: bool
    scaling_approved: bool | None
    waiting_for_approval: bool
    scaling_result: ScalingResult | None

    loop_count: int
    scaling_count: int

    final_answer: str | None
    error: str | None
```

### API 명세

| 메서드 | 경로 | 담당 | 설명 |
|---|---|---|---|
| GET | `/api/v1/agent/start` | 최소명 | TPS(동시 가상 사용자 수)·duration 수신, 에이전트 루프 시작, SSE 스트리밍 (EventSource라 GET. 코드 기준으로 정정). 다른 실행(승인 대기 포함)·끊긴 실행의 부하 테스트·서버 수 초기화가 진행 중이면 409 (#87) |
| POST | `/api/v1/agent/approve` | 최소명 | HITL 스케일링 승인. 저신뢰 제안은 `acknowledge_low_confidence=true` 필요 (#83) |
| GET | `/api/v1/agent/report/{task_id}` | 최소명 | 최종 종합 분석 리포트 반환. Phase 3·4에서 필드 추가 (기존 필드 유지) |
| GET | `/api/v1/agent/replicas` | 최강우 | 현재 target-server replica 수(docker compose 실제 값)와 실행 중 여부 `busy`, 이유 `busy_reason`(`agent_running` / `load_test_running` / `replica_reset`, #87) |
| POST | `/api/v1/agent/replicas/reset` | 최강우 | 측정 회차 사이 1대 초기화. 에이전트 실행·승인 대기 중이거나 끝나지 않은 부하 테스트가 있으면 409. results/에 기록하지 않음 |
| GET | `/api/v1/results/saved` | 최강우 | `docs/evidence/` 결과 파일 목록 (읽기 전용, 파일명 순, 읽지 못한 파일은 `error` 표시, #93) |
| GET | `/api/v1/results/saved/{file_name}` | 최강우 | 결과 파일 하나를 `/report` 모양(`report`)과 파일 정보(`saved`)로 반환. 이름 규칙 불일치 400, evidence 밖·없는 파일 404 (#93) |

## HITL(Human-In-The-Loop) 지점

반드시 사람 승인을 받아야 하는 지점:

1. **컨테이너 스케일링 실행 전** — `replica를 2→4로 늘리겠습니다. 실행할까요?`
2. **진단 결과 불확실 시** — LLM confidence < 0.6인 스케일링 제안은 승인 전 확인 게이트를 건다.
   경고와 "낮은 신뢰도를 확인했습니다" 체크 없이는 승인할 수 없고, `/approve`도 `acknowledge_low_confidence` 없이 승인하면 400이다.
   거절은 항상 가능하다 (#83, docs/02 ISSUE-13). 설계 초안의 "추가 정보 요청" 흐름은 구현하지 않았다.
3. **외부 비용 발생 작업** — 클라우드 프로비저닝은 MVP 범위 밖, 로컬 Docker만

HITL 없이 자율 실행 가능: 부하 테스트 실행, 메트릭 수집, 병목 진단

## 가드레일

- `MAX_LOOP = 10` — ReAct Loop 최대 반복 횟수. 초과 시 `agent_outcome = "failed"` 처리
- Locust 부하 상한 **동시 가상 사용자 50** — 로컬 환경 CPU 고갈 방지. `run_load_test.py`가 target_tps를 Locust `--users`로 넘기고 `MAX_TPS = 50`으로 막는다 (`locust.conf`의 users 기본값도 50). 처리량(TPS) 상한이 아니다 — 스케일 후 실측 98.5 TPS (docs/02 ISSUE-11)
- `scale_service` replica 최대 **8개**
- 에이전트 루프 `asyncio.timeout(300)` — 5분 초과 시 강제 종료
- 저신뢰 확인 게이트 기준 `LOW_CONFIDENCE_THRESHOLD = 0.6` — `backend/app/api/v1/agent.py` 코드 상수(환경변수 아님). 0.6 미만만 게이트 대상

## LLM 설정

- 모델: **Solar Pro** (Upstage)
- Tool Use 방식: ReAct Loop (Thought → Action → Observation 반복)
- 관측성: **Langfuse** 트레이싱 — 설계 목표. 2026-09-12 기준 코드 연동 없음 (`requirements.txt`·`.env.example`에만 있고 `backend/app`에서 쓰지 않는다, docs/02 ISSUE-18)
- 평가: **LLM-as-Judge** — 최종 병목 진단 리포트 정확성 정량 검증

## 환경변수 (.env)

```bash
UPSTAGE_API_KEY=        # Solar Pro API 키 (필수)
LANGFUSE_SECRET_KEY=    # Langfuse 트레이싱 (선택)
LANGFUSE_PUBLIC_KEY=
PROMETHEUS_URL=http://localhost:9090
MAX_LOOP=10
TARGET_SERVER_URL=http://localhost:8080
P95_SLO_MS=1000                # P95 SLO(ms), 진단 프롬프트 판단 기준 (docs/02 ISSUE-10)
DEBUG_ENDPOINTS_ENABLED=false  # 디버그 전용(force_scaling, UI는 ?debug=1일 때만 요청), 발표·측정 시 반드시 false
```

## 테스트 작성 시 주의사항

- Locust Tool 테스트는 `subprocess.Popen` Mock으로 처리 (실제 Locust 프로세스 실행 X)
- Prometheus 쿼리 테스트는 `httpx.MockTransport`로 HTTP 목킹
- Docker Compose 제어 테스트는 `docker` SDK Mock 사용
- 모든 테스트는 `tmp_path` 픽스처 사용 (`tempfile.TemporaryDirectory` 대신)

## Out of Scope (건드리지 말 것)

- 실제 AWS/GCP 클라우드 프로비저닝 — 로컬 Docker로만 증명
- Kubernetes HPA 연동 — Docker Compose 수준에서 먼저 구현
- 멀티 에이전트 구조 — 단일 ReAct Loop MVP 완성 후 확장
- 실시간 FinOps 비용 최적화 — 2단계 목표

## 현재 단계: 우수작품경진대회 추가 개발 (제출 9/15)

작업 전 반드시 `docs/00_작업가이드.md`와 해당 작업 문서를 먼저 읽는다.
측정하지 않은 수치를 코드·UI·문서에 넣지 않는다. 가드레일 값 변경 금지.
문서와 코드가 다르면 코드가 기준이며, 차이를 보고한다.
발표 수치는 `docs/04_발표용측정결과.md`(증빙 `docs/evidence/`)만 쓴다. 측정 정확성 수정(#85 #87 #88) 전 측정과 합치지 않는다.

### 절대 원칙
- 측정하지 않은 수치를 코드·UI·문서에 하드코딩하거나 예시값을 실측처럼 표시하지 않는다.
- 가드레일 값(MAX_LOOP 10, Locust 동시 가상 사용자 50, replica 8, timeout 300, confidence 0.6)은 변경 금지.
- 기존 API 응답 필드는 삭제·이름 변경 금지 (추가만 허용).
- 변경 후 반드시 `pytest tests/` 통과 확인.

### 알려진 이슈
- ~~prometheus.yml이 target-server 단일 타깃이라 스케일 후 메트릭이 과소집계됨~~ → **해결됨** (Phase 1, dns_sd_configs 적용. 2026-09-11 replica 3개 모두 UP 확인, docs/02 ISSUE-2)
- ~~스케일 아웃해도 부하가 replica 1개로만 감 / 재생성 후 호스트 8080이 비어 부하 대상이 사라짐~~ → **해결됨** (Phase 2, nginx 로드밸런서가 호스트 8080 고정, target-server replica는 호스트 포트 없음. 2026-09-11 replica 3개 균등 분산·재생성 후 8080 유지 확인, docs/02 ISSUE-1·9)
- ~~LLM 진단 입력에 CPU/메모리 0% 고정값이 측정값처럼 들어가고 P95 판단 기준이 없음~~ → **해결됨** (#78, 미수집 항목 "측정 불가" 표시 + `P95_SLO_MS` 기본 1000ms. 2026-09-11 5회 모두 CPU/메모리를 근거에서 제외·SLO 언급 확인, target_tps 50 3회 중 2회 스케일링 제안, docs/02 ISSUE-10)
- ~~신뢰도 0.6 미만 HITL이 설계 문서에만 있고 코드에 없음~~ → **해결됨** (#83, 승인 전 확인 게이트 + `/approve` 400. 단위 테스트로 검증, 실측 confidence는 80~95%라 UI 수동 검증 없음, docs/02 ISSUE-13)
- Windows Docker Desktop에서 cAdvisor `name` 라벨 미지원 → CPU/Mem/Replica 패널이 비어 있었다. Grafana CPU·메모리 패널은 제거했다(Phase 5, #74). Replica 패널은 `count(up{job="target-server"} == 1)`. LLM 입력에는 "측정 불가"로 표시 (#78, docs/02 ISSUE-3)
- ~~LLM 프롬프트가 target_tps(동시 가상 사용자 수)를 "목표 TPS"로 표기해 LLM이 처리량 목표로 읽음~~ → **해결됨** (#88, 프롬프트 "동시 가상 사용자 {n}명 (Locust 동시 접속 수, 처리량 목표 아님)", "목표 TPS 달성" 분석 항목·조치 제거. UI 입력 라벨과 결과 패널은 Phase 4에서 "동시 가상 사용자 수"로 바꿨다. API 파라미터 이름 `target_tps`는 유지. 2026-09-12 발표용 측정 결과 파일에 "목표 TPS"·"미달" 문구 없음, docs/02 ISSUE-11)
- ~~Grafana datasource/dashboard 프로비저닝 설정 없음 (수동 import 필요)~~ → **해결됨** (Phase 5, #74. 프로비저닝 + 익명 보기 전용 + 홈 대시보드, `grafana/grafana:13.1.0` 고정. 에러율 라벨·P95 1초 상한도 수정. 2026-09-12 `down -v` 후 로그인 없이 표시, Grafana P95가 Locust P95와 버킷 해상도 범위 안, docs/02 ISSUE-4·7·8)
- e2e(`tests/integration/test_e2e.py`)는 LLM이 스케일링을 제안하면 실패한다. httpx `ASGITransport`가 SSE를 앱 종료까지 버퍼링해 승인 대기에서 교착한다 (docs/02 ISSUE-12, #80)
- ~~실행 중 SSE 스트림이 끊기면 Locust가 끝까지 돌아 다음 실행과 겹치면 측정이 오염됨~~ → **해결됨** (#87, 실행 중·승인 대기·끊긴 실행의 부하 테스트가 남아 있으면 시작·초기화 409, UI 버튼 비활성. 끊긴 실행의 Locust는 강제로 끝내지 않고 끝날 때까지 기다린다. 2026-09-12 UI 차단 확인, 끊긴 실행 차단은 단위 테스트로만 확인, docs/02 ISSUE-17)
- ~~Locust `_stats.csv`가 부하 마지막 약 1초를 빠뜨려 헤드라인 값이 적은 요청으로 계산됨~~ → **해결됨** (#85, locustfile이 종료 시 통계 `_final_stats.json`을 쓰고 헤드라인을 여기서 계산, 결과 파일에 `headline_source` 기록. 2026-09-12 발표용 측정 레코드 13개 모두 ① 초별 합 = 엔드포인트 합 = total_requests, docs/02 ISSUE-15)
- run_load_test가 Locust 출력을 cp949로 읽어 연결 불가 시 stderr가 사라지고, 같은 조건에서 요청 0건 LoadTestResult를 정상 반환한다 (docs/02 ISSUE-16, #84)
- LLM 진단 응답에 필수 필드(`requires_scaling` 등)가 빠지면 추측하지 않고 실행을 실패로 끝낸다(설계대로). 진단 응답 원문은 결과 파일·로그에 남지 않고 재시도도 없다 — 2026-09-12 발표용 측정 5명 2회 중 1회 실측. 재시도·원문 보존은 대회 이후 과제 (docs/02 ISSUE-18)

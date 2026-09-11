# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

**InfraGuard Agent** — Locust로 실제 부하를 직접 만들어 시스템 한계를 측정하고,
AI 에이전트가 TPS·Latency 데이터를 해석해 스스로 인프라를 최적화하는 자율 진단 파이프라인.

사후 알림이 아닌 **사전 진단 + 자율 판단 + HITL 승인** 후 실행이 핵심 가치다.

## 팀 역할 분담

| 팀원 | 역할 | 담당 파일 |
|---|---|---|
| 이하은 | 부하 생성 | `tools/run_load_test.py`, `infra/locust/` |
| 박정기 | 진단 에이전트 | `app/agent/`, `tools/generate_plan.py` |
| 최강우 | 스케일링 & 인프라 | `tools/get_metrics.py`, `tools/scale_service.py`, `infra/`, `docker-compose.yml` |
| 최소명 | API & UI | `app/api/`, `app/static/`, `app/main.py` |

## 커맨드

> **요구사항: Python 3.11+ 필수** — `engine.py`가 `asyncio.timeout`(3.11 신규 API)을 사용한다. 3.10 이하에서는 engine 테스트가 전부 실패한다.

```bash
# 의존성 설치
pip install -r backend/requirements.txt

# 전체 테스트
pytest tests/

# 역할별 단위 테스트
pytest tests/unit/test_load_runner.py -v   # 이하은
pytest tests/unit/test_agent.py -v         # 박정기
pytest tests/unit/test_metrics.py -v       # 최강우
pytest tests/unit/test_api.py -v           # 최소명

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

## 파일 구조 및 소유권

```
InfraGuard_Agent
├── backend
│   ├── app
│   │   ├── agent
│   │   │   ├── engine.py          # 박정기 - ReAct Loop 오케스트레이터
│   │   │   ├── state.py           # 박정기 - Agent 런타임 상태 정의
│   │   │   ├── prompts.py         # 박정기 - 시스템 프롬프트
│   │   │   └── nodes.py           # 박정기 - LLM reasoning node
│   │   ├── tools
│   │   │   ├── run_load_test.py   # 이하은 - Locust 실행 + 결과 파싱
│   │   │   ├── get_metrics.py     # 최강우 - Prometheus 쿼리
│   │   │   ├── scale_service.py   # 최강우 - Docker replica 조정
│   │   │   └── generate_plan.py   # 박정기 - 최적화 플랜 생성
│   │   ├── api
│   │   │   └── v1
│   │   │       └── agent.py       # 최소명 - /start /approve /report
│   │   ├── static                 # 최소명 - SSE UI + HITL 버튼
│   │   │   ├── index.html
│   │   │   ├── main.js
│   │   │   └── style.css
│   │   └── main.py                # 최소명 - FastAPI 앱 진입점
│   ├── Dockerfile
│   └── requirements.txt
├── infra
│   ├── locust
│   │   ├── locustfile.py          # 이하은 - 부하 시나리오
│   │   └── locust.conf            # 이하은 - 부하 기본 설정 (동시 가상 사용자 기본값 50)
│   ├── nginx
│   │   └── nginx.conf             # 최강우 - target-server 로드밸런서 (localhost:8080)
│   ├── prometheus
│   │   └── prometheus.yml         # 최강우
│   ├── grafana
│   │   └── dashboard.json         # 최강우
│   └── target-server              # 최강우 - 부하 받을 샘플 앱
│       ├── main.py
│       └── Dockerfile
├── tests
│   ├── unit
│   │   ├── test_load_runner.py    # 이하은
│   │   ├── test_metrics.py        # 최강우
│   │   ├── test_agent.py          # 박정기
│   │   └── test_api.py            # 최소명
│   └── integration
│       └── test_e2e.py
├── docker-compose.yml             # 최강우
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
| POST | `/api/v1/agent/start` | 최소명 | TPS·duration 수신, 에이전트 루프 시작, SSE 스트리밍 |
| POST | `/api/v1/agent/approve` | 최소명 | HITL 스케일링 승인 |
| GET | `/api/v1/agent/report/{task_id}` | 최소명 | 최종 종합 분석 리포트 반환 |

## HITL(Human-In-The-Loop) 지점

반드시 사람 승인을 받아야 하는 지점:

1. **컨테이너 스케일링 실행 전** — `replica를 2→4로 늘리겠습니다. 실행할까요?`
2. **진단 결과 불확실 시** — LLM confidence < 0.6이면 추가 정보 요청
3. **외부 비용 발생 작업** — 클라우드 프로비저닝은 MVP 범위 밖, 로컬 Docker만

HITL 없이 자율 실행 가능: 부하 테스트 실행, 메트릭 수집, 병목 진단

## 가드레일

- `MAX_LOOP = 10` — ReAct Loop 최대 반복 횟수. 초과 시 `agent_outcome = "failed"` 처리
- Locust 부하 상한 **동시 가상 사용자 50** — 로컬 환경 CPU 고갈 방지. `run_load_test.py`가 target_tps를 Locust `--users`로 넘기고 `MAX_TPS = 50`으로 막는다 (`locust.conf`의 users 기본값도 50). 처리량(TPS) 상한이 아니다 — 스케일 후 실측 98.5 TPS (docs/02 ISSUE-11)
- `scale_service` replica 최대 **8개**
- 에이전트 루프 `asyncio.timeout(300)` — 5분 초과 시 강제 종료

## LLM 설정

- 모델: **Solar Pro** (Upstage)
- Tool Use 방식: ReAct Loop (Thought → Action → Observation 반복)
- 관측성: **Langfuse** 트레이싱 — 모든 LLM 호출 추적
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
DEBUG_ENDPOINTS_ENABLED=false  # 디버그 전용(force_scaling), 발표·측정 시 반드시 false
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

### 절대 원칙
- 측정하지 않은 수치를 코드·UI·문서에 하드코딩하거나 예시값을 실측처럼 표시하지 않는다.
- 가드레일 값(MAX_LOOP 10, Locust 동시 가상 사용자 50, replica 8, timeout 300, confidence 0.6)은 변경 금지.
- 기존 API 응답 필드는 삭제·이름 변경 금지 (추가만 허용).
- 변경 후 반드시 `pytest tests/` 통과 확인.

### 알려진 이슈
- ~~prometheus.yml이 target-server 단일 타깃이라 스케일 후 메트릭이 과소집계됨~~ → **해결됨** (Phase 1, dns_sd_configs 적용. 2026-09-11 replica 3개 모두 UP 확인, docs/02 ISSUE-2)
- ~~스케일 아웃해도 부하가 replica 1개로만 감 / 재생성 후 호스트 8080이 비어 부하 대상이 사라짐~~ → **해결됨** (Phase 2, nginx 로드밸런서가 호스트 8080 고정, target-server replica는 호스트 포트 없음. 2026-09-11 replica 3개 균등 분산·재생성 후 8080 유지 확인, docs/02 ISSUE-1·9)
- ~~LLM 진단 입력에 CPU/메모리 0% 고정값이 측정값처럼 들어가고 P95 판단 기준이 없음~~ → **해결됨** (#78, 미수집 항목 "측정 불가" 표시 + `P95_SLO_MS` 기본 1000ms. 2026-09-11 5회 모두 CPU/메모리를 근거에서 제외·SLO 언급 확인, target_tps 50 3회 중 2회 스케일링 제안, docs/02 ISSUE-10)
- Windows Docker Desktop에서 cAdvisor `name` 라벨 미지원 → CPU/Mem/Replica 패널 비어 있음 (LLM 입력에는 "측정 불가"로 표시, #78)
- target_tps는 처리량이 아니라 Locust 동시 가상 사용자 수(`--users`)다. UI·프롬프트의 "목표 TPS" 표기가 오해를 만든다 (docs/02 ISSUE-11, 기록만)
- Grafana datasource/dashboard 프로비저닝 설정 없음 (수동 import 필요)
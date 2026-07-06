# InfraGuard Agent

> **AI 인프라 자율 진단 에이전트** — 부하 테스트를 누르면, AI가 내 서버가 어디서 무너지는지 알아내고 스스로 고쳐준다.

> **면책 조항**: InfraGuard Agent는 인프라 진단 및 최적화 제안 도구입니다. 운영 환경에서의 스케일링 결정은 반드시 전문가 검토를 받으시기 바랍니다.

---

## 왜 필요한가

개인 개발 환경에서는 실제 수백 명이 동시에 접속하는 부하 상황을 만들기 사실상 불가능합니다.
InfraGuard Agent는 Locust로 의도된 부하 시나리오를 직접 만들고, AI 에이전트가 TPS·Latency·Error Rate
데이터를 종합 해석하여 병목 원인을 진단하고 자율 스케일링을 제안합니다.

단순 CPU 임계치 알림이 아닌, **맥락 기반 진단 + HITL 승인 후 실행**이 핵심입니다.

---

## 기능 (MVP)

- **Locust 부하 생성** — TPS·duration 설정으로 의도된 과부하 시나리오 실행
- **Prometheus 메트릭 수집** — CPU, Memory, Connection Count 실시간 조회
- **LLM 병목 진단** — TPS × Latency × Error Rate 복합 데이터 기반 원인 추론 (Solar Pro)
- **자율 스케일링 판단** — Docker Compose로 컨테이너 replica 수 자동 조정
- **HITL 승인 게이트** — 스케일링 실행 전 사용자 확인 요청
- **SSE 실시간 스트리밍** — 에이전트 사고 과정(Thought) 프론트엔드에 실시간 출력
- **종합 리포트** — 스케일링 전후 성능 비교 데이터 제공

---

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
│   │   │   ├── run_load_test.py   # Locust 실행 + 결과 파싱
│   │   │   ├── get_metrics.py     # Prometheus 쿼리
│   │   │   ├── scale_service.py   # Docker replica 조정
│   │   │   └── generate_plan.py   # 최적화 플랜 생성
│   │   ├── api
│   │   │   └── v1
│   │   │       └── agent.py       # /start /approve /report
│   │   ├── static
│   │   │   ├── index.html
│   │   │   ├── main.js
│   │   │   └── style.css
│   │   └── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── infra
│   ├── locust
│   │   ├── locustfile.py          # 부하 시나리오
│   │   └── locust.conf            # TPS 상한 설정
│   ├── prometheus
│   │   └── prometheus.yml
│   ├── grafana
│   │   └── dashboard.json
│   └── target-server              # 부하 받을 샘플 앱
│       ├── main.py
│       └── Dockerfile
├── tests
│   ├── unit
│   │   ├── test_load_runner.py
│   │   ├── test_metrics.py
│   │   ├── test_agent.py
│   │   └── test_api.py
│   └── integration
│       └── test_e2e.py
├── docker-compose.yml
├── .env.example
├── CLAUDE.md
├── CONTRIBUTING.md
└── README.md
```

---

## 빠른 시작

### 사전 요구사항

- **Python 3.11 이상** (필수 — `engine.py`가 3.11 신규 API `asyncio.timeout`을 사용. 3.10 이하에서는 engine 테스트가 전부 실패한다)
- **Docker Desktop** (설치 후 실행 중이어야 함)
- **Upstage API Key** (Solar Pro)

### 1. 코드 내려받기 & API 키 설정

```bash
git clone https://github.com/kwoo618/InfraGuard_Agent.git
cd InfraGuard_Agent

cp .env.example .env        # Windows: copy .env.example .env
# .env 파일을 열어 UPSTAGE_API_KEY=<발급받은_키> 입력
```

### 2. 파이썬 가상환경 + 의존성 설치

```bash
py -3.11 -m venv .venv          # Windows
# python3.11 -m venv .venv      # macOS/Linux

.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r backend/requirements.txt   # locust 포함
```

### 3. 인프라 컨테이너 기동

```bash
docker compose up -d --build    # target-server, prometheus, grafana 등
```

### 4. 백엔드 서버 실행

```bash
cd backend
uvicorn app.main:app --port 8000
```

> 반드시 `backend/` 디렉터리에서 실행한다(정적 파일 경로 때문). 가상환경을 activate한 상태여야 부하 테스트 시 `locust`가 subprocess에서 잡힌다.

### 5. 브라우저 접속

```
http://localhost:8000
```

---

## 웹에서 사용하기

1. **목표 TPS**(부하 세기, 최대 50)와 **테스트 시간(초)** 을 입력하고 **[진단 시작]** 클릭
2. 부하 테스트 → 메트릭 수집 → AI 병목 진단 로그가 실시간(SSE)으로 흐른다
3. 병목이 감지되면 **승인 모달**이 뜬다 → **[승인]** 시 실제 스케일링 실행
4. 스케일링 후 재검증 → **전/후 성능 비교 리포트**까지 확인

> **입력값 이해** — "목표 TPS"는 실제 처리량이 아니라 **동시 가상 사용자 수**를 정하는 값이다. 결과로 나오는 TPS는 서버가 실제로 처리한 초당 요청 수(측정값)이며, 보통 입력값과 다르다. 병목을 제대로 관찰하려면 부하를 높이고(예: 40~50) 시간을 충분히(예: 20~30초) 준다.

---

## 문제 해결 (Windows)

- **`locust`를 찾을 수 없음 / 부하 테스트 실패(WinError 2)** — 가상환경을 activate한 뒤 서버를 실행하거나, `.venv\Scripts`가 PATH에 있는지 확인한다.
- **Prometheus가 `unable to find user nobody` 또는 `exec format error`로 안 뜸** — Docker Desktop **Settings > General > "Use containerd for pulling and storing images"** 를 끄고 재시작한다. 동봉된 `docker-compose.override.yml`이 이를 우회하도록 돕는다. 그래도 안 되면 `docker rmi -f prom/prometheus:latest` 후 `docker compose up -d`로 이미지를 다시 받는다.
- **engine 관련 테스트가 대량으로 실패** — `python --version`으로 3.11 이상인지 확인한다.

---

## API 명세

### `POST /api/v1/agent/start`

목표 TPS와 테스트 기간을 전달받아 에이전트 루프를 시작합니다.
에이전트 사고 과정(Thought)을 SSE로 실시간 스트리밍합니다.

```json
// Request
{ "target_tps": 40, "duration": 60 }

// Response (SSE stream)
data: {"type": "thought", "content": "Locust 결과를 분석 중..."}
data: {"type": "action", "tool": "get_metrics", "result": {...}}
data: {"type": "hitl", "message": "replica를 2→4로 늘리겠습니다. 승인하시겠습니까?", "task_id": "abc123"}
```

### `POST /api/v1/agent/approve`

에이전트 스케일링 판단에 대한 사용자 승인을 받습니다.

```json
// Request
{ "task_id": "abc123", "approved": true }
```

### `GET /api/v1/agent/report/{task_id}`

최종 종합 분석 리포트를 반환합니다.

```json
// Response
{
  "task_id": "abc123",
  "bottleneck_cause": "DB Connection Pool 고갈",
  "scaling_before": 2,
  "scaling_after": 4,
  "latency_p95_before": 2100,
  "latency_p95_after": 480,
  "optimization_plan": ["DB 커넥션 풀 사이즈 증가", "쿼리 인덱스 점검"]
}
```

---

## 환경변수

```bash
UPSTAGE_API_KEY=        # Solar Pro API 키 (필수)
LANGFUSE_SECRET_KEY=    # Langfuse 트레이싱 (선택)
LANGFUSE_PUBLIC_KEY=
PROMETHEUS_URL=http://localhost:9090
MAX_LOOP=10
TARGET_SERVER_URL=http://localhost:8080
```

---

## 개발

자세한 기여 방법과 역할 분담은 [CONTRIBUTING.md](./CONTRIBUTING.md)를 참고하세요.

```bash
pip install -r backend/requirements.txt
pytest tests/
```

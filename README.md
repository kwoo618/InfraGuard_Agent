# InfraGuard Agent

> **AI 인프라 자율 진단 에이전트** — 내 PC에서 실제 부하를 만들어 서버가 어디서 느려지는지 측정하고,
> AI가 원인을 진단해 "서버를 몇 대로 늘리자"고 제안한다. **실행은 사람이 승인한 뒤에만** 일어난다.

> **면책 조항**: InfraGuard Agent는 인프라 진단 및 최적화 제안 도구입니다. 운영 환경에서의 스케일링 결정은 반드시 전문가 검토를 받으시기 바랍니다.

---

## 왜 필요한가

개인 개발 환경에서는 수백 명이 동시에 접속하는 상황을 재현하기 어렵고, 재현하더라도
"무엇 때문에 느린지"를 숫자에서 읽어내는 일은 사람 몫입니다.

InfraGuard Agent는 Locust로 부하를 직접 만들어 **TPS·응답 시간(P95)·오류율을 측정**하고,
그 측정값을 LLM이 해석해 병목 원인과 조치를 제안합니다.
단순 임계치 알림이 아니라 **측정 → 진단 → 사람 승인 → 조치 → 재측정**이 한 번에 돌아가는 것이 핵심입니다.

### 한 번 실행하면 일어나는 일

```
[웹 UI] 동시 가상 사용자 수·시간 입력
   │
   ├─ 1. 부하 테스트      Locust → nginx(:8080) → target-server replica들
   ├─ 2. 지표 수집        Locust 통계(TPS·P95·오류율) + Prometheus(활성 요청 수, 서버별 요청 수)
   ├─ 3. AI 진단          Upstage Solar가 측정값을 읽고 원인·심각도·신뢰도·스케일링 필요 여부 판단
   ├─ 4. 승인 요청 (HITL) "서버를 N대로 늘릴까요?" → 사람이 승인/거절
   ├─ 5. 서버 늘리기      승인된 경우에만 docker compose로 replica 조정
   ├─ 6. 재검증           같은 조건으로 다시 측정해 전/후 비교
   └─ 7. 결과 정리        화면에 결과 패널 표시 + results/에 JSON 파일 저장
```

---

## 기능

- **Locust 부하 생성** — 동시 가상 사용자 수(1~50)와 시간(초)을 지정해 실제 HTTP 부하를 발생
- **측정값 수집** — Locust 종료 시 통계(TPS, 응답 시간 P95·평균, 오류율, 총 요청 수)와
  Prometheus 조회(활성 요청 수, 서버별 요청 수 분산)
- **LLM 병목 진단** — Upstage Solar 모델이 측정값과 P95 SLO 기준을 함께 읽고
  원인·심각도·권장 조치·신뢰도·스케일링 필요 여부를 반환
- **HITL 승인 게이트** — 스케일링은 사람이 승인해야만 실행. AI 신뢰도가 기준(0.6) 미만이면
  "낮은 신뢰도를 확인했습니다" 체크 없이는 승인할 수 없음
- **자율 스케일링 실행** — 승인 후 Docker Compose로 target-server replica 수 조정 → 자동 재검증
- **SSE 실시간 진행 표시** — 단계 상태와 진행 문구를 브라우저로 스트리밍
- **결과 화면 2종** — 처음 보는 사람을 위한 **기본 보기**와 전체 정보를 담은 **상세 보기**
- **저장된 실행 기록 불러오기** — 지난 실행 결과 파일을 골라 같은 화면으로 다시 보기
- **결과 파일 저장** — 모든 실행을 `results/*.json`으로 저장(측정값·LLM 원문·승인 내역 포함)
- **동시 실행 방지 / 서버 초기화** — 실행이 겹쳐 측정이 오염되지 않도록 차단, 회차 사이 1대로 초기화
- **Grafana 대시보드** — 로그인 없이 보기 전용으로 열림

> **수집하지 않는 것**: CPU·메모리 사용률은 측정하지 않습니다. Windows Docker Desktop에서 cAdvisor가
> 컨테이너 이름 라벨을 붙이지 않아 컨테이너별 집계가 불가능하기 때문입니다
> (`backend/app/tools/get_metrics.py`, `docs/02_알려진이슈.md` ISSUE-3).
> 값을 추측해 넣지 않고 LLM 입력에서도 "측정 불가"로 제외합니다.

---

## 구성과 포트

| 주소 | 서비스 | 설명 |
|---|---|---|
| `http://localhost:8000` | 백엔드 + 웹 UI | FastAPI. 여기서 실행하고 결과를 본다 |
| `http://localhost:8080` | **nginx (부하 대상)** | target-server replica들 앞단 로드밸런서 |
| `http://localhost:9090` | Prometheus | 지표 수집 |
| `http://localhost:3000` | Grafana | 대시보드 (로그인 없이 보기) |
| `http://localhost:8090` | cAdvisor | 컨테이너 지표 수집기 |

부하는 항상 **nginx(8080)** 로 갑니다. target-server(부하를 받는 샘플 앱)는 호스트 포트를 받지 않고,
nginx가 replica들로 요청을 나눠 보냅니다. 그래서 스케일로 컨테이너가 새로 뜨거나 사라져도
부하 대상 주소는 8080으로 고정입니다. 서버별로 요청이 어떻게 나뉘었는지는 Prometheus의
`instance` 라벨(결과 화면의 "서버별 처리한 요청 수")로 확인합니다.

Grafana는 `GF_AUTH_ANONYMOUS_ENABLED=true`로 열려 있어 **로그인 없이 보기 전용**으로 들어가고,
InfraGuard 대시보드가 홈 화면으로 뜹니다. 데이터소스·대시보드는 `infra/grafana/provisioning/`이 자동 설정합니다.
편집하려면 `admin` / `admin`으로 로그인합니다(대시보드 원본이 `infra/grafana/dashboard.json`이라 UI 수정은 저장되지 않습니다).

---

## 빠른 시작

### 사전 요구사항

- **Docker Desktop** (설치 후 실행 중이어야 함)
- **Upstage API Key** (Solar 모델)
- **Python 3.11 이상** — 방법 B(호스트에서 백엔드 실행)에만 필요.
  `engine.py`가 3.11 신규 API `asyncio.timeout`을 사용해 3.10 이하에서는 engine 테스트가 전부 실패합니다.

### 공통: 코드 내려받기 & API 키

```bash
git clone https://github.com/kwoo618/InfraGuard_Agent.git
cd InfraGuard_Agent

cp .env.example .env        # Windows: copy .env.example .env
# .env를 열어 UPSTAGE_API_KEY=<발급받은_키> 입력
```

### 방법 A. 원클릭 (전체를 Docker로)

```bash
./start.sh        # macOS / Linux
start.bat         # Windows (더블클릭도 가능)
```

내부적으로 `docker compose --profile app up -d --build`를 실행하고 브라우저로 `http://localhost:8000`을 엽니다.
종료는 `docker compose --profile app down`.

### 방법 B. 인프라는 Docker, 백엔드는 직접 실행 (개발용)

```bash
# 1) 인프라만 기동 (target-server, nginx, prometheus, grafana, cadvisor)
docker compose up -d --build

# 2) 파이썬 가상환경 + 의존성
py -3.11 -m venv .venv          # Windows
# python3.11 -m venv .venv      # macOS/Linux
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r backend/requirements.txt   # locust 포함

# 3) 백엔드 실행 (backend 디렉터리에서)
cd backend
uvicorn app.main:app --port 8000
```

> `backend/`에서 실행하는 이유는 `uvicorn app.main:app`이 `app` 패키지를 현재 디렉터리에서 찾기 때문입니다.
> 또한 부하 테스트가 `locust`를 별도 프로세스로 띄우므로, 가상환경을 activate한 셸에서 서버를 실행해야 합니다.
>
> `docker compose up -d`(프로필 없음)로는 백엔드 컨테이너가 뜨지 않습니다. 의도된 동작입니다 — 방법 B는 백엔드를 호스트에서 돌립니다.

### 접속

```
http://localhost:8000
```

---

## 웹에서 사용하기

1. **동시 가상 사용자 수**(1~50)와 **테스트 시간(초)** 을 입력하고 **[자율 진단 및 부하 테스트 시작]** 클릭
2. 부하 테스트 → 지표 수집 → AI 분석 순서로 진행 상황이 실시간(SSE)으로 흐릅니다
3. AI가 스케일링이 필요하다고 판단하면 **승인 모달**이 뜹니다
   - **[승인하고 서버 늘리기]** 를 누르면 실제로 replica가 늘어납니다
   - **[거절]** 하면 서버를 건드리지 않고 실행이 끝납니다
   - AI 신뢰도가 **0.6 미만**이면 경고와 함께 "낮은 신뢰도를 확인했습니다" 체크박스가 나타나고,
     체크 없이는 승인 버튼이 동작하지 않습니다 (API를 직접 호출해도 막힙니다)
4. 승인한 경우 서버 증설 → **재검증 측정** → 전/후 비교까지 이어집니다
5. 실행이 끝나면 **결과 화면**이 나타납니다

### 결과 화면 — 기본 보기 / 상세 보기

화면 위 전환 버튼으로 두 보기를 오갑니다. 주소에 `?view=simple` / `?view=detail`로도 지정할 수 있고, 기본값은 기본 보기입니다.

- **기본 보기** — 처음 보는 사람을 위한 화면. 결론 한 줄, P95 응답 시간, 서버 흐름 그림,
  라운드별 P95, 초당 응답 시간 그래프, 핵심 지표 4개. "AI가 이렇게 판단한 이유"는 접어 둡니다.
- **상세 보기** — 라운드별 측정 이력, AI 판단 근거(신뢰도·진단에 쓰인 측정값·LLM 원문),
  전/후 비교, 부하 중 그래프 5종, 측정 조건까지 전부 표시합니다.

용어(P95, SLO, TPS, 활성 연결 등)는 화면 안에 짧은 풀이가 함께 나옵니다.
판단 모델·결과 파일 경로·코드 버전은 "기술 정보" 토글 안에 있습니다.

### 저장된 실행 기록 불러오기

상단 **[저장된 실행 기록]** 드롭다운에서 지난 실행(`docs/evidence/`의 결과 파일)을 골라 **[불러오기]** 하면
같은 결과 화면으로 다시 볼 수 있습니다. 읽기 전용이며, 불러온 상태에서는 화면에 파일명과 실행 시각이 표시됩니다.
**[실시간 보기로 돌아가기]** 로 빠져나옵니다.

### 서버 초기화 · 동시 실행 방지

- **[서버 1대로 초기화]** — 측정 회차 사이에 target-server를 1대로 되돌립니다. 에이전트 실행이 아니므로 결과 파일을 남기지 않습니다.
- 다른 실행이 진행 중이거나(승인 대기 포함), 연결이 끊긴 실행의 부하 테스트가 아직 돌고 있거나, 초기화 중이면
  새 실행과 초기화가 **409로 차단**되고 버튼이 비활성화됩니다. 부하가 겹쳐 측정이 오염되는 것을 막기 위해서입니다.

> **입력값 이해** — "동시 가상 사용자 수"는 처리량 목표가 아니라 Locust 동시 접속 수(`--users`)입니다.
> API 파라미터 이름은 `target_tps`지만 뜻은 같습니다. 결과로 나오는 TPS는 서버가 실제로 처리한 초당 요청 수(측정값)이며,
> 입력값과 다를 수 있습니다. 병목을 관찰하려면 부하를 높이고(예: 40~50) 시간을 충분히(예: 20~30초) 줍니다.

---

## 결과 파일

모든 실행은 종료 시 `results/{실행시각}_{task_id}.json`으로 저장됩니다 (예: `20260912_005741_<task_id>.json`).
`results/`는 git에 올라가지 않습니다. 발표·보고에 쓰는 실행만 골라 `docs/evidence/`로 복사합니다.

파일에는 라운드별 측정 이력(`measurement_history`), 측정 조건(`conditions` — 동시 가상 사용자 수, 시간,
P95 SLO, 시작 서버 대수, 모델명, 부하 시나리오 가중치, 실행 환경), LLM 진단 원문(`diagnoses`),
승인 내역(`approvals`), 스케일링 결과(`scaling_results`), 재검증(`revalidations`), 종료 경로(`outcome`)가 들어갑니다.

측정 수치는 이 결과 파일이 유일한 출처입니다. 발표에 쓴 수치는 [docs/04_발표용측정결과.md](./docs/04_발표용측정결과.md)에 정리돼 있습니다.

---

## API 명세

값은 **타입 표기**입니다. 실제 수치 예시는 넣지 않았습니다 (측정하지 않은 값을 예시로 쓰지 않는다는 원칙).

### `GET /api/v1/agent/start`

동시 가상 사용자 수(`target_tps`, Locust `--users`, 1~50, 처리량 목표 아님)와 테스트 시간(`duration`, 초)을
쿼리 파라미터로 받아 에이전트 루프를 시작하고 진행 상황을 SSE로 스트리밍합니다.
다른 실행이 진행 중이거나 끊긴 실행의 부하 테스트가 남아 있으면 **409**를 반환합니다.

```
GET /api/v1/agent/start?target_tps=<1~50>&duration=<초>

data: {"status": "<상태>", "task_id": "<uuid 또는 null>", "message": "<진행 문구>"}
```

`status` 값: `running` · `analyzing` · `need_approval` · `scaling` · `remeasuring` · `done` · `failed`.
`need_approval` 프레임에는 `low_confidence`, `confidence`, `low_confidence_threshold`가 함께 실립니다.

### `POST /api/v1/agent/approve`

스케일링 제안에 대한 승인/거절을 받습니다.

```jsonc
{
  "task_id": "<uuid>",
  "approved": true,
  "acknowledge_low_confidence": false   // 신뢰도 0.6 미만 제안을 승인할 때 true 필요. 거절에는 불필요
}
```

신뢰도 기준 미만 제안을 `acknowledge_low_confidence` 없이 승인하면 **400**을 반환합니다.

### `GET /api/v1/agent/report/{task_id}`

최종 종합 리포트를 반환합니다. 결과 파일과 같은 값입니다.

```jsonc
{
  "task_id": "<uuid>",
  "outcome": "<pending|diagnosed|awaiting_approval|scaled|failed>",
  "end_reason": "<no_scaling_proposed|scaled|rejected|failed|...>",
  "measurement":       { "tps": <float>, "latency_p95": <ms>, "latency_avg": <ms>,
                         "error_rate": <0~1>, "total_requests": <int>, "duration": <초> },
  "measurement_after": { /* 스케일링 후 재검증 측정값. 없으면 null */ },
  "improvement":       { "tps_delta": <float>, "latency_p95_delta": <ms>, "error_rate_delta": <float> },
  "bottleneck":        { "cause": "<LLM 진단 원문>", "severity": "<low|medium|high>",
                         "recommendation": "<권장 조치>", "confidence": <0~1>, "requires_scaling": <bool> },
  "action":            { "before_replicas": <int>, "after_replicas": <int>,
                         "success": <bool>, "error_message": "<실패 사유 또는 null>" },
  "optimization_plan": ["<조치 항목>"],
  "summary": "<LLM 최종 요약>",
  "measurement_history": [ /* 라운드별 측정 이력 + 그래프용 시계열 */ ],
  "conditions": { "virtual_users": <int>, "duration_sec": <int>, "p95_slo_ms": <int>,
                  "start_replicas": <int>, "llm_model": "<모델명>", "resource_metrics_collected": false },
  "diagnoses": [], "approvals": [], "scaling_results": [], "revalidations": [],
  "user_decision": "<approved|rejected|no_response|not_applicable>",
  "low_confidence": <bool>, "low_confidence_threshold": 0.6,
  "forced_scaling": <bool>,
  "result_file": "<results/... 경로 또는 null>"
}
```

### `GET /api/v1/agent/replicas`

현재 target-server 서버 대수와 새 실행 가능 여부를 반환합니다.

```jsonc
{ "replicas": <int 또는 null>, "busy": <bool>,
  "busy_reason": "<agent_running|load_test_running|replica_reset|null>" }
```

### `POST /api/v1/agent/replicas/reset`

target-server를 1대로 되돌립니다. 실행 중·승인 대기 중이면 **409**. 결과 파일을 남기지 않습니다.

```jsonc
{ "before_replicas": <int>, "after_replicas": <int>, "success": <bool>, "error_message": "<또는 null>" }
```

### `GET /api/v1/results/saved` · `GET /api/v1/results/saved/{file_name}`

저장된 실행 기록(`docs/evidence/`) 목록 조회와 단건 조회입니다. **읽기 전용**이며,
파일명이 결과 파일 규칙과 다르면 400, 해당 디렉터리 밖이거나 없는 파일이면 404입니다.
단건 조회는 `/agent/report`와 같은 모양으로 변환해 돌려주므로 같은 화면이 그대로 그립니다.

---

## 환경변수 (.env)

```bash
UPSTAGE_API_KEY=                          # Upstage API 키 (필수)
UPSTAGE_API_URL=                          # 기본값 사용 시 생략 가능
UPSTAGE_MODEL=solar-pro2                  # 진단·재검증에 쓰는 모델

PROMETHEUS_URL=http://localhost:9090      # 컨테이너로 백엔드를 띄우면 http://prometheus:9090
TARGET_SERVER_URL=http://localhost:8080   # 부하 대상 = nginx 로드밸런서

MAX_LOOP=10                               # ReAct Loop 최대 반복
P95_SLO_MS=1000                           # P95 응답 시간 목표(ms). LLM 진단의 판단 기준
SCALE_MAX_REPLICAS=8                      # 스케일 상한
DEBUG_ENDPOINTS_ENABLED=false             # 디버그용 force_scaling 허용 여부. 측정·발표 시 반드시 false
```

항목별 설명과 "값을 비웠을 때 쓰이는 기본값"은 [.env.example](./.env.example)에 주석으로 적혀 있습니다.
코드가 읽는 환경변수는 위 9개가 전부입니다.

---

## 가드레일

측정의 재현성과 로컬 PC 보호를 위해 다음 값은 고정입니다.

| 항목 | 값 | 위치 |
|---|---|---|
| ReAct Loop 최대 반복 | 10 | `MAX_LOOP` (환경변수) |
| 동시 가상 사용자 상한 | 50 | `run_load_test.py` `MAX_TPS` — 처리량 상한이 아님 |
| replica 최대 | 8 | `SCALE_MAX_REPLICAS` (환경변수) |
| 에이전트 루프 타임아웃 | 300초 | `engine.py` |
| 저신뢰 확인 게이트 기준 | 0.6 | `api/v1/agent.py` `LOW_CONFIDENCE_THRESHOLD` (코드 상수) |

사람 승인이 반드시 필요한 지점은 **컨테이너 스케일링 실행 전**과 **신뢰도 0.6 미만 제안의 승인**입니다.
부하 테스트·지표 수집·병목 진단은 승인 없이 진행됩니다.

---

## 파일 구조

```
InfraGuard_Agent
├── backend
│   ├── app
│   │   ├── agent
│   │   │   ├── engine.py          # ReAct Loop 오케스트레이터
│   │   │   ├── state.py           # Agent 런타임 상태 정의
│   │   │   ├── prompts.py         # 시스템 프롬프트 (P95 SLO 기준 포함)
│   │   │   └── nodes.py           # LLM reasoning node
│   │   ├── tools
│   │   │   ├── run_load_test.py   # Locust 실행 + 결과 파싱 (초 단위 시계열·엔드포인트 통계 포함)
│   │   │   ├── get_metrics.py     # Prometheus 쿼리 (활성 요청 수, 서버별 요청 수)
│   │   │   ├── scale_service.py   # Docker replica 조정
│   │   │   └── generate_plan.py   # 최적화 플랜 생성
│   │   ├── api
│   │   │   └── v1
│   │   │       ├── agent.py         # /start /approve /report /replicas /replicas/reset
│   │   │       ├── run_history.py   # 라운드별 이력 + results/ 결과 파일 저장
│   │   │       └── saved_results.py # 저장된 실행 기록 조회 (읽기 전용)
│   │   ├── static                 # 웹 UI (SSE + HITL + 결과 화면)
│   │   │   ├── index.html
│   │   │   ├── main.js
│   │   │   ├── view_switch.js     # 기본/상세 보기 전환 (?view=)
│   │   │   ├── saved_results.js   # 저장된 실행 기록 불러오기
│   │   │   ├── result_panel.js    # 결과 화면 공용 계산
│   │   │   ├── toolbar.css
│   │   │   └── refresh            # 보기별 렌더링 (common / c=기본 / a=상세)
│   │   └── main.py                # FastAPI 앱 진입점
│   ├── Dockerfile
│   └── requirements.txt
├── infra
│   ├── locust
│   │   ├── locustfile.py          # 부하 시나리오 (/light /heavy /flaky /health)
│   │   └── locust.conf            # 기본 설정 (동시 가상 사용자 기본값 50)
│   ├── nginx
│   │   └── nginx.conf             # target-server 로드밸런서 (localhost:8080)
│   ├── prometheus
│   │   └── prometheus.yml         # replica 자동 탐색(dns_sd)
│   ├── grafana
│   │   ├── dashboard.json         # 홈 대시보드 원본
│   │   └── provisioning           # 데이터소스·대시보드 자동 로드
│   └── target-server              # 부하를 받는 샘플 앱
│       ├── main.py
│       ├── requirements.txt
│       └── Dockerfile
├── tests
│   ├── unit                       # test_load_runner / test_metrics / test_prompts / test_agent /
│   │                              # test_api / test_run_history / test_replica_reset /
│   │                              # test_run_gate / test_grafana_config / test_saved_results
│   └── integration
│       └── test_e2e.py
├── docs                           # 00 작업가이드 · 01 개발계획 · 02 알려진이슈 · 03 대시보드명세 · 04 발표용측정결과
│   └── evidence                   # 발표에 쓴 실행의 결과 파일
├── results                        # 실행 결과 JSON (git 제외)
├── docker-compose.yml
├── docker-compose.override.yml    # Prometheus 기동 우회 (아래 문제 해결 참고)
├── start.bat / start.sh           # 원클릭 실행
├── .env.example
├── CLAUDE.md · CONTRIBUTING.md · DEPLOY.md
└── README.md
```

---

## 부하 시나리오

`infra/locust/locustfile.py`가 target-server의 네 가지 엔드포인트를 가중치대로 호출합니다.

| 엔드포인트 | 뜻 | 서버 동작 | 가중치 |
|---|---|---|---|
| `/light` | 가벼운 요청 | 약 0.01초 후 응답 | 6 |
| `/heavy` | 무거운 작업 | 동시에 5개까지만 처리, 0.2~0.6초 소요 | 3 |
| `/flaky` | 가끔 실패하는 요청 | 5% 확률로 500 응답 | 1 |
| `/health` | 상태 확인 | 즉시 응답 | 1 |

`/heavy`의 동시 처리 제한이 병목의 주된 원인이 되도록 만들어진 샘플 앱입니다.

---

## 문제 해결

- **`locust`를 찾을 수 없음 / 부하 테스트 실패(WinError 2)** — 가상환경을 activate한 뒤 서버를 실행하거나,
  `.venv\Scripts`가 PATH에 있는지 확인합니다.
- **Prometheus가 `unable to find user nobody` 또는 `exec format error`로 안 뜸** —
  Docker Desktop **Settings > General > "Use containerd for pulling and storing images"** 를 끄고 재시작합니다.
  동봉된 `docker-compose.override.yml`이 Prometheus를 숫자 UID(65534)로 실행해 이를 우회합니다.
  그래도 안 되면 `docker rmi -f prom/prometheus:latest` 후 다시 받습니다.
- **engine 관련 테스트가 대량으로 실패** — `python --version`으로 3.11 이상인지 확인합니다.
- **시작 버튼이 비활성이고 "실행 중" 안내가 뜸** — 다른 실행이나 부하 테스트가 끝나기를 기다리는 중입니다.
  끊긴 실행의 Locust는 강제 종료하지 않고 끝날 때까지 기다립니다.

---

## 문서

| 문서 | 내용 |
|---|---|
| [docs/01_개발계획.md](./docs/01_개발계획.md) | 단계별 개발 계획 |
| [docs/02_알려진이슈.md](./docs/02_알려진이슈.md) | 알려진 이슈와 해결 이력 (측정 신뢰도에 영향 있는 항목 포함) |
| [docs/03_대시보드명세.md](./docs/03_대시보드명세.md) | 결과 화면 명세 |
| [docs/04_발표용측정결과.md](./docs/04_발표용측정결과.md) | **발표에 쓰는 측정 수치와 그 출처** |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | 기여 방법·역할 분담 |
| [DEPLOY.md](./DEPLOY.md) | 배포 메모 |

---

## 개발

```bash
pip install -r backend/requirements.txt
pytest tests/
```

`tests/unit`은 Locust·Prometheus·Docker를 모두 목킹하므로 인프라 없이 돌아갑니다.
`tests/integration/test_e2e.py`는 실제 인프라와 LLM을 사용하며, LLM이 스케일링을 제안하면
승인 대기에서 멈춥니다(알려진 제약, `docs/02_알려진이슈.md` ISSUE-12).

# 배포 · 실행 가이드

InfraGuard Agent를 다른 사람도 쉽게 띄울 수 있도록 두 가지 실행 방법을 제공한다.

| 방법 | 필요한 것 | 난이도 | 특징 |
|---|---|---|---|
| **A. Docker 한 방** | Docker Desktop + `.env` | ★ 쉬움 | Python 설치 불필요, 명령 한 줄 |
| **B. 수동 실행** | Python 3.11 + Docker + `.env` | ★★ | 개발/디버깅에 적합 (README 참고) |

---

## 사전 준비 (공통)

```bash
git clone https://github.com/kwoo618/InfraGuard_Agent.git
cd InfraGuard_Agent

cp .env.example .env        # Windows: copy .env.example .env
# .env 파일을 열어 UPSTAGE_API_KEY=<발급받은_키> 입력
```

그리고 **Docker Desktop을 실행**해 둔다(고래 아이콘 초록).

---

## 방법 A — Docker 한 방 (권장)

전체 스택(타겟 서버 · nginx 로드밸런서 · Prometheus · 백엔드 API/UI)을 컨테이너로 한 번에 띄운다.

```bash
docker compose --profile app up -d --build
```

브라우저에서 접속:

```
http://localhost:8000
```

종료:

```bash
docker compose --profile app down
```

> **원클릭 실행**: 저장소 루트의 `start.bat`(Windows) 또는 `start.sh`(macOS/Linux)를
> 실행하면 위 과정 + 브라우저 열기까지 자동으로 처리한다.

### 동작 원리 (참고)
- 백엔드 컨테이너는 `profiles: ["app"]`에 속해 있어, 프로필 없이 `docker compose up -d`를
  하면 **인프라만** 뜬다(기존 개발 방식 유지). `--profile app`을 줄 때만 백엔드까지 뜬다.
- 백엔드는 부하 테스트(`locust`)와 스케일링(`docker compose`)을 subprocess로 실행하므로,
  이미지에 docker CLI가 포함되고, 런타임에 **호스트 docker 소켓**과 **repo 루트**를 마운트한다.
- 컨테이너 네트워크에서는 `localhost` 대신 서비스 이름으로 접근한다:
  `PROMETHEUS_URL=http://prometheus:9090`, `TARGET_SERVER_URL=http://nginx:8080`.
- 부하는 nginx 로드밸런서(호스트 `localhost:8080`, 컨테이너 네트워크 `nginx:8080`)를 거쳐
  target-server replica들로 분산된다. replica는 호스트 포트를 받지 않으므로, 스케일로 컨테이너가
  재생성돼도 부하 대상 주소는 바뀌지 않는다.

### ⚠️ 방법 A 사전 조건 (코드 1줄)
컨테이너 안에서는 `localhost:9090`이 백엔드 자기 자신을 가리키므로, 헬스체크가
환경변수를 사용하도록 아래 1줄이 반영되어 있어야 한다.

```python
# backend/app/api/v1/agent.py (인프라 헬스체크)
response = await client.get(f"{os.getenv('PROMETHEUS_URL', 'http://localhost:9090')}/", timeout=3.0)
```

또한 `backend/requirements.txt`에 `locust>=2.20`이 포함되어 있어야 한다
(부하 테스트 subprocess에 필요). 이미지 빌드 시에도 별도 설치하지만, 방법 B(수동)에서도 필요하다.

---

## 방법 B — 수동 실행

Python 가상환경에서 백엔드를 직접 실행한다. 자세한 절차는 [README.md](./README.md#빠른-시작)를 참고한다.

```bash
py -3.11 -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r backend/requirements.txt
docker compose up -d --build                          # 인프라만
cd backend && uvicorn app.main:app --port 8000
```

---

## 문제 해결

- **백엔드 컨테이너가 헬스체크에서 실패** → 위 "방법 A 사전 조건"의 `PROMETHEUS_URL` 처리 확인.
- **부하 테스트 실패(locust 없음)** → `backend/requirements.txt`에 `locust` 포함 여부 확인.
- **Prometheus가 `nobody`/`exec format`으로 안 뜸** → `docker-compose.override.yml`이 우회하거나,
  Docker Desktop Settings > General에서 "Use containerd for pulling and storing images"를 끈다.
- **부하 테스트가 전부 연결 거부(에러율 100%)** → `docker compose ps`로 `infraguard-nginx`가 `8080`을 받고 있는지 확인.
  로드밸런서 도입 전 설정(target-server 범위 포트)으로 떠 있던 스택이면 호스트에서 `docker compose up -d`로 한 번 재생성한다
  (스케일링은 `--no-recreate`라 기존 컨테이너 설정을 바꾸지 않는다).
- **스케일링이 새 프로젝트를 만들며 동작 안 함** → 백엔드 서비스의 `COMPOSE_PROJECT_NAME=infraguard_agent`
  환경변수가 유지되는지 확인(호스트 프로젝트 이름과 일치해야 기존 컨테이너를 조종함).

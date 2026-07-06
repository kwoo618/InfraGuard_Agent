# InfraGuard Agent — 백엔드 API 서버 이미지
#
# FastAPI 앱을 구동하며, 다음 두 가지를 위해 추가 도구를 담는다:
#   1) 부하 테스트: run_load_test가 `locust`를 subprocess로 호출
#   2) 스케일링:   scale_service가 `docker compose`를 subprocess로 호출
#      → docker CLI + compose 플러그인을 설치하고, 런타임에 호스트 docker
#        소켓을 마운트해 호스트의 컨테이너를 조종한다(docker-out-of-docker).
#
# 앱 코드와 docker-compose.yml은 런타임에 repo 루트를 /app로 볼륨 마운트해
# 제공한다. scale_service가 PROJECT_ROOT(=repo 루트)의 compose 파일을
# 필요로 하기 때문이다. (docker-compose.yml의 backend 서비스 정의 참고)

FROM python:3.11-slim

# --- docker CLI + compose 플러그인 ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        docker-ce-cli docker-compose-plugin && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# --- 파이썬 의존성 ---
# locust는 requirements에 없을 수 있어 명시적으로도 설치한다(부하 테스트 필수).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir "locust>=2.20"

# 앱 코드는 compose에서 repo 루트를 /app로 마운트해 제공한다.
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

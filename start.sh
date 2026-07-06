#!/usr/bin/env bash
# ============================================================
#  InfraGuard Agent - 원클릭 실행 (macOS / Linux)
#  전체 스택(인프라 + 백엔드)을 Docker로 띄운다.
#  사전 준비: Docker 실행 중, .env에 UPSTAGE_API_KEY 입력
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null 2>&1 || { echo "[ERROR] Docker CLI를 찾을 수 없습니다."; exit 1; }
docker info >/dev/null 2>&1 || { echo "[ERROR] Docker 데몬이 응답하지 않습니다. Docker를 실행하세요."; exit 1; }
[ -f .env ] || { echo "[ERROR] .env 파일이 없습니다. .env.example을 복사해 UPSTAGE_API_KEY를 채우세요."; exit 1; }

echo "[InfraGuard] 전체 스택을 기동합니다... (최초 실행은 이미지 빌드로 수 분 소요)"
docker compose --profile app up -d --build

echo "[InfraGuard] 대시보드: http://localhost:8000"
echo "[InfraGuard] 종료:     docker compose --profile app down"

# 브라우저 자동 열기(있으면)
( command -v open >/dev/null && open http://localhost:8000 ) || \
( command -v xdg-open >/dev/null && xdg-open http://localhost:8000 ) || true

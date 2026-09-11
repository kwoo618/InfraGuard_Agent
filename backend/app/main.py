import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.v1.agent import router as agent_router
from app.api.v1.saved_results import router as saved_results_router

app = FastAPI(title="InfraGuard Agent System")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(agent_router, prefix="/api/v1")
# 저장된 결과 파일(docs/evidence) 읽기 전용 조회 — 결과 패널 불러오기 (#93)
app.include_router(saved_results_router, prefix="/api/v1")


# 상대경로("app/static")는 실행 시점의 cwd에 따라 깨진다.
# 루트에서 `pytest tests/`를 돌리면 cwd가 backend/가 아니라서
# RuntimeError: Directory 'app/static' does not exist가 났었다.
# 이 파일(main.py) 위치 기준 절대경로로 고정해서 cwd와 무관하게 동작하도록 한다.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
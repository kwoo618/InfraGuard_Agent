from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from backend.app.api.v1.agent import router as agent_router

app = FastAPI(title="InfraGuard Agent System")

# CORS 설정 (프론트-백엔드 간 통신 허용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API 라우터 등록
app.include_router(agent_router, prefix="/api/v1")

# UI 정적 파일 마운트 (이 주소로 접속 시 index.html 자동 로드)
app.mount("/", StaticFiles(directory="backend/app/static", html=True), name="static")
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.v1.agent import router as agent_router

app = FastAPI(title="InfraGuard Agent System")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(agent_router, prefix="/api/v1")


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
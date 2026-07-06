@echo off
REM ============================================================
REM  InfraGuard Agent - 원클릭 실행 (Windows)
REM  전체 스택(인프라 + 백엔드)을 Docker로 띄우고 브라우저를 연다.
REM  사전 준비: Docker Desktop 실행 중, .env에 UPSTAGE_API_KEY 입력
REM ============================================================
setlocal
cd /d "%~dp0"

where docker >nul 2>nul || (echo [ERROR] Docker CLI를 찾을 수 없습니다. Docker Desktop 설치/실행을 확인하세요.& pause & exit /b 1)
docker info >nul 2>nul || (echo [ERROR] Docker 데몬이 응답하지 않습니다. Docker Desktop을 실행하세요.& pause & exit /b 1)
if not exist ".env" (echo [ERROR] .env 파일이 없습니다. .env.example을 복사해 UPSTAGE_API_KEY를 채우세요.& pause & exit /b 1)

echo [InfraGuard] 전체 스택을 기동합니다... (최초 실행은 이미지 빌드로 수 분 소요)
docker compose --profile app up -d --build
if errorlevel 1 (echo [ERROR] 기동 실패. 위 로그를 확인하세요.& pause & exit /b 1)

echo [InfraGuard] 잠시 후 브라우저를 엽니다: http://localhost:8000
timeout /t 4 >nul
start "" http://localhost:8000
echo.
echo [InfraGuard] 실행 완료.
echo   - 대시보드 : http://localhost:8000
echo   - 종료     : docker compose --profile app down
echo.
pause
endlocal

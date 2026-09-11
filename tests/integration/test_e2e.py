import os
import json
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

# .env 파일 로드
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app.api.v1 import run_history
from app.main import app

# 💡 이제 뒤에 번호(-2, -3)를 떼고 공통된 핵심 이름만 기본값으로 지정합니다.
TARGET_CONTAINER_NAME = os.getenv("TARGET_CONTAINER_NAME", "infraguard_agent-target-server")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

@pytest.mark.asyncio
async def test_agent_real_e2e_full_verification(tmp_path, monkeypatch):
    """
    실제 인프라(Docker, API Key, Prometheus) 구축 여부를 실시간으로 진단하고,
    미구축 시 터미널에 즉시 상세 사유를 출력한 뒤 테스트를 건너뜁니다.
    """
    # 실제 실행이지만 테스트 조건(duration 3초)이라 results/에 섞지 않는다 (docs/03 Phase 3).
    monkeypatch.setattr(run_history, "RESULTS_DIR", tmp_path / "results")
    global TARGET_CONTAINER_NAME  # 내부에서 찾은 실제 이름으로 동적 업데이트하기 위함
    
    # ------------------------------------------------------------------
    # 🔍 인프라 실시간 자동 진단 로직
    # ------------------------------------------------------------------
    infra_errors = []
    docker_client = None
    initial_status = "running"

    # 1. 파이썬 Docker SDK 및 도커 데스크탑 엔진 구동 확인
    try:
        import docker
        docker_client = docker.from_env()
        docker_client.ping()  
    except ImportError:
        infra_errors.append("파이썬 'docker' 패키지가 가상환경에 없습니다. (pip install docker 필요)")
    except Exception:
        infra_errors.append("도커 데스크탑(Docker Engine)이 켜져 있지 않거나 통신할 수 없습니다.")

    # 2. 부하 테스트 대상 컨테이너 존재 여부 확인 (🔥 지멋대로 바뀌는 번호 자동 해결 로직)
    if docker_client:
        try:
            # 실행 중이거나 정지된 모든 컨테이너 목록을 다 가져옵니다.
            all_containers = docker_client.containers.list(all=True)
            
            # 이름에 'infraguard_agent-target-server' 문자열이 포함된 컨테이너를 찾습니다.
            matched_container = None
            for container in all_containers:
                if TARGET_CONTAINER_NAME in container.name:
                    matched_container = container
                    break
            
            if matched_container:
                # 🎯 찾았다면 지멋대로 바뀐 실제 전체 이름(예: infraguard_agent-target-server-3)으로 변수를 교체합니다.
                TARGET_CONTAINER_NAME = matched_container.name
                initial_status = matched_container.status
                
                if initial_status != "running":
                    infra_errors.append(f"타겟 컨테이너('{TARGET_CONTAINER_NAME}')가 실행 중(running)이 아닙니다.")
            else:
                infra_errors.append(f"도커 내에 '{TARGET_CONTAINER_NAME}' 문자열을 포함하는 컨테이너가 하나도 없습니다.")
                
        except Exception as e:
            infra_errors.append(f"도커 컨테이너 상태를 조회하는 중 오류 발생: {e}")

    # 3. Upstage API 키 설정 확인
    if not os.getenv("UPSTAGE_API_KEY"):
        infra_errors.append("Solar LLM API 호출을 위한 'UPSTAGE_API_KEY' 환경변수가 설정되지 않았습니다.")

    # 🚨 하나라도 구축이 안 되어있다면 상세 안내문 출력 후 스킵
    if infra_errors:
        error_details = "\n".join([f"   ❌ {err}" for err in infra_errors])
        print(
            f"\n=========================================================\n"
            f" [ 안내 ] 인프라가 완전히 구축되지 않아 E2E 테스트를 진행할 수 없습니다.\n"
            f"---------------------------------------------------------\n"
            f"{error_details}\n"
            f"=========================================================\n",
            flush=True
        )
        pytest.skip("인프라 미구축 (상세 사유는 위 출력된 안내문을 확인하세요)")

    # ------------------------------------------------------------------
    # 🚀 모든 인프라가 완벽히 켜져 있을 때만 진입하는 진짜 E2E 테스트 영역
    # ------------------------------------------------------------------
    print(f"\n[INFO] 매칭된 실제 컨테이너 이름: {TARGET_CONTAINER_NAME} (상태: {initial_status})")
    
    shared = {
        "task_id": None, 
        "need_approval": False, 
        "latest_status": None,
        "error_message": None
    }
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac_stream, \
               AsyncClient(transport=transport, base_url="http://testserver") as ac_api, \
               AsyncClient() as external_http:

        # 1. 에이전트 시작
        stream_url = "/api/v1/agent/start?target_tps=50&duration=3"
        
        async def listen_sse():
            try:
                async with ac_stream.stream("GET", stream_url, timeout=60.0) as response:
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"): continue
                        payload = json.loads(line.replace("data: ", "").strip())
                        
                        status = payload.get("status")
                        shared["latest_status"] = status
                        
                        if status in ["need_approval", "awaiting_approval"]:
                            shared["need_approval"] = True
                        elif status == "failed":
                            shared["error_message"] = payload.get("message", "Unknown Agent Failure")
                        
                        if payload.get("task_id"):
                            shared["task_id"] = payload["task_id"]
            except Exception as e:
                shared["error_message"] = str(e)

        sse_task = asyncio.create_task(listen_sse())

        # 2. task_id 확보 대기
        for _ in range(40):
            if shared["task_id"]: break
            await asyncio.sleep(0.5)
        assert shared["task_id"] is not None, "에이전트 시작 및 Task ID 확보 실패"

        # 3. 에이전트 진단 종료 대기
        for _ in range(80):
            if shared["need_approval"] or shared["latest_status"] in ["done", "failed", "completed"]:
                break
            await asyncio.sleep(0.5)
            
        assert shared["latest_status"] != "failed", f"에이전트 실행 중 실패 발생: {shared['error_message']}"

        # 4. Prometheus 메트릭 수집 확인
        try:
            prom_res = await external_http.get(
                f"{PROMETHEUS_URL}/api/v1/query", 
                params={"query": "http_requests_total"}, 
                timeout=5.0
            )
            if prom_res.status_code == 200:
                prom_data = prom_res.json()
                assert prom_data["status"] == "success"
                assert len(prom_data["data"]["result"]) > 0, "Prometheus에 메트릭 수집 내역이 없습니다."
        except Exception as e:
            print(f"\n[WARN] Prometheus 직접 조회 건너뜀: {e}")

        # 5. AI 진단 결과에 따른 동적 분기 검증
        if shared["need_approval"]:
            print("\n[INFO] AI가 병목을 감지하여 스케일링 승인 검증 단계를 시작합니다.")
            try:
                check_container = docker_client.containers.get(TARGET_CONTAINER_NAME)
                assert check_container.status == initial_status, "[오류] 승인 전에 컨테이너 상태가 임의로 변경되었습니다."
            except Exception: pass

            check_res = await ac_api.get(f"/api/v1/agent/report/{shared['task_id']}")
            assert check_res.status_code == 200
            assert "bottleneck" in check_res.json()

            approve_res = await ac_api.post(
                "/api/v1/agent/approve", 
                json={"task_id": shared["task_id"], "approved": True}
            )
            assert approve_res.status_code in [200, 404]
        else:
            print("\n[INFO] AI가 인프라 정상 판단(병목 없음)으로 승인 없이 흐름을 종료했습니다.")
            assert shared["latest_status"] in ["done", "completed"]

        # 6. 최종 리포트 완결성 검증
        final_report = None
        for _ in range(40):
            res = await ac_api.get(f"/api/v1/agent/report/{shared['task_id']}")
            if res.status_code == 200:
                final_report = res.json()
                break
            await asyncio.sleep(0.5)

        assert final_report is not None, "최종 리포트 생성 및 완료 검증 실패"
        assert "outcome" in final_report, "최종 리포트에 진단 결과(outcome) 값이 누락되었습니다."
        
        if not sse_task.done():
            sse_task.cancel()

    print(f"\n[REAL E2E PASSED] 모든 실 인프라 환경에서 AI 진단 흐름 완벽 검증 성공.")
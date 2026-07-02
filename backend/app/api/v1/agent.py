import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.agent.state import create_initial_state, AgentRuntimeState
from app.tools.run_load_test import run_load_test, LoadTestError
from app.tools.scale_service import scale_service

# 이 파일 하나로 라우팅까지 끝내기 위해 라우터 객체 선언
router = APIRouter()
executor = ThreadPoolExecutor(max_workers=3)


# =================================================================
# 🔒 비동기 자물쇠(Event)와 상태를 안전하게 관리하는 매니저
# =================================================================
class AgentTaskManager:
    def __init__(self):
        self.states: dict[str, AgentRuntimeState] = {}
        self.futures: dict[str, asyncio.Future] = {}

    def register_task(self, state: AgentRuntimeState) -> str:
        task_id = state["task_id"]

        self.states[task_id] = state

        loop = asyncio.get_running_loop()
        self.futures[task_id] = loop.create_future()

        return task_id

    async def wait_for_approval(self, task_id: str) -> bool:
        print(f"[wait] waiting... {task_id}")

        future = self.futures[task_id]

        approved = await future

        print(f"[wait] resumed! approved={approved}")

        return approved

    def approve_task(self, task_id: str, approved: bool) -> bool:
        print(f"[approve] task={task_id}, approved={approved}")

        future = self.futures.get(task_id)
        state = self.states.get(task_id)

        if future is None or state is None:
            print("[approve] task not found")
            return False

        if future.done():
            print("[approve] already completed")
            return False

        state["scaling_approved"] = approved

        future.set_result(approved)

        print("[approve] future completed")

        return True
        

# 전역 매니저 인스턴스
task_manager = AgentTaskManager()


# =================================================================
# 📥 [POST] 프론트엔드가 승인/거절 누르면 때리는 엔드포인트
# =================================================================
class ApprovalRequest(BaseModel):
    task_id: str
    approved: bool

@router.post("/agent/approve")
async def approve_task(req: ApprovalRequest):
    """
    주황색 모달창에서 [승인] / [거절] 버튼을 누르면 호출되는 API입니다.
    """
    success = task_manager.approve_task(req.task_id, req.approved)
    if success:
        return {"status": "success", "message": f"Task {req.task_id} 승인 상태 반영 완료"}
    return {"status": "error", "message": "해당 작업 ID를 찾을 수 없습니다."}


# =================================================================
# 📄 [GET] 최종 리포트 조회 엔드포인트
# =================================================================
@router.get("/agent/report/{task_id}")
async def get_report(task_id: str):
    """
    지금까지의 측정값(부하 테스트 결과)과 실제로 취해진 조치(스케일링 결과)를
    함께 반환합니다. 진단이 끝나지 않았어도(진행 중이어도) 그 시점까지의
    값을 그대로 보여줍니다.
    """
    state = task_manager.states.get(task_id)

    if state is None:
        raise HTTPException(status_code=404, detail="해당 task_id를 찾을 수 없습니다.")

    load_test_result = state.get("load_test_result")
    scaling_result = state.get("scaling_result")

    measurement = None
    if load_test_result is not None:
        measurement = {
            "tps": load_test_result.tps,
            "error_rate": load_test_result.error_rate,
            "latency_p95": load_test_result.latency_p95,   # ms
            "latency_avg": load_test_result.latency_avg,   # ms
            "total_requests": load_test_result.total_requests,
            "duration": load_test_result.duration,          # 초
        }

    action = None
    if scaling_result is not None:
        action = {
            "before_replicas": scaling_result.before_replicas,
            "after_replicas": scaling_result.after_replicas,
            "success": scaling_result.success,
            "error_message": scaling_result.error_message,
        }

    return {
        "task_id": task_id,
        "outcome": state.get("agent_outcome"),           # diagnosed / awaiting_approval / scaled / failed 등
        "waiting_for_approval": state.get("waiting_for_approval", False),
        "measurement": measurement,                       # 부하 테스트 실측값 (없으면 None = 아직 측정 전)
        "action": action,                                  # 실제 스케일링 조치 결과 (없으면 None = 아직 조치 전)
        "error": state.get("error"),
    }


# =================================================================
# 📤 [GET] 실시간 진단 로그 스트리밍 엔드포인트
# =================================================================
@router.get("/agent/start")
async def start_agent(target_tps: int = 30, duration: int = 10):
    """
    [진단 시작] 버튼을 누르면 호출되는 SSE 스트리밍 엔드포인트입니다.
    """
    return StreamingResponse(
        start_agent_stream(target_tps, duration), 
        media_type="text/event-stream"
    )


async def start_agent_stream(target_tps: int = 30, duration: int = 10):
    """
    인프라 자율 진단 및 조치 프로세스를 실시간으로 스트리밍하는 핵심 로직.
    """
    
    # 1. AgentRuntimeState 데이터 초기화
    try:
        state = create_initial_state(target_tps=target_tps, duration=duration)
    except ValueError as e:
        yield f"data: {{\"status\": \"failed\", \"task_id\": null, \"message\": \"[입력 에러] {str(e)}\"}}\n\n"
        return

    task_id = task_manager.register_task(state)

    # =================================================================
    # [1단계: 인프라 헬스체크]
    # =================================================================
    yield f"data: {{\"status\": \"running\", \"task_id\": \"{task_id}\", \"message\": \"[1/6] 로컬 인프라(Prometheus) 생사 확인 중...\"}}\n\n"
    await asyncio.sleep(0.5)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get("http://localhost:9090/", timeout=3.0)
            if response.status_code not in [200, 302]:
                yield f"data: {{\"status\": \"failed\", \"task_id\": \"{task_id}\", \"message\": \"❌ 프로메테우스 인프라 응답 비정상 (Status: {response.status_code})\"}}\n\n"
                return
        except (httpx.ConnectError, httpx.TimeoutException):
            yield f"data: {{\"status\": \"failed\", \"task_id\": \"{task_id}\", \"message\": \"❌ [에러] 도커 인프라가 꺼져 있거나 응답이 없습니다! Docker Desktop을 확인해 주세요.\"}}\n\n"
            return

    yield f"data: {{\"status\": \"running\", \"task_id\": \"{task_id}\", \"message\": \"🟢 로컬 도커 인프라 연결 확인 완료!\"}}\n\n"
    await asyncio.sleep(0.5)


    # =================================================================
    # [2단계: 부하 테스트 실행] 
    # =================================================================
    yield f"data: {{\"status\": \"analyzing\", \"task_id\": \"{task_id}\", \"message\": \"[2/6] target-server 대상 부하 테스트(Locust) 가동 중... ({duration}초 대기)\"}}\n\n"
    
    try:
        loop = asyncio.get_running_loop()
        load_test_result = await loop.run_in_executor(
            executor, 
            run_load_test, 
            target_tps, 
            duration
        )
        
        state["load_test_result"] = load_test_result
        state["agent_outcome"] = "diagnosed"
        
        yield f"data: {{\"status\": \"analyzing\", \"task_id\": \"{task_id}\", \"message\": \"📊 부하 테스트 완료! (실측 TPS: {load_test_result.tps:.1f} / 에러율: {load_test_result.error_rate * 100:.1f}%)\"}}\n\n"
        await asyncio.sleep(1.0)

    except LoadTestError as e:
        state["agent_outcome"] = "failed"
        state["error"] = str(e)
        yield f"data: {{\"status\": \"failed\", \"task_id\": \"{task_id}\", \"message\": \"❌ [부하 테스트 실패] {str(e)}\"}}\n\n"
        return

    except Exception as e:
        # LoadTestError로 분류되지 않은 예외(예: Locust 실행 환경 문제, subprocess 오류 등).
        # 여기서 안 잡으면 generator 전체가 죽어서 SSE 연결이 비정상 종료되고,
        # 프론트는 원인을 알 수 없는 "도커 다운" 메시지만 보게 된다.
        import traceback
        traceback.print_exc()  # uvicorn 콘솔에 실제 스택트레이스 출력

        state["agent_outcome"] = "failed"
        state["error"] = str(e)

        safe_message = str(e).replace('"', "'").replace("\n", " ")
        yield f"data: {{\"status\": \"failed\", \"task_id\": \"{task_id}\", \"message\": \"❌ [부하 테스트 중 예외 발생] {safe_message}\"}}\n\n"
        return


    # =================================================================
    # [3단계 ~ 4단계: 모니터링 및 AI 분석 목업]
    # =================================================================
    yield f"data: {{\"status\": \"analyzing\", \"task_id\": \"{task_id}\", \"message\": \"[3/6] Prometheus에서 실시간 자원 메트릭 수집 중...\"}}\n\n"
    await asyncio.sleep(1.5)

    yield f"data: {{\"status\": \"analyzing\", \"task_id\": \"{task_id}\", \"message\": \"[4/6] AI 에이전트가 병목 구간 추론 및 가드레일 조건 검증 중...\"}}\n\n"
    await asyncio.sleep(1.5)


    # =================================================================
    # [5단계: 가드레일 승인 대기] ── 안전한 비동기 자물쇠 작동 🔒
    # =================================================================
    state["waiting_for_approval"] = True
    state["agent_outcome"] = "awaiting_approval"
    
    yield f"data: {{\"status\": \"need_approval\", \"task_id\": \"{task_id}\", \"message\": \"⚠️ [가드레일 제한] /heavy 엔드포인트 세마포어 임계치 초과 발생! 대시보드에서 승인이 필요합니다.\"}}\n\n"
    
    # 🔒 유저가 위의 approve_task API를 호출해 줄 때까지 락 걸고 대기 (자원 소모 없음)
    approved = await task_manager.wait_for_approval(task_id)

    if not approved:
        state["waiting_for_approval"] = False
        state["agent_outcome"] = "failed"
        yield f"data: {{\"status\": \"failed\", \"task_id\": \"{task_id}\", \"message\": \"❌ 사용자가 스케일아웃 조치를 거절했습니다. 프로세스를 중단합니다.\"}}\n\n"
        return


    # =================================================================
    # [6단계: 인프라 조치 및 최종 리포트]
    # =================================================================
    state["waiting_for_approval"] = False

    yield (
        f"data: {{"
        f"\"status\":\"scaling\","
        f"\"task_id\":\"{task_id}\","
        f"\"message\":\"[5/6] 승인 확인됨. Docker Scale-out 실행 중...\""
        f"}}\n\n"
    )

    target_replicas = 2

    print("=== scale_service 호출 직전 ===")

    try:
        result = await scale_service(target_replicas)
    except Exception as e:
        # scale_service 내부에서 못 잡은 예외(예상 못한 OSError 등).
        # 여기서 안 잡으면 generator가 죽어서 SSE 연결이 비정상 종료되고,
        # 프론트는 원인을 알 수 없는 "도커 다운" 메시지만 보게 된다.
        import traceback
        traceback.print_exc()

        state["agent_outcome"] = "failed"
        state["error"] = str(e)

        safe_message = str(e).replace('"', "'").replace("\n", " ")
        yield (
            f"data: {{"
            f"\"status\":\"failed\","
            f"\"task_id\":\"{task_id}\","
            f"\"message\":\"❌ [스케일링 중 예외 발생] {safe_message}\""
            f"}}\n\n"
        )
        return

    print(result)

    if not result.success:
        state["agent_outcome"] = "failed"

        yield (
            f"data: {{"
            f"\"status\":\"failed\","
            f"\"task_id\":\"{task_id}\","
            f"\"message\":\"❌ Scale 실패 : {result.error_message}\""
            f"}}\n\n"
        )

        return

    state["scaling_result"] = result
    state["agent_outcome"] = "scaled"

    yield (
        f"data: {{"
        f"\"status\":\"done\","
        f"\"task_id\":\"{task_id}\","
        f"\"message\":\"[6/6] 🎉 Scale 완료 ({result.before_replicas} → {result.after_replicas})\""
        f"}}\n\n"
    )
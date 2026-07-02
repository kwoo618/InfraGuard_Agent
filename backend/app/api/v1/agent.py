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

# state.py의 AgentRuntimeState 스펙을 건드리지 않기 위해,
# 재측정(re-measurement) 결과는 agent.py 자체 저장소에 따로 보관한다.
# key: task_id, value: LoadTestResult (스케일링 후 재측정 결과)
remeasurement_results: dict[str, "LoadTestResult"] = {}


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

    def approve_task(self, task_id: str, approved: bool) -> str:
        """
        반환값: "success" | "not_found" | "already_done"
        """
        print(f"[approve] task={task_id}, approved={approved}")

        future = self.futures.get(task_id)
        state = self.states.get(task_id)

        if future is None or state is None:
            print("[approve] task not found")
            return "not_found"

        if future.done():
            print("[approve] already completed")
            return "already_done"

        state["scaling_approved"] = approved

        future.set_result(approved)

        print("[approve] future completed")

        return "success"
        

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
    result = task_manager.approve_task(req.task_id, req.approved)

    if result == "success":
        return {"status": "success", "message": f"Task {req.task_id} 승인 상태 반영 완료"}

    if result == "already_done":
        return {"status": "error", "message": "이미 처리된 작업입니다. (중복 클릭)"}

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
    load_test_result_after = remeasurement_results.get(task_id)   # 재측정 결과 (없으면 아직 재측정 전/실패)
    scaling_result = state.get("scaling_result")

    def _serialize_measurement(r):
        if r is None:
            return None
        return {
            "tps": r.tps,
            "error_rate": r.error_rate,
            "latency_p95": r.latency_p95,   # ms
            "latency_avg": r.latency_avg,   # ms
            "total_requests": r.total_requests,
            "duration": r.duration,          # 초
        }

    measurement = _serialize_measurement(load_test_result)
    measurement_after = _serialize_measurement(load_test_result_after)

    improvement = None
    if measurement is not None and measurement_after is not None:
        improvement = {
            "tps_delta": measurement_after["tps"] - measurement["tps"],
            "latency_p95_delta": measurement_after["latency_p95"] - measurement["latency_p95"],   # 음수면 개선
            "error_rate_delta": measurement_after["error_rate"] - measurement["error_rate"],
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
        "measurement": measurement,                       # 스케일링 전 부하 테스트 실측값
        "measurement_after": measurement_after,            # 스케일링 후 재측정 결과 (없으면 None)
        "improvement": improvement,                         # 전/후 비교 (없으면 None = 재측정 안 됨)
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
    
    # 1. AgentState 데이터 초기화
    try:
        state = create_initial_state(target_tps=target_tps, duration=duration)
    except ValueError as e:
        yield f"data: {{\"status\": \"failed\", \"task_id\": null, \"message\": \"[입력 에러] {str(e)}\"}}\n\n"
        return

    task_id = task_manager.register_task(state)

    # =================================================================
    # [1단계: 인프라 헬스체크]
    # =================================================================
    yield f"data: {{\"status\": \"running\", \"task_id\": \"{task_id}\", \"message\": \"🔍 인프라 연결 확인 중...\"}}\n\n"
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

    yield f"data: {{\"status\": \"running\", \"task_id\": \"{task_id}\", \"message\": \"✅ 인프라 연결 확인\"}}\n\n"
    await asyncio.sleep(0.5)


    # =================================================================
    # [2단계: 부하 테스트 실행] 
    # =================================================================
    yield f"data: {{\"status\": \"analyzing\", \"task_id\": \"{task_id}\", \"message\": \"⚡ 부하 테스트 진행 중... ({duration}초)\"}}\n\n"
    
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
        
        yield f"data: {{\"status\": \"analyzing\", \"task_id\": \"{task_id}\", \"message\": \"✅ 부하 테스트 완료  TPS {load_test_result.tps:.1f} / 에러율 {load_test_result.error_rate * 100:.1f}%\"}}\n\n"
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
    yield f"data: {{\"status\": \"analyzing\", \"task_id\": \"{task_id}\", \"message\": \"📊 메트릭 수집 중...\"}}\n\n"
    await asyncio.sleep(1.0)

    yield f"data: {{\"status\": \"analyzing\", \"task_id\": \"{task_id}\", \"message\": \"✅ 메트릭 수집 완료\"}}\n\n"
    await asyncio.sleep(0.3)

    yield f"data: {{\"status\": \"analyzing\", \"task_id\": \"{task_id}\", \"message\": \"🧠 AI 분석 중...\"}}\n\n"
    await asyncio.sleep(1.5)


    # =================================================================
    # [5단계: 가드레일 승인 대기] ── 안전한 비동기 자물쇠 작동 🔒
    # =================================================================
    state["waiting_for_approval"] = True
    state["agent_outcome"] = "awaiting_approval"
    
    yield f"data: {{\"status\": \"need_approval\", \"task_id\": \"{task_id}\", \"message\": \"⚠️ 병목 감지 — 서버 증설이 필요합니다. 승인해주세요.\"}}\n\n"
    
    # 🔒 유저가 위의 approve_task API를 호출해 줄 때까지 락 걸고 대기 (자원 소모 없음)
    approved = await task_manager.wait_for_approval(task_id)

    if not approved:
        state["waiting_for_approval"] = False
        state["agent_outcome"] = "failed"
        yield f"data: {{\"status\": \"failed\", \"task_id\": \"{task_id}\", \"message\": \"❌ 서버 증설이 거부되었습니다.\"}}\n\n"
        return


    # =================================================================
    # [6단계: 인프라 조치 및 최종 리포트]
    # =================================================================
    state["waiting_for_approval"] = False

    yield (
        f"data: {{"
        f"\"status\":\"scaling\","
        f"\"task_id\":\"{task_id}\","
        f"\"message\":\"🚀 서버 증설 중...\""
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
        f"\"status\":\"scaling\","
        f"\"task_id\":\"{task_id}\","
        f"\"message\":\"✅ 서버 증설 완료  {result.before_replicas}대 → {result.after_replicas}대\""
        f"}}\n\n"
    )

    # =================================================================
    # [재측정] 스케일링 후 실제로 성능이 개선됐는지 동일 조건으로 재검증
    # =================================================================
    yield (
        f"data: {{"
        f"\"status\":\"remeasuring\","
        f"\"task_id\":\"{task_id}\","
        f"\"message\":\"🔁 재측정 중...\""
        f"}}\n\n"
    )

    load_test_result_after = None
    try:
        load_test_result_after = await loop.run_in_executor(
            executor,
            run_load_test,
            target_tps,
            duration,
        )
        remeasurement_results[task_id] = load_test_result_after

    except LoadTestError as e:
        # 재측정 실패는 스케일링 자체의 성공 여부에 영향을 주지 않는다. 전/후 비교만 못 할 뿐.
        yield (
            f"data: {{"
            f"\"status\":\"scaling\","
            f"\"task_id\":\"{task_id}\","
            f"\"message\":\"⚠️ 재측정 실패 (서버 증설은 완료됨): {str(e)}\""
            f"}}\n\n"
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        safe_message = str(e).replace('"', "'").replace("\n", " ")
        yield (
            f"data: {{"
            f"\"status\":\"scaling\","
            f"\"task_id\":\"{task_id}\","
            f"\"message\":\"⚠️ 재측정 중 예외 발생 (서버 증설은 완료됨): {safe_message}\""
            f"}}\n\n"
        )

    if load_test_result_after is not None:
        final_message = f"✅ 최종 응답시간  P95 {load_test_result_after.latency_p95:.0f}ms"
    else:
        final_message = "🎉 진단이 완료됐습니다."

    yield (
        f"data: {{"
        f"\"status\":\"done\","
        f"\"task_id\":\"{task_id}\","
        f"\"message\":\"{final_message}\""
        f"}}\n\n"
    )
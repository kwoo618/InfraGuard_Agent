import asyncio
import os

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.engine import resume_after_approval, run_diagnosis
from app.agent.state import AgentRuntimeState, create_initial_state

# 이 파일 하나로 라우팅까지 끝내기 위해 라우터 객체 선언
router = APIRouter()

# 디버그 전용 기능(force_scaling) 활성화 여부.
# 인증 없이 URL 파라미터 하나로 실제 Docker 스케일링을 유발할 수 있는 기능이라,
# 로컬 개발 환경에서만 명시적으로 켜도록 기본값을 꺼짐(false)으로 둔다.
# 켜려면 .env에 DEBUG_ENDPOINTS_ENABLED=true 추가.
DEBUG_ENDPOINTS_ENABLED = os.getenv("DEBUG_ENDPOINTS_ENABLED", "false").lower() == "true"

# engine.resume_after_approval()이 재검증을 마치면 state["load_test_result"]는
# "스케일링 전" 값이 아니라 "재검증(스케일링 후)" 값으로 덮어써진다(before/after를
# 따로 안 들고 있음). /report에서 전/후 비교를 보여주려면 "전" 값을 스케일링 호출
# 직전에 별도로 스냅샷해둬야 하는데, state.py 스펙은 건드리지 않기 위해 agent.py
# 자체 저장소에 따로 보관한다. key: task_id, value: LoadTestResult(스케일링 전).
before_measurements: dict[str, "LoadTestResult"] = {}


def _sse(status: str, task_id: str | None, message: str) -> str:
    """
    SSE data 프레임을 안전하게 만든다.

    LLM이 생성한 문자열(cause/recommendation 등)이 message에 그대로 들어오는 경우가
    많아졌기 때문에, 따옴표/줄바꿈을 이스케이프하지 않으면 JSON이 깨질 수 있다.
    """
    safe_message = message.replace('"', "'").replace("\n", " ")
    task_id_json = f"\"{task_id}\"" if task_id else "null"
    return f"data: {{\"status\": \"{status}\", \"task_id\": {task_id_json}, \"message\": \"{safe_message}\"}}\n\n"


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
        return task_id

    def create_approval_gate(self, task_id: str) -> None:
        """
        승인이 필요한 라운드마다 새 Future를 만든다.

        ReAct Loop 특성상 한 task_id로 승인 요청이 여러 번(스케일링 후에도
        개선이 부족하면 다시 승인 대기) 발생할 수 있어서, register_task
        시점에 한 번만 만드는 게 아니라 매 라운드 새로 발급해야 한다.
        """
        loop = asyncio.get_running_loop()
        self.futures[task_id] = loop.create_future()

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
    측정값(load_test_result, 스케일링 전/후), LLM 병목 진단(bottleneck_report),
    최적화 조치 목록(optimization_plan), 스케일링 조치(scaling_result),
    그리고 LLM이 생성한 최종 요약(final_answer)을 함께 반환합니다.
    """
    state = task_manager.states.get(task_id)

    if state is None:
        raise HTTPException(status_code=404, detail="해당 task_id를 찾을 수 없습니다.")

    load_test_result = state.get("load_test_result")
    scaling_result = state.get("scaling_result")
    bottleneck_report = state.get("bottleneck_report")
    before_load_test_result = before_measurements.get(task_id)

    def _serialize_measurement(result):
        if result is None:
            return None
        return {
            "tps": result.tps,
            "error_rate": result.error_rate,
            "latency_p95": result.latency_p95,   # ms
            "latency_avg": result.latency_avg,   # ms
            "total_requests": result.total_requests,
            "duration": result.duration,          # 초
        }

    measurement = None
    measurement_after = None
    improvement = None

    if before_load_test_result is not None and scaling_result is not None:
        # 스케일링이 실제로 일어난 경우: 전/후를 분리해서 보여준다.
        measurement = _serialize_measurement(before_load_test_result)
        measurement_after = _serialize_measurement(load_test_result)

        if measurement_after is not None:
            improvement = {
                "tps_delta": measurement_after["tps"] - measurement["tps"],
                "latency_p95_delta": measurement_after["latency_p95"] - measurement["latency_p95"],   # 음수면 개선
                "error_rate_delta": measurement_after["error_rate"] - measurement["error_rate"],
            }
    else:
        # 스케일링이 없었던 경우(병목 없음 등): 측정값이 하나뿐이다.
        measurement = _serialize_measurement(load_test_result)

    bottleneck = None
    if bottleneck_report is not None:
        bottleneck = {
            "cause": bottleneck_report.cause,
            "severity": bottleneck_report.severity,
            "recommendation": bottleneck_report.recommendation,
            "confidence": bottleneck_report.confidence,
            "requires_scaling": bottleneck_report.requires_scaling,
        }

    action = None
    if scaling_result is not None:
        action = {
            "before_replicas": scaling_result.before_replicas,
            "after_replicas": scaling_result.after_replicas,
            "success": scaling_result.success,
            "error_message": scaling_result.error_message,
        }

    optimization_plan = state.get("optimization_plan") or []

    return {
        "task_id": task_id,
        "outcome": state.get("agent_outcome"),           # diagnosed / awaiting_approval / scaled / failed 등
        "waiting_for_approval": state.get("waiting_for_approval", False),
        "loop_count": state.get("loop_count"),
        "measurement": measurement,           # 스케일링 전(또는 유일한) 측정값
        "measurement_after": measurement_after,   # 스케일링 후 재검증 측정값 (스케일링 없었으면 None)
        "improvement": improvement,               # 전/후 개선폭 (스케일링 없었으면 None)
        "bottleneck": bottleneck,     # LLM 병목 진단 결과
        "action": action,             # 실제 스케일링 조치 결과 (없으면 None = 스케일링 없었음)
        "optimization_plan": optimization_plan,   # 최적화 조치 목록 (generate_plan_node 결과)
        "summary": state.get("final_answer"),   # LLM이 생성한 자연어 최종 요약
        "error": state.get("error"),
    }


# =================================================================
# 📤 [GET] 실시간 진단 로그 스트리밍 엔드포인트
# =================================================================
@router.get("/agent/start")
async def start_agent(target_tps: int = 30, duration: int = 10, force_scaling: bool = False):
    """
    [진단 시작] 버튼을 누르면 호출되는 SSE 스트리밍 엔드포인트입니다.

    force_scaling=true로 호출하면, 실제 LLM이 "병목 없음"으로 판단해도
    강제로 스케일링 승인 흐름(need_approval → scaling → remeasuring)을
    태워서 HITL·스케일링·재검증 UI/배선을 수동으로 테스트할 수 있습니다.
    (디버그/개발용 — 부하테스트·메트릭·스케일링·재검증 자체는 전부 진짜로 실행됨)

    .env에 DEBUG_ENDPOINTS_ENABLED=true가 없으면 force_scaling은 무시됩니다.
    인증 없이 실제 Docker 스케일링을 유발할 수 있는 기능이라 기본값은 꺼짐입니다.
    """
    return StreamingResponse(
        start_agent_stream(target_tps, duration, force_scaling),
        media_type="text/event-stream"
    )


async def start_agent_stream(target_tps: int = 30, duration: int = 10, force_scaling: bool = False):
    """
    인프라 자율 진단 및 조치 프로세스를 실시간으로 스트리밍하는 핵심 로직.

    실제 부하 테스트 + 메트릭 수집 + LLM 병목 진단은 app.agent.engine.run_diagnosis가,
    승인 후 스케일링 + LLM 재검증은 app.agent.engine.resume_after_approval이 담당한다.
    이 함수는 그 결과를 받아 SSE로 중계하고, 스케일링 후에도 개선이 부족해 LLM이
    다시 승인을 요구하면(ReAct Loop) MAX_LOOP까지 반복해서 승인 단계로 돌아간다.
    """

    # 1. AgentRuntimeState 데이터 초기화
    try:
        state = create_initial_state(target_tps=target_tps, duration=duration)
    except ValueError as e:
        yield _sse("failed", None, f"[입력 에러] {str(e)}")
        return

    task_id = task_manager.register_task(state)

    # =================================================================
    # [1단계: 인프라 헬스체크]
    # =================================================================
    yield _sse("running", task_id, "🔍 인프라 연결 확인 중...")
    await asyncio.sleep(0.3)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{os.getenv('PROMETHEUS_URL', 'http://localhost:9090')}/", timeout=3.0)
            if response.status_code not in [200, 302]:
                yield _sse("failed", task_id, f"❌ 프로메테우스 인프라 응답 비정상 (Status: {response.status_code})")
                return
        except (httpx.ConnectError, httpx.TimeoutException):
            yield _sse("failed", task_id, "❌ 도커 인프라가 꺼져 있거나 응답이 없습니다! Docker Desktop을 확인해 주세요.")
            return

    yield _sse("running", task_id, "✅ 인프라 연결 확인")
    await asyncio.sleep(0.3)

    # =================================================================
    # [2~4단계: 부하 테스트 + 메트릭 수집 + AI 병목 진단]
    # engine.run_diagnosis가 세 단계를 순차적으로 실행하고,
    # 실패해도 예외를 던지지 않고 agent_outcome="failed" 상태로 안전하게 반환한다.
    # =================================================================
    yield _sse("analyzing", task_id, f"⚡ 부하 테스트 진행 중... ({duration}초)")

    state = await run_diagnosis(state)

    if state["agent_outcome"] == "failed":
        yield _sse("failed", task_id, f"❌ {state.get('error') or '진단에 실패했습니다.'}")
        return

    load_test_result = state.get("load_test_result")
    if load_test_result is not None:
        yield _sse(
            "analyzing",
            task_id,
            f"✅ 부하 테스트 완료  TPS {load_test_result.tps:.1f} / 에러율 {load_test_result.error_rate * 100:.1f}%",
        )

    yield _sse("analyzing", task_id, "🧠 AI 분석 완료")

    # ---- [디버그 전용] force_scaling=true인데 실제로는 병목이 없다고 판단된 경우 ----
    # HITL 승인 → 스케일링 → 재검증 흐름을 수동으로 테스트하기 위해,
    # 병목 판단 결과만 강제로 덮어쓴다. 이후 로직(승인 대기, execute_scaling_node,
    # 재검증)은 전부 실제 코드 경로를 그대로 탄다 — 가짜인 건 "병목이 있다는 판단"뿐이다.
    #
    # main.js가 force_scaling=true를 매 요청마다 자동으로 보내기 때문에,
    # DEBUG_ENDPOINTS_ENABLED가 꺼져있는(=정상적인 프로덕션/일반 사용자) 경우에도
    # 이 분기를 매번 타게 된다. 화면에 디버그 경고를 노출하면 사용자가 오해할 수
    # 있으므로, 꺼져있을 땐 조용히 무시하고 서버 콘솔에만 남긴다.
    if force_scaling and not DEBUG_ENDPOINTS_ENABLED:
        print(f"[force_scaling] 무시됨 (DEBUG_ENDPOINTS_ENABLED=false) task_id={task_id}")

    if force_scaling and DEBUG_ENDPOINTS_ENABLED and state["agent_outcome"] == "diagnosed":
        from app.tools.scale_service import SERVICE_NAME, get_current_replicas

        current_replicas = max(get_current_replicas(), 1)

        yield _sse("analyzing", task_id, "🧪 [디버그] force_scaling=true — 병목 판단을 강제로 덮어씁니다.")

        state.update({
            "agent_outcome": "awaiting_approval",
            "scaling_required": True,
            "waiting_for_approval": True,
            "scaling_plan": {
                "service_name": SERVICE_NAME,
                "current_replicas": current_replicas,
                "desired_replicas": current_replicas + 1,
                "reason": "[디버그] force_scaling 파라미터로 강제 지정된 사유입니다.",
            },
            "final_answer": (
                f"[디버그] {SERVICE_NAME}를 {current_replicas}개에서 "
                f"{current_replicas + 1}개로 강제 확장 테스트를 진행합니다."
            ),
        })

    # =================================================================
    # [5~6단계: 승인 → 스케일링 → 재검증] — ReAct Loop
    # 병목이 없으면(diagnosed) 바로 종료.
    # 병목이 있으면(awaiting_approval) 승인 받고 스케일링 + 재검증.
    # 재검증 후에도 개선이 부족하면 LLM이 다시 awaiting_approval을 반환할 수 있어
    # MAX_LOOP(engine/nodes.py에서 강제)까지 이 루프를 반복한다.
    # =================================================================
    while True:
        outcome = state["agent_outcome"]

        if outcome == "diagnosed":
            summary = state.get("final_answer") or "병목이 발견되지 않았습니다."
            yield _sse("done", task_id, f"✅ {summary}")
            return

        if outcome == "failed":
            yield _sse("failed", task_id, f"❌ {state.get('error') or '진단에 실패했습니다.'}")
            return

        if outcome != "awaiting_approval":
            # 예상 못한 상태값에 대한 방어 (engine/nodes 계약이 바뀌었을 가능성)
            yield _sse("failed", task_id, f"❌ 알 수 없는 진단 상태입니다: {outcome}")
            return

        # ---- 승인 요청 ----
        task_manager.create_approval_gate(task_id)

        approval_message = state.get("final_answer") or "병목 감지 — 서버 증설이 필요합니다. 승인해주세요."
        yield _sse("need_approval", task_id, f"⚠️ {approval_message}")

        # 🔒 유저가 위의 approve_task API를 호출해 줄 때까지 락 걸고 대기 (자원 소모 없음)
        approved = await task_manager.wait_for_approval(task_id)

        # resume_after_approval()이 재검증하면서 load_test_result를 "이후" 값으로
        # 덮어쓰기 전에, "이전" 값을 스냅샷해둔다. (다회차 루프에서도 최초 1회만 저장)
        if task_id not in before_measurements and state.get("load_test_result") is not None:
            before_measurements[task_id] = state["load_test_result"]

        state = await resume_after_approval(state, approved)

        if not approved:
            message = state.get("final_answer") or "서버 증설이 거부되었습니다."
            yield _sse("failed", task_id, f"❌ {message}")
            return

        if state["agent_outcome"] == "failed":
            yield _sse("failed", task_id, f"❌ {state.get('error') or '스케일링에 실패했습니다.'}")
            return

        # ---- 스케일링 결과 안내 ----
        scaling_result = state.get("scaling_result")
        if scaling_result is not None:
            yield _sse(
                "scaling",
                task_id,
                f"✅ 서버 증설 완료  {scaling_result.before_replicas}대 → {scaling_result.after_replicas}대",
            )

        yield _sse("remeasuring", task_id, "🔁 재검증 중...")

        if state["agent_outcome"] == "scaled":
            summary = state.get("final_answer") or "스케일링 후 성능이 개선됐습니다."
            yield _sse("done", task_id, f"✅ {summary}")
            return

        # agent_outcome이 다시 "awaiting_approval"(또는 "diagnosed")이면
        # while 루프 맨 위로 돌아가 그 상태에 맞게 처리한다.
        continue
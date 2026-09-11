import asyncio
import json
import os

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.engine import resume_after_approval, run_diagnosis
from app.agent.nodes import call_solar_api
from app.agent.state import AgentRuntimeState, create_initial_state
from app.api.v1.run_history import RunRecorder
from app.tools.scale_service import get_current_replicas

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

# 실행 1회의 라운드별 측정 이력·AI 판단·사용자 결정·스케일링 결과를 모으는 기록기.
# before_measurements와 같은 이유로 state.py 대신 agent.py 자체 저장소에 둔다.
# /report의 measurement_history와 results/*.json 결과 파일이 여기서 나온다 (docs/03 Phase 3, #75).
# key: task_id, value: RunRecorder
run_records: dict[str, RunRecorder] = {}

# HITL 지점 2 (CLAUDE.md "진단 결과 불확실 시"): LLM 신뢰도가 이 값 미만인 스케일링 제안은
# 사용자가 낮은 신뢰도를 확인해야 승인할 수 있다 (#83, docs/02 ISSUE-13).
# 가드레일 값이라 환경변수로 두지 않는다. 기준값과 같은 0.6은 게이트 대상이 아니다.
LOW_CONFIDENCE_THRESHOLD = 0.6


def _is_low_confidence(state: AgentRuntimeState) -> bool:
    """승인을 요청하는 LLM 판단이 스케일링 제안이면서 신뢰도가 기준 미만인지."""

    report = state.get("bottleneck_report")
    return (
        report is not None
        and report.requires_scaling
        and report.confidence < LOW_CONFIDENCE_THRESHOLD
    )


def _sse(
    status: str,
    task_id: str | None,
    message: str,
    extra: dict | None = None,
) -> str:
    """
    SSE data 프레임을 안전하게 만든다.

    LLM이 생성한 문자열(cause/recommendation 등)이 message에 그대로 들어오는 경우가
    많아졌기 때문에, 따옴표/줄바꿈을 이스케이프하지 않으면 JSON이 깨질 수 있다.
    extra는 status/task_id/message 뒤에 붙는 추가 필드다 (값은 json.dumps로 직렬화).
    """
    safe_message = message.replace('"', "'").replace("\n", " ")
    task_id_json = f"\"{task_id}\"" if task_id else "null"
    extra_json = "".join(
        f", {json.dumps(key)}: {json.dumps(value)}"
        for key, value in (extra or {}).items()
    )
    return f"data: {{\"status\": \"{status}\", \"task_id\": {task_id_json}, \"message\": \"{safe_message}\"{extra_json}}}\n\n"


async def _read_replicas() -> int:
    """
    현재 target-server replica 수를 읽는다.

    docker compose ps를 subprocess로 실행하는 동기 함수라 이벤트 루프를 막지 않게
    스레드에서 실행한다. 조회 실패는 0 (결과 파일에는 null로 저장된다).
    """
    try:
        return await asyncio.to_thread(get_current_replicas)
    except Exception as exc:
        print(f"[run_history] replica 조회 실패: {exc}")
        return 0


# =================================================================
# 🔒 비동기 자물쇠(Event)와 상태를 안전하게 관리하는 매니저
# =================================================================
class AgentTaskManager:
    def __init__(self):
        self.states: dict[str, AgentRuntimeState] = {}
        self.futures: dict[str, asyncio.Future] = {}
        # 현재 승인 요청이 저신뢰 확인 게이트인지 (#83). /approve가 확인 플래그를 요구할지 정한다
        self.low_confidence: dict[str, bool] = {}
        # 사용자가 결정과 함께 보낸 acknowledge_low_confidence 값 (결과 파일 approvals[].acknowledged)
        self.acknowledgements: dict[str, bool] = {}

    def register_task(self, state: AgentRuntimeState) -> str:
        task_id = state["task_id"]
        self.states[task_id] = state
        return task_id

    def create_approval_gate(self, task_id: str, low_confidence: bool = False) -> None:
        """
        승인이 필요한 라운드마다 새 Future를 만든다.

        ReAct Loop 특성상 한 task_id로 승인 요청이 여러 번(스케일링 후에도
        개선이 부족하면 다시 승인 대기) 발생할 수 있어서, register_task
        시점에 한 번만 만드는 게 아니라 매 라운드 새로 발급해야 한다.

        low_confidence=True면 이번 요청은 확인 플래그 없이는 승인을 받지 않는다 (#83).
        """
        loop = asyncio.get_running_loop()
        self.futures[task_id] = loop.create_future()
        self.low_confidence[task_id] = low_confidence
        self.acknowledgements.pop(task_id, None)

    async def wait_for_approval(self, task_id: str) -> bool:
        print(f"[wait] waiting... {task_id}")
        future = self.futures[task_id]
        approved = await future
        print(f"[wait] resumed! approved={approved}")
        return approved

    def approve_task(
        self,
        task_id: str,
        approved: bool,
        acknowledge_low_confidence: bool = False,
    ) -> str:
        """
        반환값: "success" | "not_found" | "already_done" | "acknowledgement_required"

        저신뢰 확인 게이트(#83)가 걸린 요청을 확인 플래그 없이 승인하면
        "acknowledgement_required"를 반환하고 승인 대기를 그대로 둔다. 거절은 플래그 없이 받는다.
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

        if (
            approved
            and self.low_confidence.get(task_id, False)
            and not acknowledge_low_confidence
        ):
            print("[approve] low confidence - acknowledgement required")
            return "acknowledgement_required"

        self.acknowledgements[task_id] = acknowledge_low_confidence
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
    # 신뢰도 기준 미만 스케일링 제안을 승인할 때 true가 필요하다 (#83).
    # 기존 클라이언트 호환을 위해 기본값은 false. 거절에는 필요 없다
    acknowledge_low_confidence: bool = False

@router.post("/agent/approve")
async def approve_task(req: ApprovalRequest):
    """
    주황색 모달창에서 [승인] / [거절] 버튼을 누르면 호출되는 API입니다.

    신뢰도 기준 미만 스케일링 제안(#83)을 acknowledge_low_confidence 없이 승인하면 400을 반환합니다.
    UI 체크박스만이 아니라 API를 직접 호출해도 확인 없이 승인할 수 없게 하기 위해서입니다.
    """
    result = task_manager.approve_task(
        req.task_id,
        req.approved,
        req.acknowledge_low_confidence,
    )

    if result == "success":
        return {"status": "success", "message": f"Task {req.task_id} 승인 상태 반영 완료"}

    if result == "acknowledgement_required":
        raise HTTPException(
            status_code=400,
            detail=(
                f"AI 신뢰도가 기준({LOW_CONFIDENCE_THRESHOLD:.0%}) 미만인 스케일링 제안입니다. "
                "낮은 신뢰도를 확인했다면 acknowledge_low_confidence=true로 다시 승인하세요."
            ),
        )

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

    Phase 3(#75)에서 추가한 필드: measurement_history(라운드별 측정 이력),
    forced_scaling(force_scaling 덮어쓰기 여부), result_file(저장된 결과 파일 경로).

    Phase 4(#75)에서 추가한 필드 (UI 결과 패널용, 결과 파일과 같은 값):
    conditions, end_reason, user_decision, diagnoses, approvals, scaling_results, revalidations.
    기존 필드는 삭제·이름 변경하지 않는다.
    """
    state = task_manager.states.get(task_id)

    if state is None:
        raise HTTPException(status_code=404, detail="해당 task_id를 찾을 수 없습니다.")

    load_test_result = state.get("load_test_result")
    scaling_result = state.get("scaling_result")
    bottleneck_report = state.get("bottleneck_report")
    before_load_test_result = before_measurements.get(task_id)
    recorder = run_records.get(task_id)

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
        # 라운드별 측정 이력 (initial, after_scaling...). 결과 파일의 measurement_history와 같다
        "measurement_history": recorder.measurement_history if recorder else [],
        # force_scaling이 LLM 판단을 실제로 덮어썼으면 true (이 실행의 수치는 발표용이 아님)
        "forced_scaling": recorder.forced_scaling if recorder else False,
        # 저장된 결과 파일 경로 (repo 루트 기준). 실행이 끝나기 전이거나 저장 실패면 None
        "result_file": recorder.result_file_relative if recorder else None,
        # ---- Phase 4(#75) 결과 패널용 필드. 결과 파일의 같은 이름 항목과 같은 값이다 ----
        # 측정 조건 (virtual_users, duration_sec, p95_slo_ms, start_replicas, llm_model 등)
        "conditions": recorder.conditions if recorder else None,
        # 종료 경로 (no_scaling_proposed / scaled / rejected / failed 등). 실행이 끝나기 전이면 None
        "end_reason": recorder.end_reason if recorder else None,
        # 마지막 승인 요청 기준 사용자 결정 (approved / rejected / no_response / not_applicable)
        "user_decision": recorder.user_decision if recorder else None,
        "diagnoses": recorder.diagnoses if recorder else [],          # 라운드별 LLM 원본 판단
        "approvals": recorder.approvals if recorder else [],          # 승인 요청과 사용자 결정
        "scaling_results": recorder.scaling_results if recorder else [],
        "revalidations": recorder.revalidations if recorder else [],  # 재검증 요약 (LLM 변화량 제외)
        # ---- #83 저신뢰 확인 게이트 ----
        # 마지막 승인 요청이 신뢰도 기준 미만 스케일링 제안이었는지.
        # 승인 대기 중이면 /approve에 acknowledge_low_confidence=true가 있어야 승인된다
        "low_confidence": recorder.low_confidence if recorder else False,
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
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

    결과 저장 (Phase 3, #75): 실행이 끝나는 모든 경로(미제안·스케일링 완료·거절·실패)에서
    종료 SSE를 보내기 직전에 results/*.json으로 저장한다(RunRecorder.finalize).
    UI가 종료 이벤트를 받자마자 /report를 조회하므로 저장이 먼저 끝나야 한다.
    이 경로를 거치지 못한 종료(클라이언트 연결 종료 등)는 finally에서 stream_closed로 저장한다.
    저장 실패는 로그만 남기고 흐름을 멈추지 않는다.
    """

    # 1. AgentRuntimeState 데이터 초기화
    try:
        state = create_initial_state(target_tps=target_tps, duration=duration)
    except ValueError as e:
        yield _sse("failed", None, f"[입력 에러] {str(e)}")
        return

    task_id = task_manager.register_task(state)

    recorder = RunRecorder(
        task_id=task_id,
        target_tps=target_tps,
        duration=duration,
        force_scaling_requested=force_scaling,
        debug_endpoints_enabled=DEBUG_ENDPOINTS_ENABLED,
    )
    run_records[task_id] = recorder

    # 재검증 LLM 응답 원문을 결과 파일에 남기기 위해 호출 결과만 가로챈다.
    # 호출 자체는 engine 기본값(call_solar_api)과 같다.
    async def _capture_revalidation(system_prompt: str, user_prompt: str) -> str:
        content = await call_solar_api(system_prompt, user_prompt)
        recorder.capture_revalidation(content)
        return content

    try:
        # =================================================================
        # [1단계: 인프라 헬스체크]
        # =================================================================
        yield _sse("running", task_id, "🔍 인프라 연결 확인 중...")
        await asyncio.sleep(0.3)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(f"{os.getenv('PROMETHEUS_URL', 'http://localhost:9090')}/", timeout=3.0)
                if response.status_code not in [200, 302]:
                    message = f"❌ 프로메테우스 인프라 응답 비정상 (Status: {response.status_code})"
                    recorder.finalize(state, "failed", message)
                    yield _sse("failed", task_id, message)
                    return
            except (httpx.ConnectError, httpx.TimeoutException):
                message = "❌ 도커 인프라가 꺼져 있거나 응답이 없습니다! Docker Desktop을 확인해 주세요."
                recorder.finalize(state, "failed", message)
                yield _sse("failed", task_id, message)
                return

        yield _sse("running", task_id, "✅ 인프라 연결 확인")

        # 측정 조건: 진단 시작 전 실제 replica 수 (결과 파일 start_replicas = 최초 측정의 replicas)
        recorder.set_start_replicas(await _read_replicas())
        await asyncio.sleep(0.3)

        # =================================================================
        # [2~4단계: 부하 테스트 + 메트릭 수집 + AI 병목 진단]
        # engine.run_diagnosis가 세 단계를 순차적으로 실행하고,
        # 실패해도 예외를 던지지 않고 agent_outcome="failed" 상태로 안전하게 반환한다.
        # =================================================================
        yield _sse("analyzing", task_id, f"⚡ 부하 테스트 진행 중... ({duration}초)")

        state = await run_diagnosis(state)
        recorder.observe(state)

        if state["agent_outcome"] == "failed":
            message = f"❌ {state.get('error') or '진단에 실패했습니다.'}"
            recorder.finalize(state, "failed", message)
            yield _sse("failed", task_id, message)
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
        # main.js는 URL에 ?debug=1이 있을 때만 force_scaling=true를 보낸다 (Phase 4, #75).
        # DEBUG_ENDPOINTS_ENABLED가 꺼져 있으면 요청이 와도 조용히 무시하고 서버 콘솔에만 남긴다.
        if force_scaling and not DEBUG_ENDPOINTS_ENABLED:
            print(f"[force_scaling] 무시됨 (DEBUG_ENDPOINTS_ENABLED=false) task_id={task_id}")

        if force_scaling and DEBUG_ENDPOINTS_ENABLED and state["agent_outcome"] == "diagnosed":
            # get_current_replicas는 모듈 상단 import를 쓴다. 여기서 함께 import하면
            # 함수 전체에서 지역 변수가 되어 위의 _read_replicas 흐름과 테스트 mock이 어긋난다.
            from app.tools.scale_service import SERVICE_NAME

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

            # LLM 판단(diagnosed)을 실제로 덮어썼다 → 결과 파일 forced_scaling=true.
            # bottleneck_report는 LLM 원본(requires_scaling=false) 그대로 남는다. (docs/02 ISSUE-10)
            recorder.mark_forced()

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
                message = f"✅ {summary}"
                end_reason = (
                    "no_further_scaling" if recorder.scaling_performed else "no_scaling_proposed"
                )
                recorder.finalize(state, end_reason, message)
                yield _sse("done", task_id, message)
                return

            if outcome == "failed":
                message = f"❌ {state.get('error') or '진단에 실패했습니다.'}"
                recorder.finalize(state, "failed", message)
                yield _sse("failed", task_id, message)
                return

            if outcome != "awaiting_approval":
                # 예상 못한 상태값에 대한 방어 (engine/nodes 계약이 바뀌었을 가능성)
                message = f"❌ 알 수 없는 진단 상태입니다: {outcome}"
                recorder.finalize(state, "failed", message)
                yield _sse("failed", task_id, message)
                return

            # ---- 승인 요청 ----
            # 신뢰도 기준 미만 스케일링 제안이면 확인 게이트를 건다 (#83). 기준 이상이면 기존 흐름과 같다
            low_confidence = _is_low_confidence(state)
            task_manager.create_approval_gate(task_id, low_confidence=low_confidence)
            recorder.open_approval(state, low_confidence=low_confidence)

            approval_message = state.get("final_answer") or "병목 감지 — 서버 증설이 필요합니다. 승인해주세요."
            diagnosis_report = state.get("bottleneck_report")
            yield _sse(
                "need_approval",
                task_id,
                f"⚠️ {approval_message}",
                extra={
                    "low_confidence": low_confidence,
                    "confidence": diagnosis_report.confidence if diagnosis_report is not None else None,
                    "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
                },
            )

            # 🔒 유저가 위의 approve_task API를 호출해 줄 때까지 락 걸고 대기 (자원 소모 없음)
            approved = await task_manager.wait_for_approval(task_id)
            recorder.close_approval(
                approved,
                acknowledged=(
                    task_manager.acknowledgements.get(task_id, False)
                    if low_confidence
                    else None
                ),
            )

            # resume_after_approval()이 재검증하면서 load_test_result를 "이후" 값으로
            # 덮어쓰기 전에, "이전" 값을 스냅샷해둔다. (다회차 루프에서도 최초 1회만 저장)
            if task_id not in before_measurements and state.get("load_test_result") is not None:
                before_measurements[task_id] = state["load_test_result"]

            state = await resume_after_approval(
                state,
                approved,
                revalidation_caller=_capture_revalidation,
            )
            recorder.observe(state)

            if not approved:
                message = state.get("final_answer") or "서버 증설이 거부되었습니다."
                message = f"❌ {message}"
                recorder.finalize(state, "rejected", message)
                yield _sse("failed", task_id, message)
                return

            if state["agent_outcome"] == "failed":
                message = f"❌ {state.get('error') or '스케일링에 실패했습니다.'}"
                recorder.finalize(state, "failed", message)
                yield _sse("failed", task_id, message)
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
                message = f"✅ {summary}"
                recorder.finalize(state, "scaled", message)
                yield _sse("done", task_id, message)
                return

            # agent_outcome이 다시 "awaiting_approval"(또는 "diagnosed")이면
            # while 루프 맨 위로 돌아가 그 상태에 맞게 처리한다.
            continue

    except Exception as exc:
        # 예상 못한 예외도 실행 기록으로 남긴 뒤 원래대로 전파한다.
        recorder.finalize(state, "failed", f"스트림 처리 중 예외: {exc}")
        raise

    finally:
        # 정상 종료 경로는 이미 저장했다(finalize는 멱등). 종료 경로를 거치지 못한 실행
        # (승인 대기·진단 중 클라이언트 연결 종료로 스트림이 닫힌 경우 등)도 결과 파일로 남긴다.
        recorder.finalize(state, "stream_closed")

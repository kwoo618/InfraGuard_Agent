"""
InfraGuard Agent 전체 파이프라인 E2E 테스트.

이 테스트는 실제 로컬 인프라(Docker Compose: target-server, Prometheus)가
떠 있어야 통과합니다. 먼저 `docker compose up -d`로 인프라를 띄워주세요.

LLM(Solar API) 호출만 결정론적인 테스트 대역으로 교체합니다. 실제 LLM 판단에
맡기면(1) UPSTAGE_API_KEY/네트워크가 없는 환경에서 테스트가 아예 안 되고,
(2) 가벼운 부하로는 LLM이 "스케일링 불필요"로 판단할 확률이 높아 테스트가
awaiting_approval 단계에 도달하지 못해 타임아웃날 수 있습니다. 그래서 LLM
호출부만 "스케일링이 필요하다"는 고정 응답으로 바꿔치기하고, 부하 테스트
(Locust), 메트릭 수집(Prometheus), 최적화 플랜 생성(generate_plan), 실제
스케일링(Docker Compose)은 전부 진짜로 수행시켜 진짜 배선을 검증합니다.
"""

import os
import sys
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

# 백엔드 경로 주입
backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from app.main import app
from app.agent import nodes as agent_nodes
from app.agent import engine as agent_engine
from app.tools.scale_service import SERVICE_NAME, get_current_replicas


# =================================================================
# LLM 호출 대역 (테스트 전용 결정론적 응답)
# =================================================================

async def _fake_bottleneck_llm_call(system_prompt: str, user_prompt: str) -> str:
    """
    1차 병목 진단 LLM 호출 대역. 항상 "스케일링이 필요하다"는 응답을 돌려준다.

    scaling_plan.current_replicas는 nodes.py의 _validate_scaling_plan이
    실제 현재 replica 수와 일치하는지 검증하므로, 실제 값을 조회해서 채운다.
    """
    current_replicas = max(get_current_replicas(), 1)

    payload = {
        "bottleneck_report": {
            "cause": "[E2E TEST] 테스트를 위해 강제로 병목이 있다고 가정합니다.",
            "severity": "high",
            "recommendation": "[E2E TEST] 테스트용 스케일아웃을 진행합니다.",
            "confidence": 0.99,
            "requires_scaling": True,
        },
        "agent_outcome": "awaiting_approval",
        "scaling_plan": {
            "service_name": SERVICE_NAME,
            "current_replicas": current_replicas,
            "desired_replicas": current_replicas + 1,
            "reason": "[E2E TEST] 강제 스케일링 사유",
        },
    }
    return json.dumps(payload, ensure_ascii=False)


async def _fake_revalidation_llm_call(system_prompt: str, user_prompt: str) -> str:
    """
    스케일링 후 재검증 LLM 호출 대역. 항상 "개선됐고 추가 조치 불필요"로 응답해
    한 번의 스케일링 라운드로 시나리오가 종료되게 한다.
    """
    payload = {
        "summary": "[E2E TEST] 재검증 결과 성능이 개선된 것으로 간주합니다.",
        "performance_improved": True,
        "tps_change": 10.0,
        "latency_avg_change": -50.0,
        "latency_p95_change": -200.0,
        "error_rate_change": 0.0,
        "cpu_pct_change": 0.0,
        "mem_pct_change": 0.0,
        "connection_count_change": 0,
        "additional_action_required": False,
        "recommended_action": None,
    }
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture(autouse=True)
def mock_llm_calls(monkeypatch):
    """
    LLM 호출(Solar API)만 테스트 대역으로 교체하고, 나머지(Locust 부하테스트,
    Prometheus 메트릭 수집, generate_plan 최적화 플랜 생성, Docker 스케일링)는
    전부 실제로 동작시킨다.

    - llm_reasoning_node(state, llm_caller=call_solar_api)의 기본값은 함수
      "정의 시점"에 이미 바인딩돼 있어서, nodes.call_solar_api를 단순히
      patch하는 것만으로는 이미 만들어진 함수의 기본값이 안 바뀐다.
      함수 객체의 __defaults__를 직접 교체해서 확실하게 대역으로 붙인다.
    - resume_after_approval 내부의 revalidation_caller는 함수 "호출 시점"에
      engine 모듈 네임스페이스에서 call_solar_api를 조회하므로, 일반적인
      monkeypatch.setattr(agent_engine, "call_solar_api", ...)로 충분하다.
    """
    monkeypatch.setattr(
        agent_nodes.llm_reasoning_node,
        "__defaults__",
        (_fake_bottleneck_llm_call,),
    )
    monkeypatch.setattr(agent_engine, "call_solar_api", _fake_revalidation_llm_call)
    yield


def _consume_stream(client: TestClient, url: str, shared_info: dict) -> None:
    """
    SSE 스트림을 백그라운드 스레드에서 끝까지 소비하며 task_id를 확보한다.
    """
    try:
        with client.stream("GET", url) as response:
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                try:
                    data_json = json.loads(line.replace("data: ", "", 1))
                except json.JSONDecodeError:
                    continue
                if data_json.get("task_id") and not shared_info.get("task_id"):
                    shared_info["task_id"] = data_json["task_id"]
    except Exception as e:
        shared_info["error"] = str(e)


def test_agent_real_server_e2e_scenario():
    """
    1~6단계(인프라 헬스체크 → 부하테스트 → 메트릭수집 → AI진단 →
    최적화플랜 → 승인 → 스케일링 → 재검증) 전체 시나리오를 검증한다.

    In-process TestClient를 사용해 실제 서버 프로세스를 따로 띄우지 않고도
    task_manager 싱글턴을 공유한 상태로 승인 API를 호출할 수 있게 한다.
    (subprocess로 별도 uvicorn을 띄우면 이 테스트 프로세스에서 건 monkeypatch가
    그 프로세스엔 전혀 적용되지 않아 LLM 대역 교체가 불가능하다.)
    """
    client = TestClient(app)

    target_tps = 10
    duration = 3

    shared_info: dict = {"task_id": None, "error": None}
    start_url = f"/api/v1/agent/start?target_tps={target_tps}&duration={duration}"

    print(f"\n🚀 [E2E TEST] 진단을 요청합니다. target_tps={target_tps}, duration={duration}")

    # STEP 1: SSE 스트림을 별도 스레드에서 소비 시작 (메인 스레드 블로킹 방지)
    stream_thread = threading.Thread(
        target=_consume_stream,
        args=(client, start_url, shared_info),
        daemon=True,
    )
    stream_thread.start()

    # task_id가 발급될 때까지 최대 10초 대기
    for _ in range(10):
        if shared_info["task_id"]:
            break
        time.sleep(1)

    task_id = shared_info["task_id"]
    assert task_id is not None, f"task_id 발급 실패. 에러: {shared_info.get('error')}"
    print(f"🎯 [E2E TEST] Task ID 확보 완료: {task_id}")

    # STEP 2: awaiting_approval 상태 도달 대기 (부하테스트+메트릭+LLM진단+플랜생성 완료까지)
    status_ok = False
    print("⏳ [E2E TEST] 진단 완료 및 승인 대기(awaiting_approval) 상태를 기다립니다...")

    for _ in range(30):
        time.sleep(1)
        report_res = client.get(f"/api/v1/agent/report/{task_id}")
        if report_res.status_code == 200:
            state_data = report_res.json()
            outcome = state_data.get("outcome")
            if outcome == "awaiting_approval":
                status_ok = True
                break
            if outcome == "failed":
                pytest.fail(f"진단이 실패로 종료됐습니다: {state_data.get('error')}")

    assert status_ok, "제한 시간 내에 승인 대기(awaiting_approval) 상태에 도달하지 못했습니다."

    # 이 시점에 optimization_plan(최적화 조치 목록)도 함께 생성돼 있어야 한다.
    report_before_approval = client.get(f"/api/v1/agent/report/{task_id}").json()
    assert report_before_approval.get("optimization_plan"), (
        "awaiting_approval 상태인데 optimization_plan이 비어 있습니다 "
        "(generate_plan_node 연동을 확인하세요)."
    )
    print(f"📋 [E2E TEST] 최적화 조치 {len(report_before_approval['optimization_plan'])}건 확인")

    # STEP 3: 승인 API 호출
    approve_res = client.post(
        "/api/v1/agent/approve",
        json={"task_id": task_id, "approved": True},
    )
    assert approve_res.status_code == 200
    assert approve_res.json()["status"] == "success"
    print("✅ [E2E TEST] 승인 API 전송 성공. 스케일링 + 재검증 단계로 진입합니다.")

    # STEP 4: 최종 조치 완료(scaled) 상태 도달 대기
    final_ok = False
    for _ in range(20):
        time.sleep(1)
        final_report_res = client.get(f"/api/v1/agent/report/{task_id}")
        if final_report_res.status_code == 200:
            final_state = final_report_res.json()
            outcome = final_state.get("outcome")
            if outcome == "scaled":
                final_ok = True
                break
            if outcome == "failed":
                pytest.fail(f"스케일링/재검증이 실패로 종료됐습니다: {final_state.get('error')}")

    assert final_ok, "6단계 인프라 증설 완료(scaled) 상태가 확인되지 않았습니다."

    # 실제로 replica 수가 desired_replicas만큼 늘었는지도 확인
    final_state = client.get(f"/api/v1/agent/report/{task_id}").json()
    action = final_state.get("action")
    assert action is not None and action["success"] is True, "스케일링 조치 결과가 비어있거나 실패했습니다."
    assert action["after_replicas"] > action["before_replicas"], (
        f"replica 수가 실제로 늘지 않았습니다: {action['before_replicas']} → {action['after_replicas']}"
    )

    print(
        f"\n🎉 [E2E TEST SUCCESS] 전체 시나리오 검증 통과 "
        f"(replica {action['before_replicas']} → {action['after_replicas']})"
    )
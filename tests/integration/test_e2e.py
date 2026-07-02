import os
import sys
import time
import subprocess
import threading
import requests
import pytest

# 백엔드 경로 주입
backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


@pytest.fixture(scope="module", autouse=True)
def manage_local_server():
    """
    현재 가상환경의 Python 인터프리터를 사용하여 백그라운드에서 uvicorn을 구동합니다.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=backend_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    time.sleep(3)
    if proc.poll() is not None:
        stderr_output = proc.stderr.read() if proc.stderr else "알 수 없는 오류"
        raise RuntimeError(f"자동 uvicorn 서버 구동 실패. 원인:\n{stderr_output}")
    
    yield
    
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def consume_stream(url, shared_info):
    """
    백엔드가 멈추지 않도록 스트림 데이터를 끝까지 안정적으로 소비하는 스레드 함수
    """
    try:
        with requests.get(url, stream=True, timeout=10) as response:
            for line in response.iter_lines():
                if line:
                    line_str = line.decode("utf-8")
                    if line_str.startswith("data:"):
                        import json
                        try:
                            data_json = json.loads(line_str.replace("data: ", ""))
                            if data_json.get("task_id") and not shared_info.get("task_id"):
                                shared_info["task_id"] = data_json["task_id"]
                        except Exception:
                            pass
    except Exception as e:
        shared_info["error"] = str(e)


@pytest.mark.anyio
def test_agent_real_server_e2e_scenario():
    """
    스트림 락(Lock) 현상을 방지하여 1~6단계 전체 시나리오를 완주하는 E2E 테스트 (로그 간소화 버전)
    """
    TARGET_SERVER_URL = "http://127.0.0.1:8000"
    target_tps = 10
    duration = 3
    
    shared_info = {"task_id": None, "error": None}
    start_url = f"{TARGET_SERVER_URL}/api/v1/agent/start?target_tps={target_tps}&duration={duration}"

    print(f"\n🚀 [E2E TEST] 구동된 {TARGET_SERVER_URL} 서버에 진단을 요청합니다. (스트림 격리)")

    # STEP 1: 백엔드 스트림 요청을 별도 스레드에서 시작 (서버 블로킹 방지)
    stream_thread = threading.Thread(target=consume_stream, args=(start_url, shared_info), daemon=True)
    stream_thread.start()

    # Task ID 가 발급될 때까지 최대 5초 대기
    for _ in range(5):
        if shared_info["task_id"]:
            break
        time.sleep(1)

    task_id = shared_info["task_id"]
    assert task_id is not None, f"task_id 발급 실패. 에러: {shared_info.get('error')}"
    print(f"🎯 [E2E TEST] Task ID 확보 완료: {task_id}")

    # STEP 2 & 3: 5단계 가드레일 대기방(awaiting_approval) 상태 확인 (Polling)
    status_ok = False
    print("⏳ 백엔드 에이전트 연산 진행 중... (5단계 승인 대기방 진입을 기다립니다)")
    
    for _ in range(25):
        time.sleep(1)
        report_res = requests.get(f"{TARGET_SERVER_URL}/api/v1/agent/report/{task_id}")
        if report_res.status_code == 200:
            state_data = report_res.json()
            if state_data.get("outcome") == "awaiting_approval":
                print(f"\n📢 [E2E TEST] 5단계 승인 대기(awaiting_approval) 상태 포착!")
                status_ok = True
                break
                
    assert status_ok is True, "제한 시간 내에 5단계(awaiting_approval)에 도달하지 못했습니다."

    # STEP 4: 유저 승인 API 호출 (가드레일 자물쇠 해제)
    approval_payload = {"task_id": task_id, "approved": True}
    approve_res = requests.post(f"{TARGET_SERVER_URL}/api/v1/agent/approve", json=approval_payload)
    assert approve_res.status_code == 200
    print("✅ [E2E TEST] 외부 유저 승인 API 전송 성공. 최종 조치 단계로 진입합니다.")

    # STEP 5: 6단계 최종 조치 완료(scaled) 확인
    final_ok = False
    for _ in range(15):
        time.sleep(1)
        final_report_res = requests.get(f"{TARGET_SERVER_URL}/api/v1/agent/report/{task_id}")
        if final_report_res.status_code == 200:
            final_state = final_report_res.json()
            if final_state.get("outcome") == "scaled":
                final_ok = True
                break
                
    assert final_ok is True, "6단계 인프라 증설 완료(scaled) 상태가 확인되지 않았습니다."
    print("\n🎉 [E2E TEST SUCCESS] 전체 E2E 시나리오 검증 완벽 통과!")
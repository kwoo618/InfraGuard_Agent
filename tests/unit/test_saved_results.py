"""
저장된 결과 불러오기 API 테스트 (#93, backend/app/api/v1/saved_results.py).

- 목록: 이름 규칙에 맞는 evidence 디렉터리 바로 아래 파일만, 파일명 순. 읽지 못한 파일도 error와 함께 넣는다.
- 조회: /report 모양(report)과 파일 정보(saved). evidence 밖 경로·규칙에 맞지 않는 이름은 읽지 않는다.
- 실제 docs/evidence 파일이 모두 이름 규칙에 맞고 변환된다.

결과 파일과 /report의 값 대응은 test_run_history._assert_report_matches_file에서 확인한다(모든 스트림 경로).
임시 파일은 tmp_path에만 쓴다. 아래 수치는 테스트 입력값이며 측정값이 아니다.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import run_history, saved_results
from app.main import app

client = TestClient(app)

FILE_A = "20260912_005124_c460660d-48bc-46b5-95a1-90d551a79e76.json"
FILE_B = "20260912_005741_7e3ee5cf-8bd8-4aa9-a075-69f2b4da32e0.json"

REAL_EVIDENCE_DIR = saved_results.EVIDENCE_DIR


def _record(task_id: str, end_reason: str = "scaled") -> dict:
    """RunRecorder.build_record와 같은 모양의 최소 결과 파일."""

    return {
        "schema_version": 1,
        "task_id": task_id,
        "started_at": "2026-09-12T00:57:41+09:00",
        "ended_at": "2026-09-12T00:59:55+09:00",
        "conditions": {"virtual_users": 50, "duration_sec": 30, "p95_slo_ms": 1000},
        "forced_scaling": False,
        "measurement_history": [
            {"round": 1, "replicas": 1, "tps": 40.0, "latency_p95": 3000.0, "latency_avg": 700.0,
             "error_rate": 0.005, "total_requests": 1200, "duration": 30},
            {"round": 2, "replicas": 2, "tps": 90.0, "latency_p95": 900.0, "latency_avg": 200.0,
             "error_rate": 0.004, "total_requests": 2700, "duration": 30},
        ],
        "diagnoses": [
            {"round": 1, "requires_scaling": True, "severity": "high", "confidence": 0.8,
             "cause": "테스트 원인", "recommendation": "테스트 권장 조치"},
        ],
        "approvals": [
            {"round": 1, "source": "llm", "scaling_plan": {"current_replicas": 1, "desired_replicas": 2},
             "low_confidence": False, "acknowledged": None, "decision": "approved"},
        ],
        "scaling_results": [
            {"round": 1, "before_replicas": 1, "after_replicas": 2, "success": True, "error_message": None},
        ],
        "revalidations": [],
        "user_decision": "approved",
        "outcome": {
            "agent_outcome": "scaled",
            "end_reason": end_reason,
            "final_message": "완료",
            "final_answer": "재검증 완료",
            "error": None,
            "loop_count": 1,
            "scaling_count": 1,
            "optimization_plan": ["조치 1"],
        },
    }


def _write(directory: Path, name: str, content) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def evidence_dir(tmp_path, monkeypatch):
    directory = tmp_path / "evidence"
    monkeypatch.setattr(saved_results, "EVIDENCE_DIR", directory)
    return directory


# ----------------------------------------------------------------------
# 목록
# ----------------------------------------------------------------------


def test_list_returns_only_saved_files_in_name_order(evidence_dir):
    _write(evidence_dir, FILE_B, _record("7e3ee5cf-8bd8-4aa9-a075-69f2b4da32e0"))
    _write(evidence_dir, FILE_A, _record("c460660d-48bc-46b5-95a1-90d551a79e76", "no_scaling_proposed"))
    _write(evidence_dir, f"{FILE_A}.tmp", "{}")
    _write(evidence_dir, "notes.json", "{}")
    _write(evidence_dir, FILE_A.replace(".json", ".JSON"), "{}")
    (evidence_dir / "20260912_010000_00000000-0000-0000-0000-000000000000.json").mkdir()

    response = client.get("/api/v1/results/saved")

    assert response.status_code == 200
    files = response.json()["files"]
    assert [item["file_name"] for item in files] == [FILE_A, FILE_B]

    second = files[1]
    assert second["virtual_users"] == 50
    assert second["duration_sec"] == 30
    assert second["end_reason"] == "scaled"
    assert second["user_decision"] == "approved"
    assert second["replicas"] == [1, 2]
    assert second["forced_scaling"] is False
    assert second["error"] is None


def test_list_keeps_unreadable_file_with_error(evidence_dir):
    _write(evidence_dir, FILE_A, "{깨진 JSON")

    files = client.get("/api/v1/results/saved").json()["files"]

    assert [item["file_name"] for item in files] == [FILE_A]
    assert files[0]["error"]


def test_list_without_directory_is_empty(evidence_dir):
    response = client.get("/api/v1/results/saved")

    assert response.status_code == 200
    assert response.json()["files"] == []


def test_list_does_not_follow_symlink_outside(evidence_dir, tmp_path):
    outside = _write(tmp_path / "results", FILE_A, _record("x"))
    evidence_dir.mkdir()

    try:
        (evidence_dir / FILE_A).symlink_to(outside)
    except OSError:
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없다 (Windows 권한)")

    assert client.get("/api/v1/results/saved").json()["files"] == []
    assert client.get(f"/api/v1/results/saved/{FILE_A}").status_code == 404


# ----------------------------------------------------------------------
# 조회
# ----------------------------------------------------------------------


def test_get_returns_report_shape_and_file_info(evidence_dir):
    record = _record("7e3ee5cf-8bd8-4aa9-a075-69f2b4da32e0")
    _write(evidence_dir, FILE_B, record)

    response = client.get(f"/api/v1/results/saved/{FILE_B}")

    assert response.status_code == 200
    body = response.json()
    assert body["saved"]["file_name"] == FILE_B
    assert body["saved"]["started_at"] == record["started_at"]
    assert body["saved"]["ended_at"] == record["ended_at"]
    assert body["saved"]["path"].endswith(f"/{FILE_B}")

    report = body["report"]
    assert report["end_reason"] == "scaled"
    assert report["outcome"] == "scaled"
    assert report["measurement_history"] == record["measurement_history"]
    assert report["result_file"].endswith(f"/{FILE_B}")
    # 스케일링 결과가 있으면 첫 측정 / 마지막 측정 (agent.get_report와 같은 규칙)
    assert report["measurement"]["latency_p95"] == 3000.0
    assert report["measurement_after"]["latency_p95"] == 900.0
    assert report["improvement"]["latency_p95_delta"] == -2100.0
    assert report["action"]["after_replicas"] == 2
    assert report["bottleneck"]["requires_scaling"] is True
    assert report["waiting_for_approval"] is None
    assert report["low_confidence_threshold"] == 0.6


def test_get_without_history_has_null_measurements(evidence_dir):
    record = _record("7e3ee5cf-8bd8-4aa9-a075-69f2b4da32e0", "failed")
    record.update(measurement_history=[], diagnoses=[], approvals=[], scaling_results=[])
    _write(evidence_dir, FILE_B, record)

    report = client.get(f"/api/v1/results/saved/{FILE_B}").json()["report"]

    assert report["measurement"] is None
    assert report["measurement_after"] is None
    assert report["bottleneck"] is None
    assert report["action"] is None
    assert report["low_confidence"] is False


@pytest.mark.parametrize(
    "name",
    [
        "notes.json",
        FILE_A.replace(".json", ".JSON"),
        f"{FILE_A}.tmp",
        "..%2Fresults%2F" + FILE_A,
        "..%5Cresults%5C" + FILE_A,
        "C:%5CWindows%5Cwin.ini",
        "%2Fetc%2Fpasswd",
    ],
)
def test_get_rejects_names_outside_rule(evidence_dir, tmp_path, name):
    _write(tmp_path / "results", FILE_A, _record("x"))

    response = client.get(f"/api/v1/results/saved/{name}")

    assert response.status_code in (400, 404)


def test_get_does_not_read_same_name_outside_directory(evidence_dir, tmp_path):
    # 이름은 규칙에 맞지만 evidence가 아니라 results/에만 있는 파일
    _write(tmp_path / "results", FILE_A, _record("x"))
    evidence_dir.mkdir()

    assert client.get(f"/api/v1/results/saved/{FILE_A}").status_code == 404


def test_get_unreadable_file_is_422(evidence_dir):
    _write(evidence_dir, FILE_A, "[1, 2]")

    assert client.get(f"/api/v1/results/saved/{FILE_A}").status_code == 422


# ----------------------------------------------------------------------
# 실제 증빙 파일 (docs/evidence, 읽기만 한다)
# ----------------------------------------------------------------------


def test_real_evidence_files_follow_name_rule_and_convert():
    assert Path(REAL_EVIDENCE_DIR) == run_history.PROJECT_ROOT / "docs" / "evidence"

    paths = sorted(Path(REAL_EVIDENCE_DIR).glob("*.json"))
    assert paths, "docs/evidence에 결과 파일이 없다"

    listed = client.get("/api/v1/results/saved").json()
    assert listed["directory"] == "docs/evidence"
    assert [item["file_name"] for item in listed["files"]] == [path.name for path in paths]
    assert all(item["error"] is None for item in listed["files"])

    for path in paths:
        record = json.loads(path.read_text(encoding="utf-8"))
        body = client.get(f"/api/v1/results/saved/{path.name}").json()
        report = body["report"]

        assert report["result_file"] == f"docs/evidence/{path.name}"
        assert report["measurement_history"] == record["measurement_history"]
        assert report["end_reason"] == record["outcome"]["end_reason"]
        assert report["diagnoses"] == record["diagnoses"]
        assert body["saved"]["started_at"] == record["started_at"]

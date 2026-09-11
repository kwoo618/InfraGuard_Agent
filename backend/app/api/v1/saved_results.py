"""
saved_results — 저장된 실행 결과 파일(docs/evidence/*.json)을 결과 패널에 다시 그리기 위한 읽기 전용 API.

담당: 최강우 (디자인 개편, #93)
소비자: static/saved_results.js — 드롭다운 목록을 받고, 고른 파일을 /report 모양으로 받아 결과 패널에 그린다.

원칙:
- 읽기 전용이다. 파일을 만들거나 고치지 않고, 에이전트 실행 상태와도 무관하다.
- docs/evidence/ 바로 아래 파일만 읽는다. 파일명은 결과 파일 이름 규칙(run_history.save_run_record)과 정확히 맞아야 하고,
  경로를 풀었을 때도 evidence 디렉터리 바로 아래의 일반 파일이어야 한다.
- 파일에 없는 값은 추정하지 않는다. /report와 모양을 맞추는 변환은 saved_record_to_report 한 곳에서만 한다.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.v1 import run_history
from app.api.v1.agent import LOW_CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)

router = APIRouter()

# 발표 증빙 결과 파일 위치 (docs/04). 조회할 때마다 읽는다. 테스트는 monkeypatch로 tmp_path를 넣는다.
EVIDENCE_DIR = run_history.PROJECT_ROOT / "docs" / "evidence"

# run_history.save_run_record 이름 규칙: {YYYYMMDD_HHMMSS}_{task_id(uuid4)}.json
SAVED_FILE_NAME = re.compile(
    r"\d{8}_\d{6}_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json"
)

# /report measurement 항목의 키 (agent.get_report._serialize_measurement와 같다)
_MEASUREMENT_KEYS = (
    "tps",
    "error_rate",
    "latency_p95",
    "latency_avg",
    "total_requests",
    "duration",
)


def _evidence_dir() -> Path:
    return Path(EVIDENCE_DIR).resolve()


def _display_dir() -> str:
    """화면·result_file에 쓰는 디렉터리 이름 (repo 루트 기준 상대경로)."""

    try:
        return _evidence_dir().relative_to(run_history.PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(_evidence_dir())


def _is_saved_file(path: Path) -> bool:
    """evidence 디렉터리 바로 아래, 이름 규칙에 맞는 일반 파일인지 (심볼릭 링크로 밖을 가리키는 파일 제외)."""

    return (
        SAVED_FILE_NAME.fullmatch(path.name) is not None
        and path.resolve().parent == _evidence_dir()
        and path.is_file()
    )


def _read_record(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError("결과 파일 최상위가 객체가 아닙니다.")

    return data


def _list_of(value: Any) -> list:
    return list(value) if isinstance(value, list) else []


def _measurement(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None

    return {key: record.get(key) for key in _MEASUREMENT_KEYS}


def _improvement(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | None:
    """agent.get_report의 improvement와 같은 계산. 값이 하나라도 숫자가 아니면 null."""

    try:
        return {
            "tps_delta": after["tps"] - before["tps"],
            "latency_p95_delta": after["latency_p95"] - before["latency_p95"],
            "error_rate_delta": after["error_rate"] - before["error_rate"],
        }
    except TypeError:
        return None


def saved_record_to_report(record: dict[str, Any], file_name: str) -> dict[str, Any]:
    """
    결과 파일(run_history.RunRecorder.build_record)을 /report 응답과 같은 모양으로 바꾼다.

    결과 패널용 필드는 결과 파일에 같은 값이 그대로 있다(test_run_history 대응성 테스트).
    /report의 레거시 필드는 agent.get_report와 같은 규칙으로 파일에서 복원한다.
    - measurement / measurement_after: before_measurements는 최초 측정을 한 번만 스냅샷하므로,
      스케일링 결과가 있으면 첫 측정 / 마지막 측정, 없으면 마지막 측정 하나다.
    - bottleneck: 마지막 진단, action: 마지막 스케일링 결과.
    - waiting_for_approval은 결과 파일에 없어 null이다.
    - low_confidence_threshold는 파일에 없는 코드 상수(가드레일, agent.LOW_CONFIDENCE_THRESHOLD)다. 측정값이 아니다.
    """

    history = _list_of(record.get("measurement_history"))
    diagnoses = _list_of(record.get("diagnoses"))
    approvals = _list_of(record.get("approvals"))
    scaling_results = _list_of(record.get("scaling_results"))
    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}

    measurement = None
    measurement_after = None
    improvement = None

    if scaling_results and history:
        measurement = _measurement(history[0])
        measurement_after = _measurement(history[-1])

        if measurement is not None and measurement_after is not None:
            improvement = _improvement(measurement, measurement_after)
    elif history:
        measurement = _measurement(history[-1])

    bottleneck = None
    last_diagnosis = diagnoses[-1] if diagnoses and isinstance(diagnoses[-1], dict) else None

    if last_diagnosis is not None:
        bottleneck = {
            key: last_diagnosis.get(key)
            for key in ("cause", "severity", "recommendation", "confidence", "requires_scaling")
        }

    action = None
    last_scaling = scaling_results[-1] if scaling_results and isinstance(scaling_results[-1], dict) else None

    if last_scaling is not None:
        action = {
            key: last_scaling.get(key)
            for key in ("before_replicas", "after_replicas", "success", "error_message")
        }

    last_approval = approvals[-1] if approvals and isinstance(approvals[-1], dict) else {}

    return {
        "task_id": record.get("task_id"),
        "outcome": outcome.get("agent_outcome"),
        "waiting_for_approval": None,
        "loop_count": outcome.get("loop_count"),
        "measurement": measurement,
        "measurement_after": measurement_after,
        "improvement": improvement,
        "bottleneck": bottleneck,
        "action": action,
        "optimization_plan": _list_of(outcome.get("optimization_plan")),
        "summary": outcome.get("final_answer"),
        "error": outcome.get("error"),
        "measurement_history": history,
        "forced_scaling": bool(record.get("forced_scaling")),
        "result_file": f"{_display_dir()}/{file_name}",
        "conditions": record.get("conditions"),
        "end_reason": outcome.get("end_reason"),
        "user_decision": record.get("user_decision"),
        "diagnoses": diagnoses,
        "approvals": approvals,
        "scaling_results": scaling_results,
        "revalidations": _list_of(record.get("revalidations")),
        "low_confidence": bool(last_approval.get("low_confidence")),
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
    }


def _file_summary(record: dict[str, Any], file_name: str) -> dict[str, Any]:
    """드롭다운 한 줄에 쓰는 값 (파일 값 그대로)."""

    history = _list_of(record.get("measurement_history"))
    conditions = record.get("conditions") if isinstance(record.get("conditions"), dict) else {}
    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}

    return {
        "file_name": file_name,
        "started_at": record.get("started_at"),
        "virtual_users": conditions.get("virtual_users"),
        "duration_sec": conditions.get("duration_sec"),
        "end_reason": outcome.get("end_reason"),
        "user_decision": record.get("user_decision"),
        "replicas": [item.get("replicas") for item in history if isinstance(item, dict)],
        "forced_scaling": bool(record.get("forced_scaling")),
        "error": None,
    }


@router.get("/results/saved")
async def list_saved_results():
    """
    docs/evidence/의 결과 파일 목록 (파일명 순 = 실행 시작 시각 순).

    읽지 못한 파일도 빼지 않고 error와 함께 넣는다 (골라서 보여주지 않는다).
    """

    directory = _evidence_dir()
    files: list[dict[str, Any]] = []

    if directory.is_dir():
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if not _is_saved_file(path):
                continue

            try:
                files.append(_file_summary(_read_record(path), path.name))
            except Exception:
                logger.warning("결과 파일을 읽지 못했습니다: %s", path, exc_info=True)
                files.append({"file_name": path.name, "error": "결과 파일을 읽을 수 없습니다."})

    return {"directory": _display_dir(), "files": files}


@router.get("/results/saved/{file_name}")
async def get_saved_result(file_name: str):
    """
    결과 파일 하나를 /report 모양(report)과 파일 정보(saved)로 반환한다.

    이름 규칙에 맞지 않으면 400, evidence 디렉터리 바로 아래 파일이 아니면 404, 읽지 못하면 422.
    """

    if SAVED_FILE_NAME.fullmatch(file_name) is None:
        raise HTTPException(status_code=400, detail="결과 파일 이름 형식이 아닙니다.")

    path = _evidence_dir() / file_name

    if not _is_saved_file(path):
        raise HTTPException(status_code=404, detail="저장된 결과 파일을 찾을 수 없습니다.")

    try:
        record = _read_record(path)
    except Exception:
        logger.warning("결과 파일을 읽지 못했습니다: %s", path, exc_info=True)
        raise HTTPException(status_code=422, detail="결과 파일을 읽을 수 없습니다.")

    return {
        "saved": {
            "file_name": file_name,
            "path": f"{_display_dir()}/{file_name}",
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
        },
        "report": saved_record_to_report(record, file_name),
    }

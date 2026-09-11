"""
run_history — 에이전트 실행 1회의 라운드별 측정 이력과 결과 파일(results/*.json)을 만든다.

담당: 최강우 (Phase 3, #75)
소비자: api/v1/agent.py — start_agent_stream이 engine 호출이 끝날 때마다 observe(state)를 부르고,
        실행이 끝나는 모든 경로에서 finalize()로 results/{YYYYMMDD_HHMMSS}_{task_id}.json을 남긴다.
        /report의 measurement_history도 이 모듈이 만든다.

원칙 (docs/03_대시보드명세.md Phase 3, docs/02_알려진이슈.md ISSUE-5·10·11):
- 모든 실행을 저장한다. 스케일링 성공뿐 아니라 미제안·거절·실패·forced·스트림 종료도 남긴다.
  성공 케이스만 남기면 결과를 골라낸 것이 된다.
- 실측값만 저장한다. 수집하지 않는 cpu_pct/mem_pct(get_metrics.RESOURCE_METRICS_COLLECTED=False)는
  0.0이 아니라 "측정 불가"로 저장한다. 재검증 LLM이 계산한 변화량(tps_change 등)은 측정값이 아니라서
  저장하지 않는다. 변화량은 measurement_history에서 계산한다.
- target_tps는 처리량이 아니라 Locust --users(동시 가상 사용자 수)라서 virtual_users로 저장한다 (ISSUE-11).
- forced_scaling: force_scaling 요청이 DEBUG_ENDPOINTS_ENABLED=true로 LLM 판단을 실제로 덮어썼는지.
  forced 실행의 측정값은 발표 수치로 쓰지 않는다.
- 기록·저장 실패는 로그만 남기고 에이전트 흐름을 멈추지 않는다.

라운드 판별:
engine은 상태 dict를 제자리에서 갱신하고, 새 측정·진단·스케일링 결과는 매번 새 객체로 넣는다.
그래서 직전에 본 객체와 identity(is)를 비교해 새로 생긴 값만 누적한다.
"""

import ast
import copy
import json
import logging
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.engine import _parse_revalidation_response
from app.agent.nodes import UPSTAGE_MODEL
from app.agent.prompts import get_p95_slo_ms
from app.agent.state import AgentRuntimeState
from app.schemas import (
    BottleneckReport,
    LoadTestResult,
    ScalingResult,
    SystemMetrics,
)
from app.tools.get_metrics import RESOURCE_METRICS_COLLECTED
from app.tools.run_load_test import DEFAULT_HOST, LOCUSTFILE

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 수집하지 않는 메트릭 자리에 숫자 대신 저장하는 표기 (ISSUE-10).
# prompts.UNMEASURED_TEXT는 LLM에 주는 지시문이라 파일에는 이 짧은 표기를 쓴다.
UNMEASURED_LABEL = "측정 불가"

# 이 파일(backend/app/api/v1/run_history.py) 기준 parents 인덱스:
#   [0] = v1, [1] = api, [2] = app, [3] = backend, [4] = 프로젝트 루트
# backend 컨테이너는 repo 루트를 /app에 마운트하므로(docker-compose.yml) /app/results가
# 호스트의 results/가 된다.
PROJECT_ROOT = Path(__file__).resolve().parents[4]

# 저장 시점에 읽는다. 테스트는 monkeypatch로 tmp_path를 넣는다.
RESULTS_DIR = PROJECT_ROOT / "results"

# 재검증 응답을 파싱하지 못했을 때 남기는 원문 길이
RAW_TEXT_LIMIT = 300

# 결과 파일 outcome.end_reason 값.
# agent_outcome="diagnosed"만으로는 미제안·거절을 구분할 수 없어서 따로 둔다.
#   no_scaling_proposed : 최초 진단에서 스케일링 미제안
#   no_further_scaling  : 스케일링 후 재진단에서 추가 스케일링 미제안
#   scaled              : 스케일링 후 재검증에서 개선 확인
#   rejected            : 사용자가 스케일링 거절
#   failed              : 인프라 확인·부하 테스트·LLM·스케일링 등 실패
#   stream_closed       : 위 경로를 거치지 못하고 스트림이 끝남 (클라이언트 연결 종료 등)

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _now() -> datetime:
    """offset이 붙은 현재 로컬 시각."""

    return datetime.now().astimezone()


def _run_git(*args: str) -> str | None:
    """git 명령 결과. git이 없거나 실패하면 None."""

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    return result.stdout.strip()


def _read_code_version() -> dict[str, Any]:
    """
    서버가 코드를 읽은 시점(모듈 import)의 git 커밋과 미커밋 변경 여부.

    코드가 바뀌면 서버를 다시 띄워야 반영되므로(--reload 포함) import 시점 값이
    이 서버가 실행 중인 코드와 맞는다. git이 없는 환경(backend 컨테이너 이미지)에서는 null.
    """

    commit = _run_git("rev-parse", "--short", "HEAD")
    status = _run_git("status", "--porcelain") if commit else None

    return {
        "code_version": commit,
        "code_dirty": bool(status) if status is not None else None,
    }


CODE_VERSION = _read_code_version()


def _task_weight(decorator: ast.expr) -> int | None:
    """@task / @task(N) / @task(weight=N) 데코레이터면 가중치, task가 아니면 None."""

    if isinstance(decorator, ast.Name) and decorator.id == "task":
        return 1

    if not (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "task"
    ):
        return None

    weight_node: ast.expr | None = decorator.args[0] if decorator.args else None

    for keyword in decorator.keywords:
        if keyword.arg == "weight":
            weight_node = keyword.value

    if weight_node is None:
        return 1

    if isinstance(weight_node, ast.Constant) and isinstance(weight_node.value, int):
        return weight_node.value

    # 상수가 아닌 가중치는 추정하지 않는다 → 호출부에서 전체를 None 처리
    raise ValueError("task 가중치가 정수 상수가 아닙니다.")


def _request_name(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """task 함수 안 self.client.<method>() 호출의 name= 값, 없으면 경로 인자, 둘 다 없으면 함수 이름."""

    for node in ast.walk(function):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _HTTP_METHODS
        ):
            continue

        for keyword in node.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)

        if node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)

    return function.name


def read_locust_task_weights(
    locustfile: Path = LOCUSTFILE,
) -> dict[str, int] | None:
    """
    locustfile의 @task 가중치를 {요청 이름: 가중치}로 읽는다.

    Locust Aggregated P95는 엔드포인트 구성에 따라 달라지므로 측정 조건으로 남긴다 (ISSUE-10).
    locust를 import하면 gevent monkey patch가 백엔드 프로세스에 적용되므로 import하지 않고 ast로 읽는다.
    Locust는 실행마다 이 파일을 새로 읽으므로 호출하는 쪽도 실행마다 읽는다.
    읽지 못하면 None을 반환한다 (추정값을 넣지 않는다).
    """

    try:
        tree = ast.parse(Path(locustfile).read_text(encoding="utf-8"))
        weights: dict[str, int] = {}

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            for decorator in node.decorator_list:
                weight = _task_weight(decorator)

                if weight is not None:
                    name = _request_name(node)
                    weights[name] = weights.get(name, 0) + weight
                    break

        return weights or None

    except Exception:
        logger.warning(
            "locustfile 가중치를 읽지 못했습니다: %s",
            locustfile,
            exc_info=True,
        )
        return None


def _read_p95_slo_ms() -> int | None:
    """진단 프롬프트와 같은 P95 SLO. 설정이 잘못됐으면(진단도 실패한다) 추정값 대신 None."""

    try:
        return get_p95_slo_ms()
    except ValueError:
        return None


def _replicas_or_none(value: Any) -> int | None:
    """
    replica 조회 결과를 저장값으로 바꾼다.

    scale_service는 Docker 조회에 실패하면 예외 대신 0을 반환한다. 부하 테스트가 도는 중에
    replica 0은 있을 수 없으므로 0 이하는 측정값이 아니라 조회 실패로 보고 None으로 저장한다.
    """

    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value

    return None


def _to_utc_iso(timestamp: str | None) -> str | None:
    """
    SystemMetrics.timestamp를 offset이 붙은 ISO로 바꾼다.

    schemas.py 기본값은 datetime.utcnow().isoformat()(offset 없는 UTC)이다.
    offset이 이미 있으면 그대로 두고, 해석하지 못하면 원문을 남긴다.
    """

    if not timestamp:
        return None

    try:
        parsed = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return str(timestamp)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.isoformat()


def save_run_record(
    record: dict[str, Any],
    started_at: datetime,
    results_dir: Path | None = None,
) -> Path | None:
    """
    실행 기록을 results/{YYYYMMDD_HHMMSS}_{task_id}.json으로 저장한다.

    파일명 시각은 실행 시작 시점의 백엔드 프로세스 로컬 시간이다(컨테이너 실행 시 UTC).
    임시 파일에 쓴 뒤 os.replace로 바꿔 쓰다 만 파일이 남지 않게 한다.
    저장에 실패하면 로그만 남기고 None을 반환한다. 에이전트 흐름을 멈추지 않는다.
    """

    directory = Path(results_dir) if results_dir is not None else RESULTS_DIR
    tmp_file: Path | None = None

    try:
        directory.mkdir(parents=True, exist_ok=True)

        path = directory / (
            f"{started_at:%Y%m%d_%H%M%S}_{record['task_id']}.json"
        )
        tmp_file = path.with_name(f"{path.name}.tmp")

        tmp_file.write_text(
            json.dumps(
                record,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        os.replace(tmp_file, path)

        return path

    except Exception:
        logger.exception(
            "결과 파일 저장 실패 (에이전트 흐름은 계속) task_id=%s",
            record.get("task_id"),
        )

        if tmp_file is not None:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass

        return None


class RunRecorder:
    """
    실행 1회(task_id)의 측정 조건, 라운드별 측정 이력, AI 판단, 사용자 결정,
    스케일링 결과, 재검증 결과를 모은다.

    모든 공개 메서드는 내부 오류를 로그로만 남기고 예외를 던지지 않는다.
    """

    def __init__(
        self,
        *,
        task_id: str,
        target_tps: int,
        duration: int,
        force_scaling_requested: bool,
        debug_endpoints_enabled: bool,
    ) -> None:
        self.task_id = task_id
        self.started_at = _now()

        self.conditions: dict[str, Any] = {
            # = state.target_tps. run_load_test가 Locust --users로 넘긴다. 처리량 목표가 아니다 (ISSUE-11)
            "virtual_users": target_tps,
            "duration_sec": duration,
            "p95_slo_ms": _read_p95_slo_ms(),
            "llm_model": UPSTAGE_MODEL,
            # 진단 시작 전 실제 replica 수. set_start_replicas()로 채운다
            "start_replicas": None,
            "resource_metrics_collected": RESOURCE_METRICS_COLLECTED,
            # 요청 파라미터 (main.js는 ?debug=1일 때만 true). 실제 덮어쓰기 여부는 forced_scaling을 본다
            "force_scaling_requested": force_scaling_requested,
            "debug_endpoints_enabled": debug_endpoints_enabled,
            "load_target": DEFAULT_HOST,
            "locust_task_weights": read_locust_task_weights(),
            # backend 프로세스 실행 환경 (컨테이너 실행 시 컨테이너 기준). PC 사양(RAM 등)은 문서에 기록한다
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
                **CODE_VERSION,
            },
        }

        self.forced_scaling = False
        self.result_file: Path | None = None
        # finalize()가 기록하는 종료 경로 (결과 파일 outcome.end_reason). 실행 중이면 None
        self.end_reason: str | None = None

        self._history: list[dict[str, Any]] = []
        self._diagnoses: list[dict[str, Any]] = []
        self._approvals: list[dict[str, Any]] = []
        self._scaling_results: list[dict[str, Any]] = []
        self._revalidations: list[dict[str, Any]] = []
        self._pending_revalidations: list[str] = []

        # identity 비교용: 직전에 기록한 객체
        self._last_load_test: LoadTestResult | None = None
        self._last_metrics: SystemMetrics | None = None
        self._last_report: BottleneckReport | None = None
        self._last_scaling: ScalingResult | None = None

        # 다음 측정 레코드에 넣을 실제 replica 수
        self._current_replicas: int | None = None

        self._finalized = False

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    @property
    def measurement_history(self) -> list[dict[str, Any]]:
        """/report measurement_history와 결과 파일에 들어가는 라운드별 측정 레코드."""

        return [dict(record) for record in self._history]

    # 아래 네 속성은 /report(Phase 4 결과 패널)와 결과 파일이 같이 쓴다. 복사본을 돌려준다.

    @property
    def diagnoses(self) -> list[dict[str, Any]]:
        """라운드별 LLM 원본 판단 (forced 실행도 LLM 원본 그대로)."""

        return copy.deepcopy(self._diagnoses)

    @property
    def approvals(self) -> list[dict[str, Any]]:
        """승인 요청(source: llm/forced, scaling_plan)과 사용자 결정."""

        return copy.deepcopy(self._approvals)

    @property
    def scaling_results(self) -> list[dict[str, Any]]:
        """스케일링 실행 결과 (before/after replicas, success)."""

        return copy.deepcopy(self._scaling_results)

    @property
    def revalidations(self) -> list[dict[str, Any]]:
        """재검증 LLM 요약과 개선 여부 (LLM이 계산한 변화량은 제외)."""

        return copy.deepcopy(self._revalidations)

    @property
    def scaling_performed(self) -> bool:
        """이 실행에서 스케일링이 한 번이라도 성공했는지."""

        return any(
            result["success"] for result in self._scaling_results
        )

    @property
    def user_decision(self) -> str:
        """마지막 승인 요청 기준 사용자 결정."""

        if not self._approvals:
            return "not_applicable"

        decision = self._approvals[-1]["decision"]

        return decision if decision is not None else "no_response"

    @property
    def result_file_relative(self) -> str | None:
        """저장한 결과 파일 경로 (repo 루트 기준 상대경로). 저장 전·실패 시 None."""

        if self.result_file is None:
            return None

        try:
            return self.result_file.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return str(self.result_file)

    def _latest_round(self) -> int | None:
        return self._history[-1]["round"] if self._history else None

    # ------------------------------------------------------------------
    # 기록
    # ------------------------------------------------------------------

    def set_start_replicas(self, replicas: int) -> None:
        """진단 시작 전 조회한 실제 replica 수. 최초 측정 레코드의 replicas가 된다."""

        try:
            value = _replicas_or_none(replicas)
            self.conditions["start_replicas"] = value
            self._current_replicas = value
        except Exception:
            logger.exception("start_replicas 기록 실패 task_id=%s", self.task_id)

    def mark_forced(self) -> None:
        """force_scaling이 LLM 판단(diagnosed)을 실제로 덮어썼다."""

        self.forced_scaling = True

    def open_approval(self, state: AgentRuntimeState) -> None:
        """사용자에게 스케일링 승인을 요청했다."""

        try:
            plan = state.get("scaling_plan") or {}
            # force 덮어쓰기는 최초 진단 직후 한 번만 일어나므로 첫 승인 요청만 forced다
            source = (
                "forced"
                if self.forced_scaling and not self._approvals
                else "llm"
            )

            self._approvals.append(
                {
                    "round": self._latest_round(),
                    "source": source,
                    "scaling_plan": {
                        "current_replicas": plan.get("current_replicas"),
                        "desired_replicas": plan.get("desired_replicas"),
                    },
                    "decision": None,
                }
            )
        except Exception:
            logger.exception("승인 요청 기록 실패 task_id=%s", self.task_id)

    def close_approval(self, approved: bool) -> None:
        """사용자가 승인 또는 거절했다."""

        try:
            if self._approvals:
                self._approvals[-1]["decision"] = (
                    "approved" if approved else "rejected"
                )
        except Exception:
            logger.exception("사용자 결정 기록 실패 task_id=%s", self.task_id)

    def capture_revalidation(self, content: str) -> None:
        """재검증 LLM 응답 원문. 다음 observe()에서 재측정 라운드에 붙인다."""

        try:
            self._pending_revalidations.append(str(content))
        except Exception:
            logger.exception("재검증 응답 기록 실패 task_id=%s", self.task_id)

    def observe(self, state: AgentRuntimeState) -> None:
        """engine 호출이 끝난 상태에서 새로 생긴 스케일링·측정·재검증·진단 결과를 누적한다."""

        try:
            self._observe(state)
        except Exception:
            logger.exception("측정 이력 기록 실패 task_id=%s", self.task_id)

    def _observe(self, state: AgentRuntimeState) -> None:
        # 한 번의 resume_after_approval 안에서는 스케일링 → 재측정 → 재검증 → 재진단 순서로 일어나므로
        # 같은 순서로 기록한다.
        scaling_result = state.get("scaling_result")

        if (
            scaling_result is not None
            and scaling_result is not self._last_scaling
        ):
            self._last_scaling = scaling_result
            self._scaling_results.append(
                {
                    # 이 스케일링의 근거가 된 측정 라운드
                    "round": self._latest_round(),
                    "before_replicas": scaling_result.before_replicas,
                    "after_replicas": scaling_result.after_replicas,
                    "success": scaling_result.success,
                    "error_message": scaling_result.error_message,
                }
            )

            if scaling_result.success:
                self._current_replicas = _replicas_or_none(
                    scaling_result.after_replicas
                )

        load_test_result = state.get("load_test_result")

        if (
            load_test_result is not None
            and load_test_result is not self._last_load_test
        ):
            self._last_load_test = load_test_result

            metrics = state.get("system_metrics")
            fresh_metrics: SystemMetrics | None = None

            # 메트릭 수집이 실패하면 이전 라운드 객체가 그대로 남는다. 그 값은 이번 측정이 아니다.
            if metrics is not None and metrics is not self._last_metrics:
                self._last_metrics = metrics
                fresh_metrics = metrics

            self._history.append(
                self._measurement_record(
                    load_test_result,
                    fresh_metrics,
                )
            )

        for content in self._pending_revalidations:
            self._revalidations.append(
                self._revalidation_record(content)
            )

        self._pending_revalidations.clear()

        report = state.get("bottleneck_report")

        if report is not None and report is not self._last_report:
            self._last_report = report
            self._diagnoses.append(
                {
                    # 이 진단의 입력이 된 측정 라운드. forced 실행도 LLM 원본 판단을 그대로 남긴다
                    "round": self._latest_round(),
                    "requires_scaling": report.requires_scaling,
                    "severity": report.severity,
                    "confidence": report.confidence,
                    "cause": report.cause,
                    "recommendation": report.recommendation,
                }
            )

    def _resource_value(
        self,
        metrics: SystemMetrics | None,
        value: float | None,
    ) -> float | str | None:
        if not RESOURCE_METRICS_COLLECTED:
            return UNMEASURED_LABEL

        return value if metrics is not None else None

    def _measurement_record(
        self,
        result: LoadTestResult,
        metrics: SystemMetrics | None,
    ) -> dict[str, Any]:
        return {
            "round": len(self._history) + 1,
            # engine은 최초 진단 뒤에는 스케일링 후에만 다시 측정한다
            "phase": "initial" if not self._history else "after_scaling",
            "replicas": self._current_replicas,
            "virtual_users": self.conditions["virtual_users"],
            "duration": result.duration,
            # LoadTestResult 원본값 (Locust Aggregated 행). 반올림하지 않는다
            "tps": result.tps,
            "latency_p95": result.latency_p95,
            "latency_avg": result.latency_avg,
            "error_rate": result.error_rate,
            "total_requests": result.total_requests,
            # 이번 라운드에 새로 수집한 SystemMetrics가 없으면 None
            "connection_count": (
                metrics.connection_count if metrics is not None else None
            ),
            "cpu_pct": self._resource_value(
                metrics,
                metrics.cpu_pct if metrics is not None else None,
            ),
            "mem_pct": self._resource_value(
                metrics,
                metrics.mem_pct if metrics is not None else None,
            ),
            # 부하 테스트 직후 메트릭 수집 시각 (UTC)
            "timestamp": (
                _to_utc_iso(metrics.timestamp)
                if metrics is not None
                else None
            ),
        }

    def _revalidation_record(self, content: str) -> dict[str, Any]:
        round_number = self._latest_round()

        try:
            data = _parse_revalidation_response(content)
        except Exception as exc:
            return {
                "round": round_number,
                "parse_error": str(exc),
                "raw": content[:RAW_TEXT_LIMIT],
            }

        # tps_change 등 LLM이 계산한 변화량은 측정값이 아니므로 저장하지 않는다
        return {
            "round": round_number,
            "summary": str(data["summary"]),
            "performance_improved": data["performance_improved"],
            "additional_action_required": data["additional_action_required"],
            "recommended_action": data.get("recommended_action"),
        }

    # ------------------------------------------------------------------
    # 저장
    # ------------------------------------------------------------------

    def build_record(
        self,
        state: AgentRuntimeState,
        end_reason: str,
        final_message: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": _now().isoformat(),
            "conditions": self.conditions,
            "forced_scaling": self.forced_scaling,
            "measurement_history": self.measurement_history,
            "diagnoses": self.diagnoses,
            "approvals": self.approvals,
            "scaling_results": self.scaling_results,
            "revalidations": self.revalidations,
            "user_decision": self.user_decision,
            "outcome": {
                "agent_outcome": state.get("agent_outcome"),
                "end_reason": end_reason,
                # UI 로그에 마지막으로 표시된 종료 메시지 (SSE 원문)
                "final_message": final_message,
                "final_answer": state.get("final_answer"),
                "error": state.get("error"),
                "loop_count": state.get("loop_count"),
                "scaling_count": state.get("scaling_count"),
                "optimization_plan": list(
                    state.get("optimization_plan") or []
                ),
            },
        }

    def finalize(
        self,
        state: AgentRuntimeState,
        end_reason: str,
        final_message: str | None = None,
    ) -> Path | None:
        """
        실행 기록을 결과 파일로 저장한다. 멱등이다(두 번째 호출부터는 첫 저장 결과를 반환).

        observe()를 거치지 않은 마지막 상태도 반영하도록 저장 전에 한 번 더 observe한다.
        """

        if self._finalized:
            return self.result_file

        self._finalized = True
        self.end_reason = end_reason

        try:
            self._observe(state)
            record = self.build_record(
                state,
                end_reason,
                final_message,
            )
        except Exception:
            logger.exception(
                "결과 기록 생성 실패 (에이전트 흐름은 계속) task_id=%s",
                self.task_id,
            )
            return None

        self.result_file = save_run_record(
            record,
            self.started_at,
        )

        return self.result_file

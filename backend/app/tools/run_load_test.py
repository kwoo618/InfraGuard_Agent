"""
run_load_test.py
Locust 실행 + 결과 파싱 Tool.

ReAct Loop의 첫 Action으로 호출되어 target-server에 실제 부하를 발생시키고,
Locust의 통계 출력을 파싱해 LoadTestResult로 변환한다.

`infra/locust/locustfile.py` 시나리오를 headless 모드(`-f`, `--headless`)로
서브프로세스 실행하고 `--csv` prefix를 넘긴다.

헤드라인 값(LoadTestResult)의 출처 (#85, docs/02 ISSUE-15):
- 우선 locustfile이 부하가 멈춘 뒤 쓰는 `<prefix>_final_stats.json`(Locust runner.stats 최종값)을 쓴다.
  `_stats.csv`의 Aggregated 행과 같은 계산이고, 부하 전체의 요청이 들어간다.
- 이 파일이 없거나 읽지 못하면 `<prefix>_stats.csv`의 Aggregated 행을 쓴다. Locust는 `_stats.csv`를
  1초마다 다시 쓰고 종료 시에는 닫기만 해서, 이 경우 부하 마지막 약 1초의 요청이 빠질 수 있다.
- run_load_test_detailed()는 어느 쪽을 썼는지 details["headline_source"]로 알려 준다.

결과 패널 그래프용 추가 데이터 (docs/03 Phase 4 그래프, #75):
run_load_test_detailed()는 같은 실행에서 LoadTestResult와 함께
- 초 단위 TPS·P95 시계열: locustfile이 쓰는 `<prefix>_requests.csv`(요청별 원시 기록)
- 엔드포인트별 통계: 종료 시 통계의 엔드포인트 항목 (없으면 `_stats.csv`의 엔드포인트 행)
을 돌려준다. LoadTestResult(schemas.py)는 바꾸지 않는다. 추가 데이터 파싱이 실패해도
LoadTestResult는 그대로 반환하고 해당 항목만 None이다.
"""

import csv
import json
import logging
import math
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.schemas import LoadTestResult

logger = logging.getLogger(__name__)

# CLAUDE.md 가드레일: "Locust 부하 상한 50 TPS" — 로컬 PC 한 대에서
# Locust + target-server + Prometheus + 백엔드를 동시에 돌리기 때문에
# CPU가 고갈되지 않도록 가상 사용자 수(≈TPS) 상한을 둔다.
MAX_TPS = 50

# 이 파일(backend/app/tools/run_load_test.py)을 기준으로 프로젝트 루트를 계산한다.
# Path(__file__).resolve().parents 인덱스:
#   [0] = backend/app/tools
#   [1] = backend/app
#   [2] = backend
#   [3] = 프로젝트 루트 (InfraGuard_Agent)
# uvicorn을 backend/ 디렉터리에서 실행하더라도 infra/locust 경로를 항상 절대경로로
# 찾을 수 있도록 이렇게 계산해둔다. (cwd에 의존하면 실행 위치가 바뀔 때 깨짐)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCUST_DIR = PROJECT_ROOT / "infra" / "locust"
LOCUSTFILE = LOCUST_DIR / "locustfile.py"

# .env의 TARGET_SERVER_URL을 우선 사용하고, 없으면 docker-compose 기본 포트(8080)로 폴백.
DEFAULT_HOST = os.getenv("TARGET_SERVER_URL", "http://localhost:8080")

# locustfile.py REQUEST_LOG_SUFFIX / FINAL_STATS_SUFFIX와 같아야 한다
# (locust를 import하면 백엔드 프로세스에 gevent 패치가 적용되므로 값을 따로 둔다)
REQUEST_LOG_SUFFIX = "_requests.csv"
FINAL_STATS_SUFFIX = "_final_stats.json"

# 헤드라인·엔드포인트 통계의 출처 (결과 파일 measurement_history 레코드에 기록된다)
SOURCE_FINAL_STATS = "locust_final_stats"   # Locust 종료 시 통계: 부하 전체 요청
SOURCE_STATS_CSV = "locust_stats_csv"       # _stats.csv: 마지막 약 1초 누락 가능

# 초 단위 시계열 구간 길이(초)와 P95 계산 방식
TIMESERIES_BUCKET_SEC = 1
TIMESERIES_PERCENTILE = 0.95


class LoadTestError(RuntimeError):
    """Locust 실행 또는 결과 파싱이 실패했을 때 발생.

    ReAct Loop(박정기 담당)이 이 예외를 잡아 agent_outcome="failed"로
    처리할 수 있도록 RuntimeError를 상속한 전용 예외로 분리했다.
    """


def run_load_test(
    target_tps: int,
    duration: int,
    host: str = DEFAULT_HOST,
    spawn_rate: int | None = None,
) -> LoadTestResult:
    """Locust 부하 테스트를 실행하고 결과를 LoadTestResult로 반환한다.

    Args:
        target_tps: 목표 TPS(=가상 사용자 수로 근사). MAX_TPS를 넘을 수 없다.
            locustfile.py의 wait_time(0.1~0.5초)을 기준으로 설계했기 때문에
            "가상 사용자 1명 ≈ 초당 요청 1~2건" 정도로 근사된다. 정확한 TPS는
            Locust가 직접 측정한 값(결과의 tps 필드)을 신뢰해야 한다.
        duration: 부하 테스트 지속 시간(초). Locust의 --run-time 옵션에 그대로 전달.
        host: 부하를 받을 target-server 주소. 기본값은 DEFAULT_HOST.
        spawn_rate: 초당 추가로 생성할 가상 사용자 수(--spawn-rate).
            None이면 target_tps와 동일하게 설정해 거의 즉시 목표 사용자 수에
            도달하도록 한다(점진적 ramp-up이 필요 없는 짧은 진단 테스트이므로).

    Returns:
        LoadTestResult: tps, latency_p95, latency_avg, error_rate, duration,
        total_requests를 담은 dataclass (schemas.py 정의, 박정기 nodes.py가 소비).
        값은 Locust 종료 시 통계(없으면 _stats.csv Aggregated 행)에서 온다.

    Raises:
        ValueError: target_tps/duration이 가드레일을 벗어난 경우.
        LoadTestError: Locust 프로세스가 비정상 종료했거나, 타임아웃됐거나,
            결과 CSV 파싱에 실패한 경우.
    """
    result, _ = _run_locust(target_tps, duration, host, spawn_rate, collect_details=False)
    return result


def run_load_test_detailed(
    target_tps: int,
    duration: int,
    host: str = DEFAULT_HOST,
    spawn_rate: int | None = None,
) -> tuple[LoadTestResult, dict[str, Any]]:
    """run_load_test와 같은 부하 테스트를 실행하고, 결과 패널 그래프용 추가 데이터도 함께 반환한다.

    Returns:
        (LoadTestResult, details)
        - LoadTestResult: run_load_test와 같은 값
        - details["timeseries"]: 초 단위 요청 수·실패 수·P95 (요청별 원시 기록 기준). 없거나 파싱 실패면 None
        - details["endpoints"]: 엔드포인트별 Locust 통계. 파싱 실패면 None
        - details["headline_source"]: LoadTestResult 값의 출처 (SOURCE_FINAL_STATS / SOURCE_STATS_CSV)
        - details["endpoints_source"]: endpoints의 출처 (같은 값 중 하나, 없으면 None)

    Raises:
        run_load_test와 같다. 추가 데이터 파싱 실패로는 예외를 던지지 않는다.
    """
    result, details = _run_locust(target_tps, duration, host, spawn_rate, collect_details=True)
    return result, details or {
        "timeseries": None,
        "endpoints": None,
        "headline_source": None,
        "endpoints_source": None,
    }


def _run_locust(
    target_tps: int,
    duration: int,
    host: str,
    spawn_rate: int | None,
    collect_details: bool,
) -> tuple[LoadTestResult, dict[str, Any] | None]:
    """Locust를 한 번 실행하고 (LoadTestResult, details 또는 None)을 반환한다."""
    # --- 입력 검증: 잘못된 값으로 Locust를 띄워 자원을 낭비하지 않도록 사전에 막는다 ---
    if target_tps <= 0:
        raise ValueError("target_tps는 1 이상이어야 합니다.")
    if target_tps > MAX_TPS:
        raise ValueError(f"target_tps는 {MAX_TPS}를 초과할 수 없습니다 (요청값: {target_tps}).")
    if duration <= 0:
        raise ValueError("duration은 1 이상이어야 합니다.")

    # 결과 CSV를 요청마다 격리된 임시 디렉터리에 쓴다.
    # with 블록을 빠져나가면 디렉터리가 자동 삭제되므로 디스크에 테스트 잔여물이 남지 않는다.
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Locust --csv 옵션은 "prefix"를 받아 prefix_stats.csv / prefix_failures.csv 등을 생성한다.
        # locustfile.py는 같은 prefix로 prefix_requests.csv(요청별 원시 기록)와
        # prefix_final_stats.json(종료 시 통계)을 쓴다.
        csv_prefix = str(Path(tmp_dir) / "result")

        cmd = [
            "locust",
            "-f",
            str(LOCUSTFILE),       # 실행할 시나리오 파일 (locustfile.py)
            "--headless",          # 웹 UI 없이 CLI에서 바로 실행 (CI/자동화 친화적)
            "--host",
            host,                  # 부하를 받을 target-server 주소
            "--users",
            str(target_tps),       # 동시 가상 사용자 수 (target_tps로 근사)
            "--spawn-rate",
            str(spawn_rate or target_tps),  # 초당 사용자 증가 속도
            "--run-time",
            f"{duration}s",        # 테스트 지속 시간
            "--csv",
            csv_prefix,            # 통계 결과를 csv_prefix_*.csv 로 저장
            "--only-summary",      # 매 요청 로그 대신 요약만 출력 (stdout 노이즈 감소)
            "--exit-code-on-error", "0",  # 요청 실패(예: /flaky 500)로는 non-zero 종료 안 함
        ]

        # subprocess.Popen으로 비동기 실행 후 communicate()로 종료를 기다린다.
        # cwd를 LOCUST_DIR로 줘서 locust.conf(같은 디렉터리)가 함께 로드되게 한다.
        process = subprocess.Popen(
            cmd,
            cwd=str(LOCUST_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 가드레일: ReAct Loop 전체에 asyncio.timeout(300)이 걸려있지만,
            # Locust 프로세스 자체가 멈추는 경우를 대비해 duration + 60초로
            # 별도 타임아웃을 둔다 (Locust 기동/종료 오버헤드 감안).
            _, stderr = process.communicate(timeout=duration + 60)
        except subprocess.TimeoutExpired:
            # 좀비 프로세스가 남지 않도록 반드시 kill 후 예외를 던진다.
            process.kill()
            raise LoadTestError(f"Locust 실행이 {duration + 60}초를 초과해 타임아웃 되었습니다.")

        stats_path = Path(f"{csv_prefix}_stats.csv")

        # CSV 존재 여부를 exit code보다 우선 판단한다.
        # CSV가 있으면 Locust가 정상 완주한 것이므로 exit code와 무관하게 파싱한다.
        # (--exit-code-on-error 0으로 요청 실패는 exit 1을 내지 않지만, 이중 방어로 유지)
        if not stats_path.exists():
            # CSV가 없으면 Locust 자체가 크래시한 것 (문법 오류, host 연결 실패 등)
            raise LoadTestError(
                f"Locust 결과 파일을 찾을 수 없습니다 (exit={process.returncode}): {stderr}"
            )

        # 헤드라인: 종료 시 통계 우선, 없거나 읽지 못하면 _stats.csv
        final_stats = _safe_details(
            "종료 시 통계",
            _read_final_stats,
            Path(f"{csv_prefix}{FINAL_STATS_SUFFIX}"),
        )
        result = _safe_details("종료 시 통계 헤드라인", _result_from_final_stats, final_stats, duration) if final_stats else None

        if result is not None:
            headline_source = SOURCE_FINAL_STATS
        else:
            logger.warning(
                "Locust 종료 시 통계가 없어 _stats.csv로 헤드라인을 계산한다 (마지막 약 1초 누락 가능, #85)"
            )
            result = _parse_stats_csv(stats_path, duration)
            headline_source = SOURCE_STATS_CSV

        if not collect_details:
            return result, None

        # 임시 디렉터리가 지워지기 전에 읽는다. 실패해도 LoadTestResult는 그대로 반환한다.
        endpoints = None
        endpoints_source = None

        if final_stats:
            endpoints = _safe_details("종료 시 엔드포인트 통계", _endpoints_from_final_stats, final_stats)
            endpoints_source = SOURCE_FINAL_STATS if endpoints is not None else None

        if endpoints is None:
            endpoints = _safe_details("엔드포인트별 통계", _parse_endpoint_rows, stats_path)
            endpoints_source = SOURCE_STATS_CSV if endpoints is not None else None

        details = {
            "timeseries": _safe_details(
                "초 단위 시계열",
                _parse_request_log,
                Path(f"{csv_prefix}{REQUEST_LOG_SUFFIX}"),
            ),
            "endpoints": endpoints,
            "headline_source": headline_source,
            "endpoints_source": endpoints_source,
        }

        return result, details


def _safe_details(label: str, parser, *args):
    """추가 데이터 파서를 실행한다. 실패하면 로그만 남기고 None (에이전트 흐름을 멈추지 않는다)."""
    try:
        return parser(*args)
    except Exception:
        logger.warning("Locust %s 파싱 실패 (LoadTestResult는 정상 반환)", label, exc_info=True)
        return None


def _to_float(value: Any, default: float | None = 0.0) -> float | None:
    # 테스트 시간이 너무 짧거나 요청 수가 적으면 Locust가 'N/A'(CSV) 또는 None(종료 시 통계)을 기록한다.
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _read_final_stats(path: Path) -> dict[str, Any] | None:
    """locustfile이 쓴 종료 시 통계. 파일이 없으면 None, 형식이 틀리면 예외."""
    if not path.exists():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict) or not isinstance(data.get("aggregated"), dict):
        raise ValueError("종료 시 통계에 aggregated 항목이 없습니다.")

    return data


def _result_from_final_stats(data: dict[str, Any], duration: int) -> LoadTestResult:
    """종료 시 통계의 Aggregated 값으로 LoadTestResult를 만든다 (_parse_stats_csv와 같은 규칙)."""
    aggregated = data["aggregated"]
    total_requests = int(aggregated["num_requests"])
    failure_count = int(aggregated["num_failures"])

    return LoadTestResult(
        tps=_to_float(aggregated.get("total_rps")),
        latency_p95=_to_float(aggregated.get("p95_response_time")),
        latency_avg=_to_float(aggregated.get("avg_response_time")),
        error_rate=(failure_count / total_requests) if total_requests > 0 else 0.0,
        duration=duration,
        total_requests=total_requests,
    )


def _endpoints_from_final_stats(data: dict[str, Any]) -> list[dict[str, Any]]:
    """종료 시 통계의 엔드포인트 항목을 _parse_endpoint_rows와 같은 모양으로 옮긴다."""
    endpoints = []

    for entry in data.get("entries") or []:
        requests = int(entry["num_requests"])
        failures = int(entry["num_failures"])

        endpoints.append({
            "name": entry["name"],
            "method": entry.get("method") or None,
            "requests": requests,
            "failures": failures,
            "error_rate": (failures / requests) if requests > 0 else None,
            "p95_ms": _to_float(entry.get("p95_response_time"), None),
            "avg_ms": _to_float(entry.get("avg_response_time"), None),
            "rps": _to_float(entry.get("total_rps"), None),
        })

    return endpoints


def _read_stats_rows(stats_path: Path) -> list[dict[str, str]]:
    with stats_path.open(newline="", encoding="utf-8") as f:
        # csv.DictReader: 첫 줄을 헤더로 사용해 각 행을 {컬럼명: 값} dict로 변환
        return list(csv.DictReader(f))


def _parse_stats_csv(stats_path: Path, duration: int) -> LoadTestResult:
    """`<prefix>_stats.csv`의 Aggregated 행을 LoadTestResult로 변환한다.

    Locust의 stats CSV는 엔드포인트(Name)별 행 + 마지막에 모든 엔드포인트를
    합산한 "Aggregated" 행을 포함한다. InfraGuard Agent는 개별 엔드포인트가
    아니라 시스템 전체의 TPS/Latency를 진단하므로 Aggregated 행 하나만 사용한다.
    종료 시 통계가 없을 때만 쓴다 (마지막 약 1초 누락 가능, #85).
    """
    rows = _read_stats_rows(stats_path)

    aggregated = next((row for row in rows if row.get("Name") == "Aggregated"), None)
    if aggregated is None:
        raise LoadTestError("Locust 결과 CSV에서 Aggregated 행을 찾을 수 없습니다.")

    total_requests = int(aggregated["Request Count"])
    failure_count = int(aggregated["Failure Count"])
    # 0으로 나누기 방지: 요청이 한 건도 없었다면 error_rate는 0으로 처리
    error_rate = (failure_count / total_requests) if total_requests > 0 else 0.0

    return LoadTestResult(
        tps=_to_float(aggregated["Requests/s"]),
        latency_p95=_to_float(aggregated["95%"]),
        latency_avg=_to_float(aggregated["Average Response Time"]),
        error_rate=error_rate,
        duration=duration,
        total_requests=total_requests,
    )


def _parse_endpoint_rows(stats_path: Path) -> list[dict[str, Any]]:
    """`<prefix>_stats.csv`의 엔드포인트 행(Aggregated 제외)을 그대로 옮긴다 (Locust 값, 반올림 없음).

    'N/A' 같은 값은 0으로 채우지 않고 None으로 둔다 (측정값이 아니므로).
    """
    endpoints = []

    for row in _read_stats_rows(stats_path):
        name = row.get("Name")

        if not name or name == "Aggregated":
            continue

        requests = int(row["Request Count"])
        failures = int(row["Failure Count"])

        endpoints.append({
            "name": name,
            "method": row.get("Type") or None,
            "requests": requests,
            "failures": failures,
            "error_rate": (failures / requests) if requests > 0 else None,
            "p95_ms": _to_float(row.get("95%"), None),
            "avg_ms": _to_float(row.get("Average Response Time"), None),
            "rps": _to_float(row.get("Requests/s"), None),
        })

    return endpoints


def nearest_rank_percentile(sorted_values: list[float], q: float) -> float | None:
    """nearest-rank 백분위수: 정렬된 값에서 ceil(q × n)번째 값. 값이 없으면 None."""
    if not sorted_values:
        return None

    index = max(math.ceil(q * len(sorted_values)) - 1, 0)
    return sorted_values[index]


def _parse_request_log(log_path: Path) -> dict[str, Any] | None:
    """locustfile이 쓴 요청별 원시 기록으로 초 단위 요청 수·실패 수·P95를 만든다.

    - 구간: 부하 시작(start 행) 기준 [k, k+1)초. 요청은 완료 시각으로 구간을 정한다
      (Locust의 초당 요청 수 집계와 같은 기준).
    - P95: 구간 안 원시 응답시간(ms)의 nearest-rank 95%. Locust 요약 P95는 응답시간을
      반올림해 저장한 뒤 전체 구간으로 계산하므로 값이 조금 다를 수 있다.
    - 요청이 없는 구간은 requests=0, p95_ms=None으로 둔다 (0ms로 채우지 않는다).
    - 파일이 없거나 start 행·요청이 없으면 None.
    """
    if not log_path.exists():
        return None

    start: float | None = None
    requests: list[tuple[float, float, bool]] = []

    with log_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            event = row.get("event")

            if event == "start":
                start = float(row["time"])
            elif event == "request":
                requests.append((
                    float(row["time"]),
                    float(row["response_time_ms"]),
                    row["failed"] == "1",
                ))

    if start is None or not requests:
        return None

    buckets: dict[int, list[tuple[float, bool]]] = {}

    for completed_at, response_time, failed in requests:
        index = max(int((completed_at - start) // TIMESERIES_BUCKET_SEC), 0)
        buckets.setdefault(index, []).append((response_time, failed))

    points = []

    for index in range(max(buckets) + 1):
        items = buckets.get(index, [])
        response_times = sorted(response_time for response_time, _ in items)

        points.append({
            "t": index * TIMESERIES_BUCKET_SEC,
            "requests": len(items),
            "failures": sum(1 for _, failed in items if failed),
            "p95_ms": nearest_rank_percentile(response_times, TIMESERIES_PERCENTILE),
        })

    return {
        "bucket_sec": TIMESERIES_BUCKET_SEC,
        "source": "locust_request_log",
        "percentile": "nearest_rank_p95",
        "points": points,
    }

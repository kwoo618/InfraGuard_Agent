"""
test_load_runner.py - run_load_test Tool 단위 테스트.

CONTRIBUTING.md 테스트 규칙에 따라 실제 Locust 프로세스를 띄우지 않고
subprocess.Popen을 Mock 처리한다 (CPU/시간 비용이 큰 실제 부하 테스트를
CI에서 매번 돌릴 수 없기 때문).

아래 CSV·요청 기록·종료 시 통계의 수치는 테스트 입력값이며 측정값이 아니다.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.schemas import LoadTestResult
from app.tools.run_load_test import (
    MAX_TPS,
    SOURCE_FINAL_STATS,
    SOURCE_STATS_CSV,
    LoadTestError,
    nearest_rank_percentile,
    run_load_test,
    run_load_test_detailed,
)

# run_load_test._parse_stats_csv가 실제로 파싱하게 될 Locust 표준 stats CSV 포맷.
# 컬럼 순서/이름은 Locust 라이브러리가 --csv 옵션으로 생성하는 실제 헤더와 동일하게 맞췄다.
# 행 구성:
#   - /light: 정상 600건 (실패 0건)
#   - /heavy: 300건 중 5건 실패 (병목 시나리오 흉내)
#   - Aggregated: 위 두 엔드포인트(+나머지)를 합산한 전체 행 → 코드가 실제로 읽는 행
STATS_CSV = (
    "Type,Name,Request Count,Failure Count,Median Response Time,"
    "Average Response Time,Min Response Time,Max Response Time,"
    "Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%\n"
    "GET,/light,600,0,5,6.2,1,40,12,20.0,0.0,5,6,7,8,9,10,11,12,13,14,15\n"
    "GET,/heavy,300,5,400,420.5,100,900,12,10.0,0.2,400,420,440,460,480,495,500,510,520,530,540\n"
    "None,Aggregated,900,5,10,150.3,1,900,12,30.0,0.2,10,20,30,40,50,95,98,99,100,101,102\n"
)


def _request_log(start: float = 1000.0) -> str:
    """locustfile.py 훅이 쓰는 형식의 요청별 원시 기록.

    구간 0초: 응답시간 100, 200, ..., 2000ms 20건 (nearest-rank P95 = 1900)
    구간 1초: 3건 중 1건 실패
    구간 2초: 요청 없음
    구간 3초: 1건
    합계 24건 (/light 21, /heavy 1, /flaky 1 실패, /health 1)
    """
    lines = ["event,time,name,response_time_ms,failed", f"start,{start:.6f},,,"]

    for i in range(20):
        lines.append(f"request,{start + 0.04 * i + 0.01:.6f},/light,{100.0 * (i + 1):.3f},0")

    lines.append(f"request,{start + 1.10:.6f},/heavy,300.000,0")
    lines.append(f"request,{start + 1.50:.6f},/flaky,50.000,1")
    lines.append(f"request,{start + 1.99:.6f},/light,10.000,0")
    lines.append(f"request,{start + 3.20:.6f},/health,5.000,0")
    lines.append(f"stop,{start + 4.00:.6f},,,")

    return "\n".join(lines) + "\n"


def _entry(name, method, num_requests, num_failures, avg, rps, p95):
    return {
        "name": name,
        "method": method,
        "num_requests": num_requests,
        "num_failures": num_failures,
        "avg_response_time": avg,
        "total_rps": rps,
        "p95_response_time": p95,
    }


# locustfile.py _write_final_stats가 쓰는 형식의 종료 시 통계.
# _request_log()와 같은 24건이다. _stats.csv(STATS_CSV)는 이보다 오래된 스냅샷을 흉내 낸다.
FINAL_STATS = {
    "source": "locust_runner_stats",
    "written_at": 1004.0,
    "aggregated": _entry("Aggregated", None, 24, 1, 1060.0, 6.0, 1900),
    "entries": [
        _entry("/flaky", "GET", 1, 1, 50.0, 0.25, 50),
        _entry("/health", "GET", 1, 0, 5.0, 0.25, 5),
        _entry("/heavy", "GET", 1, 0, 300.0, 0.25, 300),
        _entry("/light", "GET", 21, 0, 1000.5, 5.25, 1900),
    ],
}


def _fake_popen_factory(
    tmp_path: Path,
    returncode: int = 0,
    write_csv: bool = True,
    requests_log: str | None = None,
    final_stats: str | None = None,
):
    """subprocess.Popen을 대체할 Mock 팩토리 함수를 만든다.

    run_load_test()는 내부에서 tempfile.TemporaryDirectory()로 만든 임시 경로를
    --csv 인자로 Locust에 넘기는데, 그 정확한 경로는 호출 시점에만 알 수 있다.
    그래서 실제 Popen 호출 시 전달되는 cmd 리스트에서 "--csv" 다음 토큰(=csv_prefix)을
    그때그때 읽어내, communicate() 시점에 그 자리에 가짜 stats CSV를 써준다.
    이렇게 하면 run_load_test()의 실제 코드 흐름(파일 존재 확인 → 파싱)을
    그대로 검증할 수 있다.

    Args:
        tmp_path: pytest 기본 제공 fixture (이 함수 시그니처상 직접 쓰이진 않지만,
            테스트 함수에서 격리된 임시 디렉터리가 필요할 때 전달용으로 남겨둠).
        returncode: Locust 프로세스의 가짜 종료 코드. 0이 아니면 run_load_test가
            LoadTestError를 던지는 경로를 테스트할 수 있다.
        write_csv: False면 stats CSV를 일부러 쓰지 않아, "결과 파일을 찾을 수 없음"
            에러 경로를 재현한다.
        requests_log: 주면 locustfile 훅처럼 <prefix>_requests.csv에 이 내용을 쓴다.
        final_stats: 주면 locustfile 훅처럼 <prefix>_final_stats.json에 이 내용을 쓴다.
    """

    def _popen(cmd, cwd=None, stdout=None, stderr=None, text=None):
        # 실제 subprocess.Popen 객체 대신 반환할 가짜 객체.
        # run_load_test()가 사용하는 속성/메서드(returncode, communicate)만 흉내내면 충분하다.
        mock_process = MagicMock()
        mock_process.returncode = returncode

        # cmd 예: [..., "--csv", "C:/temp/xxx/result", ...]
        # "--csv" 바로 다음 원소가 run_load_test()가 기대하는 csv_prefix다.
        csv_prefix = cmd[cmd.index("--csv") + 1]

        def _communicate(timeout=None):
            # 실제 Locust라면 이 시점에 *_stats.csv 파일을 디스크에 써놓고 종료한다.
            # 여기서는 그 효과만 흉내내서 stats CSV를 직접 작성해준다.
            if write_csv:
                Path(f"{csv_prefix}_stats.csv").write_text(STATS_CSV, encoding="utf-8")
            if requests_log is not None:
                Path(f"{csv_prefix}_requests.csv").write_text(requests_log, encoding="utf-8")
            if final_stats is not None:
                Path(f"{csv_prefix}_final_stats.json").write_text(final_stats, encoding="utf-8")
            # subprocess.Popen.communicate()는 (stdout, stderr) 튜플을 반환한다.
            return ("", "" if returncode == 0 else "boom")

        mock_process.communicate.side_effect = _communicate
        return mock_process

    return _popen


def test_run_load_test_parses_aggregated_row(tmp_path):
    """정상 케이스: Locust가 성공적으로 끝나고 Aggregated 행을 올바르게 파싱하는지 확인."""
    with patch("app.tools.run_load_test.subprocess.Popen", side_effect=_fake_popen_factory(tmp_path)):
        result = run_load_test(target_tps=10, duration=5)

    # 반환 타입이 schemas.py의 LoadTestResult 계약을 그대로 지키는지 확인 (박정기가 소비하는 형태)
    assert isinstance(result, LoadTestResult)
    # STATS_CSV의 Aggregated 행 값과 1:1 매칭되는지 확인
    assert result.tps == 30.0                # Requests/s 컬럼
    assert result.latency_p95 == 95.0         # 95% 컬럼 (ms)
    assert result.latency_avg == 150.3        # Average Response Time 컬럼 (ms)
    assert result.total_requests == 900       # Request Count 컬럼
    assert result.error_rate == pytest.approx(5 / 900)  # Failure Count / Request Count
    assert result.duration == 5               # 호출 시 넘긴 duration 그대로 보존되는지


def test_run_load_test_rejects_tps_over_guardrail():
    """가드레일: target_tps가 MAX_TPS(50)를 넘으면 Locust를 띄우기도 전에 ValueError."""
    with pytest.raises(ValueError):
        run_load_test(target_tps=MAX_TPS + 1, duration=5)


def test_run_load_test_rejects_non_positive_duration():
    """가드레일: duration이 0 이하면 ValueError (Locust --run-time에 0s를 넘기지 않도록 방지)."""
    with pytest.raises(ValueError):
        run_load_test(target_tps=10, duration=0)


def test_run_load_test_raises_on_nonzero_exit_without_csv():
    """Locust가 크래시(exit != 0)하고 CSV도 없으면 LoadTestError.

    진짜 크래시 판정 기준은 CSV 부재이며, exit code는 보조 정보로만 쓴다.
    예: locustfile 문법 오류, target-server 연결 실패 등.
    """
    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(Path("."), returncode=1, write_csv=False),
    ):
        with pytest.raises(LoadTestError):
            run_load_test(target_tps=10, duration=5)


def test_run_load_test_succeeds_with_nonzero_exit_when_csv_exists(tmp_path):
    """/flaky 실패로 exit=1이더라도 CSV가 있으면 LoadTestResult를 정상 반환.

    Locust 기본 동작: 요청 실패가 1건이라도 있으면 exit 1.
    --exit-code-on-error 0으로 막지만, CSV 존재 우선 판단으로 이중 방어.
    """
    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(tmp_path, returncode=1, write_csv=True),
    ):
        result = run_load_test(target_tps=10, duration=5)

    assert isinstance(result, LoadTestResult)
    assert result.total_requests == 900


def test_run_load_test_raises_when_stats_csv_missing():
    """exit code는 0인데 결과 CSV가 안 만들어진 비정상 케이스도 LoadTestError로 처리되는지 확인."""
    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(Path("."), returncode=0, write_csv=False),
    ):
        with pytest.raises(LoadTestError):
            run_load_test(target_tps=10, duration=5)


# ----------------------------------------------------------------------
# run_load_test_detailed — 결과 패널 그래프용 추가 데이터 (docs/03 Phase 4 그래프)
# ----------------------------------------------------------------------


def test_nearest_rank_percentile():
    values = [float(v) for v in range(1, 21)]   # 1..20

    assert nearest_rank_percentile(values, 0.95) == 19.0   # ceil(0.95 × 20) = 19번째
    assert nearest_rank_percentile([7.0], 0.95) == 7.0
    assert nearest_rank_percentile([], 0.95) is None


def test_detailed_returns_same_result_with_timeseries_and_endpoints(tmp_path):
    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(tmp_path, requests_log=_request_log()),
    ):
        result, details = run_load_test_detailed(target_tps=10, duration=5)

    # 종료 시 통계가 없으면 LoadTestResult는 run_load_test와 같은 _stats.csv 값이다
    assert result == LoadTestResult(
        tps=30.0,
        latency_p95=95.0,
        latency_avg=150.3,
        error_rate=5 / 900,
        duration=5,
        total_requests=900,
    )
    assert details["headline_source"] == SOURCE_STATS_CSV
    assert details["endpoints_source"] == SOURCE_STATS_CSV

    timeseries = details["timeseries"]

    assert timeseries["bucket_sec"] == 1
    assert timeseries["source"] == "locust_request_log"
    assert timeseries["points"] == [
        {"t": 0, "requests": 20, "failures": 0, "p95_ms": 1900.0},
        {"t": 1, "requests": 3, "failures": 1, "p95_ms": 300.0},
        # 요청이 없는 구간은 0ms가 아니라 None
        {"t": 2, "requests": 0, "failures": 0, "p95_ms": None},
        {"t": 3, "requests": 1, "failures": 0, "p95_ms": 5.0},
    ]

    assert details["endpoints"] == [
        {
            "name": "/light",
            "method": "GET",
            "requests": 600,
            "failures": 0,
            "error_rate": 0.0,
            "p95_ms": 10.0,
            "avg_ms": 6.2,
            "rps": 20.0,
        },
        {
            "name": "/heavy",
            "method": "GET",
            "requests": 300,
            "failures": 5,
            "error_rate": 5 / 300,
            "p95_ms": 495.0,
            "avg_ms": 420.5,
            "rps": 10.0,
        },
    ]


def test_detailed_without_request_log_gives_null_timeseries(tmp_path):
    """요청 기록이 없어도(예: 예전 locustfile) LoadTestResult와 엔드포인트 통계는 정상이다."""
    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(tmp_path),
    ):
        result, details = run_load_test_detailed(target_tps=10, duration=5)

    assert result.total_requests == 900
    assert details["timeseries"] is None
    assert [endpoint["name"] for endpoint in details["endpoints"]] == ["/light", "/heavy"]


def test_detailed_with_broken_request_log_gives_null_timeseries(tmp_path):
    """요청 기록이 깨져 있으면 시계열만 None이고 예외를 던지지 않는다."""
    broken = "event,time,name,response_time_ms,failed\nstart,not-a-number,,,\n"

    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(tmp_path, requests_log=broken),
    ):
        result, details = run_load_test_detailed(target_tps=10, duration=5)

    assert result.total_requests == 900
    assert details["timeseries"] is None
    assert details["endpoints"] is not None


def test_detailed_with_start_only_log_gives_null_timeseries(tmp_path):
    """요청이 한 건도 기록되지 않았으면 시계열은 None (빈 그래프 대신 측정값 없음)."""
    start_only = "event,time,name,response_time_ms,failed\nstart,1000.0,,,\nstop,1005.0,,,\n"

    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(tmp_path, requests_log=start_only),
    ):
        _, details = run_load_test_detailed(target_tps=10, duration=5)

    assert details["timeseries"] is None


# ----------------------------------------------------------------------
# 헤드라인 값의 출처 — Locust 종료 시 통계 (#85, docs/02 ISSUE-15)
# ----------------------------------------------------------------------


def test_headline_uses_final_stats_when_available(tmp_path):
    """종료 시 통계가 있으면 오래된 _stats.csv 스냅샷(900건)이 아니라 최종값을 쓴다."""
    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(tmp_path, final_stats=json.dumps(FINAL_STATS)),
    ):
        result = run_load_test(target_tps=10, duration=5)

    assert result == LoadTestResult(
        tps=6.0,
        latency_p95=1900.0,
        latency_avg=1060.0,
        error_rate=1 / 24,
        duration=5,
        total_requests=24,
    )


def test_detailed_headline_total_matches_timeseries_sum(tmp_path):
    """
    그래프 ①(요청별 기록)의 초별 요청 수 합과 헤드라인 total_requests가 같다.
    두 값 모두 Locust의 같은 request 이벤트를 전부 센 값이기 때문이다 (#85).
    """
    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(
            tmp_path,
            requests_log=_request_log(),
            final_stats=json.dumps(FINAL_STATS),
        ),
    ):
        result, details = run_load_test_detailed(target_tps=10, duration=5)

    points = details["timeseries"]["points"]

    assert result.total_requests == sum(point["requests"] for point in points) == 24
    assert round(result.error_rate * result.total_requests) == sum(point["failures"] for point in points) == 1
    assert details["headline_source"] == SOURCE_FINAL_STATS

    # 엔드포인트 통계도 종료 시 통계에서 온다 (합계가 헤드라인과 같다)
    assert details["endpoints_source"] == SOURCE_FINAL_STATS
    assert [endpoint["name"] for endpoint in details["endpoints"]] == ["/flaky", "/health", "/heavy", "/light"]
    assert sum(endpoint["requests"] for endpoint in details["endpoints"]) == 24
    assert details["endpoints"][0] == {
        "name": "/flaky",
        "method": "GET",
        "requests": 1,
        "failures": 1,
        "error_rate": 1.0,
        "p95_ms": 50.0,
        "avg_ms": 50.0,
        "rps": 0.25,
    }


def test_broken_final_stats_falls_back_to_csv(tmp_path):
    """종료 시 통계가 깨져 있으면 예외 없이 _stats.csv로 계산하고 출처를 그렇게 남긴다."""
    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(tmp_path, final_stats="{not json"),
    ):
        result, details = run_load_test_detailed(target_tps=10, duration=5)

    assert result.total_requests == 900
    assert details["headline_source"] == SOURCE_STATS_CSV
    assert details["endpoints_source"] == SOURCE_STATS_CSV


def test_final_stats_without_requests_matches_csv_rules(tmp_path):
    """요청 0건이면 P95는 None → 0.0, 에러율 0.0 (_stats.csv의 N/A 처리와 같은 규칙)."""
    empty = {
        "source": "locust_runner_stats",
        "aggregated": _entry("Aggregated", None, 0, 0, 0.0, 0.0, None),
        "entries": [],
    }

    with patch(
        "app.tools.run_load_test.subprocess.Popen",
        side_effect=_fake_popen_factory(tmp_path, final_stats=json.dumps(empty)),
    ):
        result, details = run_load_test_detailed(target_tps=10, duration=5)

    assert result.total_requests == 0
    assert result.latency_p95 == 0.0
    assert result.error_rate == 0.0
    assert details["headline_source"] == SOURCE_FINAL_STATS
    assert details["endpoints"] == []

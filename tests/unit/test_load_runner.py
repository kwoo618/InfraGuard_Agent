"""
test_load_runner.py - run_load_test Tool 단위 테스트.

CONTRIBUTING.md 테스트 규칙에 따라 실제 Locust 프로세스를 띄우지 않고
subprocess.Popen을 Mock 처리한다 (CPU/시간 비용이 큰 실제 부하 테스트를
CI에서 매번 돌릴 수 없기 때문).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.schemas import LoadTestResult
from app.tools.run_load_test import MAX_TPS, LoadTestError, run_load_test

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


def _fake_popen_factory(tmp_path: Path, returncode: int = 0, write_csv: bool = True):
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

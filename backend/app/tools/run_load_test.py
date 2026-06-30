"""
이하은 - Locust 실행 + 결과 파싱 Tool.

ReAct Loop의 첫 Action으로 호출되어 target-server에 실제 부하를 발생시키고,
Locust의 CSV 통계 출력을 파싱해 LoadTestResult로 변환한다.

`infra/locust/locustfile.py` 시나리오를 headless 모드(`-f`, `--headless`)로
서브프로세스 실행하고, `--csv` 옵션으로 떨어지는 `<prefix>_stats.csv`의
Aggregated 행을 읽어 TPS/Latency/에러율을 계산한다.
"""

import csv
import os
import subprocess
import tempfile
from pathlib import Path

from app.schemas import LoadTestResult

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

    Raises:
        ValueError: target_tps/duration이 가드레일을 벗어난 경우.
        LoadTestError: Locust 프로세스가 비정상 종료했거나, 타임아웃됐거나,
            결과 CSV 파싱에 실패한 경우.
    """
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

        if process.returncode != 0:
            # Locust 자체가 에러로 죽은 경우 (예: locustfile 문법 오류, host 연결 실패 등)
            raise LoadTestError(f"Locust 실행 실패 (exit={process.returncode}): {stderr}")

        stats_path = Path(f"{csv_prefix}_stats.csv")
        if not stats_path.exists():
            # returncode가 0이어도 CSV가 안 만들어지는 비정상 케이스 방어
            raise LoadTestError(f"Locust 결과 파일을 찾을 수 없습니다: {stats_path}")

        return _parse_stats_csv(stats_path, duration)


def _parse_stats_csv(stats_path: Path, duration: int) -> LoadTestResult:
    """`<prefix>_stats.csv`의 Aggregated 행을 LoadTestResult로 변환한다.

    Locust의 stats CSV는 엔드포인트(Name)별 행 + 마지막에 모든 엔드포인트를
    합산한 "Aggregated" 행을 포함한다. InfraGuard Agent는 개별 엔드포인트가
    아니라 시스템 전체의 TPS/Latency를 진단하므로 Aggregated 행 하나만 사용한다.
    """
    with stats_path.open(newline="", encoding="utf-8") as f:
        # csv.DictReader: 첫 줄을 헤더로 사용해 각 행을 {컬럼명: 값} dict로 변환
        rows = list(csv.DictReader(f))

    aggregated = next((row for row in rows if row.get("Name") == "Aggregated"), None)
    if aggregated is None:
        raise LoadTestError("Locust 결과 CSV에서 Aggregated 행을 찾을 수 없습니다.")

    total_requests = int(aggregated["Request Count"])
    failure_count = int(aggregated["Failure Count"])
    # 0으로 나누기 방지: 요청이 한 건도 없었다면 error_rate는 0으로 처리
    error_rate = (failure_count / total_requests) if total_requests > 0 else 0.0

    return LoadTestResult(
        tps=float(aggregated["Requests/s"]),            # Locust가 직접 측정한 실측 TPS
        latency_p95=float(aggregated["95%"]),            # 95th percentile 응답시간 (ms)
        latency_avg=float(aggregated["Average Response Time"]),  # 평균 응답시간 (ms)
        error_rate=error_rate,                           # 0.0~1.0 사이 실패율
        duration=duration,                               # 호출 시점에 받은 목표 duration 그대로 기록
        total_requests=total_requests,
    )

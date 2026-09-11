"""
scale_service — target-server 컨테이너 replica 수를 Docker Compose로 조정하는 Tool.

담당: 최강우
소비자: 최소명 (api/v1/agent.py) — HITL 승인 후 이 함수가 호출되어 실제 스케일링이 실행됨.

동작 순서:
1. docker compose ps로 target-server의 현재 replica 수를 센다.
2. 요청받은 목표 replica가 SCALE_MAX_REPLICAS(가드레일)를 넘지 않는지 확인한다.
3. docker compose up -d --wait --scale target-server=N --no-recreate target-server 명령으로
   실제 스케일링을 실행하고, 새 replica가 healthy가 될 때까지 기다린다.
4. 결과를 ScalingResult(schemas.py)로 포장해 반환한다.

주의:
- HITL 승인 없이 이 함수가 직접 호출되면 안 된다. 호출 시점 통제는 api 레이어(최소명) 책임.
- subprocess 명령은 docker-compose.yml이 있는 프로젝트 루트에서 실행되어야 한다.
- --wait가 실패하면(타임아웃·unhealthy) 컨테이너는 이미 늘었을 수 있지만,
  다른 명령 실패와 같이 after_replicas=before_replicas인 실패로 보고된다.
"""

import os
import subprocess

from app.schemas import ScalingResult

SERVICE_NAME = "target-server"
SCALE_MAX_REPLICAS = int(os.getenv("SCALE_MAX_REPLICAS", "8"))
COMMAND_TIMEOUT_SECONDS = 30

# docker-compose.yml이 있는 위치. 이 파일 기준 상위 2단계(backend/app/tools -> 프로젝트 루트).
PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)


def _get_current_replica_count() -> int:
    """
    docker compose ps로 target-server 컨테이너가 현재 몇 개 떠 있는지 센다.
    Docker가 안 떠 있거나 명령 실패 시 0을 반환한다 (예외로 죽이지 않음).
    """
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--services", "--filter", "status=running"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return 0

    if result.returncode != 0:
        return 0

    # --services는 이름만 한 줄씩 주기 때문에 컨테이너 "개수"를 얻으려면 ps -a 형태가 필요하다.
    # 더 정확하게는 docker compose ps target-server -q 로 컨테이너 ID 목록을 센다.
    try:
        id_result = subprocess.run(
            ["docker", "compose", "ps", SERVICE_NAME, "-q"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return 0

    container_ids = [line for line in id_result.stdout.strip().splitlines() if line]
    return len(container_ids)


def get_current_replicas() -> int:
    """target-server의 현재 실행 중 replica 수를 반환하는 공개 헬퍼.

    소비자: 박정기 (agent/nodes.py) — 병목 분석 프롬프트의 current_replicas 인자와
    스케일링 목표(desired_replicas) 계산에 현재 replica 수가 필요하다.
    내부 구현(_get_current_replica_count)을 감싸 팀 외부에 안정적인 이름으로 노출한다.

    Docker가 꺼져 있거나 조회 실패 시 0을 반환한다(예외로 루프를 막지 않음).
    """
    return _get_current_replica_count()


async def scale_service(target_replicas: int) -> ScalingResult:
    """
    target-server를 target_replicas 개수로 스케일링한다.

    가드레일: target_replicas가 SCALE_MAX_REPLICAS를 넘으면 실행하지 않고 실패로 반환한다.
    """
    before_replicas = _get_current_replica_count()

    if target_replicas > SCALE_MAX_REPLICAS:
        return ScalingResult(
            before_replicas=before_replicas,
            after_replicas=before_replicas,
            success=False,
            error_message=(
                f"요청한 replica 수({target_replicas})가 상한({SCALE_MAX_REPLICAS})을 "
                f"초과해 거부되었습니다."
            ),
        )

    if target_replicas < 1:
        return ScalingResult(
            before_replicas=before_replicas,
            after_replicas=before_replicas,
            success=False,
            error_message="replica 수는 1 이상이어야 합니다.",
        )

    try:
        result = subprocess.run(
            [
                "docker", "compose", "up", "-d",
                # 새 replica가 healthy가 된 뒤 반환한다. 스케일 직후 곧바로 재측정(Locust)이
                # 시작되므로, 기동 중인 replica가 재측정 초반에 섞이지 않게 한다.
                "--wait",
                "--scale", f"{SERVICE_NAME}={target_replicas}",
                "--no-recreate",
                # 대상을 target-server로 한정 — --wait가 다른 서비스(cadvisor 등) 상태로 실패하지 않게 한다.
                SERVICE_NAME,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ScalingResult(
            before_replicas=before_replicas,
            after_replicas=before_replicas,
            success=False,
            error_message="스케일링 명령이 시간 초과되었습니다.",
        )
    except FileNotFoundError:
        return ScalingResult(
            before_replicas=before_replicas,
            after_replicas=before_replicas,
            success=False,
            error_message="docker 명령을 찾을 수 없습니다. Docker가 설치/실행 중인지 확인하세요.",
        )

    if result.returncode != 0:
        return ScalingResult(
            before_replicas=before_replicas,
            after_replicas=before_replicas,
            success=False,
            error_message=result.stderr.strip()[:300],
        )

    after_replicas = _get_current_replica_count()

    return ScalingResult(
        before_replicas=before_replicas,
        after_replicas=after_replicas,
        success=True,
    )
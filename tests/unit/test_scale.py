"""
test_scale.py — scale_service Tool 단위 테스트.

실제 Docker 없이 subprocess.run을 Mock으로 교체해서 테스트한다.

_get_current_replica_count()는 내부에서 subprocess.run을 2번 호출한다.
  1차: docker compose ps --services --filter status=running
  2차: docker compose ps target-server -q  (컨테이너 ID 목록)
테스트에서는 이 2번 호출을 순서대로 응답 목록에 포함시켜야 한다.

테스트 4가지:
  1. 정상 스케일링 → before=1, after=3, success=True
  2. 상한(8개) 초과 → 거부, success=False
  3. 1개 미만 요청 → 거부, success=False
  4. Docker 명령 실패 → success=False, 에러 메시지 확인
"""

import pytest
from unittest.mock import patch, MagicMock

import app.tools.scale_service as ss_module
from app.schemas import ScalingResult


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _make_run_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """subprocess.run 반환값 Mock."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


def _ps_sequence(count: int) -> list:
    """
    _get_current_replica_count() 내부의 2번 subprocess.run 호출에 대한 응답 시퀀스.
      1차 호출(--services): returncode=0, stdout 불필요
      2차 호출(-q):         컨테이너 ID를 count 줄만큼 반환
    """
    ids = "\n".join([f"container_id_{i}" for i in range(count)])
    return [
        _make_run_result(returncode=0, stdout="target-server"),  # 1차: --services
        _make_run_result(returncode=0, stdout=ids),              # 2차: -q
    ]


# ── 테스트 1: 정상 스케일링 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scale_service_normal():
    """
    현재 replica 1개 → 3개로 스케일링.
    성공 시 before=1, after=3, success=True.
    """
    run_results = iter([
        *_ps_sequence(1),                        # before: replica 1개
        _make_run_result(returncode=0),          # docker compose up --scale
        *_ps_sequence(3),                        # after: replica 3개
    ])

    with patch.object(ss_module.subprocess, "run", side_effect=run_results):
        result = await ss_module.scale_service(target_replicas=3)

    assert isinstance(result, ScalingResult)
    assert result.before_replicas == 1
    assert result.after_replicas == 3
    assert result.success is True
    assert result.error_message is None


@pytest.mark.asyncio
async def test_scale_service_up_command_waits_for_target_server_only():
    """
    docker compose up 명령에 --wait(새 replica healthy까지 대기)와 --no-recreate가 들어가고,
    대상 서비스가 target-server로 한정된다.
    """
    run_results = iter([
        *_ps_sequence(1),
        _make_run_result(returncode=0),
        *_ps_sequence(3),
    ])

    with patch.object(ss_module.subprocess, "run", side_effect=run_results) as mock_run:
        await ss_module.scale_service(target_replicas=3)

    up_cmd = mock_run.call_args_list[2].args[0]   # ps 2번 다음이 up 호출
    assert up_cmd[:4] == ["docker", "compose", "up", "-d"]
    assert "--wait" in up_cmd
    assert "--no-recreate" in up_cmd
    assert f"{ss_module.SERVICE_NAME}=3" in up_cmd
    assert up_cmd[-1] == ss_module.SERVICE_NAME


# ── 테스트 2: 상한 초과 거부 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scale_service_exceeds_max():
    """
    SCALE_MAX_REPLICAS(8) 초과 → 거부, docker compose up 실행 안 됨.
    """
    run_results = iter([
        *_ps_sequence(2),   # before count만 조회되고 이후 명령은 차단
    ])

    with patch.object(ss_module.subprocess, "run", side_effect=run_results):
        result = await ss_module.scale_service(target_replicas=9)

    assert result.success is False
    assert "상한" in result.error_message
    assert result.before_replicas == 2
    assert result.after_replicas == 2   # 스케일링 안 했으니 그대로


# ── 테스트 3: 1개 미만 거부 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scale_service_below_minimum():
    """replica=0 요청 → 거부, success=False."""
    run_results = iter([
        *_ps_sequence(2),
    ])

    with patch.object(ss_module.subprocess, "run", side_effect=run_results):
        result = await ss_module.scale_service(target_replicas=0)

    assert result.success is False
    assert "1 이상" in result.error_message


# ── 테스트 4: Docker 명령 실패 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scale_service_docker_command_fails():
    """
    docker compose up 명령이 returncode=1로 실패.
    success=False, error_message에 stderr 내용 포함.
    """
    run_results = iter([
        *_ps_sequence(2),                                              # before
        _make_run_result(returncode=1, stderr="no such service"),      # 명령 실패
    ])

    with patch.object(ss_module.subprocess, "run", side_effect=run_results):
        result = await ss_module.scale_service(target_replicas=4)

    assert result.success is False
    assert "no such service" in result.error_message


# ── 테스트 5: 공개 헬퍼 get_current_replicas ──────────────────────────────────

def test_get_current_replicas_returns_count():
    """
    get_current_replicas()가 현재 실행 중인 replica 수를 반환한다.
    (박정기 engine이 프롬프트의 current_replicas 값을 얻는 통합 API)
    """
    run_results = iter(_ps_sequence(3))

    with patch.object(ss_module.subprocess, "run", side_effect=run_results):
        count = ss_module.get_current_replicas()

    assert count == 3


def test_get_current_replicas_zero_when_docker_down():
    """Docker가 꺼져 있으면(FileNotFoundError) 0을 반환한다 (예외로 죽지 않음)."""
    with patch.object(ss_module.subprocess, "run", side_effect=FileNotFoundError):
        count = ss_module.get_current_replicas()

    assert count == 0
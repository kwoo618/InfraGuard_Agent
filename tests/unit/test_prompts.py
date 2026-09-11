"""
test_prompts.py — 병목 진단 / 재검증 프롬프트 빌더 단위 테스트 (docs/02 ISSUE-10).

확인하는 것:
  1. CPU/메모리 미수집 환경에서 0.0이 측정값처럼 들어가지 않는지
  2. P95 SLO(P95_SLO_MS) 기준이 프롬프트에 들어가는지
  3. 활성 연결 수(connection_count) 설명이 들어가는지
  4. "무조건 스케일링하지 않는다" 원칙이 유지되는지

LLM / Docker / Prometheus는 호출하지 않는다. 아래 수치는 빌더 입력용 테스트 값이다.
"""

import pytest

from app.agent.prompts import (
    DEFAULT_P95_SLO_MS,
    SYSTEM_PROMPT,
    UNMEASURED_TEXT,
    build_bottleneck_analysis_prompt,
    build_revalidation_prompt,
    get_p95_slo_ms,
)
from app.schemas import LoadTestResult, SystemMetrics


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _load_test_result() -> LoadTestResult:
    return LoadTestResult(
        tps=40.0,
        latency_p95=1500.0,
        latency_avg=600.0,
        error_rate=0.01,
        duration=10,
        total_requests=400,
    )


def _system_metrics(cpu_pct: float = 0.0, mem_pct: float = 0.0) -> SystemMetrics:
    return SystemMetrics(
        cpu_pct=cpu_pct,
        mem_pct=mem_pct,
        connection_count=12,
    )


def _bottleneck_prompt(
    system_metrics: SystemMetrics | None = None,
    **kwargs,
) -> str:
    return build_bottleneck_analysis_prompt(
        target_tps=50,
        load_test_result=_load_test_result(),
        system_metrics=system_metrics or _system_metrics(),
        service_name="target-server",
        current_replicas=1,
        **kwargs,
    )


def _revalidation_prompt(
    system_metrics: SystemMetrics | None = None,
    **kwargs,
) -> str:
    metrics = system_metrics or _system_metrics()
    return build_revalidation_prompt(
        previous_load_test_result=_load_test_result(),
        current_load_test_result=_load_test_result(),
        previous_system_metrics=metrics,
        current_system_metrics=metrics,
        **kwargs,
    )


# ── 1. 미수집 메트릭 표시 ──────────────────────────────────────────────────────

def test_bottleneck_prompt_marks_unmeasured_metrics():
    prompt = _bottleneck_prompt(resource_metrics_collected=False, p95_slo_ms=1000)

    assert f'"cpu_pct": "{UNMEASURED_TEXT}"' in prompt
    assert f'"mem_pct": "{UNMEASURED_TEXT}"' in prompt
    assert '"cpu_pct": 0.0' not in prompt
    assert '"mem_pct": 0.0' not in prompt
    assert "CPU 또는 메모리 사용량이 과도한지" not in prompt


def test_bottleneck_prompt_keeps_values_when_collected():
    prompt = _bottleneck_prompt(
        system_metrics=_system_metrics(cpu_pct=72.5, mem_pct=40.0),
        resource_metrics_collected=True,
        p95_slo_ms=1000,
    )

    assert '"cpu_pct": 72.5' in prompt
    assert '"mem_pct": 40.0' in prompt
    assert UNMEASURED_TEXT not in prompt
    assert "CPU 또는 메모리 사용량이 과도한지" in prompt


def test_bottleneck_prompt_default_follows_get_metrics_flag():
    """기본값은 get_metrics.RESOURCE_METRICS_COLLECTED(현재 False)를 따른다."""
    prompt = _bottleneck_prompt(p95_slo_ms=1000)

    assert UNMEASURED_TEXT in prompt
    assert '"cpu_pct": 0.0' not in prompt


# ── 2. P95 SLO ────────────────────────────────────────────────────────────────

def test_p95_slo_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("P95_SLO_MS", raising=False)

    assert get_p95_slo_ms() == DEFAULT_P95_SLO_MS == 1000
    assert f"SLO: {DEFAULT_P95_SLO_MS}ms 이하" in _bottleneck_prompt()


def test_p95_slo_read_from_env_at_call_time(monkeypatch):
    monkeypatch.setenv("P95_SLO_MS", "800")

    prompt = _bottleneck_prompt()

    assert "SLO: 800ms 이하" in prompt
    assert "SLO(800ms)를 충족하는지" in prompt


@pytest.mark.parametrize("raw_value", ["abc", "0", "-5", "1.5"])
def test_p95_slo_invalid_env_raises(monkeypatch, raw_value):
    monkeypatch.setenv("P95_SLO_MS", raw_value)

    with pytest.raises(ValueError):
        get_p95_slo_ms()

    with pytest.raises(ValueError):
        _bottleneck_prompt()


def test_p95_slo_invalid_argument_raises():
    with pytest.raises(ValueError):
        _bottleneck_prompt(p95_slo_ms=0)


# ── 3. 활성 연결 수 설명 ──────────────────────────────────────────────────────

def test_bottleneck_prompt_describes_connection_count():
    prompt = _bottleneck_prompt(p95_slo_ms=1000)

    assert "[메트릭 설명]" in prompt
    assert "처리 중이거나" in prompt
    assert "처리를 기다리는 동시 요청 수의 합" in prompt
    assert "순간값" in prompt


# ── 재검증 프롬프트 ───────────────────────────────────────────────────────────

def test_revalidation_prompt_marks_unmeasured_metrics():
    prompt = _revalidation_prompt(resource_metrics_collected=False, p95_slo_ms=1000)

    assert f'"cpu_pct": "{UNMEASURED_TEXT}"' in prompt
    assert '"cpu_pct": 0.0' not in prompt
    assert '"cpu_pct_change": null' in prompt
    assert '"mem_pct_change": null' in prompt
    assert "- CPU 사용률 변화" not in prompt
    assert "SLO(1000ms)를 충족하는지" in prompt
    assert "처리를 기다리는 동시 요청 수의 합" in prompt


def test_revalidation_prompt_keeps_values_when_collected():
    prompt = _revalidation_prompt(
        system_metrics=_system_metrics(cpu_pct=72.5, mem_pct=40.0),
        resource_metrics_collected=True,
        p95_slo_ms=1000,
    )

    assert '"cpu_pct": 72.5' in prompt
    assert '"cpu_pct_change": 0.0' in prompt
    assert "- CPU 사용률 변화" in prompt
    assert UNMEASURED_TEXT not in prompt


# ── 4. 스케일링 원칙 유지 ─────────────────────────────────────────────────────

def test_system_prompt_keeps_no_forced_scaling_principle():
    assert "무조건 스케일링을 제안하지 않습니다." in SYSTEM_PROMPT
    assert '"측정 불가"로 표시된 항목은 판단 근거로 사용하지 않으며' in SYSTEM_PROMPT

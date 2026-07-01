"""
역할 간 데이터 교환 계약 (Interface Contract)

이 파일의 dataclass는 4명의 작업이 맞물리는 지점이다.
구조 변경 시 반드시 팀 전체에 공유하고 CONTRIBUTING.md도 함께 수정한다.

소유권:
- LoadTestResult   : 이하은이 생성 (run_load_test.py) → 박정기가 소비 (agent/nodes.py)
- SystemMetrics    : 최강우가 생성 (get_metrics.py)    → 박정기가 소비 (agent/nodes.py)
- BottleneckReport : 박정기가 생성 (agent/nodes.py)     → 최소명이 소비 (api/v1/agent.py)
- ScalingResult    : 최강우가 생성 (scale_service.py)   → 최소명이 소비 (api/v1/agent.py)
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class LoadTestResult:
    """이하은 - run_load_test Tool의 반환값"""
    tps: float
    latency_p95: float       # ms
    latency_avg: float       # ms
    error_rate: float        # 0.0 ~ 1.0
    duration: int            # 초
    total_requests: int


@dataclass
class SystemMetrics:
    """최강우 - get_metrics Tool의 반환값"""
    cpu_pct: float
    mem_pct: float
    connection_count: int
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class BottleneckReport:
    """박정기 - LLM 진단 결과"""
    cause: str
    severity: str             # "low" | "medium" | "high"
    recommendation: str
    confidence: float         # 0.0 ~ 1.0
    requires_scaling: bool = False


@dataclass
class ScalingResult:
    """최강우 - scale_service Tool의 반환값"""
    before_replicas: int
    after_replicas: int
    success: bool
    error_message: str | None = None
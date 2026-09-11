import json
import os
from dataclasses import asdict

from app.schemas import LoadTestResult, SystemMetrics
from app.tools.get_metrics import RESOURCE_METRICS_COLLECTED

# 수집되지 않는 메트릭(cpu_pct, mem_pct) 자리에 숫자 대신 넣는 문구.
# get_metrics.py가 0.0을 고정 반환하는 환경에서 0.0이 측정값처럼
# LLM에 전달되지 않도록 한다. (docs/02_알려진이슈.md ISSUE-10)
UNMEASURED_TEXT = "측정 불가 — 판단 근거로 사용하지 말 것"

# P95 응답 시간 SLO 기본값(ms). 측정 전에 정한 값이며 결과를 보고 조정하지 않는다.
# 근거 (docs/02_알려진이슈.md ISSUE-10):
# - generate_plan.py가 P95 >= 1000ms를 "높음"으로 분류하는 기존 기준과 같다.
# - target-server /heavy 처리시간 설계값(0.2~0.6초)으로 추정한 대기 없는 P95(약 530ms)는
#   이 값을 충족하고, 대기열이 쌓이면 위반한다. (설계값 계산이며 실측이 아니다)
DEFAULT_P95_SLO_MS = 1000

# 이 프롬프트는 모든 병목 분석 요청에 공통으로 사용한다.
# 사용자가 입력한 데이터와 관계없이 LLM이 항상 지켜야 하는
# 역할, 판단 기준, 출력 규칙을 작성한다.
SYSTEM_PROMPT = """
당신은 클라우드 인프라와 DevOps 환경을 분석하는
InfraGuard Agent입니다.

당신의 역할은 Locust 부하 테스트 결과와 Prometheus 시스템
메트릭을 함께 분석하여 서버의 성능 병목 원인을 진단하는 것입니다.

분석할 때 다음 원칙을 반드시 지켜야 합니다.

1. TPS, 응답 지연 시간, 오류율, 활성 연결 수, CPU 사용률,
   메모리 사용률 등 제공된 측정값을 근거로 판단합니다.

2. CPU 사용률 하나만으로 병목을 판단하지 않습니다.
   부하 테스트 결과와 시스템 메트릭을 종합적으로 분석합니다.

3. "측정 불가"로 표시된 항목은 판단 근거로 사용하지 않으며,
   그 항목을 근거로 병목이 없다고 결론짓지 않습니다.

4. 병목 원인을 확실하게 판단하기 어려운 경우에는
   추측하지 말고 불확실하다고 표시합니다.

5. 무조건 스케일링을 제안하지 않습니다.
   실제로 성능 개선이 필요하다고 판단될 때만 제안합니다.

6. 스케일링이 필요한 경우에는 대상 서비스, 현재 컨테이너 수,
   목표 컨테이너 수와 판단 근거를 제공합니다.

7. 사용자의 승인 없이 직접 인프라를 변경하지 않습니다.

8. severity는 반드시 low, medium, high 중 하나를 사용합니다.

9. confidence는 반드시 0.0부터 1.0 사이의 숫자로 작성합니다.

10. 응답은 반드시 요청된 JSON 형식으로만 반환합니다.

11. 응답은 반드시 요청된 JSON 형식으로만 반환합니다.
   JSON 외의 설명이나 마크다운 문법은 포함하지 않습니다.
""".strip()


def get_p95_slo_ms() -> int:
    """
    P95_SLO_MS 환경변수를 읽어 P95 응답 시간 SLO(ms)를 반환한다.

    nodes.py가 이 모듈을 import한 뒤에 load_dotenv()를 호출하므로
    모듈 로드 시점이 아니라 호출 시점에 읽는다.

    Raises
    ------
    ValueError
        값이 정수가 아니거나 1 미만인 경우.
        잘못된 설정을 기본값으로 조용히 대체하지 않는다.
    """

    raw_value = os.getenv("P95_SLO_MS", "").strip()

    if not raw_value:
        return DEFAULT_P95_SLO_MS

    try:
        slo_ms = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"P95_SLO_MS는 정수(ms)여야 합니다: {raw_value!r}"
        ) from exc

    if slo_ms < 1:
        raise ValueError(
            f"P95_SLO_MS는 1 이상이어야 합니다: {slo_ms}"
        )

    return slo_ms


def _resolve_p95_slo_ms(p95_slo_ms: int | None) -> int:
    """인자로 받은 SLO가 없으면 환경변수(기본값 포함)에서 읽는다."""

    if p95_slo_ms is None:
        return get_p95_slo_ms()

    if p95_slo_ms < 1:
        raise ValueError(
            f"p95_slo_ms는 1 이상이어야 합니다: {p95_slo_ms}"
        )

    return p95_slo_ms


def _serialize_system_metrics(
    system_metrics: SystemMetrics,
    resource_metrics_collected: bool,
) -> str:
    """
    SystemMetrics를 프롬프트용 JSON 문자열로 만든다.

    CPU/메모리가 수집되지 않는 환경이면 0.0 대신 UNMEASURED_TEXT를 넣는다.
    """

    metrics = asdict(system_metrics)

    if not resource_metrics_collected:
        metrics["cpu_pct"] = UNMEASURED_TEXT
        metrics["mem_pct"] = UNMEASURED_TEXT

    return json.dumps(
        metrics,
        ensure_ascii=False,
        indent=2,
    )


def _build_metrics_description(
    resource_metrics_collected: bool,
) -> str:
    """시스템 메트릭 각 필드가 무엇을 뜻하는지 설명하는 문단을 만든다."""

    lines = [
        "- connection_count:",
        "  target-server 전체 replica에 들어와 처리 중이거나",
        "  처리를 기다리는 동시 요청 수의 합입니다.",
        "  부하 테스트가 끝난 직후 조회한 순간값(수집 주기 5초)이며,",
        "  부하 테스트 구간 전체의 평균이 아닙니다.",
    ]

    if resource_metrics_collected:
        lines.extend(
            [
                "- cpu_pct, mem_pct:",
                "  CPU 사용률과 메모리 사용률(%)입니다.",
            ]
        )
    else:
        lines.extend(
            [
                "- cpu_pct, mem_pct:",
                "  이 환경에서는 수집되지 않습니다.",
                f'  값 자리에 "{UNMEASURED_TEXT}" 문구가 들어 있으며,',
                "  CPU·메모리의 여유나 과부하를 판단 근거로 사용하지 마세요.",
            ]
        )

    return "\n".join(lines)


def build_bottleneck_analysis_prompt(
    target_tps: int,
    load_test_result: LoadTestResult,
    system_metrics: SystemMetrics,
    service_name: str,
    current_replicas: int,
    *,
    resource_metrics_collected: bool = RESOURCE_METRICS_COLLECTED,
    p95_slo_ms: int | None = None,
) -> str:
    """
    Locust 부하 테스트 결과와 Prometheus 시스템 메트릭을
    Solar Pro가 분석할 수 있는 프롬프트로 변환한다.

    Parameters
    ----------
    target_tps:
        동시 가상 사용자 수 (run_load_test가 Locust --users로 넘기는 값).
        이름과 달리 처리량(TPS) 목표가 아니므로 LLM에도 동시 사용자 수로 전달한다
        (docs/02_알려진이슈.md ISSUE-11, #88).

    load_test_result:
        schemas.py의 LoadTestResult 객체.

    system_metrics:
        schemas.py의 SystemMetrics 객체.

    service_name:
        스케일링 대상 Docker Compose 서비스 이름.

    current_replicas:
        현재 실행 중인 서비스 컨테이너 수.

    resource_metrics_collected:
        cpu_pct / mem_pct를 실제로 수집하는지 여부.
        False면 두 값을 "측정 불가"로 표시한다.
        기본값은 get_metrics.RESOURCE_METRICS_COLLECTED.

    p95_slo_ms:
        P95 응답 시간 SLO(ms). None이면 P95_SLO_MS 환경변수
        (없으면 DEFAULT_P95_SLO_MS)를 사용한다.
    Returns
    -------
    str
        LLM에 전달할 병목 분석 프롬프트.

    Raises
    ------
    ValueError
        target_tps가 1 미만이거나 SLO 값이 잘못된 경우.
    """

    if target_tps <= 0:
        raise ValueError("target_tps는 1 이상이어야 합니다.")

    slo_ms = _resolve_p95_slo_ms(p95_slo_ms)

    load_test_json = json.dumps(
        asdict(load_test_result),
        ensure_ascii=False,
        indent=2,
    )

    system_metrics_json = _serialize_system_metrics(
        system_metrics,
        resource_metrics_collected,
    )

    metrics_description = _build_metrics_description(
        resource_metrics_collected
    )

    # 처리량을 동시 사용자 수와 비교하는 항목은 두지 않는다 (#88).
    # 동시 사용자 수는 부하 조건이지 처리량 목표가 아니다.
    analysis_items = [
        f"- P95 응답 시간이 SLO({slo_ms}ms)를 충족하는지, 평균 응답 시간은 어떤지",
        "- 오류율이 허용 가능한 수준인지",
    ]

    if resource_metrics_collected:
        analysis_items.append("- CPU 또는 메모리 사용량이 과도한지")

    analysis_items.extend(
        [
            "- 활성 연결 수가 병목에 영향을 주는지",
            "- 현재 문제가 일시적인 트래픽 증가인지 구조적인 병목인지",
            "- 컨테이너 스케일링이 필요한지",
        ]
    )

    analysis_text = "\n".join(analysis_items)

    # 실제 측정 데이터와 LLM이 반환해야 할 JSON 구조를 함께 전달한다.
    return f"""
다음 Locust 부하 테스트 결과와 Prometheus 시스템 메트릭을
종합적으로 분석하여 서버의 성능 병목을 진단하세요.

[부하 조건]

동시 가상 사용자 {target_tps}명 (Locust 동시 접속 수, 처리량 목표 아님)
처리량(TPS)은 아래 Locust 결과의 tps(측정값)입니다.
동시 가상 사용자 수와 처리량을 비교해 달성 여부를 판단하지 마세요.

[판단 기준]

- P95 응답 시간 SLO: {slo_ms}ms 이하
  (Locust 결과의 latency_p95, 전체 요청 기준)
- latency_p95가 SLO를 넘으면 SLO 위반입니다.
  위반했다면 그 원인을 측정값으로 설명하고,
  컨테이너 수를 늘려 해소될 수 있는 병목인지 판단하세요.

[스케일링 대상 서비스]

서비스 이름 : {service_name}
현재 컨테이너 수 : {current_replicas}

[Locust 부하 테스트 결과]

{load_test_json}

[Prometheus 시스템 메트릭]

{system_metrics_json}

[메트릭 설명]

{metrics_description}

다음 내용을 분석하세요.

{analysis_text}

반드시 다음 JSON 구조로만 응답하세요.

{{
  "bottleneck_report": {{
    "cause": "병목 원인 또는 병목이 없다는 설명",
    "severity": "low",
    "recommendation": "권장 조치",
    "confidence": 0.9,
    "requires_scaling": false
  }},
  "agent_outcome": "diagnosed",
  "scaling_plan": null
}}

각 필드의 작성 기준은 다음과 같습니다.

- bottleneck_report.cause:
  병목 원인을 구체적인 측정값을 근거로 설명합니다.
  병목이 없다면 정상이라고 판단한 근거를 작성합니다.
  "측정 불가"로 표시된 항목은 근거로 쓰지 않습니다.

- bottleneck_report.severity:
  반드시 "low", "medium", "high" 중 하나를 사용합니다.

- bottleneck_report.recommendation:
  병목 해소를 위해 권장하는 조치를 작성합니다.

- bottleneck_report.confidence:
  진단 신뢰도를 0.0부터 1.0 사이의 숫자로 작성합니다.

- bottleneck_report.requires_scaling:
  컨테이너 확장이 필요하면 true,
  필요하지 않으면 false를 반환합니다.

- agent_outcome:
  스케일링이 필요하지 않으면 "diagnosed",
  스케일링 승인 대기 상태이면 "awaiting_approval",
  분석 또는 응답 생성에 실패했다면 "failed"를 반환합니다.

  만약에 bottleneck_report.requires_scaling이 true이면
  반드시 "awaiting_approval"을 반환합니다.

  bottleneck_report.requires_scaling이 false이고
  진단이 정상적으로 완료되었다면 반드시 "diagnosed"를 반환합니다.

- scaling_plan:
  bottleneck_report.requires_scaling이 true인 경우
  반드시 다음 구조로 작성합니다.
  {{
    "service_name": "{service_name}",
    "current_replicas": {current_replicas},
    "desired_replicas": {2 * current_replicas},
    "reason": "스케일링이 필요한 구체적인 측정 근거"
  }}

  service_name은 반드시 "{service_name}"을 사용합니다.

  current_replicas는 반드시 {current_replicas}을 사용합니다.

  desired_replicas는 current_replicas보다 큰 정수여야 합니다.

  bottleneck_report.requires_scaling이 false인 경우
  scaling_plan은 반드시 null이어야 합니다.

  다음 관계를 반드시 지키세요.

- requires_scaling이 true이면:
  agent_outcome은 "awaiting_approval"
  scaling_plan은 null이 아닌 객체

- requires_scaling이 false이면:
  agent_outcome은 "diagnosed"
  scaling_plan은 null

JSON 외의 설명, 코드 블록, 마크다운 문법은 절대 포함하지 마세요.
""".strip()


def build_revalidation_prompt(
    previous_load_test_result: LoadTestResult,
    current_load_test_result: LoadTestResult,
    previous_system_metrics: SystemMetrics,
    current_system_metrics: SystemMetrics,
    *,
    resource_metrics_collected: bool = RESOURCE_METRICS_COLLECTED,
    p95_slo_ms: int | None = None,
) -> str:
    """
    스케일링 전후의 성능 데이터를 비교하기 위한 프롬프트를 생성한다.

    스케일링 실행 후 성능이 실제로 개선되었는지 판단할 때 사용한다.

    Parameters
    ----------
    previous_load_test_result:
        스케일링 전 Locust 부하 테스트 결과.

    current_load_test_result:
        스케일링 후 Locust 부하 테스트 결과.

    previous_system_metrics:
        스케일링 전 Prometheus 시스템 메트릭.

    current_system_metrics:
        스케일링 후 Prometheus 시스템 메트릭.

    resource_metrics_collected:
        cpu_pct / mem_pct를 실제로 수집하는지 여부.
        False면 두 값을 "측정 불가"로 표시하고 변화량을 null로 받는다.

    p95_slo_ms:
        P95 응답 시간 SLO(ms). None이면 P95_SLO_MS 환경변수
        (없으면 DEFAULT_P95_SLO_MS)를 사용한다.

    Returns
    -------
    str
        스케일링 전후 성능 비교를 위한 프롬프트.
    """

    slo_ms = _resolve_p95_slo_ms(p95_slo_ms)

    previous_load_test_json = json.dumps(
        asdict(previous_load_test_result),
        ensure_ascii=False,
        indent=2,
    )

    current_load_test_json = json.dumps(
        asdict(current_load_test_result),
        ensure_ascii=False,
        indent=2,
    )

    previous_metrics_json = _serialize_system_metrics(
        previous_system_metrics,
        resource_metrics_collected,
    )

    current_metrics_json = _serialize_system_metrics(
        current_system_metrics,
        resource_metrics_collected,
    )

    metrics_description = _build_metrics_description(
        resource_metrics_collected
    )

    comparison_items = [
        "- 실제 처리 TPS 변화",
        "- 평균 응답 시간 변화",
        "- P95 응답 시간 변화",
        f"- 스케일링 후 P95 응답 시간이 SLO({slo_ms}ms)를 충족하는지",
        "- 오류율 변화",
    ]

    if resource_metrics_collected:
        comparison_items.extend(
            [
                "- CPU 사용률 변화",
                "- 메모리 사용률 변화",
            ]
        )

    comparison_items.extend(
        [
            "- 활성 연결 수 변화",
            "- 스케일링 조치의 전체적인 효과",
        ]
    )

    comparison_text = "\n".join(comparison_items)

    if resource_metrics_collected:
        resource_change_template = "0.0"
        cpu_change_rule = (
            "스케일링 후 CPU 사용률에서\n"
            "  스케일링 전 CPU 사용률을 뺀 값입니다."
        )
        mem_change_rule = (
            "스케일링 후 메모리 사용률에서\n"
            "  스케일링 전 메모리 사용률을 뺀 값입니다."
        )
    else:
        resource_change_template = "null"
        cpu_change_rule = (
            "CPU 사용률은 측정 불가이므로\n"
            "  반드시 null을 반환합니다."
        )
        mem_change_rule = (
            "메모리 사용률은 측정 불가이므로\n"
            "  반드시 null을 반환합니다."
        )

    return f"""
다음은 컨테이너 스케일링 전후의 성능 측정 결과입니다.

각 데이터를 비교하여 스케일링으로 성능이 개선되었는지 분석하세요.

[판단 기준]

- P95 응답 시간 SLO: {slo_ms}ms 이하
  (Locust 결과의 latency_p95, 전체 요청 기준)

[스케일링 전 Locust 결과]

{previous_load_test_json}

[스케일링 후 Locust 결과]

{current_load_test_json}

[스케일링 전 시스템 메트릭]

{previous_metrics_json}

[스케일링 후 시스템 메트릭]

{current_metrics_json}

[메트릭 설명]

{metrics_description}

다음 항목을 비교하세요.

{comparison_text}

모든 변화량은 반드시 다음 기준으로 계산하세요.

변화량 = 스케일링 후 값 - 스케일링 전 값

예시는 다음과 같습니다.

- TPS가 30에서 45로 증가했다면
  tps_change는 15.0입니다.

- 평균 응답 시간이 500ms에서 300ms로 감소했다면
  latency_avg_change는 -200.0입니다.

- P95 응답 시간이 1000ms에서 600ms로 감소했다면
  latency_p95_change는 -400.0입니다.

- 오류율이 0.05에서 0.01로 감소했다면
  error_rate_change는 -0.04입니다.

반드시 다음 JSON 구조로만 응답하세요.

{{
  "summary": "스케일링 전후 비교 결과",
  "performance_improved": true,
  "tps_change": 0.0,
  "latency_avg_change": 0.0,
  "latency_p95_change": 0.0,
  "error_rate_change": 0.0,
  "cpu_pct_change": {resource_change_template},
  "mem_pct_change": {resource_change_template},
  "connection_count_change": 0,
  "additional_action_required": false,
  "recommended_action": null
}}

각 필드의 작성 기준은 다음과 같습니다.

- summary:
  스케일링 전후의 주요 변화를 측정값을 근거로 요약합니다.
  "측정 불가"로 표시된 항목은 근거로 쓰지 않습니다.

- performance_improved:
  TPS, 지연 시간, 오류율 및 시스템 메트릭을 종합적으로 판단하여
  성능이 개선되었다면 true를 반환합니다.

- tps_change:
  스케일링 후 TPS에서 스케일링 전 TPS를 뺀 값입니다.

- latency_avg_change:
  스케일링 후 평균 응답 시간에서
  스케일링 전 평균 응답 시간을 뺀 값입니다.

- latency_p95_change:
  스케일링 후 P95 응답 시간에서
  스케일링 전 P95 응답 시간을 뺀 값입니다.

- error_rate_change:
  스케일링 후 오류율에서 스케일링 전 오류율을 뺀 값입니다.

- cpu_pct_change:
  {cpu_change_rule}

- mem_pct_change:
  {mem_change_rule}

- connection_count_change:
  스케일링 후 활성 연결 수에서
  스케일링 전 활성 연결 수를 뺀 값입니다.

- additional_action_required:
  스케일링 후에도 P95 응답 시간이 SLO({slo_ms}ms)를 충족하지 못했거나
  다른 병목이 남아 있다면 true를 반환합니다.

- recommended_action:
  추가 조치가 필요하면 구체적인 권장 조치를 작성하고,
  필요하지 않으면 null을 반환합니다.

JSON 외의 설명, 코드 블록, 마크다운 문법은 포함하지 마세요.
""".strip()

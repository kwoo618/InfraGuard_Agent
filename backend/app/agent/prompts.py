import json
from dataclasses import asdict

from app.schemas import LoadTestResult, SystemMetrics

# 이 프롬프트는 모든 병목 분석 요청에 공통으로 사용한다.
# 사용자가 입력한 데이터와 관계없이 LLM이 항상 지켜야 하는
# 역할, 판단 기준, 출력 규칙을 작성한다.
SYSTEM_PROMPT = """
당신은 클라우드 인프라와 DevOps 환경을 분석하는
InfraGuard Agent입니다.

당신의 역할은 Locust 부하 테스트 결과와 Prometheus 시스템
메트릭을 함께 분석하여 서버의 성능 병목 원인을 진단하는 것입니다.

분석할 때 다음 원칙을 반드시 지켜야 합니다.

1. TPS, 응답 지연 시간, 오류율, CPU 사용률, 메모리 사용률 등의
   수치 데이터를 근거로 판단합니다.

2. CPU 사용률 하나만으로 병목을 판단하지 않습니다.
   부하 테스트 결과와 시스템 메트릭을 종합적으로 분석합니다.

3. 병목 원인을 확실하게 판단하기 어려운 경우에는
   추측하지 말고 불확실하다고 표시합니다.

4. 무조건 스케일링을 제안하지 않습니다.
   실제로 성능 개선이 필요하다고 판단될 때만 제안합니다.

5. 스케일링이 필요한 경우에는 대상 서비스, 현재 컨테이너 수,
   목표 컨테이너 수와 판단 근거를 제공합니다.

6. 사용자의 승인 없이 직접 인프라를 변경하지 않습니다.

7. severity는 반드시 low, medium, high 중 하나를 사용합니다.

8. confidence는 반드시 0.0부터 1.0 사이의 숫자로 작성합니다.

9. 응답은 반드시 요청된 JSON 형식으로만 반환합니다.

10. 응답은 반드시 요청된 JSON 형식으로만 반환합니다.
   JSON 외의 설명이나 마크다운 문법은 포함하지 않습니다.
""".strip()


def build_bottleneck_analysis_prompt(
    target_tps: int,
    load_test_result: LoadTestResult,
    system_metrics: SystemMetrics,
    service_name: str,
    current_replicas: int,
) -> str:
    """
    Locust 부하 테스트 결과와 Prometheus 시스템 메트릭을
    Solar Pro가 분석할 수 있는 프롬프트로 변환한다.

    Parameters
    ----------
    target_tps:
        사용자가 요청한 목표 TPS.

    load_test_result:
        schemas.py의 LoadTestResult 객체.

    system_metrics:
        schemas.py의 SystemMetrics 객체.

    service_name:
        스케일링 대상 Docker Compose 서비스 이름.

    current_replicas:
        현재 실행 중인 서비스 컨테이너 수.
    Returns
    -------
    str
        LLM에 전달할 병목 분석 프롬프트.

    Raises
    ------
    ValueError
        target_tps가 1 이하일 경우.
    """

    if target_tps <= 0:
        raise ValueError("target_tps는 1 이상이어야 합니다.")

    load_test_json = json.dumps(
        asdict(load_test_result),
        ensure_ascii=False,
        indent=2,
    )

    system_metrics_json = json.dumps(
        asdict(system_metrics),
        ensure_ascii=False,
        indent=2,
    )

    # 실제 측정 데이터와 LLM이 반환해야 할 JSON 구조를 함께 전달한다.
    return f"""
다음 Locust 부하 테스트 결과와 Prometheus 시스템 메트릭을
종합적으로 분석하여 서버의 성능 병목을 진단하세요.

[사용자가 요청한 목표 TPS]

{target_tps}

[스케일링 대상 서비스]

서비스 이름 : {service_name}
현재 컨테이너 수 : {current_replicas}

[Locust 부하 테스트 결과]

{load_test_json}

[Prometheus 시스템 메트릭]

{system_metrics_json}

다음 내용을 분석하세요.

- 목표 TPS를 실제로 달성했는지
- 평균 응답 시간과 P95 응답 시간이 적절한지
- 오류율이 허용 가능한 수준인지
- CPU 또는 메모리 사용량이 과도한지
- 활성 연결 수가 병목에 영향을 주는지
- 현재 문제가 일시적인 트래픽 증가인지 구조적인 병목인지
- 컨테이너 스케일링이 필요한지

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

    Returns
    -------
    str
        스케일링 전후 성능 비교를 위한 프롬프트.
    """

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

    previous_metrics_json = json.dumps(
        asdict(previous_system_metrics),
        ensure_ascii=False,
        indent=2,
    )

    current_metrics_json = json.dumps(
        asdict(current_system_metrics),
        ensure_ascii=False,
        indent=2,
    )

    return f"""
다음은 컨테이너 스케일링 전후의 성능 측정 결과입니다.

각 데이터를 비교하여 스케일링으로 성능이 개선되었는지 분석하세요.

[스케일링 전 Locust 결과]

{previous_load_test_json}

[스케일링 후 Locust 결과]

{current_load_test_json}

[스케일링 전 시스템 메트릭]

{previous_metrics_json}

[스케일링 후 시스템 메트릭]

{current_metrics_json}

다음 항목을 비교하세요.

- 실제 처리 TPS 변화
- 평균 응답 시간 변화
- P95 응답 시간 변화
- 오류율 변화
- CPU 사용률 변화
- 메모리 사용률 변화
- 활성 연결 수 변화
- 스케일링 조치의 전체적인 효과

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
  "cpu_pct_change": 0.0,
  "mem_pct_change": 0.0,
  "connection_count_change": 0,
  "additional_action_required": false,
  "recommended_action": null
}}

각 필드의 작성 기준은 다음과 같습니다.

- summary:
  스케일링 전후의 주요 변화를 측정값을 근거로 요약합니다.

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
  스케일링 후 CPU 사용률에서
  스케일링 전 CPU 사용률을 뺀 값입니다.

- mem_pct_change:
  스케일링 후 메모리 사용률에서
  스케일링 전 메모리 사용률을 뺀 값입니다.

- connection_count_change:
  스케일링 후 활성 연결 수에서
  스케일링 전 활성 연결 수를 뺀 값입니다.

- additional_action_required:
  스케일링 후에도 목표 성능을 달성하지 못했거나
  다른 병목이 남아 있다면 true를 반환합니다.

- recommended_action:
  추가 조치가 필요하면 구체적인 권장 조치를 작성하고,
  필요하지 않으면 null을 반환합니다.

JSON 외의 설명, 코드 블록, 마크다운 문법은 포함하지 마세요.
""".strip()
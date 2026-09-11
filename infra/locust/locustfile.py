"""
locustfile.py - InfraGuard target-server에 대한 Locust 부하 시나리오.

infra/target-server/main.py 의 엔드포인트(/light, /heavy, /flaky, /health)를 대상으로
실제 트래픽 패턴을 흉내낸다. 가벼운 트래픽이 대부분이고 일부 요청이
DB connection pool을 흉내내는 /heavy로 몰리도록 가중치를 둬서
의도적으로 병목이 드러나게 만든다.

이 파일은 backend/app/tools/run_load_test.py 가 `locust -f locustfile.py ...`로
서브프로세스 실행한다. 직접 `locust --host http://localhost:8080` 명령으로도
웹 UI(localhost:8089)에서 수동 실행 가능하다.
"""

import csv
import json
import os
import time

from locust import HttpUser, between, events, task

# ---------------------------------------------------------------------
# 요청별 원시 기록 (결과 패널의 초 단위 TPS·P95 시계열용, docs/03 Phase 4 그래프)
#
# Locust stats_history.csv는 초마다 한 행을 쓰지만, 그 행의 P95는 시작부터 그 시점까지의
# 누적값이고 Requests/s는 최근 약 10초 평균이다. 초 단위 값을 직접 계산하려고
# 요청이 끝날 때마다 완료 시각·응답시간·실패 여부를 기록한다.
#
# 파일 위치는 --csv prefix 옆(<prefix>_requests.csv)이다. run_load_test.py가 이미 --csv를
# 넘기므로 Locust 명령 인자는 바뀌지 않는다. csv prefix가 없으면 기록하지 않는다.
# (infra/locust에서 수동 실행하면 locust.conf의 `csv = result` 때문에 result_requests.csv가 생긴다)
# 부하 시나리오(가중치, wait_time)에는 영향이 없다.
# ---------------------------------------------------------------------
REQUEST_LOG_SUFFIX = "_requests.csv"

# ---------------------------------------------------------------------
# 종료 시 통계 (LoadTestResult 헤드라인 값용, #85, docs/02 ISSUE-15)
#
# Locust는 _stats.csv를 1초마다 다시 쓰고 종료할 때는 파일을 닫기만 한다. 그래서 _stats.csv에는
# 부하 마지막 약 1초의 요청이 빠진다. 부하가 멈춘 뒤(test_stop, quitting) runner.stats의 최종값을
# <prefix>_final_stats.json에 쓴다. 값은 _stats.csv의 Aggregated·엔드포인트 행과 같은 계산이다
# (num_requests, num_failures, avg_response_time, total_rps, get_response_time_percentile(0.95)).
# ---------------------------------------------------------------------
FINAL_STATS_SUFFIX = "_final_stats.json"

_request_log = {"file": None, "writer": None}


def _csv_prefix(environment):
    options = environment.parsed_options
    return getattr(options, "csv_prefix", None) if options is not None else None


@events.test_start.add_listener
def _open_request_log(environment, **kwargs):
    prefix = _csv_prefix(environment)

    if not prefix:
        return

    handle = open(f"{prefix}{REQUEST_LOG_SUFFIX}", "w", newline="", encoding="utf-8")
    writer = csv.writer(handle)
    writer.writerow(["event", "time", "name", "response_time_ms", "failed"])
    # 부하 시작 시각. 초 단위 구간은 이 시각을 0초로 센다
    writer.writerow(["start", f"{time.time():.6f}", "", "", ""])
    _request_log.update(file=handle, writer=writer)


@events.request.add_listener
def _log_request(name, response_time, exception=None, **kwargs):
    writer = _request_log["writer"]

    if writer is None:
        return

    # request 이벤트는 요청이 끝났을 때 발생한다 → 지금 시각 = 완료 시각
    writer.writerow([
        "request",
        f"{time.time():.6f}",
        name,
        f"{response_time:.3f}",
        "1" if exception is not None else "0",
    ])


def _close_request_log():
    handle = _request_log["file"]

    if handle is None:
        return

    _request_log["writer"].writerow(["stop", f"{time.time():.6f}", "", "", ""])
    handle.close()
    _request_log.update(file=None, writer=None)


def _stats_fields(entry):
    """_stats.csv 한 행과 같은 계산의 최종값."""
    return {
        "name": entry.name,
        "method": entry.method or None,
        "num_requests": entry.num_requests,
        "num_failures": entry.num_failures,
        "avg_response_time": entry.avg_response_time,
        "total_rps": entry.total_rps,
        "p95_response_time": (
            entry.get_response_time_percentile(0.95) if entry.num_requests else None
        ),
    }


def _write_final_stats(environment):
    prefix = _csv_prefix(environment)
    runner = environment.runner

    if not prefix or runner is None:
        return

    stats = runner.stats
    data = {
        "source": "locust_runner_stats",
        "written_at": time.time(),
        "aggregated": _stats_fields(stats.total),
        # _stats.csv와 같은 순서 (이름, 메서드)
        "entries": [
            _stats_fields(entry)
            for entry in sorted(stats.entries.values(), key=lambda item: (item.name, item.method or ""))
        ],
    }

    # 부분 파일이 남지 않게 임시 파일에 쓴 뒤 바꾼다. 실패하면 run_load_test.py가 _stats.csv로 대신 계산하고
    # 결과 파일에 그 출처(locust_stats_csv)를 남긴다. 부하 테스트 자체는 멈추지 않는다.
    try:
        tmp_path = f"{prefix}{FINAL_STATS_SUFFIX}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, f"{prefix}{FINAL_STATS_SUFFIX}")
    except Exception:
        pass


@events.test_stop.add_listener
def _on_test_stop(environment, **kwargs):
    _close_request_log()
    _write_final_stats(environment)


@events.quitting.add_listener
def _on_quitting(environment, **kwargs):
    # test_stop이 오지 않은 종료에서도 버퍼를 비우고 닫는다. 종료 시 통계도 최종값으로 한 번 더 쓴다
    _close_request_log()
    _write_final_stats(environment)


class TargetServerUser(HttpUser):
    """target-server를 호출하는 가상 사용자(VU) 한 명을 정의하는 클래스.

    Locust는 이 클래스를 --users 옵션 수만큼 인스턴스화해서 동시에 돌린다.
    HttpUser를 상속하면 self.client(requests 세션 wrapper)가 자동 제공되고,
    요청 통계(응답시간, 성공/실패)가 Locust 내부적으로 자동 집계된다.
    """

    # 한 사용자가 task를 한 번 실행한 뒤 다음 task를 실행하기 전까지 대기하는 시간(초).
    # between(0.1, 0.5): 0.1~0.5초 사이 무작위 대기 → 너무 짧으면(=0) 모든 가상 사용자가
    # 쉴 새 없이 요청을 쏴서 --users 값과 실제 TPS가 크게 어긋나고, 너무 길면
    # MAX_TPS=50 가드레일 안에서 의미 있는 부하를 만들기 어렵다.
    wait_time = between(0.1, 0.5)

    # @task(weight): weight가 클수록 해당 메서드가 더 자주 선택된다.
    # 아래 가중치 합은 6+3+1+1=11이므로, light가 전체 요청의 약 6/11(~55%)을 차지한다.

    @task(6)
    def light(self):
        """기본 트래픽 — 대부분의 요청은 가벼운 엔드포인트로 간다.

        target-server의 /light는 asyncio.sleep(0.01)만 하고 바로 응답하므로
        baseline(평상시) 트래픽을 흉내낸다.
        """
        # name="/light"를 명시하지 않으면 Locust가 쿼리스트링 등으로 같은 경로를
        # 여러 통계 행으로 쪼갤 수 있어, 항상 name을 고정해 집계를 깔끔하게 만든다.
        self.client.get("/light", name="/light")

    @task(3)
    def heavy(self):
        """connection pool 고갈을 유도하는 무거운 요청.

        target-server의 /heavy는 asyncio.Semaphore(5)로 동시 처리량이 5개로
        제한돼 있다. 동시 요청이 5개를 넘으면 대기열이 쌓여 latency가 급증하는데,
        이것이 InfraGuard Agent가 진단해야 할 '병목 시나리오'다.
        """
        self.client.get("/heavy", name="/heavy")

    @task(1)
    def flaky(self):
        """가끔 실패하는 엔드포인트 — error_rate 계산용.

        target-server의 /flaky는 5% 확률로 500 에러를 반환한다.
        LoadTestResult.error_rate가 0이 아닌 값을 갖도록 하기 위한 시나리오.
        """
        self.client.get("/flaky", name="/flaky")

    @task(1)
    def health(self):
        """헬스체크 — baseline latency 측정용.

        거의 즉시 응답하는 엔드포인트라서, /heavy 트래픽으로 latency가
        전반적으로 늘어났을 때와 비교할 기준선(baseline) 역할을 한다.
        """
        self.client.get("/health", name="/health")

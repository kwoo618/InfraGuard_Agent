"""
이하은 - InfraGuard target-server에 대한 Locust 부하 시나리오.

infra/target-server/main.py 의 엔드포인트(/light, /heavy, /flaky, /health)를 대상으로
실제 트래픽 패턴을 흉내낸다. 가벼운 트래픽이 대부분이고 일부 요청이
DB connection pool을 흉내내는 /heavy로 몰리도록 가중치를 둬서
의도적으로 병목이 드러나게 만든다.

이 파일은 backend/app/tools/run_load_test.py 가 `locust -f locustfile.py ...`로
서브프로세스 실행한다. 직접 `locust --host http://localhost:8080` 명령으로도
웹 UI(localhost:8089)에서 수동 실행 가능하다.
"""

from locust import HttpUser, between, task


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

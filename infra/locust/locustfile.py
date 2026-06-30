from locust import HttpUser, task, between

class InfraGuardTargetUser(HttpUser):
    # 가상 유저들의 행동 간격 제어 (1초~2초 랜덤 대기하여 현실적인 부하 생성)
    wait_time = between(1.0, 2.0)

    @task(2)
    def view_index(self):
        """기획안 웹 페이지 메인 렌더링 시나리오"""
        self.client.get("/")

    @task(1)
    def call_tickets_api(self):
        """기획안 핵심 시나리오: 티켓 조회 API 호출"""
        self.client.get("/api/v1/tickets")
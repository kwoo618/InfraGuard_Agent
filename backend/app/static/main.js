const startBtn = document.getElementById('start-btn');
const logWindow = document.getElementById('log-window');
const approvalModal = document.getElementById('approval-modal');
const modalMessage = document.getElementById('modal-message');
const approveBtn = document.getElementById('approve-btn');
const rejectBtn = document.getElementById('reject-btn');

const statusLabel = document.getElementById("agent-status");

const steps = [
    "step-load",
    "step-metric",
    "step-ai",
    "step-plan",
    "step-approval",
    "step-scale",
    "step-report"
];

let currentTaskId = null;
let eventSource = null;

function appendLog(message, type = 'info') {
    let icon = "ℹ";
    switch (type) {
        case "system":
            icon = "⚙";
            break;
        case "success":
            icon = "✅";
            break;
        case "warning":
            icon = "⚠";
            break;
        case "error":
            icon = "❌";
            break;
    }

    const logEntry = document.createElement('div');
    logEntry.className = `log-entry ${type}`;
    logEntry.innerText = `[${new Date().toLocaleTimeString()}] ${icon} ${message}`;
    logWindow.appendChild(logEntry);
    logWindow.scrollTop = logWindow.scrollHeight;
}

function updateStatus(text, cls) {
    statusLabel.className = `status ${cls}`;
    statusLabel.innerHTML = text;
}

function activateStep(id) {
    document.getElementById(id).classList.add("active");
}

function completeStep(id) {
    const step = document.getElementById(id);
    step.classList.remove("active");
    step.classList.add("done");
}

function resetProgress() {
    steps.forEach(id => {
        const step = document.getElementById(id);
        step.classList.remove("active");
        step.classList.remove("done");
    });
}

async function fetchReport(taskId) {
    try {
        const response = await fetch(`/api/v1/agent/report/${taskId}`);
        if (!response.ok) {
            appendLog("리포트 조회 실패", "error");
            return;
        }

        const report = await response.json();

        if (report.measurement) {
            appendLog(`📊 측정값 — TPS: ${report.measurement.tps.toFixed(1)} / 에러율: ${(report.measurement.error_rate * 100).toFixed(1)}%`, 'info');
            appendLog(`📊 지연시간 — P95: ${report.measurement.latency_p95.toFixed(0)}ms / 평균: ${report.measurement.latency_avg.toFixed(0)}ms`, 'info');
            appendLog(`📊 요청 통계 — 총 ${report.measurement.total_requests}건 요청 (${report.measurement.duration}초간)`, 'info');
        }

        if (report.action) {
            const actionType = report.action.success ? 'success' : 'error';
            const actionMsg = report.action.success
                ? `🔧 조치 — Scale-out 완료 (${report.action.before_replicas} → ${report.action.after_replicas})`
                : `🔧 조치 실패 — ${report.action.error_message}`;
            appendLog(actionMsg, actionType);
        } else {
            appendLog("🔧 조치 — 아직 스케일링이 실행되지 않았습니다.", 'info');
        }
    } catch (error) {
        appendLog("리포트 조회 중 오류가 발생했습니다.", "error");
    }
}

startBtn.addEventListener('click', () => {
    logWindow.innerHTML = "";
    resetProgress();
    appendLog('자율 진단 시스템 가동 요청 중...', 'system');
    updateStatus("🔵 RUNNING", "running");
    activateStep("step-load");
    startBtn.disabled = true;

    const tpsInput = document.getElementById('target-tps-input');
    const durationInput = document.getElementById('duration-input');

    if (!tpsInput || !durationInput) {
        console.warn('입력 필드를 찾지 못했습니다 (target-tps-input / duration-input). index.html의 id를 확인하세요.');
    }

    const targetTps = tpsInput ? (parseInt(tpsInput.value, 10) || 30) : 30;
    const duration = durationInput ? (parseInt(durationInput.value, 10) || 10) : 10;

    console.log(`[진단 시작] target_tps=${targetTps}, duration=${duration}`);

    currentTaskId = null;
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource(
        `/api/v1/agent/start?target_tps=${encodeURIComponent(targetTps)}&duration=${encodeURIComponent(duration)}`
    );

    eventSource.onmessage = function (event) {
        const data = JSON.parse(event.data);

        if (data.task_id && currentTaskId && data.task_id !== currentTaskId) {
            console.warn('다른 task_id의 이벤트를 무시했습니다:', data.task_id, '(현재 추적 중:', currentTaskId + ')');
            return;
        }

        if (data.status === 'running') {
            if (!currentTaskId && data.task_id) {
                currentTaskId = data.task_id;
            }
            appendLog(data.message, 'info');
            completeStep("step-load");
            activateStep("step-metric");
        }

        else if (data.status === 'analyzing') {
            appendLog(data.message, 'info');
            completeStep("step-metric");
            activateStep("step-ai");
        }
        
        else if (data.status === 'error') {
            appendLog(data.message, 'error');
            updateStatus("🔴 ERROR", "error");
            eventSource.close();
            startBtn.disabled = false;
        }
        
        else if (data.status === 'need_approval') {
            appendLog(`가드레일 작동 : ${data.message}`, 'warning');
            currentTaskId = data.task_id;
            modalMessage.innerText = data.message;
            completeStep("step-ai");
            activateStep("step-plan");
            completeStep("step-plan");
            activateStep("step-approval");
            updateStatus("🟠 WAITING APPROVAL", "waiting");
            approvalModal.classList.remove("hidden");
        }

        else if (data.status === 'scaling') {
            appendLog(data.message, 'info');
            completeStep("step-approval");
            activateStep("step-scale");
            updateStatus("🟣 SCALING", "scaling");
        }

        else if (data.status === 'done') {
            appendLog(data.message, 'success');
            completeStep("step-scale");
            activateStep("step-report");
            completeStep("step-report");
            updateStatus("🟢 COMPLETED", "completed");
            startBtn.disabled = false;
            eventSource.close();
            fetchReport(data.task_id);
        }

        else if (data.status === 'failed') {
            appendLog(data.message, 'error');
            updateStatus("🔴 ERROR", "error");
            approvalModal.classList.add("hidden");
            startBtn.disabled = false;
            eventSource.close();
            if (data.task_id) {
                fetchReport(data.task_id);
            }
        }
    };

    eventSource.onerror = function () {
        // [수정 가드레일]: 부하 테스트 후 단계 전환 시 브라우저가 자동 재연결(CONNECTING)할 때는 튕기지 않고 기다립니다.
        if (eventSource.readyState === EventSource.CONNECTING) {
            console.log("단계 전환 또는 재연결 시도 중... 대기합니다.");
            return;
        }
        if (eventSource.readyState === EventSource.CLOSED) {
            appendLog("[에러] 도커 인프라가 꺼져 있거나 응답이 없습니다! docker compose up -d를 확인하세요.", "error");
            updateStatus("🔴 ERROR", "error");
            eventSource.close();
            startBtn.disabled = false;
        }
    };
});

// [수정 구역]: 승인 버튼 클릭 시 일단 모달창부터 즉시 숨기고 통신 시작
approveBtn.addEventListener('click', async () => {
    if (!currentTaskId) return;

    // 1. 화면에서 모달창을 0.1초 만에 먼저 숨기기 (Class 방식 + display 명시적 처리로 이중 잠금)
    approvalModal.classList.add('hidden');
    approvalModal.style.display = 'none';
    appendLog("스케일링 제안을 승인했습니다. 조치를 시작합니다.", "system");

    try {
        const response = await fetch('/api/v1/agent/approve', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                task_id: currentTaskId,
                approved: true
            })
        });

        if (response.ok) {
            const result = await response.json();
            if (result && result.message) {
                appendLog(result.message, 'success');
            }
        }
    }
    catch (error) {
        console.error("승인 요청 통신 실패:", error);
        appendLog("승인 요청 전달 중 네트워크 지연이 발생했으나 에이전트 상태를 이어서 관측합니다.", "warning");
    }
});

// [수정 구역]: 거절 버튼 클릭 시에도 즉시 모달창부터 숨기기
rejectBtn.addEventListener('click', async () => {
    if (!currentTaskId) return;

    // 1. 모달창 즉시 숨기기
    approvalModal.classList.add("hidden");
    approvalModal.style.display = 'none';
    appendLog("스케일링 제안을 거절했습니다.", "warning");

    try {
        await fetch('/api/v1/agent/approve', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                task_id: currentTaskId,
                approved: false
            })
        });
    }
    catch (error) {
        console.error("거절 요청 통신 실패:", error);
    }
});
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

    logEntry.innerText =
        `[${new Date().toLocaleTimeString()}] ${icon} ${message}`;

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


// /agent/report/{task_id} 조회 → 여태까지의 측정값(measurement)과
// 실제로 취해진 조치(action)를 로그창에 정리해서 보여준다.
async function fetchReport(taskId) {

    try {

        const response = await fetch(`/api/v1/agent/report/${taskId}`);

        if (!response.ok) {
            appendLog("리포트 조회 실패", "error");
            return;
        }

        const report = await response.json();

        if (report.bottleneck) {
            appendLog(
                `🧠 병목 진단 — ${report.bottleneck.cause} (심각도: ${report.bottleneck.severity} / 신뢰도: ${(report.bottleneck.confidence * 100).toFixed(0)}%)`,
                'info'
            );
            appendLog(`💡 권장 조치 — ${report.bottleneck.recommendation}`, 'info');
        }

        if (report.measurement) {
            appendLog(
                `📊 측정값 — TPS: ${report.measurement.tps.toFixed(1)} / P95: ${report.measurement.latency_p95.toFixed(0)}ms / 에러율: ${(report.measurement.error_rate * 100).toFixed(1)}%`,
                'info'
            );
        }

        if (report.action) {
            const actionType = report.action.success ? 'success' : 'error';
            const actionMsg = report.action.success
                ? `🔧 조치 — Scale-out 완료 (${report.action.before_replicas} → ${report.action.after_replicas})`
                : `🔧 조치 실패 — ${report.action.error_message}`;
            appendLog(actionMsg, actionType);
        }

        if (report.optimization_plan && report.optimization_plan.length > 0) {
            appendLog('📋 최적화 조치 목록', 'info');
            report.optimization_plan.forEach((item, idx) => {
                appendLog(`  ${idx + 1}. ${item}`, 'info');
            });
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

    // 새 진단을 시작하므로 이전 task 흔적을 초기화하고,
    // 혹시 남아있는 이전 연결이 있다면 정리한다.
    currentTaskId = null;
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource(
        `/api/v1/agent/start?target_tps=${encodeURIComponent(targetTps)}&duration=${encodeURIComponent(duration)}`
    );

    eventSource.onmessage = function (event) {
        const data = JSON.parse(event.data);

        // uvicorn --reload 등으로 서버가 재시작되면 브라우저 EventSource가
        // 자동으로 재연결을 시도하는데, 이때 서버는 완전히 새로운 task를
        // 발급한다. 이미 확정된 currentTaskId와 다른 task_id의 이벤트는
        // 이 화면과 무관한 흐름이므로 무시한다.
        if (data.task_id && currentTaskId && data.task_id !== currentTaskId) {
            console.warn('다른 task_id의 이벤트를 무시했습니다:', data.task_id, '(현재 추적 중:', currentTaskId + ')');
            return;
        }

        if (data.status === 'running') {
            if (!currentTaskId && data.task_id) {
                currentTaskId = data.task_id;   // 이 진단 흐름의 실제 task_id를 여기서 확정
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
        
        // 에러 상태 분기 추가
        else if (data.status === 'error') {
            appendLog(data.message, 'error');
            updateStatus("🔴 ERROR", "error");
            eventSource.close();
            startBtn.disabled = false;
        }
        
        else if (data.status === 'need_approval') {
            appendLog(data.message, 'warning');
            currentTaskId = data.task_id;
            modalMessage.innerText = data.message;
            completeStep("step-ai");
            activateStep("step-plan");
            completeStep("step-plan");
            activateStep("step-approval");
            updateStatus("🟠 WAITING APPROVAL", "waiting");
            approvalModal.classList.remove("hidden");
            // 승인 대기 중에도 서버가 결과를 이어서 보내줘야 하므로 연결을 끊지 않는다.
        }

        // scale_service 호출 중 백엔드가 보내는 진행 상태
        else if (data.status === 'scaling') {
            appendLog(data.message, 'info');
            completeStep("step-approval");
            activateStep("step-scale");
            updateStatus("🟣 SCALING", "scaling");
        }

        // 스케일링 후 동일 조건으로 재측정 중
        else if (data.status === 'remeasuring') {
            appendLog(data.message, 'info');
            updateStatus("🟣 RE-MEASURING", "scaling");
        }

        // scale_service 결과 success=True → 최종 완료
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

        // agent.py가 보내는 모든 실패 케이스(인프라 다운, 부하테스트 실패,
        // 승인 거절, scale_service 실패)는 status: 'failed'로 통일되어 온다.
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
        // 백엔드가 정상적으로 close()한 게 아니라, 진짜 도커가 꺼져서 통신이 터진 경우
        if (eventSource.readyState !== EventSource.CLOSED) {
            appendLog("[에러] 도커 인프라가 꺼져 있거나 응답이 없습니다! docker compose up -d를 확인하세요.", "error");
            updateStatus("🔴 ERROR", "error");
        } else {
            appendLog("스트리밍 연결 종료.", "system");
        }
        eventSource.close();
        startBtn.disabled = false;
    };
});



approveBtn.addEventListener('click', async () => {

    if (!currentTaskId) return;

    // 중복 클릭으로 같은 task_id를 두 번 승인 요청하는 것을 방지
    approveBtn.disabled = true;
    rejectBtn.disabled = true;

    // 네트워크 왕복을 기다리지 않고 클릭 즉시 모달을 닫는다 (체감 지연 제거).
    // 실패하면 catch에서 에러 로그로 알려준다.
    approvalModal.classList.add('hidden');

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

        const result = await response.json();

        // 성공 시엔 별도 로그 없이(이후 'scaling' 이벤트가 곧 뜬다), 실패했을 때만 로그로 알려준다.
        if (result.status !== 'success') {
            appendLog(result.message, 'error');
        }

        // 이후 진행 상황(스케일링 → 완료/실패)은 이미 열려 있는 SSE 스트림의
        // 'scaling' / 'done' / 'failed' 이벤트에서 실시간으로 갱신된다.
        // (scale_service.py 실제 실행 결과를 그대로 반영)

    }

    catch (error) {

        appendLog("승인 요청 실패", "error");

        updateStatus("🔴 ERROR", "error");

        startBtn.disabled = false;

    }

    finally {
        approveBtn.disabled = false;
        rejectBtn.disabled = false;
    }

});



rejectBtn.addEventListener('click', async () => {

    if (!currentTaskId) return;

    // 중복 클릭으로 같은 task_id를 두 번 거절 요청하는 것을 방지
    approveBtn.disabled = true;
    rejectBtn.disabled = true;

    approvalModal.classList.add("hidden");

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

        // agent.py가 승인 거절을 감지하면 status: 'failed' 이벤트를 스트리밍으로
        // 보내주므로, 최종 로그/상태 갱신은 위 eventSource.onmessage에서 처리한다.

    }

    catch (error) {

        appendLog("거절 요청 실패", "error");

        updateStatus("🔴 ERROR", "error");

        startBtn.disabled = false;
    }

    finally {
        approveBtn.disabled = false;
        rejectBtn.disabled = false;

    }

});
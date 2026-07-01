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



startBtn.addEventListener('click', () => {

    logWindow.innerHTML = "";

    resetProgress();

    appendLog('자율 진단 시스템 가동 요청 중...', 'system');

    updateStatus("🔵 RUNNING", "running");

    activateStep("step-load");

    startBtn.disabled = true;

    eventSource = new EventSource('/api/v1/agent/start');

    eventSource.onmessage = function (event) {
        const data = JSON.parse(event.data);

        if (data.status === 'running') {
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
            appendLog(`가드레일 작동 : ${data.message}`, 'warning');
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

        // scale_service 결과 success=True → 최종 완료
        else if (data.status === 'done') {
            appendLog(data.message, 'success');
            completeStep("step-scale");
            activateStep("step-report");
            completeStep("step-report");
            updateStatus("🟢 COMPLETED", "completed");
            startBtn.disabled = false;
            eventSource.close();
        }

        // agent.py가 보내는 모든 실패 케이스(인프라 다운, 부하테스트 실패,
        // 승인 거절, scale_service 실패)는 status: 'failed'로 통일되어 온다.
        else if (data.status === 'failed') {
            appendLog(data.message, 'error');
            updateStatus("🔴 ERROR", "error");
            approvalModal.classList.add("hidden");
            startBtn.disabled = false;
            eventSource.close();
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

        appendLog(result.message, 'success');

        approvalModal.classList.add('hidden');

        // 이후 진행 상황(스케일링 → 완료/실패)은 이미 열려 있는 SSE 스트림의
        // 'scaling' / 'done' / 'failed' 이벤트에서 실시간으로 갱신된다.
        // (scale_service.py 실제 실행 결과를 그대로 반영)

    }

    catch (error) {

        appendLog("승인 요청 실패", "error");

        updateStatus("🔴 ERROR", "error");

        startBtn.disabled = false;

    }

});



rejectBtn.addEventListener('click', async () => {

    if (!currentTaskId) return;

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

        approvalModal.classList.add("hidden");

        // agent.py가 승인 거절을 감지하면 status: 'failed' 이벤트를 스트리밍으로
        // 보내주므로, 최종 로그/상태 갱신은 위 eventSource.onmessage에서 처리한다.

    }

    catch (error) {

        appendLog("거절 요청 실패", "error");

        updateStatus("🔴 ERROR", "error");

        startBtn.disabled = false;

    }

});
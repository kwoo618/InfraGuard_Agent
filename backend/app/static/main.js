const startBtn = document.getElementById('start-btn');
const logWindow = document.getElementById('log-window');
const approvalModal = document.getElementById('approval-modal');
const modalMessage = document.getElementById('modal-message');
const approveBtn = document.getElementById('approve-btn');
const rejectBtn = document.getElementById('reject-btn');

// =======================
// Agent Status
// =======================

const statusLabel = document.getElementById("agent-status");

// =======================
// Progress
// =======================

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

// =======================
// Log
// =======================

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

// =======================
// Status
// =======================

function updateStatus(text, cls) {

    statusLabel.className = `status ${cls}`;
    statusLabel.innerHTML = text;

}

// =======================
// Progress
// =======================

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

// =======================
// Start Button
// =======================

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

        // ===================
        // Running
        // ===================

        if (data.status === 'running') {

            appendLog(data.message, 'info');

            completeStep("step-load");

            activateStep("step-metric");

        }

        // ===================
        // AI Analysis
        // ===================

        else if (data.status === 'analyzing') {

            appendLog(data.message, 'info');

            completeStep("step-metric");

            activateStep("step-ai");

        }

        // ===================
        // Approval
        // ===================

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

            eventSource.close();

        }

    };

    eventSource.onerror = function () {

        appendLog("스트리밍 연결 종료.", "system");

        eventSource.close();

        startBtn.disabled = false;

    };

});

// =======================
// Approve
// =======================

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

        completeStep("step-approval");

        activateStep("step-scale");

        updateStatus("🟣 SCALING", "scaling");

        setTimeout(() => {

            completeStep("step-scale");

            activateStep("step-report");

            setTimeout(() => {

                completeStep("step-report");

                updateStatus("🟢 COMPLETED", "completed");

                appendLog("최종 리포트 생성 완료.", "success");

                startBtn.disabled = false;

            }, 700);

        }, 1000);

    }

    catch (error) {

        appendLog("승인 요청 실패", "error");

        updateStatus("🔴 ERROR", "error");

        startBtn.disabled = false;

    }

});

// =======================
// Reject
// =======================

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

        appendLog("유저가 인프라 조치를 거절했습니다.", "error");

        approvalModal.classList.add("hidden");

        updateStatus("🔴 REJECTED", "error");

        startBtn.disabled = false;

    }

    catch (error) {

        appendLog("거절 요청 실패", "error");

        updateStatus("🔴 ERROR", "error");

        startBtn.disabled = false;

    }

});
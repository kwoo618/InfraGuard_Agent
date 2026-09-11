const startBtn = document.getElementById('start-btn');
const logWindow = document.getElementById('log-window');
const approvalModal = document.getElementById('approval-modal');
const modalMessage = document.getElementById('modal-message');
const approveBtn = document.getElementById('approve-btn');
const rejectBtn = document.getElementById('reject-btn');
const lowConfidenceBox = document.getElementById('low-confidence-box');
const lowConfidenceText = document.getElementById('low-confidence-text');
const lowConfidenceAck = document.getElementById('low-confidence-ack');
const replicaCount = document.getElementById('replica-count');
const resetReplicasBtn = document.getElementById('reset-replicas-btn');
const busyNote = document.getElementById('busy-note');



const statusLabel = document.getElementById("agent-status");

// URL에 ?debug=1이 있으면 디버그 모드: 진단 요청에 force_scaling=true를 붙인다.
// 발표·측정 화면에서 실수로 켜져 있지 않은지 보이도록 상태 표시줄에 배지를 띄운다.
const DEBUG_MODE = new URLSearchParams(window.location.search).get('debug') === '1';

if (DEBUG_MODE) {
    const debugBadge = document.getElementById('debug-badge');
    if (debugBadge) debugBadge.classList.remove('hidden');
}


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

// 저신뢰 확인 게이트 (#83): 현재 승인 요청이 신뢰도 기준 미만 스케일링 제안인지.
// UI는 체크박스를 누르기 전까지 승인 버튼을 막고, 서버(/approve)도 확인 플래그 없는 승인을 400으로 막는다.
let pendingLowConfidence = false;

function syncApproveButton() {
    approveBtn.disabled = pendingLowConfidence && !lowConfidenceAck.checked;
}

function setupLowConfidenceGate(data) {
    pendingLowConfidence = data.low_confidence === true;
    lowConfidenceAck.checked = false;

    if (pendingLowConfidence) {
        const confidence = fmtConfidence(data.confidence);   // result_panel.js
        const threshold = typeof data.low_confidence_threshold === 'number'
            ? `${Math.round(data.low_confidence_threshold * 100)}%`
            : '';
        lowConfidenceText.textContent = `⚠ AI 신뢰도 ${confidence} — 기준 ${threshold} 미만`;
        lowConfidenceBox.classList.remove('hidden');
    } else {
        lowConfidenceText.textContent = '';
        lowConfidenceBox.classList.add('hidden');
    }

    syncApproveButton();
}

lowConfidenceAck.addEventListener('change', syncApproveButton);

// 현재 서버 수 표시 · 1대 초기화 (측정 회차 사이 사람의 수동 조작, 결과 파일에 기록하지 않음).
// 동시 실행 방지 (docs/02 ISSUE-17, #87): 이 탭에서 실행 중이거나 서버가 busy이면
// (다른 실행 진행 중·끊긴 실행의 부하 테스트 대기·초기화 중) 시작 버튼과 초기화 버튼을 막는다. 서버도 409로 거부한다.
let agentRunning = false;
let replicaResetting = false;
let serverBusy = false;          // /agent/replicas의 busy (다른 탭의 실행, 끊긴 실행의 부하 테스트 포함)
let serverBusyReason = null;     // agent_running / load_test_running / replica_reset
let busyPollTimer = null;

// 서버 busy_reason별 안내 (agent.py BUSY_MESSAGES와 같은 뜻)
const BUSY_REASON_TEXT = {
    agent_running: '다른 실행이 진행 중입니다(승인 대기 포함). 끝난 뒤 시작할 수 있습니다.',
    load_test_running: '연결이 끊긴 이전 실행의 부하 테스트가 아직 돌고 있습니다. 끝나면 시작할 수 있습니다.',
    replica_reset: '서버 수 초기화가 진행 중입니다.',
};

function busyReasonText() {
    return BUSY_REASON_TEXT[serverBusyReason] || '지금은 새 실행을 시작할 수 없습니다.';
}

function syncControlButtons() {
    const blocked = agentRunning || replicaResetting || serverBusy;
    resetReplicasBtn.disabled = blocked;
    startBtn.disabled = blocked;

    // 이 탭의 실행이 아닌 이유로 막혔으면 이유를 보여준다
    const showNote = serverBusy && !agentRunning && !replicaResetting;
    busyNote.textContent = showNote ? `⛔ ${busyReasonText()}` : '';
    busyNote.classList.toggle('hidden', !showNote);
}

function setAgentRunning(running) {
    const wasRunning = agentRunning;
    agentRunning = running;
    syncControlButtons();
    // 실행이 끝날 때마다 실제 서버 수를 다시 읽는다
    if (wasRunning && !running) {
        refreshReplicas();
    }
}

/** 서버 수와 busy 상태를 다시 읽는다. 반환값: 서버가 새 실행을 막고 있는지 */
async function refreshReplicas() {
    try {
        const response = await fetch('/api/v1/agent/replicas');
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        replicaCount.textContent = typeof data.replicas === 'number' ? `${data.replicas}대` : '조회 실패';
        serverBusy = data.busy === true;
        serverBusyReason = data.busy_reason || null;
    } catch (error) {
        console.warn('[replicas] 조회 실패:', error);
        replicaCount.textContent = '조회 실패';
        serverBusy = false;
        serverBusyReason = null;
    }
    syncControlButtons();

    // 다른 실행이나 끊긴 실행의 부하 테스트가 끝나면 버튼을 다시 켜기 위해 주기적으로 확인한다
    if (busyPollTimer) {
        clearTimeout(busyPollTimer);
        busyPollTimer = null;
    }
    if (serverBusy && !agentRunning) {
        busyPollTimer = setTimeout(refreshReplicas, 3000);
    }

    return serverBusy;
}

resetReplicasBtn.addEventListener('click', async () => {
    if (resetReplicasBtn.disabled) return;

    const confirmed = window.confirm(
        `서버(target-server)를 1대로 초기화합니다. (현재 ${replicaCount.textContent})\n`
        + '측정 회차 사이에만 사용하세요. 결과 파일에는 기록되지 않습니다. 계속할까요?'
    );
    if (!confirmed) return;

    replicaResetting = true;
    syncControlButtons();
    replicaCount.textContent = '초기화 중…';
    appendLog('서버 1대로 초기화 요청', 'system');

    try {
        const response = await fetch('/api/v1/agent/replicas/reset', { method: 'POST' });
        const result = await response.json();

        if (!response.ok) {
            appendLog(result.detail || '서버 수 초기화가 거부되었습니다.', 'error');
        } else if (result.success) {
            appendLog(`서버 수 초기화 완료: ${result.before_replicas}대 → ${result.after_replicas}대`, 'success');
        } else {
            appendLog(`서버 수 초기화 실패: ${result.error_message || '원인 미상'}`, 'error');
        }
    } catch (error) {
        appendLog('서버 수 초기화 요청 실패', 'error');
    } finally {
        replicaResetting = false;
        await refreshReplicas();
    }
});

refreshReplicas();

// 부하 테스트 프로그레스 바 타이머
let loadTestTimer = null;
let loadTestDuration = 0;
let loadTestElapsed = 0;

function stopLoadTestTimer() {
    if (loadTestTimer) {
        clearInterval(loadTestTimer);
        loadTestTimer = null;
    }
}

function updateLoadTestProgressUI() {
    const total = Math.max(loadTestDuration, 1);
    const elapsed = Math.min(loadTestElapsed, total);
    const pct = Math.min((elapsed / total) * 100, 100);
    const fill = document.getElementById('progress-bar-fill');
    const text = document.getElementById('progress-text');
    if (fill) fill.style.width = `${pct}%`;
    if (text) text.innerText = `${elapsed.toFixed(1)}s / ${total}s (${pct.toFixed(0)}%)`;
}

function setLoadTestProgressCompleteStyle(isComplete) {
    const fill = document.getElementById('progress-bar-fill');
    if (fill) fill.classList.toggle('is-complete', isComplete);
}

function resetLoadTestProgress() {
    stopLoadTestTimer();
    loadTestDuration = 0;
    loadTestElapsed = 0;
    setLoadTestProgressCompleteStyle(false);
    const container = document.getElementById('load-test-progress-container');
    const fill = document.getElementById('progress-bar-fill');
    const text = document.getElementById('progress-text');
    if (container) container.classList.add('hidden');
    if (fill) fill.style.width = '0%';
    if (text) text.innerText = '0.0s / 0s (0%)';
}

function startLoadTestProgress(totalSeconds, sourceMessage) {
    stopLoadTestTimer();
    setLoadTestProgressCompleteStyle(false);
    loadTestDuration = Math.max(parseInt(totalSeconds, 10) || 1, 1);
    loadTestElapsed = 0;
    const container = document.getElementById('load-test-progress-container');
    const label = document.getElementById('progress-label');
    if (container) container.classList.remove('hidden');
    if (label) {
        label.textContent = sourceMessage || `⚡ 부하 테스트 진행 중... (${loadTestDuration}초)`;
    }
    updateLoadTestProgressUI();
    loadTestTimer = setInterval(() => {
        loadTestElapsed += 0.1;
        if (loadTestElapsed >= loadTestDuration) {
            loadTestElapsed = loadTestDuration;
            stopLoadTestTimer();
        }
        updateLoadTestProgressUI();
    }, 100);
}

function completeLoadTestProgress() {
    stopLoadTestTimer();
    if (loadTestDuration > 0) {
        loadTestElapsed = loadTestDuration;
    }
    const fill = document.getElementById('progress-bar-fill');
    const text = document.getElementById('progress-text');
    if (fill) fill.style.width = '100%';
    setLoadTestProgressCompleteStyle(true);
    if (text && loadTestDuration > 0) {
        text.innerText = `${loadTestDuration.toFixed(1)}s / ${loadTestDuration}s (100%)`;
    }
}

function hideLoadTestProgress() {
    stopLoadTestTimer();
    const container = document.getElementById('load-test-progress-container');
    if (container) container.classList.add('hidden');
}

/** SSE "⚡ 부하 테스트 진행 중... (N초)" 메시지에서 N(초) 추출 */
function parseLoadTestDurationSeconds(message) {
    if (!message) return null;

    const text = String(message);
    const match = text.match(/부하\s*테스트\s*진행\s*중[^)]*\(\s*(\d+)\s*초\s*\)/u);
    if (!match) return null;

    const seconds = parseInt(match[1], 10);
    return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
}

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

    // 진행 중 상태(RUNNING·WAITING·SCALING)면 에이전트 실행 중으로 보고 시작·서버 초기화 버튼을 막는다.
    // 끝난 상태(COMPLETED·ERROR)로 바뀌면 실제 서버 수와 busy 상태를 다시 읽는다.
    setAgentRunning(['running', 'waiting', 'scaling'].includes(cls));

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

        if (report.measurement_after) {
            appendLog(
                `📈 재검증 — TPS: ${report.measurement_after.tps.toFixed(1)} / P95: ${report.measurement_after.latency_p95.toFixed(0)}ms / 에러율: ${(report.measurement_after.error_rate * 100).toFixed(1)}%`,
                'info'
            );
        }

        if (report.improvement) {
            const latencyDelta = report.improvement.latency_p95_delta;
            const tpsDelta = report.improvement.tps_delta;
            const improved = latencyDelta < 0;   // P95가 줄었으면 개선
            appendLog(
                `${improved ? '✨' : '⚠️'} 개선 결과 — TPS ${tpsDelta >= 0 ? '+' : ''}${tpsDelta.toFixed(1)}, P95 ${latencyDelta >= 0 ? '+' : ''}${latencyDelta.toFixed(0)}ms`,
                improved ? 'success' : 'warning'
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

        // 결과 패널 (Phase 4, #75, result_panel.js). 위의 로그 출력은 그대로 두고 패널을 추가로 그린다.
        try {
            renderResultPanel(report);
        } catch (panelError) {
            console.error('[result-panel] 렌더링 실패:', panelError);
            appendLog("결과 패널을 표시하지 못했습니다. (로그의 결과는 위와 같습니다)", "error");
        }

    } catch (error) {
        appendLog("리포트 조회 중 오류가 발생했습니다.", "error");
    }
}



startBtn.addEventListener('click', async () => {

    if (startBtn.disabled) return;

    // 동시 실행 방지 (docs/02 ISSUE-17, #87): 시작 전에 서버가 새 실행을 받을 수 있는지 한 번 더 확인한다.
    // 막혀 있으면 이전 로그를 지우지 않고 이유만 남긴다. 확인과 시작 사이에 끼어든 경우는 서버가 409로 막는다.
    startBtn.disabled = true;
    if (await refreshReplicas()) {
        appendLog(busyReasonText(), 'warning');
        return;
    }

    logWindow.innerHTML = "";

    resetProgress();
    resetLoadTestProgress();
    resetResultPanel();

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

    // force_scaling은 URL에 ?debug=1이 있을 때만 보낸다 (디버그 전용).
    // 예전에는 매 요청 보냈는데, 서버 DEBUG_ENDPOINTS_ENABLED가 켜진 채로 발표·측정하면
    // LLM 판단이 덮어써질 위험이 있어서 없앴다. 실제 덮어쓰기 여부는 여전히 서버 설정이 결정한다.
    const params = new URLSearchParams({
        target_tps: String(targetTps),   // Locust 동시 가상 사용자 수 (처리량 목표 아님, docs/02 ISSUE-11)
        duration: String(duration),
    });
    if (DEBUG_MODE) {
        params.set('force_scaling', 'true');
    }
    eventSource = new EventSource(`/api/v1/agent/start?${params.toString()}`);

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
                currentTaskId = data.task_id;
            }
            appendLog(data.message, 'info');
        }

        else if (data.status === 'analyzing') {
            appendLog(data.message, 'info');
            const msg = data.message || '';

            if (msg.includes('부하 테스트') && msg.includes('진행 중')) {
                const seconds = parseLoadTestDurationSeconds(msg);
                if (seconds !== null) {
                    activateStep('step-load');
                    startLoadTestProgress(seconds, msg);
                } else {
                    console.warn('[load-test] SSE duration 파싱 실패:', msg);
                }
            } else if (msg.includes('부하 테스트 완료')) {
                completeLoadTestProgress();
                completeStep('step-load');
                activateStep('step-metric');
            } else if (msg.includes('AI 분석 완료')) {
                hideLoadTestProgress();
                completeStep('step-metric');
                activateStep('step-ai');
            }
        }

        else if (data.status === 'error') {
            hideLoadTestProgress();

            appendLog(data.message, 'error');
            updateStatus("🔴 ERROR", "error");
            eventSource.close();
            startBtn.disabled = false;
        }

        else if (data.status === 'need_approval') {
            appendLog(data.message, 'warning');
            currentTaskId = data.task_id;
            modalMessage.innerText = data.message;
            setupLowConfidenceGate(data);
            if (pendingLowConfidence) {
                appendLog(`${lowConfidenceText.textContent} — 승인하려면 낮은 신뢰도 확인이 필요합니다.`, 'warning');
            }
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
        // 승인 거절, scale_service 실패, 동시 실행 거부)는 status: 'failed'로 통일되어 온다.
        else if (data.status === 'failed') {
            hideLoadTestProgress();

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
        hideLoadTestProgress();

        if (!currentTaskId) {
            // 첫 이벤트도 받기 전에 끊겼다: 서버가 새 실행을 거부했거나(409, 동시 실행 방지) 서버에 연결할 수 없다.
            // EventSource는 응답 본문을 읽을 수 없어서 이유는 busy 상태를 다시 읽어 표시한다.
            appendLog("실행을 시작하지 못했습니다. 다른 실행이 진행 중이거나 서버에 연결할 수 없습니다.", "error");
            updateStatus("🔴 ERROR", "error");
        } else if (eventSource.readyState !== EventSource.CLOSED) {
            // 백엔드가 정상적으로 close()한 게 아니라, 진짜 도커가 꺼져서 통신이 터진 경우
            appendLog("[에러] 도커 인프라가 꺼져 있거나 응답이 없습니다! docker compose up -d를 확인하세요.", "error");
            updateStatus("🔴 ERROR", "error");
        } else {
            appendLog("스트리밍 연결 종료.", "system");
        }
        eventSource.close();
        startBtn.disabled = false;
        refreshReplicas();
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
                approved: true,
                // 저신뢰 확인 게이트(#83): 체크박스를 눌렀을 때만 true. 없으면 서버가 400으로 막는다
                acknowledge_low_confidence: pendingLowConfidence && lowConfidenceAck.checked

            })

        });

        const result = await response.json();

        if (!response.ok) {
            // 400: 저신뢰 제안을 확인 없이 승인한 경우 (#83). 승인 대기가 유지되므로 모달을 다시 연다
            appendLog(result.detail || '승인 요청이 거부되었습니다.', 'error');
            approvalModal.classList.remove('hidden');
            return;
        }

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
        rejectBtn.disabled = false;
        // 저신뢰 게이트면 체크박스 상태를 따른다 (#83)
        syncApproveButton();
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
                approved: false,
                // 거절은 확인 플래그 없이 받는다. 결과 파일 acknowledged 기록용으로 체크 상태만 함께 보낸다 (#83)
                acknowledge_low_confidence: pendingLowConfidence && lowConfidenceAck.checked

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
        rejectBtn.disabled = false;
        syncApproveButton();
    }

});

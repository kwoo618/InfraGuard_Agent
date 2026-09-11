/*
 * 결과 패널 공용 계산 (docs/03 Phase 4 #75, 디자인 개편 #93)
 *
 * /api/v1/agent/report/{task_id} 응답(또는 같은 모양으로 바꾼 저장된 결과 파일)의 값을 화면 문자열로 바꾸는 규칙을 모은다.
 * 기본 보기(refresh/c.js)와 상세 보기(refresh/a.js)가 같은 함수를 불러서 자릿수·SLO 판정·결론 규칙이 같다.
 * #93에서 기존 화면(legacy) 렌더러를 지웠다. 이 파일에는 계산과 두 보기가 함께 쓰는 작은 DOM 조각만 남긴다.
 *
 * 표시 규칙
 * - 화면 문장은 측정값으로 만든 템플릿이다. LLM 문장은 상세 보기에서 "원문 보기"로 접어서만 보여준다.
 * - 목업·샘플 데이터 없음. 값이 없으면 "측정값 없음"을 표시한다.
 * - SLO 판정: P95 < SLO 충족, P95 == SLO 경계, P95 > SLO 미충족.
 *   Locust는 응답시간을 반올림해 기록하므로 SLO와 같은 값은 실제로는 SLO를 조금 넘었을 수 있다 (docs/02 ISSUE-10).
 * - 자릿수: TPS 소수 1자리, P95 정수 ms, 에러율 % 소수 2자리. 변화량은 원본값으로 계산한 뒤 표시할 때만 반올림한다.
 * - 동시 가상 사용자 수(target_tps)는 처리량 목표가 아니다 (docs/02 ISSUE-11). 처리량은 측정값 tps만 쓴다.
 * - LLM 문자열은 textContent로만 넣는다 (innerHTML 사용 안 함).
 */

const NO_DATA = '측정값 없음';

// run_history.UNMEASURED_LABEL과 같은 표기
const UNMEASURED_LABEL = '측정 불가';

const SLO_LABEL = {
    met: '충족',
    boundary: '경계(= SLO)',
    exceeded: '미충족',
};

const SVG_NS = 'http://www.w3.org/2000/svg';


// ---------------------------------------------------------------------
// 값 포맷
// ---------------------------------------------------------------------

function isNumber(value) {
    return typeof value === 'number' && Number.isFinite(value);
}

function fmtTps(value) {
    return isNumber(value) ? value.toFixed(1) : NO_DATA;
}

function fmtMs(value) {
    return isNumber(value) ? `${Math.round(value)}ms` : NO_DATA;
}

function fmtPct(rate) {
    return isNumber(rate) ? `${(rate * 100).toFixed(2)}%` : NO_DATA;
}

function fmtReplicas(value) {
    return isNumber(value) ? `${value}대` : NO_DATA;
}

/** 신뢰도 %. 0.595를 60%로 올려 보이지 않도록 소수 1자리까지 남긴다. */
function fmtConfidence(confidence) {
    if (!isNumber(confidence)) return NO_DATA;
    return `${Math.round(confidence * 1000) / 10}%`;
}

function shorten(text, limit) {
    const value = String(text);
    return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

function lastOf(list) {
    return Array.isArray(list) && list.length > 0 ? list[list.length - 1] : null;
}

function findByRound(list, round) {
    if (!Array.isArray(list)) return null;
    return list.find(item => item && item.round === round) || null;
}

/** P95와 SLO 비교: met / boundary / exceeded / unknown */
function sloStatus(p95, slo) {
    if (!isNumber(p95) || !isNumber(slo)) return 'unknown';
    if (p95 === slo) return 'boundary';
    return p95 < slo ? 'met' : 'exceeded';
}

function sloText(slo, status) {
    if (status === 'unknown') {
        return isNumber(slo) ? `SLO ${slo}ms 판정 불가` : 'SLO 기준 없음';
    }
    return `SLO ${slo}ms ${SLO_LABEL[status]}`;
}

function replicasArrow(before, after) {
    if (!isNumber(before) || !isNumber(after)) return `서버 수 ${NO_DATA}`;
    return `서버 ${before}→${after}대`;
}

function planText(approval) {
    const plan = approval && approval.scaling_plan;
    if (!plan || !isNumber(plan.current_replicas) || !isNumber(plan.desired_replicas)) return '';
    return `${plan.current_replicas}→${plan.desired_replicas}대`;
}

/**
 * 전/후 변화량. higherIsBetter가 없으면 좋고 나쁨을 판단하지 않는다(서버 수).
 * 표시 자릿수로 반올림해서 0이면 "변화 없음"으로 쓴다.
 */
function describeDelta(before, after, { digits, unit = '', scale = 1, higherIsBetter }) {
    if (!isNumber(before) || !isNumber(after)) {
        return { text: NO_DATA, tone: 'neutral' };
    }

    const factor = 10 ** digits;
    const delta = Math.round((after - before) * scale * factor) / factor;

    if (delta === 0) {
        return { text: '변화 없음', tone: 'neutral' };
    }

    const arrow = delta > 0 ? '▲' : '▼';
    const sign = delta > 0 ? '+' : '−';
    const text = `${arrow} ${sign}${Math.abs(delta).toFixed(digits)}${unit}`;

    if (higherIsBetter === undefined) {
        return { text, tone: 'neutral' };
    }

    const better = higherIsBetter ? delta > 0 : delta < 0;
    return { text: `${text} ${better ? '좋아짐' : '나빠짐'}`, tone: better ? 'good' : 'bad' };
}


// ---------------------------------------------------------------------
// 요약 문구 (측정값 템플릿, LLM 문장 사용 안 함) — 상세 보기 판정 띠의 둘째 줄
// ---------------------------------------------------------------------

function buildSummary(report) {
    const history = report.measurement_history || [];
    const slo = report.conditions ? report.conditions.p95_slo_ms : null;
    const endReason = report.end_reason;
    const failed = endReason === 'failed' || report.outcome === 'failed';

    if (history.length === 0) {
        const parts = [failed ? '실행 실패' : '실행 결과', NO_DATA];
        if (failed && report.error) parts.push(shorten(report.error, 80));
        return { tone: failed ? 'danger' : 'neutral', text: parts.join(' · ') };
    }

    const first = history[0];
    const last = history[history.length - 1];
    const multi = history.length >= 2;
    const status = sloStatus(last.latency_p95, slo);

    const p95Part = multi
        ? `P95 ${fmtMs(first.latency_p95)} → ${fmtMs(last.latency_p95)}`
        : `P95 ${fmtMs(last.latency_p95)}`;
    const sloPart = sloText(slo, status);
    const serverPart = multi
        ? replicasArrow(first.replicas, last.replicas)
        : `서버 ${fmtReplicas(first.replicas)} 유지`;

    // SLO 판정별 톤: 충족 초록 / 경계 노랑 / 미충족 주황
    const sloTone = { met: 'success', boundary: 'boundary', exceeded: 'warning', unknown: 'neutral' }[status];

    switch (endReason) {
        case 'scaled':
            return { tone: sloTone, text: [p95Part, sloPart, serverPart].join(' · ') };

        case 'no_further_scaling':
            return { tone: sloTone, text: [p95Part, sloPart, serverPart, '재진단: 추가 스케일링 미제안'].join(' · ') };

        case 'no_scaling_proposed': {
            // SLO를 충족했을 때만 "불필요"라고 쓴다. 미충족·경계면 "미제안" (결론 문구와 같은 규칙).
            // SLO를 넘었는데 제안하지 않은 경우도 그대로 보여준다 (docs/02 ISSUE-5 관찰 1)
            const lead = status === 'met' ? '스케일링 불필요 판단' : '스케일링 미제안';
            const tone = status === 'met' ? 'info' : sloTone;
            return { tone, text: [lead, p95Part, sloPart, serverPart].join(' · ') };
        }

        case 'rejected': {
            const plan = planText(lastOf(report.approvals));
            const target = plan ? `(${plan})` : '';
            const lead = multi ? `추가 스케일링 제안${target} 거절` : `스케일링 제안${target} 거절`;
            return { tone: 'warning', text: [lead, p95Part, sloPart, serverPart].join(' · ') };
        }

        case 'failed': {
            const parts = ['실행 실패', p95Part, sloPart, serverPart];
            if (report.error) parts.push(shorten(report.error, 80));
            return { tone: 'danger', text: parts.join(' · ') };
        }

        default:
            return { tone: 'neutral', text: [p95Part, sloPart, serverPart].join(' · ') };
    }
}


// ---------------------------------------------------------------------
// DOM 헬퍼 (두 보기가 함께 쓴다)
// ---------------------------------------------------------------------

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function svgEl(tag, attrs, text) {
    const node = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text !== undefined) node.textContent = text;
    return node;
}


// ---------------------------------------------------------------------
// AI 판단 규칙
// ---------------------------------------------------------------------

/** 진단 라운드의 측정 레코드로 계산한 P95 SLO 판정 (met / boundary / exceeded / unknown) */
function diagnosisSloState(diagnosis, report) {
    const record = findByRound((report && report.measurement_history) || [], diagnosis.round);
    const slo = report && report.conditions ? report.conditions.p95_slo_ms : null;
    return sloStatus(record ? record.latency_p95 : null, slo);
}

/**
 * 결론 표기. sloState는 그 진단 라운드의 P95 SLO 판정(sloStatus 결과)이다.
 * 스케일링을 제안하지 않았을 때 SLO를 충족한 경우에만 "불필요"라고 쓴다.
 * 미충족·경계·판정 불가면 "미제안"이다 (필요 없다고 단정하지 않는다).
 * 반환하는 요소의 글자와 badge-* 클래스(톤)를 두 보기가 읽어 쓴다.
 */
function conclusionBadge(diagnosis, approval, sloState) {
    if (approval && approval.source === 'forced') {
        return el('span', 'badge badge-danger', 'LLM 판단: 스케일링 불필요 → 디버그로 강제 승인 요청');
    }

    if (diagnosis.requires_scaling) {
        const plan = planText(approval);
        return el('span', 'badge badge-warning', plan ? `스케일링 제안 ${plan}` : '스케일링 제안');
    }

    return sloState === 'met'
        ? el('span', 'badge badge-info', '스케일링 불필요')
        : el('span', 'badge badge-muted', '스케일링 미제안');
}

/** 신뢰도 막대. threshold(#83 저신뢰 확인 게이트 기준, /report low_confidence_threshold)가 있으면 기준선을 그린다. */
function confidenceMeter(confidence, threshold) {
    const wrap = el('div', 'confidence');
    wrap.appendChild(el('span', 'confidence-label', '신뢰도'));

    const track = el('div', 'confidence-track');
    const fill = el('div', 'confidence-fill');
    if (isNumber(confidence)) {
        fill.style.width = `${Math.max(0, Math.min(confidence, 1)) * 100}%`;
    }
    track.appendChild(fill);

    if (isNumber(threshold)) {
        const mark = el('div', 'confidence-mark');
        mark.style.left = `${threshold * 100}%`;
        mark.title = `승인 확인 기준 ${Math.round(threshold * 100)}% (미만이면 확인 후 승인)`;
        track.appendChild(mark);

        const markLabel = el('span', 'confidence-mark-label', `기준 ${Math.round(threshold * 100)}%`);
        markLabel.style.left = `${threshold * 100}%`;
        track.appendChild(markLabel);
    }

    wrap.appendChild(track);
    wrap.appendChild(el('span', 'confidence-value', fmtConfidence(confidence)));
    return wrap;
}

/** 진단 라운드의 측정값. LLM이 실제로 무엇을 근거로 썼는지는 코드로 알 수 없어 "진단 입력 측정값"으로 부른다. */
function evidenceChips(record, slo, conditions) {
    const group = el('div', 'chip-group');

    if (!record) {
        group.appendChild(el('span', 'chip', NO_DATA));
        return group;
    }

    const p95 = record.latency_p95;
    const status = sloStatus(p95, slo);
    const p95Text = {
        exceeded: () => `P95 ${fmtMs(p95)} > SLO ${slo}ms · ${(p95 / slo).toFixed(1)}배`,
        boundary: () => `P95 ${fmtMs(p95)} = SLO ${slo}ms (경계)`,
        met: () => `P95 ${fmtMs(p95)} < SLO ${slo}ms`,
        unknown: () => `P95 ${fmtMs(p95)} · SLO 기준 없음`,
    }[status]();

    group.appendChild(el('span', `chip chip-slo-${status}`, p95Text));
    group.appendChild(el(
        'span',
        'chip',
        isNumber(record.connection_count) ? `활성 연결 ${record.connection_count}` : `활성 연결 ${NO_DATA}`,
    ));
    group.appendChild(el('span', 'chip', `에러율 ${fmtPct(record.error_rate)}`));
    group.appendChild(el('span', 'chip', `TPS ${fmtTps(record.tps)}`));

    const resourcesUnmeasured = (conditions && conditions.resource_metrics_collected === false)
        || record.cpu_pct === UNMEASURED_LABEL;
    if (resourcesUnmeasured) {
        group.appendChild(el('span', 'chip chip-muted', `판단 제외: CPU·메모리 (${UNMEASURED_LABEL})`));
    }

    return group;
}

/** 저신뢰 확인 게이트(#83)가 걸렸던 승인 요청 표시 */
function lowConfidenceBadge(report) {
    const threshold = report.low_confidence_threshold;
    const text = isNumber(threshold)
        ? `신뢰도 기준 미만 (< ${Math.round(threshold * 100)}%)`
        : '신뢰도 기준 미만';
    return el('span', 'badge badge-danger', text);
}

function singleRoundNote(report) {
    switch (report.end_reason) {
        case 'rejected':
            return '스케일링을 실행하지 않아 비교할 재측정이 없습니다 (사용자 거절).';
        case 'no_scaling_proposed':
            return '스케일링 미제안 — 재측정 없음.';
        case 'failed':
            return '재측정 전에 실행이 끝났습니다 (실패).';
        default:
            return '재측정이 없습니다.';
    }
}


// ---------------------------------------------------------------------
// 결과 패널 비우기 (view_switch.js clearReport)
// ---------------------------------------------------------------------

function resetResultPanel() {
    const panel = document.getElementById('result-panel');
    const body = document.getElementById('result-body');
    if (body) body.replaceChildren();
    if (panel) panel.classList.add('hidden');
}

/*
 * 실행 결과 패널 (docs/03 Phase 4, #75)
 *
 * /api/v1/agent/report/{task_id} 응답만으로 "뭐가 문제였고, 뭘 했고, 나아졌나"를 그린다.
 * - 화면 문장은 전부 측정값으로 만든 템플릿이다. LLM 문장은 "원문 보기"로 접어서만 보여준다.
 * - 목업·샘플 데이터 없음. 값이 없으면 "측정값 없음"을 표시한다.
 * - 외부 라이브러리·CDN 없음. 차트는 inline SVG로 그린다 (시연장 네트워크 리스크, docs/03 금지 사항).
 * - LLM 문자열은 textContent로만 넣는다 (innerHTML 사용 안 함).
 *
 * 표시 규칙
 * - 전/후 = 첫 라운드 vs 마지막 라운드. 중간 라운드는 차트 추이로만 보인다.
 * - SLO 판정: P95 < SLO 충족, P95 == SLO 경계, P95 > SLO 미충족.
 *   Locust는 응답시간을 반올림해 기록하므로 SLO와 같은 값은 실제로는 SLO를 조금 넘었을 수 있다 (docs/02 ISSUE-10).
 * - 자릿수: TPS 소수 1자리, P95 정수 ms, 에러율 % 소수 2자리. 변화량은 원본값으로 계산한 뒤 표시할 때만 반올림한다.
 * - 동시 가상 사용자 수(target_tps)는 처리량 목표가 아니다 (docs/02 ISSUE-11). 처리량은 측정값 tps만 쓴다.
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
// 요약 배너 문구 (측정값 템플릿, LLM 문장 사용 안 함)
// ---------------------------------------------------------------------

function buildSummary(report) {
    const history = report.measurement_history || [];
    const slo = report.conditions ? report.conditions.p95_slo_ms : null;
    const endReason = report.end_reason;
    const failed = endReason === 'failed' || report.outcome === 'failed';

    if (history.length === 0) {
        const parts = [failed ? '실행 실패' : '실행 결과', NO_DATA];
        if (failed && report.error) parts.push(shorten(report.error, 80));
        return { tone: failed ? 'danger' : 'neutral', icon: failed ? '❌' : 'ℹ️', text: parts.join(' · ') };
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

    // SLO 판정별 배너 색: 충족 초록 / 경계 노랑 / 미충족 주황
    const sloTone = { met: 'success', boundary: 'boundary', exceeded: 'warning', unknown: 'neutral' }[status];
    const sloIcon = { met: '✅', boundary: '⚠', exceeded: '⚠', unknown: 'ℹ️' }[status];

    switch (endReason) {
        case 'scaled':
            return { tone: sloTone, icon: sloIcon, text: [p95Part, sloPart, serverPart].join(' · ') };

        case 'no_further_scaling':
            return {
                tone: sloTone,
                icon: sloIcon,
                text: [p95Part, sloPart, serverPart, '재진단: 추가 스케일링 미제안'].join(' · '),
            };

        case 'no_scaling_proposed': {
            // SLO를 충족했을 때만 "불필요"라고 쓴다. 미충족·경계면 "미제안" (결론 배지와 같은 규칙).
            // SLO를 넘었는데 제안하지 않은 경우도 그대로 보여준다 (docs/02 ISSUE-5 관찰 1)
            const lead = status === 'met' ? '스케일링 불필요 판단' : '스케일링 미제안';
            const tone = status === 'met' ? 'info' : sloTone;
            return { tone, icon: 'ℹ️', text: [lead, p95Part, sloPart, serverPart].join(' · ') };
        }

        case 'rejected': {
            const plan = planText(lastOf(report.approvals));
            const target = plan ? `(${plan})` : '';
            const lead = multi ? `추가 스케일링 제안${target} 거절` : `스케일링 제안${target} 거절`;
            return { tone: 'warning', icon: '⛔', text: [lead, p95Part, sloPart, serverPart].join(' · ') };
        }

        case 'failed': {
            const parts = ['실행 실패', p95Part, sloPart, serverPart];
            if (report.error) parts.push(shorten(report.error, 80));
            return { tone: 'danger', icon: '❌', text: parts.join(' · ') };
        }

        default:
            return { tone: 'neutral', icon: 'ℹ️', text: [p95Part, sloPart, serverPart].join(' · ') };
    }
}


// ---------------------------------------------------------------------
// DOM 헬퍼
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

function sectionTitle(text) {
    return el('h4', 'result-section-title', text);
}

/** LLM 원문을 접어서 보여준다. 비어 있는 항목은 뺀다. */
function rawDetails(summaryText, pairs) {
    const details = el('details', 'raw-details');
    details.appendChild(el('summary', null, summaryText));

    pairs.forEach(([label, text]) => {
        if (text === undefined || text === null || text === '') return;
        details.appendChild(el('div', 'raw-label', label));
        details.appendChild(el('p', 'raw-text', String(text)));
    });

    return details;
}


// ---------------------------------------------------------------------
// ④ 측정 조건
// ---------------------------------------------------------------------

function buildConditions(report) {
    const conditions = report.conditions;
    const box = el('div', 'result-conditions');
    box.appendChild(el('span', 'result-conditions-title', '측정 조건'));

    const items = el('div', 'result-conditions-items');

    if (!conditions) {
        items.appendChild(el('span', 'condition-item', '측정 조건 기록 없음'));
        box.appendChild(items);
        return box;
    }

    [
        `동시 가상 사용자 ${isNumber(conditions.virtual_users) ? `${conditions.virtual_users}명` : NO_DATA}`,
        `부하 ${isNumber(conditions.duration_sec) ? `${conditions.duration_sec}초` : NO_DATA}`,
        `P95 SLO ${isNumber(conditions.p95_slo_ms) ? `${conditions.p95_slo_ms}ms` : '기준 없음'}`,
        `시작 서버 ${fmtReplicas(conditions.start_replicas)}`,
    ].forEach(text => items.appendChild(el('span', 'condition-item', text)));

    box.appendChild(items);

    const meta = [];
    if (conditions.llm_model) meta.push(`LLM ${conditions.llm_model}`);
    meta.push(`결과 파일 ${report.result_file || '저장되지 않음'}`);

    const environment = conditions.environment || {};
    if (environment.code_version) {
        meta.push(`코드 ${environment.code_version}${environment.code_dirty ? ' (미커밋 변경 있음)' : ''}`);
    }

    box.appendChild(el('div', 'result-conditions-meta', meta.join(' · ')));
    return box;
}


// ---------------------------------------------------------------------
// ② AI 판단 카드
// ---------------------------------------------------------------------

/** 진단 라운드의 측정 레코드로 계산한 P95 SLO 판정 (met / boundary / exceeded / unknown) */
function diagnosisSloState(diagnosis, report) {
    const record = findByRound((report && report.measurement_history) || [], diagnosis.round);
    const slo = report && report.conditions ? report.conditions.p95_slo_ms : null;
    return sloStatus(record ? record.latency_p95 : null, slo);
}

/**
 * 결론 배지. sloState는 그 진단 라운드의 P95 SLO 판정(sloStatus 결과)이다.
 * 스케일링을 제안하지 않았을 때 SLO를 충족한 경우에만 "불필요"라고 쓴다.
 * 미충족·경계·판정 불가면 "미제안"이다 (필요 없다고 단정하지 않는다).
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

function severityBadge(severity) {
    const styles = {
        high: ['HIGH', 'badge-danger'],
        medium: ['MEDIUM', 'badge-warning'],
        low: ['LOW', 'badge-success'],
    };
    const [label, className] = styles[severity] || [severity || NO_DATA, 'badge-muted'];
    return el('span', `badge ${className}`, `심각도 ${label}`);
}

/** threshold(#83 저신뢰 확인 게이트 기준, /report low_confidence_threshold)가 있으면 막대에 기준선을 그린다. */
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

/** 진단 라운드의 측정값 칩. LLM이 실제로 무엇을 근거로 썼는지는 코드로 알 수 없어 "진단 입력 측정값"으로 부른다. */
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

function decisionLine(approval) {
    const styles = {
        approved: ['승인 ✔', 'decision-approved'],
        rejected: ['거절 ✖', 'decision-rejected'],
    };
    const [label, className] = styles[approval.decision] || ['응답 없음', 'decision-none'];
    // 저신뢰 확인 게이트(#83)에서 확인 체크 후 승인했으면 함께 표시한다
    const acknowledged = approval.low_confidence && approval.acknowledged && approval.decision === 'approved'
        ? ' (낮은 신뢰도 확인 후)'
        : '';
    return el('div', `decision-line ${className}`, `사용자 결정: ${label}${acknowledged}`);
}

function diagnosisRawDetails(diagnosis) {
    return rawDetails('LLM 진단 원문 보기', [
        ['원인 (cause)', diagnosis.cause],
        ['권장 조치 (recommendation)', diagnosis.recommendation],
    ]);
}

/** 저신뢰 확인 게이트(#83)가 걸렸던 승인 요청 표시 */
function lowConfidenceBadge(report) {
    const threshold = report.low_confidence_threshold;
    const text = isNumber(threshold)
        ? `신뢰도 기준 미만 (< ${Math.round(threshold * 100)}%)`
        : '신뢰도 기준 미만';
    return el('span', 'badge badge-danger', text);
}

function buildDiagnosisCard(diagnosis, approval, record, report) {
    const slo = report.conditions ? report.conditions.p95_slo_ms : null;
    const card = el('div', 'diagnosis-card');

    const head = el('div', 'diagnosis-head');
    head.appendChild(conclusionBadge(diagnosis, approval, diagnosisSloState(diagnosis, report)));
    head.appendChild(severityBadge(diagnosis.severity));
    if (approval && approval.low_confidence) head.appendChild(lowConfidenceBadge(report));
    head.appendChild(confidenceMeter(diagnosis.confidence, report.low_confidence_threshold));
    card.appendChild(head);

    card.appendChild(el('div', 'chip-group-title', '진단 입력 측정값'));
    card.appendChild(evidenceChips(record, slo, report.conditions));

    if (approval) card.appendChild(decisionLine(approval));

    card.appendChild(diagnosisRawDetails(diagnosis));
    return card;
}

function buildRevalidationRow(revalidation) {
    const row = el('div', 'followup-row');
    const round = revalidation.round ?? '-';

    if (revalidation.parse_error) {
        row.appendChild(el('div', 'followup-title', `라운드 ${round} 재검증 (LLM): 응답 해석 실패`));
        row.appendChild(rawDetails('응답 원문 보기', [['원문 (앞부분)', revalidation.raw]]));
        return row;
    }

    const verdict = revalidation.performance_improved ? '성능 개선 판단' : '개선 부족 판단';
    const extra = revalidation.additional_action_required ? ' · 추가 조치 필요' : '';
    row.appendChild(el('div', 'followup-title', `라운드 ${round} 재검증 (LLM 판단): ${verdict}${extra}`));
    row.appendChild(rawDetails('재검증 원문 보기', [
        ['요약 (summary)', revalidation.summary],
        ['추가 권장 조치', revalidation.recommended_action],
    ]));
    return row;
}

function buildReDiagnosisRow(diagnosis, approval, report = {}) {
    const row = el('div', 'followup-row');

    const head = el('div', 'followup-head');
    head.appendChild(el('span', 'followup-title', `라운드 ${diagnosis.round ?? '-'} 재진단`));
    head.appendChild(conclusionBadge(diagnosis, approval, diagnosisSloState(diagnosis, report)));
    head.appendChild(severityBadge(diagnosis.severity));
    if (approval && approval.low_confidence) head.appendChild(lowConfidenceBadge(report));
    head.appendChild(el('span', 'followup-meta', `신뢰도 ${fmtConfidence(diagnosis.confidence)}`));
    row.appendChild(head);

    if (approval) row.appendChild(decisionLine(approval));

    row.appendChild(diagnosisRawDetails(diagnosis));
    return row;
}

function buildDiagnosisSection(report) {
    const section = el('section', 'result-section');
    const diagnoses = report.diagnoses || [];
    const approvals = report.approvals || [];
    const history = report.measurement_history || [];

    if (diagnoses.length === 0) {
        section.appendChild(sectionTitle('🧠 AI 판단'));
        section.appendChild(el('p', 'result-empty', 'AI 진단 결과 없음 (진단 전에 실행이 끝났습니다)'));
        return section;
    }

    // 문제가 무엇이었는지 = 최초 진단. 이후 라운드는 재검증 → 재진단 순서로 아래에 붙인다.
    const initial = diagnoses[0];
    section.appendChild(sectionTitle(`🧠 AI 판단 (라운드 ${initial.round ?? '-'})`));
    section.appendChild(buildDiagnosisCard(
        initial,
        findByRound(approvals, initial.round),
        findByRound(history, initial.round),
        report,
    ));

    const followUps = [];
    (report.revalidations || []).forEach(revalidation => {
        followUps.push({ round: revalidation.round ?? 0, order: 0, node: buildRevalidationRow(revalidation) });
    });
    diagnoses.slice(1).forEach(diagnosis => {
        followUps.push({
            round: diagnosis.round ?? 0,
            order: 1,
            node: buildReDiagnosisRow(diagnosis, findByRound(approvals, diagnosis.round), report),
        });
    });
    followUps
        .sort((a, b) => (a.round - b.round) || (a.order - b.order))
        .forEach(item => section.appendChild(item.node));

    return section;
}


// ---------------------------------------------------------------------
// ③ 전/후 비교 카드 + 라운드별 막대 차트
// ---------------------------------------------------------------------

function compareCard(title, beforeText, afterText, delta, note) {
    const card = el('div', 'metric-card');
    card.appendChild(el('div', 'metric-title', title));

    const values = el('div', 'metric-values');
    values.append(
        el('span', 'metric-before', beforeText),
        el('span', 'metric-arrow', '→'),
        el('span', 'metric-after', afterText),
    );
    card.appendChild(values);

    if (delta) card.appendChild(el('div', `metric-delta delta-${delta.tone}`, delta.text));
    if (note) card.appendChild(note);
    return card;
}

function singleCard(title, valueText, note) {
    const card = el('div', 'metric-card');
    card.appendChild(el('div', 'metric-title', title));
    card.appendChild(el('div', 'metric-values', valueText));
    if (note) card.appendChild(note);
    return card;
}

function sloNote(p95, slo) {
    const status = sloStatus(p95, slo);
    return el('div', `metric-slo chip-slo-${status}`, sloText(slo, status));
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

/**
 * 라운드별 막대 차트 (inline SVG).
 * bars: [{ label, sublabel, value, tone }], refLine: { value, label } | null
 */
function buildBarChart({ title, bars, refLine, format }) {
    const figure = el('figure', 'chart');
    figure.appendChild(el('figcaption', 'chart-title', title));

    const measured = bars.filter(bar => isNumber(bar.value));
    if (measured.length === 0) {
        figure.appendChild(el('p', 'result-empty', NO_DATA));
        return figure;
    }

    const width = 360;
    const height = 220;
    const padLeft = 12;
    const padRight = refLine ? 78 : 12;   // 기준선 라벨 자리
    const padTop = 26;
    const padBottom = 44;
    const plotWidth = width - padLeft - padRight;
    const plotHeight = height - padTop - padBottom;
    const baseY = padTop + plotHeight;

    const refValue = refLine && isNumber(refLine.value) ? refLine.value : 0;
    const maxValue = Math.max(...measured.map(bar => bar.value), refValue) * 1.1 || 1;
    const toY = value => baseY - (value / maxValue) * plotHeight;

    const slot = plotWidth / bars.length;
    const barWidth = Math.min(56, slot * 0.55);

    const svg = svgEl('svg', {
        viewBox: `0 0 ${width} ${height}`,
        class: 'chart-svg',
        role: 'img',
        'aria-label': title,
    });

    svg.appendChild(svgEl('line', { x1: padLeft, x2: padLeft + plotWidth, y1: baseY, y2: baseY, class: 'chart-axis' }));

    bars.forEach((bar, index) => {
        const centerX = padLeft + slot * index + slot / 2;

        if (isNumber(bar.value)) {
            const top = toY(bar.value);
            svg.appendChild(svgEl('rect', {
                x: centerX - barWidth / 2,
                y: top,
                width: barWidth,
                height: Math.max(baseY - top, 0),
                rx: 3,
                class: `chart-bar bar-${bar.tone || 'neutral'}`,
            }));
            svg.appendChild(svgEl('text', { x: centerX, y: top - 6, 'text-anchor': 'middle', class: 'chart-value' }, format(bar.value)));
        } else {
            svg.appendChild(svgEl('text', { x: centerX, y: baseY - 6, 'text-anchor': 'middle', class: 'chart-nodata' }, NO_DATA));
        }

        svg.appendChild(svgEl('text', { x: centerX, y: baseY + 17, 'text-anchor': 'middle', class: 'chart-label' }, bar.label));
        svg.appendChild(svgEl('text', { x: centerX, y: baseY + 33, 'text-anchor': 'middle', class: 'chart-sublabel' }, bar.sublabel));
    });

    if (refLine && isNumber(refLine.value)) {
        const refY = toY(refLine.value);
        svg.appendChild(svgEl('line', { x1: padLeft, x2: padLeft + plotWidth + 4, y1: refY, y2: refY, class: 'chart-ref' }));
        svg.appendChild(svgEl('text', { x: padLeft + plotWidth + 8, y: refY + 4, class: 'chart-ref-label' }, refLine.label));
    }

    figure.appendChild(svg);
    return figure;
}

function buildCharts(history, slo) {
    const row = el('div', 'chart-row');
    const labelOf = record => `R${record.round ?? '-'}`;
    const sublabelOf = record => `서버 ${fmtReplicas(record.replicas)}`;
    const toneOfP95 = record => ({ met: 'good', boundary: 'boundary', exceeded: 'bad' }[sloStatus(record.latency_p95, slo)] || 'neutral');

    row.appendChild(buildBarChart({
        title: 'P95 응답 시간 (ms) · 점선 = SLO',
        bars: history.map(record => ({
            label: labelOf(record),
            sublabel: sublabelOf(record),
            value: record.latency_p95,
            tone: toneOfP95(record),
        })),
        refLine: isNumber(slo) ? { value: slo, label: `SLO ${slo}ms` } : null,
        format: value => `${Math.round(value)}ms`,
    }));

    row.appendChild(buildBarChart({
        title: '처리량 TPS (초당 요청 수)',
        bars: history.map(record => ({
            label: labelOf(record),
            sublabel: sublabelOf(record),
            value: record.tps,
            tone: 'tps',
        })),
        refLine: null,
        format: value => value.toFixed(1),
    }));

    return row;
}

function buildComparisonSection(report) {
    const section = el('section', 'result-section');
    const history = report.measurement_history || [];
    const slo = report.conditions ? report.conditions.p95_slo_ms : null;

    if (history.length === 0) {
        section.appendChild(sectionTitle('📈 전/후 비교'));
        section.appendChild(el('p', 'result-empty', NO_DATA));
        return section;
    }

    const first = history[0];
    const last = history[history.length - 1];
    const grid = el('div', 'metric-grid');

    if (history.length >= 2) {
        section.appendChild(sectionTitle(`📈 전/후 비교 (라운드 ${first.round ?? '-'} → 라운드 ${last.round ?? '-'})`));

        grid.append(
            compareCard('TPS', fmtTps(first.tps), fmtTps(last.tps),
                describeDelta(first.tps, last.tps, { digits: 1, higherIsBetter: true })),
            compareCard('P95 응답 시간', fmtMs(first.latency_p95), fmtMs(last.latency_p95),
                describeDelta(first.latency_p95, last.latency_p95, { digits: 0, unit: 'ms', higherIsBetter: false }),
                sloNote(last.latency_p95, slo)),
            compareCard('에러율', fmtPct(first.error_rate), fmtPct(last.error_rate),
                describeDelta(first.error_rate, last.error_rate, { digits: 2, unit: '%p', scale: 100, higherIsBetter: false })),
            compareCard('서버 수', fmtReplicas(first.replicas), fmtReplicas(last.replicas),
                describeDelta(first.replicas, last.replicas, { digits: 0, unit: '대' })),
        );
        section.appendChild(grid);

        if (history.length > 2) {
            section.appendChild(el('p', 'result-note', '중간 라운드는 아래 차트의 추이로 표시합니다.'));
        }
    } else {
        section.appendChild(sectionTitle(`📈 측정 결과 (라운드 ${first.round ?? '-'}, 1회)`));

        grid.append(
            singleCard('TPS', fmtTps(first.tps)),
            singleCard('P95 응답 시간', fmtMs(first.latency_p95), sloNote(first.latency_p95, slo)),
            singleCard('에러율', fmtPct(first.error_rate)),
            singleCard('서버 수', fmtReplicas(first.replicas)),
        );
        section.appendChild(grid);
        section.appendChild(el('p', 'result-note', singleRoundNote(report)));
    }

    section.appendChild(buildCharts(history, slo));
    section.appendChild(el(
        'p',
        'result-note',
        'TPS·P95·에러율은 Locust 전체 엔드포인트 합산(Aggregated) 측정값입니다.',
    ));

    return section;
}


// ---------------------------------------------------------------------
// 진입점
// ---------------------------------------------------------------------

function resetResultPanel() {
    const panel = document.getElementById('result-panel');
    const body = document.getElementById('result-body');
    if (body) body.replaceChildren();
    if (panel) panel.classList.add('hidden');
}

function renderResultPanel(report) {
    const panel = document.getElementById('result-panel');
    const body = document.getElementById('result-body');
    if (!panel || !body) return;

    body.replaceChildren();

    if (!report) {
        panel.classList.add('hidden');
        return;
    }

    if (report.forced_scaling) {
        body.appendChild(el(
            'div',
            'result-alert',
            '⚠ 디버그 강제 실행 — 발표 수치 아님 (force_scaling이 LLM 판단을 덮어썼습니다)',
        ));
    }

    const summary = buildSummary(report);
    const banner = el('div', `result-banner tone-${summary.tone}`);
    banner.append(
        el('span', 'result-banner-icon', summary.icon),
        el('span', 'result-banner-text', summary.text),
    );
    body.appendChild(banner);

    body.appendChild(buildConditions(report));
    body.appendChild(buildDiagnosisSection(report));
    body.appendChild(buildComparisonSection(report));

    panel.classList.remove('hidden');
}

/*
 * 기본 보기(C 미니멀형)·상세 보기(A 관제 콘솔형) 결과 패널 공용 코드 (디자인 개편 #93)
 *
 * - 뷰모델: 계산은 result_panel.js 함수를 그대로 부른다 (buildSummary, sloStatus, sloText, describeDelta, fmt*, planText,
 *   conclusionBadge, evidenceChips, confidenceMeter, singleRoundNote …). 두 보기의 숫자 자릿수·판정 규칙이 같아서
 *   "결과 파일 수치 = 화면 수치" 대조가 두 보기에서 똑같이 성립한다.
 * - 결론 한 줄(headlineOf)은 측정값·필드로만 만든다 (LLM 문장 없음, 인과를 단정하지 않는다).
 * - SLO·P95 풀이(glossaryOf)는 마우스 올리기가 아니라 화면에 보이게 둔다 (모바일 포함).
 * - 차트: inline SVG, 외부 라이브러리 없음. 색은 보기별 스타일 시트의 CSS 변수(--ig-*)로 정한다.
 *   측정 회차 색은 순서가 의미라서 한 색상의 명도 단계(--ig-round-ramp, 앞 회차 → 뒤 회차)로 칠한다.
 * - docs/03 디자인 개편 대기 목록: 보기 좋은 눈금 간격(niceScale), 초당 그래프 마지막 구간 불완전 표시, LLM 원문의 긴 소수 표시.
 * - 목업 데이터 없음. 값이 없으면 "측정값 없음"을 표시한다. LLM 문자열은 textContent로만 넣는다.
 */
const IGRefresh = (function () {

    const SEVERITY_TEXT = { high: '높음', medium: '보통', low: '낮음' };

    const DECISION_TEXT = { approved: '승인', rejected: '거절' };

    const SLO_SHORT = { met: '충족', boundary: '경계(= SLO)', exceeded: '미충족', unknown: '판정 불가' };

    // 결론 한 줄 앞부분 (SLO 판정)
    const SLO_PHRASE = {
        met: '응답 목표(SLO) 충족',
        boundary: '응답 목표(SLO) 경계',
        exceeded: '응답 목표(SLO) 미충족',
        unknown: '응답 목표(SLO) 판정 불가',
    };

    // SLO 판정 → 상태 색 이름 (--ig-good / --ig-boundary / --ig-bad)
    const SLO_TONE = { met: 'good', boundary: 'boundary', exceeded: 'bad', unknown: 'muted' };

    const BADGE_TONE = { 'badge-danger': 'bad', 'badge-warning': 'warn', 'badge-info': 'info', 'badge-success': 'good', 'badge-muted': 'muted' };

    const GRAFANA_URL = 'http://localhost:3000';

    const LONG_DECIMAL = /-?\d+\.\d{3,}/g;

    // 부하 중 그래프 필드가 없거나(이 기능 추가 전 결과 파일) null이면(수집 실패) 쓰는 표기
    const NO_GRAPH_DATA = `${NO_DATA} (이 기능 추가 전 실행이거나 수집 실패)`;

    // 판단 이유 발췌: LLM 원인(cause)에서 스케일링 판단을 말하는 문장을 찾는 말
    const REASON_KEYWORD = /스케일링|확장|증설|컨테이너 수/;

    // 부하 시나리오가 부르는 테스트 서버의 요청 종류. 이름만으로는 처음 보는 사람이 알 수 없어 짧게 덧붙인다.
    // 근거: infra/locust/locustfile.py(가중치 /light 6, /heavy 3, /flaky 1, /health 1),
    //       infra/target-server/main.py(/light 10ms, /heavy 동시 5개 제한·0.2~0.6초, /flaky 5% 확률 500, /health 즉시 응답)
    const ENDPOINT_NOTE = {
        '/light': '가벼운 요청',
        '/heavy': '무거운 작업',
        '/flaky': '가끔 실패하는 요청',
        '/health': '상태 확인',
    };

    const ENDPOINT_NOTE_LINE = '요청 종류는 테스트 서버가 받는 요청입니다. '
        + '/light는 가벼운 요청(10ms), /heavy는 무거운 작업(동시 5개까지만 처리, 0.2~0.6초), '
        + '/flaky는 가끔 실패하는 요청(5% 오류), /health는 상태 확인입니다.';


    // -----------------------------------------------------------------
    // 기본 도구
    // -----------------------------------------------------------------

    function cssVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    /** 측정 회차 색. 램프에서 고르게 뽑고, 회차가 하나면 가장 진한 색을 쓴다. */
    function roundColors(count) {
        const ramp = cssVar('--ig-round-ramp').split(',').map(value => value.trim()).filter(Boolean);
        const palette = ramp.length > 0 ? ramp : ['#6b7280'];
        if (count <= 1) return [palette[palette.length - 1]];

        return Array.from({ length: count }, (_, index) => {
            const position = Math.round((index * (palette.length - 1)) / (count - 1));
            return palette[Math.min(position, palette.length - 1)];
        });
    }

    function statusColor(tone) {
        return cssVar(`--ig-${tone}`) || cssVar('--ig-text-faint');
    }

    function node(tag, className, text) {
        return el(tag, className, text);   // result_panel.js
    }

    function instanceShortName(instance) {
        const host = String(instance).split(':')[0];
        return `서버 .${host.split('.').pop()}`;
    }

    function shortNumber(text) {
        const value = Number(text);
        if (!Number.isFinite(value)) return text;
        if (Math.abs(value) >= 1) return value.toFixed(2);
        if (value === 0) return '0';
        return String(Number(value.toPrecision(3)));
    }

    /** LLM 문장을 넣는다. 소수점 셋째 자리 이상 숫자만 줄여 보여 주고 원문 값은 title로 둔다. 줄인 개수를 반환한다. */
    function appendRichText(parent, text) {
        const value = String(text);
        let last = 0;
        let shortened = 0;

        value.replace(LONG_DECIMAL, (match, offset) => {
            if (offset > last) parent.appendChild(document.createTextNode(value.slice(last, offset)));
            const span = node('span', 'ig-shortnum', shortNumber(match));
            span.title = `원문: ${match}`;
            parent.appendChild(span);
            last = offset + match.length;
            shortened += 1;
            return match;
        });

        if (last < value.length) parent.appendChild(document.createTextNode(value.slice(last)));
        return shortened;
    }

    /** LLM 원문 접힘 영역. pairs: [[라벨, 문장], …] (빈 항목은 뺀다) */
    function rawDetails(summaryText, pairs, key) {
        const details = node('details', 'ig-raw');
        if (key) details.dataset.key = key;
        details.appendChild(node('summary', null, summaryText));

        let shortened = 0;
        pairs.forEach(([label, text]) => {
            if (text === undefined || text === null || text === '') return;
            details.appendChild(node('div', 'ig-raw-label', label));
            const paragraph = node('p', 'ig-raw-text');
            shortened += appendRichText(paragraph, text);
            details.appendChild(paragraph);
        });

        if (shortened > 0) {
            details.appendChild(node(
                'p',
                'ig-raw-note',
                '긴 소수는 소수 둘째 자리(1 미만은 유효숫자 3자리)로 줄여 표시했습니다. 점선 밑줄에 마우스를 올리면 원문 값이 보입니다.',
            ));
        }
        return details;
    }

    function grafanaLink(text) {
        const link = node('a', 'ig-grafana', text || 'Grafana 대시보드 열기');
        link.href = GRAFANA_URL;
        link.target = '_blank';
        link.rel = 'noopener';
        return link;
    }

    function empty(text) {
        return node('p', 'ig-empty', text || NO_DATA);
    }

    function note(text) {
        return node('p', 'ig-note', text);
    }

    /** 붙어 있는 도구 막대 아래로 대상을 스크롤한다 */
    function scrollBelowToolbar(target) {
        if (!target) return;
        const toolbar = document.getElementById('ig-toolbar');
        const offset = (toolbar ? toolbar.getBoundingClientRect().height : 0) + 12;
        const top = target.getBoundingClientRect().top + window.scrollY - offset;
        window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
    }


    // -----------------------------------------------------------------
    // 뷰모델
    // -----------------------------------------------------------------

    function decisionText(approval) {
        if (!approval) return null;
        const base = DECISION_TEXT[approval.decision] || '응답 없음';
        const acknowledged = approval.low_confidence && approval.acknowledged && approval.decision === 'approved'
            ? ' (낮은 신뢰도 확인 후)'
            : '';
        return `${base}${acknowledged}`;
    }

    function conclusionOf(diagnosis, approval, report) {
        const badge = conclusionBadge(diagnosis, approval, diagnosisSloState(diagnosis, report));   // result_panel.js
        const toneClass = [...badge.classList].find(name => BADGE_TONE[name]);
        return { text: badge.textContent, tone: BADGE_TONE[toneClass] || 'muted' };
    }

    /** 진단 입력 측정값. 문구는 result_panel.js evidenceChips와 같다. */
    function evidenceOf(record, slo, conditions) {
        const group = evidenceChips(record, slo, conditions);   // result_panel.js
        return [...group.children].map(chip => {
            const match = chip.className.match(/chip-slo-(\w+)/);
            return {
                text: chip.textContent,
                sloState: match ? match[1] : null,
                excluded: chip.classList.contains('chip-muted'),
            };
        });
    }

    function revalidationOf(revalidation) {
        if (revalidation.parse_error) {
            return {
                kind: 'revalidation',
                round: revalidation.round,
                verdict: '응답 해석 실패',
                extra: null,
                tone: 'muted',
                pairs: [['응답 원문 (앞부분)', revalidation.raw]],
            };
        }

        return {
            kind: 'revalidation',
            round: revalidation.round,
            verdict: revalidation.performance_improved ? '성능 개선 판단' : '개선 부족 판단',
            extra: revalidation.additional_action_required ? '추가 조치 필요' : '추가 조치 불필요',
            tone: revalidation.performance_improved ? 'good' : 'warn',
            pairs: [['요약 (summary)', revalidation.summary], ['추가 권장 조치', revalidation.recommended_action]],
        };
    }

    function model(report, meta) {
        const history = Array.isArray(report.measurement_history) ? report.measurement_history : [];
        const conditions = report.conditions || null;
        const slo = conditions ? conditions.p95_slo_ms : null;
        const approvals = Array.isArray(report.approvals) ? report.approvals : [];
        const diagnoses = Array.isArray(report.diagnoses) ? report.diagnoses : [];
        const revalidations = Array.isArray(report.revalidations) ? report.revalidations : [];
        const scalingResults = Array.isArray(report.scaling_results) ? report.scaling_results : [];
        const first = history[0] || null;
        const last = lastOf(history);
        const colors = roundColors(history.length);

        const rounds = history.map((record, index) => {
            const state = sloStatus(record.latency_p95, slo);
            const round = record.round ?? index + 1;
            return {
                index,
                record,
                round,
                name: `측정 ${round}`,
                color: colors[index],
                replicas: record.replicas,
                replicasText: fmtReplicas(record.replicas),
                tpsText: fmtTps(record.tps),
                p95Text: fmtMs(record.latency_p95),
                errText: fmtPct(record.error_rate),
                sloState: state,
                sloShort: SLO_SHORT[state],
                sloText: sloText(slo, state),
            };
        });

        const diagnosisItems = diagnoses.map((diagnosis, index) => {
            const approval = findByRound(approvals, diagnosis.round);
            const record = findByRound(history, diagnosis.round);
            return {
                kind: 'diagnosis',
                initial: index === 0,
                round: diagnosis.round,
                diagnosis,
                approval,
                record,
                conclusion: conclusionOf(diagnosis, approval, report),
                severityText: SEVERITY_TEXT[diagnosis.severity] || diagnosis.severity || NO_DATA,
                confidenceText: fmtConfidence(diagnosis.confidence),
                decisionText: decisionText(approval),
                lowConfidence: Boolean(approval && approval.low_confidence),
                evidence: evidenceOf(record, slo, conditions),
                pairs: [['원인 (cause)', diagnosis.cause], ['권장 조치 (recommendation)', diagnosis.recommendation]],
            };
        });

        // 최초 진단 → 이후 라운드는 재검증 → 재진단 순서 (기존 결과 패널과 같은 순서)
        const followUps = [
            ...revalidations.map(item => ({ order: 0, item: revalidationOf(item) })),
            ...diagnosisItems.slice(1).map(item => ({ order: 1, item })),
        ].sort((a, b) => ((a.item.round ?? 0) - (b.item.round ?? 0)) || (a.order - b.order));
        const timeline = [...diagnosisItems.slice(0, 1), ...followUps.map(entry => entry.item)];

        // 라운드 사이 단계: 그 라운드의 승인 요청(제안·결정)과 스케일링 결과
        const steps = approvals.map(approval => {
            const diagnosis = diagnosisItems.find(item => item.round === approval.round) || null;
            const scaling = findByRound(scalingResults, approval.round);
            return {
                round: approval.round,
                approval,
                plan: planText(approval),
                forced: approval.source === 'forced',
                confidenceText: diagnosis ? diagnosis.confidenceText : null,
                decision: approval.decision,
                decisionText: decisionText(approval),
                lowConfidence: Boolean(approval.low_confidence),
                scaling,
                scalingFailed: Boolean(scaling && scaling.success === false),
            };
        });

        const confidences = diagnoses.map(item => item.confidence).filter(isNumber);
        const resourcesUnmeasured = (conditions && conditions.resource_metrics_collected === false)
            || history.some(record => record.cpu_pct === UNMEASURED_LABEL);
        const environment = (conditions && conditions.environment) || {};

        const vm = {
            report,
            meta: meta || { source: 'live' },
            saved: Boolean(meta && meta.source === 'saved'),
            history,
            rounds,
            first,
            last,
            multi: history.length >= 2,
            slo,
            conditions,
            summary: buildSummary(report),   // result_panel.js
            endReason: report.end_reason,
            failed: report.end_reason === 'failed' || report.outcome === 'failed',
            forced: Boolean(report.forced_scaling),
            error: report.error ? shorten(report.error, 160) : null,
            approvals,
            diagnoses: diagnosisItems,
            timeline,
            steps,
            scalingResults,
            lastState: last ? sloStatus(last.latency_p95, slo) : 'unknown',
            counts: {
                proposals: approvals.length,
                approved: approvals.filter(item => item.decision === 'approved').length,
                rejected: approvals.filter(item => item.decision === 'rejected').length,
                noResponse: approvals.filter(item => !item.decision).length,
            },
            // 신뢰도는 측정값이 아니라서 "측정값 없음" 대신 "없음"
            confidenceRange: confidences.length === 0
                ? '없음'
                : (Math.min(...confidences) === Math.max(...confidences)
                    ? fmtConfidence(confidences[0])
                    : `${fmtConfidence(Math.min(...confidences))}~${fmtConfidence(Math.max(...confidences))}`),
            resourcesUnmeasured,
            threshold: report.low_confidence_threshold,
            // 측정 조건은 결과를 이해하는 데 필요해서 두 보기 모두 접지 않고 보여 준다
            conditionItems: conditions ? [
                ['동시 가상 사용자 수', isNumber(conditions.virtual_users) ? `${conditions.virtual_users}명` : NO_DATA],
                ['부하 시간', isNumber(conditions.duration_sec) ? `${conditions.duration_sec}초` : NO_DATA],
                ['응답 목표(SLO)', isNumber(conditions.p95_slo_ms) ? `${conditions.p95_slo_ms}ms` : '기준 없음'],
                ['시작 서버 대수', fmtReplicas(conditions.start_replicas)],
            ] : [],
            metaItems: [
                ...(conditions && conditions.llm_model ? [['판단 모델', conditions.llm_model]] : []),
                ['결과 파일', report.result_file || '저장되지 않음'],
                ...(environment.code_version
                    ? [['코드', `${environment.code_version}${environment.code_dirty ? ' (미커밋 변경 있음)' : ''}`]]
                    : []),
            ],
        };

        vm.conditionLine = conditions
            ? vm.conditionItems.map(([label, value]) => `${label} ${value}`).join(' · ')
            : '측정 조건 기록 없음';
        vm.verdict = verdictOf(vm);
        vm.headline = headlineOf(vm);
        vm.glossary = glossaryOf(vm);
        vm.deltas = vm.multi ? deltasOf(first, last) : null;
        return vm;
    }

    function deltasOf(first, last) {
        return {
            tps: describeDelta(first.tps, last.tps, { digits: 1, higherIsBetter: true }),
            p95: describeDelta(first.latency_p95, last.latency_p95, { digits: 0, unit: 'ms', higherIsBetter: false }),
            err: describeDelta(first.error_rate, last.error_rate, { digits: 2, unit: '%p', scale: 100, higherIsBetter: false }),
            replicas: describeDelta(first.replicas, last.replicas, { digits: 0, unit: '대' }),
        };
    }

    /** 짧은 판정 (결론 한 줄의 톤) */
    function verdictOf(vm) {
        if (vm.history.length === 0) {
            return vm.failed ? { text: '실행 실패', tone: 'bad' } : { text: '측정값 없음', tone: 'muted' };
        }
        if (vm.failed) return { text: '실행 실패', tone: 'bad' };
        if (vm.endReason === 'stream_closed') return { text: '연결 종료', tone: 'warn' };
        return {
            met: { text: 'SLO 충족', tone: 'good' },
            boundary: { text: 'SLO 경계', tone: 'boundary' },
            exceeded: { text: 'SLO 미충족', tone: 'bad' },
            unknown: { text: 'SLO 판정 불가', tone: 'muted' },
        }[vm.lastState];
    }

    /**
     * 결론 한 줄 (두 보기 공통). "판정 · 무슨 일이 있었나" 순서. 측정값·필드로만 만든다.
     * 예: "응답 목표(SLO) 미충족 · AI가 스케일링을 제안하지 않았습니다"
     */
    function headlineOf(vm) {
        const { history, first, last } = vm;

        if (history.length === 0) {
            if (vm.endReason === 'stream_closed') return '연결 종료 · 측정이 끝나기 전에 연결이 끊겼습니다';
            if (vm.failed) return '실행 실패 · 측정 전에 실행이 끝났습니다';
            return '측정값 없음';
        }

        const slo = SLO_PHRASE[vm.lastState];
        const met = vm.lastState === 'met';

        switch (vm.endReason) {
            case 'no_scaling_proposed':
                return met
                    ? `${slo} · AI가 스케일링이 필요 없다고 판단했습니다`
                    : `${slo} · AI가 스케일링을 제안하지 않았습니다`;
            case 'no_further_scaling':
                return met
                    ? `${slo} · 서버 ${fmtReplicas(last.replicas)}에서 AI가 추가 스케일링이 필요 없다고 판단했습니다`
                    : `${slo} · 서버 ${fmtReplicas(last.replicas)}에서 AI가 추가 스케일링을 제안하지 않았습니다`;
            case 'rejected': {
                const plan = planText(lastOf(vm.approvals));
                return `${slo} · AI의 스케일링 제안${plan ? `(서버 ${plan})` : ''}을 거절했습니다`;
            }
            case 'failed':
                return `실행 실패 · 측정 ${history.length}회 뒤 실행이 끝났습니다`;
            case 'stream_closed':
                return `연결 종료 · 측정 ${history.length}회 뒤 연결이 끊겼습니다`;
            default:
                if (vm.multi && isNumber(first.replicas) && isNumber(last.replicas) && first.replicas !== last.replicas) {
                    const verb = last.replicas > first.replicas ? '늘렸습니다' : '줄였습니다';
                    return `${slo} · 서버를 ${first.replicas}대에서 ${last.replicas}대로 ${verb}`;
                }
                return `${slo} · 서버 ${fmtReplicas(last.replicas)}`;
        }
    }

    /** 용어 풀이. [용어, 뜻] — 처음 보는 사람 기준으로 짧게. 마우스 올리기가 아니라 화면에 보이게 쓴다 */
    function glossaryOf(vm) {
        const slo = vm.slo;
        const seconds = isNumber(slo) ? `${Number((slo / 1000).toFixed(3))}초` : null;
        return {
            p95: ['P95', '느린 순서로 상위 5%를 뺀 응답 시간'],
            slo: isNumber(slo)
                ? [`SLO ${slo}ms`, `요청의 95%가 ${seconds} 안에 응답해야 한다는 목표`]
                : ['SLO', '요청의 95%가 정해진 시간 안에 응답해야 한다는 목표 (이 실행은 기준값 기록 없음)'],
            tps: ['처리량(TPS)', '1초에 처리한 요청 수'],
            replicas: ['서버 대수', '같은 테스트 서버를 몇 개 띄웠는지 (replica)'],
            rounds: ['측정 회차', '부하 테스트를 한 번 돌려 값을 잰 단위'],
            connections: ['활성 연결', '그 순간 서버가 처리 중이거나 기다리는 요청 수'],
            revalidation: ['재검증', '서버를 늘린 뒤 같은 조건으로 다시 측정해 앞 측정과 비교한 것'],
            evidence: ['진단 입력 측정값', 'AI가 판단할 때 받은 측정값'],
        };
    }

    function glossaryLine([term, meaning], className) {
        const line = node('p', className || 'ig-glossary');
        line.append(node('b', null, term), `: ${meaning}`);
        return line;
    }

    /**
     * 판단 이유 한 줄: LLM 원인(cause)에서 스케일링 판단을 말하는 마지막 문장을 그대로 발췌한다.
     * 요약·수정하지 않는다. 그런 문장이 없으면 마지막 문장. 전체 원문은 상세 보기에 있다.
     */
    function causeExcerpt(text) {
        if (!text) return null;
        const sentences = String(text).split(/(?<=[.!?])\s+/).map(sentence => sentence.trim()).filter(Boolean);
        if (sentences.length === 0) return null;
        const matching = sentences.filter(sentence => REASON_KEYWORD.test(sentence));
        return matching.length > 0 ? matching[matching.length - 1] : sentences[sentences.length - 1];
    }


    // -----------------------------------------------------------------
    // 차트 공통
    // -----------------------------------------------------------------

    function niceStep(raw) {
        if (!(raw > 0)) return 1;
        const exponent = Math.floor(Math.log10(raw));
        const base = 10 ** exponent;
        const fraction = raw / base;
        const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
        return nice * base;
    }

    function decimalsOf(step) {
        const text = String(Number(step.toPrecision(6)));
        const dot = text.indexOf('.');
        return dot < 0 ? 0 : text.length - dot - 1;
    }

    /** 0부터 시작하는 보기 좋은 눈금 (간격 1·2·2.5·5 × 10^k) */
    function niceScale(maxValue, target) {
        const max = maxValue > 0 ? maxValue * 1.04 : 1;
        const step = niceStep(max / (target || 4));
        const top = Math.ceil(max / step - 1e-9) * step;
        const decimals = decimalsOf(step);
        const ticks = [];
        for (let value = 0; value <= top + step / 2; value += step) {
            ticks.push(Number(value.toFixed(decimals)));
        }
        return { top, step, ticks, decimals };
    }

    function measureWidth(host) {
        let current = host;
        while (current && current.clientWidth === 0) current = current.parentElement;
        const width = current ? current.clientWidth : 640;
        // 접힌 영역 안이면 바깥 너비에서 안쪽 여백만큼 뺀다
        return Math.max(260, Math.floor(current === host ? width : width - 32));
    }

    /** 차트 틀. figure를 먼저 붙이고 실제 너비를 잰다. */
    function chartFrame(host, title, subtitle) {
        const figure = node('figure', 'igc');
        const caption = node('figcaption', 'igc-caption');
        caption.appendChild(node('span', 'igc-title', title));
        if (subtitle) caption.appendChild(node('span', 'igc-sub', subtitle));
        figure.appendChild(caption);
        host.appendChild(figure);

        const style = getComputedStyle(figure);
        const padding = (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
        const width = figure.clientWidth > 0 ? figure.clientWidth - padding : measureWidth(host);
        return { figure, width: Math.max(260, Math.floor(width)) };
    }

    function emptyChart(host, title, subtitle, text) {
        const { figure } = chartFrame(host, title, subtitle);
        figure.appendChild(node('p', 'igc-empty', text || NO_GRAPH_DATA));
        return figure;
    }

    function svgRoot(width, height, label) {
        return svgEl('svg', {
            viewBox: `0 0 ${width} ${height}`,
            width: '100%',
            role: 'img',
            'aria-label': label,
            class: 'igc-svg',
        });
    }

    /** 세로 막대: 값 쪽 끝만 4px 둥글게, 기준선 쪽은 각지게 */
    function columnPath(x, y, width, height) {
        const radius = Math.min(4, width / 2, height);
        if (height <= 0) return '';
        return `M${x},${y + height} L${x},${y + radius} Q${x},${y} ${x + radius},${y} `
            + `L${x + width - radius},${y} Q${x + width},${y} ${x + width},${y + radius} L${x + width},${y + height} Z`;
    }

    /** 가로 막대: 오른쪽 끝만 둥글게 */
    function barPath(x, y, width, height) {
        const radius = Math.min(4, height / 2, width);
        if (width <= 0) return '';
        return `M${x},${y} L${x + width - radius},${y} Q${x + width},${y} ${x + width},${y + radius} `
            + `L${x + width},${y + height - radius} Q${x + width},${y + height} ${x + width - radius},${y + height} L${x},${y + height} Z`;
    }

    function legend(items) {
        const box = node('div', 'igc-legend');
        items.forEach(item => {
            const entry = node('span', 'igc-legend-item');
            const key = node('span', `igc-key igc-key-${item.shape || 'box'}`);
            if (item.color) key.style.setProperty('--key-color', item.color);
            entry.append(key, node('span', null, item.label));
            box.appendChild(entry);
        });
        return box;
    }

    function refLabel(svg, x, y, text) {
        svg.appendChild(svgEl('text', { x, y: y - 5, 'text-anchor': 'end', class: 'igc-ref-label' }, text));
    }


    // -----------------------------------------------------------------
    // 선 그래프 (초당 처리량·P95, 측정 회차별 겹침)
    // -----------------------------------------------------------------

    /**
     * series: [{ label, color, points: [{ x, y(null이면 선을 끊음), incomplete }] }]
     * 불완전 구간(부하 종료 시점에 걸린 마지막 구간)은 점선·빈 점·옅은 배경으로 표시한다. 값은 지우지 않는다.
     */
    function lineChart(host, { title, subtitle, series, refLine, formatValue, formatTick, xUnit }) {
        const drawable = series.filter(item => item.points.some(point => isNumber(point.y)));
        if (drawable.length === 0) return emptyChart(host, title, subtitle);

        const { figure, width } = chartFrame(host, title, subtitle);
        const narrow = width < 480;
        const height = narrow ? 190 : 220;
        const pad = { left: 44, right: 10, top: 18, bottom: 26 };
        const plotWidth = width - pad.left - pad.right;
        const baseY = height - pad.bottom;
        const plotHeight = baseY - pad.top;

        const measured = drawable.flatMap(item => item.points.filter(point => isNumber(point.y)));
        const maxX = Math.max(1, ...measured.map(point => point.x));
        const refValue = refLine && isNumber(refLine.value) ? refLine.value : 0;
        const scale = niceScale(Math.max(...measured.map(point => point.y), refValue), narrow ? 3 : 4);
        const toX = x => pad.left + (x / maxX) * plotWidth;
        const toY = y => baseY - (y / scale.top) * plotHeight;

        const svg = svgRoot(width, height, title);

        scale.ticks.forEach(tick => {
            const y = toY(tick);
            svg.appendChild(svgEl('line', { x1: pad.left, x2: pad.left + plotWidth, y1: y, y2: y, class: tick === 0 ? 'igc-axis-line' : 'igc-grid' }));
            svg.appendChild(svgEl('text', { x: pad.left - 8, y: y + 4, 'text-anchor': 'end', class: 'igc-tick' }, formatTick(tick, scale.decimals)));
        });

        const xStep = Math.max(1, niceStep(maxX / (narrow ? 4 : 6)));
        for (let x = 0; x <= maxX + 1e-9; x += xStep) {
            svg.appendChild(svgEl('text', { x: toX(x), y: baseY + 16, 'text-anchor': 'middle', class: 'igc-tick' }, `${Math.round(x)}`));
        }

        // 불완전 구간 배경
        const incompleteXs = drawable.flatMap(item => item.points.filter(point => point.incomplete && isNumber(point.y)).map(point => point.x));
        if (incompleteXs.length > 0) {
            const start = Math.min(...incompleteXs);
            const bandLeft = Math.max(pad.left, toX(start) - (plotWidth / maxX) / 2);
            svg.appendChild(svgEl('rect', {
                x: bandLeft,
                y: pad.top,
                width: pad.left + plotWidth - bandLeft + 4,
                height: plotHeight,
                class: 'igc-incomplete-band',
            }));
            svg.appendChild(svgEl('text', { x: pad.left + plotWidth, y: pad.top - 6, 'text-anchor': 'end', class: 'igc-incomplete-label' }, '불완전 구간'));
        }

        if (refLine && isNumber(refLine.value)) {
            const y = toY(refLine.value);
            svg.appendChild(svgEl('line', { x1: pad.left, x2: pad.left + plotWidth, y1: y, y2: y, class: 'igc-ref' }));
            refLabel(svg, pad.left + plotWidth, y, refLine.label);
        }

        drawable.forEach(item => {
            let solid = '';
            let dashed = '';
            let penDown = false;
            let previous = null;

            item.points.forEach(point => {
                if (!isNumber(point.y)) {
                    penDown = false;
                    previous = null;
                    return;
                }
                const x = toX(point.x).toFixed(1);
                const y = toY(point.y).toFixed(1);

                if (point.incomplete) {
                    if (previous) dashed += `M${previous.x},${previous.y} L${x},${y} `;
                    penDown = false;
                } else {
                    solid += `${penDown ? 'L' : 'M'}${x},${y} `;
                    penDown = true;
                }
                previous = { x, y };
            });

            if (solid) svg.appendChild(svgEl('path', { d: solid.trim(), class: 'igc-line', stroke: item.color }));
            if (dashed) svg.appendChild(svgEl('path', { d: dashed.trim(), class: 'igc-line igc-line-incomplete', stroke: item.color }));

            item.points.forEach(point => {
                if (!point.incomplete || !isNumber(point.y)) return;
                svg.appendChild(svgEl('circle', { cx: toX(point.x), cy: toY(point.y), r: 4, class: 'igc-dot-open', stroke: item.color }));
            });
        });

        figure.appendChild(svg);
        attachCrosshair({ figure, svg, drawable, toX, maxX, pad, plotWidth, baseY, width, formatValue, xUnit });

        const legendItems = drawable.length >= 2
            ? drawable.map(item => ({ label: item.label, color: item.color, shape: 'line' }))
            : [];
        if (incompleteXs.length > 0) {
            legendItems.push({ label: '마지막 구간: 부하가 끝나는 시점이라 요청이 덜 잡힙니다 (앞 구간과 직접 비교하지 않음)', shape: 'dash' });
        }
        if (legendItems.length > 0) figure.appendChild(legend(legendItems));
        return figure;
    }

    /** 십자선 툴팁: 포인터에서 가장 가까운 x의 모든 회차 값을 보여 준다 (값은 범례·표·요약에도 있다) */
    function attachCrosshair({ figure, svg, drawable, toX, maxX, pad, plotWidth, baseY, width, formatValue, xUnit }) {
        const xs = [...new Set(drawable.flatMap(item => item.points.filter(point => isNumber(point.y)).map(point => point.x)))].sort((a, b) => a - b);
        if (xs.length === 0) return;

        const line = svgEl('line', { x1: 0, x2: 0, y1: pad.top, y2: baseY, class: 'igc-crosshair', visibility: 'hidden' });
        const hit = svgEl('rect', { x: pad.left, y: pad.top, width: plotWidth, height: baseY - pad.top, class: 'igc-hit' });
        svg.append(line, hit);

        const tip = node('div', 'igc-tip hidden');
        figure.appendChild(tip);

        function hide() {
            line.setAttribute('visibility', 'hidden');
            tip.classList.add('hidden');
        }

        hit.addEventListener('pointermove', event => {
            const box = svg.getBoundingClientRect();
            if (box.width === 0) return;
            const ratio = width / box.width;
            const pointerX = (event.clientX - box.left) * ratio;
            const value = ((pointerX - pad.left) / plotWidth) * maxX;
            const nearest = xs.reduce((best, x) => (Math.abs(x - value) < Math.abs(best - value) ? x : best), xs[0]);

            const lineX = toX(nearest);
            line.setAttribute('x1', lineX);
            line.setAttribute('x2', lineX);
            line.setAttribute('visibility', 'visible');

            const incomplete = drawable.some(item => item.points.some(point => point.x === nearest && point.incomplete));
            const rows = [node('div', 'igc-tip-head', `${nearest}${xUnit}${incomplete ? ' · 불완전 구간' : ''}`)];
            drawable.forEach(item => {
                const point = item.points.find(candidate => candidate.x === nearest);
                const row = node('div', 'igc-tip-row');
                const key = node('span', 'igc-tip-key');
                key.style.background = item.color;
                row.append(
                    key,
                    node('span', 'igc-tip-value', point && isNumber(point.y) ? formatValue(point.y, point) : '요청 없음'),
                    node('span', 'igc-tip-label', item.label),
                );
                rows.push(row);
            });
            tip.replaceChildren(...rows);
            tip.classList.remove('hidden');

            const left = svg.offsetLeft + lineX / ratio;
            const tipWidth = tip.offsetWidth;
            const placeLeft = left + 12 + tipWidth > figure.clientWidth;
            tip.style.left = `${Math.max(0, placeLeft ? left - 12 - tipWidth : left + 12)}px`;
            tip.style.top = `${svg.offsetTop + 8}px`;
        });
        hit.addEventListener('pointerleave', hide);
    }


    // -----------------------------------------------------------------
    // 세로 막대 (라운드별 값)
    // -----------------------------------------------------------------

    /**
     * bars: [{ label, sublabel, value, color, title }] — 모든 막대에 값이 붙으므로 y축 눈금은 그리지 않는다.
     * statusLegend: [{ label, color }] (막대 색이 SLO 판정일 때)
     */
    function columnChart(host, { title, subtitle, bars, refLine, format, statusLegend }) {
        if (!bars.some(bar => isNumber(bar.value))) return emptyChart(host, title, subtitle, NO_DATA);

        const { figure, width } = chartFrame(host, title, subtitle);
        const height = 200;
        const pad = { left: 8, right: 8, top: 22, bottom: 40 };
        const plotWidth = width - pad.left - pad.right;
        const baseY = height - pad.bottom;
        const plotHeight = baseY - pad.top;

        const values = bars.filter(bar => isNumber(bar.value)).map(bar => bar.value);
        const refValue = refLine && isNumber(refLine.value) ? refLine.value : 0;
        const scale = niceScale(Math.max(...values, refValue), 4);
        const toY = value => baseY - (value / scale.top) * plotHeight;
        const slot = plotWidth / bars.length;
        const barWidth = Math.min(24, slot * 0.5);

        const svg = svgRoot(width, height, title);
        svg.appendChild(svgEl('line', { x1: pad.left, x2: pad.left + plotWidth, y1: baseY, y2: baseY, class: 'igc-axis-line' }));

        bars.forEach((bar, index) => {
            const centerX = pad.left + slot * index + slot / 2;
            if (isNumber(bar.value)) {
                const top = toY(bar.value);
                const shape = svgEl('path', { d: columnPath(centerX - barWidth / 2, top, barWidth, baseY - top), fill: bar.color, class: 'igc-bar' });
                shape.appendChild(svgEl('title', {}, bar.title || `${bar.label} ${bar.sublabel || ''}: ${format(bar.value)}`));
                svg.appendChild(shape);
                svg.appendChild(svgEl('text', { x: centerX, y: top - 6, 'text-anchor': 'middle', class: 'igc-value' }, format(bar.value)));
            } else {
                svg.appendChild(svgEl('text', { x: centerX, y: baseY - 6, 'text-anchor': 'middle', class: 'igc-sublabel' }, NO_DATA));
            }
            svg.appendChild(svgEl('text', { x: centerX, y: baseY + 16, 'text-anchor': 'middle', class: 'igc-label' }, bar.label));
            if (bar.sublabel) {
                svg.appendChild(svgEl('text', { x: centerX, y: baseY + 31, 'text-anchor': 'middle', class: 'igc-sublabel' }, bar.sublabel));
            }
        });

        if (refLine && isNumber(refLine.value)) {
            const y = toY(refLine.value);
            svg.appendChild(svgEl('line', { x1: pad.left, x2: pad.left + plotWidth, y1: y, y2: y, class: 'igc-ref' }));
            // 라벨이 없으면 부제가 기준선을 설명한다 (막대 끝 값 라벨과 겹치지 않게)
            if (refLine.label) refLabel(svg, pad.left + plotWidth, y, refLine.label);
        }

        figure.appendChild(svg);
        if (statusLegend && statusLegend.length > 0) figure.appendChild(legend(statusLegend));
        return figure;
    }

    /** SLO 판정 색의 범례 (화면에 나온 판정만) */
    function sloLegend(rounds) {
        const seen = [];
        rounds.forEach(round => {
            if (round.sloState === 'unknown' || seen.includes(round.sloState)) return;
            seen.push(round.sloState);
        });
        return ['met', 'boundary', 'exceeded']
            .filter(state => seen.includes(state))
            .map(state => ({ label: `SLO ${SLO_SHORT[state]}`, color: statusColor(SLO_TONE[state]) }));
    }


    // -----------------------------------------------------------------
    // 묶음 막대 (엔드포인트별 P95, 첫 vs 마지막 측정)
    // -----------------------------------------------------------------

    function groupedColumnChart(host, { title, subtitle, groups, series, format }) {
        const values = groups.flatMap(group => group.values.filter(isNumber));
        if (values.length === 0) return emptyChart(host, title, subtitle);

        const { figure, width } = chartFrame(host, title, subtitle);
        const height = 224;
        const pad = { left: 8, right: 8, top: 22, bottom: 40 };
        const plotWidth = width - pad.left - pad.right;
        const baseY = height - pad.bottom;
        const plotHeight = baseY - pad.top;
        const scale = niceScale(Math.max(...values), 4);
        const toY = value => baseY - (value / scale.top) * plotHeight;
        const slot = plotWidth / groups.length;
        const gap = 2;
        const barWidth = Math.min(24, (slot * 0.7 - gap * (series.length - 1)) / series.length);

        const svg = svgRoot(width, height, title);
        svg.appendChild(svgEl('line', { x1: pad.left, x2: pad.left + plotWidth, y1: baseY, y2: baseY, class: 'igc-axis-line' }));

        groups.forEach((group, groupIndex) => {
            const groupCenter = pad.left + slot * groupIndex + slot / 2;
            const groupWidth = barWidth * series.length + gap * (series.length - 1);
            const start = groupCenter - groupWidth / 2;

            group.values.forEach((value, seriesIndex) => {
                const x = start + (barWidth + gap) * seriesIndex;
                const centerX = x + barWidth / 2;
                if (isNumber(value)) {
                    const top = toY(value);
                    const shape = svgEl('path', { d: columnPath(x, top, barWidth, Math.max(baseY - top, 0.5)), fill: series[seriesIndex].color, class: 'igc-bar' });
                    shape.appendChild(svgEl('title', {}, `${group.label} · ${series[seriesIndex].label}: ${format(value)}`));
                    svg.appendChild(shape);
                    svg.appendChild(svgEl('text', { x: centerX, y: top - 5, 'text-anchor': 'middle', class: 'igc-value igc-value-small' }, format(value)));
                } else {
                    svg.appendChild(svgEl('text', { x: centerX, y: baseY - 5, 'text-anchor': 'middle', class: 'igc-sublabel' }, '없음'));
                }
            });

            svg.appendChild(svgEl('text', { x: groupCenter, y: baseY + 17, 'text-anchor': 'middle', class: 'igc-label' }, group.label));
            // 요청 종류가 무엇인지 이름 아래에 한 마디 (처음 보는 사람 기준)
            if (group.sublabel) {
                svg.appendChild(svgEl('text', { x: groupCenter, y: baseY + 31, 'text-anchor': 'middle', class: 'igc-sublabel' }, group.sublabel));
            }
        });

        figure.appendChild(svg);
        if (series.length >= 2) figure.appendChild(legend(series.map(item => ({ label: item.label, color: item.color }))));
        return figure;
    }


    // -----------------------------------------------------------------
    // 부하 중 그래프 (데이터 규칙은 Phase 4 그래프와 같다)
    // -----------------------------------------------------------------

    function seriesLabel(round) {
        return `${round.name} · 서버 ${round.replicasText}`;
    }

    function timeSeries(vm, valueOf) {
        return vm.rounds.map(round => {
            const { record } = round;
            const timeseries = record.timeseries;
            const bucket = timeseries && isNumber(timeseries.bucket_sec) && timeseries.bucket_sec > 0 ? timeseries.bucket_sec : 1;
            const duration = isNumber(record.duration) ? record.duration : null;
            const points = timeseries && Array.isArray(timeseries.points)
                ? timeseries.points.filter(point => isNumber(point.t)).map(point => ({
                    x: point.t,
                    y: valueOf(point, bucket),
                    raw: point,
                    // 부하 종료 시점에 걸린 구간 (구간 끝이 부하 시간에 닿는다)
                    incomplete: duration !== null && point.t + bucket >= duration,
                }))
                : [];
            return { label: seriesLabel(round), color: round.color, points };
        });
    }

    function tpsTimeline(host, vm) {
        return lineChart(host, {
            title: '초당 처리한 요청 수',
            subtitle: '1초 구간마다 끝난 요청 수 (처리량·TPS) · 측정 회차별 겹쳐 그림',
            series: timeSeries(vm, (point, bucket) => (isNumber(point.requests) ? point.requests / bucket : null)),
            refLine: null,
            formatValue: (value, point) => `${fmtTps(value)} req/s (요청 ${point.raw.requests}건, 실패 ${point.raw.failures}건)`,
            formatTick: (tick, decimals) => tick.toFixed(decimals),
            xUnit: '초',
        });
    }

    function p95Timeline(host, vm) {
        return lineChart(host, {
            title: '초당 응답 시간 (P95, ms)',
            subtitle: '1초 구간마다 계산한 P95 · 점선 = 응답 목표(SLO)',
            series: timeSeries(vm, point => (isNumber(point.p95_ms) ? point.p95_ms : null)),
            refLine: isNumber(vm.slo) ? { value: vm.slo, label: `SLO ${vm.slo}ms` } : null,
            formatValue: (value, point) => `${fmtMs(value)} (요청 ${point.raw.requests}건)`,
            formatTick: (tick, decimals) => tick.toFixed(decimals),
            xUnit: '초',
        });
    }

    function endpointChart(host, vm) {
        const title = '요청 종류별 응답 시간 (P95, ms)';
        const withEndpoints = vm.rounds.filter(round => Array.isArray(round.record.endpoints) && round.record.endpoints.length > 0);
        if (withEndpoints.length === 0) return emptyChart(host, title, '요청 종류(엔드포인트)별 Locust 통계');

        const chosen = withEndpoints.length >= 2 ? [withEndpoints[0], lastOf(withEndpoints)] : [withEndpoints[0]];
        const names = [];
        chosen.forEach(round => round.record.endpoints.forEach(endpoint => {
            if (!names.includes(endpoint.name)) names.push(endpoint.name);
        }));

        const figure = groupedColumnChart(host, {
            title,
            subtitle: chosen.length >= 2 ? '요청 종류(엔드포인트)별 · 첫 측정과 마지막 측정' : '요청 종류(엔드포인트)별',
            groups: names.map(name => ({
                label: name,
                sublabel: ENDPOINT_NOTE[name] || null,
                values: chosen.map(round => {
                    const endpoint = round.record.endpoints.find(item => item.name === name);
                    return endpoint && isNumber(endpoint.p95_ms) ? endpoint.p95_ms : null;
                }),
            })),
            series: chosen.map(round => ({ label: seriesLabel(round), color: round.color })),
            format: value => `${Math.round(value)}`,
        });

        figure.appendChild(note(ENDPOINT_NOTE_LINE));
        return figure;
    }

    /** 측정별 에러율. 측정이 1회면 막대 하나짜리 차트 대신 값만 보인다. */
    function errorRateChart(host, vm) {
        const title = '측정별 오류율 (%)';
        const subtitle = '전체 요청 중 실패한 요청 비율';

        if (vm.rounds.length < 2) {
            const { figure } = chartFrame(host, title, subtitle);
            const round = vm.rounds[0];
            figure.appendChild(node('p', 'igc-single', round ? `${round.name} (서버 ${round.replicasText}): 오류율 ${round.errText}` : NO_DATA));
            return figure;
        }

        return columnChart(host, {
            title,
            subtitle,
            bars: vm.rounds.map(round => ({
                label: round.name,
                sublabel: `서버 ${round.replicasText}`,
                value: isNumber(round.record.error_rate) ? round.record.error_rate * 100 : null,
                color: cssVar('--ig-single'),
            })),
            refLine: null,
            format: value => `${value.toFixed(2)}%`,
        });
    }

    function instanceChart(host, vm) {
        const title = '서버별 처리한 요청 수';
        const subtitle = '서버(replica)마다 받은 요청 수 · /health·/metrics 제외';
        const rounds = vm.rounds.filter(round => {
            const data = round.record.requests_by_instance;
            return data && data.counts && Object.keys(data.counts).length > 0;
        });
        if (rounds.length === 0) return emptyChart(host, title, subtitle);

        const { figure, width } = chartFrame(host, title, subtitle);
        const allCounts = rounds.flatMap(round => Object.values(round.record.requests_by_instance.counts).filter(isNumber));
        const maxCount = Math.max(1, ...allCounts);
        const labelWidth = Math.min(96, Math.floor(width * 0.22));
        const valueWidth = 118;
        const barMax = Math.max(40, width - labelWidth - valueWidth - 12);
        const rowHeight = 22;
        const headerHeight = 22;
        const groupGap = 10;
        const rows = rounds.reduce((sum, round) => sum + Object.keys(round.record.requests_by_instance.counts).length, 0);
        const height = rounds.length * (headerHeight + groupGap) + rows * rowHeight;

        const svg = svgRoot(width, height, title);
        let y = 0;

        rounds.forEach(round => {
            const counts = round.record.requests_by_instance.counts;
            const total = Object.values(counts).filter(isNumber).reduce((sum, value) => sum + value, 0);

            svg.appendChild(svgEl('text', { x: 0, y: y + 15, class: 'igc-label' }, seriesLabel(round)));
            y += headerHeight;

            Object.entries(counts).forEach(([instance, count]) => {
                const label = svgEl('text', { x: labelWidth - 8, y: y + 15, 'text-anchor': 'end', class: 'igc-sublabel' }, instanceShortName(instance));
                label.appendChild(svgEl('title', {}, instance));
                svg.appendChild(label);

                if (isNumber(count)) {
                    const barWidth = Math.max((count / maxCount) * barMax, 1);
                    const shape = svgEl('path', { d: barPath(labelWidth, y + 4, barWidth, rowHeight - 9), fill: round.color, class: 'igc-bar' });
                    shape.appendChild(svgEl('title', {}, `${instance}: ${count}건`));
                    svg.appendChild(shape);
                    const share = total > 0 ? `${Math.round((count / total) * 100)}%` : '-';
                    svg.appendChild(svgEl('text', { x: labelWidth + barWidth + 6, y: y + 15, class: 'igc-value igc-value-small' }, `${count}건 (${share})`));
                } else {
                    svg.appendChild(svgEl('text', { x: labelWidth, y: y + 15, class: 'igc-sublabel' }, `${NO_DATA} (카운터 초기화)`));
                }
                y += rowHeight;
            });
            y += groupGap;
        });

        figure.appendChild(svg);
        figure.appendChild(note('요청 수는 부하 전후 Prometheus 값의 차이입니다. 서버 이름은 컨테이너 IP 끝자리입니다. '
            + '/health는 Docker 상태 확인 요청과, /metrics는 Prometheus 수집 요청과 구분할 수 없어 뺐습니다.'));
        return figure;
    }

    const LOAD_GRAPH_NOTE = '초당 값은 요청 하나하나의 기록으로 1초 구간마다 계산했습니다(구간 P95는 nearest-rank). '
        + '요청이 없는 구간은 선을 끊습니다. 위 요약 P95(Locust 계산)와 조금 다를 수 있습니다.';

    const AGGREGATED_NOTE = '처리량·응답 시간(P95)·오류율은 Locust가 잰 전체 요청 합산값입니다.';

    /** 라운드별 P95 (SLO 판정 색) · 처리량 막대. 측정이 2회 이상일 때만 쓴다 */
    function roundP95Chart(host, vm) {
        return columnChart(host, {
            title: '측정별 응답 시간 (P95)',
            subtitle: isNumber(vm.slo) ? `점선 = 응답 목표(SLO) ${vm.slo}ms` : null,
            bars: vm.rounds.map(round => ({
                label: round.name,
                sublabel: `서버 ${round.replicasText}`,
                value: round.record.latency_p95,
                color: statusColor(SLO_TONE[round.sloState]),
                title: `${round.name} · 서버 ${round.replicasText}: P95 ${round.p95Text} (${round.sloText})`,
            })),
            refLine: isNumber(vm.slo) ? { value: vm.slo, label: null } : null,
            format: value => `${Math.round(value)}ms`,
            statusLegend: sloLegend(vm.rounds),
        });
    }

    function roundTpsChart(host, vm) {
        return columnChart(host, {
            title: '측정별 처리량',
            subtitle: '1초에 처리한 요청 수 (TPS)',
            bars: vm.rounds.map(round => ({
                label: round.name,
                sublabel: `서버 ${round.replicasText}`,
                value: round.record.tps,
                color: cssVar('--ig-single'),
            })),
            refLine: null,
            format: value => value.toFixed(1),
        });
    }


    // -----------------------------------------------------------------
    // 공용 조각
    // -----------------------------------------------------------------

    function forcedAlert(vm) {
        return vm.forced
            ? node('div', 'ig-alert', '디버그 강제 실행 — 발표 수치 아님 (force_scaling이 LLM 판단을 덮어썼습니다)')
            : null;
    }

    /** 신뢰도 막대 (result_panel.js confidenceMeter, 기준선 = 저신뢰 확인 게이트) */
    function meter(confidence, threshold) {
        return confidenceMeter(confidence, threshold);
    }

    function definitionList(items, className) {
        const list = node('dl', className || 'ig-dl');
        items.forEach(([label, value]) => {
            const row = node('div', 'ig-dl-row');
            row.append(node('dt', null, label), node('dd', null, value));
            list.appendChild(row);
        });
        return list;
    }

    /** 참고 문장 (자원 메트릭 미수집, 측정값 출처) */
    function referenceNotes(vm) {
        const notes = [];
        if (vm.resourcesUnmeasured) {
            notes.push(`CPU·메모리는 이 환경에서 수집되지 않아 AI 판단 입력에 "${UNMEASURED_LABEL}"으로 들어갔습니다.`);
        }
        notes.push(AGGREGATED_NOTE);
        return notes;
    }

    /**
     * 기술 정보 접힘 (판단 모델·결과 파일 경로·코드 버전). 결과를 읽는 데 필요한 값이 아니라서 접어 둔다.
     * 측정 조건은 접지 않는다 (model().conditionItems).
     */
    function techDetails(vm, className, key) {
        const details = node('details', className || 'ig-tech');
        details.dataset.key = key || 'tech-info';
        details.appendChild(node('summary', null, '기술 정보'));
        details.appendChild(definitionList(vm.metaItems));
        return details;
    }

    return {
        cssVar,
        node,
        appendRichText,
        rawDetails,
        grafanaLink,
        empty,
        note,
        scrollBelowToolbar,
        model,
        glossaryLine,
        causeExcerpt,
        meter,
        forcedAlert,
        definitionList,
        referenceNotes,
        techDetails,
        ENDPOINT_NOTE,
        ENDPOINT_NOTE_LINE,
        statusColor,
        niceScale,
        chartFrame,
        emptyChart,
        svgRoot,
        legend,
        sloLegend,
        charts: {
            tpsTimeline,
            p95Timeline,
            endpointChart,
            errorRateChart,
            instanceChart,
            roundP95Chart,
            roundTpsChart,
        },
        SLO_TONE,
        LOAD_GRAPH_NOTE,
        AGGREGATED_NOTE,
    };
})();

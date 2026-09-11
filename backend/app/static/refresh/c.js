/*
 * 기본 보기 (C 미니멀형) 결과 패널 (디자인 개편 #93)
 *
 * 한눈에 보이게: 결론 한 줄 → P95 큰 숫자(첫 → 마지막) → 서버 흐름 그림 → 측정별 P95 · 초당 P95 → 지표 4개.
 * 첫 화면 본문 문장은 3개 이하로 둔다. SLO·P95 풀이는 처음 나오는 곳 바로 아래에 화면에 보이게 둔다.
 * 판단 원문·진단 입력 측정값·엔드포인트별/서버별 그래프·초당 TPS·측정 조건·결과 파일·코드 버전은 상세 보기(a.js)에만 있다.
 * 예외: "AI가 이렇게 판단한 이유"는 접어 둔다 (판단 한 줄 = 측정값·필드 템플릿, 그 아래 LLM 원인에서 발췌한 문장 하나).
 * 계산은 refresh/common.js 뷰모델(= result_panel.js 함수)만 쓴다.
 */
(function () {
    const { node } = IGRefresh;

    function toDetail(selector) {
        InfraGuardView.setView('detail');
        IGRefresh.scrollBelowToolbar(document.querySelector(selector) || document.getElementById('result-panel'));
    }

    function linkButton(text, className, onClick) {
        const button = node('button', className || 'ig-linkbtn', text);
        button.type = 'button';
        button.addEventListener('click', onClick);
        return button;
    }

    // ---------------- 결론 한 줄 ----------------

    function head(root, vm) {
        const box = root.appendChild(node('div', 'rc-head'));

        const title = node('h2', `rc-headline rc-state-${vm.verdict.tone}`);
        title.append(node('span', 'ig-dot'), vm.headline);
        box.appendChild(title);

        // "응답 목표(SLO)"가 처음 나오는 곳이라 풀이를 바로 아래에 둔다
        box.appendChild(IGRefresh.glossaryLine(vm.glossary.slo));

        if (vm.diagnoses.length > 0) {
            box.appendChild(linkButton('AI가 이렇게 판단한 이유 ↓', 'ig-linkbtn', () => {
                const fold = root.querySelector('[data-key=c-reason]');
                if (!fold) return;
                fold.open = true;
                IGRefresh.scrollBelowToolbar(fold);
            }));
        }

        if (vm.error) box.appendChild(node('p', 'rc-error', `오류: ${vm.error}`));
    }

    // ---------------- P95 큰 숫자 ----------------

    function hero(root, vm) {
        const box = root.appendChild(node('div', 'rc-hero-block'));
        box.appendChild(node('p', 'rc-kicker', 'P95 응답 시간'));
        box.appendChild(IGRefresh.glossaryLine(vm.glossary.p95));

        const big = node('p', vm.history.length === 0 ? 'rc-hero rc-hero-empty' : 'rc-hero');
        if (vm.history.length === 0) {
            big.textContent = NO_DATA;
        } else if (vm.multi) {
            big.append(node('span', 'rc-from', fmtMs(vm.first.latency_p95)), node('span', 'rc-arrow', ' → '), fmtMs(vm.last.latency_p95));
        } else {
            big.textContent = fmtMs(vm.first.latency_p95);
        }
        box.appendChild(big);

        if (vm.history.length > 0) {
            const last = vm.rounds[vm.rounds.length - 1];
            const slo = node('p', `rc-slo ig-slo ig-slo-${last.sloState}`);
            slo.append(node('span', 'ig-dot'), last.sloText);
            box.appendChild(slo);
        }
    }

    // ---------------- 서버 흐름 그림 ----------------

    function stepLabel(step) {
        if (step.scalingFailed) return '스케일링 실패';
        return `${step.forced ? '디버그 요청' : 'AI 제안'} · ${step.decisionText || '응답 없음'}`;
    }

    /** 마지막 측정 뒤에 어떻게 끝났는지 (텍스트 최소) */
    function endLabel(vm, step) {
        if (step) {
            return {
                text: step.scalingFailed ? `AI 제안 ${step.plan} · 스케일링 실패` : `AI 제안 ${step.plan} · ${step.decisionText || '응답 없음'}`,
                tone: step.scalingFailed ? 'bad' : (step.decision === 'rejected' ? 'warn' : 'muted'),
            };
        }
        switch (vm.endReason) {
            case 'no_scaling_proposed':
                return { text: 'AI 제안 없음', tone: 'muted' };
            case 'no_further_scaling':
                return { text: '추가 제안 없음', tone: 'muted' };
            case 'failed':
                return { text: '실행 실패', tone: 'bad' };
            case 'stream_closed':
                return { text: '연결 끊김', tone: 'warn' };
            default:
                return null;
        }
    }

    function flowNode(round) {
        const box = node('div', 'rc-node');

        const servers = node('div', 'rc-servers');
        servers.setAttribute('aria-hidden', 'true');
        if (isNumber(round.replicas)) {
            for (let index = 0; index < Math.min(round.replicas, 8); index += 1) servers.appendChild(node('i'));
        }

        const p95 = node('div', `rc-node-p95 ig-slo ig-slo-${round.sloState}`);
        p95.append(node('span', 'ig-dot'), round.p95Text);

        box.append(servers, node('div', 'rc-node-label', `서버 ${round.replicasText}`), p95);
        return box;
    }

    function flow(root, vm) {
        const figure = root.appendChild(node('figure', 'rc-flow'));
        const caption = node('figcaption', 'igc-caption');
        caption.append(node('span', 'igc-title', '서버 흐름'), node('span', 'igc-sub', '측정마다 서버 수와 P95'));
        figure.appendChild(caption);

        const track = figure.appendChild(node('div', 'rc-flow-track'));
        vm.rounds.forEach((round, index) => {
            track.appendChild(flowNode(round));
            const step = vm.steps.find(item => item.round === round.round);

            if (index < vm.rounds.length - 1) {
                const link = node('div', 'rc-link');
                link.appendChild(node('span', null, step ? stepLabel(step) : '재측정'));
                track.appendChild(link);
                return;
            }

            const end = endLabel(vm, step);
            if (end) {
                const cap = node('div', `rc-end tone-${end.tone}`);
                cap.appendChild(node('span', null, end.text));
                track.appendChild(cap);
            }
        });
    }

    // ---------------- 측정별 P95 계단 차트 ----------------

    /** 측정별 P95 계단: 측정마다 가로 선 하나, 회색 영역 = SLO 이하. 글자는 본문 색, 판정은 점 색 + 범례 */
    function stepChart(host, vm) {
        const title = '측정별 P95';
        const subtitle = isNumber(vm.slo) ? `회색 영역 = SLO ${vm.slo}ms 이하` : null;
        const { figure, width } = IGRefresh.chartFrame(host, title, subtitle);
        const height = 220;
        const pad = { left: 4, right: 4, top: 26, bottom: 38 };
        const plotWidth = width - pad.left - pad.right;
        const baseY = height - pad.bottom;
        const plotHeight = baseY - pad.top;
        const values = vm.rounds.map(round => round.record.latency_p95).filter(isNumber);
        const scale = IGRefresh.niceScale(Math.max(...values, isNumber(vm.slo) ? vm.slo : 0), 4);
        const toY = value => baseY - (value / scale.top) * plotHeight;
        const segment = plotWidth / vm.rounds.length;
        const inset = Math.min(10, segment * 0.1);

        const svg = IGRefresh.svgRoot(width, height, title);
        if (isNumber(vm.slo)) {
            svg.appendChild(svgEl('rect', { x: pad.left, y: toY(vm.slo), width: plotWidth, height: baseY - toY(vm.slo), class: 'rc-zone' }));
        }
        svg.appendChild(svgEl('line', { x1: pad.left, x2: pad.left + plotWidth, y1: baseY, y2: baseY, class: 'igc-axis-line' }));

        vm.rounds.forEach((round, index) => {
            const x0 = pad.left + segment * index + inset;
            const x1 = pad.left + segment * (index + 1) - inset;
            const value = round.record.latency_p95;

            if (isNumber(value)) {
                const y = toY(value);
                svg.appendChild(svgEl('line', { x1: x0, x2: x1, y1: y, y2: y, class: 'rc-segment' }));
                const marker = svgEl('circle', { cx: x0, cy: y, r: 5, fill: IGRefresh.statusColor(IGRefresh.SLO_TONE[round.sloState]), class: 'rc-marker' });
                marker.appendChild(svgEl('title', {}, `${round.name} · 서버 ${round.replicasText}: P95 ${round.p95Text} (${round.sloText})`));
                svg.appendChild(marker);
                svg.appendChild(svgEl('text', { x: x0 + 10, y: y - 10, class: 'igc-value' }, round.p95Text));

                const next = vm.rounds[index + 1];
                if (next && isNumber(next.record.latency_p95)) {
                    svg.appendChild(svgEl('line', { x1: x1, x2: x1 + inset * 2, y1: y, y2: toY(next.record.latency_p95), class: 'rc-connector' }));
                }
            } else {
                svg.appendChild(svgEl('text', { x: x0, y: baseY - 8, class: 'igc-sublabel' }, NO_DATA));
            }

            svg.appendChild(svgEl('text', { x: x0, y: baseY + 17, class: 'igc-label' }, `서버 ${round.replicasText}`));
            svg.appendChild(svgEl('text', { x: x0, y: baseY + 32, class: 'igc-sublabel' }, round.name));
        });

        figure.appendChild(svg);
        figure.appendChild(IGRefresh.legend(IGRefresh.sloLegend(vm.rounds)));
        return figure;
    }

    // ---------------- 지표 4개 ----------------

    function stat(label, value, sub, tone) {
        const box = node('div', 'rc-stat');
        box.append(node('div', 'rc-stat-l', label), node('div', 'rc-stat-v', value));
        if (sub) box.appendChild(node('div', `rc-stat-d${tone ? ` tone-${tone}` : ''}`, sub));
        return box;
    }

    function proposalText(vm) {
        const { counts } = vm;
        if (counts.proposals === 0) return vm.diagnoses.length > 0 ? 'AI 스케일링 제안 없음' : 'AI 진단 결과 없음';
        const parts = [`AI 제안 ${counts.proposals}회`];
        if (counts.approved > 0) parts.push(`승인 ${counts.approved}회`);
        if (counts.rejected > 0) parts.push(`거절 ${counts.rejected}회`);
        if (counts.noResponse > 0) parts.push(`응답 없음 ${counts.noResponse}회`);
        return parts.join(' · ');
    }

    function stats(vm) {
        const grid = node('div', 'rc-stats');
        const first = vm.rounds[0];
        const last = vm.rounds[vm.rounds.length - 1];

        grid.append(
            stat('처리량 (TPS)', last.tpsText, vm.multi ? `${first.tpsText}에서 ${vm.deltas.tps.text}` : '측정 1회', vm.multi ? vm.deltas.tps.tone : null),
            stat('에러율', last.errText, vm.multi ? `${first.errText}에서 ${vm.deltas.err.text}` : null, vm.multi ? vm.deltas.err.tone : null),
            stat('서버', last.replicasText, proposalText(vm)),
            stat('AI 신뢰도', vm.confidenceRange, vm.diagnoses.length > 0 ? `AI 판단 ${vm.diagnoses.length}회` : 'AI 진단 결과 없음'),
        );
        return grid;
    }

    // ---------------- AI가 이렇게 판단한 이유 (접힘) ----------------

    function reasons(root, vm) {
        const details = root.appendChild(node('details', 'rc-fold'));
        details.dataset.key = 'c-reason';
        details.appendChild(node('summary', null, 'AI가 이렇게 판단한 이유'));
        const inner = details.appendChild(node('div', 'rc-fold-body'));

        if (vm.diagnoses.length === 0) {
            inner.appendChild(node('p', 'rc-reason-line', 'AI 진단 결과가 없습니다. 진단 결과를 받기 전에 실행이 끝났습니다.'));
        }

        vm.diagnoses.forEach(item => {
            const round = vm.rounds.find(candidate => candidate.round === item.round);
            const line = node('p', 'rc-reason-line');
            line.append(
                `측정 ${item.round ?? '-'}${round ? ` · 서버 ${round.replicasText} · P95 ${round.p95Text}` : ''} → `,
                node('strong', `tone-${item.conclusion.tone}`, item.conclusion.text),
                node('span', 'rc-reason-meta', ` (신뢰도 ${item.confidenceText}${item.decisionText ? ` · ${item.decisionText}` : ''})`),
            );
            inner.appendChild(line);

            const excerpt = IGRefresh.causeExcerpt(item.diagnosis.cause);
            if (excerpt) {
                const quote = node('blockquote', 'rc-quote');
                IGRefresh.appendRichText(quote, shorten(excerpt, 180));
                quote.appendChild(node('span', 'rc-quote-src', ' — AI 설명 발췌'));
                inner.appendChild(quote);
            }
        });

        inner.appendChild(linkButton('판단 원문과 진단 입력 측정값은 상세 보기에서 →', 'ig-linkbtn', () => toDetail('.ra-judgement')));
    }

    function links(root) {
        const box = root.appendChild(node('div', 'rc-links'));
        box.append(
            linkButton('상세 보기에서 전체 정보 보기 →', 'rc-more', () => toDetail('#result-panel')),
            IGRefresh.grafanaLink('Grafana 대시보드'),
        );
    }

    function render(report, body, meta) {
        const vm = IGRefresh.model(report, meta);
        const root = body.appendChild(node('section', 'rc'));

        const alert = IGRefresh.forcedAlert(vm);
        if (alert) root.appendChild(alert);

        head(root, vm);
        hero(root, vm);

        if (vm.history.length === 0) {
            links(root);
            return;
        }

        flow(root, vm);

        const charts = root.appendChild(node('div', vm.multi ? 'rc-charts' : 'rc-charts rc-charts-single'));
        if (vm.multi) stepChart(charts, vm);
        IGRefresh.charts.p95Timeline(charts, vm);

        root.appendChild(stats(vm));
        reasons(root, vm);
        links(root);
    }

    InfraGuardView.register('simple', { label: '기본 보기', render });
})();

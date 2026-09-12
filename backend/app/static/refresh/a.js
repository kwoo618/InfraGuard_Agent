/*
 * 상세 보기 (A 관제 콘솔형) 결과 패널 (디자인 개편 #93)
 *
 * 전체 정보를 둔다: 판정(결론 한 줄) → 측정 흐름(서버 수·P95·AI 제안·결정) → AI 판단 근거(진단 입력 측정값·LLM 원문)
 * → 전/후 비교 → 부하 중 그래프 5종 → 측정 조건(결과 파일·코드 버전). 기본 보기(c.js)에서 뺀 정보는 모두 여기에 있다.
 * 계산은 refresh/common.js 뷰모델(= result_panel.js 함수)만 쓴다.
 */
(function () {
    const { node } = IGRefresh;

    function sectionHead(text) {
        const head = node('div', 'ra-section-h');
        head.appendChild(node('h3', null, text));
        return head;
    }

    function header(vm) {
        const top = node('div', 'ra-top');
        top.appendChild(node('h2', 'ra-title', vm.saved ? '저장된 실행 기록' : '진단 결과'));
        if (vm.saved) top.appendChild(node('span', 'ra-source', vm.meta.startedAtText || '실행 시각 기록 없음'));
        top.append(node('span', 'ra-cond', vm.conditionLine), IGRefresh.grafanaLink('Grafana에서 보기'));
        return top;
    }

    /** SLO·P95 풀이. 머리줄의 "P95 SLO …"가 처음 나오는 곳이라 바로 아래 화면에 보이게 둔다 */
    function glossary(vm) {
        const box = node('div', 'ra-glossary');
        box.append(IGRefresh.glossaryLine(vm.glossary.p95), IGRefresh.glossaryLine(vm.glossary.slo));
        return box;
    }

    /** 판정 띠: 결론 한 줄(측정값·필드 템플릿) + 수치 요약. 이유는 AI 판단 근거로 연결한다 */
    function verdict(vm) {
        const box = node('div', `ra-verdict ra-tone-${vm.verdict.tone}`);
        box.append(node('b', `tone-${vm.verdict.tone}`, vm.headline), node('span', null, vm.summary.text));

        if (vm.diagnoses.length > 0) {
            const link = node('button', 'ig-linkbtn', 'AI 판단 근거 보기 ↓');
            link.type = 'button';
            link.addEventListener('click', () => IGRefresh.scrollBelowToolbar(document.querySelector('.ra-reasoning')));
            box.appendChild(link);
        }
        return box;
    }

    // ---------------- 측정 흐름 ----------------

    function bigMs(record) {
        const wrap = node('div', 'ra-p95');
        if (isNumber(record.latency_p95)) {
            wrap.append(String(Math.round(record.latency_p95)), node('small', null, 'ms'));
        } else {
            wrap.textContent = NO_DATA;
        }
        return wrap;
    }

    function roundBox(round) {
        const box = node('div', 'ra-round');

        const head = node('div', 'ra-round-h');
        head.append(node('span', null, round.name), node('span', null, `서버 ${round.replicasText}`));
        box.appendChild(head);

        const boxes = node('div', 'ra-boxes');
        boxes.setAttribute('aria-hidden', 'true');
        if (isNumber(round.replicas)) {
            for (let index = 0; index < Math.min(round.replicas, 8); index += 1) boxes.appendChild(node('i'));
        }
        box.append(boxes, bigMs(round.record));

        const slo = node('div', `ra-slo ig-slo ig-slo-${round.sloState}`);
        slo.append(node('span', 'ig-dot'), round.sloText);
        box.append(slo, node('div', 'ra-sub', `처리량 ${round.tpsText} · 오류율 ${round.errText}`));
        return box;
    }

    function stepBox(lines, end) {
        const box = node('div', `ra-step${end ? ' ra-step-end' : ''}`);
        const inner = node('div', 'ra-step-inner');
        lines.forEach(([label, value, tone]) => {
            const line = node('div');
            if (label) line.append(`${label} `);
            if (value) line.appendChild(node('em', tone ? `tone-${tone}` : null, value));
            inner.appendChild(line);
        });
        box.appendChild(inner);
        return box;
    }

    function approvalLines(step) {
        const lines = [[step.forced ? '디버그 강제 요청' : 'AI 제안', step.plan || '스케일링']];
        if (step.confidenceText) lines.push(['신뢰도', step.confidenceText]);
        lines.push([null, step.decisionText || '응답 없음']);
        if (step.scalingFailed) lines.push([null, '스케일링 실패', 'bad']);
        return lines;
    }

    /** 마지막 측정 뒤에 무엇으로 끝났는지: 승인 요청 → 재진단 → 재검증 → 실패·연결 종료 순으로 찾는다 */
    function endLines(vm, round, step) {
        if (step) return approvalLines(step);

        const diagnosis = [...vm.diagnoses].reverse().find(item => item.round === round.round);
        if (diagnosis) return [['AI 판단', diagnosis.conclusion.text], ['신뢰도', diagnosis.confidenceText]];

        const revalidation = vm.timeline.find(item => item.kind === 'revalidation' && item.round === round.round);
        if (revalidation) return [['재검증', revalidation.verdict], [null, revalidation.extra]];

        if (vm.failed) return [[null, '실행 실패', 'bad']];
        if (vm.endReason === 'stream_closed') return [[null, '연결 종료']];
        return null;
    }

    function flow(vm) {
        const box = node('div', 'ra-flow');
        vm.rounds.forEach((round, index) => {
            box.appendChild(roundBox(round));
            const step = vm.steps.find(item => item.round === round.round);

            if (index < vm.rounds.length - 1) {
                box.appendChild(stepBox(step ? approvalLines(step) : [[null, '재측정']]));
                return;
            }
            const end = endLines(vm, round, step);
            if (end) box.appendChild(stepBox(end, true));
        });
        return box;
    }

    // ---------------- AI 판단 근거 ----------------

    function diagnosisRow(item, vm) {
        const row = node('div', 'ra-reason-row');

        const head = node('div', 'ra-reason-head');
        head.append(
            node('span', 'ra-reason-round', `측정 ${item.round ?? '-'} ${item.initial ? '진단' : '재진단'}`),
            node('span', `ra-conclusion tone-${item.conclusion.tone}`, item.conclusion.text),
            node('span', 'ra-kv-item', `심각도 ${item.severityText}`),
        );
        if (item.lowConfidence) head.appendChild(node('span', 'tone-bad', lowConfidenceBadge(vm.report).textContent));
        head.appendChild(IGRefresh.meter(item.diagnosis.confidence, vm.threshold));
        row.appendChild(head);

        const evidence = node('div', 'ra-evidence');
        const values = node('div', 'ra-evidence-values');
        item.evidence.filter(entry => !entry.excluded).forEach(entry => {
            const span = node('span', entry.sloState ? `ig-slo ig-slo-${entry.sloState}` : null);
            if (entry.sloState) span.appendChild(node('span', 'ig-dot'));
            span.append(entry.text);
            values.appendChild(span);
        });
        evidence.append(node('span', 'ra-evidence-label', 'AI가 본 측정값'), values);
        row.appendChild(evidence);

        const excluded = item.evidence.find(entry => entry.excluded);
        if (excluded) row.appendChild(node('div', 'ra-excluded', excluded.text));

        if (item.decisionText) {
            const decision = node('div', 'ra-decision', '사용자 결정: ');
            decision.appendChild(node('b', null, item.decisionText));
            row.appendChild(decision);
        }

        row.appendChild(IGRefresh.rawDetails('LLM 진단 원문 보기', item.pairs, `diag-${item.round}-${item.initial ? 'i' : 'r'}`));
        return row;
    }

    function revalidationRow(item) {
        const row = node('div', 'ra-reason-row');
        const head = node('div', 'ra-reason-head');
        head.append(
            node('span', 'ra-reason-round', `측정 ${item.round ?? '-'} 재검증`),
            node('span', `ra-conclusion tone-${item.tone}`, item.verdict),
        );
        if (item.extra) head.appendChild(node('span', 'ra-kv-item', item.extra));
        head.appendChild(node('span', 'ra-kv-item', 'LLM 판단'));
        row.append(head, IGRefresh.rawDetails('재검증 원문 보기', item.pairs, `reval-${item.round}`));
        return row;
    }

    function judgement(vm) {
        const section = node('section', 'ra-section ra-reasoning');
        section.appendChild(sectionHead('AI 판단 근거'));
        // 처음 보는 사람 기준 용어 풀이 (진단 입력 측정값 = AI가 본 측정값)
        section.appendChild(IGRefresh.note(
            'AI가 본 측정값 = AI가 판단할 때 받은 측정값(진단 입력) · '
            + '활성 연결 = 그 순간 서버가 처리 중이거나 기다리는 요청 수 · '
            + '재검증 = 서버를 늘린 뒤 같은 조건으로 다시 측정해 비교한 것',
        ));

        if (vm.diagnoses.length === 0) {
            section.appendChild(IGRefresh.empty('AI 진단 결과 없음 (진단 결과를 받기 전에 실행이 끝났습니다)'));
            return section;
        }

        const list = node('div', 'ra-reason');
        vm.timeline.forEach(item => list.appendChild(item.kind === 'diagnosis' ? diagnosisRow(item, vm) : revalidationRow(item)));
        section.appendChild(list);
        return section;
    }

    // ---------------- 전/후 비교 ----------------

    function card(title, values, delta, sloRound) {
        const box = node('div', 'ra-card');
        box.appendChild(node('div', 'ra-card-t', title));

        const value = node('div', 'ra-card-v');
        if (values.length === 2) {
            value.append(node('span', 'ra-before', values[0]), node('span', 'ra-arrow', '→'), node('span', null, values[1]));
        } else {
            value.textContent = values[0];
        }
        box.appendChild(value);

        if (delta) box.appendChild(node('div', `ra-card-d tone-${delta.tone}`, delta.text));
        if (sloRound) {
            const slo = node('div', `ra-slo ig-slo ig-slo-${sloRound.sloState}`);
            slo.append(node('span', 'ig-dot'), sloRound.sloText);
            box.appendChild(slo);
        }
        return box;
    }

    function comparison(root, vm) {
        const section = root.appendChild(node('section', 'ra-section'));
        const first = vm.rounds[0];
        const last = vm.rounds[vm.rounds.length - 1];
        const cards = node('div', 'ra-cards');

        if (vm.multi) {
            section.appendChild(sectionHead(`전/후 비교 (측정 ${first.round} → 측정 ${last.round})`));
            cards.append(
                card('처리량 (1초에 처리한 요청 수)', [first.tpsText, last.tpsText], vm.deltas.tps),
                card('응답 시간 (P95)', [first.p95Text, last.p95Text], vm.deltas.p95, last),
                card('오류율 (실패한 요청 비율)', [first.errText, last.errText], vm.deltas.err),
                card('서버 대수 (replica)', [first.replicasText, last.replicasText], vm.deltas.replicas),
            );
        } else {
            section.appendChild(sectionHead(`측정 결과 (측정 ${first.round}, 1회)`));
            cards.append(
                card('처리량 (1초에 처리한 요청 수)', [first.tpsText]),
                card('응답 시간 (P95)', [first.p95Text], null, first),
                card('오류율 (실패한 요청 비율)', [first.errText]),
                card('서버 대수 (replica)', [first.replicasText]),
            );
        }
        section.appendChild(cards);

        if (!vm.multi) section.appendChild(IGRefresh.note(singleRoundNote(vm.report)));   // result_panel.js
        section.appendChild(IGRefresh.note(IGRefresh.AGGREGATED_NOTE));

        if (vm.multi) {
            const grid = section.appendChild(node('div', 'ra-grid2'));
            IGRefresh.charts.roundP95Chart(grid, vm);
            IGRefresh.charts.roundTpsChart(grid, vm);
        }
    }

    // ---------------- 부하 중 그래프 ----------------

    function loadGraphs(root, vm) {
        const section = root.appendChild(node('section', 'ra-section'));
        section.appendChild(sectionHead('부하를 주는 동안의 그래프'));

        const top = section.appendChild(node('div', 'ra-grid2'));
        IGRefresh.charts.tpsTimeline(top, vm);
        IGRefresh.charts.p95Timeline(top, vm);

        const middle = section.appendChild(node('div', 'ra-grid2'));
        IGRefresh.charts.endpointChart(middle, vm);
        IGRefresh.charts.errorRateChart(middle, vm);

        const bottom = section.appendChild(node('div', 'ra-grid1'));
        IGRefresh.charts.instanceChart(bottom, vm);

        section.appendChild(IGRefresh.note(IGRefresh.LOAD_GRAPH_NOTE));
    }

    function footer(vm) {
        const foot = node('footer', 'ra-foot');
        // 측정 조건은 접지 않고, 판단 모델·결과 파일·코드 버전은 "기술 정보"로 접는다
        foot.append(
            node('div', 'ra-foot-h', '측정 조건'),
            IGRefresh.definitionList(vm.conditionItems, 'ig-dl ra-dl'),
            IGRefresh.techDetails(vm, 'ig-raw ra-tech', 'tech-info'),
        );
        if (vm.resourcesUnmeasured) foot.appendChild(IGRefresh.note(IGRefresh.referenceNotes(vm)[0]));
        return foot;
    }

    function render(report, body, meta) {
        const vm = IGRefresh.model(report, meta);
        const root = body.appendChild(node('div', 'ra'));

        const alert = IGRefresh.forcedAlert(vm);
        if (alert) root.appendChild(alert);
        root.append(header(vm), glossary(vm), verdict(vm));

        if (vm.history.length === 0) {
            root.appendChild(IGRefresh.empty('측정값 없음 (측정 전에 실행이 끝났습니다)'));
            root.appendChild(footer(vm));
            return;
        }

        root.appendChild(flow(vm));
        root.appendChild(judgement(vm));
        comparison(root, vm);
        loadGraphs(root, vm);
        root.appendChild(footer(vm));
    }

    InfraGuardView.register('detail', { label: '상세 보기', render });
})();

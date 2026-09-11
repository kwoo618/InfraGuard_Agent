/*
 * 결과 패널 — 부하 중 그래프 (docs/03 Phase 4 그래프, #75)
 *
 * measurement_history 레코드의 그래프용 필드로 그래프 4종을 그린다.
 *   ① 초 단위 TPS·P95 시계열 (라운드별 겹침) ← record.timeseries (locustfile 요청별 원시 기록, 1초 구간)
 *   ② 엔드포인트별 P95 (첫 vs 마지막 라운드) ← record.endpoints (Locust stats CSV 엔드포인트 행)
 *   ③ 에러율 (라운드별)                      ← record.error_rate (Locust Aggregated)
 *   ④ 서버별 요청 분산                       ← record.requests_by_instance (Prometheus 부하 전후 차이, /health·/metrics 제외)
 *
 * 필드가 없거나(이 기능 추가 전 결과 파일) null이면(수집 실패) "측정값 없음"을 표시한다. 값을 추정하거나 채우지 않는다.
 * result_panel.js의 헬퍼(el, svgEl, isNumber, fmt*, sectionTitle, buildBarChart)를 쓴다. 외부 라이브러리 없음.
 */

const SERIES_CLASS_COUNT = 5;   // style.css .series-0 ~ .series-4

const NO_GRAPH_DATA = `${NO_DATA} (이 기능 추가 전 실행이거나 수집 실패)`;

function seriesClass(index) {
    return `series-${index % SERIES_CLASS_COUNT}`;
}

function roundLabel(record) {
    return `R${record.round ?? '-'} · 서버 ${fmtReplicas(record.replicas)}`;
}

function emptyFigure(title, reason) {
    const figure = el('figure', 'chart');
    figure.appendChild(el('figcaption', 'chart-title', title));
    figure.appendChild(el('p', 'result-empty', reason || NO_GRAPH_DATA));
    return figure;
}

function chartLegend(items) {
    const box = el('div', 'chart-legend');

    items.forEach(({ label, className }) => {
        const item = el('span', 'legend-item');
        item.appendChild(el('span', `legend-swatch ${className}`));
        item.appendChild(el('span', null, label));
        box.appendChild(item);
    });

    return box;
}


// ---------------------------------------------------------------------
// ① 초 단위 시계열 (선 그래프, 라운드별 겹침)
// ---------------------------------------------------------------------

/**
 * series: [{ label, className, hasData, points: [{ x, y(null이면 선을 끊음), tooltip }] }]
 */
function buildLineChart({ title, series, refLine, yFormat }) {
    const figure = el('figure', 'chart');
    figure.appendChild(el('figcaption', 'chart-title', title));

    const drawable = series.filter(item => item.hasData);
    if (drawable.length === 0) {
        figure.appendChild(el('p', 'result-empty', NO_GRAPH_DATA));
        return figure;
    }

    const width = 720;
    const height = 240;
    const padLeft = 56;
    const padRight = refLine ? 88 : 16;
    const padTop = 14;
    const padBottom = 40;
    const plotWidth = width - padLeft - padRight;
    const plotHeight = height - padTop - padBottom;
    const baseY = padTop + plotHeight;

    const measuredPoints = drawable.flatMap(item => item.points.filter(point => isNumber(point.y)));
    const maxX = Math.max(1, ...measuredPoints.map(point => point.x));
    const refValue = refLine && isNumber(refLine.value) ? refLine.value : 0;
    const maxY = Math.max(...measuredPoints.map(point => point.y), refValue) * 1.1 || 1;
    const toX = x => padLeft + (x / maxX) * plotWidth;
    const toY = y => baseY - (y / maxY) * plotHeight;

    const svg = svgEl('svg', {
        viewBox: `0 0 ${width} ${height}`,
        class: 'chart-svg',
        role: 'img',
        'aria-label': title,
    });

    // y축 눈금 (0, 1/4 … 최대)
    for (let i = 0; i <= 4; i++) {
        const value = (maxY / 4) * i;
        const y = toY(value);
        svg.appendChild(svgEl('line', { x1: padLeft, x2: padLeft + plotWidth, y1: y, y2: y, class: 'chart-grid' }));
        svg.appendChild(svgEl('text', { x: padLeft - 6, y: y + 4, 'text-anchor': 'end', class: 'chart-axis-label' }, yFormat(value)));
    }

    // x축 눈금 (초, 최대 10개 안팎)
    const step = Math.max(1, Math.ceil(maxX / 10));
    for (let x = 0; x <= maxX; x += step) {
        svg.appendChild(svgEl('text', { x: toX(x), y: baseY + 16, 'text-anchor': 'middle', class: 'chart-axis-label' }, `${x}`));
    }
    svg.appendChild(svgEl('text', {
        x: padLeft + plotWidth,
        y: height - 4,
        'text-anchor': 'end',
        class: 'chart-axis-label',
    }, '부하 시작 후 경과(초)'));

    if (refLine && isNumber(refLine.value)) {
        const refY = toY(refLine.value);
        svg.appendChild(svgEl('line', { x1: padLeft, x2: padLeft + plotWidth + 4, y1: refY, y2: refY, class: 'chart-ref' }));
        svg.appendChild(svgEl('text', { x: padLeft + plotWidth + 8, y: refY + 4, class: 'chart-ref-label' }, refLine.label));
    }

    drawable.forEach(item => {
        // 요청이 없는 구간(null)에서는 선을 끊는다. 0으로 이어 그리지 않는다
        let path = '';
        let penDown = false;

        item.points.forEach(point => {
            if (!isNumber(point.y)) {
                penDown = false;
                return;
            }
            path += `${penDown ? 'L' : 'M'}${toX(point.x).toFixed(1)},${toY(point.y).toFixed(1)} `;
            penDown = true;
        });

        svg.appendChild(svgEl('path', { d: path.trim(), class: `chart-line ${item.className}` }));

        item.points.forEach(point => {
            if (!isNumber(point.y)) return;
            const dot = svgEl('circle', { cx: toX(point.x), cy: toY(point.y), r: 2.5, class: `chart-dot ${item.className}` });
            dot.appendChild(svgEl('title', {}, point.tooltip));
            svg.appendChild(dot);
        });
    });

    figure.appendChild(svg);
    figure.appendChild(chartLegend(series.map(item => ({
        label: item.hasData ? item.label : `${item.label} · ${NO_DATA}`,
        className: item.className,
    }))));
    return figure;
}

function timeseriesSeries(history, valueOf, tooltipOf) {
    return history.map((record, index) => {
        const timeseries = record.timeseries;
        const bucket = timeseries && isNumber(timeseries.bucket_sec) && timeseries.bucket_sec > 0
            ? timeseries.bucket_sec
            : 1;
        const points = timeseries && Array.isArray(timeseries.points)
            ? timeseries.points.map(point => {
                const y = valueOf(point, bucket);
                return { x: point.t, y, tooltip: tooltipOf(record, point, y) };
            })
            : [];

        return {
            label: roundLabel(record),
            className: seriesClass(index),
            points,
            hasData: points.some(point => isNumber(point.y)),
        };
    });
}

function buildTimeseriesCharts(history, slo) {
    const tps = buildLineChart({
        title: '① 초당 처리량 (req/s) · 1초 구간 완료 요청 수 · 라운드별 겹침',
        series: timeseriesSeries(
            history,
            (point, bucket) => (isNumber(point.requests) ? point.requests / bucket : null),
            (record, point, y) => `R${record.round ?? '-'} · ${point.t}초: ${fmtTps(y)} req/s (요청 ${point.requests}건, 실패 ${point.failures}건)`,
        ),
        refLine: null,
        yFormat: value => value.toFixed(0),
    });

    const p95 = buildLineChart({
        title: '② 초당 P95 (ms) · 요청별 원시 응답시간 기준 · 점선 = SLO',
        series: timeseriesSeries(
            history,
            point => (isNumber(point.p95_ms) ? point.p95_ms : null),
            (record, point, y) => `R${record.round ?? '-'} · ${point.t}초: P95 ${fmtMs(y)} (요청 ${point.requests}건)`,
        ),
        refLine: isNumber(slo) ? { value: slo, label: `SLO ${slo}ms` } : null,
        yFormat: value => `${Math.round(value)}`,
    });

    return [tps, p95];
}


// ---------------------------------------------------------------------
// ② 엔드포인트별 P95 (그룹 막대, 첫 vs 마지막 라운드)
// ---------------------------------------------------------------------

/**
 * groups: [{ label, values: [값 또는 null (series 순서)] }], series: [{ label, className }]
 */
function buildGroupedBarChart({ title, groups, series, format }) {
    const figure = el('figure', 'chart');
    figure.appendChild(el('figcaption', 'chart-title', title));

    const values = groups.flatMap(group => group.values.filter(isNumber));
    if (values.length === 0) {
        figure.appendChild(el('p', 'result-empty', NO_GRAPH_DATA));
        return figure;
    }

    const width = 400;
    const height = 240;
    const padLeft = 10;
    const padRight = 10;
    const padTop = 22;
    const padBottom = 30;
    const plotWidth = width - padLeft - padRight;
    const plotHeight = height - padTop - padBottom;
    const baseY = padTop + plotHeight;
    const maxValue = Math.max(...values) * 1.12 || 1;

    const slot = plotWidth / groups.length;
    const barWidth = Math.min(28, (slot * 0.8) / series.length);

    const svg = svgEl('svg', {
        viewBox: `0 0 ${width} ${height}`,
        class: 'chart-svg',
        role: 'img',
        'aria-label': title,
    });

    svg.appendChild(svgEl('line', { x1: padLeft, x2: padLeft + plotWidth, y1: baseY, y2: baseY, class: 'chart-axis' }));

    groups.forEach((group, groupIndex) => {
        const groupCenter = padLeft + slot * groupIndex + slot / 2;
        const groupStart = groupCenter - (barWidth * series.length) / 2;

        group.values.forEach((value, seriesIndex) => {
            const x = groupStart + barWidth * seriesIndex;
            const centerX = x + barWidth / 2;

            if (isNumber(value)) {
                const top = baseY - (value / maxValue) * plotHeight;
                const bar = svgEl('rect', {
                    x: x + 1,
                    y: top,
                    width: Math.max(barWidth - 2, 1),
                    height: Math.max(baseY - top, 0),
                    rx: 2,
                    class: `chart-bar ${series[seriesIndex].className}`,
                });
                bar.appendChild(svgEl('title', {}, `${group.label} · ${series[seriesIndex].label}: ${format(value)}`));
                svg.appendChild(bar);
                svg.appendChild(svgEl('text', { x: centerX, y: top - 4, 'text-anchor': 'middle', class: 'chart-value-small' }, format(value)));
            } else {
                svg.appendChild(svgEl('text', { x: centerX, y: baseY - 4, 'text-anchor': 'middle', class: 'chart-nodata' }, '없음'));
            }
        });

        svg.appendChild(svgEl('text', { x: groupCenter, y: baseY + 17, 'text-anchor': 'middle', class: 'chart-label' }, group.label));
    });

    figure.appendChild(svg);
    figure.appendChild(chartLegend(series));
    return figure;
}

function buildEndpointChart(history) {
    const title = '③ 엔드포인트별 P95 (ms) · Locust 기준 · 첫 vs 마지막 라운드';
    const rounds = history.filter(record => Array.isArray(record.endpoints) && record.endpoints.length > 0);

    if (rounds.length === 0) {
        return emptyFigure(title, NO_GRAPH_DATA);
    }

    const chosen = rounds.length >= 2 ? [rounds[0], rounds[rounds.length - 1]] : [rounds[0]];
    const names = [];
    chosen.forEach(record => record.endpoints.forEach(endpoint => {
        if (!names.includes(endpoint.name)) names.push(endpoint.name);
    }));

    return buildGroupedBarChart({
        title,
        groups: names.map(name => ({
            label: name,
            values: chosen.map(record => {
                const endpoint = record.endpoints.find(item => item.name === name);
                return endpoint && isNumber(endpoint.p95_ms) ? endpoint.p95_ms : null;
            }),
        })),
        series: chosen.map(record => ({ label: roundLabel(record), className: seriesClass(history.indexOf(record)) })),
        format: value => `${Math.round(value)}`,
    });
}


// ---------------------------------------------------------------------
// ③ 에러율 (라운드별 막대, result_panel.js buildBarChart 재사용)
// ---------------------------------------------------------------------

function buildErrorRateChart(history) {
    return buildBarChart({
        title: '④ 에러율 (%) · 라운드별 · Locust 전체 요청 기준',
        bars: history.map(record => ({
            label: `R${record.round ?? '-'}`,
            sublabel: `서버 ${fmtReplicas(record.replicas)}`,
            value: isNumber(record.error_rate) ? record.error_rate * 100 : null,
            tone: 'err',
        })),
        refLine: null,
        format: value => `${value.toFixed(2)}%`,
    });
}


// ---------------------------------------------------------------------
// ④ 서버별 요청 분산 (가로 막대, 라운드별 묶음)
// ---------------------------------------------------------------------

function instanceShortName(instance) {
    const host = String(instance).split(':')[0];
    return `서버 .${host.split('.').pop()}`;
}

function buildInstanceChart(history) {
    const title = '⑤ 서버별 요청 분산 · Prometheus 부하 전후 차이 · /health·/metrics 제외';
    const rounds = history.filter(record => {
        const data = record.requests_by_instance;
        return data && data.counts && Object.keys(data.counts).length > 0;
    });

    if (rounds.length === 0) {
        return emptyFigure(title, NO_GRAPH_DATA);
    }

    const figure = el('figure', 'chart');
    figure.appendChild(el('figcaption', 'chart-title', title));

    // 모든 라운드를 같은 척도로 그린다 (스케일 전/후 서버당 요청 수 비교)
    const allCounts = rounds.flatMap(record => Object.values(record.requests_by_instance.counts).filter(isNumber));
    const maxCount = Math.max(1, ...allCounts);

    const width = 720;
    const labelWidth = 150;
    const barMax = 400;
    const rowHeight = 22;
    const headerHeight = 22;
    const groupGap = 10;
    const rows = rounds.reduce((sum, record) => sum + Object.keys(record.requests_by_instance.counts).length, 0);
    const height = rounds.length * (headerHeight + groupGap) + rows * rowHeight + 6;

    const svg = svgEl('svg', {
        viewBox: `0 0 ${width} ${height}`,
        class: 'chart-svg',
        role: 'img',
        'aria-label': title,
    });

    let y = 4;

    rounds.forEach(record => {
        const index = history.indexOf(record);
        const counts = record.requests_by_instance.counts;
        const total = Object.values(counts).filter(isNumber).reduce((sum, value) => sum + value, 0);

        svg.appendChild(svgEl('text', { x: 0, y: y + 15, class: 'chart-label' }, roundLabel(record)));
        y += headerHeight;

        Object.entries(counts).forEach(([instance, count]) => {
            const label = svgEl('text', { x: labelWidth - 8, y: y + 15, 'text-anchor': 'end', class: 'chart-sublabel' }, instanceShortName(instance));
            label.appendChild(svgEl('title', {}, instance));
            svg.appendChild(label);

            if (isNumber(count)) {
                const barWidth = (count / maxCount) * barMax;
                svg.appendChild(svgEl('rect', {
                    x: labelWidth,
                    y: y + 3,
                    width: Math.max(barWidth, 1),
                    height: rowHeight - 7,
                    rx: 2,
                    class: `chart-bar ${seriesClass(index)}`,
                }));
                const share = total > 0 ? `${Math.round((count / total) * 100)}%` : '-';
                svg.appendChild(svgEl('text', {
                    x: labelWidth + barWidth + 6,
                    y: y + 15,
                    class: 'chart-value-small',
                }, `${count}건 (${share})`));
            } else {
                svg.appendChild(svgEl('text', { x: labelWidth, y: y + 15, class: 'chart-nodata' }, `${NO_DATA} (카운터 초기화)`));
            }

            y += rowHeight;
        });

        y += groupGap;
    });

    figure.appendChild(svg);
    figure.appendChild(el(
        'p',
        'result-note',
        '서버 이름은 컨테이너 IP 끝자리입니다. /health는 Docker 헬스체크 요청과, /metrics는 Prometheus 수집 요청과 구분할 수 없어 뺐습니다.',
    ));
    return figure;
}


// ---------------------------------------------------------------------
// 섹션
// ---------------------------------------------------------------------

function buildLoadGraphsSection(report) {
    const section = el('section', 'result-section');
    section.appendChild(sectionTitle('📊 부하 중 그래프'));

    const history = report.measurement_history || [];
    if (history.length === 0) {
        section.appendChild(el('p', 'result-empty', NO_DATA));
        return section;
    }

    const slo = report.conditions ? report.conditions.p95_slo_ms : null;

    const timeline = el('div', 'chart-stack');
    buildTimeseriesCharts(history, slo).forEach(chart => timeline.appendChild(chart));
    section.appendChild(timeline);

    const bars = el('div', 'chart-row');
    bars.append(buildEndpointChart(history), buildErrorRateChart(history));
    section.appendChild(bars);

    const distribution = el('div', 'chart-stack');
    distribution.appendChild(buildInstanceChart(history));
    section.appendChild(distribution);

    section.appendChild(el(
        'p',
        'result-note',
        '①② 초 단위 값은 요청별 원시 응답시간으로 1초 구간마다 계산했습니다(구간 P95는 nearest-rank). '
        + '요청이 없는 구간은 비워 둡니다. 위 카드의 Locust 요약 P95와 조금 다를 수 있습니다.',
    ));

    return section;
}

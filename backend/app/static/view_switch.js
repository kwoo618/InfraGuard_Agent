/*
 * 보기 전환: 기본 보기(simple, refresh/c.js) · 상세 보기(detail, refresh/a.js) (디자인 개편 #93)
 *
 * URL ?view=simple|detail 로 유지하고 기본값은 simple이다. <head>에서 먼저 실행해 보기별 스타일시트를 고른다.
 * - 스타일시트: <link data-view-sheet="detail">처럼 켤 보기를 적는다. 해당하지 않는 시트는 media="not all".
 *   (disabled로 켜고 끄면 Chrome이 시트를 다시 불러오느라 전환 직후 스타일이 빠진 화면이 보였다.)
 * - 결과 패널: 받아 둔 report 하나를 현재 보기의 렌더러로 그린다. 보기를 바꾸면 다시 조회하지 않고 다시 그린다.
 * - 백엔드 문구(SSE 메시지)의 앞머리 이모지는 표시할 때만 뗀다. 원문은 바꾸지 않는다.
 * - 비교용 4개 스타일 시절의 ?style= 파라미터는 읽지 않고 주소에서 지운다.
 */
(function () {
    const VIEWS = ['simple', 'detail'];
    const DEFAULT_VIEW = 'simple';
    const RESIZE_DEBOUNCE_MS = 200;
    // 이 너비 이상 바뀌었을 때만 그래프를 다시 그린다 (그래프는 실제 너비로 그린다)
    const RESIZE_MIN_DELTA_PX = 24;

    // 백엔드 문구 앞머리의 장식 (이모지, 변형 선택자, 결합 문자, 공백)
    const LEADING_DECORATION = /^[\p{Extended_Pictographic}\p{Emoji_Modifier}️‍\s]+/u;

    const renderers = {};
    const listeners = [];
    let current = readViewFromUrl();
    let shown = null;          // { report, meta: { source: 'live' | 'saved', fileName, startedAt, startedAtText } }
    let renderedWidth = 0;
    let resizeTimer = null;

    function readViewFromUrl() {
        const value = new URLSearchParams(window.location.search).get('view');
        return VIEWS.includes(value) ? value : DEFAULT_VIEW;
    }

    function writeViewToUrl(view) {
        // debug 등 다른 파라미터는 그대로 둔다
        const url = new URL(window.location.href);
        url.searchParams.set('view', view);
        url.searchParams.delete('style');
        window.history.replaceState(null, '', url);
    }

    function applyStyleSheets(view) {
        document.querySelectorAll('link[data-view-sheet]').forEach(link => {
            link.media = link.dataset.viewSheet === view ? 'all' : 'not all';
        });
        document.documentElement.dataset.view = view;
    }

    function syncButtons() {
        document.querySelectorAll('[data-view-option]').forEach(button => {
            button.setAttribute('aria-pressed', String(button.dataset.viewOption === current));
        });
    }

    function plainText(text) {
        const value = String(text ?? '');
        const stripped = value.replace(LEADING_DECORATION, '');
        return stripped || value;
    }

    function notify(reason) {
        listeners.forEach(listener => {
            try {
                listener(reason, current);
            } catch (error) {
                console.error('[view] 변경 알림 처리 실패:', error);
            }
        });
    }

    // ---------------------------------------------------------------
    // 결과 패널
    // ---------------------------------------------------------------

    function panelBody() {
        return document.getElementById('result-body');
    }

    function render() {
        if (!shown) return;

        const renderer = renderers[current];
        const panel = document.getElementById('result-panel');
        const body = panelBody();
        if (!renderer || !panel || !body) return;

        // 다시 그려도 사용자가 펼쳐 둔 접힘 영역은 유지한다
        const openKeys = new Set(
            [...body.querySelectorAll('details[data-key][open]')].map(node => node.dataset.key),
        );

        body.replaceChildren();
        panel.classList.remove('hidden');   // 그래프 너비를 재려면 먼저 보여야 한다
        renderer.render(shown.report, body, shown.meta);

        body.querySelectorAll('details[data-key]').forEach(node => {
            if (openKeys.has(node.dataset.key)) node.open = true;
        });
        renderedWidth = body.clientWidth;
    }

    function showRenderError(error) {
        console.error('[view] 결과 패널 렌더링 실패:', error);
        const body = panelBody();
        if (body) body.textContent = '결과 패널을 이 보기로 표시하지 못했습니다. 다른 보기를 선택해 보세요.';
    }

    /** report를 받아 두고 현재 보기로 그린다. 렌더링 오류는 호출한 쪽으로 던진다. */
    function showReport(report, meta) {
        shown = { report, meta: meta || { source: 'live' } };
        const body = panelBody();
        if (body) body.replaceChildren();
        document.documentElement.dataset.hasReport = 'true';
        notify('report');
        render();
    }

    function clearReport() {
        shown = null;
        delete document.documentElement.dataset.hasReport;
        resetResultPanel();   // result_panel.js
        notify('report');
    }

    function setView(view) {
        if (!VIEWS.includes(view) || view === current) return;

        current = view;
        applyStyleSheets(view);
        writeViewToUrl(view);
        syncButtons();
        notify('view');

        try {
            render();
        } catch (error) {
            showRenderError(error);
        }
    }

    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
            const body = panelBody();
            if (!shown || !body) return;
            if (Math.abs(body.clientWidth - renderedWidth) < RESIZE_MIN_DELTA_PX) return;

            try {
                render();
            } catch (error) {
                showRenderError(error);
            }
        }, RESIZE_DEBOUNCE_MS);
    });

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('[data-view-option]').forEach(button => {
            button.addEventListener('click', () => setView(button.dataset.viewOption));
        });
        syncButtons();
        if (new URLSearchParams(window.location.search).has('style')) writeViewToUrl(current);
    });

    window.InfraGuardView = {
        VIEWS,
        register(name, renderer) {
            renderers[name] = renderer;
        },
        getView: () => current,
        setView,
        showReport,
        clearReport,
        shownReport: () => shown,
        onChange(listener) {
            listeners.push(listener);
        },
        plainText,
    };

    applyStyleSheets(current);
})();

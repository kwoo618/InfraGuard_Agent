/*
 * 저장된 실행 기록 불러오기 (#93)
 *
 * docs/evidence/의 결과 파일 목록을 드롭다운으로 보여 주고, 고른 파일을 결과 패널에 그린다 (4개 스타일 모두).
 * - 데이터: GET /api/v1/results/saved (목록), GET /api/v1/results/saved/{파일명} (report = /report와 같은 모양, saved = 파일 정보)
 * - 불러온 동안 도구 막대에 "저장된 실행 기록: 파일명, 실행 시각"을 항상 띄운다. 실시간 실행과 헷갈리지 않게 하기 위해서다.
 * - 에이전트 실행 중(승인 대기 포함)·서버 수 초기화 중에는 막는다 (main.js syncControlButtons가 setBlocked를 부른다).
 * - 파일 값만 쓴다. 옵션 문구도 파일의 실행 시각·동시 가상 사용자 수·서버 수·종료 경로로 만든다.
 */
(function () {
    const select = document.getElementById('saved-select');
    const loadBtn = document.getElementById('saved-load-btn');
    const errorNote = document.getElementById('saved-error');
    const banner = document.getElementById('saved-banner');
    const bannerText = document.getElementById('saved-banner-text');
    const exitBtn = document.getElementById('saved-exit-btn');

    // 결과 파일 outcome.end_reason (run_history.py)
    const END_REASON_TEXT = {
        scaled: '스케일링 완료',
        no_further_scaling: '추가 스케일링 미제안',
        no_scaling_proposed: '스케일링 미제안',
        rejected: '제안 거절',
        failed: '실행 실패',
        stream_closed: '연결 종료',
    };

    let blocked = false;
    let loading = false;
    let listReady = false;
    let active = null;   // 불러온 파일 정보 (saved)

    function pad(value) {
        return String(value).padStart(2, '0');
    }

    function parseTime(iso) {
        const date = iso ? new Date(iso) : null;
        return date && !Number.isNaN(date.getTime()) ? date : null;
    }

    /** 실행 시각 (결과 파일 started_at, 이 브라우저의 로컬 시각) */
    function fmtDateTime(iso) {
        const date = parseTime(iso);
        if (!date) return '실행 시각 기록 없음';
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
            + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    }

    function fmtShortTime(iso) {
        const date = parseTime(iso);
        if (!date) return '시각 없음';
        return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }

    function optionText(item) {
        if (item.error) return `${item.file_name} (읽을 수 없음)`;

        const parts = [fmtShortTime(item.started_at)];
        if (typeof item.virtual_users === 'number') parts.push(`${item.virtual_users}명`);

        const replicas = Array.isArray(item.replicas) ? item.replicas : [];
        if (replicas.length > 0) {
            parts.push(`서버 ${replicas.map(value => (typeof value === 'number' ? value : '?')).join('→')}대`);
        }

        parts.push(END_REASON_TEXT[item.end_reason] || item.end_reason || '종료 경로 기록 없음');
        if (item.forced_scaling) parts.push('디버그 강제 실행');
        return parts.join(' · ');
    }

    function showError(text) {
        errorNote.textContent = text;
        errorNote.classList.toggle('hidden', !text);
    }

    function sync() {
        select.disabled = blocked || loading || !listReady;
        loadBtn.disabled = blocked || loading || !select.value;
        exitBtn.disabled = loading;
    }

    async function loadList() {
        try {
            const response = await fetch('/api/v1/results/saved');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);

            const data = await response.json();
            const files = Array.isArray(data.files) ? data.files : [];
            // 화면 문구는 "저장된 실행 기록"만 쓴다 (데이터 출처는 그대로 docs/evidence)
            const placeholder = files.length > 0
                ? `저장된 실행 기록 ${files.length}개 중 선택`
                : '저장된 실행 기록이 없습니다';

            select.replaceChildren(new Option(placeholder, ''));
            files.forEach(item => {
                const option = new Option(optionText(item), item.file_name);
                option.disabled = Boolean(item.error);
                select.appendChild(option);
            });
            listReady = files.length > 0;
        } catch (error) {
            console.warn('[saved-results] 목록 조회 실패:', error);
            select.replaceChildren(new Option('저장된 실행 기록 목록을 불러오지 못했습니다', ''));
            listReady = false;
        }
        sync();
    }

    async function loadSelected() {
        const fileName = select.value;
        if (blocked || loading || !fileName) return;

        loading = true;
        showError('');
        sync();

        try {
            const response = await fetch(`/api/v1/results/saved/${encodeURIComponent(fileName)}`);
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);

            // 조회하는 사이 새 실행이 시작됐으면 그 실행 화면을 덮지 않는다
            if (blocked) return;

            active = data.saved;
            enterSavedView();   // main.js: 로그·진행 표시를 비운다

            bannerText.textContent = `저장된 실행 기록: ${data.saved.file_name}, ${fmtDateTime(data.saved.started_at)}`;
            banner.classList.remove('hidden');

            try {
                InfraGuardView.showReport(data.report, {
                    source: 'saved',
                    fileName: data.saved.file_name,
                    startedAt: data.saved.started_at,
                    startedAtText: fmtDateTime(data.saved.started_at),
                });
            } catch (renderError) {
                console.error('[saved-results] 결과 패널 렌더링 실패:', renderError);
                showError('결과 패널을 표시하지 못했습니다.');
            }
        } catch (error) {
            console.warn('[saved-results] 파일 조회 실패:', error);
            showError(`불러오지 못했습니다: ${error.message}`);
        } finally {
            loading = false;
            sync();
        }
    }

    /** 저장 기록 보기를 끝낸다. quiet면 결과 패널·로그는 호출한 쪽(새 실행 시작)이 정리한다. */
    function exit({ quiet = false } = {}) {
        if (!active) return;

        active = null;
        banner.classList.add('hidden');
        bannerText.textContent = '';
        select.value = '';
        showError('');

        if (!quiet) leaveSavedView();   // main.js
        sync();
    }

    select.addEventListener('change', sync);
    loadBtn.addEventListener('click', loadSelected);
    exitBtn.addEventListener('click', () => exit());

    window.SavedResults = {
        setBlocked(value) {
            blocked = Boolean(value);
            sync();
        },
        exit,
        isActive: () => active !== null,
    };

    loadList();
})();

# 디자인 개편 대표 스크린샷 (#93)

기본 보기(C 미니멀형)와 상세 보기(A 관제 콘솔형) 확정 구성의 대표 화면이다. 2026-09-12 브랜치 `feature/design-refresh`에서 캡처했다.
데스크톱은 폭 1100px, 모바일은 390px 틀에서 결과 패널 위치를 찍었다.

| 파일 | 보기 | 상태 | 데이터 출처 |
|---|---|---|---|
| `c-scaled-005741.png` | 기본 | 2회 루프 성공 (서버 1→2→4대, SLO 충족) — 첫 화면 | `docs/evidence/20260912_005741_…json` |
| `c-scaled-005741-lower.png` | 기본 | 같은 실행 — 지표 4개, "AI가 이렇게 판단한 이유" 펼침 | 같음 |
| `c-no-proposal-slo-met-012415.png` | 기본 | 스케일링 미제안, SLO 충족 (5명) | `docs/evidence/20260912_012415_…json` |
| `c-no-proposal-slo-missed-005124.png` | 기본 | 스케일링 미제안, SLO 미충족 (50명) | `docs/evidence/20260912_005124_…json` |
| `c-rejected-verification-034443.png` | 기본 | AI 제안(1→2대) 거절 | **검증용 실행** `results/20260912_034443_…json` (evidence 아님, 발표 수치 아님) |
| `c-failed-012257.png` | 기본 | 실행 실패 (LLM 응답 형식 오류, 측정 1회) | `docs/evidence/20260912_012257_…json` |
| `a-scaled-005741.png` | 상세 | 2회 루프 성공 — 판정 띠·측정 흐름·AI 판단 근거 | `docs/evidence/20260912_005741_…json` |
| `mobile-c-005741.png` | 기본 (390px) | 2회 루프 성공 | `docs/evidence/20260912_005741_…json` |
| `mobile-a-005741.png` | 상세 (390px) | 2회 루프 성공 | 같음 |

- 거절 화면은 evidence에 거절 기록이 없어 검증용 실행(50명 30초, AI가 1→2대 제안 → 사용자 역할로 거절)으로 만들었다. 발표 수치로 쓰지 않는다.
- 화면의 수치는 결과 파일 값과 같다 (docs/03 "디자인 개편" 절 완료 기준, 대조 불일치 0건).
- 시안 원본: `docs/design/mockups.html` (기록용).

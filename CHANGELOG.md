# Changelog | 변경 이력

이 문서는 개발 마일스톤별 변경 사항을 기록합니다. 아직 정식 릴리스 버전 변경은 아닙니다. / This log records development milestones, not new formal releases.

## [Unreleased] — Part 5-2 (planned / 예정)

- 다양한 타임스탬프 형식을 위한 확장형 패턴 파서 및 기존 인터페이스 호환성. / Extensible pattern parser with backward-compatible interfaces.
- `YYYYMMDD-HHmmss`, `YYYY_MM_DD_HH_mm_ss_SSS` 등 지원 검토. / Evaluate additional timestamp formats, including the two examples above.
- 소스 격리 및 모호한 시간 해석 방지. / Preserve source isolation and reject ambiguous interpretations.
- 사용자 정의 템플릿, 프리셋 및 미리보기는 후속 단계에서 검토. / Custom templates, presets and previews are candidates for a later milestone.

## 2026-11-27 — Part 5-1: Filename compatibility validation

### Motivation | 개발 배경

기존 파서는 접두사가 없는 Agent DVR 타임스탬프에 의존했습니다. 카메라 ID나 장치명이 포함된 실제 녹화 파일 479개가 기존 C500 조사에서 인식되지 않아, 다른 사용자 환경에 대응하기 위한 첫 단계로 접두사 지원을 추가했습니다.

The original parser expected an unprefixed Agent DVR timestamp. In the C500 inventory, 479 recordings with a numeric prefix were not recognized. Flexible prefix support is a first step toward broader recorder compatibility.

### Added and changed | 개선 사항

- 선택적 접두사 지원 및 기존 파일명/`RecordingInfo` 호환성 유지. / Optional prefixes with legacy filename and `RecordingInfo` compatibility.
- 잘못된 날짜·시간 거부 및 시간순 정렬 유지. / Reject invalid dates and times and preserve chronological sorting.
- 소스 폴더 혼합 및 명시적 접두사 충돌 검사. / Reject mixed source folders and conflicting explicit labels; numeric IDs are not proof of camera identity.
- SQLite 런타임 보조 파일과 작업 잠금·복구 상태 파일의 Git 제외 규칙 추가. / Ignore SQLite sidecars, job locks and recovery state files.

### Validation | 검증 결과

| 항목 / Check | 결과 / Result |
|---|---|
| C500 인식률 / Recognition | 7,378/7,857 (93.90%) → 7,857/7,857 (100%) |
| 신규 4채널 / Four-channel filenames | 63/63 |
| 전체 파일명 검사 / Total filename checks | 7,920 |
| 카탈로그·계획 / Catalog and planning | 52/52 PASS |
| 병합 준비 안전성 / Merge preparation safety | 27/27 PASS |
| C500 혼합 파일명 병합 / Mixed filenames | 2/2 PASS |
| 신규 4채널 병합 / Four-channel merges | 8/8 PASS |
| A/B 다중 분할 / Multi-part merges | 4/4 PASS |
| 전체 실제 병합·메타데이터 / Real merge and metadata checks | 14/14 PASS |
| 검사 대상 입력 해시 / Tested input SHA-256 | Unchanged |
| 회귀 테스트 / Regression tests | 210 passed, 1 skipped, 0 failed (211 total; 3.581 s) |

C500 8개 파일의 계획 기준 Part 간 편차: A 1,241.288초 → B 559.482초 (약 54.93% 감소). / Planned duration spread on the eight-file C500 sample: 1,241.288 s (A) → 559.482 s (B), approximately 54.93% lower.

### Limitations | 제한사항

- 현재 타임스탬프는 `YYYY-MM-DD_HH-mm-ss_SSS`로 고정되어 있으며 접미사 및 밀리초 생략은 지원하지 않습니다. / The timestamp format remains fixed; suffixes and omitted milliseconds are unsupported.
- 병합 결과의 전체 디코딩과 SEI/DTS 원인 조사는 보류했습니다. 메타데이터 검사 성공은 프레임 단위 무결성을 보장하지 않습니다. / Full output decoding and SEI/DTS investigation were deferred; metadata checks do not guarantee frame-level integrity.
- 전체 데이터 병합이나 모든 NVR 형식 호환을 검증한 것은 아닙니다. / Neither full-inventory merging nor universal NVR compatibility was verified.
- 기존 무결성 정책은 유지했습니다. / Existing integrity policy remains unchanged.

Code commits: [`332cb6e`](https://github.com/GinGer-JSK/CCTV-Archive-Manager/commit/332cb6e), [`8fc1b20`](https://github.com/GinGer-JSK/CCTV-Archive-Manager/commit/8fc1b20).

## Earlier milestones | 이전 마일스톤

- Part 6 (in progress): SQLite 작업 스키마 및 트랜잭션 상태 관리 구현, 실제 작업 엔진 연동과 원클릭 복구는 미완료. / Job schema and transactional state management implemented; engine integration and one-click recovery remain pending. Commits `0a85409`, `2f18f70`.
- Part 5 (2026-11-25): 실제 재생시간 기반 분할, JSON 체크포인트, 무결성 검증 및 승인 기반 복구. / Duration-based partitioning, JSON checkpoints, integrity checks and approved recovery. Commit `0a17248`.

See [README](README.md) for the current feature matrix and historical validation results.

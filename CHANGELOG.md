# Changelog | 변경 이력

이 문서는 개발 마일스톤별 변경 사항을 기록합니다. 아직 정식 릴리스 버전 변경은 아닙니다. / This log records development milestones, not new formal releases.

## [Unreleased] — Part 5-3 (proposed / 검토 예정)

- 사용자 정의 파일명 템플릿, 프리셋 및 분석 미리보기 검토. / Evaluate custom filename templates, presets and parsing previews.
- Part 6 작업 엔진 연동 및 원클릭 복구는 기존 보류 상태 유지. / Part 6 engine integration and one-click recovery remain paused.

## 2026-11-27 — Part 5-2: Multi-format filename parser

### Added and changed | 개선 사항

- 12개 내장 타임스탬프 패턴(P01–P12) 및 선택적 접두사·접미사 자동 분석. / Automatic recognition of 12 built-in timestamp patterns with optional prefixes and suffixes.
- `RecordingInfo` 및 기존 공개 파서 인터페이스 유지, `FilenameMatch` 및 `analyze_recording_filename()` 추가. / Preserve `RecordingInfo` and the existing public parser interface; add detailed match metadata.
- 밀리초 생략 시 0ms 적용, 잘못된 날짜·시간 및 모호한 타임스탬프 거부. / Default omitted milliseconds to zero; reject invalid dates/times and ambiguous timestamps.
- Source Guard를 다중 패턴 분석과 연동하여 혼합 폴더, 충돌 이름 집합 및 채널 ID 차단. / Integrate Source Guard with multi-format parsing and reject mixed folders, conflicting label sets and channel IDs.
- 기존 SQLite 카탈로그 스키마 및 GUI 코드 유지. / Retain the existing catalog schema and GUI code.
- 신규 회귀 테스트 5개 추가. / Add five regression tests.

### Validation | 검증 결과

| 항목 / Check | 결과 / Result |
|---|---|
| 기존 C500 파일명 / Legacy C500 exact matches | 7,857/7,857 PASS; 0 mismatches |
| 기존·신규 파서 시간 / Legacy vs new parser | 0.138369 s / 0.248854 s (single run) |
| 정상 가상 파일명 / Synthetic valid filenames | 57,600/57,600 PASS |
| 비정상 파일명 거부 / Invalid filename rejection | 18/18 PASS |
| 전체 가상 검사 / Synthetic matrix | 57,618/57,618 PASS (4.344 s) |
| 실제 MKV 패턴 적용 / Real MKV renamed copies | 12/12 PASS |
| A/B 병합 및 메타데이터 / Real A/B merges and metadata | 6/6 PASS; max duration difference 348 ms |
| 로컬 입력 해시 / Local input SHA-256 | 12/12 unchanged |
| SQLite 카탈로그 / SQLite catalog | 12 rows; integrity_check: ok |
| 재생시간 캐시 / Duration cache | 12 probes → 12 hits, 0 probes |
| 실제 GUI 조회·계획 / Real GUI lookup and planning | PASS; 6 screenshots, 0 dialogs |
| 회귀 테스트 / Regression suite | 215 passed, 1 skipped, 0 failed (216 total; 2.569 s) |

### Limitations | 제한사항

- 내장 패턴에 대한 검증이며 사용자 정의 규칙 및 모든 제조사 호환성은 미검증입니다. / Built-in patterns were validated; custom rules and universal vendor compatibility remain unverified.
- 실제 병합은 메타데이터 검사 기준이며 전체 디코딩은 미실행입니다. / Real merges were checked at metadata level; full decoding was not performed.
- 외장 HDD 원본 해시는 복사 시 비교했고 최종 재시도에서는 크기·수정 시각만 재확인했습니다. 초기 I/O 오류 원인은 미확정입니다. / External original hashes were compared at copy time; the final retry rechecked size and modification time only. The initial I/O error remains unexplained.
- 운영 DB 미사용, 기존 무결성 정책 유지. 심볼릭 링크 테스트 1개는 권한 제한으로 건너뛰었습니다. / Production DB not used; integrity policy unchanged. One symlink test was skipped due to permissions.

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

- 제5-1부 당시 타임스탬프는 `YYYY-MM-DD_HH-mm-ss_SSS`로 고정되어 있었으며 접미사 및 밀리초 생략은 미지원이었습니다(제5-2부에서 확장). / At Part 5-1, the timestamp format was fixed, without suffix or omitted-millisecond support (expanded in Part 5-2).
- 병합 결과의 전체 디코딩과 SEI/DTS 원인 조사는 보류했습니다. 메타데이터 검사 성공은 프레임 단위 무결성을 보장하지 않습니다. / Full output decoding and SEI/DTS investigation were deferred; metadata checks do not guarantee frame-level integrity.
- 전체 데이터 병합이나 모든 NVR 형식 호환을 검증한 것은 아닙니다. / Neither full-inventory merging nor universal NVR compatibility was verified.
- 기존 무결성 정책은 유지했습니다. / Existing integrity policy remains unchanged.

Code commits: [`332cb6e`](https://github.com/GinGer-JSK/CCTV-Archive-Manager/commit/332cb6e), [`8fc1b20`](https://github.com/GinGer-JSK/CCTV-Archive-Manager/commit/8fc1b20).

## Earlier milestones | 이전 마일스톤

- Part 6 (in progress): SQLite 작업 스키마 및 트랜잭션 상태 관리 구현, 실제 작업 엔진 연동과 원클릭 복구는 미완료. / Job schema and transactional state management implemented; engine integration and one-click recovery remain pending. Commits `0a85409`, `2f18f70`.
- Part 5 (2026-11-25): 실제 재생시간 기반 분할, JSON 체크포인트, 무결성 검증 및 승인 기반 복구. / Duration-based partitioning, JSON checkpoints, integrity checks and approved recovery. Commit `0a17248`.

See [README](README.md) for the current feature matrix and historical validation results.

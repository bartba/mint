# CLAUDE.md — 작업 지침

제품 범위·의사결정·KPI 상세는 [docs/prd.md](./docs/prd.md) 참조 (필요 시에만 로드).
아래 "제품 핵심" 섹션은 docs/prd.md의 KPI/Scope/Out of Scope 변경 시 동기화. 그 외 PRD 갱신은 반영 불필요.

## 개발 원칙
- 구현이 [제품 핵심] 섹션 또는 docs/prd.md의 범위·의사결정에서 벗어나면 코드 작성 전 사용자 확인.

> 프로젝트 개요·아키텍처·데이터 흐름·인텐트 목록은 [README.md](./README.md) 참조. 이 문서는 코드 작성·수정 시 따를 규약과 확장 절차만 다룬다.

## 제품 핵심 (코드 작업 시 준수)

- **지연 예산 (P95)**: 발화 종료→첫 TTS ≤ 2.5s / STT ≤ 600ms / 인텐트 분류 ≤ 80ms.
- **P0 인텐트**: `end_session`, `pause_listening`(10분 고정 — 정규식 추출 없음). `list_sessions` 및 가변 duration은 P1. 디바이스 제어 인텐트도 P1.
- **`display_enabled` 계약**: SPOKEN은 두 모드 모두 항상 TTS로 전달, DISPLAY 섹션만 토글 대상. `display_enabled: true`여도 첫 TTS 지연 증가 ≤ 100ms.
- **LLM**: 단일 프로바이더 (런타임 전환 없음). 실패 시 안내 메시지 후 세션 종료. fallback chain은 Backlog.
- **프라이버시**: 원본 PCM 디스크 저장 금지. Cloud로는 추론 시점 텍스트만 전송.
- **Out of Scope (영구 제외)**: Barge-in, 모바일 앱, SaaS 호스팅.

위 사항이 변경되어야 한다고 판단되면 코드 수정 전 사용자 확인 후 [docs/prd.md](./docs/prd.md)도 함께 갱신.

## 디렉터리

- `shared/` — 디바이스 프로파일, 공용 유틸. `device_profiles.py`에 디바이스별 메모리 예산/GPIO/sysfs 경로 정의.
- `edge/` — 마이크/스피커 I/O, VAD, 웨이크워드, TTS.
- `server/` — STT, 임베딩 인텐트 분류, Cloud 중계, FastAPI 대시보드. 인텐트 핸들러는 `server/intent_handlers.py`.
- `proto/` — gRPC 계약 (`voice_service.proto`).
- `config/` — 런타임 설정 (`server.yaml` 등).

## 코드 규약

- Python 3.10+, type hints 필수, asyncio 기반.
- snake_case (함수/변수), PascalCase (클래스).
- gRPC 호출은 try/except + 폴백 필수.
- API 키는 환경변수/`.env`로만 주입. 코드 하드코딩 금지.
- 로깅은 Python `logging` 사용, 레벨은 config로 관리.
- 단순·간결 우선. 복잡성 대비 성능 향상이 작은 최적화는 지양.
- 가독성 우선. 3줄 중복이 불필요한 추상화보다 낫다.

## 확장 절차

- **새 디바이스 추가** — `shared/device_profiles.py`에 프로파일만 등록. 나머지 코드는 변경 없이 동작해야 한다. README의 지원 디바이스 표도 함께 갱신.
- **새 인텐트 추가** — `config/server.yaml`의 `intents` 섹션에 예시 문장 추가 + `server/intent_handlers.py`에 핸들러 함수 추가. README의 인텐트 표에 항목 추가.
- **proto 변경** — `proto/voice_service.proto` 수정 후 stub 재생성. README의 통신 계약 절도 함께 점검.

## 커밋

- Prefix: `edge:`, `server:`, `proto:`, `shared:`, `docs:`, `infra:`.

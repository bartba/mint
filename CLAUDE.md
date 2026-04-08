# CLAUDE.md — Voice Assistant Project

## 프로젝트 개요

3-Tier 분산 AI 음성 비서: Edge(Jetson Nano 4GB) → Server(Jetson Orin Nano 8GB) → Cloud(Claude/GPT).
음성 입력을 실시간 처리하고, 질문 언어(한국어/영어)에 맞춰 해당 언어 음성으로 응답.

## 아키텍처 원칙

- **Edge**: 사용자 I/O (마이크/스피커, Silero VAD, openWakeWord, ReSpeaker XVF3800)
- **Server**: AI 추론 (Whisper-large-v3-turbo STT, Supertonic TTS), 인텐트 라우팅, Cloud API 중계
- **Cloud**: LLM 대화/응답 생성 (Haiku 기본, Sonnet 승격)
- Server/Cloud 단절 시 Edge 단독 폴백

## 데이터 흐름 핵심

- Server가 STT → 인텐트 체크 → Cloud LLM → TTS를 일관 처리. Edge에는 TTS 오디오만 전달.
- 로컬 인텐트(종료 등) 매칭 시 Cloud 스킵, Server에서 즉시 TTS 응답.
- 대화 타임아웃: Edge가 타이머 관리 (15초 무발화 → 의사확인, 5초 추가 무발화 → 세션 종료).
- TTS 재생 중 타임아웃 타이머 중지. IDLE 전환 시 Server 대화 히스토리 초기화.

## 통신 계약

- Edge ↔ Server: gRPC (`proto/voice_service.proto` 참조)
- Server → Cloud: HTTPS
- 상세 메시지 정의는 proto 파일 참조

## 코드 규약

- Python 3.10+, type hints 필수, asyncio 기반
- snake_case (함수/변수), PascalCase (클래스)
- gRPC 호출은 try/except + 폴백 필수
- API 키는 환경변수/.env, 코드 하드코딩 금지
- 로깅: Python logging, 레벨은 config 관리
- Commit prefix: `edge:`, `server:`, `proto:`, `shared:`, `docs:`, `infra:`
- proto 변경 시 양쪽 CLAUDE.md도 함께 업데이트

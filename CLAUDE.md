# CLAUDE.md — Voice Assistant Project

## 프로젝트 개요

3-Tier 분산 AI 음성 비서: Edge → Server → Cloud.
음성 입력을 실시간 처리하고, 질문 언어(한국어/영어)에 맞춰 해당 언어 음성으로 응답.

## 지원 디바이스

Edge와 Server 각각 여러 디바이스를 지원하며, 실행 시 `--device` 인자로 선택한다.
디바이스별 설정(메모리 예산, GPIO, sysfs 경로 등)은 `shared/device_profiles.py`에 정의한다.
새 디바이스 추가 시 프로파일만 등록하면 나머지 코드는 변경 없이 동작해야 한다.

| 역할 | 디바이스 | RAM | 식별자 |
|------|----------|-----|--------|
| Edge | Jetson Nano 4GB | 4GB | `jetson_nano` |
| Edge | Raspberry Pi 4B 8GB | 8GB | `raspberry_pi_4b` |
| Server | Jetson Orin Nano 8GB | 8GB | `jetson_orin_nano` |
| Server | Jetson Orin NX 16GB | 16GB | `jetson_orin_nx` |

실행 예:
```bash
python main.py --edge   --device raspberry_pi_4b
python main.py --server --device jetson_orin_nano
```

## 아키텍처 원칙

- **Edge**: 사용자 I/O (마이크/스피커 ReSpeaker XVF3800 입출력, openWakeWord, Silero VAD, Supertonic TTS)
  - 사용자 음성을 캡처하여 Server로 전송하고, Server가 보내준 텍스트를 TTS로 재생한다.
  - VAD로 발화 구간을 감지하고, wake word로 대화를 시작한다.
  - TTS는 Edge에서 실행한다. Server로부터 텍스트 문장을 수신하면 즉시 TTS 변환 후 재생한다.
  - 재생 중에도 다음 문장을 수신하여 큐잉하므로, 응답 재생이 끊기지 않는 파이프라인 구조이다.

- **Server**: AI 추론 (Whisper-large-v3-turbo STT, 로컬 인텐트 추론 [Gemma4:e2B], Cloud API 중계, FastAPI 대시보드)
  - Edge에서 받은 오디오를 STT로 텍스트 변환한다.
  - STT 결과를 로컬 LLM(Gemma4:e2B)에 전달하여 사용자 의도(인텐트)를 분석한다.
  - 인텐트가 감지되면 Cloud 호출 없이 즉시 결과 텍스트를 Edge로 전송한다.
  - 인텐트 미감지 시 Cloud LLM에 전달하고, **이중 섹션 출력**으로 스트리밍 응답을 받는다:
    - `[SPOKEN]` 섹션: 구어체 요약 (3~5문장) → 즉시 Edge로 전송하여 TTS 재생
    - `[DISPLAY]` 섹션: 완전한 상세 응답 (마크다운 포함) → FastAPI 대시보드 GUI에 표시
  - FastAPI 대시보드(`port 8080`)로 실시간 대화 모니터링 및 마크다운 렌더링을 제공한다.

- **Cloud**: LLM 대화/응답 생성 (Claude, GPT, and more)
  - 복잡한 대화/질의에 대해 고품질 응답을 생성한다.
  - Server/Cloud 단절 시 Server 로컬 LLM으로 폴백한다.

## 데이터 흐름

```
[Edge]                    [Server]                              [Cloud]        [Browser]
마이크 캡처
  → audio stream ────────→ STT (Whisper)
      (gRPC)                → 텍스트
                            → 로컬 LLM 인텐트 분석
                              ├─ 인텐트 감지 → 결과 텍스트 ──────────────────────────→ Edge
                              └─ 인텐트 없음 → Cloud LLM 호출 ──→ 이중 섹션 스트리밍
                                                [SPOKEN] 구어체 요약 ──→ Edge (TTS)
                                                [DISPLAY] 상세 응답  ──────────────→ Dashboard
  ← text (gRPC) ──────────                                                  (WebSocket)
TTS 재생 (Supertonic)
```

핵심 포인트:
- Server → Edge 응답은 **텍스트**이다 (오디오가 아님). TTS는 Edge에서 수행한다.
- Cloud LLM은 **두 섹션**을 순서대로 생성한다:
  - `[SPOKEN]` 먼저: 구어체 3~5문장 요약. 즉시 TTS 파이프라인으로 전달. 마크다운 금지.
  - `[DISPLAY]` 이후: 완전한 상세 응답. 마크다운/표/코드 허용. 대시보드 GUI로 전달.
- TTS 지연은 현재와 동일 — SPOKEN이 먼저 생성되므로 첫 문장 지연 증가 없음.
- 로컬 인텐트 매칭 시 Cloud를 스킵하여 지연을 최소화한다.
- 대화 타임아웃: Edge가 타이머 관리 (60초 무발화 → 의사확인, 5초 추가 무발화 → 세션 종료).
- TTS 재생 중 타임아웃 타이머 중지. IDLE 전환 시 Server 대화 히스토리 초기화.
- 대시보드: `http://<server_ip>:8080` — 별도 HTTP 포트, gRPC 프로토콜 변경 없음.

## 통신 계약

- Edge → Server: gRPC, **오디오 데이터** (PCM 16kHz mono, 기존과 동일)
- Server → Edge: gRPC, **텍스트 데이터** (STT 결과 + 응답 문장)
- Server → Cloud: HTTPS (LangChain astream)
- 상세 메시지 정의는 `proto/voice_service.proto` 참조

## 로컬 인텐트 시스템

Server의 로컬 LLM(Gemma4:e2B)이 사용자 발화의 의도를 분석하여 라우팅한다.
시스템 프롬프트를 상세하게 작성하여, 특정 command 실행 의도가 있는지 판단한다.

- 인텐트 감지 시: Cloud LLM 호출 없이, 인텐트별 지정된 로직을 실행하고 결과 텍스트를 Edge에 반환.
- 인텐트 미감지 시: Cloud LLM에 발화 내용을 전달.

Gemma 인텐트 응답은 JSON 포맷: `{"intent": "<name>", ...추가 필드}`. 미감지 시 `{"intent": "none"}` → Cloud LLM 호출.

현재 구현된 인텐트:
| 인텐트 | 동작 | Edge 수신 텍스트 |
|--------|------|------------------|
| 대화 세션 종료 (`end_session`) | Server → Edge에 `end_session` 신호 전송. 대화 요약(Gemma) → SQLite 저장 후 히스토리 초기화. | ko: "대화를 종료합니다. 언제든 다시 불러 주세요" / en: "End of conversation. Call me anytime" |
| 일시정지 (`pause_listening`) | Server → Edge에 `pause_listening` 신호 전송. Edge는 PAUSED 상태 진입 (오디오 전송 OFF, 웨이크워드 감시 ON). Gemma가 `duration_s` 추출 시 그 값 사용, 없으면 기본 5분. 히스토리 유지. | ko: "네, {duration}분간 대기할게요. 부르시면 다시 들을게요." / en: "Okay, I'll pause for {duration} minutes. Call me when you're ready." |
| 지난 대화 목록 (`list_sessions`) | SQLite에서 최근 5개 세션 조회 → 번호 리스트를 Edge(음성)와 대시보드(WebSocket)에 전송. 번호 선택 시 히스토리 복원하여 이전 대화 이어감. | ko: "최근 대화 {count}개입니다. 번호를 말씀해 주세요." / en: "Here are your last {count} conversations. Please say a number." |

인텐트는 향후 계속 추가될 수 있다 (예: 볼륨 조절, 타이머 설정 등).

## 세션 저장 및 Resume

- 세션 종료 시(`end_session` 또는 타임아웃) Gemma가 대화 첫 2~3턴을 한 줄 요약(subject) 생성 → `SessionRecord`로 SQLite(`data/sessions.db`) 저장.
- 사용자가 "지난 대화 보여줘" → `list_sessions` 인텐트 → 최근 5개 목록을 Edge(음성) + 대시보드(WebSocket)에 전송 → 번호 선택 → 해당 세션 히스토리를 Cloud LLM 클라이언트에 복원하여 이전 맥락에서 대화 계속.
- 번호가 아닌 발화 시 선택 모드 즉시 해제 → 일반 대화 처리.

## 코드 규약

- Python 3.10+, type hints 필수, asyncio 기반
- snake_case (함수/변수), PascalCase (클래스)
- gRPC 호출은 try/except + 폴백 필수
- API 키는 환경변수/.env, 코드 하드코딩 금지
- 로깅: Python logging, 레벨은 config 관리
- 코드는 단순하고 간결하게. 복잡성 대비 성능 향상이 작은 최적화는 지양
- 가독성 우선. 3줄 중복이 불필요한 추상화보다 낫다
- Commit prefix: `edge:`, `server:`, `proto:`, `shared:`, `docs:`, `infra:`
- proto 변경 시 양쪽 CLAUDE.md도 함께 업데이트

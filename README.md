# Voice Assistant — 3-Tier 분산 AI 음성 비서

Edge → Server → Cloud 구조의 실시간 음성 비서. 음성 입력을 캡처해 서버로 스트리밍하고, 질문 언어(한국어/영어)에 맞는 음성으로 응답한다.

## 지원 디바이스

Edge와 Server 각각 여러 디바이스를 지원하며, 실행 시 `--device` 인자로 선택한다.
디바이스별 설정(메모리 예산, GPIO, sysfs 경로 등)은 `shared/device_profiles.py`에 정의되어 있다.

| 역할 | 디바이스 | RAM | 식별자 |
|------|----------|-----|--------|
| Edge | Jetson Nano 4GB | 4GB | `jetson_nano` |
| Edge | Raspberry Pi 4B 8GB | 8GB | `raspberry_pi_4b` |
| Server | Jetson Orin Nano 8GB | 8GB | `jetson_orin_nano` |
| Server | Jetson Orin NX 16GB | 16GB | `jetson_orin_nx` |

## 실행

```bash
python main.py --edge   --device raspberry_pi_4b
python main.py --server --device jetson_orin_nano
```

## 아키텍처

- **Edge** — 사용자 I/O (마이크/스피커 ReSpeaker XVF3800, openWakeWord, Silero VAD, Supertonic TTS)
  - 사용자 음성을 캡처해 Server로 전송하고, Server가 보내준 텍스트를 TTS로 재생.
  - VAD로 발화 구간 감지, wake word로 대화 시작.
  - Server로부터 텍스트 문장을 수신하면 즉시 TTS 변환 후 재생. 재생 중에도 다음 문장을 수신·큐잉하므로 응답이 끊기지 않는 파이프라인 구조.

- **Server** — AI 추론 (Whisper-large-v3-turbo STT, 임베딩 인텐트 분류, Cloud API 중계, FastAPI 대시보드)
  - Edge에서 받은 오디오를 STT로 텍스트 변환.
  - STT 결과를 임베딩 유사도 분류기(`sentence-transformers`)로 인텐트 판별 (~50ms, CPU).
  - 인텐트 감지 시 Cloud 호출 없이 즉시 결과 텍스트를 Edge로 전송.
  - 인텐트 미감지 시 Cloud LLM에 전달하고 스트리밍 응답을 받음.
  - FastAPI 대시보드(`port 8080`)로 실시간 대화 모니터링 및 마크다운 렌더링 제공 (`display_enabled: true`일 때 활성화).

- **Cloud** — LLM 대화/응답 생성 (Claude, GPT 등)
  - 복잡한 대화/질의에 대한 고품질 응답 생성.
  - 세션 종료 시 대화 요약(subject) 생성에도 사용 (non-streaming, 사용자 대기 없음).

### 출력 모드 — `display_enabled` 플래그

`config/server.yaml`의 `display_enabled` 플래그로 출력 모드를 제어한다.

- **`display_enabled: false` (기본)** — 단일 섹션 음성 전용 프롬프트. 토큰을 TTS에 직접 연결.
- **`display_enabled: true`** — 이중 섹션 출력. Cloud LLM이 두 섹션을 순서대로 생성한다.
  - `[SPOKEN]` 먼저: 구어체 3~5문장 요약. 즉시 TTS 파이프라인으로 전달. 마크다운 금지.
  - `[DISPLAY]` 이후: 완전한 상세 응답. 마크다운/표/코드 허용. 대시보드 GUI로 전달.

SPOKEN은 두 모드 모두에서 항상 TTS로 전달된다. DISPLAY 섹션만 on/off 된다.
첫 문장 지연 증가는 ~50~100ms 이내.

## 데이터 흐름

```
[Edge]                    [Server]                              [Cloud]        [Browser]
마이크 캡처
  → audio stream ────────→ STT (Whisper)
      (gRPC)                → 텍스트
                            → 임베딩 인텐트 분류 (~50ms)
                              ├─ 인텐트 감지 → 결과 텍스트 ──────────────────────────→ Edge
                              └─ 인텐트 없음 → Cloud LLM 호출 ──→ 이중 섹션 스트리밍
                                                [SPOKEN] 구어체 요약 ──→ Edge (TTS)
                                                [DISPLAY] 상세 응답  ──────────────→ Dashboard
  ← text (gRPC) ──────────                                                  (WebSocket)
TTS 재생 (Supertonic)
```

핵심 포인트:
- Server → Edge 응답은 **텍스트**다 (오디오가 아님). TTS는 Edge에서 수행.
- 임베딩 인텐트 분류는 CPU에서 ~50ms. GPU(Whisper 전용)와 경합 없음.
- 대화 타임아웃: Edge가 타이머 관리 (60초 무발화 → 의사확인, 5초 추가 무발화 → 세션 종료).
- TTS 재생 중 타임아웃 타이머 중지. IDLE 전환 시 Server 대화 히스토리 초기화.
- 대시보드: `http://<server_ip>:8080` — 별도 HTTP 포트, gRPC 프로토콜 변경 없음.

## 통신 계약

- Edge → Server: gRPC, **오디오 데이터** (PCM 16kHz mono)
- Server → Edge: gRPC, **텍스트 데이터** (STT 결과 + 응답 문장)
- Server → Cloud: HTTPS (LangChain astream)
- 상세 메시지 정의는 `proto/voice_service.proto` 참조.

## 인텐트 시스템

`sentence-transformers`(`paraphrase-multilingual-MiniLM-L12-v2`) 기반 임베딩 유사도 분류기.
각 인텐트별 예시 문장을 사전 임베딩하여 저장하고, 사용자 발화와의 코사인 유사도를 비교한다.

- 유사도 ≥ 임계값(0.75): 인텐트 확정 → 핸들러 실행, Cloud LLM 스킵
- 유사도 < 임계값: 인텐트 없음 → Cloud LLM 호출
- CPU 실행 (~50ms). GPU(Whisper 전용)와 경합 없음.

현재 구현된 인텐트:

| 인텐트 | 동작 | Edge 수신 텍스트 |
|--------|------|------------------|
| 대화 세션 종료 (`end_session`) | `end_session` 신호 전송. Cloud LLM으로 대화 요약(subject) 생성 → SQLite 저장 후 히스토리 초기화. | ko: "대화를 종료합니다. 언제든 다시 불러 주세요" / en: "End of conversation. Call me anytime" |
| 일시정지 (`pause_listening`) | `pause_listening` 신호 전송. Edge는 PAUSED 상태 진입 (오디오 전송 OFF, 웨이크워드 감시 ON). 정규식으로 `duration_s` 추출, 없으면 기본 5분. 히스토리 유지. | ko: "네, {duration}분간 대기할게요. 부르시면 다시 들을게요." / en: "Okay, I'll pause for {duration} minutes. Call me when you're ready." |
| 지난 대화 목록 (`list_sessions`) | SQLite에서 최근 5개 세션 조회 → 번호 리스트를 Edge(음성)와 대시보드(WebSocket)에 전송. 번호 선택 시 히스토리 복원하여 이전 대화 이어감. | ko: "최근 대화 {count}개입니다. 번호를 말씀해 주세요." / en: "Here are your last {count} conversations. Please say a number." |

인텐트는 향후 계속 추가될 수 있다 (예: 볼륨 조절, 타이머 설정 등).

## 세션 저장 및 Resume

- 세션 종료 시(`end_session` 또는 타임아웃) Cloud LLM이 대화 첫 2~3턴을 한 줄 요약(subject, 15자 이내) 생성 → `SessionRecord`로 SQLite(`data/sessions.db`)에 저장. Edge가 응답을 수신한 후 비동기 실행되므로 사용자 대기 없음. 실패 시 첫 HumanMessage 텍스트로 폴백.
- 사용자가 "지난 대화 보여줘" → `list_sessions` 인텐트 → 최근 5개 목록을 Edge(음성)+대시보드(WebSocket)로 전송 → 번호 선택 → 해당 세션 히스토리를 Cloud LLM 클라이언트에 복원하여 이전 맥락에서 대화 계속.
- 번호가 아닌 발화 시 선택 모드 즉시 해제 → 일반 대화 처리.

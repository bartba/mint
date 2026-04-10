# CLAUDE.md — Edge

## 역할

사용자 접점 디바이스. 마이크/스피커, 경량 추론(VAD/Wake word), TTS 재생, 상태 머신 관리.
무거운 AI 추론(STT, LLM)은 Server에 위임. Server/Cloud 불가 시 오프라인 폴백.

Server로부터 **텍스트**를 수신하여 Edge에서 TTS(Supertonic)로 음성 변환 후 재생한다.
재생 중에도 다음 문장을 수신하여 큐잉하므로, 응답 재생이 끊기지 않는 파이프라인 구조이다.

## 지원 디바이스

| 디바이스 | RAM | 식별자 | 비고 |
|----------|-----|--------|------|
| Jetson Nano 4GB | 4GB | `jetson_nano` | GPU 있음, CUDA 지원 |
| Raspberry Pi 4B 8GB | 8GB | `raspberry_pi_4b` | GPU 없음, CPU 전용, 메모리 여유 |

실행: `python main.py --edge --device raspberry_pi_4b`

디바이스별 차이는 `shared/device_profiles.py`에 정의한다 (오디오 디바이스, GPIO, 메모리 예산 등).

## 핵심 구조

- `orchestrator.py` — 전체 상태 머신, 폴백 판단, 타이머 관리
- `grpc_client.py` — Server 통신 (ProcessVoice, EndSession, HealthCheck)
- `audio/capture.py` — XVF3800 마이크 캡처 (PyAudio, 16kHz mono)
- `audio/playback.py` — 스피커 출력 (XVF3800 AUX, 스트리밍 재생)
- `audio/vad.py` — Silero VAD (ONNX, ~5ms)
- `audio/wakeword.py` — openWakeWord (ONNX, ~10ms)
- `tts/supertonic_service.py` — Supertonic TTS (ONNX, 한국어+영어 단일 모델, CPU)
- `tts/tts_pipeline.py` — 텍스트 수신 → TTS 변환 → 재생 파이프라인 (큐 기반)
- `cache/*.wav` — 오프라인 응답 캐시 (chime, 타임아웃 안내 등)

## 데이터 흐름

1. 마이크 캡처 → VAD 발화 감지 → 오디오 스트림을 Server에 gRPC 전송
2. Server로부터 STT 결과(텍스트) 수신 → 화면/로그 표시
3. Server로부터 응답 텍스트(문장 단위) 수신:
   - 수신 즉시 TTS 큐에 추가
   - TTS 파이프라인이 큐에서 문장을 꺼내 Supertonic으로 변환 → 스피커 재생
   - 재생 중에도 다음 문장이 도착하면 큐잉하여 연속 재생
4. `end_session` 신호(TextResponse.type) 수신 시: 미리 준비된 종료 안내 텍스트를 TTS 재생 후 IDLE 전환
5. `pause_listening` 신호(TextResponse.type) 수신 시: ack 텍스트 TTS 재생 → PAUSED 상태 진입 (오디오 전송 중단, 웨이크워드 감시만 유지, pause 타이머 시작)

## 상태 머신

`IDLE` → wake word → `LISTENING` → VAD end → `PROCESSING` → response → `SPEAKING` → done → `LISTENING`

- **IDLE**: 웨이크워드만 상시 실행. chime_wake 재생 후 LISTENING 전환.
- **LISTENING**: VAD 활성, 무발화 타이머(60초) 동작. 발화 종료 시 chime_ack 재생.
- **SPEAKING**: TTS 파이프라인 재생 중. 타이머 중지. 재생 완료 시 LISTENING 복귀.
- **PAUSED**: 일시정지 상태. 오디오 gRPC 전송 OFF, VAD OFF, 무발화 타이머 OFF, 웨이크워드 감시 ON. Server 대화 히스토리 유지. pause 타이머(duration_s, 기본 5분) 동작.
  - 웨이크워드 감지 → chime_wake 재생 → LISTENING 복귀 (세션/히스토리 유지, pause 타이머 취소).
  - pause 타이머 만료 → `timeout_messages.end` TTS 재생 → `EndSession` RPC → IDLE.
- **TIMEOUT**: 60초 무발화 후 의사확인 재생(Edge 로컬 텍스트 + TTS). 5초 내 발화 → LISTENING, 무응답 → 세션 종료 안내 재생(Edge 로컬) → `EndSession` RPC 호출 → IDLE.
- **DEGRADED**: Server 연결 불가. 로컬 기능만 동작, 주기적 재연결.

## 설계 원칙

- **TTS 파이프라인**: Server에서 텍스트 문장을 수신하면 즉시 TTS 변환 큐에 넣고, 변환 완료된 오디오를 순차 재생한다. 재생과 변환이 병렬로 동작하여 지연을 최소화한다.
- **대화 타임아웃**: Edge orchestrator가 타이머 관리. TTS 재생 중 타이머 중지. 언어는 마지막 STT 결과의 language 사용 (기본 한국어). 타임아웃 안내 메시지(의사확인/종료 안내)는 `config/edge.yaml`의 `timeout_messages`에서 읽어 Edge에서 직접 TTS 재생한다 (Server 호출 없음 → 지연 최소화 및 Server 단절 시에도 동작). 세션 종료 시점에만 `EndSession` RPC로 Server 히스토리 저장 및 초기화를 요청한다.
- **오프라인 폴백**: Server 연결 실패 시 캐시된 WAV 재생

## 인텐트별 TextResponse 처리

### `end_session` — 대화 세션 종료
Server에서 `end_session` 신호(TextResponse.type)를 수신하면:
- ko: "대화를 종료합니다. 언제든 다시 불러 주세요" TTS 재생
- en: "End of conversation. Call me anytime" TTS 재생
- 재생 후 IDLE 전환,(언어는 마지막 STT 결과의 language 사용) Server 대화 히스토리 초기화 요청

### `pause_listening` — 일시정지
Server에서 `pause_listening` 신호(TextResponse.type)를 수신하면:
- ack 텍스트 TTS 재생 (예: "네, 5분간 대기할게요. 부르시면 다시 들을게요.")
- 재생 완료 후 PAUSED 상태 진입:
  - 오디오 gRPC 전송 중단, VAD OFF, 무발화 타이머 OFF
  - 웨이크워드 감시만 유지 (IDLE과 동일한 감시 루프 재사용)
  - pause 타이머 시작 (Server가 전달한 duration_s, 없으면 `config/edge.yaml`의 `pause_default_s` 사용)
- **복귀**: 웨이크워드("hey mack") 감지 시 → chime_wake 재생 + LISTENING 진입. 세션/히스토리 유지. pause 타이머 취소. `EndSession` RPC 호출하지 않음.
- **만료**: pause 타이머 도달 시 → `timeout_messages.end` TTS → `EndSession` RPC → IDLE (기존 종료 경로 재사용).

## 주요 의존성

pyaudio, numpy, onnxruntime, openwakeword, supertonic, grpcio, protobuf, pyyaml, prometheus-client

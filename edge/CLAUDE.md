# CLAUDE.md — Edge (Jetson Nano 4GB)

## 역할

사용자 접점 디바이스. 마이크/스피커, 경량 추론(VAD/Wake word), 상태 머신 관리. 무거운 AI 추론은 Server에 위임. Server/Cloud 불가 시 오프라인 폴백.

## 핵심 구조

- `orchestrator.py` — 전체 상태 머신, 폴백 판단, 타이머 관리
- `grpc_client.py` — Server 통신 (ProcessVoice, TimeoutPrompt, EndSession, HealthCheck)
- `audio/capture.py` — XVF3800 마이크 캡처 (PyAudio, 16kHz mono)
- `audio/playback.py` — 스피커 출력 (XVF3800 AUX, 스트리밍 재생)
- `audio/vad.py` — Silero VAD (ONNX, ~5ms)
- `audio/wakeword.py` — openWakeWord (ONNX, ~10ms)
- `cache/*.wav` — 오프라인 응답 캐시 (chime, 타임아웃 안내 등)

## 상태 머신

`IDLE` → wake word → `LISTENING` → VAD end → `PROCESSING` → response → `SPEAKING` → done → `LISTENING`

- **IDLE**: 웨이크워드만 상시 실행. chime_wake 재생 후 LISTENING 전환.
- **LISTENING**: VAD 활성, 무발화 타이머(15초) 동작. 발화 종료 시 chime_ack 재생.
- **SPEAKING**: TTS 스트리밍 재생 중. 타이머 중지. VAD barge-in 감시.
- **TIMEOUT**: 15초 무발화 후 의사확인 재생. 5초 내 발화 → LISTENING, 무응답 → 세션 종료 → IDLE.
- **DEGRADED**: Server 연결 불가. 로컬 기능만 동작, 주기적 재연결.

## 설계 원칙

- **barge-in**: SPEAKING 중 VAD가 임계값(0.65, 에코 오탐 방지) 이상 음성을 500ms 연속 감지하면 TTS 즉시 중지 → LISTENING 전환. 내부 카운터로 처리, 별도 상태 없음.
- **대화 타임아웃**: Edge orchestrator가 타이머 관리. TTS 재생 중 타이머 중지. 언어는 마지막 STT 결과의 language 사용 (기본 한국어).
- **메모리 예산**: OS 1.0GB + Silero 10MB + openWakeWord 50MB + 버퍼 0.5GB = ~1.6GB (여유 2.4GB)
- **오프라인 폴백**: Server 연결 실패 시 캐시된 WAV 재생

## 주요 의존성

pyaudio, numpy, onnxruntime, openwakeword, grpcio, protobuf, pyyaml, prometheus-client

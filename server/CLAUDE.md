# CLAUDE.md — Server

## 역할

AI 추론 서버. Edge로부터 오디오 수신 → STT → 로컬 LLM 인텐트 분석 → (Cloud LLM) → 텍스트 응답을 Edge에 반환.
TTS는 Server에서 수행하지 않는다 — Edge가 수신한 텍스트를 직접 TTS 변환한다.

## 지원 디바이스

| 디바이스 | RAM | 식별자 | 비고 |
|----------|-----|--------|------|
| Jetson Orin Nano 8GB | 8GB | `jetson_orin_nano` | 통합 메모리, GPU Ampere |
| Jetson Orin NX 16GB | 16GB | `jetson_orin_nx` | 통합 메모리, GPU Ampere, 여유 메모리 더 큼 |

실행: `python main.py --server --device jetson_orin_nano`

디바이스별 차이는 `shared/device_profiles.py`에 정의한다 (메모리 예산, sysfs 경로, 전력 모드 등).

## 핵심 구조

- `orchestrator.py` — 요청 조율, 인텐트 라우팅
- `grpc_server.py` — Edge 요청 수신 (ProcessVoice, EndSession, HealthCheck)
- `stt/whisper_service.py` — Whisper-large-v3-turbo (faster-whisper, GPU, float16)
- `intent/intent_analyzer.py` — 로컬 LLM(Gemma4:e2B, Ollama) 기반 인텐트 분석
- `intent/intent_handlers.py` — 인텐트별 처리 로직 (현재: 대화 세션 종료)
- `cloud/llm_client.py` — LangChain 기반 Claude/GPT 스트리밍 (astream)
- `cloud/prompt_templates.py` — 한국어/영어 시스템 프롬프트
- `cloud/sentence_splitter.py` — LLM 스트리밍 토큰을 문장 단위로 분리
- `dashboard/app.py` — FastAPI 간단 대시보드 (입출력 모니터링)
- `monitoring/` — sysfs GPU/메모리/온도 읽기, Prometheus 메트릭

## 데이터 흐름 상세

1. Edge → Server: gRPC AudioChunk 스트림 수신
2. STT(Whisper): 오디오 → 텍스트 변환
3. STT 결과(텍스트)를 Edge에 즉시 전송 (화면 표시용)
4. 로컬 LLM 인텐트 분석 및 라우팅 (Gemma4:e2B):
   - 인텐트 감지 → 인텐트 핸들러 실행 → 결과 텍스트를 Edge에 전송 (Cloud 스킵)
   - 인텐트 미감지 → Cloud LLM 호출
5. Cloud LLM 스트리밍 응답:
   - 토큰 단위 수신 → sentence_splitter로 문장 부호/토큰 수 기준 분리
   - 문장 완성 시 즉시 Edge에 텍스트 전송
6. Edge는 수신한 텍스트를 TTS로 재생

## 로컬 인텐트 시스템

로컬 LLM(Gemma4:e2B)에 시스템 프롬프트를 전달하여 사용자 발화의 의도를 분석한다.
기존 정규식 기반 인텐트 매칭을 로컬 LLM 기반으로 교체한다.

구현된 인텐트:
- **대화 세션 종료**: 사용자가 대화 종료 의도를 보이면, Edge에 `end_session` 신호(TextResponse.type) + 종료 안내 텍스트 전송.
  - ko: "대화를 종료합니다. 언제든 다시 불러 주세요"
  - en: "End of conversation. Call me anytime"

새 인텐트 추가 시 `intent/intent_handlers.py`에 핸들러 함수만 추가하면 된다.

## 설계 원칙

- **리소스 분리**: STT는 GPU(faster-whisper, large-v3-turbo, float16, ctranslate2), 인텐트 분석은 local LLM(별도 프로세스)
  > Local LLM for Jetson Orin Nano 8GB : Gemma 4:e2B (E2B Q4_K_M)
  > Local LLM for Jetson Orin NX 16GB : Gemma 4:e4B (E4B Q4_K_M)
- **텍스트 응답**: Server → Edge 응답은 텍스트. TTS 부하를 Edge로 분산.
- **문장 단위 스트리밍**: Cloud LLM 스트리밍을 문장 부호 + 토큰 수 기준으로 분리하여 Edge에 전송. 첫 문장 도착 시 Edge가 즉시 TTS 재생 시작.
- **인텐트 우선**: 로컬 LLM 인텐트 감지 시 Cloud 스킵, 지연 최소화
- **대화 종료 인텐트 시** Edge에 종료 텍스트 전송 후 대화 히스토리 저장 및 초기화
- **프롬프트 설계**: 구어체 발화. 30자/15words 이내 3~5문장으로 응답, 마크다운/이모지/문장 부호 등 TTS 불가능한 출력 금지

## Barge-in 처리 (Edge 사용자 끼어들기 대응)

Edge에서 TTS 재생 중 사용자 발화가 감지되면(VAD 임계값 이상 500ms 연속), Edge는 TTS를 중지하고 `ProcessVoice` gRPC 스트림을 취소한다. Server는 이 취소를 barge-in 신호로 해석하여 즉시 응답 생성을 중단한다.

**처리 흐름**:
1. Edge가 `ProcessVoice` RPC 취소 → gRPC 컨텍스트에 취소 이벤트 전파
2. `grpc_server.ProcessVoice()`의 `async for` 루프가 `CancelledError`로 종료
3. `CloudLLMClient.get_response_stream()`의 `try/finally`에서 정리:
   - 지금까지 누적된 부분 응답(`full_response`)을 `AIMessage`로 히스토리에 저장
   - 별도 cancel 호출 없이 Python async generator GC가 Cloud astream을 자동 정리
4. 다음 `ProcessVoice` 호출 대기 (새 사용자 발화 처리)

**설계 의도**:
- 새 RPC나 proto 메시지 추가 없이 기존 gRPC 취소 메커니즘만 활용 → 단순성
- 부분 응답을 히스토리에 저장하여 다음 턴에 맥락 유지 ("아까 말하던 중 끊겼구나"를 LLM이 인지)
- STT/인텐트 분석은 barge-in 이전에 완료되므로 정리할 자원 없음
- Edge↔Server 간 추가 핸드셰이크 불필요

## 주요 의존성

faster-whisper, ctranslate2, grpcio, langchain, langchain-anthropic, langchain-openai, langchain-ollama, prometheus-client, fastapi, uvicorn

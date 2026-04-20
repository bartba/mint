# CLAUDE.md — Server

## 역할

AI 추론 서버. Edge로부터 오디오 수신 → STT → 임베딩 인텐트 분류 → (Cloud LLM) → 텍스트 응답을 Edge에 반환.
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
- `grpc_server.py` — Edge 요청 수신 (ProcessVoice, EndSession, HealthCheck) + 대시보드 브로드캐스트
- `stt/whisper_service.py` — Whisper-large-v3-turbo (faster-whisper, GPU, float16)
- `intent/intent_classifier.py` — 임베딩 유사도 기반 인텐트 분류 (sentence-transformers, CPU)
- `intent/intent_handlers.py` — 인텐트별 처리 로직 (end_session, pause_listening, list_sessions)
- `storage/models.py` — SessionRecord 데이터 모델
- `storage/session_repository.py` — SQLite 기반 세션 CRUD (save, get, get_recent)
- `cloud/llm_client.py` — LangChain 기반 Claude/GPT 스트리밍 (astream), 이중 섹션 출력
- `cloud/prompt_templates.py` — 한국어/영어 시스템 프롬프트 (SPOKEN/DISPLAY 이중 섹션)
- `cloud/sentence_splitter.py` — LLM 스트리밍 토큰을 문장 단위로 분리
- `cloud/dual_stream_parser.py` — 스트리밍 토큰을 tts/display 채널로 분리하는 파서
- `dashboard/app.py` — FastAPI 대시보드: WebSocket 실시간 스트리밍, 마크다운 렌더링
- `dashboard/static/index.html` — 대시보드 웹 UI (타이핑 효과, 대화 히스토리)
- `monitoring/` — sysfs GPU/메모리/온도 읽기, Prometheus 메트릭

## 데이터 흐름 상세

1. Edge → Server: gRPC AudioChunk 스트림 수신
2. STT(Whisper): 오디오 → 텍스트 변환
3. STT 결과(텍스트)를 Edge에 즉시 전송 (화면 표시용)
4. 임베딩 유사도 분류기 (`IntentClassifier`, CPU, ~50ms) 인텐트 판별:
   - 인텐트 감지 → 인텐트 핸들러 실행 → 결과 텍스트를 Edge에 전송 (Cloud 스킵)
   - 인텐트 미감지 → Cloud LLM 호출
5. Cloud LLM 스트리밍 응답 — `config/server.yaml`의 `display_enabled` 플래그로 동작 분기:
   - **`display_enabled: false`** (기본): 단일 섹션 음성 전용 프롬프트. LLM 토큰을 TTS에 직접 연결. 히스토리에 전체 응답 저장.
   - **`display_enabled: true`**: 이중 섹션 출력.
     - LLM이 `[SPOKEN]...[/SPOKEN]` 먼저 생성 → `dual_stream_parser`가 tts 채널로 분리 → Edge에 텍스트 전송 (TTS 재생)
     - 이후 `[DISPLAY]...[/DISPLAY]` 생성 → `dual_stream_parser`가 display 채널로 분리 → FastAPI WebSocket 브로드캐스트 → 대시보드 마크다운 렌더링
     - 히스토리에는 DISPLAY 섹션 내용만 저장 (맥락 품질 유지)
   - **SPOKEN은 두 모드 모두에서 항상 TTS로 전달된다.**
6. Edge는 수신한 텍스트를 TTS로 재생
7. 브라우저 대시보드: `http://<server_ip>:8080` — 실시간 대화 모니터링

## 인텐트 시스템

`sentence-transformers`(`paraphrase-multilingual-MiniLM-L12-v2`) 기반 임베딩 유사도 분류기.
각 인텐트별 예시 문장을 서버 시작 시 사전 임베딩하여 저장, 사용자 발화와 코사인 유사도를 비교.
CPU 실행 (~50ms). GPU는 Whisper 전용으로 경합 없음.

- 유사도 ≥ threshold(0.75): 인텐트 확정 → 핸들러 실행, Cloud LLM 스킵
- 유사도 < threshold: 인텐트 없음 → Cloud LLM 호출
- `pause_listening` 감지 시: `duration_pattern` 정규식으로 발화에서 `duration_s` 추출

구현된 인텐트:
- **대화 세션 종료** (`end_session`): Edge에 `end_session` 신호(TextResponse.type) + 종료 안내 텍스트 전송. 응답 전송 완료 후 Cloud LLM의 non-streaming 호출로 대화 첫 2~3턴을 한 줄 요약(subject) 생성 → SQLite 저장 → 히스토리 초기화. (요약 생성은 Edge 응답 이후 비동기 실행 → 사용자 대기 없음)
  - ko: "대화를 종료합니다. 언제든 다시 불러 주세요"
  - en: "End of conversation. Call me anytime"
- **일시정지** (`pause_listening`): Edge에 `pause_listening` 신호 + ack 텍스트 전송. 정규식으로 발화에서 `duration_s` 추출, 없으면 기본 5분. 대화 히스토리 유지.
  - ko: "네, {duration}분간 대기할게요. 부르시면 다시 들을게요."
  - en: "Okay, I'll pause for {duration} minutes. Call me when you're ready."
- **지난 대화 목록** (`list_sessions`): SQLite에서 최근 5개 세션 조회 → 번호 목록을 Edge(음성)와 대시보드(WebSocket)에 전송. orchestrator에 `waiting_for_selection=True` 플래그 설정.
  - 다음 발화에서 번호 파싱 → 해당 세션 히스토리 복원 → 이전 대화 맥락으로 계속.
  - 번호가 아닌 발화 → 플래그 해제 후 일반 처리.

새 인텐트 추가: `config/server.yaml`의 `intents` 섹션에 예시 문장 추가 + `intent/intent_handlers.py`에 핸들러 함수 추가.

## 세션 저장 및 Resume

- **저장**: 세션 종료 시(`end_session` 인텐트 또는 타임아웃) Cloud LLM의 non-streaming 호출로 첫 2~3턴을 한 줄 요약(subject, 15자 이내) → `SessionRecord(id, subject, language, turn_count, history_json, created_at, ended_at)` SQLite 저장. Edge 응답 이후 비동기 실행이므로 사용자 대기 없음. 실패 시 첫 HumanMessage 텍스트를 subject로 폴백.
- **Resume**: `list_sessions` 인텐트 → 목록 표시 → 번호 선택 → `cloud_client.restore_history(history_json)` 호출 → 이전 대화 맥락에서 계속. 히스토리 복원 후 토큰 초과 시 기존 `summarize_threshold_tokens` 메커니즘이 자동 처리.
- **DB**: `data/sessions.db` (SQLite, `config/server.yaml`의 `storage.db_path`)
- **안전장치**: `waiting_for_selection` 상태에서 Edge 60초 무발화 타임아웃이 작동하므로 별도 처리 불필요. 세션 0개 시 "저장된 대화가 없습니다" 응답.

## 설계 원칙

- **리소스 분리**: STT는 GPU(faster-whisper, large-v3-turbo, float16, ctranslate2), 인텐트 분류는 CPU(sentence-transformers, ~50ms). GPU 경합 없음.
- **텍스트 응답**: Server → Edge 응답은 텍스트. TTS 부하를 Edge로 분산.
- **display_enabled 토글**: `config/server.yaml`의 `cloud.display_enabled`(bool)로 DISPLAY 섹션을 on/off한다.
  - `false` (기본): 단일 섹션 음성 전용 프롬프트. 기존 TTS 파이프라인과 동일.
  - `true`: 이중 섹션 출력. `[SPOKEN]` → TTS, `[DISPLAY]` → 대시보드. SPOKEN은 항상 출력.
  - `display_enabled: true`여도 SPOKEN이 먼저 생성되므로 첫 TTS 지연 증가 ~50~100ms 이내.
- **문장 단위 스트리밍**: SPOKEN 섹션을 문장 부호 + 토큰 수 기준으로 분리하여 Edge에 전송. 첫 문장 도착 시 Edge가 즉시 TTS 재생 시작.
- **인텐트 우선**: 임베딩 분류기 인텐트 감지 시 Cloud 스킵, 지연 최소화.
- **세션 요약**: Cloud LLM의 non-streaming 호출로 subject 생성. Edge 응답 완료 이후 비동기 실행 → 사용자 체감 지연 없음.
- **프롬프트 설계**:
  - SPOKEN: 구어체 발화. 30자/15words 이내 3~5문장. 마크다운/이모지 금지.
  - DISPLAY: 제한 없음. 마크다운, 표, 코드 블럭 허용. 길이 제한 없음.
- **대시보드**: FastAPI + WebSocket. gRPC 포트(50051)와 별도 포트(8080)로 운영. proto 변경 없음.

## 주요 의존성

faster-whisper, ctranslate2, sentence-transformers, grpcio, langchain, langchain-anthropic, langchain-openai, prometheus-client, fastapi, uvicorn

# plan.md — 단계별 코드 개발 및 테스트 계획

> 본 문서는 [docs/prd.md](./docs/prd.md), [README.md](./README.md), 그리고 현재 코드베이스 스냅샷을 통합하여 작성한 **실행 계획**이다.
> 각 단계는 (1) 목표 → (2) 변경/생성 파일 → (3) 구현 핵심 → (4) 검증 방법 → (5) 사용자 리뷰 체크포인트 순서로 정리했다.
> 한 번에 한 파일씩 생성·리뷰·검증을 진행한다. 단계가 끝날 때마다 사용자 확인 후 다음 단계로 이동한다.

---

## 0. 현재 코드베이스 스냅샷

### 0.1 이미 구현된 것 (재사용)

| 영역 | 파일 | 상태 |
|------|------|------|
| Shared | [shared/device_profiles.py](./shared/device_profiles.py) | ✅ jetson_nano / raspberry_pi_4b / jetson_orin_nano / jetson_orin_nx 4종 등록 |
| Shared | [shared/config.py](./shared/config.py) | ✅ `get_config(role, device)`, deep_merge, 환경변수 주입 |
| Shared | [shared/utils.py](./shared/utils.py) | ✅ `pcm_to_float32`, `float32_to_pcm`, `chunk_bytes`, `setup_logging` |
| Config | [config/server.yaml](./config/server.yaml) | ✅ `intent_classifier`, `intents`(end/pause/list), `cloud`, `dashboard`, `storage` 섹션 완비 |
| Config | [config/edge.yaml](./config/edge.yaml) | ✅ `tts`, `audio`, `wakeword`, `vad`, `timeout`, `timeout_messages` |
| Proto | [proto/voice_service.proto](./proto/voice_service.proto) | ✅ TimeoutPrompt 제거, `TextResponse{text,language,type,is_final}` 정의 |
| Proto stubs | proto/voice_service_pb2*.py | ✅ 재생성 완료 |
| Server | [server/intent/intent_classifier.py](./server/intent/intent_classifier.py) | ✅ sentence-transformers 임베딩 유사도 분류기 |
| Server | [server/stt/whisper_service.py](./server/stt/whisper_service.py) | ✅ faster-whisper STTService |
| Server | [server/cloud/llm_client.py](./server/cloud/llm_client.py) | ⚠️ 단일 섹션 동작만. display_enabled / restore_history / save_and_clear_history 미구현 |
| Server | [server/cloud/prompt_templates.py](./server/cloud/prompt_templates.py) | ⚠️ 단일 섹션 프롬프트만. SPOKEN/DISPLAY 이중 섹션 미구현 |
| Server | [server/cloud/test_multiturn.py](./server/cloud/test_multiturn.py) | ✅ 멀티턴 동작 검증 스크립트 |
| Server | [server/orchestrator.py](./server/orchestrator.py) | ⚠️ 구조 정리 필요 (regex local intent, select_model_tier, server-side TTS 의존) |
| Server | [server/grpc_server.py](./server/grpc_server.py) | ⚠️ TimeoutPrompt 핸들러, `tts_audio` 응답 등 옛 경로 남아있음 |
| Server | [server/tts/supertonic_service.py](./server/tts/supertonic_service.py) | ⚠️ Edge로 이관 예정 (Phase 6에서 삭제). `_split_sentences` 로직은 Phase 2-3에서 재사용 |
| Server | [server/main.py](./server/main.py) | ⚠️ `--device` 인자, 대시보드 task 병행 실행 미구현 |
| Server | [shared/models.py](./shared/models.py) | ⚠️ TTSRequestData 제거 + TextResponseData 추가 필요 |

### 0.2 아직 없는 것 (신규 생성)

- `server/intent/intent_handlers.py`
- `server/cloud/sentence_splitter.py`
- `server/cloud/dual_stream_parser.py`
- `server/storage/__init__.py`, `server/storage/models.py`, `server/storage/session_repository.py`
- `server/dashboard/__init__.py`, `server/dashboard/app.py`, `server/dashboard/static/{index.html,app.js,style.css}`
- `edge/` 전체 (`__init__.py`, `main.py`, `orchestrator.py`, `grpc_client.py`, `audio/{capture,playback,vad,wakeword}.py`, `tts/{supertonic_service,tts_pipeline}.py`)
- `tests/` 디렉터리 (`test_dual_stream_parser.py`, `test_intent_classifier.py`, `test_sentence_splitter.py`, `test_session_repository.py`, `test_prompt_templates.py`, `test_dashboard_broadcast.py`)

### 0.3 핵심 갭 요약

1. **proto 계약은 새로 정렬되었지만, Server 코드(grpc_server, orchestrator)가 옛 계약(`tts_audio`, `TimeoutPrompt`)을 참조한다.** → Phase 2 우선 정리.
2. **`display_enabled` 이중 섹션 분기가 LLM/프롬프트/grpc_server 어디에도 구현돼 있지 않다.** → Phase 2의 핵심.
3. **세션 저장/Resume 기반 코드(SQLite Repository, SessionRecord)가 없다.** → Phase 5.
4. **대시보드는 config만 있고 코드는 없다.** → Phase 3.
5. **Edge는 CLAUDE.md만 있고 실제 코드가 0줄이다.** → Phase 4 (가장 큰 작업량).

---

## 1. 작업 원칙

- **한 파일 = 한 PR/리뷰 단위.** 사용자가 코드를 검토하며 진행하므로, 한 단계에서는 1~2개 파일만 만들거나 수정한다.
- **선(先) 단위 테스트, 후(後) 통합.** 단위 테스트가 가능한 컴포넌트(파서, 분류기, 리포지토리)는 Phase 2~5 내부에 테스트 작성을 함께 둔다.
- **계약(Contract) 우선.** proto는 이미 안정. Python 측 데이터클래스(`TextResponseData`, `SessionRecord`)를 먼저 정의한 뒤 컴포넌트를 구현한다.
- **PRD 핵심 준수**: 발화 종료→첫 TTS ≤ 2.5s / display_enabled=true 시 첫 TTS 추가 지연 ≤ 100ms / 단일 LLM 프로바이더 / Barge-in 영구 제외.
- **삭제는 마지막에**: `server/tts/`, `server/orchestrator.py` 옛 메서드(`select_model_tier`, `check_local_intent`, `_drop_page_cache` 등 외)는 Phase 6까지 보류 — 중간 단계의 import 깨짐을 막는다.
- **로깅**: 모든 신규 모듈에 `logger = logging.getLogger(__name__)`. 중요 분기·에러는 `info`/`error`. 토큰 단위 추적은 `debug`.

---

## Phase 2 — Server 코드 재구성 (총 11 단계)

목표: `display_enabled` 토글, 임베딩 인텐트 분류 통합, gRPC 응답을 텍스트 기반으로 전환.

### 2-1. `shared/models.py` 갱신
**목표**: gRPC 텍스트 응답을 위한 데이터클래스 정의. 옛 `TTSRequestData` 제거.
- `TextResponseData(text, language, type, is_final)` 추가, `to_proto()` / `from_proto()` 포함.
- `TTSRequestData` 클래스 삭제 (Server-side TTS 의존성 제거 준비).
- `STTResultData`, `AudioChunkData`, `ServerStatusData`는 유지.
**검증**:
- `python -c "from shared.models import TextResponseData; r=TextResponseData(text='hi',language='ko',type='sentence',is_final=False); print(r.to_proto())"`
**리뷰 포인트**: proto와 필드명/타입 일치 여부.

### 2-2. `server/cloud/sentence_splitter.py` (신규)
**목표**: SPOKEN 채널용 문장/절 분리기. `server/tts/supertonic_service.py`의 `_split_sentences()` 로직을 그대로 이관(언젠가 Edge에서도 활용 가능하지만, 우선 Server측 SPOKEN 분리에 사용).
- `class SentenceSplitter: def split(self, buffer: str) -> list[str]` — 마지막 원소가 미완성 버퍼.
- 종결부호(., ?, !) + 길이 10자 초과 쉼표 분리 규칙 유지.
**검증**: `tests/test_sentence_splitter.py`
- "네, 안녕." → 완성 1, 미완성 1
- "네, 오늘 서울 날씨는 맑고, 내일은" → 쉼표 분리 + 미완성
- 빈 문자열·공백·종결부호 없는 한 단어 케이스
**리뷰 포인트**: 토큰이 부호 사이 경계에 걸리는 케이스(`"맑."` + `"다."`).

### 2-3. `server/cloud/dual_stream_parser.py` (신규) ★ 핵심
**목표**: `display_enabled=true`일 때 LLM 토큰 스트림에서 `[SPOKEN]...[/SPOKEN]` / `[DISPLAY]...[/DISPLAY]` 섹션을 채널별로 분리.
- 상태 머신: `PREAMBLE → SPOKEN → INTER → DISPLAY → DONE`
- 태그가 토큰 경계에 걸릴 가능성을 고려: 마지막 `MAX_TAG_LEN-1` 자를 hold-back 버퍼로 유지.
- API: `async def parse(self, token_stream: AsyncIterator[str]) -> AsyncIterator[tuple[Literal["tts","display"], str]]`
- 태그 자체와 섹션 외부 토큰(공백/개행)은 yield하지 않음.
**검증**: `tests/test_dual_stream_parser.py`
- 태그가 한 토큰 안에 모두 포함되는 케이스
- 태그가 두 토큰에 걸치는 경계 케이스 (`"[SPOK"`, `"EN]네,"`)
- DISPLAY 섹션이 닫히지 않고 종료되는 비정상 케이스
- SPOKEN→DISPLAY 사이 공백/개행만 들어오는 케이스
**리뷰 포인트**: 메모리 안전(무한 누적 금지), `async for`의 backpressure.

### 2-4. `server/cloud/prompt_templates.py` 수정
**목표**: `get_system_prompt(language, display_enabled=False)` 시그니처로 확장.
- `display_enabled=False` (기본): 기존 SYSTEM_PROMPT_KO/EN 그대로 반환.
- `display_enabled=True`: `[SPOKEN]...[/SPOKEN]` (구어체 3~5문장, 30자/문장, 마크다운/이모지 금지, 숫자 한글)와 `[DISPLAY]...[/DISPLAY]` (마크다운 자유)를 정확한 순서로 생성하라는 새 프롬프트.
- 두 섹션 외 어떤 텍스트도 출력 금지 명시.
**검증**: `tests/test_prompt_templates.py`
- 기본 호출이 기존 문자열과 동일한지 (회귀 보호).
- `display_enabled=True` 시 `[SPOKEN]`/`[/SPOKEN]`/`[DISPLAY]`/`[/DISPLAY]` 4 토큰 모두 포함되는지.
**리뷰 포인트**: 한국어/영어 두 변형 모두 일관된 태그 규칙.

### 2-5. `server/cloud/llm_client.py` 수정
**목표**: 이중 섹션 출력 + 세션 저장/복원 메서드 추가.
- 생성자에 `display_enabled: bool = False` 추가, `self.display_enabled` 보관.
- `get_response_stream()` 분기:
  - `display_enabled=False`: 기존 코드 (변경 최소화).
  - `display_enabled=True`: `astream` → `DualStreamParser.parse()` 통과 → `AsyncIterator[tuple[channel, str]]` yield. 히스토리 누적 시 DISPLAY 섹션 내용만 `AIMessage`로 저장 (없으면 SPOKEN으로 폴백).
- 내부 안내 메시지(요약 대기, 폴백)도 `display_enabled=True`일 때 `("tts", msg)` 튜플로 yield.
- `save_and_clear_history(repository, language) -> None`: non-streaming `ainvoke`로 첫 2~3턴 한 줄 요약(15자 이내) 생성 → `SessionRecord` 작성 → `repository.save()` → `clear_history()`. 실패 시 첫 HumanMessage를 subject로 폴백.
- `restore_history(history_json: str) -> None`: JSON → `HumanMessage`/`AIMessage` 리스트로 역직렬화 후 `self.conversation_history`에 적재.
- 단일 프로바이더 강제: 생성자에서 `provider` 한 개만 받는다. 티어 분기(`models["haiku"]` / `models["sonnet"]`)는 단일 모델로 단순화 (`config/server.yaml`의 `cloud.claude_model` 또는 `cloud.gpt_model` 한 개 사용).
- **Ollama 로컬 폴백 코드 제거 (사용자 결정 확정)**: `_local_fallback`, `LOCAL_FALLBACK_MESSAGES`, `langchain-ollama` import, 관련 생성자 인자(`local_fallback`, `local_model`, `local_base_url`) 모두 삭제. 실패 시 한 번의 안내 메시지("지금 응답할 수 없어요. 잠시 뒤 다시 불러 주세요") yield 후 세션 종료 신호 반환. fallback chain은 Backlog로 유지.

**검증**:
- 기존 `server/cloud/test_multiturn.py` 그대로 통과 (회귀).
- 새 `tests/test_llm_dual_section.py`: 이중 섹션 모드에서 ("tts", str)/("display", str) 튜플이 순서대로 도착하는지 모킹된 astream으로 확인.
- `restore_history` 후 `get_response_stream`이 이전 맥락을 인식하는지 (멀티턴 회귀 테스트).
**리뷰 포인트**: 히스토리 직렬화 포맷 합의 (JSON 스키마: `[{"role": "human|ai", "content": "..."}]`).

### 2-6. `server/intent/intent_handlers.py` (신규)
**목표**: 인텐트별 응답 로직을 한 곳에 모은다.
```python
@dataclass
class IntentResult:
    text: str
    signal: str          # "end_session" | "pause_listening" | "intent" | "list_sessions"
    save_and_clear: bool = False

async def handle_end_session(intent_cfg, language) -> IntentResult: ...
async def handle_pause_listening(intent_cfg, language) -> IntentResult: ...
# list_sessions는 DB 조회를 동반하므로 orchestrator에서 직접 처리

INTENT_HANDLERS = {"end_session": handle_end_session, "pause_listening": handle_pause_listening}
```
- 응답 텍스트는 `config["intents"][name]["responses"][language]` (한/영 키 없으면 한국어 폴백).
**검증**: `tests/test_intent_handlers.py`
- end_session → `signal="end_session"`, `save_and_clear=True`
- pause_listening → `signal="pause_listening"`, `save_and_clear=False`
- 미지원 언어 → ko 폴백.
**리뷰 포인트**: list_sessions가 핸들러 dict에 들어가지 않는 이유(부수효과 = DB 조회 + waiting_for_selection 플래그)를 docstring에 명시.

### 2-7. `server/storage/{__init__.py, models.py, session_repository.py}` (신규)
**목표**: SQLite 기반 세션 저장.
- `models.py`: `@dataclass SessionRecord(id, subject, language, turn_count, history_json, created_at, ended_at)`.
- `session_repository.py`: `class SessionRepository` — 표준 `sqlite3` 사용, `aiosqlite` 미도입(asyncio 호환은 `asyncio.to_thread`로 래핑).
  - `__init__(db_path: str)`: 파일 경로 디렉터리 생성 + `CREATE TABLE IF NOT EXISTS sessions(...)`
  - `async def save(record)` / `async def get(id) -> Optional[SessionRecord]` / `async def get_recent(limit) -> list[SessionRecord]`
**검증**: `tests/test_session_repository.py`
- 임시 DB에 save → get → 일치 확인
- get_recent 정렬(`ended_at DESC`) 검증
- 빈 DB에서 get_recent → 빈 리스트
**리뷰 포인트**: id 생성 방식(`uuid4().hex` 권장), `created_at`/`ended_at` 직렬화(ISO 8601 string).

### 2-8. `server/orchestrator.py` 재구성
**목표**: 구조를 새 책임 모델에 맞춘다.
- 제거: `SupertonicTTSService` 의존, `select_model_tier`, `check_local_intent`, `LocalIntentResult`, `_load_tts_service`, `get_tts_service`, `get_timeout_prompt`, `get_timeout_end` (Edge로 이관됨).
- 유지: `SystemReader`, STT 로드, Cloud client 초기화, `is_supported_language`, `get_unsupported_language_message`, `get_system_stats`, `_drop_page_cache`.
- 추가:
  - `IntentClassifier` 초기화 (startup, 임베딩 모델 + 예시 임베딩 사전 계산).
  - `SessionRepository` 초기화.
  - `display_enabled = config["cloud"].get("display_enabled", False)` 노출.
  - `get_prompt(language)` → `get_system_prompt(language, self.display_enabled)`.
  - `async def classify_intent(text) -> Optional[dict]` — `to_thread(self.intent_classifier.classify, text)`.
  - 상태: `waiting_for_selection: bool = False`, `pending_sessions: list[SessionRecord] = []`.
  - `async def handle_session_selection(text) -> Optional[SessionRecord]` — 숫자 파싱(한글 "일/이/삼" 또는 아라비아 숫자) → 인덱스 매핑.
**검증**:
- `python -m server.main --server --device jetson_orin_nano`로 startup 로그 정상 출력.
- 단위 테스트는 Phase 6 통합 테스트에서 수행 (orchestrator는 의존성이 많아 단위 테스트 비용이 큼).
**리뷰 포인트**: 코드 라인 수가 늘어날 경우 분할 여부 (예: `server/intent/router.py`).

### 2-9. `server/grpc_server.py` 재구성
**목표**: 텍스트 기반 응답 + 인텐트 라우팅 + display_enabled 채널 분기.
- 제거: `TimeoutPrompt` 핸들러, `_tts_to_stream`, `_TTS_SAMPLE_RATE`, TTS audio chunking.
- 추가/수정:
  - `_send_text(text, language, type, is_final)` 헬퍼 — `pb2.VoiceResponse(text_response=...)` yield.
  - `ProcessVoice` 흐름:
    1. 오디오 수집 → STT
    2. STTResult yield + 대시보드 `broadcast_session_event({"type":"user_message","text":...})`
    3. **session selection 모드면** `orchestrator.handle_session_selection()` 우선 처리.
    4. 미지원 언어면 안내 텍스트 yield (type="sentence", is_final=True).
    5. `await orchestrator.classify_intent(text)` →
       - 결과 있고 `intent in ("end_session","pause_listening")`: `intent_handlers`로 응답 텍스트 + signal 결정 → text yield (type=signal, is_final=True). end_session이면 응답 yield 후 `asyncio.create_task(cloud_client.save_and_clear_history(...))`.
       - 결과 있고 `intent == "list_sessions"`: orchestrator가 DB 조회(개수 = display_enabled에 따라 5 또는 3) → 음성용 텍스트 + 대시보드 브로드캐스트 → `waiting_for_selection=True`.
       - 미감지: Cloud LLM 호출.
    6. Cloud LLM 호출:
       - `display_enabled=False`: 토큰 → `SentenceSplitter`로 문장 단위로 잘라 text yield (type="sentence"). 마지막 문장 is_final=True.
       - `display_enabled=True`: `cloud_client.get_response_stream(...)`이 yield하는 `(channel, content)`을 `asyncio.Queue`로 분기. tts 채널 → SentenceSplitter → text yield. display 채널 → `broadcast_display_token`.
    7. 종료 시 `broadcast_session_event({"type":"assistant_done"})`.
- `EndSession` RPC: 단순 안내 텍스트 yield (Edge 명시 종료 경로).
- `HealthCheck`: tts_ready 필드는 항상 True 또는 deprecation 주석.
**검증**:
- `grpcurl -plaintext localhost:50051 voiceassistant.VoiceService/HealthCheck` 응답.
- 별도 mock client 스크립트(`scripts/mock_grpc_client.py` — 신규)로 wav 파일 전송 → 텍스트 응답 수신 확인.
- display_enabled false/true 두 모드 모두 시연.
**리뷰 포인트**: `asyncio.Queue`로 채널 분기할 때 producer/consumer 종료 신호(sentinel) 처리.

### 2-10. `server/main.py` 갱신
**목표**: `--device` CLI 옵션, 대시보드 task 병행 실행.
- `argparse`로 `--server` 플래그(현재 진입점은 server 전용이므로 future-proof) + `--device` (default `jetson_orin_nano`).
- `get_config("server", device=args.device)`.
- `dashboard.enabled` 시 `asyncio.create_task(uvicorn_server.serve())`로 FastAPI 병행 실행. 종료 시 graceful shutdown.
**검증**: `python -m server.main --server --device jetson_orin_nano` 시 gRPC + Dashboard 동시 listen.
**리뷰 포인트**: uvicorn `asyncio` 이벤트 루프 충돌 — `Server.serve()` 사용 시 같은 루프에서 안전.

### 2-11. Phase 2 통합 점검
- `server/tts/`는 아직 삭제하지 않음 (Phase 6).
- 모든 신규 단위 테스트 통과 확인.
- `server/cloud/test_multiturn.py` 회귀 통과 확인.

---

## Phase 3 — 대시보드 (FastAPI WebSocket + UI, 총 4 단계)

### 3-1. `server/dashboard/__init__.py` (빈 파일) + `server/dashboard/app.py`
- 모듈 전역 `_clients: set[WebSocket]`.
- `async def broadcast_display_token(token: str)`, `async def broadcast_session_event(event: dict)`.
- `def create_app(orchestrator) -> FastAPI`:
  - `GET /` → `static/index.html` (FileResponse)
  - `GET /health` → `{"status":"ok","clients":N}`
  - `WS /ws` → 등록/해제, BasicAuth는 환경변수 `DASHBOARD_USER`/`DASHBOARD_PASS`로 보호 (없으면 anonymous + 경고 로그).
- 메시지 타입: `user_message` / `display_token` / `assistant_done` / `session_end` / `session_list`.
**검증**: `tests/test_dashboard_broadcast.py`
- WebSocket 클라이언트 연결 → broadcast 호출 → 메시지 수신.

### 3-2. `server/dashboard/static/index.html`
- 단일 HTML, CDN `marked.js` + `highlight.js`. 타이핑 효과는 단순 append.
- 다크 테마 기본.

### 3-3. `server/dashboard/static/app.js`
- `display_token` → 어시스턴트 말풍선 buffer에 append.
- `assistant_done` → buffer를 `marked.parse()`로 마크다운 렌더 + 코드 하이라이트.
- `user_message` → 사용자 말풍선.
- `session_list` → 번호 리스트 표시.

### 3-4. `server/dashboard/static/style.css`
- 사용자/어시스턴트 말풍선, 마크다운 기본 스타일.

**Phase 3 검증**: 브라우저 `http://<server_ip>:8080` 접속 → mock event로 렌더 확인. (mock event 송신 스크립트는 `scripts/dashboard_mock_emit.py` — 선택).

---

## Phase 4 — Edge 신규 구현 (총 8 단계)

목표: 발화 캡처 → 웨이크워드 → VAD → gRPC → TTS 재생 + 상태 머신 + 타임아웃/PAUSED.

### 4-1. `edge/__init__.py` + `edge/grpc_client.py`
- `class VoiceServiceClient`:
  - `__init__(host, port)`, `connect()`, `close()`
  - `async def process_voice(audio_iter) -> AsyncIterator[VoiceResponse]`
  - `async def end_session(language)`, `async def health_check() -> ServerStatus`
  - 재시도/재연결 정책은 `config/edge.yaml`의 `grpc.reconnect_interval_s` 사용.
**검증**: 더미 server 띄운 상태에서 `python -m edge.grpc_client --healthcheck`.

### 4-2. `edge/audio/capture.py`
- PyAudio (또는 `sounddevice`) 기반 16kHz mono 캡처.
- `async def stream_chunks() -> AsyncIterator[bytes]` — VAD가 사용할 chunk 단위.
- ReSpeaker XVF3800 디바이스 인덱스 자동 감지(이름 매칭) — 실패 시 default device + warning.
**검증**: `python -m edge.audio.capture --duration 3` → 48000 byte 출력 길이 확인.

### 4-3. `edge/audio/playback.py`
- PyAudio `Stream` write 기반 재생.
- `async def play_pcm(pcm: bytes, sample_rate: int)` — 비동기 래핑(`to_thread`).
- 재생 중 차단 가능, 큐는 상위 파이프라인에서 관리.

### 4-4. `edge/audio/vad.py`
- Silero VAD ONNX 로드. `async def is_speech(chunk) -> bool` + 발화 시작/종료 감지 헬퍼.

### 4-5. `edge/audio/wakeword.py`
- openWakeWord 모델 로드. `def detect(chunk) -> bool`. PAUSED와 IDLE에서 공통 사용.

### 4-6. `edge/tts/supertonic_service.py` + `edge/tts/tts_pipeline.py`
- `SupertonicTTSService.synthesize(text, language) -> bytes` — Server측 코드와 동일 (이관). `_split_sentences`는 Server `sentence_splitter`로 이미 이관됨.
- `TTSPipeline`:
  - `asyncio.Queue[Tuple[str,str]]` (text, language).
  - `add_sentence`, `run` (consumer loop: 큐 → synthesize → playback), `stop` (sentinel).
  - 재생 중에도 큐잉 가능 — 끊김 없는 파이프라인.

### 4-7. `edge/orchestrator.py`
- 상태 머신: `IDLE → LISTENING → PROCESSING → SPEAKING → LISTENING`, 부가 `PAUSED`, `DEGRADED`.
- 타임아웃 (Edge 로컬):
  - LISTENING 60s 무발화 → `timeout_messages.prompt` TTS.
  - 추가 5s 무발화 → `timeout_messages.end` TTS → `EndSession` RPC → IDLE.
- PAUSED:
  - `pause_listening` signal 수신 시 진입. 오디오 송신 OFF, VAD OFF, wakeword ON, 10분 타이머.
  - 웨이크워드 감지 → LISTENING 복귀 (세션 유지). 만료 → 종료 경로.
- end_session signal 수신 시 → ack TTS 재생 후 IDLE.
**검증**: `tests/test_edge_state_machine.py` (가능하면 — fake gRPC client + fake clock).

### 4-8. `edge/main.py`
- `argparse`: `--edge`, `--device {jetson_nano, raspberry_pi_4b}`.
- `get_config("edge", device=args.device)`.
- 모듈 로드 → orchestrator 생성 → 종료 신호 처리.
**검증**: 실 디바이스에서 wake word → 발화 → 응답 재생 end-to-end.

---

## Phase 5 — 세션 저장/Resume 연결 (총 2 단계)

### 5-1. `end_session` 경로
- grpc_server에서 end_session 응답 yield 직후 `asyncio.create_task(cloud_client.save_and_clear_history(repository, language))`. (Edge 응답 차단 없음)
- 실패 케이스(요약 LLM 실패): 첫 HumanMessage 텍스트로 폴백 후 그대로 저장.
**검증**: end_session 인텐트 발화 → SQLite `data/sessions.db` 레코드 1건 추가 확인.

### 5-2. `list_sessions` 경로
- orchestrator가 `repository.get_recent(limit)` 호출. limit = `display_enabled ? max_list_count : max_list_count_voice`.
- 음성 응답: "최근 대화 N개입니다. 1번 ..., 2번 ..." 형태.
- 대시보드 broadcast: `{"type":"session_list","items":[...]}`.
- `waiting_for_selection=True`. 다음 ProcessVoice의 STT 텍스트가 숫자로 시작하면 `restore_history` 호출, 아니면 플래그 해제 후 일반 처리.
**검증**: list_sessions → 번호 발화 → restore 후 멀티턴 이어가기 시나리오.

---

## Phase 6 — 정리 + 통합 테스트 (총 3 단계)

### 6-1. 잔존 코드 삭제
- `server/tts/supertonic_service.py`, `server/tts/__init__.py` 삭제 (모든 import가 끊겼는지 grep 후 진행).
- `server/orchestrator.py`의 옛 메서드/상수 잔재 (`_WHISPER_*`, `_TTS_MB`는 STT 로드에서만 쓰이면 유지) 정리.

### 6-2. 통합 테스트 시나리오 (수동/E2E)
- **시나리오 A** (display_enabled=false): wake → "오늘 날씨 어때" → 첫 TTS ≤ 2.5s 측정. 종료 인텐트로 마무리.
- **시나리오 B** (display_enabled=true): 동일 발화 + 대시보드 마크다운 응답 동시 표시. 첫 TTS 추가 지연 ≤ 100ms 측정.
- **시나리오 C**: pause_listening → 10분 후 자동 종료, 또는 중간 wake word 복귀 확인.
- **시나리오 D**: end_session → SQLite 레코드 → list_sessions로 복원 → 멀티턴 이어가기.
- **시나리오 E**: LLM API 강제 실패(잘못된 키) → "지금 응답할 수 없어요" 안내 후 세션 종료.

### 6-3. 회고 체크리스트
- KPI(2.5s, 80ms, 100ms) 측정값 기록.
- 메모리 사용량 (Jetson `tegrastats`) 기록.
- README/CLAUDE.md 갱신 (실제 모듈 경로/명령 일치 확인).

---

## 검증 매트릭스 (단계 완료 기준)

| 단계 | 자동 검증 | 수동 검증 | 사용자 리뷰 항목 |
|------|-----------|-----------|------------------|
| 2-1 | import smoke | — | proto 필드 일치 |
| 2-2 | pytest | — | 분리 규칙 |
| 2-3 | pytest (경계 케이스) | — | 상태 머신/메모리 안전 |
| 2-4 | pytest | — | 프롬프트 톤 |
| 2-5 | pytest + multiturn 회귀 | mock astream | 폴백 정책 결정 |
| 2-6 | pytest | — | 핸들러 분리 기준 |
| 2-7 | pytest (임시 DB) | — | 스키마 |
| 2-8 | startup 로그 | — | 책임 경계 |
| 2-9 | grpcurl + mock client | — | 채널 분기 |
| 2-10 | uvicorn 동시 listen | 브라우저 접속 | shutdown 흐름 |
| 3 | pytest + 브라우저 mock | 실제 LLM 응답 표시 | UI 톤 |
| 4 | 단위 테스트 + 디바이스 더미 | 실 하드웨어 발화 | 상태 머신 |
| 5 | DB 레코드 검사 | restore 후 멀티턴 | subject 품질 |
| 6 | 모든 회귀 통과 | 5가지 E2E 시나리오 | KPI 충족 |

---

## 의사결정 사항 (확정)

1. ✅ **LLM 폴백 정책**: Ollama 로컬 폴백 코드 **제거**. 실패 시 정적 안내 메시지 후 세션 종료. (Phase 2-5에서 적용)
2. ✅ **Edge 디바이스 우선순위**: **jetson_nano** 우선. raspberry_pi_4b는 후순위. (Phase 4 시연/검증 기준)
3. ✅ **테스트 러너**: `pytest` + `pytest-asyncio` 도입. `server/requirements.txt`(또는 신규 `requirements-dev.txt`)에 추가. async 테스트는 `@pytest.mark.asyncio` 데코레이터 사용.

## 의사결정 대기 항목 (해당 Phase 진입 시 결정)

1. **세션 history 직렬화 스키마 (Phase 2-7 진입 시)**: 기본안 `[{"role":"human|ai","content":"..."}]` JSON. 더 풍부한 메타데이터(timestamp 등) 필요 여부.
2. **list_sessions 숫자 파싱 범위 (Phase 5-2 진입 시)**: 한글 수사("일/이/삼", "하나/둘/셋") 지원 여부. 음성 인식 변형이 다양하므로 단순화(아라비아 숫자만) 가능.

---


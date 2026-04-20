# fix_plan.md

> 상세 설계 의도는 각 컴포넌트의 `CLAUDE.md` 참조. 여기서는 **무엇을, 어떻게** 만드는가에 집중.

---

## Phase 1: 기반 — 디바이스 프로파일 + Config + Proto  ✅ 완료

### 1-1. `shared/device_profiles.py` ✅
디바이스별 설정 프로파일 (`role`, `memory_mb`, `has_gpu` 등).
`get_device_profile(device_id)` / `list_devices(role)` 함수 포함.

### 1-2. `shared/config.py` 수정 ✅
`get_config(role, device=None)` — device 인자 추가.
device 지정 시 디바이스 프로파일을 config에 딥 머지. 캐시 키를 `(role, device)` 튜플로 변경.

### 1-3. `config/server.yaml` 수정 ✅
- `tts` 섹션 제거
- `timeout_messages` 섹션 제거
- `local_intent_llm` 섹션 제거 → `intent_classifier` 섹션으로 교체:
  ```yaml
  intent_classifier:
    model: "paraphrase-multilingual-MiniLM-L12-v2"
    threshold: 0.75   # 코사인 유사도 임계값
    device: "cpu"     # 임베딩은 CPU 실행 (GPU는 Whisper 전용)
  intents:
    end_session:
      examples: ["대화 종료해줘", "그만할게", "끝내줘", "stop the conversation", "end session"]
    pause_listening:
      examples: ["잠깐 멈춰", "잠시 대기해줘", "pause for a while", "wait a moment"]
      duration_pattern: '(\d+)\s*(분|시간|초|min|hour|sec)'  # duration_s 정규식 추출
    list_sessions:
      examples: ["지난 대화 보여줘", "이전 대화 목록", "show past conversations"]
  ```
- `cloud.max_tokens` → `1600` (SPOKEN+DISPLAY 동시 생성)
- `cloud.display_enabled: false` 추가 — DISPLAY 섹션 on/off 토글
  - `false`(기본): 단일 섹션 음성 전용 프롬프트, 기존 TTS 파이프라인 유지
  - `true`: 이중 섹션 출력 (`[SPOKEN]` → TTS, `[DISPLAY]` → 대시보드)
  - SPOKEN은 두 모드 모두에서 항상 출력됨
- `dashboard` 섹션 추가 (`enabled`, `host: "0.0.0.0"`, `port: 8080`)

### 1-4. `config/edge.yaml` ✅
Edge 전용 설정: `tts`, `audio`, `wakeword`, `vad`, `timeout`, `timeout_messages`.
`timeout_messages.prompt/end` — Edge가 로컬에서 직접 TTS 재생, Server 호출 없음.

### 1-5. `proto/voice_service.proto` 수정 ✅
제거: `TimeoutPrompt` RPC, `TimeoutRequest` 메시지, `TimeoutType` enum.

### 1-6. Proto stub 재생성 ✅
```bash
python -m grpc_tools.protoc -I proto --python_out=proto --grpc_python_out=proto proto/voice_service.proto
```

---

## Phase 2: Server 코드 수정

### 2-1. `server/intent/intent_classifier.py` (신규)
`sentence-transformers` 기반 임베딩 유사도 분류기.

```python
class IntentClassifier:
    def __init__(self, config: dict):
        # SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2") 로드
        # config["intents"]의 examples를 미리 임베딩하여 저장 (startup 시 1회)

    def classify(self, text: str) -> dict | None:
        # 사용자 발화 임베딩 → 저장된 예시 임베딩과 코사인 유사도 계산
        # best_score >= threshold → {"intent": name, ...}
        # best_score < threshold  → None (Cloud LLM으로 라우팅)
```
- 동기 메서드 (`encode`가 동기) — asyncio executor로 감싸거나 startup 시 워밍업
- `pause_listening` 인텐트 감지 시: `duration_pattern` 정규식으로 `duration_s` 추출
- 할루시네이션 없음. JSON 파싱 오류 없음. 추론 ~50ms (CPU)

### 2-2. `server/intent/intent_handlers.py` (신규)
인텐트별 핸들러. 새 인텐트 추가 시 이 파일에만 함수 추가.

```python
@dataclass
class IntentResult:
    text: str
    signal: str                  # TextResponse.type 값
    save_and_clear_session: bool

INTENT_HANDLERS = {"end_session": ..., "pause_listening": ...}
# list_sessions는 orchestrator에서 DB 조회 후 직접 처리
# pause_listening: duration_s는 IntentClassifier가 정규식으로 추출하여 dict에 포함
```

### 2-3. `server/cloud/sentence_splitter.py` (신규)
SPOKEN 채널 전용. `server/tts/supertonic_service.py`의 `_split_sentences()` 로직 이관.

```python
class SentenceSplitter:
    def split(self, buffer: str) -> list[str]:
        # 반환: [완성문장..., 미완성버퍼]  (마지막 원소가 미완성)
```

### 2-4. `server/cloud/dual_stream_parser.py` (신규) ★ 핵심
`display_enabled: true`일 때만 사용. 스트리밍 토큰에서 `[SPOKEN]...[/SPOKEN]` / `[DISPLAY]...[/DISPLAY]` 분리.

```python
class DualStreamParser:
    # 상태: PREAMBLE → SPOKEN → INTER → DISPLAY → DONE
    # 태그가 토큰 경계에 걸릴 수 있으므로 내부 버퍼 누적 후 처리
    # 마지막 MAX_TAG_LEN-1 자를 홀드백하여 경계 케이스 처리
    async def parse(self, token_stream) -> AsyncIterator[tuple[Literal["tts","display"], str]]:
```
- 태그 자체는 yield하지 않음. 섹션 밖 토큰(공백, 줄바꿈)은 버림.
- SPOKEN 채널 → `("tts", text)`, DISPLAY 채널 → `("display", text)`

### 2-5. `server/cloud/prompt_templates.py` 수정
`display_enabled`에 따라 프롬프트를 분기.

```python
def get_system_prompt(language: str, display_enabled: bool = False) -> str:
```

- `display_enabled=False`(기본): 기존 단일 섹션 음성 전용 프롬프트 반환 (변경 없음)
- `display_enabled=True`: 이중 섹션 프롬프트 반환
  - `[SPOKEN]`: 구어체 3~5문장, 30자/문장, 마크다운·이모지 금지, 숫자 한글 표기
  - `[DISPLAY]`: 길이 제한 없음, 마크다운 자유
  - 두 섹션 외 다른 텍스트 금지

### 2-6. `server/cloud/llm_client.py` 수정
- 생성자에 `display_enabled: bool = False` 파라미터 추가
- `get_response_stream()` 동작 분기:
  - `display_enabled=False`: 기존 코드 유지. `AsyncIterator[str]` yield.
  - `display_enabled=True`: `DualStreamParser` 사용. `AsyncIterator[tuple[Channel, str]]` yield.
- 히스토리 저장:
  - `display_enabled=False`: `full_response` 전체를 `AIMessage`로 저장 (기존 동작)
  - `display_enabled=True`: DISPLAY 섹션 내용만 저장. `_extract_display_content()` 헬퍼 사용.
- 내부 wait/fallback 메시지: `display_enabled=True`시 `("tts", msg)` 튜플로 yield
- `save_and_clear_history(repository, language)` 메서드 추가:
  - Cloud LLM에 별도 non-streaming 호출로 첫 2~3턴 한 줄 요약(subject, 15자 이내) 생성
  - 실패 시 폴백: 첫 `HumanMessage` 텍스트를 subject로 사용
  - `SessionRecord` 생성 → `repository.save()` → `clear_history()`
  - Edge가 이미 응답을 받은 후 비동기 실행 → 사용자 체감 지연 없음
- `restore_history(history_json)` 메서드 추가 (JSON → history 역직렬화)

### 2-7. `server/orchestrator.py` 수정
제거: `SupertonicTTSService`, `select_model_tier()`, `check_local_intent()`, `get_tts_service()`  
추가:
- `_init_cloud_client()`에서 `display_enabled = cloud_config.get("display_enabled", False)` 읽어 `CloudLLMClient`에 전달. `self.display_enabled`로 노출.
- `get_prompt(language)`에서 `get_system_prompt(language, self.display_enabled)` 호출.
- `IntentClassifier` 초기화 (startup, 임베딩 모델 로드 및 예시 사전 계산)
- `SessionRepository` 초기화 (startup)
- `classify_intent(text)` — 동기 분류 (asyncio executor 경유)
- `waiting_for_selection: bool`, `pending_sessions: list[str]` 세션 선택 플래그
- `handle_session_selection(text)` — 숫자 파싱 → `restore_history()`

### 2-8. `server/grpc_server.py` 수정
`orchestrator.display_enabled`에 따라 LLM 응답 처리 분기:

**`display_enabled=False`**: 기존 경로 유지. `llm_stream`(str)을 직접 TTS로 연결.

**`display_enabled=True`**: 채널별 분기.
- `asyncio.Queue`로 tts/display 채널을 비동기 분리 (SPOKEN이 먼저 오므로 순서 보장)
- `("tts", content)` → `SentenceSplitter` → Edge `TextResponse` gRPC
- `("display", content)` → `broadcast_display_token()`

추가:
- STT 결과 수신 시 `broadcast_session_event({"type": "user_message", ...})`
- 응답 완료 시 `broadcast_session_event({"type": "assistant_done"})`

제거:
- `TimeoutPrompt` 핸들러 (proto에서 삭제됨)
- `_tts_to_stream()` → `_text_response()` 헬퍼로 교체
- `select_model_tier()` 호출

### 2-9. `server/main.py` 수정
```python
parser.add_argument("--server", action="store_true")
parser.add_argument("--device", choices=["jetson_orin_nano", "jetson_orin_nx"], default="jetson_orin_nano")
```
`dashboard.enabled` 시 `asyncio.create_task(uvicorn_server.serve())`로 gRPC와 병행 실행.

### 2-10. `shared/models.py` 수정
- `TTSRequestData` 제거
- `TextResponseData` 추가 (`text`, `language`, `type`, `is_final`, `to_proto()`, `from_proto()`)

### 2-11. ~~Barge-in~~ (제거됨)
SPEAKING 상태에서 TTS 재생 완료 → LISTENING 복귀로 단순화. 구현하지 않음.

---

## Phase 3: 대시보드 (FastAPI WebSocket + 브라우저 UI)

### 3-1. `server/dashboard/__init__.py` (신규, 빈 파일)

### 3-2. `server/dashboard/app.py` (신규)
```python
_clients: set[WebSocket]  # 모듈 전역

async def broadcast_display_token(token: str) -> None: ...
async def broadcast_session_event(event: dict) -> None: ...

def create_app(orchestrator) -> FastAPI:
    # GET /        → index.html
    # GET /health  → {"status": "ok", "clients": N}
    # WS  /ws      → 클라이언트 등록/해제
```
WebSocket 메시지 타입: `user_message`, `display_token`, `assistant_done`, `interrupted`, `session_end`

### 3-3. `server/dashboard/static/index.html` (신규)
마크다운 렌더링: CDN `marked.js`. 코드 하이라이트: CDN `highlight.js`.

### 3-4. `server/dashboard/static/app.js` (신규)
- `display_token` → 버퍼에 append (스트리밍 중 plain text 표시)
- `assistant_done` → `marked.parse(buffer)` 마크다운 렌더링
- `user_message` → 사용자 말풍선 생성

### 3-5. `server/dashboard/static/style.css` (신규)
다크 모드, 사용자/어시스턴트 말풍선 구분, 마크다운 스타일.

---

## Phase 4: Edge 코드 (전체 신규 — 현재 CLAUDE.md만 존재)

### 4-1. `edge/tts/supertonic_service.py` (신규)
```python
class SupertonicTTSService:
    async def synthesize(self, text: str, language: str) -> bytes:  # PCM 16kHz mono
```
`config/edge.yaml`에서 `voice_style`, `inference_steps` 읽기.

### 4-2. `edge/tts/tts_pipeline.py` (신규)
`asyncio.Queue` 기반 수신→변환→재생 파이프라인. 재생 중 다음 문장 큐잉으로 끊김 없음.
```python
class TTSPipeline:
    async def add_sentence(self, text: str, language: str) -> None: ...
    async def run(self) -> None: ...
    async def stop(self) -> None: ...
```

### 4-3. `edge/orchestrator.py` (신규)
상태 머신: `IDLE → LISTENING → PROCESSING → SPEAKING → LISTENING`

타임아웃 (Server 호출 없이 Edge 로컬):
1. 60초 무발화 → `timeout_messages.prompt` TTS
2. 5초 추가 무발화 → `timeout_messages.end` TTS → `EndSession` RPC → IDLE

PAUSED 상태 (`pause_listening` 인텐트 수신 시):
- 오디오 전송 OFF, VAD OFF, 웨이크워드만 ON
- 웨이크워드 감지 → LISTENING 복귀 (세션 유지)
- `duration_s` 타이머 만료 → IDLE

`grpc_client.TimeoutPrompt()` 호출 코드 작성하지 않음 (proto 제거됨).

### 4-4. `edge/main.py` (신규)
```python
parser.add_argument("--edge", action="store_true")
parser.add_argument("--device", choices=["jetson_nano", "raspberry_pi_4b"], default="jetson_nano")
```

---

## Phase 5: 대화 이력 저장 및 Resume

### 5-1. `server/storage/models.py` (신규)
```python
@dataclass
class SessionRecord:
    id: str; subject: str; language: str; turn_count: int
    history_json: str  # DISPLAY 섹션 응답만 저장
    created_at: datetime; ended_at: datetime
```
subject: Cloud LLM이 첫 2~3턴을 한 줄 요약(15자 이내). 실패 시 첫 HumanMessage 텍스트로 폴백.
생성은 `llm_client.save_and_clear_history()` 내부에서 non-streaming 호출로 처리.

### 5-2. `server/storage/session_repository.py` (신규)
표준 `sqlite3` 사용 (SQLAlchemy 불필요). DB: `data/sessions.db`.
```python
class SessionRepository:
    async def save(self, record: SessionRecord) -> None: ...
    async def get(self, session_id: str) -> SessionRecord | None: ...
    async def get_recent(self, limit: int = 5) -> list[SessionRecord]: ...
```

### 5-3. `server/cloud/llm_client.py` 추가 수정
`save_and_clear_history()`, `restore_history()` — Phase 2-6에서 이미 명세.

### 5-4. `server/orchestrator.py` 추가 수정
`list_sessions` 인텐트: DB 조회 → 목록 Edge 전송 + 대시보드 브로드캐스트 → `waiting_for_selection=True`.
Phase 2-7에서 이미 명세한 `handle_session_selection()` 연결.

---

## Phase 6: 정리

### 6-1. `server/tts/` 삭제
`server/tts/supertonic_service.py`, `server/tts/__init__.py` 삭제.
(orchestrator에서 TTS import 완전 제거 후 진행)

### 6-2. `tests/` 신규
- `tests/test_dual_stream_parser.py` — 토큰 경계 케이스 포함
- `tests/test_prompt_templates.py` — 이중 섹션 태그 포함 응답 확인
- `tests/test_dashboard_broadcast.py` — WebSocket 브로드캐스트 단위 테스트

---

## 단계별 검증 포인트

| 완료 시점 | 확인 항목 |
|-----------|-----------|
| Phase 1 | `config/server.yaml`에 `dashboard` 섹션, `max_tokens: 1600` |
| 2-4 | `DualStreamParser` 단위 테스트 통과 (토큰 경계 케이스 포함) |
| 2-5 | `test_multiturn.py`로 실제 Cloud 응답에 `[SPOKEN]`/`[DISPLAY]` 태그 확인 |
| 2-8 | grpcurl로 `ProcessVoice` 호출 → 이중 채널 분기 동작 |
| Phase 3 | `http://localhost:8080` → WebSocket → mock 데이터 렌더링 |
| 2+3 통합 | server만 실행 → gRPC 호출 → 대시보드 실시간 DISPLAY 스트리밍 |
| Phase 4 | Edge-Server 통합: 발화 → SPOKEN TTS 재생 + DISPLAY 브라우저 동시 표시 |
| Phase 5 | 종료 인텐트 → SQLite 레코드 저장 확인 |

# 코드 수정 계획 (fix_plan.md)

## 배경

아키텍처 변경사항:
1. Edge 디바이스 추가: Raspberry Pi 4B 8GB (`raspberry_pi_4b`)
2. Server 디바이스 추가: Jetson Orin NX 16GB (`jetson_orin_nx`)
3. 디바이스는 실행 시 CLI 인자로 선택 (예: `python main.py --edge --device raspberry_pi_4b`)
4. TTS를 Server → Edge로 이관. Server는 텍스트 응답, Edge가 TTS 실행
5. 로컬 인텐트 분석을 정규식 → 로컬 LLM(Gemma4:e2B or Gemma4:e4B, Ollama)으로 교체
6. Cloud LLM 스트리밍 응답을 문장 단위 텍스트로 Edge에 전송
7. Edge는 수신 텍스트를 TTS 변환 후 재생 (파이프라인, 끊김 없음)
8. 대화 세션 종료 인텐트: Server가 `end_session` 신호(TextResponse.type) + 안내 텍스트 전송 → Edge가 TTS 재생
9. **타임아웃 메시지는 Edge 로컬 처리**: `TimeoutPrompt` RPC 제거. Edge가 로컬 텍스트를 직접 TTS 재생하여 지연 최소화 및 Server 단절 시에도 정상 동작

완료된 문서 수정:
- [x] `CLAUDE.md` (루트)
- [x] `server/CLAUDE.md`
- [x] `edge/CLAUDE.md`
- [x] `proto/voice_service.proto` — VoiceResponse에 TextResponse 추가, TTS 오디오 제거

---

## Phase 1: 기반 — 디바이스 프로파일 + Config + Proto

### 1-1. `shared/device_profiles.py` (신규)
Edge/Server 디바이스별 설정 프로파일 정의.
디바이스 추가 시 이 파일만 수정.

```python
# 구조 예시
DEVICE_PROFILES = {
    "jetson_nano": {
        "role": "edge",
        "memory_mb": 4096,
        "has_gpu": True,
        "audio_device": "ReSpeaker XVF3800",
        ...
    },
    "raspberry_pi_4b": {
        "role": "edge",
        "memory_mb": 8192,
        "has_gpu": False,
        "audio_device": "Poly Sync 20",
        ...
    },
    "jetson_orin_nano": {
        "role": "server",
        "memory_mb": 8192,
        "has_gpu": True,
        "gpu_load_path": "/sys/devices/platform/bus@0/17000000.gpu/load",
        "power_mode": "MAXN",
        "local_llm_model": "gemma4:e2b",  # E2B Q4_K_M, llama.cpp(Nvidia AI Lab)
        ...
    },
    "jetson_orin_nx": {
        "role": "server",
        "memory_mb": 16384,
        "has_gpu": True,
        "gpu_load_path": "/sys/devices/platform/bus@0/17000000.gpu/load",
        "power_mode": "MAXN",
        "local_llm_model": "gemma4:e4b",  # E4B Q4_K_M, llama.cpp(Nvidia AI Lab)
        ...
    },
}

def get_device_profile(device_id: str) -> dict: ...
def list_devices(role: str) -> list[str]: ...
```

**핵심 포인트**: 서버 디바이스별로 다른 로컬 LLM 모델을 사용한다.
- `jetson_orin_nano` (8GB): `gemma4:e2b` — 메모리 제약 고려
- `jetson_orin_nx` (16GB): `gemma4:e4b` — 여유 메모리 활용한 더 큰 모델로 인텐트 정확도 향상
- `local_llm_model` 필드가 `config/server.yaml`의 `local_intent_llm.model` 기본값을 오버라이드한다.

### 1-2. `shared/config.py` 수정
`get_config(role, device)` — device 인자를 추가로 받아 디바이스 프로파일을 병합.

변경 포인트:
- `get_config(role: str = "server", device: str | None = None) -> dict`
- device가 주어지면 `device_profiles.get_device_profile(device)`를 config에 딥 머지
- `_config_cache`를 `(role, device)` 키로 관리 (device별 캐시 분리)

### 1-3. `config/server.yaml` 수정
- `tts` 섹션 제거 (TTS는 Edge로 이관)
- `timeout_messages` 섹션 제거 (Edge 로컬 처리로 이관)
- `local_intent_llm` 섹션 추가:
  ```yaml
  local_intent_llm:
    model: "gemma4:e2b"          # 기본값 — 디바이스 프로파일에서 오버라이드됨
    base_url: "http://localhost:11434"
    timeout_s: 5
  ```
- `local_intents` 섹션 유지 (인텐트 이름/응답 텍스트 정의용)
- `cloud.models` 섹션 단순화:
  ```yaml
  cloud:
    provider: "claude"
    model: "claude-haiku-4-5-20251001"  # 단일 모델 (티어링 제거)
    max_tokens: 500
    ...
  ```

### 1-4. `config/edge.yaml` (신규)
Edge 전용 설정:
```yaml
tts:
  voice_style: "F1"
  inference_steps: 5

audio:
  sample_rate: 16000
  encoding: "pcm_s16le"
  chunk_size: 4096

wakeword:
  model: "hey_mack"
  threshold: 0.35

vad:
  threshold: 0.5
  min_silence_ms: 500

timeout:
  listen_s: 15
  confirm_s: 5

# 타임아웃 안내 메시지 — Server 호출 없이 Edge에서 직접 TTS 재생
timeout_messages:
  prompt:   # 15초 무발화 시 의사확인
    ko: "계속 대화하시겠어요?"
    en: "Would you like to continue?"
  end:      # 5초 추가 무발화 시 종료 안내
    ko: "대화를 종료합니다. 언제든 다시 불러 주세요"
    en: "End of conversation. Call me anytime"
```

### 1-5. `proto/voice_service.proto` 추가 수정 — `TimeoutPrompt` RPC 제거

타임아웃 메시지는 Edge가 로컬에서 직접 처리하므로 관련 RPC/메시지 모두 제거.

**제거 대상**:
- `rpc TimeoutPrompt (TimeoutRequest) returns (stream VoiceResponse);`
- `message TimeoutRequest { ... }`
- `enum TimeoutType { TIMEOUT_PROMPT = 0; TIMEOUT_END = 1; }`
- `TextResponse.type` 주석에서 `"timeout"` 항목 제거 (Edge 전용이므로 proto 정의에서 불필요)

**제거 사유**:
- `TimeoutPrompt` RPC는 정적 문자열 룩업만 수행 → 네트워크 왕복 낭비
- Edge가 타임아웃 타이머를 관리하므로 메시지도 Edge에 두는 것이 책임 분리 측면에서 자연스러움
- Server 연결 불안정(`DEGRADED` 상태) 시에도 타임아웃 안내 정상 동작
- proto 단순화로 Edge↔Server 통신 계약 축소

### 1-6. Proto stub 재생성
위 수정 반영 후 pb2 파일 재생성:
```bash
cd /home/pi/workspace/mint
python -m grpc_tools.protoc \
  -I proto \
  --python_out=proto \
  --grpc_python_out=proto \
  proto/voice_service.proto
```

---

## Phase 2: Server 코드 수정

### 2-1. `server/intent/intent_analyzer.py` (신규)
로컬 LLM(Gemma4) 기반 인텐트 분석 모듈. 구현은 langchain-ollama 사용.

역할: STT 텍스트를 받아 로컬 LLM에 보내고, 어떤 인텐트인지 판단 및 라우팅.
- 인텐트 없음 → `None` 반환 → Cloud LLM 호출
- 인텐트 있음 → 인텐트 이름 반환 → 핸들러 실행

시스템 프롬프트 설계:
- JSON으로 응답하도록 지시 (`{"intent": "end_session"}` 또는 `{"intent": null}`)
- 현재 지원 인텐트: `end_session`
- 프롬프트는 한국어/영어 모두 처리하도록 이중 예시 포함

```python
class IntentAnalyzer:
    def __init__(self, model: str, base_url: str, timeout_s: int):
        # 모델명은 config(디바이스 프로파일에서 오버라이드)에서 주입
        # jetson_orin_nano → "gemma4:e2b"
        # jetson_orin_nx   → "gemma4:e4b"
        ...

    async def analyze(self, text: str, language: str) -> str | None:
        # 로컬 LLM 호출 (ChatOllama.ainvoke)
        # JSON 응답 파싱 → intent 이름 추출
        # 반환: "end_session" | None
```

**주의**: 모델명은 하드코딩하지 않고 config에서 주입받는다. 디바이스에 따라 `gemma4:e2b` 또는 `gemma4:e4b`가 선택된다.

### 2-2. `server/intent/intent_handlers.py` (신규)
인텐트별 처리 핸들러.
새 인텐트 추가 시 이 파일에 함수 추가만 하면 됨.

```python
# 인텐트 이름 → 결과 텍스트 + 세션 처리 지시
@dataclass
class IntentResult:
    text: str                    # Edge에 전송할 응답 텍스트
    signal: str                  # TextResponse.type 값 ("end_session" 등)
    save_and_clear_session: bool # True면 히스토리 저장 후 초기화

async def handle_end_session(language: str) -> IntentResult:
    return IntentResult(
        text="대화를 종료합니다. 언제든 다시 불러 주세요" if language == "ko"
             else "End of conversation. Call me anytime",
        signal="end_session",
        save_and_clear_session=True,  # 히스토리 저장 후 초기화
    )

INTENT_HANDLERS = {
    "end_session": handle_end_session,
}
```

**변경 포인트**: 기존 `clear_session`을 `save_and_clear_session`으로 변경.
대화 종료 시 먼저 `SessionRepository.save()`를 호출하여 이력을 영구 저장한 후 `CloudLLMClient.clear_history()`를 호출한다.
저장 모듈은 Phase 5에서 구현한다.

### 2-3. `server/cloud/sentence_splitter.py` (신규)
LLM 스트리밍 토큰 → 문장 단위 분리.
기존 `server/tts/supertonic_service.py`의 `_split_sentences()` 로직을 독립 모듈로 이관.

```python
class SentenceSplitter:
    def split(self, buffer: str) -> list[str]:
        # 문장 종결 부호(. ? ! 등) 기준 분리
        # 쉼표 + 10자 이상 시 추가 분리
        # 반환: [완성문장, ..., 미완성버퍼]

    async def stream_sentences(
        self, token_stream: AsyncIterator[str]
    ) -> AsyncIterator[str]:
        # 토큰 누적 → 문장 완성 시 yield
```

### 2-4. `server/orchestrator.py` 수정
- `SupertonicTTSService` 의존성 제거
- `select_model_tier()` 제거 — 모델 티어링 기능 삭제, Cloud LLM은 단일 모델만 사용
- `IntentAnalyzer` 추가 (기존 정규식 `check_local_intent` 교체)
- `startup()`: TTS 로드 제거, IntentAnalyzer 초기화 추가
- `check_local_intent()` 제거 → `analyze_intent()` (비동기, LLM 기반)
- `get_tts_service()` 제거

### 2-5. `server/grpc_server.py` 수정
핵심 변경: TTS 오디오 스트림 → 텍스트 스트림 전송. 모델 티어링 호출 제거.

ProcessVoice 변경:
```python
# 기존: select_model_tier() 호출 후 TTS 오디오 청크 yield
# 변경: 단일 모델로 Cloud LLM 호출 후 텍스트 문장 yield
yield pb2.VoiceResponse(
    text_response=pb2.TextResponse(
        text=sentence,
        language=language,
        type="sentence",
        is_final=False,
    )
)
```

- `select_model_tier()` 호출 제거
- `_tts_to_stream()` 헬퍼 제거 → `_text_response()` 헬퍼로 교체
- `TimeoutPrompt` 핸들러 **완전 제거** (RPC 자체가 proto에서 삭제됨)
- `EndSession`: TTS 오디오 대신 텍스트 전송. 추가로 `SessionRepository.save()` + `CloudLLMClient.clear_history()` 호출 (Phase 4 완료 후 연결)

### 2-6. `server/main.py` 수정
CLI 인자 추가:
```python
parser.add_argument("--server", action="store_true")
parser.add_argument("--device", type=str, default="jetson_orin_nano",
                    choices=["jetson_orin_nano", "jetson_orin_nx"])
```
`get_config("server", device=args.device)` 호출.

### 2-7. `shared/models.py` 수정
- `TextResponseData` 데이터클래스 추가 (to_proto / from_proto)
- `TTSRequestData` 제거 (Server에서 TTS 미사용)
- `VoiceResponse` 변환 메서드에 `text_response` 분기 추가

### 2-8. `server/cloud/prompt_templates.py` 수정
시스템 프롬프트를 새 설계 원칙에 맞게 조정.

**제거할 규칙**:
- "응답의 첫 문장은 '네,', '글쎄요,', '좋은 질문이에요,' 같은 짧은 호응으로 시작합니다." (한국어)
- "Start your response with a short acknowledgment like 'Sure,', 'Well,', ..." (영어)

**유지/강화할 규칙**:
- 구어체 발화 (격식체보다 자연스러운 일상 대화체)
- 한 문장 30자 이내 (ko) / 15 words 이내 (en)
- 전체 3~5문장
- 문장 종결 부호(`. ? !`) 및 쉼표(`,`) 만 허용 → `sentence_splitter`의 분리 기준으로 필요
- 마크다운(`**`, `##`, `` ` ``), 이모지, 특수 기호(`※`, `→`, `★`, `•` 등), 괄호 주석 금지
- 숫자는 한글로 풀어 씀 (ko: "3개" → "세 개")

**추가 고려**:
- "문장 부호 금지"는 문자 그대로가 아니라 "TTS 발음 불가능한 특수 문자"로 해석.
  일반 마침표/쉼표/물음표/느낌표는 `sentence_splitter`가 문장 경계 감지에 사용하므로 유지 필수.
- 기존 주석(규칙별 의도 설명)도 "첫 문장 단축" 관련 부분은 제거하고 "구어체 유지" 이유로 교체.

### 2-9. Barge-in 처리 (Edge 끼어들기 대응)

Edge가 TTS 재생 중 사용자 발화를 감지하면 `ProcessVoice` gRPC 스트림을 취소한다.
Server는 이 취소를 barge-in 신호로 해석하여 응답 생성을 즉시 중단해야 한다.

**설계 원칙**: 새 RPC나 proto 메시지 추가 없이 기존 gRPC 취소 메커니즘만 활용 → 단순성 유지.

**수정 대상 파일 2개**:

1. **`server/cloud/llm_client.py` — `CloudLLMClient.get_response_stream()`**
   - `try/finally` 블록으로 감싸서 취소(`GeneratorExit` / `CancelledError`) 발생 시 정리 로직 보장.
   - 누적된 부분 응답(`full_response`)을 `AIMessage`로 `conversation_history`에 저장.
   - Cloud astream에 대한 별도 cancel 호출 불필요 — Python async generator GC가 자동으로 정리.

   ```python
   async def get_response_stream(self, user_text: str, language: str):
       full_response = ""
       self.conversation_history.append(HumanMessage(content=user_text))
       try:
           async for chunk in self.llm.astream(messages):
               token = chunk.content or ""
               full_response += token
               yield token
       finally:
           # 정상 완료 / barge-in 취소 모두 여기로 진입
           # 부분 응답도 히스토리에 저장하여 다음 턴에 맥락 유지
           if full_response:
               self.conversation_history.append(AIMessage(content=full_response))
   ```

2. **`server/grpc_server.py` — `ProcessVoice()` 핸들러**
   - `async for` 루프 밖에서 `CancelledError`를 잡아 정상 종료 처리.
   - STT/인텐트 분석 단계는 barge-in 이전에 이미 완료되므로 별도 정리 자원 없음.
   - 취소 로그만 남기고 다음 RPC 호출을 대기.

   ```python
   async def ProcessVoice(self, request_iterator, context):
       try:
           async for response in self._process(request_iterator, context):
               yield response
       except asyncio.CancelledError:
           logger.info("ProcessVoice cancelled by client (barge-in)")
           raise  # gRPC에 취소 전파
   ```

**검증 포인트**:
- barge-in 발생 후 `conversation_history`에 부분 응답이 `AIMessage`로 남아 있는지 확인
- 다음 `ProcessVoice` 호출에서 이전 부분 응답이 맥락으로 전달되는지 확인
- Edge가 취소한 후 Server가 추가 청크를 전송하지 않는지 확인 (stale response 방지)

**설계 의도 요약**:
- gRPC 취소 = barge-in 신호 (별도 시그널 불필요)
- 부분 응답 보존으로 자연스러운 대화 흐름 유지 ("아까 말하던 중 끊겼구나"를 LLM이 인지)
- 단일 진입점(`try/finally`)으로 복잡도 최소화

---

## Phase 3: Edge 코드 수정

> 현재 edge/ 디렉토리에는 CLAUDE.md만 있음. edge 코드 전체를 신규 생성.

### 3-1. `edge/tts/supertonic_service.py` (신규 또는 server/tts에서 이관)
Supertonic TTS를 Edge에서 실행.
- `synthesize(text, language) -> bytes` (PCM)
- 디바이스 프로파일에서 inference_steps 등 설정 읽기

### 3-2. `edge/tts/tts_pipeline.py` (신규)
텍스트 수신 → TTS 변환 → 재생 파이프라인.
asyncio.Queue 기반으로 수신/변환/재생을 분리하여 끊김 없이 동작.

```python
class TTSPipeline:
    async def add_sentence(self, text: str, language: str): ...
    async def run(self): ...  # 큐에서 문장 꺼내 TTS → 재생
    async def stop(self): ...
```

### 3-3. `edge/orchestrator.py` — 타임아웃 로컬 처리

`LISTENING` 상태에서 무발화 타이머(15초) 만료 시:
1. `config/edge.yaml`의 `timeout_messages.prompt[language]` 텍스트 읽기
2. `TTSPipeline`에 직접 enqueue → Edge TTS 재생 (Server 호출 없음)
3. 5초 추가 무발화 타이머 시작
4. 5초 내 발화 감지 → `LISTENING` 복귀, 타이머 리셋
5. 5초 경과 → `timeout_messages.end[language]` 텍스트 TTS 재생 → `EndSession` RPC 호출 → `IDLE` 전환

**주의**: 기존 `grpc_client.TimeoutPrompt()` 호출 코드는 작성하지 않는다 (proto에서 제거됨).
언어는 마지막 STT 결과의 `language` 값을 사용하며, 없으면 기본 `"ko"`.

### 3-4. `edge/main.py` (신규)
CLI 인자:
```bash
python main.py --edge --device raspberry_pi_4b
```

```python
parser.add_argument("--edge", action="store_true")
parser.add_argument("--device", type=str, default="jetson_nano",
                    choices=["jetson_nano", "raspberry_pi_4b"])
```

---

## Phase 4: 대화 이력 저장

> `server/CLAUDE.md`의 "대화 종료 인텐트 시 Edge에 종료 텍스트 전송 후 대화 히스토리 **저장 및 초기화**" 요구사항 반영.
> 기존 develop_plan.md Phase 6의 storage 설계를 당겨서 구현한다.

### 4-1. `server/storage/models.py` (신규)
```python
@dataclass
class SessionRecord:
    id: str              # UUID
    history_json: str    # HumanMessage/AIMessage 직렬화
    summary: str | None  # 히스토리 요약 (있는 경우)
    language: str        # 주 사용 언어
    turn_count: int      # 총 대화 턴 수
    created_at: datetime # 세션 시작 시각
    ended_at: datetime   # 세션 종료 시각
```

### 4-2. `server/storage/session_repository.py` (신규)
SQLite 기반 간단한 저장소. SQLAlchemy는 과도하므로 표준 `sqlite3` 모듈 사용 (단순성 우선).

```python
class SessionRepository:
    def __init__(self, db_path: str = "data/sessions.db"):
        # 테이블 없으면 생성
        ...

    async def save(self, record: SessionRecord) -> None:
        # INSERT
        ...

    async def get(self, session_id: str) -> SessionRecord | None: ...
    async def get_recent(self, limit: int = 10) -> list[SessionRecord]: ...
```

### 4-3. `server/cloud/llm_client.py` 수정
`save_and_clear_history()` 메서드 추가:
```python
async def save_and_clear_history(
    self, repository: SessionRepository, language: str
) -> None:
    # 현재 conversation_history를 SessionRecord로 직렬화
    # repository.save() 호출
    # clear_history() 호출
    ...
```

### 4-4. `server/orchestrator.py` 수정
`SessionRepository` 인스턴스를 `startup()`에서 생성.
인텐트 핸들러 실행 결과에 `save_and_clear_session=True`면 위 메서드 호출.

### 4-5. `config/server.yaml` 수정
```yaml
storage:
  db_path: "data/sessions.db"
```

---

## Phase 5: 정리

### 5-1. `server/tts/` 제거
TTS가 Edge로 이관되었으므로 Server의 TTS 코드 삭제:
- `server/tts/supertonic_service.py`
- `server/tts/__init__.py`

### 5-2. `server/develop_plan.md` 업데이트
새 아키텍처 반영하여 Phase 계획 업데이트:
- Phase 2에서 TTS 제거
- Phase 4(기존 orchestrator/grpc)에서 모델 티어링 제거
- Phase 6(storage)을 Phase 4로 전진
- 로컬 인텐트 LLM 기반으로 변경 명시

---

## 진행 순서 (권장)

```
Phase 1-1 → 1-2 → 1-3 → 1-4 → 1-5 → 1-6                      (기반 — 디바이스 프로파일/config/proto)
Phase 2-1 → 2-2 → 2-3 → 2-4 → 2-5 → 2-6 → 2-7 → 2-8 → 2-9    (Server 코드)
Phase 3-1 → 3-2 → 3-3 → 3-4                                   (Edge 코드)
Phase 4-1 → 4-2 → 4-3 → 4-4 → 4-5                             (대화 이력 저장)
Phase 5-1 → 5-2                                               (정리)
```

각 단계 완료 후 동작 확인 후 다음 단계 진행 권장.
- Phase 2-5 (grpc_server.py) 이후 서버 단독 실행 테스트 가능.
- Phase 3 완료 이후 Edge-Server 통합 테스트 가능.
- Phase 4 (대화 이력 저장)는 Phase 2-2(intent_handlers)에서 `save_and_clear_session` 플래그를 도입했으므로,
  Phase 3보다 먼저 진행해도 무방. 단, Phase 2-2 단계에서는 저장 호출을 TODO 처리하고 Phase 4에서 실제 연결.
- Phase 5는 모든 기능 구현 완료 후 최종 정리.

## 디바이스 추가 시 워크플로우 (향후 유지보수)

새 디바이스(예: `jetson_agx_orin`)를 추가하려면:
1. `shared/device_profiles.py`의 `DEVICE_PROFILES`에 프로파일 추가
2. `main.py`의 `--device` choices에 식별자 추가 (Phase 2-6, 3-3)
3. 필요 시 디바이스 전용 `config/<device>.yaml` 추가 (선택)

위 3단계만으로 새 디바이스를 지원할 수 있어야 한다. 다른 코드 수정이 필요하다면 추상화가 부족한 것이므로 재검토한다.

# 코드 수정 계획 (fix_plan.md)

## 배경

아키텍처 변경사항:
1. Edge 디바이스 추가: Raspberry Pi 4B 8GB (`raspberry_pi_4b`)
2. Server 디바이스 추가: Jetson Orin NX 16GB (`jetson_orin_nx`)
3. 디바이스는 실행 시 CLI 인자로 선택 (예: `python main.py --edge --device raspberry_pi_4b`)
4. TTS를 Server → Edge로 이관. Server는 텍스트 응답, Edge가 TTS 실행
5. 로컬 인텐트 분석을 정규식 → 로컬 LLM(Gemma4:e2B or Gemma4:e4B, Ollama)으로 교체
6. **이중 채널 출력**: Cloud LLM이 단일 API 호출에서 두 섹션(`[SPOKEN]` + `[DISPLAY]`)을 순차 생성
   - `[SPOKEN]` 섹션: 구어체 3~5문장 요약 → Edge로 전송 → TTS 재생
   - `[DISPLAY]` 섹션: 완전한 상세 응답 (마크다운 포함) → FastAPI WebSocket → 브라우저 대시보드
   - TTS 첫 문장 지연 증가 없음 (SPOKEN이 먼저 생성됨)
7. Edge는 수신 텍스트를 TTS 변환 후 재생 (파이프라인, 끊김 없음)
8. 대화 세션 종료 인텐트: Server가 `end_session` 신호(TextResponse.type) + 안내 텍스트 전송 → Edge가 TTS 재생
9. **타임아웃 메시지는 Edge 로컬 처리**: `TimeoutPrompt` RPC 제거. Edge가 로컬 텍스트를 직접 TTS 재생하여 지연 최소화 및 Server 단절 시에도 정상 동작
10. **FastAPI 대시보드**: gRPC(50051)와 별도 포트(8080)로 운영. WebSocket으로 DISPLAY 섹션 실시간 스트리밍 + 마크다운 렌더링. proto 변경 없음.

완료된 문서 수정:
- [x] `CLAUDE.md` (루트) — 이중 채널 출력 흐름 반영
- [x] `server/CLAUDE.md` — 이중 섹션 파이프라인, dual_stream_parser, dashboard 언급
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
- `cloud` 섹션 단순화 + **max_tokens 상향** (이중 섹션 출력 대응):
  ```yaml
  cloud:
    provider: "claude"
    claude_model: "claude-haiku-4-5-20251001"
    gpt_model: "gpt-5-mini"
    max_tokens: 1600   # SPOKEN(~200) + DISPLAY(~800) 동시 생성 → 기존 800에서 상향
    summarize_threshold_tokens: 8000
    timeout_s: 10
  ```
- **`dashboard` 섹션 신규 추가** (이중 채널 출력을 위한 FastAPI 서버):
  ```yaml
  dashboard:
    enabled: true
    host: "0.0.0.0"   # 모든 인터페이스 (LAN 접근 허용)
    port: 8080        # 브라우저 접속: http://<server_ip>:8080
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

**대시보드 관련 proto 변경 없음**: 대시보드는 별도 HTTP/WebSocket 포트로 운영되므로
Edge↔Server gRPC 프로토콜은 그대로 유지한다.

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
- JSON으로 응답하도록 지시
- 지원 인텐트: `end_session`, `pause_listening` (+ `duration_s` 선택 필드), `list_sessions`
- 미감지 시: `{"intent": "none"}` → Cloud LLM 호출
- 프롬프트는 한국어/영어 모두 처리하도록 이중 예시 포함

응답 포맷 예시:
```json
{"intent": "end_session"}
{"intent": "pause_listening", "duration_s": 300}
{"intent": "pause_listening"}              // duration 미지정 → default 5분
{"intent": "list_sessions"}
{"intent": "none"}
```

```python
class IntentAnalyzer:
    def __init__(self, model: str, base_url: str, timeout_s: int):
        # 모델명은 config(디바이스 프로파일에서 오버라이드)에서 주입
        # jetson_orin_nano → "gemma4:e2b"
        # jetson_orin_nx   → "gemma4:e4b"
        ...

    async def analyze(self, text: str, language: str) -> dict | None:
        # 로컬 LLM 호출 (ChatOllama.ainvoke)
        # JSON 응답 파싱 → intent dict 추출
        # 반환: {"intent": "end_session"} | {"intent": "pause_listening", "duration_s": 300} | None
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

async def handle_end_session(language: str, **kwargs) -> IntentResult:
    return IntentResult(
        text="대화를 종료합니다. 언제든 다시 불러 주세요" if language == "ko"
             else "End of conversation. Call me anytime",
        signal="end_session",
        save_and_clear_session=True,
    )

async def handle_pause_listening(language: str, duration_s: int = 300, **kwargs) -> IntentResult:
    duration_min = duration_s // 60
    return IntentResult(
        text=f"네, {duration_min}분간 대기할게요. 부르시면 다시 들을게요." if language == "ko"
             else f"Okay, I'll pause for {duration_min} minutes. Call me when you're ready.",
        signal="pause_listening",
        save_and_clear_session=False,
    )

# list_sessions 핸들러는 orchestrator에서 DB 조회 후 직접 처리 (Phase 5-4 참조)

INTENT_HANDLERS = {
    "end_session": handle_end_session,
    "pause_listening": handle_pause_listening,
}
```

**변경 포인트**: 기존 `clear_session`을 `save_and_clear_session`으로 변경.
대화 종료 시 먼저 `SessionRepository.save()`를 호출하여 이력을 영구 저장한 후 `CloudLLMClient.clear_history()`를 호출한다.
저장 모듈은 Phase 5에서 구현한다.

### 2-3. `server/cloud/sentence_splitter.py` (신규)
LLM 스트리밍 토큰 → 문장 단위 분리. SPOKEN 채널 전용.
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

**적용 범위**: SPOKEN 섹션에서만 사용. DISPLAY 섹션은 마크다운/표/코드가 포함되어 있어 문장 단위 분리가 무의미하므로, 토큰 단위로 WebSocket에 그대로 브로드캐스트한다.

### 2-4. `server/cloud/dual_stream_parser.py` (신규) ★ 이중 채널 핵심

Cloud LLM 스트리밍 토큰에서 `[SPOKEN]...[/SPOKEN]` / `[DISPLAY]...[/DISPLAY]` 섹션을 분리하는 파서.

```python
from typing import AsyncIterator, Literal

Channel = Literal["tts", "display"]

class DualStreamParser:
    """
    스트리밍 토큰에서 두 섹션을 분리하여 (channel, token) 튜플로 yield.

    상태:
        OUTSIDE    — 섹션 밖 (섹션 태그 탐색 중)
        IN_SPOKEN  — [SPOKEN] ... [/SPOKEN] 사이
        IN_DISPLAY — [DISPLAY] ... [/DISPLAY] 사이

    주의:
        - 태그 경계가 토큰에 걸칠 수 있음 (예: "[SPO" + "KEN]")
          → 내부 버퍼에 누적하고 완성된 태그를 찾으면 상태 전이
        - 태그 자체는 yield하지 않음
        - 섹션 밖의 토큰(공백, 줄바꿈 등)은 버림
    """

    def __init__(self):
        self.state: Literal["OUTSIDE", "IN_SPOKEN", "IN_DISPLAY"] = "OUTSIDE"
        self.buffer: str = ""  # 태그 경계 대응용

    async def parse(
        self, token_stream: AsyncIterator[str]
    ) -> AsyncIterator[tuple[Channel, str]]:
        """
        토큰 스트림을 받아 (channel, content) 튜플을 yield.

        channel:
            "tts"     — SPOKEN 섹션 내용 → SentenceSplitter → TTS → Edge
            "display" — DISPLAY 섹션 내용 → WebSocket → 브라우저
        """
        async for token in token_stream:
            self.buffer += token
            # 태그 탐색 루프: 버퍼에서 최대한 많은 태그/컨텐츠 추출
            while True:
                consumed = self._try_consume()
                if consumed is None:
                    break  # 더 많은 토큰이 필요
                channel, content = consumed
                if content:  # 빈 청크는 skip
                    yield channel, content

    def _try_consume(self) -> tuple[Channel, str] | None:
        """
        현재 상태에 따라 버퍼에서 컨텐츠를 추출.

        OUTSIDE:
            [SPOKEN] 또는 [DISPLAY] 태그를 찾음 → 상태 전이 → 버퍼에서 태그 제거
            미발견 시 None (더 많은 토큰 필요)

        IN_SPOKEN:
            [/SPOKEN] 태그 전까지의 모든 토큰을 ("tts", content)로 반환
            [/SPOKEN] 발견 시 상태를 OUTSIDE로 전이

        IN_DISPLAY:
            [/DISPLAY] 태그 전까지의 모든 토큰을 ("display", content)로 반환
            [/DISPLAY] 발견 시 상태를 OUTSIDE로 전이
        """
        ...
```

**테스트 케이스** (`tests/test_dual_stream_parser.py`):
- 정상: `[SPOKEN]안녕[/SPOKEN][DISPLAY]안녕하세요[/DISPLAY]` → `[("tts", "안녕"), ("display", "안녕하세요")]`
- 토큰 경계 분리: `["[SPOKEN]안", "녕[/SP", "OKEN]"]` → `[("tts", "안녕")]`
- 빈 섹션: `[SPOKEN][/SPOKEN][DISPLAY]x[/DISPLAY]` → `[("display", "x")]`
- 순서 보장: SPOKEN이 먼저 완료되어야 DISPLAY 시작됨

### 2-5. `server/cloud/prompt_templates.py` 수정 — 이중 섹션 프롬프트

시스템 프롬프트를 **이중 섹션 출력**을 유도하도록 재작성.

**새 프롬프트 구조** (한국어):
```
당신은 한국어 AI 음성 비서입니다.

반드시 아래 두 섹션 형식으로 응답하세요. 다른 텍스트(설명, 머리말)는 포함하지 마세요.

[SPOKEN]
여기에는 음성 재생용 요약을 작성합니다.
- 구어체 (일상 대화체).
- 한 문장은 30자 이내.
- 전체 3~5문장.
- 마크다운, 이모지, 특수 기호, 괄호 금지.
- 숫자는 한글로 풀어 씁니다. (예: "3개" → "세 개")
- 영어 단어는 한국어 발음으로 표기합니다. (예: "AI" → "에이아이")
[/SPOKEN]

[DISPLAY]
여기에는 브라우저 화면 표시용 상세 응답을 작성합니다.
- 길이 제한 없음.
- 마크다운(제목, 목록, 표, 코드 블럭) 자유롭게 사용 가능.
- 영어 약어, 고유명사, 숫자 그대로 표기 가능.
- 이모지 사용 가능.
[/DISPLAY]
```

**영어 프롬프트**도 동일 구조로 작성:
- SPOKEN: `Each sentence under 15 words`, `3-5 sentences total`, `No markdown/emoji`, `Spell out numbers when natural`
- DISPLAY: `No length limit`, `Markdown allowed`, `Technical formatting allowed`

**제거할 규칙** (기존 단일 섹션 프롬프트에서):
- 첫 문장 호응 강제 ("네,", "글쎄요," 등 — 선택 사항으로 완화)
- 전체 응답 길이 제한 (DISPLAY는 자유롭게)

**유지할 규칙** (SPOKEN에만 적용):
- 30자 / 15 words 이내 문장
- 3~5 문장 총량
- 마크다운/이모지 금지
- 숫자 한글 표기

**주석/의도 설명**:
- 이중 섹션의 이유: GUI 풍부한 응답 + TTS 구어체 요약 동시 제공
- 토큰 오버헤드 ~30% (SPOKEN + DISPLAY) 감수하는 이유: 단일 API 호출 + 지연 없음
- SPOKEN이 먼저 생성되는 이유: LLM 순차 생성 특성상 TTS 재생이 즉시 시작되도록 배치

### 2-6. `server/cloud/llm_client.py` 수정 — 이중 채널 스트림

`get_response_stream()`을 **이중 채널 출력**으로 변경.

**변경 포인트**:
1. 반환 타입: `AsyncIterator[str]` → `AsyncIterator[tuple[Channel, str]]`
2. 내부에서 `DualStreamParser`를 통해 토큰을 `(channel, content)` 튜플로 분리
3. 히스토리 저장: DISPLAY 섹션 내용만 `AIMessage`로 저장 (맥락 품질 우선)
4. try/finally에서 누적된 DISPLAY 부분 응답 저장 (정상 완료 시)

```python
async def get_response_stream(
    self, user_text: str, language: str
) -> AsyncIterator[tuple[Channel, str]]:
    messages = self._build_messages(user_text, language)
    self.conversation_history.append(HumanMessage(content=user_text))

    display_buffer = ""  # 히스토리 저장용 (DISPLAY 섹션만)
    parser = DualStreamParser()

    async def raw_token_stream():
        async for chunk in self.llm.astream(messages):
            token = chunk.content or ""
            yield token

    try:
        async for channel, content in parser.parse(raw_token_stream()):
            if channel == "display":
                display_buffer += content
            yield channel, content
    finally:
        # 정상 완료 시 여기로 진입
        if display_buffer:
            self.conversation_history.append(AIMessage(content=display_buffer))
```

**주의**:
- SPOKEN 섹션은 히스토리에 저장하지 않는다 (요약본이므로 다음 턴 맥락에 부적합)
- DISPLAY 섹션만 저장하여 Cloud LLM이 이전 대화 맥락을 풍부하게 유지

### 2-7. `server/orchestrator.py` 수정
- `SupertonicTTSService` 의존성 제거
- `select_model_tier()` 제거 — 모델 티어링 기능 삭제, Cloud LLM은 단일 모델만 사용
- `IntentAnalyzer` 추가 (기존 정규식 `check_local_intent` 교체)
- `startup()`: TTS 로드 제거, IntentAnalyzer 초기화, **대시보드 브로드캐스터 참조 보관**
- `check_local_intent()` 제거 → `analyze_intent()` (비동기, LLM 기반)
- `get_tts_service()` 제거

### 2-8. `server/grpc_server.py` 수정 — 이중 채널 라우팅

핵심 변경: Cloud LLM 응답을 **채널별로 분기 처리**.
- `("tts", sentence)` → Edge에 `TextResponse` gRPC 전송 (기존 문장 단위 스트리밍)
- `("display", token)` → FastAPI WebSocket 브로드캐스트 (대시보드 GUI)

```python
# ProcessVoice 내 Cloud LLM 호출 부분
from server.cloud.sentence_splitter import SentenceSplitter
from server.dashboard.app import broadcast_display_token, broadcast_session_event

async def _run_cloud_llm(self, user_text, language, context):
    # STT 결과도 대시보드에 전송 (사용자 말풍선용)
    await broadcast_session_event({
        "type": "user_message",
        "text": user_text,
        "language": language,
    })

    # SPOKEN 채널 문장 단위 분리용 SentenceSplitter
    splitter = SentenceSplitter()
    spoken_buffer = ""

    async for channel, content in self.orchestrator.cloud_client.get_response_stream(
        user_text, language
    ):
        if channel == "tts":
            # SPOKEN: 문장 단위로 분리 → Edge로 텍스트 전송
            spoken_buffer += content
            sentences = splitter.split(spoken_buffer)
            spoken_buffer = sentences.pop()  # 마지막은 미완성 버퍼
            for sentence in sentences:
                yield pb2.VoiceResponse(
                    text_response=pb2.TextResponse(
                        text=sentence,
                        language=language,
                        type="sentence",
                        is_final=False,
                    )
                )
        elif channel == "display":
            # DISPLAY: 토큰 단위로 WebSocket에 브로드캐스트 (GUI 타이핑 효과)
            await broadcast_display_token(content)

    # 남은 SPOKEN 버퍼 flush
    if spoken_buffer.strip():
        yield pb2.VoiceResponse(
            text_response=pb2.TextResponse(
                text=spoken_buffer.strip(),
                language=language,
                type="sentence",
                is_final=True,
            )
        )

    # DISPLAY 완료 신호
    await broadcast_session_event({"type": "assistant_done"})
```

기타 변경:
- `select_model_tier()` 호출 제거
- `_tts_to_stream()` 헬퍼 제거 → `_text_response()` 헬퍼로 교체
- `TimeoutPrompt` 핸들러 **완전 제거** (RPC 자체가 proto에서 삭제됨)
- `EndSession`: TTS 오디오 대신 텍스트 전송. 추가로 `SessionRepository.save()` + `CloudLLMClient.clear_history()` 호출 (Phase 4 완료 후 연결)

### 2-9. `server/main.py` 수정 — gRPC + FastAPI 병행 실행

CLI 인자 추가 및 FastAPI/Uvicorn을 asyncio에 병행 실행.

```python
parser.add_argument("--server", action="store_true")
parser.add_argument("--device", type=str, default="jetson_orin_nano",
                    choices=["jetson_orin_nano", "jetson_orin_nx"])

async def serve():
    config = get_config("server", device=args.device)
    orchestrator = ServerOrchestrator(config)
    await orchestrator.startup()

    # gRPC 서버
    grpc_server = grpc.aio.server(...)
    pb2_grpc.add_VoiceServiceServicer_to_server(
        VoiceServiceHandler(orchestrator), grpc_server
    )
    grpc_server.add_insecure_port(f"[::]:{config['grpc']['port']}")
    await grpc_server.start()

    # FastAPI 대시보드 서버 (config.dashboard.enabled일 때만)
    dashboard_task = None
    if config.get("dashboard", {}).get("enabled", False):
        from server.dashboard.app import create_app
        import uvicorn
        app = create_app(orchestrator)
        uvicorn_config = uvicorn.Config(
            app,
            host=config["dashboard"]["host"],
            port=config["dashboard"]["port"],
            log_level="info",
        )
        dashboard_server = uvicorn.Server(uvicorn_config)
        dashboard_task = asyncio.create_task(dashboard_server.serve())

    # SIGTERM/SIGINT 대기
    try:
        await grpc_server.wait_for_termination()
    finally:
        if dashboard_task:
            dashboard_task.cancel()
        await orchestrator.shutdown()
```

### 2-10. `shared/models.py` 수정
- `TextResponseData` 데이터클래스 추가 (to_proto / from_proto)
- `TTSRequestData` 제거 (Server에서 TTS 미사용)
- `VoiceResponse` 변환 메서드에 `text_response` 분기 추가

### 2-11. [삭제됨] Barge-in 처리

> Barge-in 기능은 UX 검토 결과 제거됨 (2026-04-10).
> 응답이 짧아(SPOKEN 3~5문장) 실효 가치 대비 구현·튜닝 비용이 크고,
> SPEAKING 중 VAD 오감지 리스크가 있어 삭제 결정.
> SPEAKING 상태는 TTS 재생 완료 → LISTENING 복귀로 단순화.

---

## Phase 3: 대시보드 구현 (FastAPI WebSocket + 브라우저 UI)

> `server/dashboard/` 디렉토리를 신규 생성하여 실시간 모니터링 대시보드 구현.
> gRPC와 독립된 HTTP/WebSocket 서버로 운영.

### 3-1. `server/dashboard/__init__.py` (신규)
빈 파일 또는 주요 export.

### 3-2. `server/dashboard/app.py` (신규)
FastAPI 애플리케이션 + WebSocket 브로드캐스터.

```python
"""
FastAPI 대시보드 — 이중 채널 출력의 DISPLAY 채널 실시간 표시.

아키텍처:
    grpc_server.py (DISPLAY 토큰 수신)
        → broadcast_display_token(token)
            → WebSocket clients에게 전송
                → 브라우저 index.html이 실시간 렌더링

WebSocket 메시지 형식 (JSON):
    {"type": "user_message", "text": "...", "language": "ko"}    — STT 결과
    {"type": "display_token", "content": "..."}                    — DISPLAY 토큰 조각
    {"type": "assistant_done"}                                     — 응답 완료
    {"type": "interrupted"}                                        — 응답 중단
    {"type": "session_end"}                                        — 세션 종료
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

# 연결된 WebSocket 클라이언트 전역 집합
_clients: set[WebSocket] = set()
_clients_lock = asyncio.Lock()

async def broadcast_display_token(token: str) -> None:
    """DISPLAY 섹션 토큰 조각을 모든 WebSocket 클라이언트에 전송."""
    await _broadcast({"type": "display_token", "content": token})

async def broadcast_session_event(event: dict) -> None:
    """세션 이벤트 (user_message, assistant_done, interrupted 등) 브로드캐스트."""
    await _broadcast(event)

async def _broadcast(message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False)
    async with _clients_lock:
        dead = set()
        for ws in _clients:
            try:
                await ws.send_text(payload)
            except Exception as e:
                logger.warning(f"WebSocket send failed: {e}")
                dead.add(ws)
        _clients.difference_update(dead)

def create_app(orchestrator) -> FastAPI:
    app = FastAPI(title="Mint Voice Assistant Dashboard")

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(static_dir / "index.html"))

    @app.get("/health")
    async def health():
        return {"status": "ok", "clients": len(_clients)}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        async with _clients_lock:
            _clients.add(ws)
        logger.info(f"Dashboard client connected. Total: {len(_clients)}")
        try:
            while True:
                # 클라이언트로부터 ping 등 수신 대기 (실제로는 서버→클라 방향만 사용)
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            async with _clients_lock:
                _clients.discard(ws)
            logger.info(f"Dashboard client disconnected. Total: {len(_clients)}")

    return app
```

**설계 포인트**:
- WebSocket 클라이언트 집합은 모듈 전역으로 관리 → `grpc_server.py`에서 `broadcast_*` 함수를 import하여 호출
- `_clients_lock`으로 동시 브로드캐스트/연결 관리 보호
- 연결 끊긴 클라이언트 자동 제거 (예외 처리)

### 3-3. `server/dashboard/static/index.html` (신규)
브라우저 실시간 채팅 UI.

**핵심 기능**:
- WebSocket(`ws://<host>:8080/ws`) 연결
- 사용자 말풍선 (STT 결과): `{"type": "user_message"}` 수신 시 렌더링
- 어시스턴트 말풍선 (DISPLAY 섹션): `{"type": "display_token"}` 수신 시 타이핑 효과로 append → 완료 시 마크다운 렌더링
- `{"type": "interrupted"}` 수신 시 말풍선에 "중단됨" 표시
- 상단 상태 바: 연결 상태, 현재 세션 언어, 클라이언트 수
- 자동 스크롤 (하단 고정)

**의존성** (CDN):
- [marked.js](https://cdn.jsdelivr.net/npm/marked/marked.min.js) — 마크다운 렌더링
- [highlight.js](https://cdn.jsdelivr.net/npm/highlightjs@11/highlight.min.js) — 코드 블럭 하이라이트

**구조**:
```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Mint Voice Assistant Dashboard</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <header>
        <h1>Mint Dashboard</h1>
        <span id="status">⚪ Connecting...</span>
    </header>
    <main id="chat"></main>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="/static/app.js"></script>
</body>
</html>
```

### 3-4. `server/dashboard/static/app.js` (신규)
WebSocket 클라이언트 로직. 메시지 타입별 핸들러.

```javascript
const ws = new WebSocket(`ws://${location.host}/ws`);
const chat = document.getElementById('chat');
const status = document.getElementById('status');

let currentAssistantBubble = null;
let currentAssistantBuffer = "";

ws.onopen = () => { status.textContent = "🟢 Connected"; };
ws.onclose = () => { status.textContent = "🔴 Disconnected"; };

ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    switch (msg.type) {
        case "user_message":
            appendUserBubble(msg.text, msg.language);
            startAssistantBubble();
            break;
        case "display_token":
            appendTokenToAssistant(msg.content);
            break;
        case "assistant_done":
            finalizeAssistantBubble();
            break;
        case "interrupted":
            markAssistantInterrupted();
            break;
    }
};

function startAssistantBubble() {
    currentAssistantBuffer = "";
    currentAssistantBubble = document.createElement('div');
    currentAssistantBubble.className = 'bubble assistant';
    chat.appendChild(currentAssistantBubble);
    chat.scrollTop = chat.scrollHeight;
}

function appendTokenToAssistant(token) {
    currentAssistantBuffer += token;
    // 스트리밍 중에는 plain text로 표시 (깜빡임 방지)
    currentAssistantBubble.textContent = currentAssistantBuffer;
    chat.scrollTop = chat.scrollHeight;
}

function finalizeAssistantBubble() {
    // 스트리밍 완료 시 마크다운 렌더링
    if (currentAssistantBubble) {
        currentAssistantBubble.innerHTML = marked.parse(currentAssistantBuffer);
    }
    currentAssistantBubble = null;
}
// ... (user bubble, interrupted 처리 등)
```

### 3-5. `server/dashboard/static/style.css` (신규)
채팅 UI 스타일. 사용자/어시스턴트 말풍선 구분, 마크다운 렌더링 스타일, 다크 모드 기본.

---

## Phase 4: Edge 코드 수정

> 현재 edge/ 디렉토리에는 CLAUDE.md만 있음. edge 코드 전체를 신규 생성.

### 4-1. `edge/tts/supertonic_service.py` (신규 또는 server/tts에서 이관)
Supertonic TTS를 Edge에서 실행.
- `synthesize(text, language) -> bytes` (PCM)
- 디바이스 프로파일에서 inference_steps 등 설정 읽기

### 4-2. `edge/tts/tts_pipeline.py` (신규)
텍스트 수신 → TTS 변환 → 재생 파이프라인.
asyncio.Queue 기반으로 수신/변환/재생을 분리하여 끊김 없이 동작.

```python
class TTSPipeline:
    async def add_sentence(self, text: str, language: str): ...
    async def run(self): ...  # 큐에서 문장 꺼내 TTS → 재생
    async def stop(self): ...
```

### 4-3. `edge/orchestrator.py` — 상태 머신 + 타임아웃 + pause 처리

**상태 머신**:
`IDLE` → wake word → `LISTENING` → VAD end → `PROCESSING` → response → `SPEAKING` → done → `LISTENING`
SPEAKING 상태에서 barge-in 없음: TTS 재생 완료 시에만 LISTENING 복귀.

**타임아웃 로컬 처리**: `LISTENING` 상태에서 무발화 타이머(60초) 만료 시:
1. `config/edge.yaml`의 `timeout_messages.prompt[language]` 텍스트 읽기
2. `TTSPipeline`에 직접 enqueue → Edge TTS 재생 (Server 호출 없음)
3. 5초 추가 무발화 타이머 시작
4. 5초 내 발화 감지 → `LISTENING` 복귀, 타이머 리셋
5. 5초 경과 → `timeout_messages.end[language]` 텍스트 TTS 재생 → `EndSession` RPC 호출 → `IDLE` 전환

**PAUSED 상태**: `pause_listening` 인텐트 수신 시:
1. ack 텍스트 TTS 재생 후 PAUSED 진입
2. 오디오 gRPC 전송 OFF, VAD OFF, 무발화 타이머 OFF
3. 웨이크워드 감시만 유지 (IDLE과 동일한 루프 재사용)
4. pause 타이머 시작 (Server에서 전달한 duration_s, 없으면 `config/edge.yaml`의 `pause_default_s`)
5. 웨이크워드 감지 → chime_wake + LISTENING 진입 (세션 유지, pause 타이머 취소)
6. pause 타이머 만료 → `timeout_messages.end` TTS → `EndSession` RPC → IDLE

**주의**: 기존 `grpc_client.TimeoutPrompt()` 호출 코드는 작성하지 않는다 (proto에서 제거됨).
언어는 마지막 STT 결과의 `language` 값을 사용하며, 없으면 기본 `"ko"`.

### 4-4. `edge/main.py` (신규)
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

## Phase 5: 대화 이력 저장 및 Resume

> `server/CLAUDE.md`의 "대화 종료 인텐트 시 Edge에 종료 텍스트 전송 후 대화 히스토리 **저장 및 초기화**" 요구사항 반영.
> UX 검토(2026-04-10): 세션 Resume 기능 추가 — 종료된 대화를 DB에서 불러와 이전 맥락으로 이어감.

### 5-1. `server/storage/models.py` (신규)
```python
@dataclass
class SessionRecord:
    id: str              # UUID
    subject: str         # Gemma가 생성한 한 줄 요약 (세션 목록 표시용)
    language: str        # 주 사용 언어
    turn_count: int      # 총 대화 턴 수
    history_json: str    # HumanMessage/AIMessage 직렬화 (DISPLAY 섹션 기준)
    created_at: datetime # 세션 시작 시각
    ended_at: datetime   # 세션 종료 시각
```

**주의**: `history_json`에 저장되는 내용은 Cloud LLM의 DISPLAY 섹션 응답이다
(Phase 2-6에서 `conversation_history`에 DISPLAY만 저장하도록 설계).

**subject 생성**: 세션 종료 시 Gemma 로컬 LLM이 첫 2~3턴을 한 줄 요약(15자 이내).
실패 시 폴백: 첫 HumanMessage 텍스트를 subject로 사용.

### 5-2. `server/storage/session_repository.py` (신규)
SQLite 기반 간단한 저장소. SQLAlchemy는 과도하므로 표준 `sqlite3` 모듈 사용 (단순성 우선).

```python
class SessionRepository:
    def __init__(self, db_path: str = "data/sessions.db"):
        # 테이블 없으면 생성 (id, subject, language, turn_count, history_json, created_at, ended_at)
        ...

    async def save(self, record: SessionRecord) -> None:
        # INSERT
        ...

    async def get(self, session_id: str) -> SessionRecord | None: ...
    async def get_recent(self, limit: int = 5) -> list[SessionRecord]: ...
```

### 5-3. `server/cloud/llm_client.py` 수정
`save_and_clear_history()` 메서드 추가:
```python
async def save_and_clear_history(
    self, repository: SessionRepository, language: str
) -> None:
    # Gemma로 subject 생성 (첫 2~3턴 요약)
    # 현재 conversation_history를 SessionRecord로 직렬화
    # repository.save() 호출
    # clear_history() 호출
    ...
```

`restore_history()` 메서드 추가 (Resume 용):
```python
def restore_history(self, history_json: str) -> None:
    # JSON → list[HumanMessage | AIMessage] 역직렬화
    # self.conversation_history = restored
    # self._history_summary = None (요약은 필요 시 재생성)
    ...
```

### 5-4. `server/orchestrator.py` 수정
`SessionRepository` 인스턴스를 `startup()`에서 생성.
인텐트 핸들러 실행 결과에 `save_and_clear_session=True`면 위 메서드 호출.

**세션 Resume 관련 추가**:
- `waiting_for_selection: bool = False` 플래그.
- `pending_sessions: list[str] = []` — 선택 가능한 session_id 목록.
- `handle_session_selection(text: str)` 메서드: 숫자 파싱 → session 로드 → `cloud_client.restore_history()`.
- `check_local_intent()` 앞에 `waiting_for_selection` 체크 분기 삽입.
  - 유효 번호 → 세션 복원 + 안내 텍스트 전송, 플래그 해제.
  - 무효/비숫자 → 플래그 해제, 일반 처리로 넘김.

---

## Phase 6: 정리

### 6-1. `server/tts/` 제거
TTS가 Edge로 이관되었으므로 Server의 TTS 코드 삭제:
- `server/tts/supertonic_service.py`
- `server/tts/__init__.py`

### 6-2. `server/develop_plan.md` 업데이트 (존재 시)
새 아키텍처 반영.

### 6-3. `tests/` 신규 테스트 추가
- `tests/test_dual_stream_parser.py` — Phase 2-4 검증
- `tests/test_prompt_templates.py` — 이중 섹션 프롬프트 유효성
- `tests/test_dashboard_broadcast.py` — WebSocket 브로드캐스트 단위 테스트

---

## 진행 순서 (권장)

```
Phase 1-1 → 1-2 → 1-3 → 1-4 → 1-5 → 1-6                              (기반)
Phase 2-1 → 2-2 → 2-3 → 2-4 → 2-5 → 2-6 → 2-7 → 2-8 → 2-9 → 2-10 → 2-11   (Server 코드 + 이중 채널)
Phase 3-1 → 3-2 → 3-3 → 3-4 → 3-5                                    (대시보드)
Phase 4-1 → 4-2 → 4-3 → 4-4                                          (Edge 코드)
Phase 5-1 → 5-2 → 5-3 → 5-4                                          (대화 이력 저장)
Phase 6-1 → 6-2 → 6-3                                                (정리)
```

**단계별 검증 포인트**:
- **Phase 1 완료 후**: `config/server.yaml`에 `dashboard` 섹션이 있고 `max_tokens: 1600`으로 상향되었는지 확인.
- **Phase 2-4 완료 후**: `DualStreamParser` 단위 테스트 통과 — 토큰 경계 케이스 포함.
- **Phase 2-5 완료 후**: 새 이중 섹션 프롬프트로 Cloud API 호출 시 실제로 `[SPOKEN]`/`[DISPLAY]` 태그가 포함된 응답이 오는지 `test_multiturn.py`로 확인.
- **Phase 2-8 완료 후**: Edge 없이 server만 실행하여 gRPC 클라이언트(예: grpcurl)로 ProcessVoice 호출 시 이중 섹션 분기가 동작하는지 확인.
- **Phase 3 완료 후**: 브라우저로 `http://localhost:8080` 접속 → WebSocket 연결 → mock 데이터로 렌더링 테스트.
- **Phase 2+3 통합 완료 후**: Edge 없이 server만 실행 → gRPC 호출 → 대시보드 GUI에서 실시간 DISPLAY 스트리밍 확인.
- **Phase 4 완료 후**: Edge-Server 통합 테스트. 사용자 발화 → STT → SPOKEN TTS 재생 + DISPLAY 브라우저 표시 동시 동작 확인.
- **Phase 5 완료 후**: 대화 종료 인텐트 시 SQLite DB에 레코드 저장 확인.

## 디바이스 추가 시 워크플로우 (향후 유지보수)

새 디바이스(예: `jetson_agx_orin`)를 추가하려면:
1. `shared/device_profiles.py`의 `DEVICE_PROFILES`에 프로파일 추가
2. `main.py`의 `--device` choices에 식별자 추가 (Phase 2-9, 4-4)
3. 필요 시 디바이스 전용 `config/<device>.yaml` 추가 (선택)

위 3단계만으로 새 디바이스를 지원할 수 있어야 한다. 다른 코드 수정이 필요하다면 추상화가 부족한 것이므로 재검토한다.

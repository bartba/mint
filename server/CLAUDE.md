# CLAUDE.md — Server (Jetson Orin Nano 8GB)

## Role

AI 추론 전용 서버. Edge로부터 오디오를 수신하여 STT 처리하고, Cloud LLM에 텍스트를 전달하여 응답을 받고, TTS로 변환하여 Edge에 오디오를 반환한다.

## Hardware context

| Item | Detail |
|------|--------|
| Board | Jetson Orin Nano 8GB, Cortex-A78AE×6 @1.5GHz (Super: 1.7GHz) |
| GPU | NVIDIA Ampere, 1024 CUDA Cores, 32 Tensor Cores |
| AI 성능 | 40 TOPS (Super 모드: 67 TOPS) |
| Memory | 8GB LPDDR5 **통합 메모리** (CPU/GPU 공유) |
| Storage | NVMe M.2 SSD (최소 256GB) |
| Network | Gigabit Ethernet |
| Power | 7W ~ 25W (Super 모드) |
| SDK | JetPack 6.x, CUDA 12.x, cuDNN, TensorRT |

**통합 메모리 주의**: CPU와 GPU가 8GB를 공유한다. GPU에 모델을 올리면 시스템 RAM이 줄어든다. OOM을 방지하려면 항상 메모리 예산을 확인할 것.

## Memory budget

| Allocation | Size | Notes |
|-----------|------|-------|
| OS + system | ~1.5GB | JetPack 6.x 기준 |
| Whisper-large-v3-turbo | ~3.0GB | faster-whisper, GPU 상시 로드 |
| Supertonic TTS | ~0.3GB | ONNX Runtime, 한국어+영어 통합 |
| gRPC + 통신 버퍼 | ~0.5GB | 오디오 스트림 버퍼 |
| **여유** | **~2.7GB** | 추가 용도 |
| **합계** | **~8.0GB** | |

**규칙**: 새 모델 추가 시 반드시 `tegrastats`로 실제 메모리 사용량을 확인한다. 여유 메모리가 500MB 미만이면 swap thrashing 위험.

## Dependencies

```txt
# requirements.txt
faster-whisper>=1.0
ctranslate2>=4.0
supertonic                    # Supertonic TTS (ONNX, 한국어+영어 다국어 통합)
onnxruntime                   # Supertonic 의존성 (GPU 가속: onnxruntime-gpu)
grpcio>=1.60
grpcio-tools>=1.60
protobuf>=4.25
anthropic>=0.40               # Claude API
openai>=1.50                  # GPT API (대체용)
pyyaml>=6.0
prometheus-client>=0.20
```

**Supertonic TTS 설치**:
```bash
pip install supertonic
# 첫 실행 시 HuggingFace에서 ONNX 모델 자동 다운로드 (~305MB)
# GPU 가속이 필요한 경우: pip install onnxruntime-gpu
```

> PyTorch 의존성이 제거됨 — Whisper(faster-whisper)는 CTranslate2 기반, TTS는 ONNX Runtime 기반으로 동작.

## Architecture

```
main.py (uvicorn + asyncio)
  │
  ├── orchestrator.py ─────── 요청 조율, Cloud LLM 중계
  │     │
  │     ├── grpc_server.py ── Edge 요청 수신 (ProcessVoice, HealthCheck)
  │     │
  │     ├── stt/
  │     │   └── whisper_service.py ── Whisper-large-v3-turbo (faster-whisper, GPU)
  │     │
  │     ├── tts/
  │     │   └── supertonic_service.py ── Supertonic TTS (ONNX, 한국어+영어)
  │     │
  │     └── cloud/
  │           ├── llm_client.py ────── Claude/GPT API 스트리밍 클라이언트
  │           └── prompt_templates.py ─ 시스템 프롬프트 관리
  │
  ├── model_manager.py ────── 모델 로드/언로드, GPU 메모리 관리
  ├── monitoring.py ───────── Prometheus 메트릭, tegrastats 연동
  │
  └── config (from config/server.yaml + config/default.yaml)
```

## Pipeline flow

Edge로부터 하나의 요청이 들어오면 Server 내부에서 3단계를 거친다:

```
Edge (gRPC)          Server                              Cloud
────────────────────────────────────────────────────────────────────
AudioChunk stream ──► [1] STT Service
                      Whisper-large-v3-turbo
                      GPU 추론 (~700ms)
                          │
                      STTResult ──► Edge (VoiceResponse, 참조/로깅용)
                          │
                      [2] 인텐트 라우터 (키워드/정규식 매칭)
                          ├── 매칭 성공 → [3] TTS로 직행 (Cloud 스킵)
                          └── 매칭 실패 ↓
                      [3] Cloud LLM Client
                      text ──────────────────────────────► Claude/GPT API
                                                           (스트리밍 응답)
                      sentence chunks ◄──────────────────
                          │
                      [4] TTS Service
                      Supertonic TTS per sentence
                      ONNX 추론 (~50ms/문장)
                          │
AudioChunk stream ◄── VoiceResponse (tts_audio)
```

**리소스 분리**: STT는 GPU(faster-whisper), TTS는 CPU/ONNX(Supertonic)로 실행된다. STT와 TTS가 서로 다른 연산 장치를 사용하므로 리소스 충돌이 없다.

## STT service (stt/whisper_service.py)

```python
from faster_whisper import WhisperModel

class STTService:
    def __init__(self, model_size: str = "large-v3", device: str = "cuda",
                 compute_type: str = "float16"):
        """
        faster-whisper + CTranslate2 기반.
        float16: Tensor Core 활용, 메모리 ~3GB.
        int8_float16: 메모리 ~1.5GB이지만 정확도 미세 하락.
        메모리가 부족하면 compute_type을 int8_float16으로 변경.
        """
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            download_root="models/"
        )
    
    async def transcribe(self, audio_data: bytes, language: str = "ko") -> STTResult:
        """
        전체 오디오를 받아 한 번에 변환.
        Edge에서 VAD로 발화 구간을 잘라서 보내므로, 여기서는 추가 VAD 불필요.
        """
        # bytes → numpy float32 변환
        audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        
        segments, info = self.model.transcribe(
            audio_np,
            language=language,
            beam_size=5,
            vad_filter=False,       # Edge VAD 사용, 서버 VAD 비활성
            word_timestamps=False
        )
        
        text = " ".join(seg.text.strip() for seg in segments)
        return STTResult(
            text=text,
            confidence=info.language_probability,
            is_final=True,
            language=info.language
        )
    
    async def transcribe_stream(self, audio_stream: AsyncIterator[bytes],
                                 language: str = "ko") -> AsyncIterator[STTResult]:
        """
        스트리밍 모드: 오디오 청크를 누적하다가 VAD end 신호 시 변환.
        중간 결과(is_final=False)는 현재 미지원 — 추후 확장 가능.
        """
        buffer = bytearray()
        async for chunk in audio_stream:
            buffer.extend(chunk)
        
        # 버퍼 누적 완료 → 일괄 변환
        result = await self.transcribe(bytes(buffer), language)
        yield result
```

**모델 변형 옵션**:

| Model | VRAM | 한국어 WER | 추론 시간 (10초 오디오) | 권장 상황 |
|-------|------|-----------|---------------------|----------|
| large-v3-turbo (FP16) | ~3GB | ~8-12% | ~500-800ms | **기본 권장** |
| large-v3-turbo (INT8) | ~1.5GB | ~9-13% | ~400-700ms | 메모리 부족 시 |
| large-v3 (FP16) | ~3GB | ~7-10% | ~1.5-2s | 최고 정확도 필요 시 |
| medium (FP16) | ~1.5GB | ~12-18% | ~300-500ms | 속도 우선 시 |

## TTS service (tts/supertonic_service.py)

```python
from supertonic import TTS
import numpy as np
import re

class SupertonicTTSService:
    def __init__(self, voice_style: str = "F1"):
        """
        Supertonic TTS: 66M 파라미터, ONNX Runtime 기반.
        한국어(ko) + 영어(en) 다국어 지원 — 단일 모델로 언어 전환.
        ONNX 모델 ~305MB, 첫 실행 시 HuggingFace에서 자동 다운로드.
        CPU RTF ~0.012 (2 inference steps), 매우 빠름.
        """
        self.model = TTS(auto_download=True)
        self.voice_style = self.model.get_voice_style(voice_style)

    async def synthesize(self, text: str, language: str = "ko",
                         speed: float = 1.0) -> bytes:
        """전체 텍스트를 한 번에 변환. 짧은 응답에 적합."""
        # Supertonic은 동기 API — run_in_executor로 비동기 래핑
        wav, duration = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self.model.synthesize(
                text, voice_style=self.voice_style, lang=language
            )
        )
        # WAV → PCM int16 변환
        pcm = (wav * 32767).astype(np.int16).tobytes()
        return pcm

    async def synthesize_stream(self, text_stream: AsyncIterator[str],
                                 language: str = "ko",
                                 speed: float = 1.0) -> AsyncIterator[bytes]:
        """
        Cloud LLM 스트리밍 응답을 문장 단위로 수신하여 즉시 TTS 변환.
        첫 문장 완성 즉시 오디오 생성을 시작하여 체감 지연을 최소화한다.
        """
        sentence_buffer = ""

        async for text_chunk in text_stream:
            sentence_buffer += text_chunk

            # 문장 종결 부호로 분리
            sentences = self._split_sentences(sentence_buffer)

            if len(sentences) > 1:
                # 완성된 문장들은 즉시 TTS 변환
                for sentence in sentences[:-1]:
                    if sentence.strip():
                        audio = await self.synthesize(
                            sentence.strip(), language, speed
                        )
                        yield audio
                # 마지막 미완성 문장은 버퍼에 유지
                sentence_buffer = sentences[-1]

        # 남은 버퍼 처리
        if sentence_buffer.strip():
            audio = await self.synthesize(sentence_buffer.strip(), language, speed)
            yield audio

    def _split_sentences(self, text: str) -> list[str]:
        """
        문장/절 분리. 첫 TTS 시작을 앞당기기 위해 문장 종결 부호뿐만 아니라
        쉼표/절 경계에서도 분리한다 (최소 10자 이상일 때).
        """
        # 1차: 문장 종결 부호 (. ? ! 등)
        parts = re.split(r'(?<=[.?!。？！])\s+', text)
        # 2차: 긴 문장 내 쉼표/절 경계에서 추가 분리
        result = []
        for part in parts:
            if len(part) > 10 and ',' in part:
                sub = re.split(r'(?<=[,，])\s+', part)
                result.extend(sub)
            else:
                result.append(part)
        return result if result else [text]
```

**Supertonic TTS 음성 스타일**:

| 스타일 | 성별 | 비고 |
|--------|------|------|
| M1~M5 | 남성 | 5가지 남성 보이스 |
| F1~F5 | 여성 | 5가지 여성 보이스 |

**추론 스텝 옵션**: 2~128 스텝 조절 가능 (2 스텝: 최고속, 128 스텝: 최고 품질)

**언어 라우팅**: 별도 모델 분기 없이 `lang` 파라미터로 통합 처리.
```python
# 한국어든 영어든 동일 서비스, lang 파라미터만 변경
audio = await self.tts_service.synthesize(text, language=language)
```

## Cloud LLM client (cloud/llm_client.py)

```python
import anthropic

class CloudLLMClient:
    def __init__(self, provider: str = "claude"):
        self.provider = provider
        if provider == "claude":
            self.client = anthropic.AsyncAnthropic()  # ANTHROPIC_API_KEY 환경변수
        elif provider == "gpt":
            import openai
            self.client = openai.AsyncOpenAI()        # OPENAI_API_KEY 환경변수
            
        self.conversation_history: list[dict] = []
        self.max_history = 20  # 최대 멀티턴 유지 수
    
    async def get_response_stream(
        self,
        user_text: str,
        system_prompt: str,
        model_tier: str = "sonnet"
    ) -> AsyncIterator[str]:
        """
        스트리밍 응답을 문장 단위로 yield.
        TTS 파이프라인과 직접 연결되어, 문장 완성 즉시 음성 생성이 시작된다.
        model_tier: "haiku" (단순 질문, 빠른 응답) | "sonnet" (복잡한 질문)
        """
        self.conversation_history.append({"role": "user", "content": user_text})
        self._trim_history()

        if self.provider == "claude":
            model_id = {
                "haiku": "claude-haiku-4-5-20251001",
                "sonnet": "claude-sonnet-4-20250514",
            }[model_tier]

            async with self.client.messages.stream(
                model=model_id,
                max_tokens=500,           # 음성 응답이므로 짧게
                system=system_prompt,
                messages=self.conversation_history
            ) as stream:
                full_response = ""
                async for text in stream.text_stream:
                    full_response += text
                    yield text
                    
            self.conversation_history.append({
                "role": "assistant", "content": full_response
            })
                
        elif self.provider == "gpt":
            stream = await self.client.chat.completions.create(
                model="gpt-4o",
                max_tokens=500,
                messages=[
                    {"role": "system", "content": system_prompt},
                    *self.conversation_history
                ],
                stream=True
            )
            full_response = ""
            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    full_response += text
                    yield text
                    
            self.conversation_history.append({
                "role": "assistant", "content": full_response
            })
    
    def _trim_history(self):
        """대화 히스토리가 max_history를 초과하면 오래된 것부터 제거"""
        if len(self.conversation_history) > self.max_history:
            self.conversation_history = self.conversation_history[-self.max_history:]
    
    def clear_history(self):
        """대화 초기화"""
        self.conversation_history.clear()
```

## Prompt templates (cloud/prompt_templates.py)

```python
SYSTEM_PROMPT_KO = """당신은 한국어 AI 음성 비서입니다.

규칙:
- 응답의 첫 문장은 "네,", "글쎄요,", "좋은 질문이에요," 같은 짧은 호응으로 시작합니다.
- 간결하고 자연스러운 구어체로 응답합니다.
- 한 문장은 30자 이내로 유지합니다. TTS 변환 지연을 최소화하기 위함입니다.
- 전체 응답은 3~5문장 이내로 합니다.
- 마크다운, 이모지, 특수 기호를 사용하지 않습니다. 음성으로 읽기 어렵습니다.
- 숫자는 한글로 풀어 씁니다. (예: "3개" → "세 개")
- 영어 단어가 포함될 경우 한국어 발음으로 표기합니다. (예: "AI" → "에이아이")
- 목록이 필요하면 "첫째, 둘째" 형식으로 말합니다.
"""

SYSTEM_PROMPT_EN = """You are a voice assistant.

Rules:
- Start your response with a short acknowledgment like "Sure,", "Well,", or "Great question,".
- Keep responses concise and conversational.
- Each sentence should be under 15 words for TTS optimization.
- Total response should be 3-5 sentences.
- No markdown, emoji, or special symbols.
- Spell out numbers when natural in speech.
"""
```

## gRPC server (grpc_server.py)

```python
class VoiceServiceServicer(VoiceServiceServicer):
    def __init__(self, orchestrator: ServerOrchestrator):
        self.orchestrator = orchestrator

    async def ProcessVoice(self, request_iterator, context):
        """
        Edge → Server: 오디오 스트림 수신 → STT → 인텐트 체크 → Cloud LLM → TTS
        단일 RPC로 전체 파이프라인을 처리한다.

        반환 스트림:
        1. VoiceResponse(stt_result=...) — STT 완료 시 (참조/로깅용)
        2. VoiceResponse(tts_audio=...)  — TTS 오디오 청크들
        """
        # 1. 오디오 수신 → STT
        audio_buffer = bytearray()
        async for chunk in request_iterator:
            audio_buffer.extend(chunk.data)

        stt_result = await self.orchestrator.stt_service.transcribe(
            bytes(audio_buffer)
        )

        # STT 결과를 Edge에 참조용으로 전송
        yield VoiceResponse(stt_result=stt_result)

        # 2. 인텐트 라우터 — 로컬 처리 가능 여부 판단
        local_response = self.orchestrator.check_local_intent(
            stt_result.text, stt_result.language
        )

        if local_response:
            # 로컬 인텐트 매칭 → Cloud 스킵, 정의된 응답으로 TTS
            text_stream = _single_text_iter(local_response)
        else:
            # 매칭 실패 → Cloud LLM 호출 (모델 티어링 적용)
            model_tier = self.orchestrator.select_model_tier(stt_result.text, stt_result.language)
            text_stream = self.orchestrator.cloud_client.get_response_stream(
                user_text=stt_result.text,
                system_prompt=self.orchestrator.get_prompt(stt_result.language),
                model_tier=model_tier
            )

        # 3. TTS 변환 → 오디오 스트림 반환
        tts_service = self.orchestrator.get_tts_service()
        async for audio_bytes in tts_service.synthesize_stream(
            text_stream, language=stt_result.language
        ):
            for i in range(0, len(audio_bytes), CHUNK_SIZE):
                yield VoiceResponse(tts_audio=AudioChunk(
                    data=audio_bytes[i:i+CHUNK_SIZE],
                    sample_rate=24000,
                    encoding="pcm_s16le",
                    timestamp_ms=int(time.time() * 1000)
                ))

    async def HealthCheck(self, request, context):
        """Edge의 주기적 헬스체크에 응답"""
        stats = self.orchestrator.get_system_stats()
        return ServerStatus(
            stt_ready=self.orchestrator.stt_service is not None,
            tts_ready=self.orchestrator.tts_service is not None,
            gpu_usage=stats["gpu_usage"],
            memory_usage=stats["memory_usage"],
            temperature=stats["temperature"]
        )

# 서버 시작
async def serve(orchestrator: ServerOrchestrator, port: int = 50051):
    server = grpc.aio.server()
    add_VoiceServiceServicer_to_server(VoiceServiceServicer(orchestrator), server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    await server.start()
    await server.wait_for_termination()
```

## Orchestrator (orchestrator.py)

```python
class ServerOrchestrator:
    def __init__(self, config: dict):
        self.config = config
        self.model_manager = ModelManager(config)
        
        # 모델 초기화
        self.stt_service: STTService = None
        self.tts_service: SupertonicTTSService = None
        self.cloud_client: CloudLLMClient = None

    async def startup(self):
        """서버 시작 시 모델 로드. 순서 중요 — 큰 모델부터."""
        self.stt_service = STTService(
            model_size=self.config["stt"]["model_size"],
            compute_type=self.config["stt"]["compute_type"]
        )
        self.tts_service = SupertonicTTSService(
            voice_style=self.config["tts"].get("voice_style", "F1")
        )

        self.cloud_client = CloudLLMClient(
            provider=self.config["cloud"]["provider"]
        )

        logging.info(f"Models loaded. Memory: {self.model_manager.get_memory_usage()}")

    def check_local_intent(self, text: str, language: str) -> Optional[str]:
        """
        STT 결과 텍스트에서 로컬 인텐트를 매칭한다.
        매칭 성공 시 응답 텍스트를 반환, 실패 시 None (Cloud LLM으로 폴스루).
        """
        import re
        text_lower = text.strip().lower()
        for intent in self.config.get("local_intents", []):
            patterns = intent["patterns"].get(language, [])
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    return intent["responses"][language]
        return None

    def get_tts_service(self):
        """TTS 서비스 반환 (Supertonic 단일 서비스로 한국어/영어 통합 처리)"""
        return self.tts_service

    def select_model_tier(self, text: str, language: str) -> str:
        """
        LLM 모델 티어 선택. 기본 haiku, 승격 조건 충족 시 sonnet.

        판단 기준 (우선순위 순):
        1. 명시적 깊이 요청 키워드 → sonnet
        2. 복합 질문 (질문 2개 이상) → sonnet
        3. 멀티턴 깊이 (동일 주제 3턴+) → sonnet
        4. 그 외 → haiku
        """
        text_lower = text.strip().lower()

        # 1. 명시적 깊이 요청: 사용자가 상세한 응답을 원하는 신호
        SONNET_TRIGGERS = {
            "ko": ["자세히", "구체적으로", "설명해", "분석해", "비교해",
                    "차이점", "장단점", "원리", "이유가"],
            "en": ["explain", "in detail", "analyze", "compare",
                   "difference", "pros and cons", "how does", "why does"],
        }
        triggers = SONNET_TRIGGERS.get(language, SONNET_TRIGGERS["en"])
        if any(t in text_lower for t in triggers):
            return "sonnet"

        # 2. 복합 질문: 한 발화에 질문이 여러 개
        question_count = text.count('?') + text.count('？')
        if question_count >= 2:
            return "sonnet"

        # 3. 멀티턴 깊이: 대화가 깊어지면 복잡한 맥락 이해 필요
        if len(self.cloud_client.conversation_history) >= 6:  # 3턴 = user 3 + assistant 3
            return "sonnet"

        # 4. 기본: haiku (음성 질문 대부분은 단순)
        return "haiku"

    def get_prompt(self, language: str) -> str:
        """언어에 따라 시스템 프롬프트 선택"""
        if language == "en":
            return SYSTEM_PROMPT_EN
        return SYSTEM_PROMPT_KO

    def get_system_stats(self) -> dict:
        """tegrastats 기반 시스템 상태 반환"""
        return self.model_manager.get_stats()
```

## Model manager (model_manager.py)

```python
import subprocess

class ModelManager:
    """GPU 메모리 모니터링 및 모델 라이프사이클 관리"""
    
    def get_memory_usage(self) -> dict:
        """Jetson 통합 메모리 사용량 조회"""
        # tegrastats 파싱 또는 /proc/meminfo + jetson-stats
        try:
            import jtop
            with jtop.jtop() as jetson:
                return {
                    "total_mb": jetson.memory["RAM"]["tot"] // 1024,
                    "used_mb": jetson.memory["RAM"]["use"] // 1024,
                    "free_mb": jetson.memory["RAM"]["free"] // 1024,
                }
        except ImportError:
            # jtop 미설치 시 /proc/meminfo 파싱
            return self._parse_meminfo()
    
    def get_stats(self) -> dict:
        """GPU 사용률, 온도 등 종합 상태"""
        try:
            import jtop
            with jtop.jtop() as jetson:
                return {
                    "gpu_usage": jetson.gpu["val"],
                    "memory_usage": jetson.memory["RAM"]["use"] / jetson.memory["RAM"]["tot"],
                    "temperature": jetson.temperature["GPU"]["val"],
                }
        except ImportError:
            return {"gpu_usage": 0.0, "memory_usage": 0.0, "temperature": 0.0}
    
    def check_memory_safe(self, required_mb: int = 500) -> bool:
        """추가 모델 로드 전 메모리 여유 확인"""
        usage = self.get_memory_usage()
        return usage["free_mb"] > required_mb
```

**jetson-stats 설치**:
```bash
sudo pip install jetson-stats
# jtop으로 실시간 모니터링 가능
sudo jtop
```

## Monitoring (monitoring.py)

```python
from prometheus_client import Histogram, Gauge, start_http_server

# 추론 지연 측정
stt_latency = Histogram("stt_latency_seconds", "STT inference latency",
                        buckets=[0.1, 0.3, 0.5, 0.7, 1.0, 2.0, 5.0])
tts_latency = Histogram("tts_latency_seconds", "TTS inference latency per sentence",
                        buckets=[0.05, 0.1, 0.2, 0.3, 0.5, 1.0])
cloud_latency = Histogram("cloud_first_token_seconds", "Cloud LLM time to first token",
                          buckets=[0.1, 0.3, 0.5, 1.0, 2.0, 5.0])

# 시스템 상태
gpu_usage_gauge = Gauge("gpu_usage_percent", "GPU utilization")
memory_usage_gauge = Gauge("memory_usage_percent", "Unified memory utilization")
gpu_temp_gauge = Gauge("gpu_temperature_celsius", "GPU temperature")

def start_metrics_server(port: int = 9090):
    start_http_server(port)
```

**사용 예**:
```python
with stt_latency.time():
    result = await stt_service.transcribe(audio)

with tts_latency.time():
    audio = await tts_service.synthesize(sentence)
```

**Grafana 대시보드**: Server의 9090 포트에서 Prometheus 메트릭을 수집하고, Grafana에서 시각화.

## Config (config/server.yaml)

```yaml
stt:
  model_size: "large-v3"      # large-v3 | large-v3-turbo | medium
  compute_type: "float16"      # float16 | int8_float16 | int8
  language: "ko"
  beam_size: 5
  model_dir: "models/"

tts:
  voice_style: "F1"            # 음성 스타일 (M1~M5, F1~F5)
  speed: 1.0                   # 재생 속도
  inference_steps: 2           # 추론 스텝 (2: 최고속, 128: 최고 품질)

cloud:
  provider: "claude"           # claude | gpt
  models:                       # 모델 티어링
    haiku: "claude-haiku-4-5-20251001"    # 단순 질문, TTFT ~200-500ms
    sonnet: "claude-sonnet-4-20250514"    # 복잡한 질문, TTFT ~500-1500ms
  gpt_model: "gpt-4o"         # GPT 대체 모델
  max_tokens: 500              # 음성 응답은 짧게
  max_history: 20              # 멀티턴 대화 유지 수
  timeout_s: 30                # API 타임아웃
  sonnet_turn_threshold: 6     # conversation_history 길이 >= 이 값이면 sonnet 승격

local_intents:
  - name: "대화 종료"
    patterns:
      ko: ["그만", "끝", "종료", "^안녕히", "잘가", "여기까지"]
      en: ["bye", "goodbye", "stop", "quit", "end"]
    responses:
      ko: "안녕히 가세요."
      en: "Goodbye."

grpc:
  port: 50051
  max_workers: 4
  max_message_size: 10485760   # 10MB (긴 오디오 대응)

monitoring:
  prometheus_port: 9090
  tegrastats_interval_s: 1

power:
  mode: "MAXN"                 # 15W | 25W | MAXN (Super 모드)
  
logging:
  level: "INFO"
  file: "logs/server.log"
```

## Testing

```bash
# STT 단독 테스트
python -m server.stt.whisper_service --test --input test_ko.wav

# TTS 단독 테스트 (한국어)
python -m server.tts.supertonic_service --test --text "안녕하세요, 테스트입니다." --lang ko

# TTS 단독 테스트 (영어)
python -m server.tts.supertonic_service --test --text "Hello, this is a test." --lang en

# Cloud LLM 연결 테스트
python -m server.cloud.llm_client --test --provider claude

# gRPC 서버 시작 (개발 모드)
python -m server.main --dev

# 메모리 사용량 확인
python -m server.model_manager --check-memory

# Prometheus 메트릭 확인
curl http://localhost:9090/metrics

# 전체 파이프라인 E2E 테스트 (Edge 필요)
python -m server.main --test-mode
```

## Build & deploy

```bash
# Jetson 초기 설정
./scripts/setup_server.sh

# JetPack 확인
sudo apt-get install nvidia-jetpack
jetson_release  # JetPack 버전 확인

# Super 모드 활성화 (25W → MAXN)
sudo nvpmodel -m 0
sudo jetson_clocks

# 의존성 설치
cd server/
pip install -r requirements.txt

# 모델 다운로드 (Whisper은 첫 실행 시 자동, Supertonic도 자동)
python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cuda')"
python -c "from supertonic import TTS; TTS(auto_download=True)"

# 실행
python -m server.main

# Docker (선택)
docker build -t voice-assistant-server -f server/Dockerfile .
docker run --runtime nvidia --network host voice-assistant-server
```

**Docker 주의사항**: `--runtime nvidia`로 GPU 접근. Jetson은 `nvidia-container-runtime` 사전 설치 필요.

## Performance tuning

### Jetson 전력 모드

```bash
# 현재 모드 확인
sudo nvpmodel -q

# Super 모드 (MAXN, 최대 성능)
sudo nvpmodel -m 0
sudo jetson_clocks

# 15W 모드 (저전력, 발열 제한 환경)
sudo nvpmodel -m 1
```

| Mode | CPU | GPU | AI TOPS | 전력 | 권장 상황 |
|------|-----|-----|---------|------|----------|
| 15W | 6코어 @1.5GHz | 625MHz | 40 | ~15W | 밀폐 케이스, 발열 제한 |
| MAXN (Super) | 6코어 @1.7GHz | 1007MHz | 67 | ~25W | 쿨링 충분, 최대 성능 |

### swap 설정

통합 메모리 8GB가 부족할 수 있으므로 swap을 설정한다:
```bash
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**swap은 성능 저하를 유발하므로 비상용이다. 정상 운영 시 swap 사용이 발생하면 모델 크기를 줄여야 한다.**

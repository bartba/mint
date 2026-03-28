# Server Application 개발 계획 (Jetson Orin Nano 8GB)

## Context

3-Tier 분산 AI 음성 비서의 Server 측 구현.
Server는 Edge로부터 오디오를 수신하여 **고정밀 STT → Cloud LLM → TTS** 파이프라인을 실행하고 음성을 반환한다.

**현재 상태**: CLAUDE.md 문서만 존재, 소스 코드 없음
**디바이스**: Jetson Orin Nano 8GB (CUDA 1024코어, 통합메모리 8GB, JetPack 6.x)

---

## Phase 1: 기반 인프라 (proto, shared, config, 환경설정)

### 1.1 proto/voice_service.proto
- gRPC 계약 정의 (CLAUDE.md 스펙 준수)
- `service VoiceService`: StreamSTT, StreamTTS, HealthCheck
- `message`: AudioChunk, STTResult, TTSRequest, ServerStatus, Empty
- Python stub 생성: `voice_service_pb2.py`, `voice_service_pb2_grpc.py`
```bash
python -m grpc_tools.protoc -I proto/ --python_out=. --grpc_python_out=. proto/voice_service.proto
```

### 1.2 shared/ 패키지

| 파일 | 역할 |
|------|------|
| `shared/__init__.py` | 패키지 초기화 |
| `shared/config.py` | YAML 설정 로딩 (`default.yaml` + `server.yaml`), 환경변수(API키), `get_config()` 싱글턴 |
| `shared/models.py` | 데이터클래스 (`STTResultData`, `AudioChunkData`, `TTSRequestData`, `ServerStatusData`) + `to_proto()`/`from_proto()` |
| `shared/utils.py` | `setup_logging()`, `pcm_to_float32()`, `float32_to_pcm()`, `chunk_bytes()` |

### 1.3 config/ 설정파일
- `config/default.yaml`: 공유 기본값 (로그 레벨, gRPC 포트)
- `config/server.yaml`: 전체 서버 설정
  - `stt`: model_size, compute_type, language, beam_size
  - `tts`: voice_style, speed, inference_steps
  - `cloud`: provider, model, max_tokens, timeout
  - `grpc`: port, max_workers, max_message_size
  - `monitoring`: prometheus_port
  - `power`: mode
  - `logging`: level, file

### 1.4 scripts/setup_server.sh
- JetPack 6.x 확인 (`jetson_release`)
- MAXN 전력 모드 설정 (`nvpmodel -m 0`, `jetson_clocks`)
- 8GB swap 파일 생성
- Python venv 생성 + 기본 패키지 설치
- `jetson-stats` 설치
- Proto stub 생성

### 검증
```bash
python -c "from shared.config import get_config; print(get_config())"
python -c "import voice_service_pb2; print('proto OK')"
```

### 생성 파일
```
proto/voice_service.proto
shared/__init__.py
shared/config.py
shared/models.py
shared/utils.py
config/default.yaml
config/server.yaml
scripts/setup_server.sh
```

---

## Phase 2: 핵심 AI 서비스 (STT, TTS)

### 2.1 server/model_manager.py — GPU/메모리 관리
- `ModelManager` 클래스
- `get_memory_usage() -> dict`: jtop 또는 /proc/meminfo로 메모리 조회
- `get_stats() -> dict`: GPU 사용률, 메모리, 온도
- `check_memory_safe(required_mb=500) -> bool`: 모델 로드 전 여유 확인

### 2.2 server/stt/whisper_service.py — STT 서비스
- `STTService` 클래스 (faster-whisper 기반)
- 모델: Whisper-large-v3-turbo, FP16, ~3GB VRAM
- `async transcribe(audio_data: bytes, language: str) -> STTResultData`
  - PCM int16 → float32 변환
  - `run_in_executor()`로 비동기 래핑 (동기 추론 블로킹 방지)
  - `vad_filter=False` (Edge VAD 의존)
- `async transcribe_stream(audio_stream, language) -> AsyncIterator[STTResultData]`
  - 청크 누적 후 일괄 변환

### 2.3 server/tts/supertonic_service.py — TTS 서비스 (한국어 + 영어 통합)
- `SupertonicTTSService` 클래스 (Supertonic TTS, ONNX Runtime)
- **Supertonic TTS 스펙**:
  - 66M 파라미터, ONNX 모델 ~305MB
  - 한국어(ko) + 영어(en) + 스페인어/포르투갈어/프랑스어 다국어 지원
  - ONNX Runtime 기반 (CPU 최적화, GPU 가속 가능)
  - 음성 스타일: M1~M5 (남성), F1~F5 (여성)
  - 설치: `pip install supertonic` (의존성: onnxruntime, numpy, soundfile, huggingface-hub)
  - 첫 실행 시 HuggingFace에서 모델 자동 다운로드 (~305MB)
  - 추론 스텝 조절 가능 (2~128 스텝, 2 스텝에서 RTF 0.012 on CPU)
- **API 구조**:
  ```python
  from supertonic import TTS
  tts = TTS(auto_download=True)
  style = tts.get_voice_style("F1")
  wav, duration = tts.synthesize("안녕하세요", voice_style=style, lang="ko")
  wav, duration = tts.synthesize("Hello", voice_style=style, lang="en")
  ```
- `async synthesize(text: str, language: str, speed: float) -> bytes`
  - 동기 API를 `run_in_executor()`로 비동기 래핑
  - 언어 코드로 한국어/영어 자동 전환 (별도 모델 불필요)
  - WAV 출력 → PCM int16 변환
- `async synthesize_stream(text_stream, language, speed) -> AsyncIterator[bytes]`
  - LLM 스트리밍 응답을 문장 단위로 수신 → 즉시 TTS 변환
  - 첫 문장 완성 즉시 오디오 생성 시작 (체감 지연 최소화)
- `_split_sentences(text) -> list[str]`: `[.?!。？！]\s+` 기반 문장 분리

### 메모리 예산 확인
```
OS              ~1.5GB
Whisper         ~3.0GB (FP16) 또는 ~1.5GB (INT8)
Supertonic TTS  ~0.3GB (ONNX, 단일 모델로 한국어+영어)
gRPC 버퍼        ~0.5GB
────────────────────
합계            ~5.3GB / 8GB (여유 ~2.7GB)
```
> MeloTTS(0.6GB) + Kokoro(0.3GB) 대비 Supertonic(0.3GB) 단일 모델로 메모리 0.6GB 절감.
> PyTorch 의존성 제거 → ONNX Runtime만으로 TTS 가능, 시스템 복잡도 감소.

### 검증
```bash
python -m server.stt.whisper_service --test --input test_ko.wav      # 목표 <800ms
python -m server.tts.supertonic_service --test --text "안녕하세요" --lang ko  # 목표 <100ms
python -m server.tts.supertonic_service --test --text "Hello" --lang en      # 목표 <100ms
python -m server.model_manager --check-memory                        # 목표 <5.5GB
```

### 생성 파일
```
server/__init__.py
server/model_manager.py
server/stt/__init__.py
server/stt/whisper_service.py
server/tts/__init__.py
server/tts/supertonic_service.py
```

---

## Phase 3: Cloud LLM 연동

### 3.1 server/cloud/prompt_templates.py — 시스템 프롬프트
- `SYSTEM_PROMPT_KO`: 한국어 음성비서 규칙
  - 간결한 구어체, 문장 30자 이내, 3-5문장
  - 마크다운/이모지 금지, 숫자는 한글 표기
- `SYSTEM_PROMPT_EN`: 영어 음성비서 규칙
  - 문장 15단어 이내, 3-5문장
- `get_system_prompt(language: str) -> str`

### 3.2 server/cloud/llm_client.py — LLM 스트리밍 클라이언트
- `CloudLLMClient` 클래스
- 지원 프로바이더: Claude (`anthropic.AsyncAnthropic`), GPT (`openai.AsyncOpenAI`)
- `async get_response_stream(user_text, system_prompt) -> AsyncIterator[str]`
  - Claude: `client.messages.stream()` → `text_stream`
  - GPT: `client.chat.completions.create(stream=True)`
- 대화 히스토리: `conversation_history` 리스트, max 20턴
- `_trim_history()`: 오래된 메시지 제거
- `clear_history()`: 대화 초기화
- 에러 처리: API 실패 시 폴백 메시지 yield ("죄송합니다, 일시적인 오류가 발생했습니다.")
- 타임아웃: `asyncio.wait_for()` 래핑 (config.cloud.timeout_s)

### 3.3 LLM → TTS 파이프라인 통합 테스트
```
사용자 텍스트 → CloudLLMClient.get_response_stream()
                  ↓ (토큰 스트리밍)
              SupertonicTTSService.synthesize_stream()
                  ↓ (문장 완성 즉시 변환)
              PCM 오디오 청크
```
- 목표: 첫 오디오 청크까지 < 1.5s

### 검증
```bash
ANTHROPIC_API_KEY=... python -m server.cloud.llm_client --test --provider claude
```

### 생성 파일
```
server/cloud/__init__.py
server/cloud/prompt_templates.py
server/cloud/llm_client.py
```

---

## Phase 4: gRPC 서버 + 오케스트레이터 (전체 파이프라인)

### 4.1 server/orchestrator.py — 서비스 조율
- `ServerOrchestrator` 클래스
- `async startup()`: 모델 순서대로 로드 (큰 것부터)
  1. STTService (Whisper ~3GB)
  2. SupertonicTTSService (~0.3GB, 한국어+영어 통합)
  3. CloudLLMClient
  - 각 단계 전 `model_manager.check_memory_safe()` 확인
- `get_tts_service()`: SupertonicTTSService 반환 (단일 서비스로 한국어/영어 모두 처리)
- `get_prompt(language: str)`: 언어별 시스템 프롬프트 반환
- `get_system_stats() -> dict`: 시스템 상태 조회
- `async shutdown()`: graceful 정리

### 4.2 server/grpc_server.py — gRPC 서비서

**StreamSTT** (stream AudioChunk → stream STTResult):
```
Edge 오디오 청크 수신 → 버퍼 누적 → Whisper 변환 → STTResult 반환
```

**StreamTTS** (TTSRequest → stream AudioChunk):
```
텍스트 수신 → Cloud LLM 스트리밍 호출 → 문장 분할 → Supertonic TTS 변환 → 오디오 청크 스트리밍
```
- PCM 데이터를 CHUNK_SIZE(4096) 단위로 분할 전송
- encoding="pcm_s16le"

**HealthCheck** (Empty → ServerStatus):
```
시스템 상태 조회 → stt_ready, tts_ready, gpu_usage, memory_usage, temperature
```

- gRPC 에러 처리: try/except + 적절한 StatusCode 설정
- `serve()`: `grpc.aio.server()`, 10MB max message size

### 4.3 server/main.py — 엔트리포인트
1. 로깅 설정
2. 설정 로드 (`get_config()`)
3. `ServerOrchestrator` 생성 + `startup()`
4. Prometheus 메트릭 서버 시작 (9090)
5. gRPC 서버 시작 (50051)
6. SIGINT/SIGTERM → graceful shutdown
7. CLI 플래그: `--dev` (디버그), `--test-mode` (통합테스트)

### 전체 파이프라인
```
┌─ Edge ──────────────────────────────────────────── Server ─────────────────────────── Cloud ─┐
│                                                                                              │
│  AudioChunk stream ──► StreamSTT ──► Whisper GPU (~700ms) ──► STTResult ──► Edge UI 교체     │
│                                                                                              │
│  TTSRequest ──► StreamTTS ──► Cloud LLM stream ──────────────────────────► Claude/GPT API    │
│                                    ↓ (토큰)                                                   │
│                               문장 분할 → Supertonic TTS (~50ms/문장)                         │
│                                    ↓                                                          │
│  AudioChunk stream ◄── PCM 청크                                                              │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 검증
```bash
python -m server.main --dev
# grpcurl로 HealthCheck 호출 → 상태 확인
# 테스트 WAV로 StreamSTT → 텍스트 반환 확인
# StreamTTS 요청 → 오디오 스트림 수신 확인
# E2E 지연시간 < 2.0s (발화 종료 → 첫 오디오)
# tegrastats로 메모리 모니터링
```

### 생성 파일
```
server/orchestrator.py
server/grpc_server.py
server/main.py
```

---

## Phase 5: 모니터링, Docker, 최적화

### 5.1 server/monitoring.py — Prometheus 메트릭
- Histogram: `stt_latency_seconds`, `tts_latency_seconds`, `cloud_first_token_seconds`
- Gauge: `gpu_usage_percent`, `memory_usage_percent`, `gpu_temperature_celsius`
- `start_metrics_server(port=9090)`
- 1초 주기 시스템 상태 갱신 백그라운드 태스크
- 서비스에 계측 포인트 추가:
  ```python
  with stt_latency.time():
      result = await stt_service.transcribe(audio)
  ```

### 5.2 server/requirements.txt
```
faster-whisper>=1.0
ctranslate2>=4.0
supertonic                    # Supertonic TTS (ONNX, 한국어+영어 통합)
onnxruntime                   # Supertonic 의존성 (GPU: onnxruntime-gpu)
grpcio>=1.60
grpcio-tools>=1.60
protobuf>=4.25
anthropic>=0.40
openai>=1.50
pyyaml>=6.0
prometheus-client>=0.20
jetson-stats
```
> MeloTTS 제거로 torch/torchaudio/python-mecab-ko 의존성이 불필요해짐.
> Whisper(faster-whisper)는 CTranslate2 기반이므로 PyTorch 없이 동작.
> 전체 의존성이 크게 간소화됨.

### 5.3 server/Dockerfile
- 베이스: 경량 Python 이미지 (PyTorch 불필요, ONNX Runtime만 필요)
- 포트: 50051 (gRPC), 9090 (Prometheus)
- 실행: `--runtime nvidia` (Whisper GPU 가속)

### 5.4 docker-compose.yaml
```yaml
services:
  server:
    build: { context: ., dockerfile: server/Dockerfile }
    runtime: nvidia
    network_mode: host
    environment: [ANTHROPIC_API_KEY, OPENAI_API_KEY]
    volumes: [./models:/app/models, ./logs:/app/logs]
    restart: unless-stopped
```

### 5.5 성능 최적화
| 항목 | 전략 |
|------|------|
| GPU 타임쉐어링 | STT(GPU) / TTS(CPU/ONNX) 분리로 GPU 충돌 없음 |
| 메모리 폴백 | Whisper FP16 → INT8 자동 전환 (3GB → 1.5GB) |
| TTS 파이프라이닝 | asyncio.Queue로 LLM↔TTS 디커플링, 첫 문장 즉시 변환 |
| TTS 추론 스텝 | Supertonic inference_steps 조절 (2스텝: 최고속, 품질 트레이드오프) |
| 열 관리 | 75°C 경고, 80°C 시 전력 모드 하향 |
| gRPC 최적화 | keepalive 설정, 끊긴 연결 빠른 감지 |

### 검증
```bash
docker compose up server
curl http://localhost:9090/metrics
# 30분 연속 부하 테스트
# - 메모리 누수 없음 확인
# - GPU 온도 <80°C 유지
# - 지연시간 분포 일관성
```

### 생성 파일
```
server/monitoring.py
server/requirements.txt
server/Dockerfile
docker-compose.yaml
```

---

## 파일 생성 순서 (의존성 그래프)

```
Phase 1: proto/voice_service.proto
         → shared/{__init__,config,models,utils}.py
         → config/{default,server}.yaml
         → scripts/setup_server.sh

Phase 2: server/{__init__,model_manager}.py
         → server/stt/{__init__,whisper_service}.py
         → server/tts/{__init__,supertonic_service}.py

Phase 3: server/cloud/{__init__,prompt_templates,llm_client}.py

Phase 4: server/orchestrator.py
         → server/grpc_server.py
         → server/main.py

Phase 5: server/monitoring.py
         → server/requirements.txt
         → server/Dockerfile
         → docker-compose.yaml
```

---

## Supertonic TTS 도입 효과 (vs MeloTTS + Kokoro)

| 항목 | 기존 (MeloTTS + Kokoro) | 변경 (Supertonic) |
|------|------------------------|-------------------|
| TTS 모델 수 | 2개 (한국어/영어 분리) | 1개 (다국어 통합) |
| 메모리 | ~900MB (600+300) | ~305MB |
| 런타임 | PyTorch CUDA + ONNX | ONNX Runtime만 |
| 의존성 | torch, torchaudio, mecab 등 | supertonic, onnxruntime |
| 언어 라우팅 | 코드에서 모델 분기 필요 | lang 파라미터로 통합 처리 |
| 파일 수 | melo_service.py + kokoro_service.py | supertonic_service.py 1개 |
| 추가 언어 확장 | 모델별 추가 필요 | lang 코드만 추가 (es, pt, fr 이미 지원) |

---

## 리스크 & 대응

| 리스크 | 영향 | 대응 |
|--------|------|------|
| Supertonic ONNX ARM64 호환성 | TTS 불가 | onnxruntime ARM64 wheel 확인, Phase 2 초기 검증 |
| 통합 메모리 8GB 부족 | OOM / swap thrashing | check_memory_safe() + Whisper INT8 자동 폴백 |
| 동기 추론 asyncio 블로킹 | gRPC 응답 지연 | 모든 추론 run_in_executor() 래핑 |
| Cloud API 지연 변동 (200ms~2s+) | E2E 지연 목표 초과 | 문장 단위 TTS 파이프라이닝으로 첫 응답 최소화 |
| Supertonic 한국어 음질 | 사용자 경험 저하 | inference_steps 조절로 품질/속도 트레이드오프, 음성 스타일 선택 |

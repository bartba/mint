# CLAUDE.md — Voice Assistant Project Root

## Project summary

3-Tier 분산 AI 음성 비서: Edge(Jetson Nano) → Server(Jetson Orin Nano) → Cloud(Claude/GPT).
음성 입력을 실시간 처리하고, 질문 언어(한국어 혹은 영어)에 맞춰 해당 언어의 음성으로 응답한다.

## Architecture

```
Edge (Jetson Nano 4GB)  ◄──gRPC──►  Server (Jetson Orin Nano 8GB)  ──HTTPS──►  Cloud LLM
  마이크/스피커                          STT/TTS GPU 추론                    NLU/대화/응답생성
  VAD/Wake word                         Whisper-large-turbo
                                        Supertonic TTS (한국어+영어)
  오프라인 폴백                          Cloud API 중계
```

**핵심 원칙**: 사용자 I/O는 Edge, AI 추론은 Server GPU, LLM은 Cloud. Server/Cloud 단절 시 Edge 단독 폴백.

## Hardware

| Device | Role | Key specs |
|--------|------|-----------|
| Jetson Nano 4GB | Edge | Cortex-A57×4 @1.43GHz, 128 CUDA (Maxwell), 4GB LPDDR4, GbE, USB3×1 |
| Jetson Orin Nano 8GB | Server | Cortex-A78AE×6, 1024 CUDA, 40 TOPS, 8GB LPDDR5 통합메모리 |
| ReSpeaker XVF3800 | Mic (→Edge USB) | 4-mic array, HW AEC/빔포밍/DoA/노이즈억제, 3.5mm AUX out |
| Speaker (AUX) | 출력 (→XVF3800 AUX) | 3~5W 셀프파워드, XVF3800 AUX 직결로 HW AEC 활용 |

**Edge 메모리 예산** (4GB 통합): OS 1.0GB + Silero VAD 10MB + openWakeWord 50MB + 버퍼 0.5GB = ~1.6GB (여유 2.4GB)

**Server 메모리 예산** (8GB 통합): OS 1.5GB + Whisper-large-turbo 3GB + Supertonic TTS 0.3GB + 버퍼 0.5GB = 5.3GB (여유 2.7GB)

## AI models

| Model | Location | Purpose | Latency | Memory |
|-------|----------|---------|---------|--------|
| Silero VAD | Edge CPU | 음성 활동 감지 | ~5ms | 10MB |
| openWakeWord | Edge CPU | 웨이크워드 | ~10ms | 50MB |
| Whisper-large-v3-turbo | Server GPU (faster-whisper) | 2차 고정밀 STT | ~700ms | 3GB |
| Supertonic TTS | Server CPU/ONNX | 다국어 TTS (한국어+영어) | ~50ms (짧은문장) | 305MB |

## Data flow (normal mode)

```
0ms     Edge: 마이크 캡처(wake word) → XVF3800 AEC/노이즈제거 → Silero VAD 감시
        Edge: 웨이크워드 감지 → chime_wake 재생 (상승 톤, 인식 피드백)
~Xms    Edge: 발화 감지 → gRPC 오디오 스트림 → Server
        Edge: VAD 발화 종료 감지 → chime_ack 재생 (하강 톤, 수신 확인)
~700ms  Server: Whisper-large-turbo 완료
           └── Server에서 바로 Cloud API 호출 (Edge 경유 없음)
~1.2s   Cloud: 스트리밍 응답 시작 → Server: 문장/절 단위 Supertonic TTS 변환
~1.5s   Edge: TTS 오디오 스트리밍 재생 시작
~3-5s   Edge: 전체 응답 재생 완료
```

> **설계 원칙**: STT 완료 후 텍스트를 Edge로 되돌린 뒤 다시 Server로 보내지 않는다. Server가 STT → 인텐트 체크 → Cloud LLM 호출을 일관 처리하고, Edge에는 TTS 오디오만 전달한다.

## Data flow (local command mode)

STT 결과에 대해 Cloud LLM 호출 전에 Server에서 인텐트 매칭을 수행한다.
매칭되면 Cloud를 거치지 않고 Server에서 즉시 TTS 응답을 생성하여 Edge로 전달한다.

```
0ms     Edge: 마이크 캡처 → gRPC 오디오 스트림 → Server
~700ms  Server: Whisper-large-turbo 완료 → 인텐트 라우터(키워드/정규식 매칭)
~700ms  Server: 로컬 인텐트 매칭 성공 → Cloud 스킵, 정의된 응답으로 Supertonic TTS 변환
~750ms  Edge: TTS 오디오 스트리밍 재생 시작
```

### 로컬 인텐트 정의

Server `orchestrator.py`의 인텐트 라우터가 STT 결과 텍스트를 검사한다.
매칭 실패 시 normal mode로 폴스루.

| 인텐트 | 매칭 패턴 (ko) | 매칭 패턴 (en) | 응답 (ko) | 응답 (en) |
|--------|---------------|---------------|----------|----------|
| 대화 종료 | 그만, 끝, 종료, 안녕히*, 잘가, 여기까지 | bye, goodbye, stop, quit, end | "안녕히 가세요." | "Goodbye." |

> **확장**: 볼륨 조절, 반복 요청 등 단순 커맨드를 이 테이블에 추가하여 Cloud 호출 없이 처리할 수 있다.

## Communication contracts

### Edge ↔ Server: gRPC (proto/voice_service.proto)

```protobuf
syntax = "proto3";
package voiceassistant;

service VoiceService {
  // Edge가 오디오를 보내면 Server가 STT → 인텐트 체크 → Cloud LLM → TTS를
  // 일관 처리하고, VoiceResponse 스트림으로 결과를 반환한다.
  rpc ProcessVoice (stream AudioChunk) returns (stream VoiceResponse);
  rpc HealthCheck (Empty) returns (ServerStatus);
}

message AudioChunk {
  bytes data = 1;
  int32 sample_rate = 2;    // 16000
  string encoding = 3;      // "opus" | "pcm_s16le"
  int64 timestamp_ms = 4;
}

message VoiceResponse {
  oneof response {
    STTResult stt_result = 1;   // STT 완료 시 (참조/로깅용)
    AudioChunk tts_audio = 2;   // TTS 오디오 청크
  }
}

message STTResult {
  string text = 1;
  float confidence = 2;
  bool is_final = 3;
  string language = 4;      // "ko" | "en"
}

message ServerStatus {
  bool stt_ready = 1;
  bool tts_ready = 2;
  float gpu_usage = 3;
  float memory_usage = 4;
  float temperature = 5;
}

message Empty {}
```

## Directory structure

```
voice-assistant/
├── CLAUDE.md                  ← 본 파일 (루트 컨텍스트)
├── proto/
│   └── voice_service.proto    ← gRPC 계약 (Edge/Server 공유)
├── shared/
│   ├── config.py              ← IP, 포트, 모델 경로 등 공유 설정
│   ├── models.py              ← 공통 데이터 모델
│   └── utils.py               ← 유틸리티
├── edge/
│   ├── CLAUDE.md              ← Edge 전용 컨텍스트
│   ├── main.py
│   ├── audio/                 ← 캡처, 재생, VAD, 웨이크워드
│   ├── ui/                    ← (향후 확장) 상태 표시
│   ├── cache/                 ← 오프라인 WAV 캐시
│   ├── grpc_client.py
│   ├── orchestrator.py
│   ├── requirements.txt
│   └── Dockerfile
├── server/
│   ├── CLAUDE.md              ← Server 전용 컨텍스트
│   ├── main.py
│   ├── stt/                   ← Whisper-large-turbo
│   ├── tts/                   ← Supertonic TTS (한국어+영어)
│   ├── cloud/                 ← LLM API 클라이언트
│   ├── grpc_server.py
│   ├── orchestrator.py
│   ├── model_manager.py
│   ├── monitoring.py
│   ├── requirements.txt
│   └── Dockerfile
├── scripts/
│   ├── setup_edge.sh
│   ├── setup_server.sh
│   ├── generate_tts_cache.py
│   └── deploy.sh
├── config/
│   ├── default.yaml
│   ├── edge.yaml
│   └── server.yaml
└── docker-compose.yaml
```

## Development environment

### Physical setup

```
Host PC (Ubuntu 22.04, x86)
  VS Code + Claude Code Extension
    ├── Remote-SSH → Jetson Nano (Edge)    ~/voice-assistant/
    └── Remote-SSH → Jetson Orin Nano (Server)  ~/voice-assistant/
```

양쪽 디바이스에 동일 모노레포를 clone. VS Code 창 2개로 병렬 개발.

### Why monorepo

proto/voice_service.proto가 Edge↔Server 핵심 계약이므로, 양쪽이 항상 동일 버전을 참조해야 한다.
모노레포에서는 proto 변경 + 양쪽 코드 수정을 단일 커밋으로 묶어 불일치를 방지한다.

### Git workflow

**Branch strategy**:
- `main` — 안정 릴리즈 (양쪽 검증 완료)
- `develop` — 통합 개발
- `feature/edge-*` — Edge 기능 (Jetson Nano에서 작업)
- `feature/server-*` — Server 기능 (Jetson Orin Nano에서 작업)
- `feature/proto-*` — 인터페이스 변경 (양쪽 합의 후)

**Commit prefix**: `edge:`, `server:`, `proto:`, `shared:`, `docs:`, `infra:`

**proto 변경 규칙**: proto 수정이 포함된 브랜치는 반드시 양쪽 디바이스에서 테스트 후 develop에 머지.

### Sync procedure

```bash
# 양쪽 디바이스에 클론
[Jetson Nano]      $ git clone git@github.com:user/voice-assistant.git
[Jetson Orin Nano] $ git clone git@github.com:user/voice-assistant.git

# 각자 feature 브랜치에서 작업
[Jetson Nano]      $ git checkout -b feature/edge-audio-pipeline
[Jetson Orin Nano] $ git checkout -b feature/server-stt-service

# 커밋 & 푸시
[Jetson Nano]      $ git commit -m "edge: implement VAD pipeline"
[Jetson Orin Nano] $ git commit -m "server: implement whisper service"

# develop 머지 후 양쪽 pull
[Jetson Nano]      $ git pull origin develop
[Jetson Orin Nano] $ git pull origin develop
```

## Latency 최적화 전략

발화 종료 → 첫 TTS 재생까지 예상 레이턴시:

| 구간 | 시간 | 비고 |
|------|------|------|
| gRPC 잔여 전송 | ~5ms | 발화 중 이미 스트리밍 |
| Whisper STT | ~500-800ms | Orin Nano GPU |
| 인텐트 체크 | ~0ms | 정규식 |
| Cloud LLM TTFT | ~500-1500ms | 첫 토큰까지 |
| 첫 문장/절 누적 | ~200-700ms | 문장 종결 부호까지 |
| Supertonic TTS | ~50ms | 짧은 문장 |
| **합계** | **~1.3-3.1s** | 일반적으로 ~2.0s |

### 최적화 1: Chime 즉시 피드백

Edge에서 VAD가 발화 종료를 감지하는 즉시 짧은 chime 사운드를 재생한다. 시스템이 수신했음을 알려 체감 대기 시간을 줄인다. TTS 음성 응답과 혼동되지 않도록 비음성(톤/차임) 사운드를 사용한다.

### 최적화 2: LLM 첫 문장 단축 유도

시스템 프롬프트에서 응답의 첫 문장을 짧은 호응("네,", "글쎄요,", "좋은 질문이에요,")으로 시작하도록 유도한다. 첫 문장이 5-10자로 단축되어 문장 누적 시간이 ~200ms로 줄어든다. 예상 절약: ~300-500ms.

### 최적화 3: 문장 경계 기준 완화

TTS 변환 트리거를 문장 종결 부호(. ? !)뿐만 아니라 **쉼표(,) 및 절 경계**에서도 수행한다 (최소 길이 조건 충족 시). 첫 TTS 시작이 ~100-300ms 앞당겨진다.

### 최적화 4: LLM 모델 티어링

기본 haiku, 승격 조건 충족 시에만 sonnet. 음성 비서 질문의 대부분은 단순하므로 haiku가 기본이다.

| 모델 | TTFT | 용도 |
|------|------|------|
| Claude Haiku (기본) | ~200-500ms | 대부분의 질문 |
| Claude Sonnet (승격) | ~500-1500ms | 깊이 있는 응답 필요 시 |

**Sonnet 승격 조건** (Server `orchestrator.py`에서 경량 판단):
1. 명시적 깊이 요청 키워드 ("자세히", "설명해", "비교해", "explain", "analyze" 등)
2. 복합 질문 (한 발화에 질문 2개 이상)
3. 멀티턴 깊이 (동일 대화 3턴 이상 — 맥락이 복잡해짐)

단순 질문의 경우 TTFT를 ~300-800ms 절약할 수 있다.

## Performance targets

| Scenario | Target | Measurement |
|----------|--------|-------------|
| 일반 대화 (첫 음성) | < 2.0s | 발화 종료 → TTS 첫 오디오 재생 |
| 일반 대화 (완료) | < 5.0s | 발화 종료 → 전체 응답 완료 |
| 오프라인 안내 | < 500ms | 발화 종료 → WAV 캐시 재생 |

| Device | CPU target | Memory target | Temp limit |
|--------|-----------|--------------|------------|
| Edge Jetson Nano | <50% idle, <70% active | <3GB | <80°C |
| Server Jetson | GPU <30% idle, <70% active | <5.5GB | <80°C |

## Shared dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| Python | 3.10+ | Runtime |
| grpcio + protobuf | 1.60+ / 4.25+ | Edge ↔ Server 통신 |
| pyyaml | 6.0+ | Config |
| prometheus-client | 0.20+ | Metrics |

## Security rules

- Cloud API 키: 환경변수 또는 .env 파일. 코드에 하드코딩 금지.
- Cloud에는 텍스트만 전송. 음성 원본은 로컬 네트워크 내에서만 처리.
- 대화 히스토리는 Server 메모리에만 유지.

## Code conventions

- Python 3.10+, type hints 필수, asyncio 기반
- 네이밍: snake_case (함수/변수), PascalCase (클래스)
- proto 변경 시 양쪽 CLAUDE.md의 관련 섹션도 함께 업데이트할 것
- 에러 처리: gRPC 호출은 반드시 try/except + 폴백 로직 포함
- 로깅: Python logging 모듈, 레벨은 config로 관리

## Roadmap

### Phase 1: 기반 인프라 (2주)
- [ ] 양쪽 OS 설치, 네트워크 구성, Git 클론
- [ ] proto 정의 + gRPC 기본 통신 테스트
- [ ] Edge: JetPack 설정, ReSpeaker XVF3800 오디오 캡처 파이프라인
- [ ] Server: JetPack 설정, CUDA 환경 검증

### Phase 2: 핵심 음성 파이프라인 (3주)
- [ ] Server: Whisper-large-turbo + Supertonic TTS 서비스
- [ ] Edge: Silero VAD + Wake word
- [ ] Edge ↔ Server gRPC 스트리밍 통합

### Phase 3: Cloud LLM 연동 (2주)
- [ ] Server: Claude/GPT 스트리밍 클라이언트 (모델 티어링: Haiku/Sonnet)
- [ ] Server: 문장/절 단위 청킹 → TTS 파이프라인 연동 (문장 경계 완화 포함)
- [ ] Server: LLM 프롬프트 최적화 (첫 문장 단축 유도)
- [ ] E2E 통합 테스트

### Phase 4: 오프라인 폴백 (2주)
- [ ] Edge: WAV 캐시 + 오프라인 폴백 (chime.wav 포함)
- [ ] Edge: VAD 발화 종료 시 chime 즉시 재생 구현
- [ ] Graceful Degradation 테스트

### Phase 5: 최적화 + 안정화 (2주)
- [ ] Prometheus + Grafana 모니터링
- [ ] GPU 타임쉐어링 최적화
- [ ] 장기 안정성 테스트 (메모리 누수, 열 관리)
- [ ] XVF3800 AEC 잔여 에코 테스트 → 오탐 없으면 BARGE_PENDING 상태 제거 (2단계→1단계 단순화)

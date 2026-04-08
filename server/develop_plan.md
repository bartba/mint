# Server 개발 계획 (Jetson Orin Nano 8GB)

## Phase 1: 기반 인프라

- `proto/voice_service.proto` — gRPC 계약 정의 + Python stub 생성
- `shared/config.py` — YAML 설정 로딩 (default.yaml + server.yaml), 환경변수, get_config() 싱글턴
- `shared/models.py` — 데이터클래스 + to_proto()/from_proto() 변환
- `shared/utils.py` — setup_logging(), PCM↔float32 변환, chunk_bytes()
- `config/default.yaml` — 공유 기본값 (로그 레벨, gRPC 포트)
- `config/server.yaml` — STT/TTS/Cloud/gRPC/모니터링/전력/로깅 설정, 타임아웃 메시지(ko/en), 로컬 인텐트
- `scripts/setup_server.sh` — JetPack 확인, 전력 모드, swap, venv, proto stub 생성

## Phase 2: 핵심 AI 서비스

- `server/monitoring/system_reader.py` — sysfs 직접 읽기 (GPU load, 메모리, 온도). check_memory_safe() 제공
- `server/stt/whisper_service.py` — faster-whisper, GPU FP16, ~3GB. transcribe() + transcribe_stream(). run_in_executor() 비동기 래핑
- `server/tts/supertonic_service.py` — Supertonic TTS, ONNX Runtime, ~305MB. 한국어+영어 단일 모델. synthesize() + synthesize_stream(문장 단위 즉시 변환). _split_sentences()로 문장/절 분리

## Phase 3: Cloud LLM 연동

- `server/cloud/prompt_templates.py` — 한국어/영어 시스템 프롬프트. 짧은 호응 시작 유도, 문장 길이 제한, 마크다운/이모지 금지
- `server/cloud/llm_client.py` — LangChain 기반 Claude/GPT 스트리밍. 대화 히스토리 관리(max 20턴). clear_history(). API 실패 시 폴백 메시지

## Phase 4: gRPC 서버 + 오케스트레이터

- `server/orchestrator.py` — 모델 순서 로드(큰 것부터, 메모리 확인), 인텐트 라우팅(check_local_intent), 모델 티어링(select_model_tier), 타임아웃 메시지 제공, graceful shutdown
- `server/grpc_server.py` — ProcessVoice(오디오→STT→인텐트→LLM→TTS), TimeoutPrompt(로컬 TTS), EndSession(TTS+히스토리 초기화), HealthCheck
- `server/main.py` — 로깅→설정→Orchestrator→Prometheus(9090)→gRPC(50051)→signal handler

## Phase 5: 모니터링, Docker

- `server/monitoring/metrics.py` — Prometheus Histogram(STT/TTS/Cloud 레이턴시) + Gauge(GPU/메모리/온도). 백그라운드 폴링 없음. HealthCheck 호출 및 ProcessVoice 시작/완료 시 온디맨드로 갱신
- `server/Dockerfile` — 경량 Python 이미지, ONNX Runtime, 포트 50051/9090
- `docker-compose.yaml` — runtime: nvidia, network_mode: host, 환경변수(API키)

## Phase 6: 대화 이력 저장

- `server/storage/models.py` — SessionRecord (id, history_json, summary, language, turn_count, created_at, ended_at)
- `server/storage/session_repository.py` — SQLite/SQLAlchemy. save_session(종료 시), get_session, search_sessions(FTS5), get_recent_sessions

---

## 파일 생성 순서

```
Phase 1: proto/ → shared/ → config/ → scripts/
Phase 2: server/monitoring/system_reader → server/stt/ → server/tts/
Phase 3: server/cloud/prompt_templates → server/cloud/llm_client
Phase 4: server/orchestrator → server/grpc_server → server/main
Phase 5: server/monitoring/metrics → Dockerfile → docker-compose
Phase 6: server/storage/
```

## 리스크

- **Supertonic ARM64 호환성** → Phase 2 초기에 ONNX Runtime ARM64 wheel 검증
- **통합 메모리 8GB 부족** → check_memory_safe() + Whisper INT8 자동 폴백
- **동기 추론 블로킹** → 모든 추론 run_in_executor() 래핑
- **Cloud API 지연 변동** → 문장 단위 TTS 파이프라이닝으로 첫 응답 최소화

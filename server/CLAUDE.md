# CLAUDE.md — Server (Jetson Orin Nano 8GB)

## 역할

AI 추론 전용 서버. Edge로부터 오디오 수신 → STT → 인텐트 체크 → Cloud LLM → TTS → Edge에 오디오 반환.

## 핵심 구조

- `orchestrator.py` — 요청 조율, 인텐트 라우팅, 모델 티어링 판단
- `grpc_server.py` — Edge 요청 수신 (ProcessVoice, TimeoutPrompt, EndSession, HealthCheck)
- `stt/whisper_service.py` — Whisper-large-v3-turbo (faster-whisper, GPU, float16)
- `tts/supertonic_service.py` — Supertonic TTS (ONNX, 한국어+영어 단일 모델, CPU)
- `cloud/llm_client.py` — LangChain 기반 Claude/GPT 스트리밍 (astream)
- `cloud/prompt_templates.py` — 한국어/영어 시스템 프롬프트
- `monitoring/` — sysfs GPU/메모리/온도 읽기, Prometheus 메트릭. 백그라운드 폴링 없음, HealthCheck/ProcessVoice 시 온디맨드 갱신

## 설계 원칙

- **리소스 분리**: STT는 GPU(faster-whisper), TTS는 CPU/ONNX(Supertonic). 충돌 없음.
- **메모리 예산**: OS 1.5GB + Whisper 3GB + TTS 0.3GB + 버퍼 0.5GB = 5.3GB (여유 2.7GB)
- **모델 티어링**: 기본 Haiku, 승격 조건(깊이 키워드, 복합 질문) 시 Sonnet
- **문장/절 단위 TTS**: LLM 스트리밍 응답을 문장 종결 부호 + 쉼표(10자+)에서 분리, 즉시 TTS 변환
- **인텐트 라우팅**: STT 결과를 키워드/정규식 매칭 → 성공 시 Cloud 스킵, 로컬 TTS 응답
- **대화 종료 인텐트 매칭 시** 응답 TTS 전송 후 대화 히스토리 자동 초기화
- **프롬프트 설계**: 첫 문장을 짧은 호응으로 시작 유도, 30자/15words 이내 문장, 3~5문장, 마크다운/이모지 금지

## 주요 의존성

faster-whisper, ctranslate2, supertonic, onnxruntime, grpcio, langchain, langchain-anthropic, langchain-openai, prometheus-client

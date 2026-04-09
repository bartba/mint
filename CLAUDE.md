# CLAUDE.md — Voice Assistant Project

## 프로젝트 개요

3-Tier 분산 AI 음성 비서: Edge → Server → Cloud.
음성 입력을 실시간 처리하고, 질문 언어(한국어/영어)에 맞춰 해당 언어 음성으로 응답.

## 지원 디바이스

Edge와 Server 각각 여러 디바이스를 지원하며, 실행 시 `--device` 인자로 선택한다.
디바이스별 설정(메모리 예산, GPIO, sysfs 경로 등)은 `shared/device_profiles.py`에 정의한다.
새 디바이스 추가 시 프로파일만 등록하면 나머지 코드는 변경 없이 동작해야 한다.

| 역할 | 디바이스 | RAM | 식별자 |
|------|----------|-----|--------|
| Edge | Jetson Nano 4GB | 4GB | `jetson_nano` |
| Edge | Raspberry Pi 4B 8GB | 8GB | `raspberry_pi_4b` |
| Server | Jetson Orin Nano 8GB | 8GB | `jetson_orin_nano` |
| Server | Jetson Orin NX 16GB | 16GB | `jetson_orin_nx` |

실행 예:
```bash
python main.py --edge   --device raspberry_pi_4b
python main.py --server --device jetson_orin_nano
```

## 아키텍처 원칙

- **Edge**: 사용자 I/O (마이크/스피커 ReSpeaker XVF3800 입출력, openWakeWord, Silero VAD, Supertonic TTS)
  - 사용자 음성을 캡처하여 Server로 전송하고, Server가 보내준 텍스트를 TTS로 재생한다.
  - VAD로 발화 구간을 감지하고, wake word로 대화를 시작한다.
  - TTS는 Edge에서 실행한다. Server로부터 텍스트 문장을 수신하면 즉시 TTS 변환 후 재생한다.
  - 재생 중에도 다음 문장을 수신하여 큐잉하므로, 응답 재생이 끊기지 않는 파이프라인 구조이다.

- **Server**: AI 추론 (Whisper-large-v3-turbo STT, 로컬 인텐트 추론 [Gemma4:e2B], Cloud API 중계, FastAPI 대시보드)
  - Edge에서 받은 오디오를 STT로 텍스트 변환한다.
  - STT 결과를 로컬 LLM(Gemma4:e2B)에 전달하여 사용자 의도(인텐트)를 분석한다.
  - 인텐트가 감지되면 Cloud 호출 없이 즉시 결과 텍스트를 Edge로 전송한다.
  - 인텐트 미감지 시 Cloud LLM에 전달하고, 스트리밍 응답을 문장 단위로 Edge에 전송한다.
  - FastAPI로 간단한 대시보드(입출력 모니터링)를 제공한다.

- **Cloud**: LLM 대화/응답 생성 (Claude, GPT, and more)
  - 복잡한 대화/질의에 대해 고품질 응답을 생성한다.
  - Server/Cloud 단절 시 Server 로컬 LLM으로 폴백한다.

## 데이터 흐름

```
[Edge]                         [Server]                        [Cloud]
마이크 캡처                                                      
  → audio stream (gRPC) ──────→ STT (Whisper)
                                  → 텍스트
                                  → 로컬 LLM 인텐트 분석
                                    ├─ 인텐트 감지 → 결과 텍스트 ──→ Edge
                                    └─ 인텐트 없음 → Cloud LLM 호출 ──→ 스트리밍 응답
                                                    토큰 누적 → 문장 단위 텍스트 ──→ Edge
  ← text (gRPC) ──────────────
TTS 재생 (Supertonic)
  (수신 문장 큐잉, 재생 중 다음 문장 수신 시 연속 재생)
```

핵심 포인트:
- Server → Edge 응답은 **텍스트**이다 (오디오가 아님). TTS는 Edge에서 수행한다.
- Cloud LLM의 스트리밍 응답은 문장 부호(. ? ! 등)와 토큰 수량을 기준으로 문장 단위로 분리하여 Edge에 전송한다.
- Edge는 수신한 텍스트를 즉시 TTS 변환/재생하며, 재생 중에도 다음 문장을 수신하여 파이프라인이 끊기지 않도록 한다.
- 로컬 인텐트 매칭 시 Cloud를 스킵하여 지연을 최소화한다.
- 대화 타임아웃: Edge가 타이머 관리 (15초 무발화 → 의사확인, 5초 추가 무발화 → 세션 종료).
- TTS 재생 중 타임아웃 타이머 중지. IDLE 전환 시 Server 대화 히스토리 초기화.

## 통신 계약

- Edge → Server: gRPC, **오디오 데이터** (PCM 16kHz mono, 기존과 동일)
- Server → Edge: gRPC, **텍스트 데이터** (STT 결과 + 응답 문장)
- Server → Cloud: HTTPS (LangChain astream)
- 상세 메시지 정의는 `proto/voice_service.proto` 참조

## 로컬 인텐트 시스템

Server의 로컬 LLM(Gemma4:e2B)이 사용자 발화의 의도를 분석하여 라우팅한다.
시스템 프롬프트를 상세하게 작성하여, 특정 command 실행 의도가 있는지 판단한다.

- 인텐트 감지 시: Cloud LLM 호출 없이, 인텐트별 지정된 로직을 실행하고 결과 텍스트를 Edge에 반환.
- 인텐트 미감지 시: Cloud LLM에 발화 내용을 전달.

현재 구현된 인텐트:
| 인텐트 | 동작 | Edge 수신 텍스트 |
|--------|------|------------------|
| 대화 세션 종료 | Server → Edge에 `end_session` 신호(TextResponse.type) 전송 | ko: "대화를 종료합니다. 언제든 다시 불러 주세요" / en: "End of conversation. Call me anytime" |

인텐트는 향후 계속 추가될 수 있다 (예: 볼륨 조절, 타이머 설정 등).

## 코드 규약

- Python 3.10+, type hints 필수, asyncio 기반
- snake_case (함수/변수), PascalCase (클래스)
- gRPC 호출은 try/except + 폴백 필수
- API 키는 환경변수/.env, 코드 하드코딩 금지
- 로깅: Python logging, 레벨은 config 관리
- 코드는 단순하고 간결하게. 복잡성 대비 성능 향상이 작은 최적화는 지양
- 가독성 우선. 3줄 중복이 불필요한 추상화보다 낫다
- Commit prefix: `edge:`, `server:`, `proto:`, `shared:`, `docs:`, `infra:`
- proto 변경 시 양쪽 CLAUDE.md도 함께 업데이트

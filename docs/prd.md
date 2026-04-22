# PRD — Voice Assistant (Mint)

> 본 문서는 제품 요구 사항(Product Requirements Document)이다. 무엇을(WHAT) 만들 것인가와 왜(WHY)가 중심이며, 구현 디테일(HOW)은 [README.md](../README.md), [CLAUDE.md](../CLAUDE.md)에 위임한다.
>
> 상태: **Draft v0.3** — Open Questions 정리 완료.

---

## 1. 배경 및 문제 정의

### 1.1 배경
- 기존 클라우드 음성 비서는 **고정 클라우드 종속**, **프라이버시 통제 부재**, **확장 불가능한 인텐트 세트** 에 머물러 있다.
- Edge GPU/NPU의 가용성이 높아져 **STT/TTS/임베딩 분류**를 사용자 측에서 처리할 수 있게 되었고, LLM은 클라우드 또는 로컬에서 선택할 수 있다.

### 1.2 풀고자 하는 문제
1. **환경 다양성** — 동일 코드베이스로 가정·산업 현장 등 다양한 환경에 배포할 수 있어야 한다.
2. **프라이버시** — 대화 데이터는 로컬에 머물고, 외부에는 추론 시점 텍스트만 전송한다.
3. **확장성** — 새 인텐트(특히 디바이스 제어)를 코드 변경 최소화로 추가할 수 있어야 한다.
4. **연속성** — 응답 끊김 없는 파이프라인.
5. **장애 강건성** — LLM 장애에서도 사용자 흐름이 끊기지 않아야 한다.

### 1.3 비전 한 문장
> "사용자가 한 번 호출하면, 환경에 맞는 페르소나로 즉시 응답하고, 끊김 없이 대화를 이어가며, 명령으로 주변 디바이스를 제어할 수 있는 Edge–Server–Cloud 분산 음성 비서."

---

## 2. 사용자 및 사용 환경

### 2.1 페르소나

| 페르소나 | 환경 | 핵심 니즈 | 시스템 프롬프트 톤 |
|----------|------|-----------|-------------------|
| **가정 사용자** | 거실/주방 | 일상 질의, 타이머, 가전 제어, 빠른 반응 | 친근·구어체 |
| **산업 현장 운영자** | 공장 라인 옆, 소음 환경 | 장비 상태 조회/제어, 알람 응답 | 명령 지향·짧음 |

### 2.2 배포 단위
- **Edge ↔ Server는 1:1 페어링.** 한 명의 사용자 = 한 세트.
- 한 물리 서버 안에서 여러 Edge–Server 페어를 **컨테이너로 다중 호스팅** 가능 (각 페어는 독립 세션).

### 2.3 환경 적응 방식
환경별 차이는 **코드 분기 없이** 두 파일로만 흡수한다.

| 설정 파일 | 역할 |
|-----------|------|
| `config/system_prompt.txt` | 배포 환경에 맞는 페르소나·톤·제약 기술. 배포 시 교체. |
| `config/server.yaml` | 런타임 파라미터 (LLM 프로바이더, 지연 예산, 포트 등). |

---

## 3. 성공 지표 (KPI)

| 카테고리 | 지표 | 목표 |
|----------|------|------|
| 응답 속도 | 발화 종료→첫 TTS 음성까지 P95 | ≤ 2.5s |
| 응답 속도 | STT 단계 P95 | ≤ 600ms (Whisper-large-v3-turbo, GPU) |
| 응답 속도 | 임베딩 인텐트 분류 P95 | ≤ 80ms (CPU) |
| 정확도 | 인텐트 정확률(일치 발화) | ≥ 95% |
| 정확도 | 인텐트 오탐률(LLM이어야 할 발화) | ≤ 3% |
| 가용성 | 24h 무중단 가동 | ≥ 99% |
| 강건성 | LLM 장애 시 사용자에게 응답 도달율 | 100% (인텐트 또는 안내 메시지) |

---

## 4. 범위

### 4.1 MVP (P0)

**Edge**
- 마이크 캡처, ReSpeaker XVF3800 I/O.
- openWakeWord (단일 고정 wake word).
- Silero VAD — 발화 구간 감지.
- Supertonic TTS 파이프라인 — 문장 수신 즉시 변환·재생, 큐잉.
- 60s/+5s 타임아웃 로컬 처리 (Server 호출 없음).

**Server**
- Whisper-large-v3-turbo STT (GPU).
- 임베딩 인텐트 분류 — `sentence-transformers`, CPU, ~50ms.
- **단일 LLM 프로바이더** — `config/server.yaml`의 `llm.provider`로 선택 (Claude | GPT | local 중 하나). 런타임 중 전환 없음.
- `display_enabled` 출력 모드 (`false` / `true`) 지원.
- FastAPI 대시보드 (`port 8080`) — WebSocket 라이브 뷰, BasicAuth.
- SQLite 세션 저장 (저장만, Resume UI는 P1).

**인텐트 (P0)**
| 인텐트 | 동작 |
|--------|------|
| `end_session` | 세션 종료 신호 → 히스토리 초기화 → SQLite 저장 |
| `pause_listening` | **10분 고정** 일시정지 → PAUSED 상태 → 10분 후 또는 wake word로 LISTENING 복귀 |

**배포**
- Docker / docker-compose. Edge용·Server용 이미지 분리.

### 4.2 Phase 2 (P1)
- **디바이스 제어 인텐트** — MQTT reference 어댑터, `devices.yaml` 매핑.
- **`list_sessions` 인텐트** — SQLite 조회, 번호 선택으로 맥락 복원.
- Home Assistant 어댑터 추가.
- 인텐트 추가: 타이머/알람, 볼륨/재생 제어.
- 세션 subject LLM 자동 요약.
- OTA 업데이트 메커니즘.

### 4.3 Phase 3 (P2)
- Modbus/OPC-UA 어댑터 (산업 장비 프로토콜).
- 소음 환경용 wake word 모델/오디오 전처리.
- Chroma 도입 — 의미 검색 인텐트 (`search_past_conversations`).

### 4.4 Backlog (P3+)
- 다중 화자 공유 세션.
- 화자 분리/사용자 인증.
- 일본어/중국어 등 언어 확장.
- 외부 모니터링(Prometheus/Loki) 연동.
- 커스텀 wake word 학습.
- LLM 자동 fallback chain.
- 사용자 데이터 삭제 인텐트 ("지난 대화 다 잊어줘").
- gRPC mTLS.

### 4.5 Out of Scope (명시적 제외)
- **Barge-in** (TTS 재생 중 사용자 발화 가로채기) — 복잡도 대비 가치 낮음. 영구 제외 확정.
- 클라우드 호스팅 SaaS 형태 제공.
- 모바일 앱 클라이언트.

---

## 5. 핵심 기능 상세

### 5.1 음성 입출력 파이프라인
- wake word 호출 → VAD 발화 종료 감지 → Server gRPC 스트리밍 → STT → 인텐트 분류 또는 LLM → 응답 텍스트 수신 즉시 TTS 변환·재생.
- 재생 중 다음 문장 수신·큐잉 → 끊김 없는 파이프라인.
- **언어**: 한국어/영어. 발화별 Whisper 자동 감지 → 응답 언어 동기.
- **타임아웃**: 60s 무발화 → 의사확인, +5s 무발화 → 세션 종료. Edge 로컬 처리.

### 5.2 인텐트 시스템
- **분류기**: `paraphrase-multilingual-MiniLM-L12-v2`, 코사인 유사도 ≥ 0.75 → 확정, CPU ~50ms.
- **확장**: `config/intents.yaml`에 예시 문장 추가 + `server/intent_handlers.py`에 핸들러 추가.

#### `pause_listening` 상세
- 10분 고정 (`duration_s = 600`). 정규식 추출 불필요.
- PAUSED 진입: 오디오 전송 OFF, wake word만 ON, 히스토리 유지.
- 복귀 조건: 10분 만료 또는 wake word 감지 → LISTENING. 만료 시 "다시 듣고 있어요" 안내.

### 5.3 LLM — 단일 프로바이더 선택
```yaml
llm:
  provider: anthropic       # anthropic | openai | local
  model: claude-sonnet-4-6  # 프로바이더별 모델명
  # local 시: base_url, model만 추가
```
- **런타임 전환 없음.** 배포 시 provider를 한 번 선택.
- LLM 호출 실패(5xx, 타임아웃) → "지금 응답할 수 없어요. 잠시 뒤 다시 불러 주세요" 안내 후 세션 종료.
- fallback chain은 Backlog.

### 5.4 출력 모드 (`display_enabled`)
- **`false` (기본)**: 단일 섹션 음성 전용. 토큰을 TTS에 직접 연결.
- **`true`**: 이중 섹션.
  - `[SPOKEN]` 먼저: 구어체 3~5문장, 마크다운 금지 → TTS.
  - `[DISPLAY]` 이후: 상세 응답, 마크다운 허용 → Dashboard WebSocket.
- **SPOKEN은 두 모드 모두에서 항상 TTS로 전달된다.** DISPLAY만 토글.
- 첫 음성 응답 지연 증가 ≤ 100ms (`display_enabled: true`여도 SPOKEN 먼저 생성).

### 5.5 세션 저장
- 세션 종료(`end_session` 또는 타임아웃) 시 `SessionRecord` → SQLite(`data/sessions.db`) 저장.
- P0 `subject`: 첫 사용자 발화 텍스트 (LLM 요약은 P1).
- Resume(`list_sessions` 인텐트, 번호 선택, 히스토리 복원)은 P1.

### 5.6 Dashboard
- FastAPI + WebSocket, port `8080`.
- BasicAuth (`.env`의 `DASHBOARD_USER` / `DASHBOARD_PASS`).
- 표시: 사용자 발화, 어시스턴트 응답(스트리밍 → 완료 시 마크다운 렌더), 시스템 상태.
- `display_enabled: false` 시에도 대화 모니터링 가능 (DISPLAY 섹션만 없음).

### 5.7 장애 UX

| 상황 | 동작 |
|------|------|
| LLM 5xx / 타임아웃 | "지금 응답할 수 없어요" 안내 → 세션 종료 |
| STT 실패 | "잘 못 들었어요, 다시 말씀해 주세요" |
| Edge↔Server gRPC 단절 | Edge 로컬 안내 + IDLE 복귀 |
| TTS 실패 | 텍스트는 Dashboard 도달, 음성은 무음 + 한 차례 재시도 |

---

## 6. 시스템 아키텍처 요약

상세 다이어그램·컴포넌트 책임은 [README.md](../README.md) 참조.

- **Edge ↔ Server**: gRPC, 1:1. 한 물리 서버에서 컨테이너로 N 페어 호스팅 가능.
- **Server → Cloud**: HTTPS (LangChain astream).
- **Server → Dashboard**: WebSocket.
- **TTS 위치**: Edge. Server는 텍스트만 전송.
- **GPU**: Server의 Whisper 전용. 임베딩 분류기는 CPU.

---

## 7. 데이터 모델 (요약)

| 엔티티 | 위치 | 핵심 필드 | 보존 정책 |
|--------|------|-----------|-----------|
| `SessionRecord` | SQLite (`data/sessions.db`) | `id`, `subject`, `language`, `turn_count`, `history_json`, `created_at`, `ended_at` | 무기한 (사용자 명시 삭제 시까지) |
| `Conversation history` | Server 메모리 | LangChain `Messages` | 세션 종료/IDLE 시 클리어 |
| `Audio (raw PCM)` | **저장하지 않음** | — | 디스크 저장 없음 |

---

## 8. 비기능 요구사항

### 8.1 성능
3절 KPI 표 참조.

### 8.2 가용성·강건성
- 모든 외부 호출(gRPC, HTTPS)은 try/except + 에러 처리 필수.
- LLM 장애 시 사용자에게 안내 메시지 반드시 도달.
- Server 재시작 시 진행 중 세션 손실 허용 (저장된 과거 세션은 복원 가능).

### 8.3 보안
- API 키는 `.env`/환경변수만. 코드·이미지에 미포함.
- Dashboard는 BasicAuth 필수. 비밀번호는 `.env`로 주입.
- LAN 내부 가정. 외부 노출은 사용자가 별도 reverse proxy 구성.
- gRPC mTLS — Backlog.

### 8.4 프라이버시
- 원본 PCM 오디오는 디스크에 기록하지 않는다.
- Cloud LLM으로는 추론 시점 텍스트만 전송. 음성 원본 전송 없음.
- 사용자 데이터는 로컬 SQLite만. 외부 백업 없음.

### 8.5 다국어
- 한국어/영어. 발화별 자동 감지·응답 동기.

### 8.6 운영
- **배포**: Docker / docker-compose. Edge용·Server용 이미지 분리.
- **로깅**: stdout + 파일 로테이션. 레벨은 config 관리.
- **모니터링**: Dashboard 화면(LLM 상태). 외부 메트릭 수집은 Backlog.
- **OTA**: P1.

---

## 9. 트레이드오프 및 의사결정 근거

| 결정 | 채택 | 대안 | 이유 |
|------|------|------|------|
| LLM | 단일 프로바이더, 배포 시 선택 | 멀티/자동 fallback | 복잡도 대비 가치 낮음. fallback은 Backlog |
| 인텐트 P0 | end_session + pause(10분 고정) | pause duration 가변 | 정규식 추출 제거, 상태 머신 단순화 |
| 디바이스 제어 | P1 | P0 | P0 범위 집중. 어댑터 인터페이스는 P0에서 설계만 |
| 분류기 | sentence-transformers 임베딩 | LLM 기반 분류 | 빠름(50ms), 결정적, 할루시네이션 없음, GPU 비점유 |
| 저장소 | SQLite (P0) → +Chroma (P2) | 처음부터 Chroma | 현재 요구는 정형 조회. 의미 검색 인텐트 시점에 추가 |
| TTS 위치 | Edge | Server | 음성 데이터 네트워크 전송 회피, 첫 문장 지연 단축 |
| Wake word | 디바이스당 단일 고정 | 다중 동시 감지 | CPU 부하·오인식 ↓ |
| Barge-in | 미지원 | 지원 | 복잡도 대비 가치 낮음. SPEAKING 종료 후 자동 LISTENING 복귀 |
| 환경 분기 | 시스템 프롬프트 파일 교체 | 다중 프로파일 시스템 | 코드 분기 없음. 운영 복잡도 최소화 |

---

## 10. 리스크

| 리스크 | 영향 | 대응 |
|--------|------|------|
| Whisper-large-v3-turbo 메모리 부족 (Orin Nano 8GB) | STT 미구동 | `int8/int4` 양자화 또는 `medium` 폴백 모델 옵션 |
| 다중 컨테이너 호스팅 시 GPU 경합 | 지연 증가 | 컨테이너당 GPU 슬롯 명시(`--gpus`), 페어 수 상한 권고 |
| local LLM 첫 토큰 지연이 큼 | UX 저하 | 배포 전 latency 측정. 지연이 목표 초과 시 더 작은 모델 선택 |
| 인텐트 오탐으로 LLM 발화가 인텐트로 분류 | UX 저하 | 임계값(0.75) 튜닝 + 오탐 케이스 예시 문장 추가 |

---

## 11. Open Questions (결정 필요)

1. **P1 디바이스 제어 reference 어댑터** = MQTT vs Home Assistant 직접? — P1 진입 시 타겟 디바이스 1~2개와 함께 결정.

---

## 12. 단계별 로드맵

| Phase | 주요 산출물 | 검증 |
|-------|-------------|------|
| **P0-A** 파이프라인 | wake → VAD → STT → LLM → TTS 동작, 단일 LLM 프로바이더, Docker 패키징 | 발화 → 음성 응답 end-to-end |
| **P0-B** 인텐트·세션 | `end_session`, `pause_listening`(10분), SQLite 저장 | "대화 끝내줘" → 종료·저장, "잠깐 멈춰" → PAUSED 10분 |
| **P0-C** 대시보드 | FastAPI + WebSocket + BasicAuth, `display_enabled` 양방향 | `display_enabled: true` → 대시보드 실시간 DISPLAY 렌더 |
| **P1-A** 디바이스 제어 | reference 어댑터(TBD), `devices.yaml` YAML 직접 편집으로 디바이스 등록 | "거실 불 켜줘" → 명령 성공 |
| **P1-B** Resume | `list_sessions` 인텐트, 번호 선택, 히스토리 복원 | "지난 대화 보여줘" → 선택 → 맥락 복원 |
| **P1-C** 운영 | OTA 업데이트, Home Assistant 어댑터 | 무중단 롤오버 |
| **P2-A** 산업 확장 | Modbus/OPC-UA 어댑터, 소음 강건 wake word | PLC 1종 read/write |
| **P2-B** 의미 검색 | Chroma 인덱스, `search_past_conversations` 인텐트 | 자연어 질의로 과거 세션 검색 |

각 phase 완료 후 단계별 회고 → 다음 phase 범위 재조정.

---

## 13. 참고 문서

- [README.md](../README.md) — 프로젝트 개요·아키텍처·데이터 흐름·인텐트 표
- [CLAUDE.md](../CLAUDE.md) — 코드 규약·확장 절차
- `proto/voice_service.proto` — gRPC 계약

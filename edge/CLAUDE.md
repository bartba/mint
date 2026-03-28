# CLAUDE.md — Edge (NVIDIA Jetson Nano 4GB)

## Role

사용자 접점 디바이스. 마이크 입력, 스피커 출력, 경량 추론(VAD/Wake word)을 담당한다. 무거운 AI 추론은 Server에 위임하고, Server/Cloud 불가 시 오프라인 폴백을 제공한다.

## Hardware context

| Item | Detail |
|------|--------|
| Board | Jetson Nano 4GB, Cortex-A57×4 @1.43GHz, 128 CUDA (Maxwell), 4GB LPDDR4 |
| OS | JetPack 4.x (Ubuntu 18.04 기반) 또는 커뮤니티 Ubuntu 20.04 |
| Mic | ReSpeaker XVF3800 USB — `/dev/snd` (USB Plug&Play, 드라이버 불필요) |
| Speaker | 3.5mm AUX → XVF3800 AUX 출력 잭 직결 (HW AEC 활용) |
| Network | GbE → Server(Jetson Orin Nano), Internet → Cloud(via Server) |

**리소스 제약**: CPU <70% (활성시), RAM <3GB, 온도 <80°C. 128 CUDA 코어가 있지만 메모리 제약으로 로컬 추론은 CPU ONNX 사용.

## Dependencies

```txt
# requirements.txt
pyaudio>=0.2.14
numpy>=1.26
onnxruntime>=1.17          # Silero VAD, openWakeWord
openwakeword>=0.6
grpcio>=1.60
grpcio-tools>=1.60
protobuf>=4.25
pyyaml>=6.0
prometheus-client>=0.20
```

**시스템 패키지**:
```bash
sudo apt install -y portaudio19-dev python3-pyaudio alsa-utils libopus-dev
```

## Architecture

```
main.py (asyncio event loop)
  │
  ├── orchestrator.py ─────── 전체 상태 머신, 폴백 판단
  │     │
  │     ├── grpc_client.py ── Server 통신 (ProcessVoice, HealthCheck)
  │     │
  │     ├── audio/
  │     │   ├── capture.py ── XVF3800 마이크 캡처 (PyAudio, 16kHz mono)
  │     │   ├── playback.py ─ 스피커 출력 (XVF3800 AUX, 스트리밍 재생)
  │     │   ├── vad.py ────── Silero VAD (ONNX, ~5ms)
  │     │   └── wakeword.py ─ openWakeWord (ONNX, ~10ms)
  │     │
  │     ├── ui/
  │     │   └── display.py ── (향후 확장) 상태 표시
  │     │
  │     └── cache/
  │           └── *.wav ───── 오프라인 응답 캐시
  │
  └── config (from config/edge.yaml + config/default.yaml)
```

## State machine

```
[IDLE] ──wake word──► [LISTENING] ──VAD end──► [PROCESSING] ──response──► [SPEAKING] ──done──► [IDLE]
  │                       │                        │                          │
  │                      └──timeout──► [IDLE]     ├──server OK──► gRPC STT   ├──barge-in──► [BARGE_PENDING]
  │                                                └──server DOWN──► fallback │                  │
  └──health check fail──► [DEGRADED]                                          │   confirmed      cancelled
                             │                                                │   (500ms+)       (<300ms)
                             └──recovery──► [IDLE]                            │      │              │
                                                                              │      ▼              ▼
                                                                              │  [LISTENING]    [SPEAKING]
                                                                              │  (TTS 중지,     (TTS 볼륨 복원,
                                                                              │   버퍼 포함      재생 계속)
                                                                              │   STT 시작)
                                                                              │
                                                                              └──done──► [IDLE]
```

**상태 설명**:
- `IDLE`: 웨이크워드 대기. VAD는 꺼짐, openWakeWord만 상시 실행.
- `LISTENING`: 웨이크워드 감지 후 VAD 활성화. 발화 종료 대기.
- `PROCESSING`: Server STT 실행 대기. Cloud 응답 대기.
- `SPEAKING`: TTS 오디오 스트리밍 재생 중. VAD 기반 2단계 barge-in으로 인터럽트 감지.
- `BARGE_PENDING`: barge-in 후보 감지됨. TTS 볼륨 50% 감소 상태. 발화 지속 여부 판단 대기.
- `DEGRADED`: Server 연결 불가. 로컬 기능만 동작, 주기적 연결 재시도.

**Barge-in 메커니즘 (SPEAKING 상태에서의 사용자 인터럽트)**:

XVF3800의 하드웨어 AEC가 스피커 출력을 참조 신호로 사용하여 마이크 입력에서 에코를 제거한다. 따라서 TTS 재생 중에도 마이크 신호에는 "사용자 음성만" 남으며, 이 신호에 VAD를 적용하여 웨이크워드 없이 일반 발화로도 barge-in이 가능하다.

다만 HW AEC의 에코 제거가 100% 완벽하지 않으므로, 잔여 에코에 의한 오탐을 방지하기 위해 **2단계 barge-in**을 적용한다:

```
SPEAKING 상태
  │
  ├── VAD 상시 감시 (barge-in 임계값: 0.65, 평소 0.5보다 높음)
  │
  ├── 1단계: VAD 연속 300ms 이상 음성 감지
  │   → 상태: BARGE_PENDING
  │   → TTS 볼륨 즉시 50% 감소 (fade down)
  │   → 마이크 오디오 버퍼링 시작
  │
  ├── 2단계 (확정): 500ms 이상 발화 지속
  │   → TTS 완전 중지
  │   → 상태: LISTENING
  │   → 버퍼링된 오디오 포함하여 STT 파이프라인 시작
  │
  └── 취소: 300ms 이내 음성 소멸
      → 오탐 판단
      → TTS 볼륨 복원 (fade up)
      → 상태: SPEAKING 유지
```

**barge-in 임계값 튜닝 가이드**:
- `barge_in_vad_threshold`: 0.65 (기본). 오탐이 많으면 0.7로 상향, 인터럽트가 안 걸리면 0.6으로 하향.
- `barge_in_confirm_ms`: 300 (기본). 1단계 진입까지 연속 음성 요구 시간.
- `barge_in_commit_ms`: 500 (기본). 2단계 확정까지 총 발화 지속 시간.
- 이 값들은 config/edge.yaml에서 조정 가능하며, 실 환경 테스트를 통해 최적화해야 한다.

## Audio pipeline

### Capture (capture.py)

```python
import pyaudio
import numpy as np

RATE = 16000
CHANNELS = 1        # XVF3800 USB 모드: 처리된 단일 채널 출력
CHUNK = 512          # 32ms @ 16kHz
FORMAT = pyaudio.paInt16

class AudioCapture:
    def __init__(self, device_name: str = "ReSpeaker"):
        self.pa = pyaudio.PyAudio()
        self.device_index = self._find_device(device_name)
        
    def _find_device(self, name: str) -> int:
        """XVF3800 USB 디바이스 인덱스 탐색"""
        for i in range(self.pa.get_device_count()):
            info = self.pa.get_device_info_by_index(i)
            if name.lower() in info["name"].lower() and info["maxInputChannels"] > 0:
                return i
        raise RuntimeError(f"Audio device '{name}' not found")

    async def stream(self) -> AsyncIterator[bytes]:
        """16kHz mono PCM 청크를 yield"""
        stream = self.pa.open(
            format=FORMAT, channels=CHANNELS, rate=RATE,
            input=True, input_device_index=self.device_index,
            frames_per_buffer=CHUNK
        )
        try:
            while True:
                data = stream.read(CHUNK, exception_on_overflow=False)
                yield data
        finally:
            stream.close()
```

**XVF3800 채널 구성**: USB 펌웨어 기본 모드에서 처리된(AEC/빔포밍 적용된) 단일 채널을 출력한다. 6채널 펌웨어로 전환하면 원시 4ch + 처리 2ch를 얻을 수 있으나, 본 프로젝트에서는 기본 모드(처리된 1ch)를 사용한다.

### Playback (playback.py)

```python
class AudioPlayback:
    def __init__(self, device_name: str = "ReSpeaker"):
        """XVF3800 AUX 출력으로 재생. HW AEC가 이 출력을 참조."""
        self.device_index = self._find_output_device(device_name)
        self._volume = 1.0       # 0.0 ~ 1.0
        self._is_playing = False
        
    async def play_stream(self, audio_chunks: AsyncIterator[bytes]):
        """gRPC에서 수신한 TTS 오디오 청크를 즉시 재생 (스트리밍)"""
        # 첫 청크 도착 즉시 재생 시작 — 전체 응답 완료를 기다리지 않음
        self._is_playing = True
        try:
            async for chunk in audio_chunks:
                scaled = self._apply_volume(chunk)
                # PyAudio stream.write(scaled)
        finally:
            self._is_playing = False

    async def play_cached(self, cache_key: str):
        """오프라인 캐시 WAV 파일 재생"""
        wav_path = WAV_CACHE.get(cache_key)
        if wav_path and Path(wav_path).exists():
            # scipy.io.wavfile 또는 wave 모듈로 읽어서 재생
            pass

    def fade_down(self, target: float = 0.5, duration_ms: int = 100):
        """barge-in 1단계: 볼륨을 target으로 부드럽게 감소"""
        # 별도 스레드/태스크에서 점진적 감소 (클릭 노이즈 방지)
        self._volume = target
        
    def fade_up(self, target: float = 1.0, duration_ms: int = 100):
        """barge-in 취소: 볼륨 복원"""
        self._volume = target
        
    async def stop(self):
        """barge-in 2단계 확정 또는 웨이크워드 인터럽트: 즉시 재생 중지"""
        self._is_playing = False
        self._volume = 1.0
    
    def _apply_volume(self, pcm_data: bytes) -> bytes:
        """PCM 오디오 데이터에 볼륨 계수 적용"""
        if self._volume >= 1.0:
            return pcm_data
        samples = np.frombuffer(pcm_data, dtype=np.int16)
        scaled = (samples * self._volume).astype(np.int16)
        return scaled.tobytes()
    
    @property
    def is_playing(self) -> bool:
        return self._is_playing
```

**스피커 볼륨 설정**: XVF3800의 ALSA 볼륨이 기본값으로 낮을 수 있다.
```bash
# XVF3800 사운드카드의 PCM 볼륨 조정
alsamixer  # F6 → XVF3800 선택 → PCM-1 볼륨 100%
alsactl store  # 설정 저장
```

### VAD (vad.py)

```python
class SileroVAD:
    def __init__(self, threshold: float = 0.5, min_silence_ms: int = 700):
        """
        threshold: 음성 감지 임계값 (0.5 권장, 노이즈 환경에서 0.6)
        min_silence_ms: 발화 종료 판단까지의 무음 지속 시간
        """
        import onnxruntime
        self.session = onnxruntime.InferenceSession("models/silero_vad.onnx")
        self.threshold = threshold
        self.min_silence_ms = min_silence_ms
        self._speech_start_ms = None
        self._continuous_speech_ms = 0
        
    def process_chunk(self, audio_chunk: np.ndarray) -> dict:
        """
        Returns: {
            "is_speech": bool,
            "confidence": float,
            "speech_end": bool,          # min_silence_ms 이상 무음 → 발화 종료
            "continuous_speech_ms": int   # 현재까지 연속 음성 지속 시간 (ms)
        }
        """
        pass
        
    def set_threshold(self, threshold: float):
        """상태에 따라 임계값 동적 변경. SPEAKING 상태 진입 시 barge-in용으로 상향."""
        self.threshold = threshold
```

**상태별 VAD 임계값**:
| State | Threshold | 이유 |
|-------|-----------|------|
| LISTENING | 0.5 | 표준 감도. 사용자가 이미 말하기 시작한 상태. |
| SPEAKING | 0.65 | HW AEC 잔여 에코 오탐 방지를 위해 상향. |
| BARGE_PENDING | 0.5 | barge-in 확정 판단 중이므로 표준으로 복원. |

**XVF3800 내장 VAD vs Silero VAD**: XVF3800에도 하드웨어 VAD가 있지만, xvf_host 도구로 읽는 방식이라 Python 파이프라인과 통합이 번거롭다. Silero VAD는 ONNX로 ~5ms 추론이 가능하고 Python에서 직접 제어할 수 있으므로, 소프트웨어 VAD를 주로 사용한다. XVF3800 VAD는 보조 참조로 활용 가능.

### Wake word (wakeword.py)

```python
class WakeWordDetector:
    def __init__(self, model_path: str = "models/hey_assistant.onnx"):
        """
        openWakeWord 사용. 커스텀 웨이크워드 학습 가능.
        IDLE 상태에서 상시 실행 — CPU 부하 ~3-5%.
        """
        from openwakeword import Model
        self.model = Model(wakeword_models=[model_path])
        
    def detect(self, audio_chunk: np.ndarray) -> bool:
        """웨이크워드 감지 시 True 반환"""
        prediction = self.model.predict(audio_chunk)
        return any(v > 0.7 for v in prediction.values())
```

## gRPC client (grpc_client.py)

```python
class ServerClient:
    def __init__(self, server_addr: str = "jetson.local:50051"):
        self.channel = grpc.aio.insecure_channel(server_addr)
        self.stub = VoiceServiceStub(self.channel)
        self._connected = False
        
    async def health_check(self) -> Optional[ServerStatus]:
        """5초 간격 헬스체크. 3회 연속 실패 시 DEGRADED 상태 전환."""
        try:
            status = await self.stub.HealthCheck(Empty(), timeout=2.0)
            self._connected = True
            return status
        except grpc.aio.AioRpcError:
            self._connected = False
            return None
    
    async def process_voice(self, audio_stream: AsyncIterator[bytes]) -> AsyncIterator[VoiceResponse]:
        """오디오 스트림 전송 → Server가 STT+LLM+TTS 처리 → VoiceResponse 수신"""
        async def audio_generator():
            async for chunk in audio_stream:
                yield AudioChunk(
                    data=chunk, sample_rate=16000,
                    encoding="pcm_s16le",
                    timestamp_ms=int(time.time() * 1000)
                )

        response_stream = self.stub.ProcessVoice(audio_generator())
        async for response in response_stream:
            yield response
            
    @property
    def is_connected(self) -> bool:
        return self._connected
```

**에러 처리 규칙**: 모든 gRPC 호출은 `grpc.aio.AioRpcError`를 catch하고, 실패 시 오프라인 폴백으로 전환한다. timeout은 STT 30초(STT+LLM+TTS 전체 파이프라인 포함), HealthCheck 2초.

## Orchestrator (orchestrator.py)

```python
class EdgeOrchestrator:
    """
    핵심 상태 머신. 모든 서브시스템을 조율한다.
    """
    def __init__(self, config: dict):
        self.state = "IDLE"
        self.capture = AudioCapture()
        self.playback = AudioPlayback()
        self.vad = SileroVAD()
        self.wakeword = WakeWordDetector()
        self.server = ServerClient()
        
        # barge-in 설정
        self.barge_in_vad_threshold = config["barge_in"]["vad_threshold"]     # 0.65
        self.barge_in_confirm_ms = config["barge_in"]["confirm_ms"]           # 300
        self.barge_in_commit_ms = config["barge_in"]["commit_ms"]             # 500
        self._barge_in_buffer: list[bytes] = []
        
    async def run(self):
        """메인 이벤트 루프"""
        asyncio.create_task(self._health_check_loop())
        
        async for audio_chunk in self.capture.stream():
            if self.state == "IDLE":
                if self.wakeword.detect(audio_chunk):
                    self.state = "LISTENING"
                    self.vad.set_threshold(0.5)
                    await self.playback.play_cached("chime_wake")  # 상승 톤: 듣고 있어요
                    self._on_wake()
                    
            elif self.state == "LISTENING":
                vad_result = self.vad.process_chunk(audio_chunk)
                self._buffer_audio(audio_chunk)
                
                if vad_result["speech_end"]:
                    self.state = "PROCESSING"
                    await self.playback.play_cached("chime_ack")  # 하강 톤: 수신 확인
                    await self._process_utterance()
                    
            elif self.state == "SPEAKING":
                # VAD 기반 2단계 barge-in만 사용 (웨이크워드 체크 안 함)
                vad_result = self.vad.process_chunk(audio_chunk)
                if vad_result["is_speech"] and \
                   vad_result["continuous_speech_ms"] >= self.barge_in_confirm_ms:
                    # 300ms 이상 연속 음성 → BARGE_PENDING 전환
                    self.state = "BARGE_PENDING"
                    self.playback.fade_down(target=0.5)
                    self.vad.set_threshold(0.5)  # 확정 판단은 표준 임계값으로
                    self._barge_in_buffer = [audio_chunk]
                    
            elif self.state == "BARGE_PENDING":
                self._barge_in_buffer.append(audio_chunk)
                vad_result = self.vad.process_chunk(audio_chunk)
                
                if not vad_result["is_speech"]:
                    # 음성 소멸 → 오탐, SPEAKING 복귀
                    self.playback.fade_up(target=1.0)
                    self.vad.set_threshold(self.barge_in_vad_threshold)
                    self._barge_in_buffer.clear()
                    self.state = "SPEAKING"
                    
                elif vad_result["continuous_speech_ms"] >= self.barge_in_commit_ms:
                    # 500ms 이상 지속 → barge-in 확정
                    await self.playback.stop()
                    self.state = "LISTENING"
                    # 버퍼링된 오디오를 메인 오디오 버퍼에 추가
                    for buffered_chunk in self._barge_in_buffer:
                        self._buffer_audio(buffered_chunk)
                    self._barge_in_buffer.clear()
                    # VAD는 이미 LISTENING 임계값(0.5)으로 설정됨
                    
    async def _process_utterance(self):
        """발화 처리: Server STT → Cloud LLM → TTS 파이프라인 실행"""
        audio_data = self._get_buffered_audio()

        if self.server.is_connected:
            try:
                await self._server_pipeline(audio_data)
            except Exception:
                await self._fallback_response()
        else:
            await self._fallback_response()

        self.state = "IDLE"
        
    async def _server_pipeline(self, audio_data: bytes):
        """Server에 오디오 전송 → Server가 STT → 인텐트 체크 → LLM → TTS 일관 처리"""
        self.state = "SPEAKING"
        self.vad.set_threshold(self.barge_in_vad_threshold)  # SPEAKING 진입 시 barge-in 임계값
        async for response in self.server.process_voice(self._audio_iter(audio_data)):
            if response.HasField("stt_result"):
                # STT 결과 (참조/로깅용)
                logging.info(f"STT: {response.stt_result.text}")
            elif response.HasField("tts_audio"):
                # TTS 오디오 청크 재생
                if self.state != "SPEAKING" and self.state != "BARGE_PENDING":
                    break  # barge-in에 의해 상태 전환됨
                await self.playback.play_chunk(response.tts_audio.data)

    async def _fallback_response(self):
        """Server 불가 시 오프라인 캐시 응답"""
        self.state = "SPEAKING"
        self.vad.set_threshold(self.barge_in_vad_threshold)
        await self.playback.play_cached("offline_notice")
```

## Offline fallback

### WAV cache

```python
WAV_CACHE = {
    "offline_notice":  "cache/offline.wav",       # "지금은 연결이 어려운 상태예요. 잠시 후 다시 말씀해 주세요."
    "confirm":         "cache/confirm.wav",        # "네, 알겠습니다."
    "not_understood":  "cache/not_understood.wav",  # "죄송해요, 잘 못 알아들었어요. 다시 한번 말씀해 주시겠어요?"
    "welcome":         "cache/welcome.wav",         # "안녕하세요! 무엇을 도와드릴까요?"
    "thinking":        "cache/thinking.wav",        # "네, 잠시만 기다려 주세요."
    "error":           "cache/error.wav",           # "죄송해요, 문제가 생겼어요. 잠시 후 다시 시도해 주세요."
    "goodbye_ko":      "cache/goodbye_ko.wav",       # "네, 좋은 하루 보내세요!"
    "goodbye_en":      "cache/goodbye_en.wav",       # "Okay, have a great day!"
    "chime_wake":      "cache/chime_wake.wav",         # 상승 톤 (~200ms) — 웨이크워드 인식
    "chime_ack":       "cache/chime_ack.wav",          # 하강 톤 (~200ms) — 발화 수신 확인
}
```

**캐시 생성**: Server의 Supertonic TTS로 사전 생성 후 Edge에 배포.
```bash
# scripts/generate_tts_cache.py 실행 (Server에서)
python scripts/generate_tts_cache.py --output edge/cache/ --lang ko
# 생성된 WAV를 Edge로 복사
scp -r edge/cache/ user@jetson-nano.local:~/voice-assistant/edge/cache/
```

### Degraded mode behavior

Server HealthCheck 3회 연속 실패 시:
1. 상태를 `DEGRADED`로 전환
2. 웨이크워드 + VAD는 계속 동작
3. 발화 감지 시 `offline_notice` WAV 재생
4. 10초 간격으로 Server 재연결 시도
5. 연결 복구 시 자동으로 `IDLE` 상태 전환

## Testing

```bash
# 오디오 캡처 테스트
python -m edge.audio.capture --test --duration 5

# VAD 테스트
python -m edge.audio.vad --test --input test.wav

# VAD barge-in 테스트 (TTS 재생 중 VAD 감도 측정)
python -m edge.audio.vad --test-barge-in --threshold 0.65

# gRPC 연결 테스트
python -m edge.grpc_client --health-check --server jetson.local:50051

# 전체 파이프라인 테스트 (Server 필요)
python -m edge.main --test-mode

# barge-in 통합 테스트 (TTS 재생 + 마이크 입력 동시)
python -m edge.main --test-mode --test-barge-in

# 오프라인 폴백 테스트 (Server 없이)
python -m edge.main --test-mode --force-offline
```

## Config (config/edge.yaml)

```yaml
audio:
  device_name: "ReSpeaker"
  sample_rate: 16000
  channels: 1
  chunk_size: 512

vad:
  model_path: "models/silero_vad.onnx"
  threshold: 0.5
  min_silence_ms: 700
  max_speech_ms: 30000      # 30초 초과 발화 자동 종료

barge_in:
  enabled: true
  vad_threshold: 0.65        # SPEAKING 상태 VAD 임계값 (평소 0.5보다 높음)
  confirm_ms: 300            # 1단계 진입: 연속 음성 최소 지속 시간
  commit_ms: 500             # 2단계 확정: 총 발화 지속 시간
  fade_volume: 0.5           # 1단계에서 TTS 볼륨 감소 목표 (0.0~1.0)
  fade_duration_ms: 100      # 볼륨 fade 소요 시간

wakeword:
  model_path: "models/hey_assistant.onnx"
  threshold: 0.7

server:
  address: "jetson.local:50051"
  health_check_interval_s: 5
  health_check_timeout_s: 2
  health_check_max_failures: 3
  pipeline_timeout_s: 30       # STT + LLM + TTS 전체 파이프라인 타임아웃

cache:
  directory: "cache/"

logging:
  level: "INFO"              # DEBUG for development
  file: "logs/edge.log"
```

## Build & deploy

```bash
# Jetson Nano 초기 설정
./scripts/setup_edge.sh

# 의존성 설치
cd edge/
pip install -r requirements.txt

# 모델 다운로드
./scripts/download_edge_models.sh

# 실행
python -m edge.main

# Docker (선택)
docker build -t voice-assistant-edge -f edge/Dockerfile .
docker run --device /dev/snd --network host voice-assistant-edge
```

**Docker 주의사항**: `--device /dev/snd`로 사운드 디바이스를 패스스루해야 XVF3800에 접근 가능. `--network host`로 gRPC 포트 접근.

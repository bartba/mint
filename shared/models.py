"""
공유 데이터 모델.

proto 메시지(gRPC 통신용)와 Python 데이터클래스(내부 로직용) 사이의 변환을 담당한다.

왜 분리하는가?
    proto 객체: gRPC 전송에 최적화. 타입 힌트 부족, IDE 지원 약함.
    데이터클래스: Python 내부 로직에 최적화. 타입 힌트, 자동완성, 기본값 지원.

    서버 내부에서는 데이터클래스만 사용하고,
    gRPC 경계(수신/송신)에서만 proto ↔ 데이터클래스 변환을 수행한다.

사용 예:
    # gRPC에서 수신한 proto → 데이터클래스
    chunk_data = AudioChunkData.from_proto(proto_chunk)

    # 내부 처리 후 → proto로 변환하여 gRPC 송신
    proto_chunk = chunk_data.to_proto()
"""

from dataclasses import dataclass, field

# proto 생성 파일 import
# Phase 1.1에서 생성한 voice_service_pb2를 사용
import sys
from pathlib import Path

# proto/ 디렉토리를 Python 경로에 추가
# proto/voice_service_pb2.py를 import하려면 proto/ 폴더가 sys.path에 있어야 함
_proto_dir = str(Path(__file__).resolve().parent.parent / "proto")
if _proto_dir not in sys.path:
    sys.path.insert(0, _proto_dir)

import voice_service_pb2 as pb2


# ─────────────────────────────────────────────
# AudioChunkData — 오디오 데이터 조각
# ─────────────────────────────────────────────

@dataclass
class AudioChunkData:
    """
    오디오 데이터 한 조각.

    Edge → Server 오디오 전송, Server → Edge TTS 오디오 반환에 모두 사용.

    @dataclass 설명:
        Python 3.7+에서 제공하는 데코레이터.
        __init__, __repr__, __eq__ 등을 자동 생성해준다.

        이것 없이 직접 작성하면:
            class AudioChunkData:
                def __init__(self, data, sample_rate, encoding, timestamp_ms):
                    self.data = data
                    self.sample_rate = sample_rate
                    ...
        @dataclass를 쓰면 필드 선언만으로 위 코드가 자동 생성됨.
    """
    data: bytes = b""
    sample_rate: int = 16000
    encoding: str = "pcm_s16le"
    timestamp_ms: int = 0

    def to_proto(self) -> pb2.AudioChunk:
        """데이터클래스 → proto 메시지 변환 (gRPC 송신용)"""
        return pb2.AudioChunk(
            data=self.data,
            sample_rate=self.sample_rate,
            encoding=self.encoding,
            timestamp_ms=self.timestamp_ms,
        )

    @classmethod
    def from_proto(cls, proto: pb2.AudioChunk) -> "AudioChunkData":
        """
        proto 메시지 → 데이터클래스 변환 (gRPC 수신 후 내부 사용)

        @classmethod 설명:
            일반 메서드는 인스턴스(self)에서 호출:  obj.method()
            클래스메서드는 클래스(cls)에서 호출:    AudioChunkData.from_proto(proto)

            여기서 cls = AudioChunkData 클래스 자체.
            cls(...)는 AudioChunkData(...)와 같다.
        """
        return cls(
            data=proto.data,
            sample_rate=proto.sample_rate,
            encoding=proto.encoding,
            timestamp_ms=proto.timestamp_ms,
        )


# ─────────────────────────────────────────────
# STTResultData — 음성인식 결과
# ─────────────────────────────────────────────

@dataclass
class STTResultData:
    """
    STT(음성→텍스트) 변환 결과.

    Whisper가 오디오를 처리한 후 이 객체에 결과를 담는다.
    Server → Edge로 참조/로깅용으로 전송되고,
    동시에 Server 내부에서 Cloud LLM 호출의 입력으로도 사용된다.
    """
    text: str = ""
    confidence: float = 0.0
    is_final: bool = True
    language: str = "ko"

    def to_proto(self) -> pb2.STTResult:
        """데이터클래스 → proto 변환"""
        return pb2.STTResult(
            text=self.text,
            confidence=self.confidence,
            is_final=self.is_final,
            language=self.language,
        )

    @classmethod
    def from_proto(cls, proto: pb2.STTResult) -> "STTResultData":
        """proto → 데이터클래스 변환"""
        return cls(
            text=proto.text,
            confidence=proto.confidence,
            is_final=proto.is_final,
            language=proto.language,
        )


# ─────────────────────────────────────────────
# TTSRequestData — TTS 요청
# ─────────────────────────────────────────────

@dataclass
class TTSRequestData:
    """
    TTS(텍스트→음성) 변환 요청.

    Cloud LLM의 응답 텍스트를 TTS 서비스에 전달할 때 사용.
    proto에 직접 대응하는 메시지는 없지만 (TTS는 Server 내부 처리),
    서비스 간 데이터 전달을 위해 정의한다.
    """
    text: str = ""
    language: str = "ko"
    speed: float = 1.0


# ─────────────────────────────────────────────
# ServerStatusData — 서버 상태
# ─────────────────────────────────────────────

@dataclass
class ServerStatusData:
    """
    Server 상태 정보.

    Edge의 HealthCheck 요청에 대한 응답으로 사용.
    GPU 사용률, 메모리, 온도 등 Jetson 디바이스 상태를 담는다.
    """
    stt_ready: bool = False
    tts_ready: bool = False
    gpu_usage: float = 0.0
    memory_usage: float = 0.0
    temperature: float = 0.0

    def to_proto(self) -> pb2.ServerStatus:
        """데이터클래스 → proto 변환"""
        return pb2.ServerStatus(
            stt_ready=self.stt_ready,
            tts_ready=self.tts_ready,
            gpu_usage=self.gpu_usage,
            memory_usage=self.memory_usage,
            temperature=self.temperature,
        )

    @classmethod
    def from_proto(cls, proto: pb2.ServerStatus) -> "ServerStatusData":
        """proto → 데이터클래스 변환"""
        return cls(
            stt_ready=proto.stt_ready,
            tts_ready=proto.tts_ready,
            gpu_usage=proto.gpu_usage,
            memory_usage=proto.memory_usage,
            temperature=proto.temperature,
        )

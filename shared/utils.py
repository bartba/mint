"""
공유 유틸리티 함수.

로깅 설정, 오디오 PCM 변환, 바이트 분할 등
Edge와 Server 양쪽에서 공통으로 사용하는 헬퍼 함수를 모아둔다.

사용 예:
    from shared.utils import setup_logging, pcm_to_float32, float32_to_pcm, chunk_bytes

    setup_logging("INFO", "logs/server.log")

    float_audio = pcm_to_float32(raw_bytes)   # Whisper 입력용
    pcm_audio = float32_to_pcm(float_audio)    # gRPC 전송용

    for chunk in chunk_bytes(pcm_audio, 4096):  # 4KB씩 분할
        send(chunk)
"""

import logging
import sys
from pathlib import Path
from typing import Iterator

import numpy as np


# ─────────────────────────────────────────────
# 로깅 설정
# ─────────────────────────────────────────────

def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """
    Python 로깅을 일관된 포맷으로 설정한다.

    Args:
        level: 로그 레벨. "DEBUG", "INFO", "WARNING", "ERROR" 중 하나.
               DEBUG → 모든 메시지 출력 (개발 시)
               INFO  → 일반 동작 메시지 (프로덕션 기본)
        log_file: 로그 파일 경로. None이면 콘솔에만 출력.

    출력 포맷 예시:
        2026-03-28 14:30:05 | INFO | stt_service | Whisper model loaded (3.1GB)
        2026-03-28 14:30:06 | WARNING | model_manager | Memory usage 78%

    포맷 구성:
        %(asctime)s     → 시각 (2026-03-28 14:30:05)
        %(levelname)-8s → 레벨, 8자 왼쪽정렬 (INFO    , WARNING )
        %(name)s        → 로거 이름 (어느 모듈에서 찍었는지)
        %(message)s     → 실제 메시지
    """
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # 핸들러 목록: 최소한 콘솔(stdout)에는 항상 출력
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
    ]

    # 로그 파일이 지정되면 파일 핸들러 추가
    if log_file:
        # 로그 디렉토리가 없으면 생성
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        handlers.append(
            logging.FileHandler(log_file, encoding="utf-8")
        )

    # logging.basicConfig: 루트 로거를 한 번에 설정
    # force=True: 이미 설정된 핸들러가 있어도 덮어씀 (재설정 가능)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=log_format,
        datefmt=date_format,
        handlers=handlers,
        force=True,
    )


# ─────────────────────────────────────────────
# PCM ↔ float32 변환
# ─────────────────────────────────────────────

def pcm_to_float32(pcm_bytes: bytes) -> np.ndarray:
    """
    PCM int16 바이트 → float32 numpy 배열로 변환한다.

    AI 모델(Whisper 등)은 -1.0 ~ +1.0 범위의 float32 입력을 요구한다.
    마이크에서 캡처한 오디오는 PCM int16 (-32768 ~ +32767) 형식이므로,
    모델에 전달하기 전에 변환이 필요하다.

    변환 공식:
        float_value = int16_value / 32768.0

    왜 32768인가?
        int16의 범위가 -32768 ~ +32767 이므로,
        32768.0으로 나누면 -1.0 ~ +1.0 범위로 정규화된다.

    Args:
        pcm_bytes: PCM int16 리틀엔디안 바이트 데이터.

    Returns:
        float32 numpy 배열 (값 범위: -1.0 ~ +1.0)

    사용 예:
        raw_audio = microphone.read()              # bytes (PCM int16)
        float_audio = pcm_to_float32(raw_audio)    # np.ndarray (float32)
        result = whisper.transcribe(float_audio)    # 모델 입력
    """
    # np.frombuffer: 바이트 → numpy 배열 (복사 없이 해석만)
    # dtype=np.int16: 2바이트씩 int16으로 해석
    int16_array = np.frombuffer(pcm_bytes, dtype=np.int16)

    # .astype(np.float32): int16 → float32 타입 변환
    # / 32768.0: -1.0 ~ +1.0 범위로 정규화
    return int16_array.astype(np.float32) / 32768.0


def float32_to_pcm(float_array: np.ndarray) -> bytes:
    """
    float32 numpy 배열 → PCM int16 바이트로 변환한다.

    TTS 모델의 출력(float32)을 gRPC 전송 또는 스피커 재생을 위해
    PCM int16 바이트로 변환한다.

    변환 공식:
        int16_value = clamp(float_value * 32767, -32768, +32767)

    np.clip을 쓰는 이유:
        float 값이 -1.0 ~ +1.0을 초과할 수 있다 (모델 출력 특성).
        clip 없이 변환하면 int16 범위를 넘어 오디오가 깨진다 (클리핑 노이즈).

    Args:
        float_array: float32 numpy 배열 (값 범위: 대략 -1.0 ~ +1.0)

    Returns:
        PCM int16 리틀엔디안 바이트.

    사용 예:
        wav_output = tts_model.synthesize("안녕")   # np.ndarray (float32)
        pcm_bytes = float32_to_pcm(wav_output)      # bytes (PCM int16)
        grpc_send(pcm_bytes)                         # gRPC 전송
    """
    # 1. -1.0~+1.0 → -32767~+32767 스케일 변환
    scaled = float_array * 32767.0

    # 2. int16 범위로 클리핑 (오버플로우 방지)
    clipped = np.clip(scaled, -32768, 32767)

    # 3. float → int16 타입 변환 → 바이트로
    return clipped.astype(np.int16).tobytes()


# ─────────────────────────────────────────────
# 바이트 분할
# ─────────────────────────────────────────────

def chunk_bytes(data: bytes, chunk_size: int = 4096) -> Iterator[bytes]:
    """
    바이트 데이터를 고정 크기 조각으로 분할한다.

    gRPC 스트리밍에서 큰 오디오 데이터를 한 번에 보내지 않고,
    작은 조각(chunk)으로 나눠서 순차 전송한다.

    왜 분할하는가?
        1. gRPC 메시지 크기 제한 (기본 4MB, 우리 설정 10MB)
        2. 스트리밍 특성 활용: 전체가 준비되기 전에 앞부분부터 전송/재생 가능
        3. 메모리 효율: 전체 데이터를 한 번에 메모리에 올릴 필요 없음

    Args:
        data: 분할할 바이트 데이터.
        chunk_size: 조각 크기 (바이트). 기본 4096 (4KB).
                    마지막 조각은 chunk_size보다 작을 수 있다.

    Yields:
        chunk_size 크기의 바이트 조각.

    사용 예:
        audio = tts_service.synthesize("긴 문장입니다...")  # 큰 바이트

        for chunk in chunk_bytes(audio, 4096):
            yield VoiceResponse(tts_audio=AudioChunk(data=chunk))

    Iterator 설명:
        yield를 쓰면 함수가 "제너레이터"가 된다.
        for 루프에서 하나씩 꺼내 쓸 수 있다.
        전체 결과를 리스트로 만들지 않아 메모리 효율적이다.
    """
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]

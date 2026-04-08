"""
STT(Speech-to-Text) 서비스 — faster-whisper 기반.

Edge에서 보낸 오디오 바이트를 텍스트로 변환한다.
Jetson Orin Nano의 GPU에서 Whisper-large-v3-turbo 모델을 실행하여
한국어/영어 음성을 ~700ms 이내에 인식한다.

faster-whisper란?
    OpenAI Whisper를 CTranslate2 엔진으로 재구현한 라이브러리.
    원본 Whisper(PyTorch 기반) 대비:
        - 추론 속도 ~4배 빠름
        - 메모리 사용 ~2배 적음
        - PyTorch 의존성 제거 (CTranslate2만 필요)

    Jetson에서 PyTorch는 ~1.5GB 메모리를 차지하므로,
    faster-whisper를 쓰면 모델 외에 프레임워크 오버헤드가 크게 줄어든다.

CTranslate2란?
    Transformer 모델을 최적화하여 실행하는 C++ 추론 엔진.
    INT8/FP16 양자화, 배치 처리, CPU/GPU 자동 선택을 지원한다.
    faster-whisper의 내부 엔진으로, 직접 다룰 일은 거의 없다.

compute_type (양자화 옵션):
    "float16"      — Tensor Core 활용, 정확도 최고, ~3GB VRAM
    "int8_float16"  — 메모리 절약 (~1.5GB), 정확도 미세 하락
    "int8"         — 최소 메모리, 정확도 추가 하락

    Jetson Orin Nano의 Ampere GPU는 FP16 Tensor Core를 지원하므로
    float16이 기본 권장이다. 메모리 부족 시 int8_float16으로 폴백한다.

사용 예:
    from server.stt.whisper_service import STTService

    stt = STTService(model_size="large-v3-turbo", compute_type="float16")
    result = await stt.transcribe(audio_bytes, language="ko")
    print(result.text)        # "안녕하세요 반갑습니다"
    print(result.confidence)  # 0.95
    print(result.language)    # "ko"

관련 모듈:
    shared/utils.py — pcm_to_float32() 오디오 변환 함수
    shared/models.py — STTResultData 데이터클래스
    server/monitoring/system_reader.py — 메모리 확인 (orchestrator에서 호출)
"""

import asyncio
import logging
import time
from typing import AsyncIterator

from faster_whisper import WhisperModel

from shared.models import STTResultData
from shared.utils import pcm_to_float32

logger = logging.getLogger(__name__)


class STTService:
    """
    faster-whisper 기반 STT 서비스.

    생성 시 Whisper 모델을 GPU에 로드하고,
    transcribe() 호출 시 오디오 바이트를 텍스트로 변환한다.

    중요 — 동기 추론의 비동기 래핑:
        faster-whisper의 transcribe()는 동기 함수이다.
        GPU 추론이 ~700ms 동안 스레드를 블로킹한다.

        asyncio 이벤트 루프에서 동기 함수를 직접 호출하면?
            → 이벤트 루프가 700ms 동안 멈춘다.
            → 그 사이 다른 gRPC 요청, 타이머, 네트워크 I/O가 모두 정지.

        해결: run_in_executor()
            동기 함수를 별도 스레드에서 실행하고,
            이벤트 루프는 그 사이 다른 작업을 계속 처리한다.
            완료되면 결과를 await로 받는다.

        왜 모든 AI 추론에 이 패턴을 쓰는가?
            STT(Whisper), TTS(Supertonic) 모두 동기 라이브러리이다.
            gRPC 서버는 asyncio 기반이므로, 추론 함수마다
            run_in_executor()로 감싸야 서버가 멈추지 않는다.
    """

    def __init__(
        self,
        model_size: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        model_dir: str = "models/",
    ) -> None:
        """
        Whisper 모델을 로드한다.

        Args:
            model_size: 모델 크기.
                "large-v3-turbo" — 속도/정확도 균형 (기본 권장)
                "large-v3"       — 최고 정확도, 추론 느림
                "medium"         — 메모리 부족 시 (~1.5GB)

            device: 실행 디바이스.
                "cuda" — GPU 사용 (Jetson 기본)
                "cpu"  — CPU 사용 (테스트용)

            compute_type: 양자화 수준.
                "float16"       — FP16, ~3GB, 기본
                "int8_float16"  — INT8, ~1.5GB, 메모리 부족 시 폴백

            model_dir: 모델 다운로드 경로.
                첫 실행 시 HuggingFace에서 자동 다운로드된다.
                이후에는 이 경로에 캐시된 모델을 사용한다.

        WhisperModel 초기화 과정:
            1. model_dir에 모델이 없으면 HuggingFace에서 다운로드 (~3GB)
            2. CTranslate2 엔진으로 모델 로드
            3. compute_type에 따라 양자화 적용
            4. GPU 메모리에 모델 상주 (이후 추론 시 즉시 사용)

        주의: 이 생성자는 동기적으로 실행된다.
            모델 로드에 수 초가 걸리므로, 서버 시작 시 한 번만 호출한다.
            요청마다 생성하면 안 된다.
        """
        logger.info(
            f"Whisper 모델 로드 시작: {model_size}, "
            f"device={device}, compute_type={compute_type}"
        )

        load_start = time.time()

        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            download_root=model_dir,
        )

        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type

        load_time = time.time() - load_start
        logger.info(f"Whisper 모델 로드 완료: {load_time:.1f}초")

    # ─────────────────────────────────────────────
    # 단일 오디오 변환 (주 사용 메서드)
    # ─────────────────────────────────────────────

    async def transcribe(
        self,
        audio_data: bytes,
        language: str = "ko",
        beam_size: int = 5,
    ) -> STTResultData:
        """
        오디오 바이트를 텍스트로 변환한다.

        Edge에서 VAD로 발화 구간을 잘라 보내므로,
        이 메서드는 하나의 완결된 발화에 대해 호출된다.

        Args:
            audio_data: PCM int16 리틀엔디안 바이트.
                Edge에서 gRPC로 수신한 오디오 데이터.
                sample_rate=16000, mono.

            language: 기대 언어 코드.
                "ko" — 한국어 (기본)
                "en" — 영어
                Whisper는 언어를 지정하면 해당 언어에 최적화된 디코딩을 수행.
                생략하면 자동 감지하지만, 짧은 발화에서는 오감지 가능성 있음.

            beam_size: 빔 서치 크기.
                디코딩 시 동시에 탐색하는 후보 수.
                클수록 정확하지만 느림.
                1 = 그리디 (가장 빠름, 정확도 낮음)
                5 = 기본 (속도/정확도 균형)

                빔 서치란?
                    "안녕하세요"를 인식할 때:
                    beam_size=1: "안녕" → "하세" → "요" (하나의 경로만 탐색)
                    beam_size=5: 5개의 후보 경로를 동시에 탐색하고 최적을 선택

        Returns:
            STTResultData: 변환 결과.
                text — 인식된 텍스트
                confidence — 언어 감지 확률 (0.0 ~ 1.0)
                is_final — 항상 True (스트리밍 중간 결과 아님)
                language — 감지된 언어 코드 ("ko", "en" 등)

        내부 동작:
            1. PCM int16 바이트 → float32 numpy 배열 변환 (Whisper 입력 형식)
            2. run_in_executor()로 동기 추론을 별도 스레드에서 실행
            3. 세그먼트 결과를 하나의 텍스트로 결합
            4. STTResultData 데이터클래스로 반환

        run_in_executor() 상세:
            asyncio.get_event_loop().run_in_executor(executor, func)

            매개변수:
                executor — 스레드풀. None이면 기본 ThreadPoolExecutor 사용.
                func     — 실행할 동기 함수.

            동작:
                1. func를 스레드풀의 워커 스레드에 제출
                2. 이벤트 루프는 다른 코루틴을 계속 실행
                3. func 완료 시 결과를 Future로 반환
                4. await로 결과를 받음

            왜 None(기본 풀)을 쓰는가?
                GPU 추론은 한 번에 하나만 실행된다 (GPU는 공유 자원).
                커스텀 풀을 만들어도 GPU 병렬화 이점이 없으므로
                기본 풀로 충분하다.
        """
        # 1. PCM int16 → float32 변환
        #    shared/utils.py의 pcm_to_float32() 사용
        audio_float = pcm_to_float32(audio_data)

        # 2. 동기 추론을 별도 스레드에서 실행
        loop = asyncio.get_running_loop()
        infer_start = time.time()

        segments, info = await loop.run_in_executor(
            None,  # 기본 ThreadPoolExecutor
            lambda: self.model.transcribe(
                audio_float,
                language=language,
                beam_size=beam_size,
                vad_filter=False,       # Edge VAD 사용, 서버 VAD 비활성
                word_timestamps=False,  # 단어별 타임스탬프 불필요 (속도 향상)
            ),
        )

        # 3. 세그먼트를 하나의 텍스트로 결합
        #    Whisper는 긴 오디오를 여러 세그먼트로 나눠서 반환한다.
        #    각 세그먼트의 텍스트를 공백으로 연결한다.
        #
        #    segments는 제너레이터이므로 한 번만 순회 가능하다.
        #    list()로 감싸지 않고 직접 순회하여 메모리를 절약한다.
        text_parts = []
        for segment in segments:
            stripped = segment.text.strip()
            if stripped:
                text_parts.append(stripped)

        text = " ".join(text_parts)

        infer_time = time.time() - infer_start
        logger.info(
            f"STT 완료: {infer_time:.3f}초, "
            f"언어={info.language}({info.language_probability:.2f}), "
            f"텍스트=\"{text[:50]}{'...' if len(text) > 50 else ''}\""
        )

        # 4. STTResultData로 반환
        return STTResultData(
            text=text,
            confidence=info.language_probability,
            is_final=True,
            language=info.language,
        )

    # ─────────────────────────────────────────────
    # 스트리밍 변환 (청크 누적 → 일괄 처리)
    # ─────────────────────────────────────────────

    async def transcribe_stream(
        self,
        audio_stream: AsyncIterator[bytes],
        language: str = "ko",
        beam_size: int = 5,
    ) -> AsyncIterator[STTResultData]:
        """
        오디오 청크 스트림을 수신하여 STT 결과를 반환한다.

        현재 구현: 청크를 모두 누적한 후 한 번에 변환.
        Edge에서 VAD로 발화 구간을 잘라서 스트리밍하므로,
        스트림 종료 = 발화 종료이다.

        Args:
            audio_stream: gRPC에서 수신하는 오디오 청크의 비동기 이터레이터.
                Edge가 보내는 AudioChunk.data를 순서대로 yield한다.

            language: 기대 언어 코드.
            beam_size: 빔 서치 크기.

        Yields:
            STTResultData: 변환 결과 (현재는 1개만 yield).

        AsyncIterator 설명:
            일반 Iterator는 for로 순회:
                for item in iterator: ...

            AsyncIterator는 async for로 순회:
                async for item in async_iterator: ...

            차이점: 각 item을 가져올 때 await가 가능하다.
            → 네트워크에서 데이터가 도착할 때까지 대기하면서도
              이벤트 루프가 다른 작업을 처리할 수 있다.

        향후 확장 가능성:
            중간 결과(is_final=False)를 yield하여
            실시간 자막처럼 부분 텍스트를 보여줄 수 있다.
            현재는 Edge UI에 이 기능이 없으므로 미구현.
        """
        # 오디오 청크를 버퍼에 누적
        buffer = bytearray()

        async for chunk in audio_stream:
            buffer.extend(chunk)

        if not buffer:
            logger.warning("빈 오디오 스트림 수신 — 빈 결과 반환")
            yield STTResultData(text="", confidence=0.0, is_final=True, language=language)
            return

        logger.debug(f"오디오 버퍼 누적 완료: {len(buffer)} bytes")

        # 누적된 버퍼를 한 번에 변환
        result = await self.transcribe(bytes(buffer), language, beam_size)
        yield result


# ─────────────────────────────────────────────
# CLI 테스트
# ─────────────────────────────────────────────

if __name__ == "__main__":
    """
    단독 실행으로 STT 서비스를 테스트한다.

    사용법:
        # WAV 파일로 테스트
        python -m server.stt.whisper_service --test --input test_ko.wav

        # 언어 지정
        python -m server.stt.whisper_service --test --input test_en.wav --language en

        # INT8 모드로 테스트 (메모리 절약)
        python -m server.stt.whisper_service --test --input test_ko.wav --compute-type int8_float16

    출력 예시:
        === STT 테스트 ===
        모델: large-v3-turbo (float16, cuda)
        입력: test_ko.wav (3.2초, 102400 bytes)

        === 변환 결과 ===
        텍스트: "안녕하세요 오늘 날씨가 어때요"
        신뢰도: 0.95
        언어: ko
        소요 시간: 0.682초
    """
    import argparse
    import wave

    from shared.config import get_config
    from shared.utils import setup_logging

    parser = argparse.ArgumentParser(description="STT 서비스 테스트")
    parser.add_argument("--test", action="store_true", help="테스트 실행")
    parser.add_argument("--input", type=str, help="입력 WAV 파일 경로")
    parser.add_argument("--language", type=str, default="ko", help="언어 코드 (ko/en)")
    parser.add_argument(
        "--compute-type",
        type=str,
        default=None,
        help="양자화 타입 (float16/int8_float16). 미지정 시 config 사용",
    )
    args = parser.parse_args()

    if not args.test:
        parser.print_help()
        exit(0)

    if not args.input:
        print("오류: --input으로 WAV 파일을 지정하세요.")
        print("예: python -m server.stt.whisper_service --test --input test_ko.wav")
        exit(1)

    # 설정 로드 + 로깅
    config = get_config("server")
    setup_logging("DEBUG")

    stt_config = config.get("stt", {})
    model_size = stt_config.get("model_size", "large-v3-turbo")
    compute_type = args.compute_type or stt_config.get("compute_type", "float16")
    model_dir = stt_config.get("model_dir", "models/")

    # WAV 파일 읽기
    #   wave 모듈: Python 표준 라이브러리. WAV 파일의 헤더를 파싱하고
    #   PCM 데이터를 바이트로 읽어준다.
    #
    #   WAV 파일 구조:
    #       [헤더 44바이트] + [PCM 데이터]
    #       헤더에 sample_rate, channels, bit_depth 정보가 담겨 있다.
    #       wave 모듈이 헤더를 파싱하고, readframes()가 PCM 데이터를 반환한다.
    with wave.open(args.input, "rb") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        n_frames = wf.getnframes()
        duration = n_frames / sample_rate
        audio_bytes = wf.readframes(n_frames)

    print(f"\n=== STT 테스트 ===")
    print(f"모델: {model_size} ({compute_type}, cuda)")
    print(f"입력: {args.input} ({duration:.1f}초, {len(audio_bytes)} bytes)")
    print(f"sample_rate={sample_rate}, channels={channels}")

    if sample_rate != 16000:
        print(f"경고: Whisper는 16kHz를 기대합니다. 현재 {sample_rate}Hz.")

    # STT 실행
    stt_service = STTService(
        model_size=model_size,
        compute_type=compute_type,
        model_dir=model_dir,
    )

    result = asyncio.run(stt_service.transcribe(audio_bytes, language=args.language))

    print(f"\n=== 변환 결과 ===")
    print(f"텍스트: \"{result.text}\"")
    print(f"신뢰도: {result.confidence:.2f}")
    print(f"언어: {result.language}")

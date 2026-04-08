"""
TTS(Text-to-Speech) 서비스 — Supertonic TTS 기반.

Cloud LLM이 생성한 텍스트를 음성(PCM 오디오)으로 변환한다.
한국어와 영어를 하나의 모델로 처리하며, ONNX Runtime 위에서 동작한다.

Supertonic TTS란?
    66M 파라미터의 경량 다국어 TTS 모델.
    특징:
        - ONNX Runtime 기반 → PyTorch 의존성 불필요
        - 한국어(ko), 영어(en), 스페인어, 프랑스어 등 다국어 지원
        - 단일 모델(~305MB)로 여러 언어를 처리
        - 음성 스타일 10종 (남성 M1~M5, 여성 F1~F5)
        - inference_steps로 품질/속도 트레이드오프 조절 가능

    기존 방식(MeloTTS + Kokoro)과의 차이:
        기존: 한국어용 MeloTTS(~600MB, PyTorch) + 영어용 Kokoro(~300MB, ONNX)
              → 모델 2개, 메모리 ~900MB, 언어별 코드 분기 필요
        현재: Supertonic 1개(~305MB, ONNX)
              → lang 파라미터로 언어 전환, 메모리 ~600MB 절감

    ONNX Runtime이란?
        Microsoft가 만든 ML 모델 추론 엔진.
        PyTorch/TensorFlow 모델을 ONNX 형식으로 변환하면,
        원본 프레임워크 없이 경량 런타임만으로 추론할 수 있다.
        → 메모리 절약, 시작 시간 단축, 의존성 감소.

스트리밍 TTS (synthesize_stream)의 동작 원리:
    Cloud LLM은 응답을 토큰 단위로 스트리밍한다.
    예: "네," → "오늘" → "날씨" → "는" → "맑" → "아요."

    모든 토큰이 도착할 때까지 기다리면?
        → 전체 응답 완성까지 ~2-3초 대기 후 TTS 시작
        → 사용자 체감: "대답이 느리다"

    문장 단위 TTS 파이프라이닝:
        "네," 완성 즉시 → TTS 변환 시작 (~50ms)
        → 사용자가 "네,"를 듣는 동안 다음 문장이 도착
        → 끊김 없이 연속 재생

    이것이 synthesize_stream()의 핵심 아이디어이다.
    LLM 스트리밍 출력을 문장/절 경계에서 잘라서 즉시 TTS로 넘긴다.

사용 예:
    from server.tts.supertonic_service import SupertonicTTSService

    tts = SupertonicTTSService(voice_style="F1", inference_steps=5)

    # 단일 텍스트 변환
    pcm = await tts.synthesize("안녕하세요", language="ko")

    # LLM 스트리밍과 연결
    async for audio_chunk in tts.synthesize_stream(llm_stream, language="ko"):
        send_to_edge(audio_chunk)

관련 모듈:
    shared/utils.py — float32_to_pcm() 오디오 변환 함수
    server/cloud/llm_client.py — LLM 스트리밍 응답 (synthesize_stream의 입력)
"""

import asyncio
import logging
import re
import time
from typing import AsyncIterator

from shared.utils import float32_to_pcm

logger = logging.getLogger(__name__)


class SupertonicTTSService:
    """
    Supertonic TTS 기반 음성 합성 서비스.

    생성 시 ONNX 모델을 로드하고,
    synthesize() 또는 synthesize_stream()으로 텍스트를 음성으로 변환한다.

    Whisper(STT)와 마찬가지로 동기 라이브러리이므로,
    모든 추론 호출을 run_in_executor()로 비동기 래핑한다.
    (이유는 whisper_service.py 주석 참고)
    """

    def __init__(
        self,
        voice_style: str = "F1",
        inference_steps: int = 5,
    ) -> None:
        """
        Supertonic TTS 모델을 로드한다.

        Args:
            voice_style: 음성 스타일.
                "F1"~"F5" — 여성 보이스 5종
                "M1"~"M5" — 남성 보이스 5종
                음성비서에는 F1(여성 기본)을 권장.

            inference_steps: 추론 스텝 수.
                2   — 최고속 (RTF ~0.012, CPU에서도 실시간의 80배 빠름)
                5   — 속도/품질 균형 (기본 권장)
                128 — 최고 품질 (느리지만 자연스러움)

                RTF(Real-Time Factor)란?
                    1초 분량의 오디오를 생성하는 데 걸리는 시간.
                    RTF 0.012 = 1초 오디오를 ~12ms에 생성.
                    → 음성비서에서는 2~5 스텝이면 충분하다.

        모델 다운로드:
            첫 실행 시 HuggingFace에서 ONNX 모델을 자동 다운로드한다 (~305MB).
            auto_download=True가 이를 담당한다.
            이후에는 캐시된 모델을 사용한다.
        """
        from supertonic import TTS

        logger.info(
            f"Supertonic TTS 로드 시작: voice={voice_style}, "
            f"steps={inference_steps}"
        )

        load_start = time.time()

        # TTS 엔진 초기화 (ONNX 모델 로드)
        self._model = TTS(auto_download=True)

        # 음성 스타일 객체 생성
        #   get_voice_style()은 스타일 이름을 내부 파라미터 세트로 변환한다.
        #   매 synthesize() 호출마다 생성하지 않고 한 번만 만들어 재사용한다.
        self._voice_style = self._model.get_voice_style(voice_style)
        self._inference_steps = inference_steps
        self._voice_style_name = voice_style

        load_time = time.time() - load_start
        logger.info(f"Supertonic TTS 로드 완료: {load_time:.1f}초")

    # ─────────────────────────────────────────────
    # TTS 불가 문자 필터링
    # ─────────────────────────────────────────────

    # 마크다운 서식 패턴
    #   **bold**, *italic*, `code`, ```codeblock```, ## heading, --- 등
    #
    #   왜 정규식 여러 개를 | 로 묶는가?
    #       re.sub()에 하나의 패턴을 넘기면 한 번의 순회로 모든 패턴을 처리한다.
    #       패턴별로 re.sub()을 따로 호출하면 텍스트를 여러 번 순회해야 한다.
    #       성능 차이는 미미하지만, 코드가 깔끔해진다.
    _MARKDOWN_PATTERN = re.compile(
        r"```[\s\S]*?```"   # 코드 블록 (```...```)
        r"|`[^`]+`"         # 인라인 코드 (`...`)
        r"|#{1,6}\s"        # 제목 (# ~ ######)
        r"|\*{1,3}|_{1,3}"  # 볼드/이탤릭 (**, *, ___, __)
        r"|~{2}"            # 취소선 (~~)
        r"|^-{3,}$"         # 수평선 (---)
        r"|\[([^\]]*)\]\([^\)]*\)"  # 링크 [text](url) → text만 남김
    , re.MULTILINE)

    # 이모지 유니코드 범위
    #   이모지는 여러 유니코드 블록에 흩어져 있다.
    #   주요 범위를 문자 클래스([...])로 묶어서 한 번에 매칭한다.
    #
    #   \U0001F600-\U0001F64F  — 얼굴 이모지 (😀~🙏)
    #   \U0001F300-\U0001F5FF  — 기호/픽토그래프 (🌀~🗿)
    #   \U0001F680-\U0001F6FF  — 교통/지도 (🚀~🛿)
    #   \U0001F900-\U0001F9FF  — 보충 이모지 (🤀~🧿)
    #   \U0001FA00-\U0001FA6F  — 체스/기호 확장
    #   \U0001FA70-\U0001FAFF  — 기호 확장-A
    #   \U00002702-\U000027B0  — 딩뱃 (✂~➰)
    #   \U0000FE00-\U0000FE0F  — 변형 선택자 (이모지 스타일 지정)
    #   \U0000200D             — ZWJ (이모지 결합 문자, 👨‍👩‍👧 등)
    _EMOJI_PATTERN = re.compile(
        r"[\U0001F600-\U0001F64F"
        r"\U0001F300-\U0001F5FF"
        r"\U0001F680-\U0001F6FF"
        r"\U0001F900-\U0001F9FF"
        r"\U0001FA00-\U0001FA6F"
        r"\U0001FA70-\U0001FAFF"
        r"\U00002702-\U000027B0"
        r"\U0000FE00-\U0000FE0F"
        r"\U0000200D]+"
    )

    # 특수 기호 — TTS가 "동그라미", "화살표" 등으로 읽어버리는 문자들
    _SPECIAL_SYMBOLS_PATTERN = re.compile(
        r"[•‣◦◆◇▶▷►▻▲△▼▽"
        r"←→↑↓↔↕⇐⇒⇑⇓"
        r"※★☆○●◎□■▪▫"
        r"─━│┃┌┐└┘├┤┬┴┼"
        r"⟨⟩《》〈〉〔〕〖〗"
        r"♠♣♥♦♩♪♫♬]"
    )

    def _sanitize_for_tts(self, text: str) -> str:
        """
        TTS가 처리할 수 없거나 부자연스럽게 읽는 문자를 제거한다.

        시스템 프롬프트에서 "마크다운/이모지 금지"를 지시하지만,
        LLM이 100% 따르지는 않는다. 방어적으로 필터링한다.

        필터링 대상:
            1. 마크다운 서식 — **bold**, `code`, ## heading 등
               → 제거 (링크는 텍스트 부분만 남김)
            2. 이모지 — 😊, 👍 등
               → 제거
            3. 특수 기호 — •, →, ★, ※ 등
               → 제거
            4. 연속 공백 정리
               → 단일 공백으로 축소

        적용 시점:
            - synthesize(): TTS 모델 호출 직전 (최종 관문)
            - synthesize_stream(): 토큰 누적 시점 (문장 분리 로직 보호)

        Args:
            text: 원본 텍스트.

        Returns:
            정제된 텍스트. 모든 필터링 후 빈 문자열이 될 수도 있다.

        예시:
            "**네**, 오늘 날씨는 ☀️ 맑아요!" → "네, 오늘 날씨는 맑아요!"
            "다음 항목을 보세요:\n- 첫째" → "다음 항목을 보세요: 첫째"
            "자세한 내용은 [여기](http://...)를 참고하세요" → "자세한 내용은 여기를 참고하세요"
        """
        # 1. 마크다운 서식 제거
        #    링크 [text](url)는 캡처 그룹 \1(text)로 교체하여 텍스트만 남김.
        #    나머지 패턴은 빈 문자열로 교체 (단순 삭제).
        result = self._MARKDOWN_PATTERN.sub(
            lambda m: m.group(1) if m.group(1) else "", text
        )

        # 2. 이모지 제거
        result = self._EMOJI_PATTERN.sub("", result)

        # 3. 특수 기호 제거
        result = self._SPECIAL_SYMBOLS_PATTERN.sub("", result)

        # 4. 불릿 리스트 기호 제거 (줄 시작의 "- ", "* ", "숫자. ")
        #    "- 첫째" → "첫째"
        #    "1. 첫째" → "첫째"
        result = re.sub(r"^\s*[-*]\s+", "", result, flags=re.MULTILINE)
        result = re.sub(r"^\s*\d+\.\s+", "", result, flags=re.MULTILINE)

        # 5. 연속 공백/줄바꿈 → 단일 공백
        #    마크다운 제거 후 빈 공간이 남을 수 있다.
        result = re.sub(r"\s+", " ", result)

        return result.strip()

    # ─────────────────────────────────────────────
    # 단일 텍스트 변환 (주 사용 메서드)
    # ─────────────────────────────────────────────

    async def synthesize(
        self,
        text: str,
        language: str = "ko",
        speed: float = 1.0,
    ) -> bytes:
        """
        텍스트를 PCM 오디오 바이트로 변환한다.

        Args:
            text: 변환할 텍스트.
                "안녕하세요" 또는 "Hello, how are you?"
                한 문장~수 문장 정도가 적합.

            language: 언어 코드.
                "ko" — 한국어
                "en" — 영어
                Supertonic은 lang 파라미터로 언어를 전환한다.
                별도 모델을 로드할 필요가 없다.

            speed: 재생 속도.
                1.0 = 기본 속도
                1.2 = 20% 빠르게
                0.8 = 20% 느리게

        Returns:
            PCM int16 리틀엔디안 바이트.
            sample_rate는 모델 출력에 따르며, 보통 24000Hz.
            gRPC AudioChunk.data에 직접 담아 전송할 수 있다.

        내부 동작:
            1. _sanitize_for_tts()로 TTS 불가 문자 제거
            2. run_in_executor()로 동기 TTS 추론을 별도 스레드에서 실행
            3. 모델이 float32 numpy 배열(WAV)을 반환
            4. float32_to_pcm()으로 PCM int16 바이트 변환
        """
        # TTS 불가 문자 필터링 (최종 관문)
        text = self._sanitize_for_tts(text)

        if not text:
            logger.debug("필터링 후 빈 텍스트 — 빈 바이트 반환")
            return b""

        loop = asyncio.get_running_loop()
        infer_start = time.time()

        # 동기 TTS 추론을 별도 스레드에서 실행
        #   self._model.synthesize()는 동기 함수이다.
        #   반환값: (wav: numpy.ndarray, duration: float)
        #       wav — float32 오디오 데이터 (값 범위: 대략 -1.0 ~ +1.0)
        #       duration — 오디오 길이 (초)
        wav, duration = await loop.run_in_executor(
            None,
            lambda: self._model.synthesize(
                text,
                voice_style=self._voice_style,
                lang=language,
                speed=speed,
                inference_steps=self._inference_steps,
            ),
        )

        # float32 → PCM int16 변환 (shared/utils.py)
        pcm = float32_to_pcm(wav)

        infer_time = time.time() - infer_start
        logger.info(
            f"TTS 완료: {infer_time:.3f}초, "
            f"lang={language}, duration={duration:.1f}초, "
            f"텍스트=\"{text[:30]}{'...' if len(text) > 30 else ''}\""
        )

        return pcm

    # ─────────────────────────────────────────────
    # 스트리밍 TTS (LLM 출력 → 문장 단위 즉시 변환)
    # ─────────────────────────────────────────────

    async def synthesize_stream(
        self,
        text_stream: AsyncIterator[str],
        language: str = "ko",
        speed: float = 1.0,
    ) -> AsyncIterator[bytes]:
        """
        LLM 스트리밍 출력을 문장 단위로 수신하여 즉시 TTS 변환한다.

        동작 흐름:
            LLM 토큰 스트림: "네," → "오늘" → " 날씨" → "는 " → "맑아요." → " 기온은..."
                                                                    ↑ 문장 종결 감지
            1. 토큰들을 sentence_buffer에 누적
            2. 문장 종결 부호(. ? ! 등) 또는 쉼표+최소 길이 감지
            3. 완성된 문장을 즉시 TTS 변환 → yield
            4. 미완성 부분은 버퍼에 남김
            5. 스트림 종료 시 남은 버퍼도 처리

        Args:
            text_stream: LLM의 토큰 스트리밍 출력.
                CloudLLMClient.get_response_stream()의 반환값.
                각 yield는 토큰 1~수 개의 텍스트 조각이다.

            language: 언어 코드 ("ko" / "en")
            speed: 재생 속도

        Yields:
            bytes: 한 문장/절 분량의 PCM 오디오 바이트.
                gRPC에서 이를 다시 chunk_size 단위로 분할하여 전송한다.

        왜 문장 단위인가?
            - 단어 단위: TTS 호출 횟수 과다, 오버헤드 큼
            - 전체 텍스트: 모든 토큰 도착까지 대기, 지연 큼
            - 문장 단위: 자연스러운 발화 단위, TTS 품질도 좋음
        """
        sentence_buffer = ""

        async for text_chunk in text_stream:
            # 토큰 누적 전에 TTS 불가 문자를 필터링한다.
            # 여기서 미리 제거하는 이유:
            #   마크다운 서식(**bold** 등)이 sentence_buffer에 쌓이면
            #   _split_sentences()의 문장 경계 감지를 방해할 수 있다.
            #   예: "**네,** 맑아요." → **가 쉼표 앞에 붙어 분리 패턴 불일치.
            #
            # synthesize()에서도 _sanitize_for_tts()를 호출하므로 (최종 관문),
            # 여기서 놓친 것이 있더라도 이중으로 보호된다.
            cleaned = self._sanitize_for_tts(text_chunk)
            sentence_buffer += cleaned

            # 문장/절 경계에서 분리 시도
            sentences = self._split_sentences(sentence_buffer)

            if len(sentences) > 1:
                # 마지막 요소를 제외한 나머지 = 완성된 문장들
                for sentence in sentences[:-1]:
                    stripped = sentence.strip()
                    if stripped:
                        audio = await self.synthesize(stripped, language, speed)
                        if audio:
                            yield audio

                # 마지막 미완성 부분은 버퍼에 유지
                sentence_buffer = sentences[-1]

        # 스트림 종료 — 남은 버퍼 처리
        remaining = sentence_buffer.strip()
        if remaining:
            audio = await self.synthesize(remaining, language, speed)
            if audio:
                yield audio

    # ─────────────────────────────────────────────
    # 문장/절 분리
    # ─────────────────────────────────────────────

    def _split_sentences(self, text: str) -> list[str]:
        """
        텍스트를 문장/절 경계에서 분리한다.

        첫 TTS 시작을 앞당기기 위해 두 단계로 분리한다:

        1단계 — 문장 종결 부호:
            마침표(.), 물음표(?), 느낌표(!)  뒤의 공백에서 분리.
            예: "네, 맑아요. 기온은 20도에요." → ["네, 맑아요.", " 기온은 20도에요."]

        2단계 — 쉼표/절 경계 (10자 이상일 때):
            긴 문장 내에서 쉼표(,) 뒤의 공백에서 추가 분리.
            짧은 구절("네,")은 분리하지 않음 — 너무 짧으면 TTS 품질 저하.
            예: "첫째, 날씨가 좋고, 둘째 기온이 적당해요."
                → ["첫째, 날씨가 좋고,", " 둘째 기온이 적당해요."]

        왜 쉼표에서도 분리하는가?
            LLM의 첫 응답이 "네, 오늘 서울 날씨는 맑습니다." 처럼 긴 경우,
            마침표까지 기다리면 ~500ms 추가 대기가 발생한다.
            쉼표에서 한 번 끊으면 "네, 오늘 서울 날씨는 맑습니다" 부분을
            더 빨리 TTS로 보낼 수 있다.

        Args:
            text: 분리할 텍스트 (sentence_buffer).

        Returns:
            분리된 문장/절 리스트.
            마지막 요소는 미완성 문장일 수 있다.
            리스트 길이가 1이면 아직 완성된 문장이 없다는 뜻.
        """
        # 1단계: 문장 종결 부호 뒤 공백에서 분리
        #   (?<=[.?!。？！])  — 종결 부호 뒤에서 (lookbehind)
        #   \s+               — 하나 이상의 공백에서 분리
        #
        #   lookbehind란?
        #       (?<=X) — "X 뒤에 있는 위치"를 의미.
        #       분리점을 찍되 X 자체는 이전 문장에 남긴다.
        #       "맑아요. 기온은" → ["맑아요.", "기온은"] (마침표가 앞에 남음)
        parts = re.split(r"(?<=[.?!。？！])\s+", text)

        # 2단계: 긴 문장 내 쉼표 절 경계에서 추가 분리
        result = []
        for part in parts:
            if len(part) > 10 and "," in part:
                sub = re.split(r"(?<=[,，])\s+", part)
                result.extend(sub)
            else:
                result.append(part)

        return result if result else [text]


# ─────────────────────────────────────────────
# CLI 테스트
# ─────────────────────────────────────────────

if __name__ == "__main__":
    """
    단독 실행으로 TTS 서비스를 테스트한다.

    사용법:
        # 한국어 테스트
        python -m server.tts.supertonic_service --test --text "안녕하세요" --lang ko

        # 영어 테스트
        python -m server.tts.supertonic_service --test --text "Hello there" --lang en

        # 음성 스타일 변경
        python -m server.tts.supertonic_service --test --text "안녕하세요" --voice M1

        # 결과를 WAV 파일로 저장
        python -m server.tts.supertonic_service --test --text "안녕하세요" --output test_out.wav

    출력 예시:
        === TTS 테스트 ===
        음성 스타일: F1
        추론 스텝: 5
        언어: ko
        텍스트: "안녕하세요"

        === 변환 결과 ===
        오디오 길이: 1.2초
        PCM 크기: 57600 bytes
        소요 시간: 0.048초
    """
    import argparse
    import wave

    import numpy as np

    from shared.config import get_config
    from shared.utils import setup_logging

    parser = argparse.ArgumentParser(description="TTS 서비스 테스트")
    parser.add_argument("--test", action="store_true", help="테스트 실행")
    parser.add_argument("--text", type=str, default="안녕하세요, 테스트입니다.", help="변환할 텍스트")
    parser.add_argument("--lang", type=str, default="ko", help="언어 코드 (ko/en)")
    parser.add_argument("--voice", type=str, default=None, help="음성 스타일 (F1~F5, M1~M5)")
    parser.add_argument("--output", type=str, default=None, help="출력 WAV 파일 경로")
    args = parser.parse_args()

    if not args.test:
        parser.print_help()
        exit(0)

    # 설정 로드 + 로깅
    config = get_config("server")
    setup_logging("DEBUG")

    tts_config = config.get("tts", {})
    voice_style = args.voice or tts_config.get("voice_style", "F1")
    inference_steps = tts_config.get("inference_steps", 5)

    print(f"\n=== TTS 테스트 ===")
    print(f"음성 스타일: {voice_style}")
    print(f"추론 스텝: {inference_steps}")
    print(f"언어: {args.lang}")
    print(f"텍스트: \"{args.text}\"")

    # TTS 실행
    tts_service = SupertonicTTSService(
        voice_style=voice_style,
        inference_steps=inference_steps,
    )

    start_time = time.time()
    pcm_bytes = asyncio.run(tts_service.synthesize(args.text, language=args.lang))
    elapsed = time.time() - start_time

    # PCM 바이트에서 오디오 길이 계산
    #   PCM int16 = 2바이트/샘플, sample_rate = 24000
    #   길이(초) = 바이트수 / (2 * sample_rate)
    sample_rate = 24000
    duration = len(pcm_bytes) / (2 * sample_rate)

    print(f"\n=== 변환 결과 ===")
    print(f"오디오 길이: {duration:.1f}초")
    print(f"PCM 크기: {len(pcm_bytes)} bytes")
    print(f"소요 시간: {elapsed:.3f}초")

    # WAV 파일로 저장 (선택)
    if args.output:
        with wave.open(args.output, "wb") as wf:
            wf.setnchannels(1)          # 모노
            wf.setsampwidth(2)          # int16 = 2바이트
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        print(f"\nWAV 저장: {args.output}")

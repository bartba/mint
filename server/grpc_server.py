"""
gRPC 서버 — Edge 요청 처리.

ProcessVoice, TimeoutPrompt, EndSession, HealthCheck 네 개의 RPC를 구현한다.

RPC별 처리 흐름:

    ProcessVoice (bidirectional streaming):
        Edge가 오디오 청크 스트림을 보내면:
        1. 전체 오디오를 수집
        2. STT로 텍스트 변환
        3. STT 결과를 Edge에 즉시 전송
        4. 로컬 인텐트 매칭 → Cloud 스킵 여부 결정
        5-a. 로컬 인텐트: TTS 변환 → 오디오 스트림 전송
        5-b. Cloud LLM: 스트리밍 응답 → 문장 단위 TTS → 오디오 스트림 전송

    TimeoutPrompt (unary → server streaming):
        Edge가 타임아웃 타입을 보내면:
        1. 언어에 맞는 타임아웃 메시지 텍스트 조회
        2. TTS 변환 → 오디오 스트림 전송

    EndSession (unary → server streaming):
        Edge가 세션 종료를 요청하면:
        1. 종료 안내 메시지 TTS 변환 → 오디오 스트림 전송
        2. Cloud LLM 대화 히스토리 초기화

    HealthCheck (unary → unary):
        sysfs에서 GPU/메모리/온도를 온디맨드로 읽어 반환한다.

관련 모듈:
    server/orchestrator.py — 서비스 인스턴스, 인텐트/티어 판단
    proto/voice_service_pb2.py — 메시지 타입
    proto/voice_service_pb2_grpc.py — VoiceServiceServicer 베이스 클래스
    shared/utils.py — chunk_bytes()
"""

import logging
import sys
from pathlib import Path
from typing import AsyncIterator

import grpc
import grpc.aio

from server.orchestrator import ServerOrchestrator
from shared.utils import chunk_bytes

# proto 디렉토리를 sys.path에 추가.
# voice_service_pb2_grpc.py가 "import voice_service_pb2"를 패키지 없이 임포트하므로,
# proto 디렉토리가 직접 sys.path에 있어야 한다.
_PROTO_DIR = Path(__file__).resolve().parent.parent / "proto"
if str(_PROTO_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTO_DIR))

import voice_service_pb2 as pb2
import voice_service_pb2_grpc as pb2_grpc

logger = logging.getLogger(__name__)

# Supertonic TTS 출력 샘플레이트
_TTS_SAMPLE_RATE = 22050

# gRPC 스트리밍 오디오 청크 크기 (4KB)
# config/default.yaml의 audio.chunk_size와 동일하게 맞춘다.
_STREAM_CHUNK_SIZE = 4096


class VoiceServiceHandler(pb2_grpc.VoiceServiceServicer):
    """
    Edge의 gRPC 요청을 처리하는 핸들러.

    grpc.aio 기반이므로 모든 RPC 핸들러가 async 메서드(또는 async generator)다.
    ServerOrchestrator가 관리하는 STT, TTS, Cloud LLM 서비스를 사용한다.
    """

    def __init__(self, orchestrator: ServerOrchestrator) -> None:
        self.orchestrator = orchestrator

    # ─────────────────────────────────────────────
    # ProcessVoice
    # ─────────────────────────────────────────────

    async def ProcessVoice(
        self,
        request_iterator: AsyncIterator[pb2.AudioChunk],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb2.VoiceResponse]:
        """
        오디오 → STT → (로컬 인텐트 또는 Cloud LLM) → TTS → 오디오 스트림.

        전체 파이프라인:
            Edge 오디오 청크 스트림 수집
                → STT (Whisper, GPU FP16)
                → STT 결과 즉시 전송 (Edge가 화면에 표시할 수 있도록)
                → 언어 지원 확인
                → 로컬 인텐트 매칭
                    매칭 성공 → TTS → 오디오 스트림 (Cloud 스킵)
                    매칭 실패 → Cloud LLM 스트리밍 → 문장 단위 TTS → 오디오 스트림
        """
        # ── 1. 오디오 수집 ──
        audio_data = bytearray()
        sample_rate = 16000
        async for chunk in request_iterator:
            audio_data.extend(chunk.data)
            if chunk.sample_rate:
                sample_rate = chunk.sample_rate

        if not audio_data:
            logger.warning("ProcessVoice: 빈 오디오 수신, 건너뜀")
            return

        logger.info(
            f"ProcessVoice: 오디오 수신 {len(audio_data):,}바이트, {sample_rate}Hz"
        )

        # ── 2. STT ──
        try:
            stt_result = await self.orchestrator.stt_service.transcribe(
                bytes(audio_data)
            )
        except Exception as e:
            logger.error(f"STT 실패: {e}", exc_info=True)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(f"STT error: {e}")
            return

        logger.info(
            f"STT 완료: '{stt_result.text}' "
            f"(lang={stt_result.language}, conf={stt_result.confidence:.2f})"
        )

        # ── 3. STT 결과 즉시 전송 ──
        # Edge가 화면에 인식 텍스트를 표시하거나 디버깅에 활용할 수 있다.
        yield pb2.VoiceResponse(stt_result=stt_result.to_proto())

        # ── 4. 언어 지원 확인 ──
        # Supertonic TTS는 ko/en만 지원한다.
        if not self.orchestrator.is_supported_language(stt_result.language):
            logger.info(f"미지원 언어 감지: {stt_result.language}")
            msg = self.orchestrator.get_unsupported_language_message()
            async for response in self._tts_to_stream(msg, "ko"):
                yield response
            return

        # ── 5. 로컬 인텐트 확인 ──
        # 매칭 성공 시 Cloud LLM을 호출하지 않고 즉시 응답한다.
        intent = self.orchestrator.check_local_intent(
            stt_result.text, stt_result.language
        )
        if intent:
            async for response in self._tts_to_stream(
                intent.text, stt_result.language
            ):
                yield response
            if intent.clear_session:
                self.orchestrator.cloud_client.clear_history()
                logger.info("대화 히스토리 초기화 (로컬 인텐트 clear_session=True)")
            return

        # ── 6. Cloud LLM 스트리밍 → TTS 파이프라인 ──
        model_tier = self.orchestrator.select_model_tier(
            stt_result.text, stt_result.language
        )
        system_prompt = self.orchestrator.get_prompt(stt_result.language)
        logger.info(
            f"Cloud LLM 호출: tier={model_tier}, lang={stt_result.language}"
        )

        try:
            llm_stream = self.orchestrator.cloud_client.get_response_stream(
                user_text=stt_result.text,
                system_prompt=system_prompt,
                model_tier=model_tier,
                language=stt_result.language,
            )
            tts = self.orchestrator.get_tts_service()
            async for pcm_bytes in tts.synthesize_stream(
                llm_stream, stt_result.language
            ):
                for data in chunk_bytes(pcm_bytes, _STREAM_CHUNK_SIZE):
                    yield pb2.VoiceResponse(
                        tts_audio=pb2.AudioChunk(
                            data=data,
                            sample_rate=_TTS_SAMPLE_RATE,
                            encoding="pcm_s16le",
                        )
                    )
        except Exception as e:
            logger.error(f"LLM/TTS 파이프라인 실패: {e}", exc_info=True)
            fallback = (
                "죄송합니다, 처리 중 오류가 발생했습니다."
                if stt_result.language == "ko"
                else "Sorry, an error occurred while processing your request."
            )
            async for response in self._tts_to_stream(
                fallback, stt_result.language
            ):
                yield response

    # ─────────────────────────────────────────────
    # TimeoutPrompt
    # ─────────────────────────────────────────────

    async def TimeoutPrompt(
        self,
        request: pb2.TimeoutRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb2.VoiceResponse]:
        """
        타임아웃 안내 메시지를 TTS로 변환하여 스트림으로 반환한다.

        TIMEOUT_PROMPT (type=0): "계속 대화하시겠어요?" — 15초 무발화 후
        TIMEOUT_END    (type=1): "대화를 종료합니다."  — 5초 추가 무발화 후
        """
        language = request.language or "ko"

        if request.type == pb2.TIMEOUT_END:
            text = self.orchestrator.get_timeout_end(language)
            logger.info(f"TimeoutPrompt END: lang={language}")
        else:
            text = self.orchestrator.get_timeout_prompt(language)
            logger.info(f"TimeoutPrompt PROMPT: lang={language}")

        async for response in self._tts_to_stream(text, language):
            yield response

    # ─────────────────────────────────────────────
    # EndSession
    # ─────────────────────────────────────────────

    async def EndSession(
        self,
        request: pb2.EndSessionRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb2.VoiceResponse]:
        """
        세션 종료 안내 메시지를 TTS로 반환하고 대화 히스토리를 초기화한다.

        로컬 인텐트(바이바이)는 ProcessVoice 내에서 처리된다.
        이 RPC는 Edge가 명시적으로 세션을 끊을 때(앱 종료, 강제 종료 등) 사용한다.
        """
        language = request.language or "ko"
        logger.info(f"EndSession: lang={language}")

        text = self.orchestrator.get_timeout_end(language)
        async for response in self._tts_to_stream(text, language):
            yield response

        # TTS 스트림 전송 완료 후 히스토리 초기화
        if self.orchestrator.cloud_client is not None:
            self.orchestrator.cloud_client.clear_history()
            logger.info("대화 히스토리 초기화 (EndSession)")

    # ─────────────────────────────────────────────
    # HealthCheck
    # ─────────────────────────────────────────────

    async def HealthCheck(
        self,
        request: pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> pb2.ServerStatus:
        """
        서버 상태를 반환한다.

        Edge가 주기적으로 호출하여 연결 상태와 리소스를 확인한다.
        sysfs에서 GPU/메모리/온도를 온디맨드로 읽는다 (백그라운드 폴링 없음).
        """
        stats = self.orchestrator.get_system_stats()
        return pb2.ServerStatus(
            stt_ready=self.orchestrator.stt_service is not None,
            tts_ready=self.orchestrator.tts_service is not None,
            gpu_usage=stats.get("gpu_usage", 0.0),
            memory_usage=stats.get("memory_usage", 0.0),
            temperature=stats.get("temperature", 0.0),
        )

    # ─────────────────────────────────────────────
    # 내부 헬퍼
    # ─────────────────────────────────────────────

    async def _tts_to_stream(
        self,
        text: str,
        language: str,
    ) -> AsyncIterator[pb2.VoiceResponse]:
        """
        짧은 텍스트를 TTS로 변환하고 4KB 청크로 분할하여 yield한다.

        TimeoutPrompt, EndSession, 로컬 인텐트 응답, 에러 폴백에서 공통으로 사용한다.
        긴 LLM 스트리밍 응답은 synthesize_stream()을 직접 사용한다.

        Args:
            text: TTS로 변환할 텍스트.
            language: 언어 코드 ("ko" / "en").
        """
        tts = self.orchestrator.get_tts_service()
        try:
            pcm_bytes = await tts.synthesize(text, language)
            for data in chunk_bytes(pcm_bytes, _STREAM_CHUNK_SIZE):
                yield pb2.VoiceResponse(
                    tts_audio=pb2.AudioChunk(
                        data=data,
                        sample_rate=_TTS_SAMPLE_RATE,
                        encoding="pcm_s16le",
                    )
                )
        except Exception as e:
            logger.error(
                f"TTS 실패 (text='{text[:30]}', lang={language}): {e}",
                exc_info=True,
            )

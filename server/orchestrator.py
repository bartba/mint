"""
서버 오케스트레이터 — 서비스 조율 및 요청 라우팅.

이 모듈은 Server의 중추이다. STT, TTS, Cloud LLM 세 서비스를
생성/관리하고, gRPC 서버(grpc_server.py)가 요청을 처리할 때
필요한 서비스와 설정을 제공한다.

오케스트레이터의 역할:
    1. 시작 시 모델을 순서대로 로드 (startup)
    2. 요청마다 적절한 서비스 인스턴스를 반환
    3. STT 결과에서 로컬 인텐트를 판단 (Cloud 스킵 여부)
    4. 언어에 따라 시스템 프롬프트 / 타임아웃 메시지 반환
    5. Cloud LLM 모델 티어 선택 (haiku vs sonnet)
    6. 시스템 상태 조회 (GPU, 메모리, 온도)
    7. 종료 시 리소스 정리 (shutdown)

모델 로드 순서:
    메모리가 큰 것부터 로드하면 OOM 위험을 조기에 발견할 수 있다.
    Whisper(~3GB) → Supertonic TTS(~0.3GB) → CloudLLMClient

    각 단계 전에 system_reader.check_memory_safe()로 여유를 확인한다.
    여유가 부족하면 Whisper를 INT8 모드로 폴백하거나, OOM을 방지한다.

사용 예:
    from shared.config import get_config
    from server.orchestrator import ServerOrchestrator

    config = get_config("server")
    orchestrator = ServerOrchestrator(config)
    await orchestrator.startup()

    # gRPC 핸들러에서
    stt_result = await orchestrator.stt_service.transcribe(audio_bytes)
    intent = orchestrator.check_local_intent(stt_result.text, stt_result.language)

    await orchestrator.shutdown()

관련 모듈:
    server/grpc_server.py — 이 오케스트레이터를 생성하고 gRPC 요청을 처리
    server/stt/whisper_service.py — STT 서비스
    server/tts/supertonic_service.py — TTS 서비스
    server/cloud/llm_client.py — Cloud LLM 클라이언트
    server/monitoring/system_reader.py — 시스템 상태 읽기
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from server.cloud.llm_client import CloudLLMClient
from server.cloud.prompt_templates import get_system_prompt
from server.monitoring.system_reader import SystemReader
from server.stt.whisper_service import STTService
from server.tts.supertonic_service import SupertonicTTSService

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 로컬 인텐트 결과
# ─────────────────────────────────────────────

@dataclass
class LocalIntentResult:
    """
    로컬 인텐트 매칭 결과.

    Cloud LLM을 호출하지 않고 로컬에서 처리할 수 있는
    STT 텍스트가 감지되었을 때 반환된다.

    Attributes:
        text: TTS로 변환하여 Edge에 전달할 응답 텍스트.
        clear_session: True이면 응답 후 대화 히스토리를 초기화한다.
            "대화 종료" 인텐트에서 True로 설정된다.
    """
    text: str
    clear_session: bool = False


# ─────────────────────────────────────────────
# 메모리 요구량 상수
# ─────────────────────────────────────────────

# Whisper FP16: ~3GB, INT8: ~1.5GB
_WHISPER_FP16_MB = 3000
_WHISPER_INT8_MB = 1500
_TTS_MB = 500


class ServerOrchestrator:
    """
    Server 서비스 조율자.

    STT, TTS, Cloud LLM 세 서비스의 생명주기를 관리하고,
    gRPC 서버가 요청을 처리할 때 필요한 것들을 제공한다.

    인스턴스 생성 후 반드시 await startup()을 호출해야 서비스가 준비된다.
    서버 종료 시 await shutdown()으로 리소스를 정리한다.
    """

    def __init__(self, config: dict) -> None:
        """
        오케스트레이터를 생성한다.

        모델 로드는 여기서 하지 않는다.
        startup()에서 메모리 확인 후 순서대로 로드한다.

        Args:
            config: get_config("server")의 반환값.
                stt, tts, cloud, local_intents, timeout_messages 섹션을 참조한다.
        """
        self.config = config
        self.system_reader = SystemReader()

        # 모델 서비스 — startup() 호출 전에는 None
        self.stt_service: Optional[STTService] = None
        self.tts_service: Optional[SupertonicTTSService] = None
        self.cloud_client: Optional[CloudLLMClient] = None

    # ─────────────────────────────────────────────
    # 시작/종료
    # ─────────────────────────────────────────────

    def _drop_page_cache(self) -> None:
        """
        Linux 페이지 캐시를 비워서 모델 로드 전 여유 메모리를 확보한다.

        Jetson은 CPU와 GPU가 메모리를 공유(통합 메모리)하므로,
        커널 페이지 캐시가 쌓이면 GPU 모델 로드 가능 용량이 줄어든다.
        모델 로드 직전에 캐시를 비우면 OOM 위험을 낮출 수 있다.

        /proc/sys/vm/drop_caches 값의 의미:
            1 — pagecache만 해제 (파일 읽기 캐시)
            2 — dentry/inode 캐시만 해제
            3 — pagecache + dentry/inode 모두 해제

            여기서는 3(전체)을 사용한다. pagecache가 GPU 공유 메모리의
            주요 소비원이며, dentry/inode 해제는 오버헤드가 거의 없다.

        권한 요구사항:
            /proc/sys/vm/drop_caches에 쓰려면 root 권한이 필요하다.
            권한이 없으면 경고만 로깅하고 계속 진행한다.

        주의:
            캐시를 비우면 직후 파일 I/O(모델 로드 포함)가 일시적으로
            느려질 수 있다. 모델 파일이 캐시에서 제거되었다가
            로드 시 다시 디스크에서 읽히기 때문이다.
            그러나 통합 메모리 환경에서 OOM을 방지하는 이점이 더 크다.
        """
        drop_caches_path = "/proc/sys/vm/drop_caches"
        mem_before = self.system_reader.get_memory_usage()

        try:
            with open(drop_caches_path, "w") as f:
                f.write("3\n")

            mem_after = self.system_reader.get_memory_usage()
            freed_mb = mem_after["free_mb"] - mem_before["free_mb"]
            logger.info(
                f"페이지 캐시 해제 완료: "
                f"여유 {mem_before['free_mb']}MB → {mem_after['free_mb']}MB "
                f"(+{freed_mb}MB 확보)"
            )
        except PermissionError:
            logger.warning(
                "페이지 캐시 해제 실패: root 권한 필요. "
                "실행 전 'sudo' 또는 sudoers 설정을 확인하세요. "
                "계속 진행합니다."
            )
        except OSError as e:
            logger.warning(f"페이지 캐시 해제 실패: {e}. 계속 진행합니다.")

    async def startup(self) -> None:
        """
        모델을 순서대로 로드한다.

        로드 순서:
            0. 페이지 캐시 해제 — 모델 로드 전 여유 메모리 확보
            1. STTService (Whisper, ~3GB) — 가장 크므로 먼저 로드
            2. SupertonicTTSService (ONNX, ~0.3GB)
            3. CloudLLMClient (메모리 거의 없음, API 클라이언트)

        각 단계 전에 check_memory_safe()로 여유를 확인한다.
        FP16 메모리가 부족하면 INT8로 자동 폴백한다.

        Raises:
            RuntimeError: 메모리가 너무 부족하여 최소 모델도 로드 불가.
        """
        logger.info("=== Server startup 시작 ===")
        mem = self.system_reader.get_memory_usage()
        logger.info(
            f"초기 메모리: 전체 {mem['total_mb']}MB, "
            f"사용 {mem['used_mb']}MB, "
            f"여유 {mem['free_mb']}MB"
        )

        # ── 0. 페이지 캐시 해제 — 모델 로드 전 여유 확보 ──
        self._drop_page_cache()

        # ── 1. STT 서비스 로드 ──
        self._load_stt_service()

        # ── 2. TTS 서비스 로드 ──
        self._load_tts_service()

        # ── 3. Cloud LLM 클라이언트 초기화 ──
        self._init_cloud_client()

        mem_after = self.system_reader.get_memory_usage()
        logger.info(
            f"=== Server startup 완료 === "
            f"메모리 사용: {mem_after['used_mb']}MB / {mem_after['total_mb']}MB "
            f"(여유 {mem_after['free_mb']}MB)"
        )

    def _load_stt_service(self) -> None:
        """
        STT 서비스(Whisper)를 로드한다.

        메모리 우선순위:
            1. FP16 (~3GB): 정확도 최고 → 기본 시도
            2. INT8_FP16 (~1.5GB): 메모리 부족 시 자동 폴백
            3. 1.5GB도 없으면: RuntimeError

        compute_type은 config의 stt.compute_type을 기본값으로 사용하되,
        메모리 부족 시 자동으로 int8_float16으로 내린다.
        """
        stt_config = self.config.get("stt", {})
        model_size = stt_config.get("model_size", "large-v3-turbo")
        compute_type = stt_config.get("compute_type", "float16")
        model_dir = stt_config.get("model_dir", "models/")

        # FP16 시도
        if compute_type == "float16":
            if self.system_reader.check_memory_safe(required_mb=_WHISPER_FP16_MB):
                logger.info(f"STT 로드: {model_size}, FP16 (정상 모드)")
            elif self.system_reader.check_memory_safe(required_mb=_WHISPER_INT8_MB):
                logger.warning(
                    f"메모리 부족 — FP16({_WHISPER_FP16_MB}MB) 불가, "
                    f"INT8({_WHISPER_INT8_MB}MB)로 폴백"
                )
                compute_type = "int8_float16"
            else:
                raise RuntimeError(
                    f"STT 모델을 로드할 메모리가 없음 "
                    f"(여유: {self.system_reader.get_memory_usage()['free_mb']}MB)"
                )
        else:
            # config에서 이미 int8_float16 / int8으로 설정된 경우
            if not self.system_reader.check_memory_safe(required_mb=_WHISPER_INT8_MB):
                raise RuntimeError(
                    f"STT 모델을 로드할 메모리가 없음 "
                    f"(여유: {self.system_reader.get_memory_usage()['free_mb']}MB)"
                )

        self.stt_service = STTService(
            model_size=model_size,
            compute_type=compute_type,
            model_dir=model_dir,
        )

        mem = self.system_reader.get_memory_usage()
        logger.info(f"STT 로드 완료. 남은 여유 메모리: {mem['free_mb']}MB")

    def _load_tts_service(self) -> None:
        """
        TTS 서비스(Supertonic)를 로드한다.

        Supertonic은 ~305MB ONNX 모델로, STT보다 훨씬 작다.
        여유 메모리가 500MB 이상이면 로드한다.
        """
        if not self.system_reader.check_memory_safe(required_mb=_TTS_MB):
            raise RuntimeError(
                f"TTS 모델을 로드할 메모리가 없음 "
                f"(여유: {self.system_reader.get_memory_usage()['free_mb']}MB)"
            )

        tts_config = self.config.get("tts", {})
        voice_style = tts_config.get("voice_style", "F1")
        inference_steps = tts_config.get("inference_steps", 5)

        self.tts_service = SupertonicTTSService(
            voice_style=voice_style,
            inference_steps=inference_steps,
        )

        mem = self.system_reader.get_memory_usage()
        logger.info(f"TTS 로드 완료. 남은 여유 메모리: {mem['free_mb']}MB")

    def _init_cloud_client(self) -> None:
        """
        Cloud LLM 클라이언트를 초기화한다.

        API 클라이언트이므로 메모리 확인 불필요.
        API 키는 환경변수(ANTHROPIC_API_KEY / OPENAI_API_KEY)에서 읽힌다.
        """
        cloud_config = self.config.get("cloud", {})
        provider = cloud_config.get("provider", "claude")
        max_tokens = cloud_config.get("max_tokens", 500)
        summarize_threshold_tokens = cloud_config.get("summarize_threshold_tokens", 4000)
        timeout_s = cloud_config.get("timeout_s", 30)

        self.cloud_client = CloudLLMClient(
            provider=provider,
            max_tokens=max_tokens,
            summarize_threshold_tokens=summarize_threshold_tokens,
            timeout_s=timeout_s,
            claude_models=cloud_config.get("models"),
        )

        logger.info(f"Cloud LLM 클라이언트 초기화: provider={provider}")

    async def shutdown(self) -> None:
        """
        서버 종료 시 리소스를 정리한다.

        현재는 대화 히스토리 초기화 정도만 수행한다.
        모델 메모리는 GC에 의해 해제된다.
        """
        logger.info("Server shutdown 시작")

        if self.cloud_client is not None:
            self.cloud_client.clear_history()

        # 모델 서비스는 GC가 해제하므로 명시적 del 불필요
        # (faster-whisper, onnxruntime 모두 __del__ 없음)
        self.stt_service = None
        self.tts_service = None
        self.cloud_client = None

        logger.info("Server shutdown 완료")

    # ─────────────────────────────────────────────
    # 서비스 접근자
    # ─────────────────────────────────────────────

    def get_tts_service(self) -> SupertonicTTSService:
        """
        TTS 서비스를 반환한다.

        Supertonic은 단일 모델로 한국어/영어를 처리하므로,
        언어 분기 없이 하나의 서비스를 반환한다.

        Returns:
            SupertonicTTSService 인스턴스.

        Raises:
            RuntimeError: startup() 호출 전에 접근 시.
        """
        if self.tts_service is None:
            raise RuntimeError("startup()이 호출되지 않았거나 TTS 로드에 실패했습니다.")
        return self.tts_service

    # ─────────────────────────────────────────────
    # 로컬 인텐트 처리
    # ─────────────────────────────────────────────

    def check_local_intent(
        self,
        text: str,
        language: str,
    ) -> Optional[LocalIntentResult]:
        """
        STT 결과 텍스트에서 로컬 인텐트를 매칭한다.

        config의 local_intents 섹션에 정의된 패턴들을 순서대로 검사한다.
        하나라도 매칭되면 즉시 반환하고 Cloud LLM 호출을 스킵한다.

        Args:
            text: STT로 인식된 사용자 발화 텍스트.
            language: STT가 감지한 언어 코드 ("ko" / "en").

        Returns:
            매칭된 LocalIntentResult, 또는 None (매칭 실패 → Cloud로 폴스루).

        config 예시:
            local_intents:
              - name: "대화 종료"
                patterns:
                  ko: ["그만", "끝", "종료", "^안녕히", "잘가", "여기까지"]
                  en: ["bye", "goodbye", "stop", "quit", "end"]
                responses:
                  ko: "안녕히 가세요."
                  en: "Goodbye."
                clear_session: true

        패턴 매칭 방식:
            re.search()를 사용하므로 부분 일치가 가능하다.
            "^안녕히"처럼 앵커를 붙이면 문장 시작만 매칭할 수 있다.
            대소문자 구분 없이 매칭한다 (text.lower() 적용).
        """
        text_lower = text.strip().lower()
        intents = self.config.get("local_intents", [])

        for intent in intents:
            patterns = intent.get("patterns", {}).get(language, [])
            for pattern in patterns:
                try:
                    if re.search(pattern, text_lower):
                        response_text = intent.get("responses", {}).get(
                            language,
                            intent.get("responses", {}).get("ko", "")
                        )
                        clear_session = intent.get("clear_session", False)
                        logger.info(
                            f"로컬 인텐트 매칭: '{intent.get('name')}' "
                            f"(패턴: '{pattern}', clear_session={clear_session})"
                        )
                        return LocalIntentResult(
                            text=response_text,
                            clear_session=clear_session,
                        )
                except re.error as e:
                    logger.warning(f"인텐트 패턴 오류 '{pattern}': {e}")

        return None

    # ─────────────────────────────────────────────
    # 언어별 메시지 반환
    # ─────────────────────────────────────────────

    # TTS(Supertonic)가 지원하는 언어 목록.
    # 이 외의 언어는 STT 인식 후 즉시 안내 메시지를 반환하고 처리를 중단한다.
    SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"ko", "en"})

    def is_supported_language(self, language: str) -> bool:
        """
        STT가 감지한 언어가 TTS 지원 언어인지 확인한다.

        Supertonic TTS는 한국어("ko")와 영어("en")만 지원한다.
        gRPC 핸들러에서 STT 결과를 받은 직후, 인텐트 라우팅 및 Cloud 호출 전에
        이 메서드로 언어를 확인해야 한다.

        Args:
            language: STT STTResult.language 필드값 (Whisper ISO-639-1 코드).
                예: "ko", "en", "ja", "zh", "es" 등.

        Returns:
            True  — 지원 언어 ("ko" 또는 "en")
            False — 미지원 언어, 처리 중단 후 안내 메시지 반환 필요
        """
        return language in self.SUPPORTED_LANGUAGES

    def get_unsupported_language_message(self) -> str:
        """
        미지원 언어 감지 시 TTS로 전달할 안내 메시지를 반환한다.

        TTS 자체가 미지원 언어를 합성할 수 없으므로, 안내 메시지는
        항상 한국어(기본 언어)로 반환한다. TTS 호출 시 language="ko" 고정.

        Returns:
            한국어 안내 메시지 문자열.
        """
        return (
            "죄송합니다. 해당 언어는 지원하지 않습니다. "
            "한국어 또는 영어로 말씀해 주세요."
        )

    def get_prompt(self, language: str) -> str:
        """
        언어에 따라 Cloud LLM용 시스템 프롬프트를 반환한다.

        Args:
            language: "ko" 또는 "en".

        Returns:
            시스템 프롬프트 문자열 (SYSTEM_PROMPT_KO 또는 SYSTEM_PROMPT_EN).
        """
        return get_system_prompt(language)

    def get_timeout_prompt(self, language: str) -> str:
        """
        무발화 타임아웃 의사확인 메시지를 반환한다.

        Edge에서 15초 무발화 타이머가 만료되면 이 메시지를 TTS로 변환하여 전달한다.
        Cloud LLM 호출 없이 즉시 처리한다.

        Args:
            language: "ko" 또는 "en".

        Returns:
            의사확인 메시지.
            기본값: "계속 대화하시겠어요?" (ko) / "Are you still there?" (en)
        """
        prompts = self.config.get("timeout_messages", {}).get("prompt", {})
        default_ko = "계속 대화하시겠어요?"
        return prompts.get(language, prompts.get("ko", default_ko))

    def get_timeout_end(self, language: str) -> str:
        """
        타임아웃 세션 종료 안내 메시지를 반환한다.

        의사확인 후 5초 무응답 시 Edge가 이 메시지를 요청한다.
        Cloud LLM 호출 없이 즉시 처리한다.

        Args:
            language: "ko" 또는 "en".

        Returns:
            종료 안내 메시지.
            기본값: "대화를 종료합니다." (ko) / "Ending conversation." (en)
        """
        messages = self.config.get("timeout_messages", {}).get("end", {})
        default_ko = "대화를 종료합니다."
        return messages.get(language, messages.get("ko", default_ko))

    # ─────────────────────────────────────────────
    # LLM 모델 티어 선택
    # ─────────────────────────────────────────────

    def select_model_tier(self, text: str, language: str) -> str:
        """
        STT 텍스트를 기반으로 LLM 모델 티어를 결정한다.

        기본 haiku(빠름), 아래 조건 중 하나라도 충족하면 sonnet(정확)으로 승격한다.

        승격 조건 (우선순위 순):
            1. 명시적 깊이 요청 키워드 — "자세히", "설명해", "analyze" 등
            2. 복합 질문 — 한 발화에 "?" 또는 "？"가 2개 이상

        Args:
            text: STT로 인식된 사용자 발화 텍스트.
            language: 언어 코드 ("ko" / "en").

        Returns:
            "haiku" 또는 "sonnet".

        설계 이유:
            음성 비서 질문의 대부분은 단순하다 ("날씨 알려줘", "몇 시야").
            이런 경우 Haiku로 TTFT ~300-800ms를 절약할 수 있다.
            깊이 있는 질문은 Sonnet으로 정확한 응답을 제공한다.
            턴 수는 질문의 복잡성과 무관하므로 승격 조건에서 제외한다.
        """
        text_lower = text.strip().lower()

        # 1. 명시적 깊이 요청 키워드
        SONNET_TRIGGERS = {
            "ko": [
                "자세히", "구체적으로", "설명해", "분석해", "비교해",
                "차이점", "장단점", "원리", "이유가", "왜",
            ],
            "en": [
                "explain", "in detail", "analyze", "compare",
                "difference", "pros and cons", "how does", "why does",
                "what is", "tell me about",
            ],
        }
        triggers = SONNET_TRIGGERS.get(language, SONNET_TRIGGERS["en"])
        if any(trigger in text_lower for trigger in triggers):
            logger.debug(f"Sonnet 승격: 깊이 요청 키워드 감지 (lang={language})")
            return "sonnet"

        # 2. 복합 질문 — 물음표 2개 이상
        question_count = text.count("?") + text.count("？")
        if question_count >= 2:
            logger.debug(f"Sonnet 승격: 복합 질문 (물음표 {question_count}개)")
            return "sonnet"

        logger.debug("Haiku 사용: 단순 질문")
        return "haiku"

    # ─────────────────────────────────────────────
    # 시스템 상태 조회
    # ─────────────────────────────────────────────

    def get_system_stats(self) -> dict:
        """
        현재 시스템 상태를 조회한다.

        sysfs에서 GPU 사용률, 메모리 사용률, 온도를 읽어 반환한다.
        gRPC HealthCheck 응답과 Prometheus 메트릭에서 사용한다.

        Returns:
            dict: {
                "gpu_usage": float,      # GPU 사용률 (0.0 ~ 100.0, %)
                "memory_usage": float,   # 메모리 사용률 (0.0 ~ 1.0, 비율)
                "temperature": float,    # GPU 온도 (°C)
            }
        """
        return self.system_reader.get_stats()

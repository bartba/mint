"""
Cloud LLM 스트리밍 클라이언트 — LangChain 기반.

사용자의 음성(STT 텍스트)을 Cloud LLM(Claude/GPT)에 보내고,
응답을 토큰 단위로 스트리밍 수신하여 TTS 파이프라인에 전달한다.

왜 LangChain을 사용하는가?
    Claude와 GPT의 스트리밍 API는 각각 다르다:
        Claude: client.messages.stream() → text_stream
        GPT:    client.chat.completions.create(stream=True) → choices[0].delta

    LangChain은 이 차이를 추상화한다:
        chat_model.astream(messages) → 토큰 스트림

    어떤 프로바이더든 동일한 코드로 스트리밍할 수 있다.
    프로바이더 교체 시 ChatModel 객체만 바꾸면 된다.

    추가 이점:
        - 향후 RAG(검색 증강 생성), Tool Use 확장이 용이
        - 프롬프트 체인(여러 LLM 호출을 연결)도 LangChain 생태계로 쉽게 구현

LangChain 핵심 개념 (이 파일에서 사용하는 것들):
    ChatModel:
        LLM을 추상화한 객체. ChatAnthropic, ChatOpenAI 등.
        .astream(messages)로 비동기 스트리밍 호출.

    Message 타입:
        SystemMessage  — 시스템 프롬프트 (역할 설정)
        HumanMessage   — 사용자 입력
        AIMessage      — LLM 응답 (멀티턴 유지용으로 히스토리에 저장)

    astream():
        비동기 스트리밍 메서드. 토큰이 생성될 때마다 yield한다.
        내부적으로 각 프로바이더의 스트리밍 API를 호출한다.

멀티턴 대화 관리:
    음성 비서는 이전 맥락을 기억해야 자연스럽다.
        사용자: "서울 날씨 알려줘"
        비서:   "네, 서울은 맑고 기온은 이십 도에요."
        사용자: "내일은?" ← "서울 날씨"를 기억해야 답할 수 있음

    conversation_history에 HumanMessage/AIMessage를 쌓아서
    매 호출 시 messages에 함께 전달한다.

    히스토리가 너무 길어지면?
        - API 비용 증가 (입력 토큰 과금)
        - 응답 속도 저하 (긴 컨텍스트 처리)

        해결: 히스토리 요약 압축
            단순히 오래된 메시지를 버리면 맥락이 소실된다.
            대신 일정 턴 수(기본 18턴)에 도달하면:
            1. 앞쪽 절반(9턴)을 Haiku로 요약 → 1개 메시지로 압축
            2. 뒤쪽 절반(9턴)은 원본 유지
            → 맥락은 보존하면서 토큰 수를 ~60% 절감.

            요약 중에는 "잠시만요" 캐시 메시지를 TTS로 먼저 전달하여
            사용자가 대기 중임을 인지할 수 있게 한다.

모델 티어링:
    모든 질문에 고성능 모델을 쓸 필요는 없다.
    "지금 몇 시야?"는 Haiku로 충분, "양자역학 설명해줘"는 Sonnet이 적합.
    orchestrator.py의 select_model_tier()가 판단하고,
    이 클라이언트가 해당 티어의 ChatModel을 사용한다.

    | 티어 | 모델 | TTFT | 용도 |
    |------|------|------|------|
    | haiku | claude-haiku-4-5 | ~200-500ms | 대부분의 단순 질문 |
    | sonnet | claude-sonnet-4-6 | ~500-1500ms | 깊이 있는 응답 필요 시 |

    TTFT(Time To First Token):
        API 호출 후 첫 번째 토큰이 도착하기까지의 시간.
        Haiku가 Sonnet보다 ~300-800ms 빠르다.
        음성 비서에서 이 차이는 체감 지연에 직접 영향을 준다.

사용 예:
    from server.cloud.llm_client import CloudLLMClient

    client = CloudLLMClient(provider="claude", max_tokens=500, summarize_threshold_tokens=4000)

    # 스트리밍 응답 (TTS 파이프라인과 연결)
    async for token in client.get_response_stream(
        user_text="오늘 날씨 어때?",
        system_prompt=SYSTEM_PROMPT_KO,
        model_tier="haiku",
    ):
        print(token, end="")  # "네," "오늘" " 서울" " 날씨는" ...

    # 대화 종료 시 히스토리 초기화
    client.clear_history()

관련 모듈:
    server/cloud/prompt_templates.py — 시스템 프롬프트 (get_system_prompt)
    server/tts/supertonic_service.py — synthesize_stream()이 이 스트림을 소비
    server/orchestrator.py — select_model_tier()로 티어 결정, 이 클라이언트 호출
"""

import logging
from typing import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)


class CloudLLMClient:
    """
    LangChain 기반 Cloud LLM 스트리밍 클라이언트.

    생성 시 프로바이더(Claude/GPT)에 맞는 ChatModel을 초기화하고,
    get_response_stream()으로 스트리밍 응답을 받는다.

    API 키 설정:
        LangChain의 ChatAnthropic/ChatOpenAI는 환경변수에서 자동으로 키를 읽는다.
            Claude: ANTHROPIC_API_KEY
            GPT:    OPENAI_API_KEY

        코드에 키를 하드코딩하지 않는다. (보안 규칙)
        shared/config.py의 inject_env_vars()가 환경변수 존재를 확인한다.
    """

    # ── 히스토리 요약 설정 ──
    #
    # summarize_threshold_tokens (추정 토큰 수):
    #   히스토리의 추정 토큰 합계가 이 값에 도달하면 요약을 트리거한다.
    #   config/server.yaml의 cloud.summarize_threshold_tokens 값을 사용한다.
    #   기본값 4000 ≈ 30턴 (1턴 평균 ~130 토큰).
    #
    #   토큰 추정 방식 (tokenizer 의존성 없음):
    #     len(text) // 2 — 한국어(1자≈1토큰)와 영어(4자≈1토큰)의 중간값
    #     실제보다 보수적(크게 추정)이므로 임계값 도달이 실제보다 조금 이르다.
    #
    #   왜 절반 기준 분할인가?
    #       앞쪽 ~절반 토큰 분량을 요약하고, 뒤쪽 절반은 원본 유지한다.
    #       최근 대화의 디테일을 보존하면서 오래된 맥락을 압축한다.

    # 요약 중 사용자에게 전달할 대기 메시지.
    # TTS 파이프라인을 거쳐 음성으로 재생된다.
    # 요약은 ~500ms-1s 정도 걸리므로, 짧은 메시지가 적합하다.
    SUMMARIZE_WAIT_MESSAGES = {
        "ko": "잠시만요, 대화를 정리하고 있어요.",
        "en": "One moment, organizing our conversation.",
    }

    LOCAL_FALLBACK_MESSAGES = {
        "ko": "잠시만요, 로컬 모델로 전환합니다.",
        "en": "One moment, switching to local model.",
    }

    def __init__(
        self,
        provider: str = "claude",
        max_tokens: int = 500,
        summarize_threshold_tokens: int = 4000,
        timeout_s: int = 15,
        claude_models: dict | None = None,
        local_fallback: bool = True,
        local_model: str = "gemma3:1b",
        local_base_url: str = "http://localhost:11434",
    ) -> None:
        """
        LLM 클라이언트를 초기화한다.

        Args:
            provider: LLM 프로바이더.
                "claude" — Anthropic Claude (기본, 권장)
                "gpt"    — OpenAI GPT (대체용)

            max_tokens: 최대 응답 토큰 수.
                음성 비서는 짧은 응답이 적합하므로 500이 기본.
                토큰이란? 대략 한국어 1글자 ≈ 1-2토큰, 영어 1단어 ≈ 1토큰.
                500토큰 ≈ 한국어 ~250자, 영어 ~375단어.

            summarize_threshold_tokens: 히스토리 요약을 트리거할 추정 토큰 수.
                히스토리 글자 수 합계 // 2 로 추정한다 (tokenizer 불필요).
                4000이면 약 30턴에서 요약 실행.
                config/server.yaml의 cloud.summarize_threshold_tokens로 관리.

            timeout_s: API 호출 타임아웃 (초).
                Cloud API가 이 시간 내에 응답하지 않으면 에러.
                네트워크 상태나 API 부하에 따라 조정.

            claude_models: Claude 티어별 모델 ID 딕셔너리.
                config/server.yaml의 cloud.models 값을 전달한다.
                None이면 기본값(haiku-4-5, sonnet-4-6)을 사용한다.
                예: {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-4-6"}

            local_fallback: 로컬 폴백 LLM 활성화 여부.
                True이면 Cloud LLM 실패 시 Ollama(gemma3:1b)로 자동 전환한다.
                langchain-ollama 미설치 또는 Ollama 미구동 시 경고만 출력하고 비활성화.

            local_model: 로컬 폴백에 사용할 Ollama 모델명.
                기본값 "gemma3:1b" — 815MB, 한국어/영어 지원, 메모리 부담 최소.

            local_base_url: Ollama 서버 URL.
                기본값 "http://localhost:11434" — 로컬 Ollama 기본 포트.

        ChatModel 초기화:
            provider에 따라 적절한 LangChain ChatModel을 생성한다.
            Claude는 티어별(haiku, sonnet)로 별도 ChatModel을 만든다.
            GPT는 단일 모델만 사용한다.

            streaming=True:
                ChatModel 생성 시 스트리밍 모드를 활성화한다.
                astream() 호출 시 토큰 단위로 응답을 받을 수 있게 된다.
                False이면 전체 응답이 완성될 때까지 대기해야 한다.
        """
        self.provider = provider
        self.max_tokens = max_tokens
        self.summarize_threshold_tokens = summarize_threshold_tokens
        self.timeout_s = timeout_s

        # Claude 모델 티어 매핑 — config에서 주입받고, 없으면 기본값 사용
        self._claude_models = claude_models or {
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-6",
        }

        # 멀티턴 대화 히스토리
        #   HumanMessage와 AIMessage를 번갈아 쌓는다.
        #   get_response_stream() 호출 시 messages에 함께 전달하여
        #   LLM이 이전 맥락을 참조할 수 있게 한다.
        self.conversation_history: list[HumanMessage | AIMessage] = []

        # 히스토리 요약 저장소
        #   _summarize_history()가 호출되면 앞쪽 절반을 요약한 텍스트가 여기에 저장된다.
        #   이후 _build_messages()에서 시스템 프롬프트와 히스토리 사이에 삽입된다.
        #   요약이 여러 번 누적될 수 있다 (대화가 매우 길 경우).
        #
        #   None이면 아직 요약이 발생하지 않은 상태.
        self._history_summary: str | None = None

        # 프로바이더별 ChatModel 생성
        if provider == "claude":
            from langchain_anthropic import ChatAnthropic

            # 티어별 ChatModel을 미리 생성
            #   매 요청마다 생성하면 오버헤드가 발생하므로
            #   초기화 시 한 번만 만들어 재사용한다.
            self.models = {
                tier: ChatAnthropic(
                    model=model_id,
                    max_tokens=max_tokens,
                    streaming=True,
                    timeout=timeout_s,
                )
                for tier, model_id in self._claude_models.items()
            }
            logger.info(
                f"Claude ChatModel 초기화 완료: "
                f"{', '.join(f'{t}={m}' for t, m in self._claude_models.items())}"
            )

        elif provider == "gpt":
            from langchain_openai import ChatOpenAI

            self.models = {
                "default": ChatOpenAI(
                    model="gpt-5.4",
                    max_tokens=max_tokens,
                    streaming=True,
                    timeout=timeout_s,
                )
            }
            logger.info("GPT ChatModel 초기화 완료: gpt-5.4")

        else:
            raise ValueError(f"지원하지 않는 프로바이더: {provider}")

        # 로컬 폴백 ChatModel (Ollama)
        #   Cloud LLM 실패 시 gemma3:1b로 자동 전환한다.
        #   langchain-ollama 미설치 또는 Ollama 미구동 시 None으로 설정.
        self._local_fallback = None
        if local_fallback:
            try:
                from langchain_ollama import ChatOllama

                self._local_fallback = ChatOllama(
                    model=local_model,
                    base_url=local_base_url,
                    num_predict=max_tokens,
                )
                logger.info(f"로컬 폴백 LLM 초기화 완료: {local_model} @ {local_base_url}")
            except ImportError:
                logger.warning(
                    "langchain-ollama 미설치 — 로컬 폴백 비활성화. "
                    "pip install langchain-ollama"
                )
            except Exception as e:
                logger.warning(f"로컬 폴백 LLM 초기화 실패 — 비활성화: {e}")

    # ─────────────────────────────────────────────
    # 스트리밍 응답
    # ─────────────────────────────────────────────

    async def get_response_stream(
        self,
        user_text: str,
        system_prompt: str,
        model_tier: str = "haiku",
        language: str = "ko",
    ) -> AsyncIterator[str]:
        """
        LLM에 텍스트를 보내고 스트리밍 응답을 토큰 단위로 yield한다.

        이 메서드의 반환값(AsyncIterator[str])이
        SupertonicTTSService.synthesize_stream()의 text_stream 입력이 된다.

        전체 파이프라인에서의 위치:
            STT 텍스트 → get_response_stream() → synthesize_stream() → gRPC 전송
                         ^^^^^^^^^^^^^^^^^^^^^^^^
                         이 메서드

        Args:
            user_text: 사용자 발화 텍스트.
                Whisper STT의 결과를 그대로 전달한다.
                예: "오늘 서울 날씨 어때?"

            system_prompt: 시스템 프롬프트.
                prompt_templates.py의 get_system_prompt()이 반환한 문자열.
                언어에 따라 한국어/영어 프롬프트가 선택된다.

            model_tier: 모델 티어.
                "haiku"  — 단순 질문 (기본, TTFT ~200-500ms)
                "sonnet" — 복잡한 질문 (TTFT ~500-1500ms)
                orchestrator.py의 select_model_tier()가 결정한다.

            language: 언어 코드 ("ko" / "en").
                요약 대기 메시지와 에러 폴백 메시지의 언어를 결정한다.

        Yields:
            str: 토큰 문자열 조각.
                LLM이 생성하는 순서대로 1~수 개의 토큰이 yield된다.
                예: "네," → " 오늘" → " 서울" → " 날씨는" → " 맑아요."

        에러 처리:
            API 호출 실패 시 사용자에게 에러 메시지를 음성으로 전달한다.
            LLM이 응답하지 못해도 음성 비서가 "멈추지 않는" 것이 중요하다.

        동작 흐름:
            1. user_text를 HumanMessage로 히스토리에 추가
            2. 히스토리 추정 토큰 수가 summarize_threshold_tokens에 도달했는지 확인
               → 도달 시: 대기 메시지 yield → 앞쪽 절반 요약 → 히스토리 압축
            3. [SystemMessage + (요약) + 히스토리]를 messages로 구성
            4. ChatModel.astream()으로 스트리밍 호출
            5. 각 토큰을 yield하면서 full_response에 누적
            6. 스트림 종료 후 full_response를 AIMessage로 히스토리에 추가
        """
        # 1. 사용자 입력을 히스토리에 추가
        self.conversation_history.append(HumanMessage(content=user_text))

        # 2. 히스토리 요약 필요 여부 확인
        #    추정 토큰 수가 임계값에 도달하면 앞쪽 절반을 요약한다.
        #    요약 중에는 대기 메시지를 먼저 yield하여 사용자에게 TTS로 알린다.
        if self._estimate_history_tokens() >= self.summarize_threshold_tokens:
            # 대기 메시지를 먼저 yield → TTS 파이프라인이 즉시 음성으로 변환
            wait_msg = self.SUMMARIZE_WAIT_MESSAGES.get(language, self.SUMMARIZE_WAIT_MESSAGES["ko"])
            yield wait_msg + " "  # 공백 추가: TTS 문장 분리기가 이 뒤에 오는 본문과 구분하도록

            # 앞쪽 절반을 LLM으로 요약
            await self._summarize_history()

        # 3. ChatModel 선택
        #    Claude: 티어별 모델 선택 (haiku/sonnet)
        #    GPT: "default" 하나뿐
        if self.provider == "claude":
            chat_model = self.models.get(model_tier, self.models["haiku"])
        else:
            chat_model = self.models["default"]

        # 4. messages 구성
        messages = self._build_messages(system_prompt)

        logger.info(
            f"LLM 호출: provider={self.provider}, tier={model_tier}, "
            f"history={len(self.conversation_history)}개, "
            f"summary={'있음' if self._history_summary else '없음'}, "
            f"입력=\"{user_text[:50]}{'...' if len(user_text) > 50 else ''}\""
        )

        # 5. 스트리밍 호출 + 토큰 yield
        full_response = ""

        try:
            # chat_model.astream(messages):
            #   LangChain의 비동기 스트리밍 메서드.
            #   내부적으로 Claude/GPT의 스트리밍 API를 호출한다.
            #   토큰이 생성될 때마다 AIMessageChunk를 yield한다.
            #
            # AIMessageChunk란?
            #   LangChain이 스트리밍 중 각 토큰을 감싸는 객체.
            #   .content — 토큰 텍스트 (str)
            #   .response_metadata — 모델 메타데이터 (선택)
            #
            # 타임아웃 처리:
            #   ChatModel 생성 시 timeout=timeout_s를 전달했으므로,
            #   LangChain이 HTTP 레벨에서 타임아웃을 처리한다.
            #   timeout_s 내에 API 연결이 안 되면 예외가 발생한다.
            async for chunk in chat_model.astream(messages):
                token = chunk.content
                if token:
                    full_response += token
                    yield token

        except Exception as e:
            # API 에러 (타임아웃, 인증 실패, 네트워크 에러 등)
            #
            # 부분 응답이 이미 있으면 그것을 유지한다.
            # 응답이 전혀 없을 때만 폴백을 시도한다.
            logger.error(f"LLM 호출 실패: {e}")
            if not full_response:
                if self._local_fallback:
                    # Cloud LLM 실패 → 로컬 gemma3:1b로 자동 전환
                    switch_msg = self.LOCAL_FALLBACK_MESSAGES.get(
                        language, self.LOCAL_FALLBACK_MESSAGES["ko"]
                    )
                    yield switch_msg + " "
                    full_response = switch_msg + " "

                    try:
                        async for chunk in self._local_fallback.astream(messages):
                            token = chunk.content
                            if token:
                                full_response += token
                                yield token
                    except Exception as local_e:
                        # 로컬 폴백도 실패 (Ollama 미구동 등) → 정적 메시지로 최종 폴백
                        logger.error(f"로컬 폴백도 실패: {local_e}")
                        static = self._get_fallback_message("error", language)
                        yield static
                        full_response += static
                else:
                    # 로컬 폴백 비활성화 상태 → 정적 메시지
                    fallback = self._get_fallback_message("error", language)
                    yield fallback
                    full_response = fallback

        # 6. 완성된 응답을 히스토리에 추가
        if full_response:
            self.conversation_history.append(AIMessage(content=full_response))

        logger.info(
            f"LLM 응답 완료: {len(full_response)}자, "
            f"\"{full_response[:50]}{'...' if len(full_response) > 50 else ''}\""
        )

    # ─────────────────────────────────────────────
    # messages 구성
    # ─────────────────────────────────────────────

    def _build_messages(self, system_prompt: str) -> list:
        """
        LLM API에 전달할 messages 리스트를 구성한다.

        구성 순서:
            1. SystemMessage (시스템 프롬프트)
            2. 요약 컨텍스트 (있을 경우) — HumanMessage + AIMessage 쌍
            3. conversation_history (최근 대화)

        요약이 있을 때의 messages 구조:
            [
                SystemMessage("당신은 한국어 음성 비서입니다..."),
                HumanMessage("[이전 대화 요약을 참고하세요]"),
                AIMessage("사용자가 서울 날씨에 대해 물었고..."),
                HumanMessage("내일은?"),          ← 최근 히스토리
                AIMessage("네, 내일은 흐리고..."), ← 최근 히스토리
                HumanMessage("우산 필요할까?"),    ← 현재 입력
            ]

        요약을 별도 메시지 쌍으로 넣는 이유:
            - SystemMessage에 합치면 시스템 프롬프트가 길어져
              LLM이 규칙(30자 이내 등)을 잊을 수 있다.
            - HumanMessage+AIMessage 쌍이면 LLM은 이것을
              "이전에 있었던 대화"로 자연스럽게 인식한다.

        SystemMessage를 히스토리에 저장하지 않는 이유:
            시스템 프롬프트는 언어에 따라 달라질 수 있다.
            사용자가 한국어로 말하다가 영어로 전환하면 프롬프트도 바뀌어야 한다.
            매 호출 시 현재 언어에 맞는 프롬프트를 넣는 것이 유연하다.
        """
        messages = [SystemMessage(content=system_prompt)]

        # 요약이 있으면 히스토리 앞에 삽입
        if self._history_summary:
            messages.append(HumanMessage(content="[이전 대화 요약을 참고하세요]"))
            messages.append(AIMessage(content=self._history_summary))

        messages.extend(self.conversation_history)
        return messages

    # ─────────────────────────────────────────────
    # 토큰 추정
    # ─────────────────────────────────────────────

    def _estimate_tokens(self, text: str) -> int:
        """
        텍스트의 토큰 수를 글자 수 기반으로 추정한다 (tokenizer 불필요).

        추정 공식: len(text) // 2

        근거:
            한국어/CJK: BPE 특성상 1자 ≈ 1 토큰
            영어/ASCII: 4자 ≈ 1 토큰
            혼합 텍스트 중간값 ≈ 2자당 1 토큰 (보수적 추정)

        보수적으로 추정하면 실제보다 토큰이 크게 계산되어
        요약이 조금 이르게 트리거된다 — 컨텍스트 비용 절감에 유리하다.
        """
        return len(text) // 2

    def _estimate_history_tokens(self) -> int:
        """
        현재 conversation_history 전체의 추정 토큰 수를 반환한다.

        요약(_history_summary)이 있으면 그 길이도 포함한다.
        get_response_stream()에서 요약 트리거 여부를 판단할 때 사용한다.
        """
        total = sum(
            self._estimate_tokens(str(msg.content))
            for msg in self.conversation_history
        )
        if self._history_summary:
            total += self._estimate_tokens(self._history_summary)
        return total

    # ─────────────────────────────────────────────
    # 히스토리 요약
    # ─────────────────────────────────────────────

    async def _summarize_history(self) -> None:
        """
        히스토리의 앞쪽 절반을 LLM으로 요약하여 압축한다.

        동작:
            1. conversation_history에서 앞쪽 summarize_count개를 추출
            2. 이 메시지들을 Haiku에게 보내서 요약을 요청
            3. 요약 결과를 _history_summary에 저장 (기존 요약이 있으면 합침)
            4. conversation_history에서 요약된 부분을 제거

        전후 비교:
            요약 전 (30개 메시지 = 15턴):
                [H1, A1, H2, A2, ..., H8, A8, H9, A9, H10, A10, ..., H15, A15]
                 ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                 앞쪽 15개 → 요약 대상           뒤쪽 15개 → 유지

            요약 후 (15개 메시지 = 7~8턴 + 요약):
                _history_summary = "사용자가 서울 날씨에 대해 물었고..."
                conversation_history = [H9, A9, ..., H15, A15]

        요약에 Haiku를 사용하는 이유:
            - 요약은 단순 작업이므로 Haiku로 충분
            - TTFT ~200-500ms로 빠름
            - 비용 절약

        요약이 여러 번 발생할 수 있는가?
            네. 대화가 매우 길면 요약 후에도 다시 임계값에 도달할 수 있다.
            이때 기존 요약 + 새 앞쪽 절반을 함께 요약하여 하나로 합친다.
            → 요약의 요약이 되지만, 핵심 맥락은 유지된다.
        """
        # 앞쪽 절반 토큰에 해당하는 분할 인덱스 탐색
        #   Human+AI 쌍 단위(짝수 인덱스)로 분할하여 메시지 쌍이 깨지지 않도록 한다.
        target = self.summarize_threshold_tokens // 2
        split = max(2, len(self.conversation_history) // 2)  # 기본값: 절반
        accumulated = 0
        for i in range(0, len(self.conversation_history) - 1, 2):
            accumulated += self._estimate_tokens(str(self.conversation_history[i].content))
            accumulated += self._estimate_tokens(str(self.conversation_history[i + 1].content))
            if accumulated >= target:
                split = i + 2  # 이 쌍까지 포함
                break

        to_summarize = self.conversation_history[:split]
        remaining = self.conversation_history[split:]

        # 요약 대상 메시지를 텍스트로 변환
        #   "사용자: 서울 날씨 알려줘\n비서: 네, 서울은 맑고..."
        lines = []
        for msg in to_summarize:
            role = "사용자" if isinstance(msg, HumanMessage) else "비서"
            lines.append(f"{role}: {msg.content}")
        conversation_text = "\n".join(lines)

        # 기존 요약이 있으면 합쳐서 요약 (요약의 요약)
        if self._history_summary:
            conversation_text = (
                f"[이전 요약]\n{self._history_summary}\n\n"
                f"[이어진 대화]\n{conversation_text}"
            )

        # 요약 프롬프트
        #   요약 결과가 다시 LLM 컨텍스트로 들어가므로,
        #   간결하되 핵심 정보(주제, 결론, 사용자 선호)를 유지하도록 지시한다.
        summarize_prompt = (
            "아래 대화를 3~5문장으로 요약하세요. "
            "핵심 주제, 사용자의 질문/요청, 주요 결론을 포함하세요. "
            "요약만 출력하고 다른 말은 하지 마세요."
        )

        messages = [
            SystemMessage(content=summarize_prompt),
            HumanMessage(content=conversation_text),
        ]

        # Haiku로 요약 실행 (스트리밍 아닌 일괄 호출)
        #   요약은 내부 처리이므로 스트리밍이 필요 없다.
        #   ainvoke()는 전체 응답을 한 번에 반환하는 비동기 메서드이다.
        try:
            # Haiku ChatModel 선택 (요약은 항상 Haiku — 빠르고 저렴)
            if self.provider == "claude":
                summarize_model = self.models["haiku"]
            else:
                summarize_model = self.models["default"]

            result = await summarize_model.ainvoke(messages)
            self._history_summary = result.content

            # 요약된 부분을 히스토리에서 제거
            self.conversation_history = remaining

            logger.info(
                f"히스토리 요약 완료: {len(to_summarize)}개 메시지 (~{accumulated}토큰) → "
                f"요약 {len(self._history_summary)}자, 남은 히스토리 {len(remaining)}개"
            )

        except Exception as e:
            # 요약 실패 시에도 대화는 계속되어야 한다.
            # 폴백: 단순 트리밍으로 앞쪽을 제거한다 (맥락 소실 감수).
            logger.error(f"히스토리 요약 실패, 단순 트리밍 폴백: {e}")
            self.conversation_history = remaining

    # ─────────────────────────────────────────────
    # 히스토리 관리
    # ─────────────────────────────────────────────

    def clear_history(self) -> None:
        """
        대화 히스토리와 요약을 완전히 초기화한다.

        호출 시점:
            - 대화 종료 인텐트 매칭 시 ("잘가", "goodbye" 등)
            - 타임아웃 세션 종료 시 (무발화 → 의사확인 → 무응답)
            - Edge에서 새 세션 시작 시

        왜 초기화하는가?
            이전 대화의 맥락이 새 대화에 영향을 주면 안 된다.
            "서울 날씨" 대화 후 종료 → 새 대화에서 "내일은?"이라고 하면
            히스토리가 남아있으면 "서울 날씨"로 답하지만,
            실제로는 완전히 새로운 대화이므로 혼란을 줄 수 있다.

        요약(_history_summary)도 함께 초기화한다.
            요약에 이전 세션의 맥락이 남아있으면 새 대화에 혼선을 줄 수 있다.
        """
        count = len(self.conversation_history)
        has_summary = self._history_summary is not None
        self.conversation_history.clear()
        self._history_summary = None
        logger.info(
            f"대화 히스토리 초기화: {count}개 메시지 제거"
            f"{', 요약 제거' if has_summary else ''}"
        )

    # ─────────────────────────────────────────────
    # 에러 폴백 메시지
    # ─────────────────────────────────────────────

    def _get_fallback_message(self, error_type: str, language: str = "ko") -> str:
        """
        LLM 호출 실패 시 사용자에게 전달할 폴백 메시지를 반환한다.

        음성 비서가 "멈추는" 것은 최악의 UX이다.
        API 에러가 나더라도 무언가 음성으로 안내해야 한다.

        이 메시지는 TTS 파이프라인을 거쳐 사용자에게 음성으로 전달된다.

        Args:
            error_type: 에러 유형.
                "timeout" — API 응답 시간 초과
                "error"   — 기타 API 에러
            language: 응답 언어 ("ko" / "en").
                get_response_stream()의 language 인수를 그대로 전달한다.
        """
        messages = {
            "ko": {
                "timeout": "죄송합니다, 응답 시간이 초과되었습니다. 다시 말씀해 주세요.",
                "error": "죄송합니다, 일시적인 오류가 발생했습니다. 다시 말씀해 주세요.",
            },
            "en": {
                "timeout": "Sorry, the response timed out. Please try again.",
                "error": "Sorry, a temporary error occurred. Please try again.",
            },
        }
        lang_messages = messages.get(language, messages["ko"])
        return lang_messages.get(error_type, lang_messages["error"])


# ─────────────────────────────────────────────
# CLI 테스트
# ─────────────────────────────────────────────

if __name__ == "__main__":
    """
    단독 실행으로 LLM 클라이언트를 테스트한다.

    사용법:
        # Claude 테스트 (ANTHROPIC_API_KEY 환경변수 필요)
        ANTHROPIC_API_KEY=sk-ant-... python -m server.cloud.llm_client \
            --test --provider claude --text "오늘 서울 날씨 어때?"

        # GPT 테스트 (OPENAI_API_KEY 환경변수 필요)
        OPENAI_API_KEY=sk-... python -m server.cloud.llm_client \
            --test --provider gpt --text "Hello, how are you?"

        # 모델 티어 지정
        python -m server.cloud.llm_client \
            --test --provider claude --tier sonnet --text "양자역학을 설명해줘"

    출력 예시:
        === LLM 테스트 ===
        프로바이더: claude
        모델 티어: haiku
        입력: "오늘 서울 날씨 어때?"

        === 스트리밍 응답 ===
        네, 오늘 서울 날씨는 맑고 기온은 이십 도 정도에요.
        바람이 조금 불어서 외출하기 좋은 날씨입니다.
        가벼운 겉옷 하나 챙기시면 좋을 것 같아요.

        === 결과 ===
        총 토큰 수: 47
        응답 시간: 1.23초
    """
    import argparse
    import asyncio
    import time

    from shared.config import get_config
    from shared.utils import setup_logging
    from server.cloud.prompt_templates import get_system_prompt

    parser = argparse.ArgumentParser(description="LLM 클라이언트 테스트")
    parser.add_argument("--test", action="store_true", help="테스트 실행")
    parser.add_argument("--provider", type=str, default="claude", help="프로바이더 (claude/gpt)")
    parser.add_argument("--tier", type=str, default="haiku", help="모델 티어 (haiku/sonnet)")
    parser.add_argument("--text", type=str, default="안녕하세요, 오늘 날씨가 어때요?", help="입력 텍스트")
    parser.add_argument("--lang", type=str, default="ko", help="언어 코드 (ko/en)")
    args = parser.parse_args()

    if not args.test:
        parser.print_help()
        exit(0)

    config = get_config("server")
    setup_logging("DEBUG")
    cloud_config = config.get("cloud", {})

    print(f"\n=== LLM 테스트 ===")
    print(f"프로바이더: {args.provider}")
    print(f"모델 티어: {args.tier}")
    print(f"입력: \"{args.text}\"")

    client = CloudLLMClient(
        provider=args.provider,
        max_tokens=cloud_config.get("max_tokens", 500),
        summarize_threshold_tokens=cloud_config.get("summarize_threshold_tokens", 4000),
        timeout_s=cloud_config.get("timeout_s", 30),
        claude_models=cloud_config.get("models"),
    )

    system_prompt = get_system_prompt(args.lang)

    print(f"\n=== 스트리밍 응답 ===")
    start_time = time.time()
    token_count = 0
    full_text = ""

    async def run_test():
        global token_count, full_text

        async for token in client.get_response_stream(
            user_text=args.text,
            system_prompt=system_prompt,
            model_tier=args.tier,
        ):
            print(token, end="", flush=True)
            token_count += 1
            full_text += token

    asyncio.run(run_test())

    elapsed = time.time() - start_time
    print(f"\n\n=== 결과 ===")
    print(f"총 토큰 수: {token_count}")
    print(f"응답 시간: {elapsed:.2f}초")
    print(f"히스토리: {len(client.conversation_history)}개")

"""
시스템 프롬프트 관리.

Cloud LLM(Claude/GPT)에게 "음성 비서답게 응답하라"고 지시하는
시스템 프롬프트를 정의한다.

시스템 프롬프트란?
    LLM API 호출 시 messages의 첫 번째로 전달하는 지시문이다.
    사용자 메시지가 아닌, 시스템이 LLM에게 주는 "역할 설정"이다.

    messages = [
        SystemMessage("당신은 한국어 음성 비서입니다..."),  ← 시스템 프롬프트
        HumanMessage("오늘 날씨 어때?"),                   ← 사용자 입력
        AIMessage("네, 오늘 서울 날씨는..."),              ← 이전 응답
        HumanMessage("내일은?"),                           ← 현재 입력
    ]

    LLM은 시스템 프롬프트의 규칙을 따르면서 사용자에게 응답한다.
    모든 멀티턴 대화에서 동일한 시스템 프롬프트가 유지된다.

음성 비서 프롬프트의 핵심 원칙:
    1. 짧은 문장 — TTS 변환 지연을 줄이려면 문장이 짧아야 한다.
       긴 문장은 TTS가 완성될 때까지 사용자가 기다려야 한다.

    2. 첫 문장 호응 — "네,", "글쎄요," 같은 짧은 호응으로 시작.
       첫 문장이 5~10자면 ~200ms만에 TTS가 완성된다.
       → 사용자가 "시스템이 듣고 있다"고 느끼는 시간이 크게 단축된다.
       (CLAUDE.md '최적화 2: LLM 첫 문장 단축 유도' 참고)

    3. 마크다운/이모지 금지 — TTS가 "별표별표"로 읽거나 무음을 생성한다.
       _sanitize_for_tts()가 방어하지만, 프롬프트에서 미리 차단하는 것이 효율적.

    4. 숫자 한글 표기 — "3개"를 TTS가 "삼개"로 읽을 수 있다.
       프롬프트에서 "세 개"로 쓰도록 지시하면 자연스러운 발음이 된다.

사용 예:
    from server.cloud.prompt_templates import get_system_prompt

    prompt = get_system_prompt("ko")  # 한국어 음성비서 프롬프트
    prompt = get_system_prompt("en")  # 영어 음성비서 프롬프트

관련 모듈:
    server/cloud/llm_client.py — 이 프롬프트를 messages에 포함하여 LLM 호출
    server/orchestrator.py — 언어에 따라 get_system_prompt() 호출
"""


# ─────────────────────────────────────────────
# 한국어 시스템 프롬프트
# ─────────────────────────────────────────────

SYSTEM_PROMPT_KO = """당신은 한국어 AI 음성 비서입니다.

규칙:
- 응답의 첫 문장은 "네,", "글쎄요,", "좋은 질문이에요," 같은 짧은 호응으로 시작합니다.
- 간결하고 자연스러운 구어체로 응답합니다.
- 한 문장은 30자 이내로 유지합니다. TTS 변환 지연을 최소화하기 위함입니다.
- 전체 응답은 3~5문장 이내로 합니다.
- 마크다운, 이모지, 특수 기호를 사용하지 않습니다. 음성으로 읽기 어렵습니다.
- 숫자는 한글로 풀어 씁니다. (예: "3개" → "세 개")
- 영어 단어가 포함될 경우 한국어 발음으로 표기합니다. (예: "AI" → "에이아이")
- 목록이 필요하면 "첫째, 둘째" 형식으로 말합니다.
"""

# 규칙별 의도 설명:
#
# "짧은 호응으로 시작"
#     → 첫 문장을 5~10자로 단축하여 TTS 첫 오디오를 ~200ms만에 생성.
#       사용자 체감 지연: Cloud TTFT(~500ms) + 첫 문장 누적(~100ms) + TTS(~50ms) ≈ 650ms
#       호응 없이 바로 본문을 시작하면: TTFT + 긴 문장 누적(~500ms) + TTS ≈ 1.5s
#
# "30자 이내"
#     → 한국어 30자는 약 2~3초 분량의 음성.
#       문장이 길면 TTS 변환 시간도 길어져 다음 문장 시작이 지연된다.
#       synthesize_stream()의 문장 단위 파이프라이닝 효과를 극대화하려면
#       문장이 짧을수록 좋다.
#
# "마크다운/이모지 금지"
#     → TTS 서비스의 _sanitize_for_tts()가 방어하지만,
#       LLM 단계에서 아예 생성하지 않는 것이 더 효율적이다.
#       정제 후 문장 구조가 깨질 수 있기 때문.
#
# "숫자 한글 표기"
#     → TTS가 "3"을 "삼"으로 읽을지 "셋"으로 읽을지 예측 불가.
#       프롬프트에서 한글로 쓰도록 유도하면 항상 자연스러운 발음이 된다.


# ─────────────────────────────────────────────
# 영어 시스템 프롬프트
# ─────────────────────────────────────────────

SYSTEM_PROMPT_EN = """You are a voice assistant.

Rules:
- Start your response with a short acknowledgment like "Sure,", "Well,", or "Great question,".
- Keep responses concise and conversational.
- Each sentence should be under 15 words for TTS optimization.
- Total response should be 3-5 sentences.
- No markdown, emoji, or special symbols.
- Spell out numbers when natural in speech.
"""

# 영어 프롬프트의 "15 words"는 한국어 30자와 비슷한 발화 시간(~2-3초)이다.
# 영어는 한국어보다 단어당 음절이 짧아서 단어 수 기준이 적절하다.


# ─────────────────────────────────────────────
# 프롬프트 선택 함수
# ─────────────────────────────────────────────

def get_system_prompt(language: str) -> str:
    """
    언어 코드에 따라 시스템 프롬프트를 반환한다.

    Args:
        language: "ko" 또는 "en".
            Whisper STT가 감지한 언어 코드를 그대로 전달한다.
            지원하지 않는 언어 코드가 오면 한국어 프롬프트를 기본으로 사용.

    Returns:
        시스템 프롬프트 문자열.

    사용 예:
        prompt = get_system_prompt("ko")
        # → SYSTEM_PROMPT_KO 반환

        prompt = get_system_prompt("en")
        # → SYSTEM_PROMPT_EN 반환

        prompt = get_system_prompt("ja")
        # → SYSTEM_PROMPT_KO 반환 (미지원 언어 → 한국어 폴백)
    """
    if language == "en":
        return SYSTEM_PROMPT_EN
    return SYSTEM_PROMPT_KO

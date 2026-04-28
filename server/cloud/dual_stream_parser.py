"""
LLM 스트리밍 토큰에서 [SPOKEN]...[/SPOKEN] / [DISPLAY]...[/DISPLAY] 섹션을
채널별로 분리하는 파서.

display_enabled=True일 때만 사용. LLM은 다음 순서로 출력한다:
    [SPOKEN]구어체 응답[/SPOKEN]
    [DISPLAY]마크다운 응답[/DISPLAY]

태그가 토큰 경계에 걸릴 수 있으므로 hold-back 버퍼를 사용한다.
"""

import logging
from typing import AsyncIterator, Literal

logger = logging.getLogger(__name__)

Channel = Literal["tts", "display"]

# 가장 긴 태그 길이([/DISPLAY] = 10) 기준 hold-back 버퍼 크기
_MAX_TAG_LEN = 10

_TAG_SPOKEN_OPEN = "[SPOKEN]"
_TAG_SPOKEN_CLOSE = "[/SPOKEN]"
_TAG_DISPLAY_OPEN = "[DISPLAY]"
_TAG_DISPLAY_CLOSE = "[/DISPLAY]"

_STATES = ("PREAMBLE", "SPOKEN", "INTER", "DISPLAY", "DONE")


class DualStreamParser:
    """
    LLM 토큰 스트림 → (channel, text) 튜플 스트림 변환기.

    사용법:
        async for channel, text in DualStreamParser().parse(token_stream):
            ...
    """

    async def parse(
        self,
        token_stream: AsyncIterator[str],
    ) -> AsyncIterator[tuple[Channel, str]]:
        state = "PREAMBLE"
        hold = ""  # 태그 경계 처리를 위한 hold-back 버퍼

        async for token in token_stream:
            hold += token

            while True:
                if state == "PREAMBLE":
                    idx = hold.find(_TAG_SPOKEN_OPEN)
                    if idx != -1:
                        hold = hold[idx + len(_TAG_SPOKEN_OPEN):]
                        state = "SPOKEN"
                        # 태그 발견 → 즉시 다음 상태 처리 (break 없음)
                    else:
                        # 태그 미발견 → 다음 토큰 대기
                        if len(hold) >= _MAX_TAG_LEN:
                            hold = hold[-(_MAX_TAG_LEN - 1):]
                        break

                elif state == "SPOKEN":
                    idx = hold.find(_TAG_SPOKEN_CLOSE)
                    if idx != -1:
                        content = hold[:idx]
                        if content:
                            yield "tts", content
                        hold = hold[idx + len(_TAG_SPOKEN_CLOSE):]
                        state = "INTER"
                        # 태그 발견 → 즉시 다음 상태 처리 (break 없음)
                    else:
                        # 태그 미발견 → 안전한 범위만 yield하고 다음 토큰 대기
                        safe = len(hold) - (_MAX_TAG_LEN - 1)
                        if safe > 0:
                            yield "tts", hold[:safe]
                            hold = hold[safe:]
                        break

                elif state == "INTER":
                    idx = hold.find(_TAG_DISPLAY_OPEN)
                    if idx != -1:
                        hold = hold[idx + len(_TAG_DISPLAY_OPEN):]
                        state = "DISPLAY"
                        # 태그 발견 → 즉시 다음 상태 처리 (break 없음)
                    else:
                        # 태그 미발견 → 다음 토큰 대기
                        if len(hold) >= _MAX_TAG_LEN:
                            hold = hold[-(_MAX_TAG_LEN - 1):]
                        break

                elif state == "DISPLAY":
                    idx = hold.find(_TAG_DISPLAY_CLOSE)
                    if idx != -1:
                        content = hold[:idx]
                        if content:
                            yield "display", content
                        hold = hold[idx + len(_TAG_DISPLAY_CLOSE):]
                        state = "DONE"
                        break  # DONE 상태 → 더 처리할 것 없음
                    else:
                        # 태그 미발견 → 안전한 범위만 yield하고 다음 토큰 대기
                        safe = len(hold) - (_MAX_TAG_LEN - 1)
                        if safe > 0:
                            yield "display", hold[:safe]
                            hold = hold[safe:]
                        break

                elif state == "DONE":
                    break

        # 스트림 종료 후 hold-back 버퍼 플러시
        if hold:
            if state == "SPOKEN":
                logger.warning("스트림 종료: [/SPOKEN] 태그 없이 종료됨")
                yield "tts", hold
            elif state == "DISPLAY":
                logger.warning("스트림 종료: [/DISPLAY] 태그 없이 종료됨")
                yield "display", hold

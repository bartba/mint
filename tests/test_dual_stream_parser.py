"""
DualStreamParser 테스트.

plan.md 2-3 요구 케이스:
1. 태그가 한 토큰 안에 모두 포함되는 케이스
2. 태그가 두 토큰에 걸치는 경계 케이스 ("[SPOK", "EN]네,")
3. DISPLAY 섹션이 닫히지 않고 종료되는 비정상 케이스
4. SPOKEN→DISPLAY 사이 공백/개행만 들어오는 케이스
"""

import pytest
from server.cloud.dual_stream_parser import DualStreamParser


async def collect(tokens: list[str]) -> list[tuple[str, str]]:
    """토큰 리스트를 AsyncIterator로 만들어 파싱한 결과를 수집한다."""
    async def _stream():
        for t in tokens:
            yield t

    results = []
    async for channel, text in DualStreamParser().parse(_stream()):
        results.append((channel, text))
    return results


def joined(results: list[tuple[str, str]], channel: str) -> str:
    """특정 채널의 텍스트를 모두 이어붙인다."""
    return "".join(text for ch, text in results if ch == channel)


# ─────────────────────────────────────────────
# 1. 태그가 한 토큰 안에 모두 포함되는 케이스
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_in_one_token():
    """전체 응답이 토큰 하나에 담겨 올 때."""
    tokens = ["[SPOKEN]안녕하세요.[/SPOKEN][DISPLAY]# 안녕\n내용[/DISPLAY]"]
    results = await collect(tokens)

    assert joined(results, "tts") == "안녕하세요."
    assert joined(results, "display") == "# 안녕\n내용"


@pytest.mark.asyncio
async def test_normal_multitoken():
    """정상적인 멀티 토큰 스트림."""
    tokens = [
        "[SPOKEN]", "오늘 날씨는 ", "맑아요.", "[/SPOKEN]",
        "\n",
        "[DISPLAY]", "## 날씨\n맑음", "[/DISPLAY]",
    ]
    results = await collect(tokens)

    assert joined(results, "tts") == "오늘 날씨는 맑아요."
    assert joined(results, "display") == "## 날씨\n맑음"


# ─────────────────────────────────────────────
# 2. 태그가 두 토큰에 걸치는 경계 케이스
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tag_split_across_tokens():
    """[SPOKEN] 태그가 두 토큰에 걸쳐 있는 경우."""
    tokens = ["[SPOK", "EN]네, 맑아요.[/SPOKEN][DISPLAY]맑음[/DISPLAY]"]
    results = await collect(tokens)

    assert joined(results, "tts") == "네, 맑아요."
    assert joined(results, "display") == "맑음"


@pytest.mark.asyncio
async def test_close_tag_split_across_tokens():
    """[/SPOKEN] 종료 태그가 두 토큰에 걸쳐 있는 경우."""
    tokens = ["[SPOKEN]안녕.[/SPO", "KEN][DISPLAY]내용[/DISPLAY]"]
    results = await collect(tokens)

    assert joined(results, "tts") == "안녕."
    assert joined(results, "display") == "내용"


@pytest.mark.asyncio
async def test_display_tag_split_across_tokens():
    """[DISPLAY] 태그가 두 토큰에 걸쳐 있는 경우."""
    tokens = ["[SPOKEN]안녕.[/SPOKEN][DIS", "PLAY]내용[/DISPLAY]"]
    results = await collect(tokens)

    assert joined(results, "tts") == "안녕."
    assert joined(results, "display") == "내용"


# ─────────────────────────────────────────────
# 3. 비정상 종료 케이스
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_display_not_closed():
    """[/DISPLAY] 태그 없이 스트림이 종료되는 비정상 케이스."""
    tokens = ["[SPOKEN]안녕.[/SPOKEN][DISPLAY]미완성 내용"]
    results = await collect(tokens)

    # tts는 정상 수신
    assert joined(results, "tts") == "안녕."
    # display는 hold-back 버퍼에서 플러시됨
    assert "미완성 내용" in joined(results, "display")


@pytest.mark.asyncio
async def test_spoken_not_closed():
    """[/SPOKEN] 없이 스트림이 종료되는 비정상 케이스."""
    tokens = ["[SPOKEN]미완성 응답"]
    results = await collect(tokens)

    assert "미완성 응답" in joined(results, "tts")


# ─────────────────────────────────────────────
# 4. SPOKEN→DISPLAY 사이 공백/개행만 들어오는 케이스
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_whitespace_between_sections():
    """두 섹션 사이에 공백/개행 토큰이 들어오는 경우."""
    tokens = [
        "[SPOKEN]안녕.[/SPOKEN]",
        "\n", "\n", "   ",  # 공백/개행 토큰들
        "[DISPLAY]내용[/DISPLAY]",
    ]
    results = await collect(tokens)

    assert joined(results, "tts") == "안녕."
    assert joined(results, "display") == "내용"
    # tts 채널에 공백이 섞이지 않았는지 확인
    tts_texts = [text for ch, text in results if ch == "tts"]
    assert all(t == "안녕." for t in tts_texts)


# ─────────────────────────────────────────────
# 5. 채널 순서 검증
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_channel_order():
    """tts 채널이 display 채널보다 먼저 yield되어야 한다."""
    tokens = [
        "[SPOKEN]첫 문장.[/SPOKEN]",
        "[DISPLAY]긴 마크다운 내용[/DISPLAY]",
    ]
    results = await collect(tokens)

    channels = [ch for ch, _ in results]
    tts_indices = [i for i, ch in enumerate(channels) if ch == "tts"]
    display_indices = [i for i, ch in enumerate(channels) if ch == "display"]

    assert tts_indices, "tts 채널 결과가 없음"
    assert display_indices, "display 채널 결과가 없음"
    assert max(tts_indices) < min(display_indices), "tts가 display보다 먼저여야 함"

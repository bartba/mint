"""
멀티턴 + 언어 전환 테스트.

시나리오:
    Turn 1 (ko): 한국어 질문
    Turn 2 (ko): 이전 맥락 참조 (멀티턴 확인)
    Turn 3 (en): 영어로 전환
    Turn 4 (en): 영어 맥락 유지
    Turn 5 (ko): 다시 한국어로 전환

실행:
    python -m server.cloud.test_multiturn
"""

import asyncio
import sys
import time

sys.path.insert(0, "/home/bart/workspace/mint")

from server.cloud.llm_client import CloudLLMClient
from server.cloud.prompt_templates import get_system_prompt


TURNS = [
    {"lang": "ko", "text": "인공지능이 뭔지 간단히 설명해 줘."},
    {"lang": "ko", "text": "방금 말한 것 중에서 가장 중요한 점이 뭐야?"},
    {"lang": "en", "text": "Now let's switch to English. What's the weather like today?"},
    {"lang": "en", "text": "What did we talk about just before the weather question?"},
    {"lang": "ko", "text": "다시 한국어로 돌아올게. 지금까지 우리가 나눈 대화를 요약해 줘."},
]


async def run():
    import os
    from dotenv import load_dotenv
    load_dotenv("/home/bart/workspace/mint/server/.env")

    client = CloudLLMClient(provider="claude", max_tokens=300, summarize_threshold_tokens=4000)

    for i, turn in enumerate(TURNS, 1):
        lang = turn["lang"]
        text = turn["text"]
        system_prompt = get_system_prompt(lang)
        tier = "haiku"

        print(f"\n{'='*60}")
        print(f"  Turn {i} [{lang.upper()}]  tier={tier}")
        print(f"  사용자: {text}")
        print(f"{'='*60}")
        print(f"  비서: ", end="", flush=True)

        t_start = time.time()
        first_chunk_time = None
        full = ""

        async for token in client.get_response_stream(
            user_text=text,
            system_prompt=system_prompt,
            model_tier=tier,
            language=lang,
        ):
            if first_chunk_time is None:
                first_chunk_time = time.time() - t_start
            print(token, end="", flush=True)
            full += token

        elapsed = time.time() - t_start
        print()
        print(f"\n  [첫 청크: {first_chunk_time:.2f}s | 전체: {elapsed:.2f}s | {len(full)}자 | 히스토리: {len(client.conversation_history)}개]")

    print(f"\n{'='*60}")
    print("  테스트 완료")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(run())

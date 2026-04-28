import re


class SentenceSplitter:
    """
    LLM 스트리밍 토큰을 문장/절 단위로 분리한다.

    split()은 buffer를 받아 분리된 리스트를 반환한다.
    마지막 요소는 아직 완성되지 않은 미완성 버퍼이므로,
    len(result) > 1일 때 result[:-1]이 완성된 문장이다.
    """

    # 문장 종결 부호 + 쉼표 분리 패턴 (supertonic_service._split_sentences에서 이관)
    _SENTENCE_SPLIT = re.compile(r"(?<=[.?!。？！])\s+")
    _CLAUSE_SPLIT = re.compile(r"(?<=[,，])\s+")

    def split(self, buffer: str) -> list[str]:
        """
        buffer를 문장/절 경계에서 분리한다.

        1단계: 종결 부호(. ? ! 등) 뒤 공백에서 분리.
        2단계: 10자 초과인 구절에서 쉼표 뒤 공백으로 추가 분리.

        Returns:
            분리된 리스트. 마지막 요소는 미완성 버퍼.
            길이 1이면 완성된 문장이 없다.
        """
        if not buffer:
            return [buffer]

        parts = self._SENTENCE_SPLIT.split(buffer)

        result = []
        for part in parts:
            if len(part) > 10 and re.search(r"[,，]\s", part):
                sub = self._CLAUSE_SPLIT.split(part)
                result.extend(sub)
            else:
                result.append(part)

        return result if result else [buffer]

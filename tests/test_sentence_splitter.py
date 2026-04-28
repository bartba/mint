import pytest
from server.cloud.sentence_splitter import SentenceSplitter


@pytest.fixture
def splitter():
    return SentenceSplitter()


def test_terminal_punctuation(splitter):
    result = splitter.split("네, 안녕.")
    # 완성 1개 + 미완성 빈 버퍼
    assert len(result) == 1 or (len(result) == 2 and result[-1] == "")
    assert result[0] == "네, 안녕."


def test_two_sentences(splitter):
    result = splitter.split("네, 안녕. 반가워요.")
    assert len(result) >= 2
    assert result[0] == "네, 안녕."


def test_clause_split_long(splitter):
    # 쉼표 뒤 + 전체 10자 초과 → 절 분리
    text = "네, 오늘 서울 날씨는 맑고, 내일은"
    result = splitter.split(text)
    assert len(result) >= 2
    assert any("맑고," in r for r in result)


def test_short_comma_no_split(splitter):
    # 짧은 구절은 쉼표 분리 안 함
    result = splitter.split("네,")
    assert len(result) == 1


def test_empty_string(splitter):
    result = splitter.split("")
    assert result == [""]


def test_whitespace_only(splitter):
    result = splitter.split("   ")
    assert isinstance(result, list)
    assert len(result) >= 1


def test_no_punctuation_single_word(splitter):
    result = splitter.split("안녕")
    assert result == ["안녕"]


def test_token_boundary(splitter):
    # "맑." + "다." 처럼 부호가 토큰 경계에 걸리는 케이스
    # 전체 버퍼로 들어왔을 때 정상 분리되는지
    result = splitter.split("맑. 다.")
    assert len(result) >= 2
    assert result[0] == "맑."

"""
intent_classifier.py — 임베딩 기반 인텐트 분류기.

sentence-transformers(`paraphrase-multilingual-MiniLM-L12-v2`)로
사용자 발화와 사전 정의된 예시 문장들의 코사인 유사도를 계산하여
인텐트를 판별한다. Cloud LLM을 호출하지 않고도 명령성 발화를
결정론적으로, 빠르게(~50ms CPU) 처리하기 위한 컴포넌트.

설계 포인트:
- startup 1회에 모든 예시를 임베딩하여 메모리에 캐시 (추론 시 사용자 발화만 임베딩)
- 유사도 최고점 >= threshold → 인텐트 확정, 미만 → None (Cloud 라우팅)
- pause_listening 인텐트는 duration_pattern 정규식으로 duration_s도 함께 추출
- encode()가 동기 호출이므로 상위에서 asyncio.to_thread()로 래핑하여 사용
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


@dataclass
class _IntentSpec:
    """내부용: 인텐트 하나의 임베딩/메타데이터 묶음."""
    name: str
    example_embeddings: np.ndarray       # shape: (n_examples, dim), L2 정규화됨
    duration_pattern: re.Pattern | None = None
    default_duration_s: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class IntentClassifier:
    """임베딩 유사도 기반 인텐트 분류기.

    Args:
        config: `config/server.yaml` 전체 dict. 다음 키를 사용:
            - intent_classifier.model / threshold / device
            - intents.<name>.examples / duration_pattern / default_duration_s / ...
    """

    def __init__(self, config: dict):
        classifier_cfg = config["intent_classifier"]
        self.threshold: float = float(classifier_cfg["threshold"])
        device: str = classifier_cfg.get("device", "cpu")
        model_name: str = classifier_cfg["model"]

        logger.info("Loading sentence-transformers model: %s (device=%s)", model_name, device)
        self._model = SentenceTransformer(model_name, device=device)

        self._intents: list[_IntentSpec] = []
        for name, spec in config.get("intents", {}).items():
            examples: list[str] = spec.get("examples", [])
            if not examples:
                logger.warning("Intent '%s' has no examples — skipped", name)
                continue

            # normalize_embeddings=True → L2 정규화된 벡터.
            # 이 경우 코사인 유사도 = 내적(dot product)으로 계산 가능.
            embeddings = self._model.encode(
                examples,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )

            duration_pattern = None
            if "duration_pattern" in spec:
                duration_pattern = re.compile(spec["duration_pattern"])

            self._intents.append(_IntentSpec(
                name=name,
                example_embeddings=embeddings,
                duration_pattern=duration_pattern,
                default_duration_s=spec.get("default_duration_s"),
                extra={k: v for k, v in spec.items()
                       if k not in ("examples", "duration_pattern", "default_duration_s")},
            ))
            logger.info("Registered intent '%s' with %d examples", name, len(examples))

        logger.info("IntentClassifier ready. threshold=%.2f, intents=%d",
                    self.threshold, len(self._intents))

    def classify(self, text: str) -> dict[str, Any] | None:
        """사용자 발화를 분류한다.

        Returns:
            - 인텐트 확정 시: {"intent": name, "score": float, ...추가 필드}
              (pause_listening의 경우 "duration_s" 포함)
            - 인텐트 없음: None (상위에서 Cloud LLM으로 라우팅)
        """
        text = (text or "").strip()
        if not text or not self._intents:
            return None

        # 사용자 발화 1문장 임베딩 → shape: (dim,)
        query_vec = self._model.encode(
            text,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )

        best_name: str | None = None
        best_score: float = -1.0
        best_spec: _IntentSpec | None = None

        for spec in self._intents:
            # 내적 = 코사인 유사도 (둘 다 L2 정규화됨)
            # shape: (n_examples,). 그중 최대값이 이 인텐트의 유사도.
            sims = spec.example_embeddings @ query_vec
            score = float(np.max(sims))
            if score > best_score:
                best_score = score
                best_name = spec.name
                best_spec = spec

        logger.debug("Intent classify: text=%r, best=%s, score=%.3f",
                     text, best_name, best_score)

        if best_spec is None or best_score < self.threshold:
            return None

        result: dict[str, Any] = {"intent": best_name, "score": best_score}

        if best_spec.name == "pause_listening":
            result["duration_s"] = self._extract_duration_s(text, best_spec)

        return result

    def _extract_duration_s(self, text: str, spec: _IntentSpec) -> int:
        """pause_listening 인텐트에서 '5분', '10 min' 등 지속 시간을 초로 환산."""
        default = spec.default_duration_s or 300
        if spec.duration_pattern is None:
            return default

        m = spec.duration_pattern.search(text)
        if not m:
            return default

        try:
            value = int(m.group(1))
        except (ValueError, IndexError):
            return default

        unit = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
        unit_map = {
            "초": 1, "sec": 1,
            "분": 60, "min": 60,
            "시간": 3600, "hour": 3600,
        }
        multiplier = unit_map.get(unit, 60)   # 단위 불명확하면 분으로 가정
        return value * multiplier

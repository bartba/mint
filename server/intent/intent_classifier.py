"""
intent_classifier.py — 임베딩 기반 인텐트 분류기.

sentence-transformers(`paraphrase-multilingual-MiniLM-L12-v2`)로
사용자 발화와 사전 정의된 예시 문장들의 코사인 유사도를 계산하여
인텐트를 판별한다. Cloud LLM을 호출하지 않고도 명령성 발화를
결정론적으로, 빠르게(~50ms CPU) 처리하기 위한 컴포넌트.

설계 포인트:
- startup 1회에 모든 예시를 임베딩하여 메모리에 캐시 (추론 시 사용자 발화만 임베딩)
- 유사도 최고점 >= threshold → 인텐트 확정, 미만 → None (Cloud 라우팅)
- encode()가 동기 호출이므로 상위에서 asyncio.to_thread()로 래핑하여 사용
"""

from __future__ import annotations

import logging
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
    extra: dict[str, Any] = field(default_factory=dict)


class IntentClassifier:
    """임베딩 유사도 기반 인텐트 분류기.

    Args:
        config: `config/server.yaml` 전체 dict. 다음 키를 사용:
            - intent_classifier.model / threshold / device
            - intents.<name>.examples / ...
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

            self._intents.append(_IntentSpec(
                name=name,
                example_embeddings=embeddings,
                extra={k: v for k, v in spec.items() if k != "examples"},
            ))
            logger.info("Registered intent '%s' with %d examples", name, len(examples))

        logger.info("IntentClassifier ready. threshold=%.2f, intents=%d",
                    self.threshold, len(self._intents))

    def classify(self, text: str) -> dict[str, Any] | None:
        """사용자 발화를 분류한다.

        Returns:
            - 인텐트 확정 시: {"intent": name, "score": float}
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

        return {"intent": best_name, "score": best_score}

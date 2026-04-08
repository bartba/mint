"""
shared 패키지 — Edge와 Server가 공유하는 설정, 모델, 유틸리티.

사용 예:
    from shared.config import get_config
    from shared.models import AudioChunkData, STTResultData
    from shared.utils import setup_logging, pcm_to_float32
"""

from shared.config import get_config
from shared.models import AudioChunkData, STTResultData, TTSRequestData, ServerStatusData
from shared.utils import setup_logging, pcm_to_float32, float32_to_pcm, chunk_bytes

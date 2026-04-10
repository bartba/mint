"""
디바이스 프로파일 정의 모듈.

Edge/Server 디바이스별 하드웨어 특성과 기본 설정을 여기서 관리한다.
새 디바이스를 추가할 때는 DEVICE_PROFILES에 항목만 추가하면 된다.

사용 예:
    from shared.device_profiles import get_device_profile, list_devices

    profile = get_device_profile("jetson_orin_nx")
    print(profile["role"])              # "server"
    print(profile["memory_mb"])         # 16384

    edge_devices = list_devices("edge")
    # → ["jetson_nano", "raspberry_pi_4b"]
"""

from typing import Optional


# ─────────────────────────────────────────────
# 디바이스 프로파일 정의
# ─────────────────────────────────────────────

DEVICE_PROFILES: dict[str, dict] = {

    # ── Edge 디바이스 ─────────────────────────────────

    "jetson_nano": {
        "role": "edge",
        "memory_mb": 4096,
        "has_gpu": True,
        # 사용하는 마이크 하드웨어
        "audio_device": "ReSpeaker XVF3800",
        # Supertonic TTS 추론 품질/속도 트레이드오프 (메모리 제약으로 낮게 설정)
        "tts_inference_steps": 4,
        # VAD 감도 (Silero VAD threshold)
        "vad_threshold": 0.5,
    },

    "raspberry_pi_4b": {
        "role": "edge",
        "memory_mb": 8192,
        "has_gpu": False,
        # Poly Sync 20: USB 스피커폰 (마이크+스피커 통합)
        "audio_device": "Poly Sync 20",
        # Pi는 GPU 없으므로 TTS 추론 스텝을 줄여 CPU 부하 경감
        "tts_inference_steps": 3,
        "vad_threshold": 0.5,
    },

    # ── Server 디바이스 ───────────────────────────────

    "jetson_orin_nano": {
        "role": "server",
        "memory_mb": 8192,
        "has_gpu": True,
        # Jetson GPU 부하 읽기 경로 (모니터링용)
        "gpu_load_path": "/sys/devices/platform/bus@0/17000000.gpu/load",
        # Jetson 전력 모드: MAXN = 최대 성능
        "power_mode": "MAXN",
    },

    "jetson_orin_nx": {
        "role": "server",
        "memory_mb": 16384,
        "has_gpu": True,
        "gpu_load_path": "/sys/devices/platform/bus@0/17000000.gpu/load",
        "power_mode": "MAXN",
    },
}


# ─────────────────────────────────────────────
# 조회 함수
# ─────────────────────────────────────────────

def get_device_profile(device_id: str) -> dict:
    """
    디바이스 ID로 프로파일을 반환한다.

    Args:
        device_id: 디바이스 식별자 (예: "jetson_orin_nx")

    Returns:
        해당 디바이스의 프로파일 딕셔너리 (복사본).

    Raises:
        ValueError: 등록되지 않은 device_id인 경우.

    복사본을 반환하는 이유:
        호출자가 반환값을 수정해도 DEVICE_PROFILES 원본이 오염되지 않도록.
    """
    if device_id not in DEVICE_PROFILES:
        available = ", ".join(DEVICE_PROFILES.keys())
        raise ValueError(
            f"알 수 없는 디바이스: '{device_id}'. "
            f"사용 가능한 디바이스: {available}"
        )

    import copy
    return copy.deepcopy(DEVICE_PROFILES[device_id])


def list_devices(role: Optional[str] = None) -> list[str]:
    """
    등록된 디바이스 목록을 반환한다.

    Args:
        role: "edge" 또는 "server"로 필터링. None이면 전체 반환.

    Returns:
        디바이스 ID 문자열 리스트.

    사용 예:
        list_devices()          # → ["jetson_nano", "raspberry_pi_4b", ...]
        list_devices("edge")    # → ["jetson_nano", "raspberry_pi_4b"]
        list_devices("server")  # → ["jetson_orin_nano", "jetson_orin_nx"]
    """
    if role is None:
        return list(DEVICE_PROFILES.keys())

    return [
        device_id
        for device_id, profile in DEVICE_PROFILES.items()
        if profile["role"] == role
    ]

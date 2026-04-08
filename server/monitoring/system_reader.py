"""
시스템 리소스 읽기 모듈.

Jetson Orin Nano 8GB의 sysfs를 직접 읽어서
GPU 사용률, 메모리, 온도를 조회한다.
모델 로드 전에 메모리 여유를 확인하여 OOM(Out of Memory)을 방지한다.

통합 메모리란?
    일반 PC는 CPU RAM(16GB)과 GPU VRAM(8GB)이 물리적으로 분리되어 있다.
    Jetson은 CPU와 GPU가 하나의 8GB 메모리를 공유한다.
    → GPU에 Whisper 3GB를 올리면, 시스템 전체에서 쓸 수 있는 메모리가 5GB로 줄어든다.
    → 그래서 모델 로드 전에 반드시 여유 메모리를 확인해야 한다.

데이터 소스 (sysfs):
    Linux 커널이 /proc, /sys 아래에 시스템 정보를 가상 파일로 노출한다.
    외부 라이브러리(jtop 등) 없이 직접 읽을 수 있어 의존성이 없다.

    /proc/meminfo                                       → 메모리 사용량
    /sys/devices/platform/bus@0/17000000.gpu/load        → GPU 사용률 (0~1000, ‰)
    /sys/devices/virtual/thermal/thermal_zone*/temp      → 센서별 온도 (밀리도)

사용 예:
    from server.monitoring.system_reader import SystemReader

    reader = SystemReader()

    # 메모리 확인 후 모델 로드
    if reader.check_memory_safe(required_mb=3000):
        load_whisper_model()
    else:
        print("메모리 부족! INT8 모드로 전환 필요")

    # 시스템 상태 조회
    stats = reader.get_stats()
    print(f"GPU 사용률: {stats['gpu_usage']}%")
    print(f"온도: {stats['temperature']}°C")

관련 모듈:
    monitoring/metrics.py — 이 모듈이 읽은 데이터를 Prometheus로 내보냄 (Phase 5)
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Jetson sysfs 경로 상수
# ─────────────────────────────────────────────
#
# Jetson Orin Nano의 GPU 디바이스 경로.
# 다른 Jetson 모델(Xavier, AGX Orin 등)은 경로가 다를 수 있다.
# 그 경우 이 상수만 수정하면 된다.

GPU_LOAD_PATH = Path("/sys/devices/platform/bus@0/17000000.gpu/load")

# thermal_zone은 번호가 달라질 수 있으므로, 초기화 시 gpu-thermal을 검색한다.
THERMAL_BASE = Path("/sys/devices/virtual/thermal")


class SystemReader:
    """
    Jetson sysfs에서 시스템 리소스를 읽는다.

    읽기 전용 — 시스템 상태를 변경하지 않는다.
    외부 의존성 없이 순수 파일 I/O만 사용한다.

    데이터 소스:
        - /proc/meminfo → 메모리 사용량
        - /sys/.../gpu/load → GPU 사용률
        - /sys/.../thermal_zone*/temp → GPU 온도
    """

    def __init__(self) -> None:
        """
        GPU 온도 센서의 sysfs 경로를 탐색한다.

        thermal_zone 번호는 부팅 시마다 또는 Jetson 모델에 따라 달라질 수 있다.
        그래서 초기화 시 모든 thermal_zone의 type 파일을 읽어서
        "gpu-thermal"인 것을 찾아 경로를 저장해둔다.

        이 디바이스의 실제 매핑:
            thermal_zone0 → cpu-thermal
            thermal_zone1 → gpu-thermal  ← 이것을 찾음
            thermal_zone2~8 → cv, soc, tj 등
        """
        self._gpu_temp_path = self._find_gpu_thermal()

        if self._gpu_temp_path:
            logger.info(f"GPU 온도 센서: {self._gpu_temp_path}")
        else:
            logger.warning("GPU 온도 센서를 찾을 수 없음 — 온도 0.0°C로 보고")

        if GPU_LOAD_PATH.exists():
            logger.info(f"GPU 사용률 센서: {GPU_LOAD_PATH}")
        else:
            logger.warning(f"GPU 사용률 센서 없음 ({GPU_LOAD_PATH}) — 0.0%로 보고")

    def _find_gpu_thermal(self) -> Path | None:
        """
        /sys/devices/virtual/thermal/ 아래에서 gpu-thermal 센서를 찾는다.

        탐색 방법:
            thermal_zone0/, thermal_zone1/, ... 각 디렉토리 안의
            "type" 파일을 읽어서 내용이 "gpu-thermal"인 것을 찾는다.
            찾으면 같은 디렉토리의 "temp" 파일 경로를 반환한다.

        Returns:
            gpu-thermal의 temp 파일 경로, 또는 None (센서 없음)
        """
        if not THERMAL_BASE.exists():
            return None

        # thermal_zone* 디렉토리를 순회
        for zone_dir in sorted(THERMAL_BASE.glob("thermal_zone*")):
            type_file = zone_dir / "type"
            if not type_file.exists():
                continue

            try:
                sensor_type = type_file.read_text().strip()
                if sensor_type == "gpu-thermal":
                    return zone_dir / "temp"
            except OSError:
                continue

        return None

    # ─────────────────────────────────────────────
    # 메모리 사용량 조회
    # ─────────────────────────────────────────────

    def get_memory_usage(self) -> dict:
        """
        시스템 메모리 사용량을 조회한다.

        Returns:
            dict: {
                "total_mb": int,   # 전체 메모리 (MB)
                "used_mb": int,    # 사용 중 (MB)
                "free_mb": int,    # 여유 (MB) — MemAvailable 기준
            }

        Jetson에서는 이 값이 GPU 메모리를 포함한다 (통합 메모리).

        /proc/meminfo 파싱:
            Linux 커널이 실시간으로 제공하는 가상 파일.
            파일 내용 예시:
                MemTotal:        8037048 kB
                MemFree:         1234567 kB
                MemAvailable:    3456789 kB
                ...

            MemAvailable을 사용하는 이유:
                MemFree는 "완전히 비어있는" 메모리만 포함한다.
                MemAvailable은 버퍼/캐시 중 회수 가능한 메모리를 포함하여
                "실제로 쓸 수 있는" 메모리를 나타낸다.
                → 모델 로드 가능 여부를 판단할 때는 MemAvailable이 더 정확하다.
        """
        meminfo_path = Path("/proc/meminfo")

        if not meminfo_path.exists():
            logger.error("/proc/meminfo를 찾을 수 없음")
            return {"total_mb": 0, "used_mb": 0, "free_mb": 0}

        meminfo = {}
        with open(meminfo_path, "r") as f:
            for line in f:
                # 각 줄 형식: "MemTotal:        8037048 kB"
                parts = line.split()
                if len(parts) >= 2:
                    # "MemTotal:" → "MemTotal" (콜론 제거)
                    key = parts[0].rstrip(":")
                    # "8037048" → 8037048 (정수 변환)
                    value_kb = int(parts[1])
                    meminfo[key] = value_kb

        total_kb = meminfo.get("MemTotal", 0)
        available_kb = meminfo.get("MemAvailable", 0)
        used_kb = total_kb - available_kb

        return {
            "total_mb": total_kb // 1024,
            "used_mb": used_kb // 1024,
            "free_mb": available_kb // 1024,
        }

    # ─────────────────────────────────────────────
    # GPU 사용률 조회
    # ─────────────────────────────────────────────

    def _read_gpu_load(self) -> float:
        """
        GPU 사용률을 sysfs에서 읽는다.

        /sys/devices/platform/bus@0/17000000.gpu/load 파일은
        GPU 사용률을 0~1000 범위의 정수(‰, 퍼밀)로 제공한다.
            0 = GPU 유휴 (0%)
            500 = 50% 사용
            1000 = 100% 사용

        10으로 나눠서 0~100% 범위로 변환한다.

        Returns:
            GPU 사용률 (0.0 ~ 100.0, %)
        """
        if not GPU_LOAD_PATH.exists():
            return 0.0

        try:
            raw = GPU_LOAD_PATH.read_text().strip()
            # 0~1000 (‰) → 0~100 (%)
            return int(raw) / 10.0
        except (OSError, ValueError) as e:
            logger.warning(f"GPU 사용률 읽기 실패: {e}")
            return 0.0

    # ─────────────────────────────────────────────
    # GPU 온도 조회
    # ─────────────────────────────────────────────

    def _read_gpu_temperature(self) -> float:
        """
        GPU 온도를 sysfs에서 읽는다.

        thermal_zone의 temp 파일은 온도를 밀리도(m°C) 단위로 제공한다.
            52437 = 52.437°C

        1000으로 나눠서 °C로 변환한다.

        Returns:
            GPU 온도 (°C). 센서 없으면 0.0.
        """
        if self._gpu_temp_path is None or not self._gpu_temp_path.exists():
            return 0.0

        try:
            raw = self._gpu_temp_path.read_text().strip()
            # 밀리도 → 도
            return int(raw) / 1000.0
        except (OSError, ValueError) as e:
            logger.warning(f"GPU 온도 읽기 실패: {e}")
            return 0.0

    # ─────────────────────────────────────────────
    # 시스템 상태 종합 조회
    # ─────────────────────────────────────────────

    def get_stats(self) -> dict:
        """
        GPU 사용률, 메모리 사용률, 온도를 종합적으로 조회한다.

        Returns:
            dict: {
                "gpu_usage": float,      # GPU 사용률 (0.0 ~ 100.0, %)
                "memory_usage": float,   # 메모리 사용률 (0.0 ~ 1.0, 비율)
                "temperature": float,    # GPU 온도 (°C)
            }

        memory_usage가 비율(0~1)인 이유:
            gRPC ServerStatus 메시지의 memory_usage 필드가 float(비율)이다.
            프로토콜 정의에 맞춰서 비율로 반환한다.
        """
        mem = self.get_memory_usage()
        total = mem["total_mb"]
        used = mem["used_mb"]
        memory_ratio = used / total if total > 0 else 0.0

        return {
            "gpu_usage": self._read_gpu_load(),
            "memory_usage": round(memory_ratio, 3),
            "temperature": self._read_gpu_temperature(),
        }

    # ─────────────────────────────────────────────
    # 메모리 안전 확인
    # ─────────────────────────────────────────────

    def check_memory_safe(self, required_mb: int = 500) -> bool:
        """
        추가 모델을 로드할 만큼 메모리 여유가 있는지 확인한다.

        Args:
            required_mb: 필요한 여유 메모리 (MB).
                기본값 500MB는 최소 안전 마진이다.
                Whisper 로드 시에는 3000을, TTS 로드 시에는 500을 전달한다.

        Returns:
            True이면 로드 가능, False이면 메모리 부족.

        사용 예:
            # Whisper 모델 로드 전 확인 (FP16 = ~3GB)
            if reader.check_memory_safe(required_mb=3000):
                stt_service = STTService(compute_type="float16")
            elif reader.check_memory_safe(required_mb=1500):
                # FP16은 부족하지만 INT8은 가능 → 자동 폴백
                stt_service = STTService(compute_type="int8_float16")
                logger.warning("메모리 부족 — INT8 모드로 폴백")
            else:
                raise RuntimeError("모델을 로드할 메모리가 없음")
        """
        mem = self.get_memory_usage()
        free_mb = mem["free_mb"]
        is_safe = free_mb >= required_mb

        if is_safe:
            logger.debug(
                f"메모리 확인 OK: 여유 {free_mb}MB >= 필요 {required_mb}MB"
            )
        else:
            logger.warning(
                f"메모리 부족: 여유 {free_mb}MB < 필요 {required_mb}MB"
            )

        return is_safe


# ─────────────────────────────────────────────
# CLI 테스트
# ─────────────────────────────────────────────

if __name__ == "__main__":
    """
    단독 실행으로 시스템 상태를 확인한다.

    사용법:
        python -m server.monitoring.system_reader --check-memory

    출력 예시:
        === 시스템 메모리 ===
        전체: 7607 MB
        사용: 4425 MB
        여유: 3182 MB

        === 시스템 상태 ===
        GPU 사용률: 0.0%
        메모리 사용률: 58.2%
        GPU 온도: 52.4°C

        === 모델 로드 가능 여부 ===
        Whisper FP16 (3000MB): 가능
        Whisper INT8 (1500MB): 가능
        Supertonic TTS (500MB): 가능
    """
    import argparse
    from shared.utils import setup_logging

    parser = argparse.ArgumentParser(description="Jetson 시스템 상태 확인")
    parser.add_argument(
        "--check-memory",
        action="store_true",
        help="메모리 상태 및 모델 로드 가능 여부 출력",
    )
    args = parser.parse_args()

    setup_logging("DEBUG")
    reader = SystemReader()

    # ── 메모리 사용량 ──
    mem = reader.get_memory_usage()
    print("\n=== 시스템 메모리 ===")
    print(f"전체: {mem['total_mb']} MB")
    print(f"사용: {mem['used_mb']} MB")
    print(f"여유: {mem['free_mb']} MB")

    # ── 시스템 상태 ──
    stats = reader.get_stats()
    print("\n=== 시스템 상태 ===")
    print(f"GPU 사용률: {stats['gpu_usage']}%")
    print(f"메모리 사용률: {stats['memory_usage'] * 100:.1f}%")
    print(f"GPU 온도: {stats['temperature']}°C")

    # ── 모델 로드 가능 여부 ──
    print("\n=== 모델 로드 가능 여부 ===")

    models = [
        ("Whisper FP16", 3000),
        ("Whisper INT8", 1500),
        ("Supertonic TTS", 500),
    ]

    for name, required in models:
        safe = reader.check_memory_safe(required_mb=required)
        status = "가능" if safe else "불가 (메모리 부족)"
        print(f"{name} ({required}MB): {status}")

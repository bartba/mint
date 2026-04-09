"""
설정 관리 모듈.

YAML 파일에서 설정을 로드하고, 환경변수로 민감한 값을 오버라이드한다.
get_config()를 통해 앱 어디서든 동일한 설정 객체에 접근할 수 있다.

사용 예:
    from shared.config import get_config

    config = get_config("server")                           # config/default.yaml + config/server.yaml 병합
    config = get_config("server", device="jetson_orin_nx")  # 디바이스 프로파일까지 병합
    print(config["stt"]["model_size"])                      # "large-v3"
    print(config["cloud"]["api_key"])                       # 환경변수 API_KEY 값
    print(config["local_llm_model"])                        # "gemma4:e4b" (디바이스 프로파일 값)
"""

import os
import copy
from pathlib import Path
from typing import Optional

import yaml

from shared.device_profiles import get_device_profile


# ─────────────────────────────────────────────
# 프로젝트 경로 계산
# ─────────────────────────────────────────────

# 이 파일의 위치: shared/config.py
# 프로젝트 루트: shared/ 의 부모 = voice-assistant/
#
# Path(__file__)          → /home/bart/workspace/mint/shared/config.py
# .resolve()              → 심볼릭 링크 해결한 절대경로
# .parent                 → /home/bart/workspace/mint/shared/
# .parent                 → /home/bart/workspace/mint/          ← 프로젝트 루트
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 설정 파일 디렉토리
CONFIG_DIR = PROJECT_ROOT / "config"


# ─────────────────────────────────────────────
# 딕셔너리 깊은 병합 (deep merge)
# ─────────────────────────────────────────────

def deep_merge(base: dict, override: dict) -> dict:
    """
    두 딕셔너리를 깊이 병합한다.
    override에 있는 값이 base의 값을 덮어쓴다.

    일반 dict.update()와의 차이:
        base     = {"stt": {"model": "large", "beam": 5}}
        override = {"stt": {"model": "medium"}}

        dict.update() 결과: {"stt": {"model": "medium"}}          ← beam 사라짐!
        deep_merge() 결과:  {"stt": {"model": "medium", "beam": 5}} ← beam 유지!

    중첩된 딕셔너리 안의 값도 개별적으로 병합해주는 것이 핵심이다.
    """
    # base를 복사해서 원본이 변경되지 않도록 함
    result = copy.deepcopy(base)

    for key, value in override.items():
        # 양쪽 다 dict이면 → 재귀적으로 더 깊이 병합
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            # 그 외 → override 값으로 덮어쓰기
            result[key] = copy.deepcopy(value)

    return result


# ─────────────────────────────────────────────
# YAML 파일 로드
# ─────────────────────────────────────────────

def load_yaml(file_path: Path) -> dict:
    """
    YAML 파일을 읽어서 Python 딕셔너리로 반환한다.

    yaml.safe_load()를 사용하는 이유:
        yaml.load()는 임의의 Python 객체를 실행할 수 있어 보안 위험.
        yaml.safe_load()는 기본 타입(str, int, list, dict)만 허용.
    """
    if not file_path.exists():
        return {}

    with open(file_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # YAML 파일이 비어있으면 safe_load()가 None을 반환
    return data if data is not None else {}


# ─────────────────────────────────────────────
# 환경변수 오버라이드
# ─────────────────────────────────────────────

def inject_env_vars(config: dict) -> dict:
    """
    환경변수에서 민감한 설정값을 읽어 config에 주입한다.

    왜 환경변수를 쓰는가?
        API 키를 YAML 파일에 적으면 git에 커밋될 위험이 있다.
        환경변수는 프로세스 메모리에만 존재하므로 안전하다.

    설정 방법 (터미널에서):
        export ANTHROPIC_API_KEY="sk-ant-..."
        export OPENAI_API_KEY="sk-..."

    또는 .env 파일에 적고 shell에서 source:
        source .env
    """
    # cloud 섹션이 없으면 생성
    if "cloud" not in config:
        config["cloud"] = {}

    # 환경변수 → config 매핑
    # os.environ.get()은 환경변수가 없으면 None을 반환 (에러 아님)
    env_mappings = {
        "ANTHROPIC_API_KEY": ("cloud", "anthropic_api_key"),
        "OPENAI_API_KEY": ("cloud", "openai_api_key"),
    }

    for env_name, (section, key) in env_mappings.items():
        value = os.environ.get(env_name)
        if value:
            if section not in config:
                config[section] = {}
            config[section][key] = value

    return config


# ─────────────────────────────────────────────
# 싱글턴: get_config()
# ─────────────────────────────────────────────

# 모듈 레벨 변수 — (role, device) 쌍을 키로 캐시
# 예: {("server", "jetson_orin_nx"): {...}, ("edge", None): {...}}
_config_cache: dict[tuple, dict] = {}


def get_config(role: str = "server", device: Optional[str] = None) -> dict:
    """
    설정을 로드하고 캐시된 결과를 반환한다.

    Args:
        role:   "server" 또는 "edge". 해당 YAML 파일을 추가 로드한다.
        device: 디바이스 ID (예: "jetson_orin_nx"). 지정하면 디바이스
                프로파일을 config에 딥 머지한다. None이면 프로파일 미적용.

    Returns:
        병합된 설정 딕셔너리.

    로드 순서:
        1. config/default.yaml      (공통 기본값)
        2. config/{role}.yaml       (역할별 설정, default를 덮어씀)
        3. 디바이스 프로파일         (device 지정 시, role 설정을 덮어씀)
        4. 환경변수                  (API 키 등 민감한 값)

    캐시 키:
        (role, device) 튜플로 관리한다.
        같은 role이라도 device가 다르면 별도 캐시 항목을 가진다.

    사용 예:
        config = get_config("server")
        config = get_config("server", device="jetson_orin_nx")
        config["stt"]["model_size"]      # → "large-v3"
        config["local_llm_model"]        # → "gemma4:e4b"  (디바이스 프로파일 값)
    """
    cache_key = (role, device)

    # 이미 로드했으면 캐시 반환 (싱글턴)
    if cache_key in _config_cache:
        return _config_cache[cache_key]

    # 1단계: 공통 기본값 로드
    default_config = load_yaml(CONFIG_DIR / "default.yaml")

    # 2단계: 역할별 설정 로드 및 병합
    role_config = load_yaml(CONFIG_DIR / f"{role}.yaml")
    merged = deep_merge(default_config, role_config)

    # 3단계: 디바이스 프로파일 병합 (device가 지정된 경우)
    # 프로파일의 값이 YAML 설정보다 우선한다.
    # 예: 프로파일의 local_llm_model이 server.yaml 기본값을 덮어씀
    if device is not None:
        profile = get_device_profile(device)
        merged = deep_merge(merged, profile)

    # 4단계: 환경변수 주입
    merged = inject_env_vars(merged)

    # 캐시에 저장
    _config_cache[cache_key] = merged

    return _config_cache[cache_key]


def reset_config() -> None:
    """
    캐시를 전체 초기화한다. 테스트 시 설정을 다시 로드해야 할 때 사용.

    프로덕션에서는 호출할 필요 없다 — 앱 실행 중 설정이 바뀌지 않으므로.
    """
    global _config_cache
    _config_cache = {}

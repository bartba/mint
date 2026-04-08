#!/usr/bin/env bash
# ─────────────────────────────────────────────
# setup_server.sh — Jetson Orin Nano 8GB 서버 초기 설정
# ─────────────────────────────────────────────
#
# 서버를 처음 세팅할 때 한 번만 실행한다.
# 이후 코드 변경 시에는 이 스크립트를 다시 실행할 필요 없다.
#
# 사용법:
#   chmod +x scripts/setup_server.sh
#   ./scripts/setup_server.sh
#
# set 옵션 설명:
#   -e: 명령어 하나라도 실패하면 즉시 중단 (에러 전파 방지)
#   -u: 정의되지 않은 변수 사용 시 에러 (오타 방지)
#   -o pipefail: 파이프(|) 중간 명령어 실패도 감지
set -euo pipefail

# ─────────────────────────────────────────────
# 색상 출력 (터미널에서 단계를 구분하기 쉽게)
# ─────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'  # No Color (색상 리셋)

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 프로젝트 루트 디렉토리 (이 스크립트가 scripts/ 안에 있으므로)
# dirname: 파일의 디렉토리 경로 추출
# cd + pwd: 절대경로로 변환
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
info "프로젝트 루트: $PROJECT_ROOT"


# ─────────────────────────────────────────────
# 1단계: JetPack 확인
# ─────────────────────────────────────────────
info "===== 1단계: JetPack 확인 ====="

# jetson_release: JetPack 버전 출력 명령어 (jetson-stats 패키지 제공)
# command -v: 명령어 존재 여부 확인 (which와 유사하지만 더 이식성 좋음)
if command -v jetson_release &>/dev/null; then
    jetson_release
    info "JetPack 확인 완료"
else
    warn "jetson_release 명령어를 찾을 수 없습니다."
    warn "jetson-stats 미설치 또는 Jetson이 아닌 환경입니다."
    warn "Jetson이 아닌 환경에서는 일부 단계를 건너뜁니다."
fi


# ─────────────────────────────────────────────
# 2단계: 전력 모드 설정 (MAXN)
# ─────────────────────────────────────────────
info "===== 2단계: 전력 모드 설정 ====="

# nvpmodel: Jetson 전력 모드 관리 도구
#   -m 0: MAXN 모드 (최대 성능, GPU 1007MHz)
#   -m 1: 15W 모드 (저전력)
#
# jetson_clocks: 모든 클럭을 최대로 고정
#   기본적으로 Jetson은 부하에 따라 클럭을 동적 조절하는데,
#   이 명령어로 항상 최대 클럭을 유지하게 한다.
if command -v nvpmodel &>/dev/null; then
    sudo nvpmodel -m 0
    sudo jetson_clocks
    info "MAXN 모드 설정 완료 (GPU 1007MHz)"
else
    warn "nvpmodel을 찾을 수 없습니다. Jetson이 아닌 환경입니다."
fi


# ─────────────────────────────────────────────
# 3단계: Swap 파일 생성 (8GB)
# ─────────────────────────────────────────────
info "===== 3단계: Swap 설정 ====="

# Swap이란?
#   물리 메모리(RAM)가 부족할 때 디스크를 임시 메모리로 사용하는 기능.
#   8GB 통합 메모리에서 Whisper(3GB) + TTS(0.3GB)를 로드하면
#   여유가 적으므로, swap을 설정해 OOM(Out of Memory) 킬을 방지한다.
#
#   단, swap은 RAM보다 훨씬 느리므로 비상용이다.
#   정상 운영 중 swap이 많이 사용되면 모델 크기를 줄여야 한다.

SWAPFILE="/swapfile"
SWAP_SIZE="8G"

if [ -f "$SWAPFILE" ]; then
    info "Swap 파일이 이미 존재합니다: $SWAPFILE"
    swapon --show
else
    info "Swap 파일 생성 중 ($SWAP_SIZE)..."

    # fallocate: 디스크 공간을 사전 할당 (빠름)
    sudo fallocate -l "$SWAP_SIZE" "$SWAPFILE"

    # 600: 소유자만 읽기/쓰기 (보안상 다른 사용자 접근 차단)
    sudo chmod 600 "$SWAPFILE"

    # mkswap: 파일을 swap 포맷으로 초기화
    sudo mkswap "$SWAPFILE"

    # swapon: swap 활성화
    sudo swapon "$SWAPFILE"

    # /etc/fstab에 등록: 재부팅 후에도 swap이 자동 활성화되도록
    # grep으로 이미 등록되어 있는지 확인 후 추가
    if ! grep -q "$SWAPFILE" /etc/fstab; then
        echo "$SWAPFILE none swap sw 0 0" | sudo tee -a /etc/fstab
    fi

    info "Swap 설정 완료"
    swapon --show
fi


# ─────────────────────────────────────────────
# 4단계: Python 가상환경 + 패키지 설치
# ─────────────────────────────────────────────
info "===== 4단계: Python 환경 설정 ====="

# 가상환경(venv)이란?
#   프로젝트별로 독립된 Python 환경을 만드는 기능.
#   시스템 Python과 분리되어 패키지 버전 충돌을 방지한다.
#
#   venv 활성화 후 pip install하면 이 환경에만 설치된다.
#   다른 프로젝트나 시스템에 영향 없음.

VENV_DIR="$PROJECT_ROOT/.venv"

if [ -d "$VENV_DIR" ]; then
    info "가상환경이 이미 존재합니다: $VENV_DIR"
else
    info "가상환경 생성 중..."
    python3 -m venv "$VENV_DIR"
    info "가상환경 생성 완료"
fi

# 가상환경 활성화
# source: 스크립트를 현재 셸에서 실행 (새 프로세스가 아님)
# 활성화하면 python, pip 명령이 venv 안의 것을 가리킴
source "$VENV_DIR/bin/activate"
info "가상환경 활성화: $(which python3)"

# pip 업그레이드 후 패키지 설치
pip install --upgrade pip

# requirements.txt가 있으면 설치
if [ -f "$PROJECT_ROOT/server/requirements.txt" ]; then
    info "Server 패키지 설치 중..."
    pip install -r "$PROJECT_ROOT/server/requirements.txt"
    info "Server 패키지 설치 완료"
else
    warn "server/requirements.txt를 찾을 수 없습니다."
fi

# jetson-stats 설치 (sudo 필요 — 시스템 레벨 도구)
info "jetson-stats 설치 중..."
sudo pip install jetson-stats 2>/dev/null || warn "jetson-stats 설치 실패 (Jetson이 아닌 환경일 수 있음)"


# ─────────────────────────────────────────────
# 5단계: Proto stub 생성
# ─────────────────────────────────────────────
info "===== 5단계: Proto stub 생성 ====="

# grpc_tools.protoc: .proto 파일에서 Python 코드를 생성하는 도구
#   --python_out: 메시지 클래스 코드 생성 (voice_service_pb2.py)
#   --grpc_python_out: gRPC 서비스 코드 생성 (voice_service_pb2_grpc.py)
#   -I proto/: proto 파일을 찾을 디렉토리 (Include path)

PROTO_DIR="$PROJECT_ROOT/proto"
PROTO_FILE="$PROTO_DIR/voice_service.proto"

if [ -f "$PROTO_FILE" ]; then
    info "Proto stub 생성 중..."
    python3 -m grpc_tools.protoc \
        -I "$PROTO_DIR" \
        --python_out="$PROTO_DIR" \
        --grpc_python_out="$PROTO_DIR" \
        "$PROTO_FILE"
    info "Proto stub 생성 완료"
    ls -la "$PROTO_DIR"/*.py
else
    error "Proto 파일을 찾을 수 없습니다: $PROTO_FILE"
    exit 1
fi


# ─────────────────────────────────────────────
# 완료
# ─────────────────────────────────────────────
info "===== 서버 초기 설정 완료 ====="
info "다음 단계:"
info "  1. 환경변수 설정: export ANTHROPIC_API_KEY=\"sk-ant-...\""
info "  2. 가상환경 활성화: source .venv/bin/activate"
info "  3. 서버 실행: python -m server.main"

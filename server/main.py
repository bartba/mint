"""
Voice Assistant Server 진입점.

시작 순서:
    1. proto/ 를 sys.path에 추가 (pb2 stubs가 패키지 없이 import하므로)
    2. 설정 로드 (config/default.yaml + config/server.yaml + 환경변수)
    3. 로깅 설정
    4. ServerOrchestrator 생성 및 startup() — 모델 로드 (Whisper → TTS → Cloud LLM)
    5. Prometheus HTTP 서버 시작 (포트 9090)
    6. gRPC 서버 시작 (포트 50051)
    7. SIGTERM/SIGINT 핸들러 등록
    8. 종료 신호 대기
    9. Graceful shutdown — gRPC 5초 유예 → Orchestrator 정리

실행:
    # 프로젝트 루트에서
    python -m server.main

    # 또는 스크립트로
    python server/main.py
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

# ── proto 디렉토리를 sys.path에 최우선 추가 ──
# voice_service_pb2_grpc.py가 "import voice_service_pb2"를 패키지 없이 사용한다.
# grpc_server.py에도 같은 처리가 있지만, main.py에서도 추가해야
# pb2_grpc를 직접 임포트할 때 오류가 없다.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROTO_DIR = _PROJECT_ROOT / "proto"
if str(_PROTO_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTO_DIR))

import grpc
import grpc.aio
from prometheus_client import start_http_server

import voice_service_pb2_grpc as pb2_grpc

from server.grpc_server import VoiceServiceHandler
from server.orchestrator import ServerOrchestrator
from shared.config import get_config
from shared.utils import setup_logging

logger = logging.getLogger(__name__)


async def serve() -> None:
    """서버를 시작하고 종료 신호를 기다린다."""

    # ── 1. 설정 로드 ──
    # get_config()는 싱글턴이므로 이후 어디서 호출해도 같은 객체를 반환한다.
    config = get_config("server")

    # ── 2. 로깅 설정 ──
    # 설정 로드 직후에 설정해야 이후 로그가 올바른 포맷으로 출력된다.
    log_cfg = config.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_cfg.get("file"),
    )
    logger.info("=== Voice Assistant Server 시작 ===")
    logger.info(f"프로젝트 루트: {_PROJECT_ROOT}")

    # ── 3. Orchestrator 초기화 및 모델 로드 ──
    # startup()에서 메모리 확인 후 Whisper → TTS → Cloud LLM 순서로 로드한다.
    # 이 단계가 가장 오래 걸린다 (Whisper ~10-30초).
    orchestrator = ServerOrchestrator(config)
    await orchestrator.startup()

    # ── 4. Prometheus HTTP 서버 ──
    # Phase 5에서 metrics.py가 추가되면 이 서버를 통해 메트릭이 노출된다.
    # 현재는 python-prometheus-client 기본 메트릭(프로세스 통계 등)만 제공된다.
    prometheus_port = config.get("monitoring", {}).get("prometheus_port", 9090)
    start_http_server(prometheus_port)
    logger.info(f"Prometheus 메트릭 서버: http://0.0.0.0:{prometheus_port}/metrics")

    # ── 5. gRPC 서버 ──
    grpc_cfg = config.get("grpc", {})
    port = grpc_cfg.get("port", 50051)
    max_message_size = grpc_cfg.get("max_message_size", 10 * 1024 * 1024)  # 10MB

    # grpc.aio.server: asyncio 이벤트 루프 위에서 동작하는 비동기 gRPC 서버.
    # max_workers 파라미터가 없다 — asyncio가 동시성을 처리한다.
    # (동기 grpc.server는 ThreadPoolExecutor가 필요하지만 grpc.aio는 불필요)
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", max_message_size),
            ("grpc.max_send_message_length", max_message_size),
        ]
    )

    handler = VoiceServiceHandler(orchestrator)
    pb2_grpc.add_VoiceServiceServicer_to_server(handler, server)

    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    await server.start()
    logger.info(f"gRPC 서버 시작: {listen_addr}")

    # ── 6. Graceful shutdown 설정 ──
    stop_event = asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
        logger.info(f"종료 신호 수신: {sig.name}")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig)

    logger.info("서버 준비 완료. 종료 신호(SIGTERM/SIGINT)를 기다립니다.")

    # ── 7. 종료 신호 대기 ──
    await stop_event.wait()

    # ── 8. Graceful shutdown ──
    # gRPC: 진행 중인 RPC가 완료될 때까지 최대 5초 대기
    logger.info("Graceful shutdown 시작...")
    await server.stop(grace=5.0)
    await orchestrator.shutdown()
    logger.info("=== 서버 종료 완료 ===")


def main() -> None:
    """
    서버 진입점.

    asyncio.run()으로 이벤트 루프를 생성하고 serve()를 실행한다.
    SIGTERM/SIGINT 수신 시 serve()가 반환되고 루프가 종료된다.
    """
    asyncio.run(serve())


if __name__ == "__main__":
    main()

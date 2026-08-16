"""RQ worker entry point.

CP-M1 ships a skeleton: it can connect to Redis and bind queues, but no
real jobs are wired yet (those land in CP-M2..CP-M5). The `--queues` CLI
arg lets the same image run as a generic worker (default: all generation
queues except `media_render`) or a specialized one (`media_render` only,
via `Dockerfile.ffmpeg`).

Run:
    python -m app.worker                    # default: all generation queues
    python -m app.worker --queues media_render
"""

from __future__ import annotations

import argparse
import signal
import sys
from typing import List

from redis import Redis
from rq import Queue, Worker

from app.core.config import settings
from app.core.logging import logger

# Default queues consumed by the generic worker. `brand_asset_tag` is
# a lightweight vision-LLM job (Phase 1 brand asset library); piggybacks
# on the generic worker so admins don't have to provision yet another
# container.
DEFAULT_QUEUES = [
    "analyzer",
    "brand_asset_tag",
    "director",
    "image_gen",
    "video_gen",
    "audio_gen",
    "publish",
    "planner",
]
ALL_QUEUES = DEFAULT_QUEUES + ["media_render", "frame_extract", "segment_cut"]


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="content_pipeline RQ worker")
    parser.add_argument(
        "--queues",
        nargs="+",
        default=DEFAULT_QUEUES,
        help="Queues to consume (default: all generation queues except media_render).",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    invalid = [q for q in args.queues if q not in ALL_QUEUES]
    if invalid:
        raise SystemExit(f"unknown queue(s): {invalid}; valid: {ALL_QUEUES}")

    redis_conn = Redis.from_url(settings.redis_url)

    logger.info(
        "content_pipeline_worker_starting",
        queues=args.queues,
        redis_db=settings.REDIS_DB,
    )

    # Graceful shutdown: rq.Worker handles SIGINT/SIGTERM itself, but we
    # log so the process exit reason is visible.
    def _on_signal(signum, _frame):
        logger.info("content_pipeline_worker_signal", signal=signum)

    signal.signal(signal.SIGTERM, _on_signal)

    queues = [Queue(name, connection=redis_conn) for name in args.queues]
    worker = Worker(queues, connection=redis_conn)
    worker.work(with_scheduler=False, logging_level="INFO")


if __name__ == "__main__":
    main()

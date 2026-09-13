"""验证 Redis SSE 事件可被独立进程续传。

脚本启动两个独立 Python 子进程：第一个写入一轮已完成事件，第二个重新创建
RedisStreamBuffer 并读取。它证明的是传输层跨进程恢复，不替代完整 Agent 崩溃演练。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


def _process(mode: str, turn_id: str, thread_id: str) -> dict[str, object]:
    import redis
    from app.streaming import RedisStreamBuffer

    client = redis.Redis.from_url(_redis_url(), decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
    client.ping()
    buffer = RedisStreamBuffer(client, ttl_seconds=300, max_turns=32, max_events=32)
    if mode == "write":
        buffer.append(thread_id, turn_id, 0, "agent_start", {"source": "process-a"})
        buffer.append(thread_id, turn_id, 1, "message", {"content": "跨进程恢复"})
        buffer.append(thread_id, turn_id, 2, "done", {"recovered": False})
        return {"mode": mode, "turn_id": turn_id, "status": "written"}
    status, events = buffer.read_turn(thread_id, turn_id, -1)
    if status != "complete" or [event for _, event, _ in events] != ["agent_start", "message", "done"]:
        raise AssertionError(f"Redis SSE 跨进程读取失败: status={status}, events={events}")
    if buffer.read_turn("other-thread", turn_id, -1)[0] != "foreign":
        raise AssertionError("Redis SSE 未阻断其他 thread 的续传")
    return {"mode": mode, "turn_id": turn_id, "status": status, "events": [event for _, event, _ in events]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", choices=("write", "read"))
    parser.add_argument("--turn-id")
    parser.add_argument("--thread-id")
    args = parser.parse_args()

    if args.child:
        print(json.dumps(_process(args.child, args.turn_id or "", args.thread_id or ""), ensure_ascii=False))
        return 0

    repo_root = Path(__file__).resolve().parents[1]
    script = Path(__file__).resolve()
    thread_id = f"verify-sse-{uuid.uuid4().hex}"
    turn_id = f"turn-{uuid.uuid4().hex}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "backend")
    for mode in ("write", "read"):
        completed = subprocess.run(
            [sys.executable, str(script), "--child", mode, "--turn-id", turn_id, "--thread-id", thread_id],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if completed.returncode:
            raise SystemExit(completed.stderr or completed.stdout)
        print(completed.stdout.strip())
    print("Redis SSE cross-process recovery verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

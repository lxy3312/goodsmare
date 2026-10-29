"""每个推送渠道单独排队；一个渠道慢，不阻塞别的渠道的下一批消息。"""

from __future__ import annotations

import collections
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import notify


class Dispatcher:
    def __init__(self, gate, log):
        self._gate = gate
        self._log = log
        self._lock = threading.Lock()
        self._pools = {}
        self._pending = 0
        self._closed = False
        self._samples = collections.deque(maxlen=100)
        self._failures = 0

    def submit(self, channel, hits, ctx, detected_at):
        key = channel["id"]
        with self._lock:
            if self._closed:
                raise RuntimeError("推送队列已经停止")
            pool = self._pools.get(key)
            if pool is None:
                pool = self._pools[key] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"push-{key}")
            self._pending += 1
            try:
                pool.submit(self._send, channel, list(hits), ctx, detected_at)
            except Exception:
                self._pending -= 1
                raise

    def _send(self, channel, hits, ctx, detected_at):
        failed = True
        try:
            with self._gate:
                errors = notify.send_all([channel], hits, ctx)
            failed = bool(errors)
            for name, error in errors:
                self._log(f"推送失败 · {name}：{error}")
        except Exception as error:
            self._log(f"推送失败 · {channel.get('name') or channel['type']}：{error}")
        finally:
            with self._lock:
                self._pending -= 1
                self._failures += int(failed)
                if not failed:
                    self._samples.append(max(0, time.time() - detected_at))

    def snapshot(self):
        with self._lock:
            values = sorted(self._samples)
            return {"pending": self._pending, "failures": self._failures, "samples": len(values),
                    "latency_median": round(values[len(values) // 2], 2) if values else None}

    def close(self):
        # 先让所有渠道停止接收，再等各自的队列发完。
        with self._lock:
            self._closed = True
            pools = list(self._pools.values())
        for pool in pools:
            pool.shutdown(wait=True)

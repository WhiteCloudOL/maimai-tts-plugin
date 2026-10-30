"""受容量与 TTL 限制的异步结果缓存，合并重复请求并保留独立取消语义。"""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, Generic, Literal, Set, Tuple, TypeVar

import asyncio


T = TypeVar("T")
Key = Tuple[str, str]


@dataclass
class Entry:
    value: Any
    size: int
    expires: float


@dataclass
class Flight:
    task: asyncio.Task
    waiters: int = 0


@dataclass(frozen=True)
class CachedResult(Generic[T]):
    value: T
    source: Literal["hit", "miss", "shared", "bypass"]


class AsyncResultCache:
    def __init__(self, max_bytes: int, max_entries: int = 256, clock: Callable[[], float] = monotonic) -> None:
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.clock = clock
        self.entries: OrderedDict[Key, Entry] = OrderedDict()
        self.pending: Dict[Key, Flight] = {}
        self.tasks: Set[asyncio.Task] = set()
        self.bytes = 0
        self.hits = self.misses = self.shared = self.bypasses = self.evictions = 0
        self.closed = False

    def _remove(self, key: Key) -> None:
        entry = self.entries.pop(key, None)
        if entry is not None:
            self.bytes -= entry.size

    def _expire(self) -> None:
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if entry.expires <= now:
                self._remove(key)

    def invalidate(self, namespace: str) -> None:
        for key in list(self.entries):
            if key[0] == namespace:
                self._remove(key)
        # 旧请求仍可回复原来的等待者；摘除映射后，它既不会被新请求复用，也不会回填旧结果。
        for key in list(self.pending):
            if key[0] == namespace:
                self.pending.pop(key)

    def evict(self, namespace: str, key: str) -> None:
        """移除单次无效合成，保留同账号其他模型与正文的有效缓存。"""
        cache_key = (namespace, key)
        self._remove(cache_key)
        self.pending.pop(cache_key, None)

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def get(
        self,
        namespace: str,
        key: str,
        operation: Callable[[], Awaitable[T]],
        *,
        ttl: float,
        size: Callable[[T], int],
        refresh: bool = False,
    ) -> CachedResult[T]:
        if self.closed:
            raise RuntimeError("缓存已关闭")
        if ttl <= 0 or self.max_bytes <= 0 or self.max_entries <= 0:
            self.bypasses += 1
            return CachedResult(await operation(), "bypass")
        cache_key = (namespace, key)
        self._expire()
        if refresh:
            self._remove(cache_key)
            self.pending.pop(cache_key, None)
        entry = self.entries.get(cache_key)
        if entry is not None:
            self.entries.move_to_end(cache_key)
            self.hits += 1
            return CachedResult(deepcopy(entry.value), "hit")
        flight = self.pending.get(cache_key)
        source: Literal["miss", "shared"] = "shared" if flight is not None else "miss"
        if flight is None:
            self.misses += 1

            async def load() -> T:
                try:
                    value = await operation()
                    current = self.pending.get(cache_key)
                    weight = size(value)
                    if current is not None and current.task is asyncio.current_task() and weight <= self.max_bytes:
                        self._expire()
                        self._remove(cache_key)
                        while self.entries and (
                            len(self.entries) >= self.max_entries or self.bytes + weight > self.max_bytes
                        ):
                            self._remove(next(iter(self.entries)))
                            self.evictions += 1
                        self.entries[cache_key] = Entry(deepcopy(value), weight, self.clock() + ttl)
                        self.bytes += weight
                    return value
                finally:
                    current = self.pending.get(cache_key)
                    if current is not None and current.task is asyncio.current_task():
                        self.pending.pop(cache_key)

            task = asyncio.create_task(load(), name="tts-cache-load")
            flight = Flight(task)
            self.pending[cache_key] = flight
            self.tasks.add(task)
            task.add_done_callback(self._done)
        else:
            self.shared += 1
        flight.waiters += 1
        try:
            return CachedResult(deepcopy(await asyncio.shield(flight.task)), source)
        finally:
            flight.waiters -= 1
            if not flight.waiters and not flight.task.done():
                if self.pending.get(cache_key) is flight:
                    self.pending.pop(cache_key)
                flight.task.cancel()

    def stats(self) -> Dict[str, Any]:
        self._expire()
        total = self.hits + self.misses + self.shared
        return {
            "hits": self.hits,
            "misses": self.misses,
            "shared": self.shared,
            "bypasses": self.bypasses,
            "hit_rate": round(100 * (self.hits + self.shared) / total, 1) if total else 0,
            "entries": len(self.entries),
            "bytes": self.bytes,
            "evictions": self.evictions,
        }

    async def close(self) -> None:
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.entries.clear()
        self.pending.clear()
        self.bytes = 0

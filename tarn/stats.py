"""Background sampler: turns raw libvirt counters into rates and history."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from . import host

log = logging.getLogger("tarn.stats")


class Sampler:
    def __init__(self, hv, interval: float, points: int):
        self.hv = hv
        self.interval = interval
        self.points = points
        self.vms: dict[str, dict] = {}        # uuid -> {"current": {...}, "history": deque}
        self.host_current: dict = {}
        self.host_history: deque = deque(maxlen=points)
        self._prev_raw: dict[str, dict] = {}
        self._prev_t: float | None = None
        self._prev_cpu: tuple[int, int] | None = None
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        while True:
            try:
                await asyncio.to_thread(self._tick)
            except Exception as exc:  # keep sampling whatever happens
                log.warning("stats sample failed: %s", exc)
            await asyncio.sleep(self.interval)

    def _tick(self):
        now, raw = self.hv.sample()
        wall = time.time()
        dt = (now - self._prev_t) if self._prev_t else None

        seen = set()
        for uuid, cur in raw.items():
            seen.add(uuid)
            prev = self._prev_raw.get(uuid)
            point = {"t": wall, "cpu": None, "mem_used": cur["mem_used"], "mem_max": cur["mem_max"],
                     "rd": None, "wr": None, "rx": None, "tx": None}
            if prev and dt and dt > 0:
                cpu_ns = max(0, cur["cpu_time"] - prev["cpu_time"])
                point["cpu"] = min(100.0, cpu_ns / (dt * 1e9 * max(1, cur["vcpus"])) * 100)
                for k in ("rd", "wr", "rx", "tx"):
                    point[k] = max(0.0, (cur[k] - prev[k]) / dt)
            entry = self.vms.setdefault(uuid, {"history": deque(maxlen=self.points)})
            entry["current"] = point
            if point["cpu"] is not None:
                entry["history"].append(point)

        for uuid in list(self.vms):
            if uuid not in seen:  # stopped VMs keep no stale numbers
                del self.vms[uuid]
        self._prev_raw = raw
        self._prev_t = now

        busy, total = host.cpu_times()
        cpu_pct = None
        if self._prev_cpu:
            db, dtot = busy - self._prev_cpu[0], total - self._prev_cpu[1]
            cpu_pct = (db / dtot * 100) if dtot > 0 else 0.0
        self._prev_cpu = (busy, total)
        mem = host.meminfo()
        total_mem = mem.get("MemTotal", 0)
        used_mem = total_mem - mem.get("MemAvailable", 0)
        try:
            load = [float(x) for x in host._read("/proc/loadavg").split()[:3]]
        except ValueError:
            load = []
        hp = {"t": wall, "cpu": cpu_pct, "mem_used": used_mem, "mem_total": total_mem, "load": load}
        self.host_current = hp
        if cpu_pct is not None:
            self.host_history.append(hp)

    def vm_current(self, uuid: str) -> dict | None:
        entry = self.vms.get(uuid)
        return entry.get("current") if entry else None

    def vm_history(self, uuid: str) -> list[dict]:
        entry = self.vms.get(uuid)
        return list(entry["history"]) if entry else []

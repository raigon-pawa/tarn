"""WebSocket <-> VNC bridge for the in-browser console (noVNC).

This replaces websockify. The browser speaks RFB over a WebSocket to Tarn,
and Tarn relays it to QEMU's VNC server, which only listens on 127.0.0.1
(or is reached through a socket libvirt hands us directly).
"""

from __future__ import annotations

import asyncio
import logging
import socket

from aiohttp import WSMsgType, web

log = logging.getLogger("tarn.console")


async def _open_vnc(hv, uuid: str):
    try:
        fd = await asyncio.to_thread(hv.open_graphics_fd, uuid)
        sock = socket.socket(fileno=fd)
        sock.setblocking(False)
        return await asyncio.open_connection(sock=sock)
    except Exception as exc:  # older libvirt, remote URI, or no FD passing
        log.debug("openGraphicsFD unavailable (%s); using the TCP port", exc)
    host, port = await asyncio.to_thread(hv.vnc_address, uuid)
    return await asyncio.open_connection(host, port)


async def vnc_proxy(request: web.Request) -> web.WebSocketResponse:
    hv = request.app["hv"]
    uuid = request.match_info["uuid"]
    ws = web.WebSocketResponse(protocols=("binary",), max_msg_size=0, heartbeat=30)
    await ws.prepare(request)

    try:
        reader, writer = await _open_vnc(hv, uuid)
    except Exception as exc:
        log.info("console for %s unavailable: %s", uuid, exc)
        await ws.close(code=4001, message=str(exc)[:120].encode())
        return ws

    async def browser_to_vnc():
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                writer.write(msg.data)
                await writer.drain()
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break

    async def vnc_to_browser():
        while True:
            data = await reader.read(256 * 1024)
            if not data:
                break
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(browser_to_vnc()), asyncio.create_task(vnc_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if not ws.closed:
            await ws.close()
    return ws

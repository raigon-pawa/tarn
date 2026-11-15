"""HTTP API and static file serving."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import libvirt
from aiohttp import web

from . import __version__, auth, host
from .console import vnc_proxy
from .domxml import SpecError
from .stats import Sampler
from .virt import Conflict, Hypervisor, NotFound

log = logging.getLogger("tarn.web")
STATIC = Path(__file__).parent / "static"
routes = web.RouteTableDef()


def ok(data=None, status=200):
    return web.json_response(data if data is not None else {"ok": True}, status=status)


async def run(fn, *args):
    return await asyncio.to_thread(fn, *args)


@web.middleware
async def error_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except NotFound as exc:
        return ok({"error": str(exc)}, 404)
    except Conflict as exc:
        return ok({"error": str(exc)}, 409)
    except SpecError as exc:
        return ok({"error": str(exc)}, 400)
    except libvirt.libvirtError as exc:
        # libvirt messages are precise and useful; pass them through.
        return ok({"error": exc.get_error_message() or str(exc)}, 400)
    except json.JSONDecodeError:
        return ok({"error": "Request body is not valid JSON"}, 400)
    except Exception:
        log.exception("unhandled error on %s %s", request.method, request.path)
        return ok({"error": "Internal error, see the Tarn log"}, 500)


@web.middleware
async def headers_middleware(request, handler):
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# --------------------------------------------------------------------------
# Pages and auth
# --------------------------------------------------------------------------

@routes.get("/")
async def index(_request):
    return web.FileResponse(STATIC / "index.html")


@routes.get("/console")
async def console_page(_request):
    return web.FileResponse(STATIC / "console.html")


@routes.post("/api/login")
async def login(request):
    sessions: auth.Sessions = request.app["sessions"]
    ip = request.remote or "?"
    if sessions.locked_out(ip):
        return ok({"error": "Too many attempts. Wait a few minutes."}, 429)
    body = await request.json()
    stored = auth.read_password_hash(request.app["config"]["auth"]["password_file"])
    if not stored:
        return ok({"error": "No password set. Run 'tarn passwd' on the host."}, 503)
    if not auth.verify_password(str(body.get("password", "")), stored):
        sessions.record_failure(ip)
        await asyncio.sleep(1)
        return ok({"error": "Wrong password"}, 401)
    resp = ok()
    resp.set_cookie(auth.COOKIE, sessions.create(), httponly=True, samesite="Strict",
                    secure=request.secure, max_age=int(sessions.ttl), path="/")
    return resp


@routes.post("/api/logout")
async def logout(request):
    request.app["sessions"].drop(request.cookies.get(auth.COOKIE))
    resp = ok()
    resp.del_cookie(auth.COOKIE, path="/")
    return resp


@routes.get("/api/me")
async def me(_request):
    return ok({"logged_in": True, "version": __version__})


# --------------------------------------------------------------------------
# Host
# --------------------------------------------------------------------------

@routes.get("/api/host")
async def host_get(request):
    return ok(await run(request.app["hv"].host_report))


@routes.get("/api/host/iommu")
async def host_iommu(_request):
    return ok({"groups": await run(host.iommu_groups)})


@routes.get("/api/host/stats")
async def host_stats(request):
    s: Sampler = request.app["sampler"]
    return ok({"current": s.host_current, "history": list(s.host_history),
               "power": await run(host.power_status)})


# --------------------------------------------------------------------------
# VMs
# --------------------------------------------------------------------------

@routes.get("/api/vms")
async def vms_list(request):
    hv: Hypervisor = request.app["hv"]
    s: Sampler = request.app["sampler"]
    vms = await run(hv.list_vms)
    for vm in vms:
        vm["stats"] = s.vm_current(vm["uuid"])
        # Compact CPU history for the per-row ridgeline.
        vm["cpu_history"] = [round(p["cpu"], 1) for p in s.vm_history(vm["uuid"])]
    return ok({"vms": vms, "host": s.host_current})


@routes.get("/api/meta")
async def create_meta(request):
    """Everything the create form needs in one call."""
    return ok(await run(request.app["hv"].create_meta))


@routes.post("/api/vms")
async def vms_create(request):
    spec = await request.json()
    result = await run(request.app["hv"].create_vm, spec)
    return ok(result, 201)


@routes.get("/api/vms/{uuid}")
async def vm_get(request):
    hv: Hypervisor = request.app["hv"]
    uuid = request.match_info["uuid"]
    detail = await run(hv.vm_detail, uuid)
    detail["stats"] = request.app["sampler"].vm_current(uuid)
    return ok(detail)


@routes.get("/api/vms/{uuid}/stats")
async def vm_stats(request):
    s: Sampler = request.app["sampler"]
    uuid = request.match_info["uuid"]
    return ok({"current": s.vm_current(uuid), "history": s.vm_history(uuid)})


@routes.post("/api/vms/{uuid}/action")
async def vm_action(request):
    body = await request.json()
    await run(request.app["hv"].action, request.match_info["uuid"], str(body.get("action")))
    return ok()


@routes.patch("/api/vms/{uuid}")
async def vm_patch(request):
    hv: Hypervisor = request.app["hv"]
    uuid = request.match_info["uuid"]
    body = await request.json()
    result = {}
    if "name" in body:
        await run(hv.rename, uuid, str(body["name"]).strip())
    if "autostart" in body:
        await run(hv.set_autostart, uuid, bool(body["autostart"]))
    if "vcpus" in body or "memory_mib" in body:
        vcpus = int(body["vcpus"]) if body.get("vcpus") is not None else None
        mem = int(body["memory_mib"]) if body.get("memory_mib") is not None else None
        result.update(await run(hv.set_resources, uuid, vcpus, mem))
    if body.get("boot_from_disk"):
        await run(hv.set_boot_disk_first, uuid)
    if "cpu_pinning" in body:
        pin = await run(hv.set_cpu_pinning, uuid, bool(body["cpu_pinning"]))
        result["applies_after_restart"] = result.get("applies_after_restart") or pin["applies_after_restart"]
        if pin["note"]:
            result["note"] = pin["note"]
    if "looking_glass" in body:
        lg = await run(hv.set_looking_glass, uuid, bool(body["looking_glass"]))
        if lg["note"]:
            result["note"] = lg["note"]
    return ok(result or None)


@routes.delete("/api/vms/{uuid}")
async def vm_delete(request):
    delete_disks = request.query.get("disks") == "1"
    removed = await run(request.app["hv"].delete_vm, request.match_info["uuid"], delete_disks)
    return ok({"deleted_disks": removed})


@routes.post("/api/vms/{uuid}/media")
async def vm_media(request):
    body = await request.json()
    await run(request.app["hv"].change_media, request.match_info["uuid"],
              str(body["target"]), body.get("path") or None)
    return ok()


@routes.post("/api/vms/{uuid}/pci")
async def vm_pci_attach(request):
    body = await request.json()
    await run(request.app["hv"].attach_pci, request.match_info["uuid"], _pci_addr(body.get("address")))
    return ok()


@routes.delete("/api/vms/{uuid}/pci/{address}")
async def vm_pci_detach(request):
    await run(request.app["hv"].detach_pci, request.match_info["uuid"], _pci_addr(request.match_info["address"]))
    return ok()


@routes.get("/api/pci/cards")
async def pci_cards(request):
    """GPUs on the host, each with all its functions, and whether they can be passed through."""
    return ok({"cards": await run(request.app["hv"].pci_candidates)})


@routes.post("/api/vms/{uuid}/cards")
async def vm_card_attach(request):
    body = await request.json()
    return ok(await run(request.app["hv"].attach_card, request.match_info["uuid"], _pci_slot(body.get("slot"))))


@routes.delete("/api/vms/{uuid}/cards/{slot}")
async def vm_card_detach(request):
    await run(request.app["hv"].detach_card, request.match_info["uuid"], _pci_slot(request.match_info["slot"]))
    return ok()


def _pci_slot(value) -> str:
    import re
    value = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}", value):
        raise SpecError("Card slot must look like 0000:01:00")
    return value


def _pci_addr(value) -> str:
    import re
    value = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", value):
        raise SpecError("PCI address must look like 0000:01:00.0")
    return value


@routes.get("/api/vms/{uuid}/xml")
async def vm_xml_get(request):
    xml = await run(request.app["hv"].vm_xml, request.match_info["uuid"])
    return ok({"xml": xml})


@routes.put("/api/vms/{uuid}/xml")
async def vm_xml_put(request):
    body = await request.json()
    await run(request.app["hv"].define_xml, request.match_info["uuid"], str(body.get("xml", "")))
    return ok()


@routes.get("/api/vms/{uuid}/snapshots")
async def snaps_list(request):
    return ok({"snapshots": await run(request.app["hv"].list_snapshots, request.match_info["uuid"])})


@routes.post("/api/vms/{uuid}/snapshots")
async def snaps_create(request):
    body = await request.json()
    await run(request.app["hv"].create_snapshot, request.match_info["uuid"],
              str(body.get("name", "")).strip(), str(body.get("description", "")))
    return ok(status=201)


@routes.post("/api/vms/{uuid}/snapshots/{name}/revert")
async def snaps_revert(request):
    await run(request.app["hv"].revert_snapshot, request.match_info["uuid"], request.match_info["name"])
    return ok()


@routes.delete("/api/vms/{uuid}/snapshots/{name}")
async def snaps_delete(request):
    await run(request.app["hv"].delete_snapshot, request.match_info["uuid"], request.match_info["name"])
    return ok()


routes.get("/api/vms/{uuid}/vnc")(vnc_proxy)


# --------------------------------------------------------------------------
# Storage and networks
# --------------------------------------------------------------------------

@routes.get("/api/storage")
async def storage_get(request):
    cfg = request.app["config"]["storage"]
    return ok({"pools": await run(request.app["hv"].storage),
               "roles": {"images": cfg["images_pool"], "iso": cfg["iso_pool"]}})


@routes.post("/api/storage/{pool}/volumes")
async def volume_create(request):
    body = await request.json()
    path = await run(request.app["hv"].create_volume, request.match_info["pool"],
                     str(body.get("name", "")).strip(), float(body.get("size_gib", 0)),
                     str(body.get("format", "qcow2")))
    return ok({"path": path}, 201)


@routes.delete("/api/storage/{pool}/volumes/{name}")
async def volume_delete(request):
    await run(request.app["hv"].delete_volume, request.match_info["pool"], request.match_info["name"])
    return ok()


@routes.put("/api/storage/{pool}/upload")
async def volume_upload(request):
    """Stream a file (usually an ISO) straight into a libvirt volume.

    The browser sends the raw file as the request body; we push it through a
    libvirt stream, so Tarn never needs write access to the pool directory.
    """
    hv: Hypervisor = request.app["hv"]
    name = request.query.get("name", "").strip()
    try:
        size = int(request.headers.get("Content-Length") or request.query.get("size") or 0)
    except ValueError:
        raise SpecError("Bad upload size")
    stream, vol = await run(hv.begin_upload, request.match_info["pool"], name, size)
    sent = 0
    try:
        async for chunk in request.content.iter_chunked(1024 * 1024):
            await run(stream.send, chunk)
            sent += len(chunk)
        if sent != size:
            raise SpecError(f"Upload incomplete: got {sent} of {size} bytes")
        await run(stream.finish)
    except BaseException:
        try:
            await run(stream.abort)
        except Exception:
            pass
        try:
            await run(vol.delete, 0)
        except Exception:
            pass
        raise
    return ok({"path": vol.path(), "bytes": sent}, 201)


@routes.get("/api/networks")
async def networks_get(request):
    return ok(await run(request.app["hv"].networks))


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def make_app(config) -> web.Application:
    app = web.Application(
        middlewares=[headers_middleware, error_middleware, auth.auth_middleware],
        client_max_size=1024**2,  # JSON bodies; uploads stream and bypass this
    )
    app["config"] = config
    app["sessions"] = auth.Sessions(config["auth"]["session_hours"])
    hv = Hypervisor(config["libvirt"]["uri"], config["storage"], config["network"])
    app["hv"] = hv
    app["sampler"] = Sampler(hv, config["stats"]["interval_seconds"], config["stats"]["history_points"])

    async def on_startup(app):
        try:
            notes = await run(hv.ensure_infrastructure)
            for note in notes:
                log.info(note)
            caps = await run(hv.caps)
            log.info("connected to %s (%s)", hv.uri, caps.virt_type.upper())
        except libvirt.libvirtError as exc:
            log.error("cannot reach libvirt at %s: %s", hv.uri, exc)
        app["sampler"].start()

    async def on_cleanup(app):
        await app["sampler"].stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_routes(routes)
    app.router.add_static("/static/", STATIC, append_version=False)
    return app

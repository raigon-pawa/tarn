"""End-to-end API test against a running Tarn.

Needs a reachable libvirtd (qemu:///system). Creates three throwaway VMs,
uploads a fake ISO, exercises lifecycle, console, media, snapshots and XML,
then deletes everything (pass --keep to leave the VMs for poking around).

    TARN_URL=http://127.0.0.1:8006 TARN_PASSWORD=... python3 tests/api_test.py

Every line starts with [ok ] or [BAD].
"""
import asyncio
import json
import os
import sys

import aiohttp

BASE = os.environ.get("TARN_URL", "http://127.0.0.1:8006")
PASSWORD = os.environ.get("TARN_PASSWORD", "testpass123")
H = {"X-Tarn": "1"}


async def main():
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as s:
        async def call(method, path, body=None, expect=None, **kw):
            async with s.request(method, BASE + path, json=body, headers=H, **kw) as r:
                txt = await r.text()
                data = json.loads(txt) if txt and r.content_type == "application/json" else txt
                tag = "ok " if (expect is None and r.status < 400) or r.status == expect else "BAD"
                print(f"[{tag}] {method} {path} -> {r.status} {str(data)[:220]}")
                return r.status, data

        await call("GET", "/api/me", expect=401)
        await call("POST", "/api/login", {"password": "wrong"}, expect=401)
        await call("POST", "/api/login", {"password": PASSWORD})
        # CSRF: mutation without header must be refused
        async with s.post(BASE + "/api/vms", json={}) as r:
            print(f"[{'ok ' if r.status == 403 else 'BAD'}] POST without X-Tarn -> {r.status}")

        st, host = await call("GET", "/api/host")
        for c in host["readiness"]:
            print("    ", c["level"], c["label"], "-", c["detail"])
        st, meta = await call("GET", "/api/meta")

        # Upload a fake ISO through the libvirt stream path
        payload = os.urandom(3 * 1024 * 1024 + 123)
        async with s.put(BASE + "/api/storage/iso/upload?name=test-install.iso", data=payload,
                         headers={**H, "Content-Type": "application/octet-stream"}) as r:
            body = await r.json()
            print(f"[{'ok ' if r.status == 201 else 'BAD'}] upload -> {r.status} {body}")
        iso_path = body["path"]
        if os.access(iso_path, os.R_OK):  # only on the host itself, as a user who may read the pool
            with open(iso_path, "rb") as fh:
                print(f"[{'ok ' if fh.read() == payload else 'BAD'}] upload bytes intact")
        else:  # the pool directory is root-only: compare the size libvirt reports instead
            st, pools = await call("GET", "/api/storage")
            vol = next((v for p in pools["pools"] for v in p["volumes"] if v["path"] == iso_path), None)
            print(f"[{'ok ' if vol and vol['capacity'] == len(payload) else 'BAD'}] upload size intact")

        # Linux VM, UEFI, new disk, ISO attached
        st, res = await call("POST", "/api/vms", {
            "name": "alpine-test", "os_type": "linux", "vcpus": 2, "memory_mib": 512,
            "disk": {"mode": "new", "size_gib": 2}, "iso": iso_path, "start": True}, expect=201)
        lin = res["uuid"]
        # Windows VM, secure boot + TPM, BIOS-less SATA, no start
        st, res = await call("POST", "/api/vms", {
            "name": "win11-test", "os_type": "windows", "memory_mib": 1024,
            "disk": {"mode": "new", "size_gib": 4}, "start": False}, expect=201)
        win = res["uuid"]
        # Legacy BIOS VM for snapshot test (internal snapshots work with BIOS + qcow2)
        st, res = await call("POST", "/api/vms", {
            "name": "bios-test", "os_type": "other", "memory_mib": 256,
            "disk": {"mode": "new", "size_gib": 1}, "start": False}, expect=201)
        bios = res["uuid"]
        await call("POST", "/api/vms", {"name": "alpine-test", "os_type": "linux"}, expect=409)
        await call("POST", "/api/vms", {"name": "bad name!", "os_type": "linux"}, expect=400)

        await asyncio.sleep(6)
        st, vms = await call("GET", "/api/vms")
        for vm in vms["vms"]:
            print("    ", vm["name"], vm["state"], vm["os_type"], vm["stats"] and round(vm["stats"]["cpu"] or 0, 1))
        st, d = await call("GET", f"/api/vms/{lin}")
        print("     disks:", [(x["target"], x["bus"], x["device"], x["capacity"]) for x in d["disks"]])
        print("     nics:", d["nics"], "fw:", d["firmware"], d["secure_boot"])
        st, d = await call("GET", f"/api/vms/{win}")
        print("     win:", d["firmware"], "secure", d["secure_boot"], "tpm", d["tpm"],
              [(x["target"], x["bus"]) for x in d["disks"]], d["nics"][0]["model"], d["video"])
        st, sts = await call("GET", f"/api/vms/{lin}/stats")
        print("     history points:", len(sts["history"]))

        # Console: WebSocket handshake should yield the RFB banner
        async with s.ws_connect(BASE.replace("http", "ws") + f"/api/vms/{lin}/vnc",
                                protocols=("binary",), headers={"Origin": BASE}) as ws:
            msg = await ws.receive(timeout=10)
            print(f"[{'ok ' if msg.type == aiohttp.WSMsgType.BINARY and msg.data.startswith(b'RFB') else 'BAD'}] "
                  f"VNC banner: {msg.data[:12] if msg.type == aiohttp.WSMsgType.BINARY else msg}")
        # Cross-origin WebSocket must be refused
        try:
            async with s.ws_connect(BASE.replace("http", "ws") + f"/api/vms/{lin}/vnc",
                                    headers={"Origin": "http://evil.example"}) as ws:
                print("[BAD] cross-origin ws accepted")
        except aiohttp.WSServerHandshakeError as e:
            print(f"[ok ] cross-origin ws refused ({e.status})")

        # Media: eject and re-insert
        await call("POST", f"/api/vms/{lin}/media", {"target": "sda", "path": None})
        await call("POST", f"/api/vms/{lin}/media", {"target": "sda", "path": iso_path})
        await call("PATCH", f"/api/vms/{lin}", {"vcpus": 3, "memory_mib": 768, "autostart": True})
        await call("PATCH", f"/api/vms/{lin}", {"boot_from_disk": True})

        # Snapshots on the BIOS VM
        await call("POST", f"/api/vms/{bios}/snapshots", {"name": "clean", "description": "fresh"}, expect=201)
        await call("GET", f"/api/vms/{bios}/snapshots")
        await call("POST", f"/api/vms/{bios}/snapshots/clean/revert")
        await call("DELETE", f"/api/vms/{bios}/snapshots/clean")
        # Snapshot on the UEFI VM: libvirt's own error should come back cleanly
        await call("POST", f"/api/vms/{win}/snapshots", {"name": "s1"})

        # Lifecycle
        for a in ("pause", "resume", "reboot", "poweroff", "start"):
            await call("POST", f"/api/vms/{lin}/action", {"action": a})
            await asyncio.sleep(1)
        st, xml = await call("GET", f"/api/vms/{lin}/xml")
        await call("PUT", f"/api/vms/{lin}/xml", {"xml": xml["xml"]})
        await call("GET", "/api/storage")
        await call("GET", "/api/networks")
        await call("GET", "/api/host/iommu")
        st, cards = await call("GET", "/api/pci/cards")
        print("     cards:", [(c["slot"], c["ready"], c["assigned_to"]) for c in cards["cards"]])
        # Looking Glass shared memory on a stopped VM (only where kvmfr is set up), then off again
        if os.path.exists("/dev/kvmfr0"):
            await call("PATCH", f"/api/vms/{bios}", {"looking_glass": True})
            st, d = await call("GET", f"/api/vms/{bios}")
            print(f"[{'ok ' if d['looking_glass'] else 'BAD'}] looking glass on")
            await call("PATCH", f"/api/vms/{lin}", {"looking_glass": True}, expect=409)  # running
            await call("PATCH", f"/api/vms/{bios}", {"looking_glass": False})

        if "--keep" not in sys.argv:
            for u in (lin, win, bios):
                await call("DELETE", f"/api/vms/{u}?disks=1")
            await call("DELETE", "/api/storage/iso/volumes/test-install.iso")
        await call("GET", "/api/storage")


asyncio.run(main())

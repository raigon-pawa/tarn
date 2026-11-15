"""Drive the Tarn UI in headless Chromium: real clicks, real uploads, real VMs.

    pip install playwright && playwright install chromium
    sh tests/make-demo-iso.sh /tmp/alpine-demo.iso
    TARN_PASSWORD=... DEMO_ISO=/tmp/alpine-demo.iso python3 tests/ui_test.py
    TARN_PASSWORD=... python3 tests/ui_test.py rest

With no argument it starts from an EMPTY host (no VMs, no ISOs): uploads two
ISOs and creates alpine-dev, win11 and debian-nas through the forms, then
walks every page. "rest" skips the creation step, for re-running once those
three VMs exist. Screenshots go to tests/shots/. It also checks the console
accepts keystrokes and exercises the revert and delete dialogs.
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright, expect

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.environ.get("SHOTS_DIR", os.path.join(HERE, "shots"))
os.makedirs(SHOTS, exist_ok=True)
BASE = os.environ.get("TARN_URL", "http://127.0.0.1:8006")
PASSWORD = os.environ.get("TARN_PASSWORD", "testpass123")
DEMO_ISO = os.environ.get("DEMO_ISO", "/tmp/alpine-demo.iso")
errors = []


def watch(page, label):
    page.on("console", lambda m: m.type == "error" and errors.append(f"[{label}] console: {m.text}"))
    page.on("pageerror", lambda e: errors.append(f"[{label}] pageerror: {e}"))


async def shot(page, name, full=True):
    await page.screenshot(path=os.path.join(SHOTS, f"{name}.png"), full_page=full)
    print("shot", name)


async def canvas_lit(page_or_frame):
    return await page_or_frame.evaluate("""() => {
        const c = document.querySelector('#screen canvas');
        if (!c || !c.width) return -1;
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        let lit = 0;
        for (let i = 0; i < d.length; i += 16) if (d[i] + d[i+1] + d[i+2] > 60) lit++;
        return lit;
    }""")


async def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "all"
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1360, "height": 900}, color_scheme="light")
        page = await ctx.new_page()
        watch(page, "main")

        await page.goto(BASE + "/")
        await expect(page.locator("text=Sign in to manage")).to_be_visible()
        await shot(page, "01-login", full=False)
        await page.fill("#pw", "wrong-password")
        await page.click("button:has-text('Sign in')")
        await expect(page.locator(".error-text")).to_have_text("Wrong password", timeout=5000)
        await page.fill("#pw", PASSWORD)
        await page.click("button:has-text('Sign in')")
        await expect(page.locator("h2:has-text('Virtual machines')")).to_be_visible()

        if phase in ("all", "setup"):
            await expect(page.locator("text=No virtual machines yet")).to_be_visible()
            await shot(page, "02-empty", full=False)

            # Upload ISOs through the Storage page
            await page.click("nav >> text=Storage")
            await expect(page.locator("h2:has-text('Storage')")).to_be_visible()
            await page.set_input_files("#upl", DEMO_ISO)
            await expect(page.locator("td.mono:has-text('alpine-demo.iso')")).to_be_visible(timeout=30000)
            fake = os.path.join(SHOTS, "virtio-win-0.1.266.iso")
            with open(fake, "wb") as fh:
                fh.write(os.urandom(2 * 1024 * 1024))
            await page.set_input_files("#upl", fake)
            await expect(page.locator("td.mono:has-text('virtio-win-0.1.266.iso')")).to_be_visible(timeout=30000)

            # Linux VM from the ISO, started
            await page.click("text=Create VM")
            await page.fill("#c-name", "alpine-dev")
            await page.select_option("#c-iso", label="alpine-demo.iso")
            await page.fill("#c-size", "8")
            await page.fill("#c-mem", "0.5")
            await shot(page, "03-create-linux")
            await page.click("button:has-text('Create VM')")
            await page.wait_for_url("**/#/vm/*/console", timeout=20000)
            frame = page.frame_locator("iframe.console-frame")
            await expect(frame.locator("#status")).to_contain_text("Connected", timeout=20000)
            await page.wait_for_timeout(9000)  # let GRUB draw under TCG
            await shot(page, "04-console", full=False)
            f = page.frames[-1]
            print("console lit pixels:", await canvas_lit(f))

            # Windows VM, defaults (TPM + Secure Boot + driver ISO auto-picked)
            await page.goto(BASE + "/#/new")
            await page.click("label[for=os-windows]")
            await page.fill("#c-name", "win11")
            await page.select_option("#c-iso", label="alpine-demo.iso")
            await page.fill("#c-mem", "1.5")
            await page.fill("#c-vcpus", "2")
            drv = await page.eval_on_selector("#c-driver", "e => e.selectedOptions[0].textContent")
            sb = await page.is_checked("#c-sb")
            tpm = await page.is_checked("#c-tpm")
            print("windows defaults: driver iso =", drv, "secure boot =", sb, "tpm =", tpm)
            await shot(page, "05-create-windows")
            await page.click("button:has-text('Create VM')")
            await page.wait_for_url("**/#/vm/*/console", timeout=20000)

            # A stopped VM
            await page.goto(BASE + "/#/new")
            await page.fill("#c-name", "debian-nas")
            await page.fill("#c-size", "16")
            await page.uncheck("#c-start")
            await page.click("button:has-text('Create VM')")
            await page.wait_for_url("**/#/vm/*/overview", timeout=20000)

        # Overview with live ridges
        await page.goto(BASE + "/#/")
        await expect(page.locator(".vmrow")).to_have_count(3, timeout=10000)
        await page.wait_for_timeout(16000)
        await shot(page, "06-overview", full=False)

        # VM page tabs
        await page.click(".vmrow a:has-text('alpine-dev')")
        await expect(page.locator(".vmhead h1")).to_have_text("alpine-dev")
        await page.wait_for_timeout(5000)
        await shot(page, "07-vm-overview")
        await page.click(".tabs >> text=Hardware")
        await expect(page.locator("h3:has-text('Disks and drives')")).to_be_visible()
        await page.wait_for_timeout(1500)
        await shot(page, "08-vm-hardware")

        # Snapshot on the stopped VM
        await page.goto(BASE + "/#/")
        await page.click(".vmrow a:has-text('debian-nas')")
        await page.click(".tabs >> text=Snapshots")
        await page.fill("#snap-name", "fresh-install")
        await page.fill("#snap-desc", "Before setting up shares")
        await page.click("button:has-text('Take snapshot')")
        await expect(page.locator("td b:has-text('fresh-install')")).to_be_visible(timeout=15000)
        await shot(page, "09-snapshots")
        await page.click(".tabs >> text=XML")
        await page.wait_for_timeout(1000)
        xml_len = await page.eval_on_selector("textarea", "e => e.value.length")
        print("xml chars:", xml_len)
        await shot(page, "10-xml", full=False)

        await page.click("nav >> text=Host")
        await expect(page.locator("h3:has-text('Ready for VMs?')")).to_be_visible()
        await page.wait_for_timeout(1000)
        await shot(page, "11-host")
        await page.click("nav >> text=Storage")
        await page.wait_for_timeout(1000)
        await shot(page, "12-storage")

        # Dark mode + phone width
        dark = await browser.new_context(viewport={"width": 1360, "height": 900}, color_scheme="dark",
                                         storage_state=await ctx.storage_state())
        dpage = await dark.new_page()
        watch(dpage, "dark")
        await dpage.goto(BASE + "/#/")
        await dpage.wait_for_timeout(6000)
        await shot(dpage, "13-overview-dark", full=False)
        phone = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                          storage_state=await ctx.storage_state())
        ppage = await phone.new_page()
        watch(ppage, "phone")
        await ppage.goto(BASE + "/#/")
        await ppage.wait_for_timeout(6000)
        await shot(ppage, "14-overview-phone")
        await ppage.goto(BASE + "/#/new")
        await ppage.wait_for_timeout(1500)
        await shot(ppage, "15-create-phone")
        sw = await ppage.evaluate("document.documentElement.scrollWidth")
        print("phone scrollWidth:", sw)

        # Standalone console page: keyboard reaches the VM (GRUB menu moves)
        cpage = await ctx.new_page()
        watch(cpage, "console")
        uuid = await page.evaluate("fetch('/api/vms').then(r=>r.json()).then(d=>d.vms.find(v=>v.name==='alpine-dev').uuid)")
        await cpage.goto(f"{BASE}/console?uuid={uuid}")
        await expect(cpage.locator("#status")).to_contain_text("Connected", timeout=20000)
        await cpage.wait_for_timeout(2000)
        await cpage.click("#screen canvas")
        for _ in range(2):
            await cpage.keyboard.press("ArrowDown")
            await cpage.wait_for_timeout(600)
        await cpage.wait_for_timeout(1500)
        await shot(cpage, "16-console-standalone", full=False)

        # Dialog flows: revert a snapshot, then delete a scratch VM with its disk
        await page.goto(BASE + "/#/")
        await page.click(".vmrow a:has-text('debian-nas')")
        await page.click(".tabs >> text=Snapshots")
        await page.click("tr:has-text('fresh-install') >> button:has-text('Revert')")
        await page.click("dialog button[value=ok]")
        await expect(page.locator(".toast:has-text('Reverted to fresh-install')")).to_be_visible(timeout=10000)
        print("snapshot revert via dialog: ok")

        await page.goto(BASE + "/#/new")
        await page.fill("#c-name", "scratch")
        await page.fill("#c-size", "1")
        await page.uncheck("#c-start")
        await page.click("button:has-text('Create VM')")
        await page.wait_for_url("**/#/vm/*/overview", timeout=20000)
        await page.click(".actionbar button:has-text('Delete')")
        await expect(page.locator("dialog")).to_contain_text("scratch-disk0.qcow2")
        await page.click("dialog button[value=ok]")
        await page.wait_for_url(BASE + "/#/", timeout=10000)
        await expect(page.locator(".vmrow")).to_have_count(3, timeout=10000)
        gone = not os.path.exists("/var/lib/libvirt/images/scratch-disk0.qcow2")
        print("delete VM with disk via dialog:", "ok" if gone else "DISK LEFT BEHIND")

        await browser.close()
    print("\nerrors:" if errors else "\nno browser errors")
    for e in errors:
        print("  ", e)


asyncio.run(main())

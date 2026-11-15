"""Command line entry point: `python3 -m tarn serve|passwd|check|desktop|looking-glass`."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import ssl
import sys

from . import __version__, auth, config as config_mod


def cmd_serve(args) -> int:
    from aiohttp import web

    from .web import make_app

    cfg = config_mod.load(args.config)
    server = cfg["server"]
    host = args.listen or server["listen"]
    port = args.port or server["port"]

    ssl_ctx = None
    cert, key = server["tls_cert"], server["tls_key"]
    if os.path.exists(cert) and os.path.exists(key):
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_ctx.load_cert_chain(cert, key)
    elif not (server["allow_plain_http"] or args.insecure_http):
        logging.error("No TLS certificate at %s. Run install.sh, or start with --insecure-http "
                      "for testing on localhost.", cert)
        return 2

    if not auth.read_password_hash(cfg["auth"]["password_file"]):
        logging.warning("No admin password yet. Run: tarn passwd")

    scheme = "https" if ssl_ctx else "http"
    logging.info("Tarn %s on %s://%s:%s", __version__, scheme, host, port)
    web.run_app(make_app(cfg), host=host, port=port, ssl_context=ssl_ctx,
                access_log=None, print=None)
    return 0


def cmd_passwd(args) -> int:
    cfg = config_mod.load(args.config)
    path = cfg["auth"]["password_file"]
    pw = getpass.getpass("New Tarn admin password: ")
    if len(pw) < 8:
        print("Use at least 8 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Again: ") != pw:
        print("Passwords don't match.", file=sys.stderr)
        return 1
    auth.write_password_hash(path, pw)
    print(f"Password saved to {path}")
    return 0


def cmd_check(args) -> int:
    """Print the host readiness checklist without starting the server."""
    from . import host
    from .virt import Hypervisor

    cfg = config_mod.load(args.config)
    hv = Hypervisor(cfg["libvirt"]["uri"], cfg["storage"], cfg["network"])
    try:
        caps = hv.caps()
    except Exception as exc:
        print(f"[FAIL] cannot connect to libvirt at {hv.uri}: {exc}")
        return 1
    marks = {"ok": " OK ", "warn": "WARN", "error": "FAIL"}
    for c in host.readiness(caps, None, None):
        print(f"[{marks[c['level']]}] {c['label']}: {c['detail']}")
        if c["fix"]:
            print(f"        fix: {c['fix']}")
    return 0


def cmd_looking_glass(args) -> int:
    """Open Looking Glass on the running VM that has it (bound to Super+L in the kiosk)."""
    from .desktop import lookingglass
    from .virt import Hypervisor

    cfg = config_mod.load(args.config)
    hv = Hypervisor(cfg["libvirt"]["uri"], cfg["storage"], cfg["network"])
    found = lookingglass.running_vm(hv)
    if found is None:
        print("No running VM has Looking Glass. Start one first.", file=sys.stderr)
        return 1
    started, message = lookingglass.launch(found[1])
    print(f"{found[0]}: {message}", file=sys.stdout if started else sys.stderr)
    return 0 if started else 1


def cmd_desktop(args) -> int:
    """Open the native GTK window (needs py3-gobject3 and gtk+3.0)."""
    try:
        from .desktop.app import run
    except (ImportError, ValueError) as exc:  # ValueError: gi has no GTK 3 typelib
        print(f"The desktop app needs GTK 3 and pycairo for Python: {exc}\n"
              "Install it with: doas apk add py3-gobject3 py3-cairo gtk+3.0 font-overpass", file=sys.stderr)
        return 1
    return run(config_mod.load(args.config), args.theme, args.fullscreen)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tarn", description="Web manager for KVM/libvirt hosts")
    parser.add_argument("--config", help=f"config file (default {config_mod.DEFAULT_CONFIG_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"tarn {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the web server")
    p.add_argument("--listen")
    p.add_argument("--port", type=int)
    p.add_argument("--insecure-http", action="store_true", help="allow plain HTTP (testing only)")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("passwd", help="set the admin password").set_defaults(func=cmd_passwd)
    sub.add_parser("check", help="check the host is ready for VMs").set_defaults(func=cmd_check)

    sub.add_parser("looking-glass", help="open Looking Glass on the running VM that has it"
                   ).set_defaults(func=cmd_looking_glass)

    p = sub.add_parser("desktop", help="open the native window on this machine's screen")
    p.add_argument("--theme", choices=("light", "dark"), default="light")
    p.add_argument("--fullscreen", action="store_true", help="start full screen (F11 toggles)")
    p.set_defaults(func=cmd_desktop)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

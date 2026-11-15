"""Configuration loading.

Tarn reads a TOML file (default /etc/tarn/tarn.toml). Every key has a
default, so an empty or missing file still gives a working setup.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_CONFIG_PATH = "/etc/tarn/tarn.toml"

DEFAULTS: dict = {
    "server": {
        "listen": "0.0.0.0",
        "port": 8006,
        "tls_cert": "/etc/tarn/tls/cert.pem",
        "tls_key": "/etc/tarn/tls/key.pem",
        # Plain HTTP is only allowed when explicitly requested; the login
        # password would otherwise cross the network in the clear.
        "allow_plain_http": False,
    },
    "auth": {
        "password_file": "/etc/tarn/passwd",
        "session_hours": 12,
    },
    "libvirt": {
        "uri": "qemu:///system",
    },
    "storage": {
        "images_pool": "default",
        "images_path": "/var/lib/libvirt/images",
        "iso_pool": "iso",
        "iso_path": "/var/lib/libvirt/iso",
    },
    "network": {
        "default": "default",
        # Create libvirt's NAT network (virbr0, 192.168.122.0/24) at startup
        # if it does not exist. This is the right choice on Wi-Fi hosts.
        "create_default": True,
    },
    "stats": {
        "interval_seconds": 2,
        "history_points": 150,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    path: str
    data: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))

    def __getitem__(self, section: str) -> dict:
        return self.data[section]


def load(path: str | None = None) -> Config:
    path = path or os.environ.get("TARN_CONFIG") or DEFAULT_CONFIG_PATH
    data = copy.deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            data = _merge(DEFAULTS, tomllib.load(fh))
    return Config(path=path, data=data)

"""IP geolocation lookup — free ip-api.com HTTP API, no key required.

Fail-open by design: a login must never be blocked by a flaky third-party
geo lookup. Short timeout, no retries, caller stores whatever comes back
(possibly an empty dict) alongside the LoginEvent.
"""
import os
import ipaddress

import requests

GEO_API_URL = os.environ.get("GEO_API_URL", "http://ip-api.com/json")
_TIMEOUT = 2.0


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local)


def lookup_ip(ip: str) -> dict:
    if not ip or not _is_public_ip(ip):
        return {}
    try:
        resp = requests.get(f"{GEO_API_URL}/{ip}", timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "fail":
            return {}
        return data
    except requests.RequestException:
        return {}

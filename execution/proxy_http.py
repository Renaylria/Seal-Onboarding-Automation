"""
proxy_http.py — Provide a proxy-aware httplib2.Http for Google API clients.

On this machine, DNS resolution only works through the local SOCKS5 proxy
(ALL_PROXY / port 61809).  The standard httplib2.Http used by
googleapiclient does NOT honour environment proxy variables, so API calls
fail with "Unable to find the server".

This module detects the ALL_PROXY environment variable and, when present,
returns an httplib2.Http pre-configured with the SOCKS proxy.  Scripts
pass the result to `build(..., http=AuthorizedHttp(creds, http=...))`.
"""

import os
import re
import httplib2

_SOCKS_RE = re.compile(r"socks5h?://(?P<host>[^:]+):(?P<port>\d+)")


def make_http() -> httplib2.Http:
    """Return an httplib2.Http that routes through the SOCKS proxy if configured."""
    all_proxy = os.environ.get("ALL_PROXY", "")
    m = _SOCKS_RE.match(all_proxy)
    if m:
        import socks
        proxy_info = httplib2.ProxyInfo(
            socks.PROXY_TYPE_SOCKS5,
            m.group("host"),
            int(m.group("port")),
        )
        return httplib2.Http(proxy_info=proxy_info)
    return httplib2.Http()

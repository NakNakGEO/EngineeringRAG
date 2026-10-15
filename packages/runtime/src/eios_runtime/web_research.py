"""Public web research for agents: the text of a page on an allowlisted host, nothing else.

It downloads text, never programs: only ``https``, only hosts the operator allowed, only text
content types, size-capped, no redirects to other hosts, and only public addresses (the host is
resolved first and the connection goes to that checked address, so a name cannot be re-pointed at
an internal service between the check and the request). Everything fetched becomes RAW evidence.

The allowlist comes from ``EIOS_WEB_ALLOWED_HOSTS`` (comma separated) and can be changed in the UI
(stored in ``platform.setting``, admin token). Empty = web research is off.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_core.settings import Settings
from eios_domain.ids import utcnow
from eios_storage.tables.platform import setting as setting_t

KEY = "web.hosts"
MAX_BYTES = 1_000_000
MAX_REDIRECTS = 3
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml", "application/yaml", "application/x-yaml")
_REFRESH_EVERY = 3.0
_HOST = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class WebResearchError(Exception):
    """The page could not (or may not) be fetched; the message says why."""


def clean_hosts(raw: list[str]) -> list[str]:
    out: list[str] = []
    for h in raw:
        host = h.strip().lower()
        if not host:
            continue
        if not _HOST.match(host):
            raise WebResearchError(f"'{h}' is not a host name (use example.com or *.example.com)")
        try:
            ipaddress.ip_address(host)
            raise WebResearchError("IP addresses are not accepted; use host names")
        except ValueError:
            pass
        if host not in out:
            out.append(host)
    if len(out) > 100:
        raise WebResearchError("at most 100 hosts")
    return out


class WebSettings:
    """The allowlist, from ``.env`` or chosen in the UI (the UI wins), shared by API and worker."""

    def __init__(self, engine: AsyncEngine, env: Settings) -> None:
        self._engine = engine
        self._env = env
        self._checked = 0.0
        self._stored: list[str] | None = None

    def _env_hosts(self) -> list[str]:
        try:
            return clean_hosts(self._env.web_allowed_hosts.split(","))
        except WebResearchError:
            return []

    async def refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._checked < _REFRESH_EVERY:
            return
        self._checked = now
        try:
            async with self._engine.connect() as conn:
                row = (await conn.execute(sa.select(setting_t.c.value).where(setting_t.c.key == KEY))).first()
        except Exception:  # a database hiccup must not turn the feature off or on by accident
            return
        self._stored = list(row.value.get("hosts", [])) if row is not None else None

    @property
    def hosts(self) -> list[str]:
        return list(self._stored) if self._stored is not None else self._env_hosts()

    def describe(self) -> dict[str, Any]:
        source = "ui" if self._stored is not None else ("env" if self._env_hosts() else "none")
        return {"source": source, "hosts": self.hosts, "enabled": bool(self.hosts)}

    async def save(self, hosts: list[str]) -> None:
        value = {"hosts": clean_hosts(hosts)}
        async with self._engine.begin() as conn:
            await conn.execute(
                pg_insert(setting_t)
                .values(key=KEY, value=value, updated_at=utcnow())
                .on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": utcnow()})
            )
        await self.refresh(force=True)

    async def clear(self) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(sa.delete(setting_t).where(setting_t.c.key == KEY))
        await self.refresh(force=True)


# -- fetching ----------------------------------------------------------------------------------
class _Text(HTMLParser):
    """Visible text of an HTML page: no scripts, styles or markup."""

    SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    parser = _Text()
    parser.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    return parser.title.strip(), text


def is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_global and not ip.is_multicast


async def _public_address(host: str, port: int) -> str:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebResearchError(f"cannot resolve '{host}'") from exc
    addresses = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not addresses or not all(is_public(a) for a in addresses):
        raise WebResearchError(f"'{host}' does not point at a public address")
    return str(addresses[0])


async def fetch_bytes(
    url: str, *, allow_host: Any, max_bytes: int = MAX_BYTES, timeout: float = 30.0,
    accept: tuple[str, ...] = TEXT_TYPES,
) -> dict[str, Any]:
    """Fetch one response body. ``allow_host(host)`` is awaited for the first host and every
    redirect hop and must raise to refuse. Returns ``{data, truncated, final_url, content_type}``."""
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme != "https" or not parts.hostname:
            raise WebResearchError("only https addresses can be fetched")
        if parts.username or parts.password or (parts.port not in (None, 443)):
            raise WebResearchError("addresses with credentials or a custom port are not accepted")
        host = parts.hostname.lower()
        await allow_host(host)
        ip = await _public_address(host, 443)
        netloc_ip = f"[{ip}]" if ":" in ip else ip
        target = parts._replace(netloc=netloc_ip).geturl()
        headers = {"Host": host, "User-Agent": "EngineeringOS-research/1", "Accept": ", ".join(accept)}
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream("GET", target, headers=headers, extensions={"sni_hostname": host}) as r:
                    if r.status_code in {301, 302, 303, 307, 308}:
                        location = r.headers.get("location")
                        if not location:
                            raise WebResearchError("redirect without a location")
                        current = urljoin(current, location)
                        continue
                    if r.status_code >= 400:
                        raise WebResearchError(f"the site answered HTTP {r.status_code}")
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if not any(ctype.startswith(t) for t in accept):
                        raise WebResearchError(f"'{ctype or 'unknown'}' is not an accepted type here")
                    body = bytearray()
                    truncated = False
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            truncated = True
                            del body[max_bytes:]
                            break
        except httpx.HTTPError as exc:
            raise WebResearchError(f"request failed: {type(exc).__name__}") from exc
        return {"data": bytes(body), "truncated": truncated, "final_url": current, "content_type": ctype}
    raise WebResearchError("too many redirects")


async def fetch_text(url: str, *, allow_host: Any, max_bytes: int = MAX_BYTES, timeout: float = 20.0) -> dict[str, Any]:
    """Fetch one page as text (HTML is reduced to its visible text)."""
    got = await fetch_bytes(url, allow_host=allow_host, max_bytes=max_bytes, timeout=timeout, accept=TEXT_TYPES)
    text = got["data"].decode("utf-8", errors="replace")
    title = ""
    if got["content_type"] in {"text/html", "application/xhtml+xml"}:
        title, text = html_to_text(text)
    return {"final_url": got["final_url"], "title": title, "text": text, "truncated": got["truncated"], "content_type": got["content_type"]}


def make_host_gate(policy: Any, web: WebSettings) -> Any:
    """``async (host) -> None``: refuses unless the host is allowlisted and the policy engine allows
    ``net.connect`` (private / metadata / database addresses are denied there regardless)."""
    from eios_domain.events import ActorType
    from eios_domain.policy import PolicyEffect, PolicyRequest

    async def allow(host: str) -> None:
        await web.refresh()
        hosts = web.hosts
        if not hosts:
            raise WebResearchError("web research is off: no host is allowed yet (Settings -> Web research)")
        verdict = await policy.evaluate(
            PolicyRequest(
                actor_type=ActorType.TOOL, actor_id="web_research", action="net.connect",
                target=f"{host}:443", attributes={"network_allowed": True, "allowed_hosts": hosts},
            )
        )
        if verdict.effect is not PolicyEffect.ALLOW:
            raise WebResearchError("; ".join(verdict.reasons) or "the policy refused the request")

    return allow

"""All domain checks. Each check returns a Result: ok=True / False / None (unknown, never alerts)."""
import asyncio
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import dns.asyncresolver
import dns.exception
import httpx

# name -> label (insertion order = display order)
CHECKS = {
    "dns": "DNS",
    "ns": "Nameserver",
    "mx": "MX (ایمیل)",
    "ping": "Ping",
    "http": "HTTP",
    "https": "HTTPS",
    "redirect": "ریدایرکت HTTP→HTTPS",
    "ssl": "گواهی SSL",
    "speed": "سرعت پاسخ",
    "whois": "انقضای دامنه",
}
# checks whose result does not depend on where they run from
LOCATION_FREE = {"whois"}
DEFAULT_CHECKS = [n for n in CHECKS if n != "mx"]

TIMEOUT = 10
SLOW_SECONDS = 3
SSL_WARN_DAYS = 14
DOMAIN_WARN_DAYS = 30
HEADERS = {"User-Agent": "UptimeBot/1.0"}
SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}


@dataclass
class Result:
    ok: "bool | None"
    detail: str


@dataclass
class Fetch:
    resp: "httpx.Response | None"
    elapsed: float
    error: "str | None"


def _err(e: Exception) -> str:
    return str(e) or type(e).__name__


def base_domain(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


async def _resolve(name: str, rtype: str):
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 5
    return await resolver.resolve(name, rtype)


async def _fetch(url: str, follow: bool) -> Fetch:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(follow_redirects=follow, timeout=TIMEOUT, headers=HEADERS) as client:
            resp = await client.get(url)
        return Fetch(resp, time.monotonic() - start, None)
    except Exception as e:
        return Fetch(None, time.monotonic() - start, _err(e))


async def check_dns(domain: str) -> Result:
    ips = []
    try:
        for rtype in ("A", "AAAA"):
            try:
                ips += [a.to_text() for a in await _resolve(domain, rtype)]
            except dns.resolver.NoAnswer:
                pass
    except dns.exception.DNSException as e:
        return Result(False, _err(e))
    return Result(True, ", ".join(ips)) if ips else Result(False, "رکورد A/AAAA وجود ندارد")


async def check_ns(domain: str) -> Result:
    try:
        names = sorted(a.to_text().rstrip(".") for a in await _resolve(base_domain(domain), "NS"))
    except dns.exception.DNSException as e:
        return Result(False, _err(e))
    return Result(True, ", ".join(names))


async def check_mx(domain: str) -> Result:
    try:
        names = sorted(a.exchange.to_text().rstrip(".") for a in await _resolve(domain, "MX"))
    except dns.exception.DNSException as e:
        return Result(False, _err(e))
    return Result(True, ", ".join(names))


async def check_ping(domain: str) -> Result:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "2", "-W", "3", domain,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return Result(None, "دستور ping روی سرور نصب نیست")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
    except asyncio.TimeoutError:
        proc.kill()
        return Result(False, "timeout")
    if proc.returncode != 0:
        return Result(False, "بدون پاسخ")
    m = re.search(r"= [\d.]+/([\d.]+)/", out.decode(errors="ignore"))
    return Result(True, f"{float(m.group(1)):.0f}ms" if m else "OK")


def check_http(f: Fetch) -> Result:
    if f.error:
        return Result(False, f.error)
    code = f.resp.status_code
    return Result(code < 400, f"HTTP {code}")


def check_https(f: Fetch) -> Result:
    return check_http(f)


def check_redirect(f: Fetch, http_selected: bool) -> Result:
    if f.error:
        return Result(None if http_selected else False, f.error)
    loc = f.resp.headers.get("location", "")
    if f.resp.status_code in (301, 302, 303, 307, 308) and loc.startswith("https://"):
        return Result(True, f"{f.resp.status_code}")
    return Result(False, f"ریدایرکت به HTTPS ندارد (HTTP {f.resp.status_code})")


def check_speed(f: Fetch, https_selected: bool) -> Result:
    if f.error:
        return Result(None if https_selected else False, f.error)
    ok = f.elapsed <= SLOW_SECONDS
    return Result(ok, f"{f.elapsed:.2f}s" + ("" if ok else f" (بیشتر از {SLOW_SECONDS}s)"))


async def check_ssl(domain: str) -> Result:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(domain, 443, ssl=ssl.create_default_context(), server_hostname=domain),
            TIMEOUT,
        )
    except ssl.SSLCertVerificationError as e:
        return Result(False, e.verify_message or _err(e))
    except (OSError, asyncio.TimeoutError) as e:
        return Result(False, _err(e))
    cert = writer.get_extra_info("ssl_object").getpeercert()
    writer.close()
    days = int((ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) // 86400)
    if days < SSL_WARN_DAYS:
        return Result(False, f"فقط {days} روز تا انقضا")
    return Result(True, f"{days} روز تا انقضا")


async def check_whois(domain: str) -> Result:
    """Domain expiry via RDAP. Lookup problems are 'unknown', not failures."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT, headers=HEADERS) as client:
            resp = await client.get(f"https://rdap.org/domain/{base_domain(domain)}")
        if resp.status_code != 200:
            return Result(None, f"RDAP در دسترس نیست ({resp.status_code})")
        expiry = next(
            (e["eventDate"] for e in resp.json().get("events", []) if e.get("eventAction") == "expiration"), None
        )
        if not expiry:
            return Result(None, "تاریخ انقضا در RDAP نیست")
        days = (datetime.fromisoformat(expiry.replace("Z", "+00:00")) - datetime.now(timezone.utc)).days
    except Exception as e:
        return Result(None, _err(e))
    if days < DOMAIN_WARN_DAYS:
        return Result(False, f"فقط {days} روز تا انقضای دامنه")
    return Result(True, f"{days} روز تا انقضا")


async def run_checks(domain: str, names) -> "dict[str, Result]":
    names = [n for n in CHECKS if n in set(names)]
    wanted = set(names)
    http = asyncio.create_task(_fetch(f"http://{domain}", False)) if wanted & {"http", "redirect"} else None
    https = asyncio.create_task(_fetch(f"https://{domain}", True)) if wanted & {"https", "speed"} else None

    async def run(name: str) -> Result:
        try:
            if name == "dns":
                return await check_dns(domain)
            if name == "ns":
                return await check_ns(domain)
            if name == "mx":
                return await check_mx(domain)
            if name == "ping":
                return await check_ping(domain)
            if name == "ssl":
                return await check_ssl(domain)
            if name == "whois":
                return await check_whois(domain)
            if name == "http":
                return check_http(await http)
            if name == "https":
                return check_https(await https)
            if name == "redirect":
                return check_redirect(await http, "http" in wanted)
            return check_speed(await https, "https" in wanted)
        except Exception as e:
            return Result(None, f"خطای داخلی: {_err(e)}")

    return dict(zip(names, await asyncio.gather(*(run(n) for n in names))))


async def run_remote(url: str, token: str, domain: str, names) -> "dict[str, Result]":
    """Run checks through the Iran-hosted checker (iran-checker/check.php). Raises on failure."""
    async with httpx.AsyncClient(timeout=60, headers=HEADERS) as client:
        resp = await client.post(url, headers={"X-Token": token}, json={"domain": domain, "checks": list(names)})
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    data = resp.json().get("results", {})
    return {n: Result(data[n].get("ok"), str(data[n].get("detail", ""))) for n in CHECKS if n in data}

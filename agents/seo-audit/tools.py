"""SEO fetch tools for the `seo-audit` agent.

Why these exist rather than the built-in `WebFetch` capability: `WebFetch`
(both the model's native fetch and pydantic-ai's local fallback) returns
*markdownified* content — `md(text, strip=['img', 'script', 'style'])`. That
conversion deletes precisely what an SEO audit measures:

  - `<head>` is gone, so meta description, canonical, hreflang, meta robots
    and Open Graph tags are unobservable;
  - `script` is stripped, so JSON-LD structured data is unobservable;
  - `img` is stripped, so alt-text coverage is unobservable;
  - there is no status code and no response headers, so redirect chains,
    `X-Robots-Tag` and soft-404s are unobservable;
  - it sends `Accept: text/markdown`, so a server that honours that header
    returns a representation Googlebot never receives — the audit would be
    measuring a different page than the one that gets indexed.

So these tools do the fetching themselves and return *facts*: status codes,
headers, the redirect chain, and deterministically extracted head/body
signals. Judgement stays with the model; extraction stays in Python, where it
is reproducible and cheap.

Parsing uses only `html.parser` from the standard library. BeautifulSoup and
lxml are not in the miragen base image, and adding them would require a new
image release — a human-gated step this agent does not need.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag, urlparse
from xml.etree import ElementTree

import httpx

from miragen import register

# Bounds. An autonomous weekly job should never be the reason a page, a host,
# or a token budget falls over.
MAX_BYTES = 2_500_000
TIMEOUT_S = 20.0
MAX_REDIRECTS = 10
MAX_SITEMAP_URLS = 2_000

# Identify honestly. This bot hits Muutto365's own site on a schedule; an
# operator reading access logs should be able to tell what it is without
# guessing, and it must not masquerade as Googlebot.
USER_AGENT = (
    "Muutto365-SEO-Audit/1.0 (+https://muutto365.fi; miragen autonomous agent)"
)

# Response headers an SEO audit actually reasons about. The full header set is
# mostly noise in a model's context window; these are the ones that change a
# conclusion.
HEADERS_OF_INTEREST = (
    "content-type",
    "x-robots-tag",
    "location",
    "link",
    "cache-control",
    "content-encoding",
    "content-language",
    "vary",
)


class FetchError(Exception):
    """A fetch could not be completed. Surfaced as `ok: false`, never as empty
    data — an unfetched page and a page with no findings must never look the
    same to the model reading these results."""


# ── Safety ───────────────────────────────────────────────────────────────────

def _assert_fetchable(url: str) -> None:
    """Reject non-HTTP schemes and hosts that resolve to private space.

    This agent runs on `miragen-net`, alongside the daemon and every other
    agent container, and it takes URLs discovered from page content — so
    "fetch this URL" must not become a way to probe the internal network.

    Resolution here and connection later are not atomic (a DNS rebind between
    the two would defeat this). That residual risk is accepted for a job whose
    targets are its own public marketing site; it is not a general-purpose
    fetcher.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"refusing non-HTTP scheme '{parsed.scheme or ''}' in {url!r}")
    host = parsed.hostname
    if not host:
        raise FetchError(f"no host in {url!r}")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"DNS resolution failed for {host}: {exc}") from exc

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise FetchError(
                f"refusing to fetch {url!r}: {host} resolves to non-public address {addr}"
            )


# ── HTML extraction ──────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


class _SEOParser(HTMLParser):
    """Collects the head/body signals an SEO audit is built from.

    Deliberately tolerant: real pages are malformed, and a parser that raises
    on bad markup would turn "this page has a problem" into "the audit
    crashed". Unclosed tags degrade into missing data, not exceptions.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_lang: str | None = None
        self.title: str | None = None
        self.metas: list[dict[str, str]] = []
        self.link_tags: list[dict[str, str]] = []
        self.headings: list[tuple[int, str]] = []
        self.images: list[dict[str, str]] = []
        self.anchors: list[dict[str, str]] = []
        self.jsonld_blocks: list[str] = []
        self.text_words = 0

        self._title_parts: list[str] = []
        self._in_title = False
        self._heading_level: int | None = None
        self._heading_parts: list[str] = []
        self._in_jsonld = False
        self._jsonld_parts: list[str] = []
        # Depth of nesting inside non-content elements. Their text is not page
        # copy and must not inflate the word count.
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}

        if tag == "html" and "lang" in a:
            self.html_lang = a["lang"].strip() or None
        elif tag == "title" and self.title is None:
            self._in_title = True
        elif tag == "meta":
            self.metas.append(a)
        elif tag == "link":
            self.link_tags.append(a)
        elif tag == "img":
            self.images.append(a)
        elif tag == "a":
            self.anchors.append(a)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_level = int(tag[1])
            self._heading_parts = []
        elif tag in ("script", "style", "noscript", "template"):
            if tag == "script" and a.get("type", "").strip().lower() == "application/ld+json":
                self._in_jsonld = True
                self._jsonld_parts = []
            self._suppress += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = "".join(self._title_parts).strip()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._heading_level is not None:
            text = " ".join("".join(self._heading_parts).split())
            self.headings.append((self._heading_level, text))
            self._heading_level = None
        elif tag in ("script", "style", "noscript", "template"):
            if tag == "script" and self._in_jsonld:
                self.jsonld_blocks.append("".join(self._jsonld_parts))
                self._in_jsonld = False
            self._suppress = max(0, self._suppress - 1)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_jsonld:
            self._jsonld_parts.append(data)
        if self._heading_level is not None:
            self._heading_parts.append(data)
        if self._suppress == 0:
            self.text_words += len(_WORD_RE.findall(data))


def _meta_content(metas: list[dict[str, str]], *, name: str = "", prop: str = "") -> str | None:
    for m in metas:
        if name and m.get("name", "").strip().lower() == name:
            return m.get("content", "").strip()
        if prop and m.get("property", "").strip().lower() == prop:
            return m.get("content", "").strip()
    return None


def _extract(html: str, final_url: str) -> dict:
    """Turn HTML into the structured facts the model reasons over."""
    parser = _SEOParser()
    parser.feed(html)
    parser.close()

    canonical = None
    hreflang: list[dict[str, str]] = []
    for link in parser.link_tags:
        rels = link.get("rel", "").strip().lower().split()
        href = link.get("href", "").strip()
        if "canonical" in rels and canonical is None and href:
            canonical = urljoin(final_url, href)
        if "alternate" in rels and link.get("hreflang"):
            hreflang.append(
                {"hreflang": link["hreflang"].strip(), "href": urljoin(final_url, href)}
            )

    # Structured data: report parse failures explicitly. Broken JSON-LD is a
    # real and common SEO defect, and silently dropping it would hide it.
    structured_data: list[dict] = []
    for raw in parser.jsonld_blocks:
        block = raw.strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError as exc:
            structured_data.append({"valid": False, "error": str(exc), "raw_excerpt": block[:300]})
            continue
        nodes = parsed if isinstance(parsed, list) else [parsed]
        types = [
            n.get("@type")
            for n in nodes
            if isinstance(n, dict) and n.get("@type") is not None
        ]
        structured_data.append({"valid": True, "types": types})

    host = urlparse(final_url).netloc.lower()
    internal = external = nofollow = 0
    for a in parser.anchors:
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        target = urldefrag(urljoin(final_url, href))[0]
        netloc = urlparse(target).netloc.lower()
        if netloc == host:
            internal += 1
        else:
            external += 1
        if "nofollow" in a.get("rel", "").lower():
            nofollow += 1

    missing_alt = [
        i.get("src", "")[:200] for i in parser.images if not i.get("alt", "").strip()
    ]

    return {
        "title": parser.title,
        "title_length": len(parser.title) if parser.title else 0,
        "meta_description": _meta_content(parser.metas, name="description"),
        "meta_robots": _meta_content(parser.metas, name="robots"),
        "canonical": canonical,
        "html_lang": parser.html_lang,
        "hreflang": hreflang,
        "og": {
            k: _meta_content(parser.metas, prop=f"og:{k}")
            for k in ("title", "description", "image", "url", "type")
        },
        "twitter_card": _meta_content(parser.metas, name="twitter:card"),
        "viewport": _meta_content(parser.metas, name="viewport"),
        "headings": [{"level": lvl, "text": txt[:200]} for lvl, txt in parser.headings],
        "h1_count": sum(1 for lvl, _ in parser.headings if lvl == 1),
        "word_count": parser.text_words,
        "images_total": len(parser.images),
        "images_missing_alt": len(missing_alt),
        "images_missing_alt_examples": missing_alt[:10],
        "links_internal": internal,
        "links_external": external,
        "links_nofollow": nofollow,
        "structured_data": structured_data,
    }


# ── Transport ────────────────────────────────────────────────────────────────

async def _get(url: str) -> dict:
    """Fetch one URL, following redirects manually so the chain is observable.

    `follow_redirects=True` would collapse the hop sequence into a final
    response, and the sequence is itself an SEO finding: a 302 where a 301
    belongs, or a chain more than one hop deep, is a defect you cannot see
    from the endpoint alone.
    """
    chain: list[dict] = []
    current = url

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=TIMEOUT_S,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _assert_fetchable(current)
            try:
                response = await client.get(current)
            except httpx.HTTPError as exc:
                raise FetchError(f"request to {current} failed: {exc}") from exc

            if response.is_redirect and "location" in response.headers:
                target = urljoin(current, response.headers["location"])
                chain.append({"url": current, "status": response.status_code, "location": target})
                current = target
                continue

            body = response.content[:MAX_BYTES]
            return {
                "requested_url": url,
                "final_url": str(response.url),
                "status": response.status_code,
                "redirect_chain": chain,
                "redirect_count": len(chain),
                "headers": {
                    h: response.headers[h] for h in HEADERS_OF_INTEREST if h in response.headers
                },
                "body_bytes": len(response.content),
                "truncated": len(response.content) > MAX_BYTES,
                "text": body.decode(response.encoding or "utf-8", errors="replace"),
            }

    raise FetchError(f"more than {MAX_REDIRECTS} redirects starting at {url}")


# ── Registered tools ─────────────────────────────────────────────────────────

@register
async def fetch_page(ctx, url: str) -> dict:
    """Fetch a URL and return its HTTP facts and extracted SEO signals.

    Returns status, redirect chain, selected response headers, and — for HTML
    responses — title, meta description, meta robots, canonical, hreflang,
    Open Graph, heading structure, word count, image alt coverage, link counts
    and JSON-LD structured data. On failure returns `ok: false` with a reason.
    """
    try:
        result = await _get(url)
    except FetchError as exc:
        # Explicit failure, never an empty result: "could not measure" and
        # "measured, found nothing" are opposite conclusions.
        return {"ok": False, "url": url, "error": str(exc)}

    content_type = result["headers"].get("content-type", "")
    is_html = "html" in content_type.lower() or not content_type

    payload = {
        "ok": True,
        "requested_url": result["requested_url"],
        "final_url": result["final_url"],
        "status": result["status"],
        "redirect_chain": result["redirect_chain"],
        "redirect_count": result["redirect_count"],
        "headers": result["headers"],
        "body_bytes": result["body_bytes"],
        "truncated": result["truncated"],
        "is_html": is_html,
    }
    if is_html:
        payload["seo"] = _extract(result["text"], result["final_url"])
    return payload


@register
async def fetch_robots(ctx, site_url: str) -> dict:
    """Fetch and parse /robots.txt for a site.

    Returns the raw file, the sitemap URLs it declares, and the directives
    grouped by user-agent, so the model can check whether the crawlers that
    matter are actually allowed to reach the pages that matter.
    """
    robots_url = urljoin(site_url, "/robots.txt")
    try:
        result = await _get(robots_url)
    except FetchError as exc:
        return {"ok": False, "url": robots_url, "error": str(exc)}

    if result["status"] != 200:
        # A missing robots.txt is a finding in itself, not an error — report
        # the status and let the model judge it.
        return {
            "ok": True,
            "url": result["final_url"],
            "status": result["status"],
            "exists": False,
            "sitemaps": [],
            "groups": [],
        }

    sitemaps: list[str] = []
    groups: list[dict] = []
    current: dict | None = None

    for line in result["text"].splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()

        if field == "sitemap":
            sitemaps.append(value)
        elif field == "user-agent":
            if current is None or current["rules"]:
                current = {"user_agents": [value], "rules": []}
                groups.append(current)
            else:
                # Consecutive User-agent lines share one rule block.
                current["user_agents"].append(value)
        elif field in ("allow", "disallow", "crawl-delay") and current is not None:
            current["rules"].append({"directive": field, "value": value})

    return {
        "ok": True,
        "url": result["final_url"],
        "status": result["status"],
        "exists": True,
        "sitemaps": sitemaps,
        "groups": groups,
        "raw": result["text"][:5_000],
    }


@register
async def fetch_sitemap(ctx, sitemap_url: str) -> dict:
    """Fetch a sitemap (or sitemap index) and return the URLs it lists.

    Handles `<sitemapindex>` by reporting the child sitemap URLs rather than
    fetching them, so the model decides how deep to go and the tool never
    fans out unboundedly on its own.
    """
    try:
        result = await _get(sitemap_url)
    except FetchError as exc:
        return {"ok": False, "url": sitemap_url, "error": str(exc)}

    if result["status"] != 200:
        return {
            "ok": False,
            "url": result["final_url"],
            "status": result["status"],
            "error": f"sitemap returned HTTP {result['status']}",
        }

    try:
        root = ElementTree.fromstring(result["text"])
    except ElementTree.ParseError as exc:
        return {
            "ok": False,
            "url": result["final_url"],
            "status": result["status"],
            "error": f"sitemap is not well-formed XML: {exc}",
        }

    # Sitemaps are namespaced; match on the local tag name so a non-standard
    # or absent namespace declaration doesn't silently yield zero URLs.
    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    is_index = _local(root.tag) == "sitemapindex"
    entries: list[dict] = []
    for child in root:
        entry: dict[str, str] = {}
        for node in child:
            name = _local(node.tag)
            if name in ("loc", "lastmod", "changefreq", "priority") and node.text:
                entry[name] = node.text.strip()
        if entry.get("loc"):
            entries.append(entry)
        if len(entries) >= MAX_SITEMAP_URLS:
            break

    return {
        "ok": True,
        "url": result["final_url"],
        "status": result["status"],
        "is_index": is_index,
        "count": len(entries),
        "truncated": len(entries) >= MAX_SITEMAP_URLS,
        "entries": entries,
    }

"""URL extraction, canonicalization, and crawl policy for Discord dumps."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .types import DomainClass, ExtractedLink, NormalizedMessage


_URL_RE = re.compile(r"https?://[^\s<>)\]]+")
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "s"}
_MEDIA_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".mov",
    ".avi",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".zip",
    ".pdf",
)
_MEDIA_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
    "images-ext-1.discordapp.net",
    "pbs.twimg.com",
    "i.ytimg.com",
}


def extract_links_from_message(message: NormalizedMessage) -> list[ExtractedLink]:
    """Extract crawl-eligible links from content and embed URL fields."""

    candidates: list[tuple[str, str]] = []
    candidates.extend((url, "content") for url in _urls_in_text(message.content))
    for embed in message.embeds:
        for key in ("url", "author_url", "provider_url"):
            value = embed.get(key)
            if isinstance(value, str):
                candidates.extend((url, f"embed.{key}") for url in _urls_in_text(value))

    out: list[ExtractedLink] = []
    seen: set[str] = set()
    for raw_url, source_field in candidates:
        canonical = canonicalize_url(raw_url)
        if canonical in seen:
            continue
        seen.add(canonical)
        domain_class = classify_domain(canonical)
        eligible = is_crawl_eligible(canonical)
        if not eligible:
            continue
        out.append(
            ExtractedLink(
                raw_url=raw_url,
                canonical_url=canonical,
                source_message_id=message.message_id,
                source_field=source_field,
                domain_class=domain_class,
                crawl_eligible=eligible,
            )
        )
    return out


def canonicalize_url(url: str) -> str:
    """Canonicalize high-volume URL families before dedupe and crawl policy."""

    cleaned = url.strip().rstrip(".,;:!?)>]\\")
    parts = urlsplit(cleaned)
    scheme = "https"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or parts.path

    x_status = _x_status_id(host, path)
    if x_status:
        return f"https://x.com/i/status/{x_status}"

    youtube_id = _youtube_video_id(host, path, parts.query)
    if youtube_id:
        return f"https://youtube.com/watch?v={youtube_id}"

    arxiv_id = _arxiv_id(host, path)
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"

    query = _clean_query(parts.query)
    return urlunsplit((scheme, host, path, query, ""))


def classify_domain(url: str) -> DomainClass:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path.lower()
    if host.startswith("www."):
        host = host[4:]
    if "discord.com" in host or "discord.gg" in host:
        return "discord_private"
    if host in _MEDIA_HOSTS or path.endswith(_MEDIA_EXTENSIONS):
        return "media"
    if host in {"x.com", "twitter.com", "mobile.twitter.com", "fxtwitter.com", "vxtwitter.com"}:
        return "x"
    if host in {"youtube.com", "youtu.be", "m.youtube.com"}:
        return "youtube"
    if host == "github.com":
        return "github"
    if host == "arxiv.org":
        return "arxiv"
    if host in {"huggingface.co", "hf.co"}:
        return "huggingface"
    return "web"


def is_crawl_eligible(url: str) -> bool:
    return classify_domain(canonicalize_url(url)) not in {"discord_private", "media"}


def _urls_in_text(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;:!?)>]\\") for m in _URL_RE.finditer(text)]


def _clean_query(query: str) -> str:
    safe: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        key_l = key.lower()
        if key_l.startswith("utm_") or key_l in _TRACKING_KEYS:
            continue
        safe.append((key, value))
    return urlencode(safe)


def _x_status_id(host: str, path: str) -> str | None:
    if host not in {"x.com", "twitter.com", "mobile.twitter.com", "fxtwitter.com", "vxtwitter.com"}:
        return None
    match = re.search(r"/(?:i/)?status(?:es)?/(\d+)", path)
    return match.group(1) if match else None


def _youtube_video_id(host: str, path: str, query: str) -> str | None:
    if host == "youtu.be":
        return path.strip("/") or None
    if host not in {"youtube.com", "m.youtube.com"}:
        return None
    if path == "/watch":
        params = dict(parse_qsl(query, keep_blank_values=True))
        return params.get("v") or None
    match = re.match(r"/(?:shorts|embed)/([^/]+)", path)
    return match.group(1) if match else None


def _arxiv_id(host: str, path: str) -> str | None:
    if host != "arxiv.org":
        return None
    match = re.match(r"/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?", path)
    return match.group(1) if match else None

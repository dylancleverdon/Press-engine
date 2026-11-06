"""URL normalisation and social-profile parsing (identity keys for dedupe)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PARAMS = {
    "fbclid", "gclid", "igsh", "igshid", "si", "feature", "_r", "_t", "is_from_webapp", "sender_device",
    "share_app_id", "share_link_id", "ref", "ref_src", "mc_cid", "mc_eid", "spm", "lang",
}


def normalize_url(url: str) -> str:
    """Canonical form used as a cache / dedupe key."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower() if parts.scheme in ("http", "https") else "https"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
               if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS)
    )
    return urlunsplit((scheme, host + port, path, query, ""))


def host_of(url: str | None) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def domain_matches(host: str, domains: list[str] | set[str]) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def absolutize(base: str, href: str) -> str | None:
    href = (href or "").strip()
    if not href or href.startswith(("javascript:", "#", "tel:", "data:")):
        return None
    if href.startswith("mailto:"):
        return href
    try:
        return urljoin(base, href)
    except ValueError:
        return None


@dataclass(frozen=True)
class SocialRef:
    type: str  # IdentityType value
    value: str  # normalized key
    display: str  # human-friendly form
    url: str


LINK_IN_BIO_HOSTS = {
    "linktr.ee", "beacons.ai", "lnk.bio", "solo.to", "bio.link", "campsite.bio", "hoo.be", "msha.ke",
    "linkin.bio", "tap.bio", "komi.io", "allmylinks.com", "bio.site", "linkfly.to", "snipfeed.co",
    "carrd.co", "stan.store", "koji.to", "lnk.to", "fanlink.to", "linkpop.com", "taplink.cc",
}

_RESERVED = {
    "instagram": {"p", "reel", "reels", "stories", "explore", "accounts", "direct", "tv", "about", "legal", "developer"},
    "x": {"intent", "share", "home", "i", "search", "hashtag", "explore", "settings", "login", "signup", "tos", "privacy"},
    "facebook": {"sharer", "sharer.php", "share", "dialog", "plugins", "tr", "groups", "events", "watch", "login", "pages", "profile.php"},
    "tiktok": {"discover", "tag", "music", "foryou", "explore", "live", "login", "upload"},
    "soundcloud": {"discover", "search", "upload", "stream", "you", "pages", "terms-of-use"},
}

HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _first_segment(path: str) -> str:
    segs = [s for s in path.split("/") if s]
    return unquote(segs[0]) if segs else ""


def parse_social(url: str) -> SocialRef | None:
    """Map a profile/channel URL to an identity key, or None if it isn't one."""
    if not url or url.startswith("mailto:"):
        return None
    full = url if "://" in url else "https://" + url
    try:
        parts = urlsplit(full)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.startswith(("www.", "m.", "mobile.")):
        host = host.split(".", 1)[1]
    path = parts.path or ""
    seg = _first_segment(path)

    if host in ("tiktok.com",) or host.endswith(".tiktok.com") and host not in ("vm.tiktok.com", "vt.tiktok.com"):
        if seg.startswith("@") and HANDLE_RE.match(seg[1:]):
            h = seg[1:].lower()
            return SocialRef("tiktok", h, "@" + seg[1:], f"https://www.tiktok.com/@{h}")
        return None
    if host in ("instagram.com", "instagr.am"):
        if seg and seg.lower() not in _RESERVED["instagram"] and HANDLE_RE.match(seg):
            h = seg.lower()
            return SocialRef("instagram", h, "@" + seg, f"https://www.instagram.com/{h}/")
        return None
    if host in ("youtube.com", "music.youtube.com"):
        segs = [s for s in path.split("/") if s]
        if segs and segs[0].startswith("@"):
            h = unquote(segs[0]).lower()
            return SocialRef("youtube", h, unquote(segs[0]), f"https://www.youtube.com/{h}")
        if len(segs) >= 2 and segs[0] == "channel" and segs[1].startswith("UC"):
            return SocialRef("youtube", segs[1], segs[1], f"https://www.youtube.com/channel/{segs[1]}")
        if len(segs) >= 2 and segs[0] in ("c", "user"):
            v = f"{segs[0]}/{unquote(segs[1]).lower()}"
            return SocialRef("youtube", v, segs[1], f"https://www.youtube.com/{v}")
        return None
    if host in ("twitter.com", "x.com"):
        if seg and seg.lower() not in _RESERVED["x"] and HANDLE_RE.match(seg):
            h = seg.lower()
            return SocialRef("x", h, "@" + seg, f"https://x.com/{h}")
        return None
    if host == "bsky.app":
        segs = [s for s in path.split("/") if s]
        if len(segs) >= 2 and segs[0] == "profile":
            h = segs[1].lower()
            return SocialRef("bluesky", h, "@" + segs[1], f"https://bsky.app/profile/{h}")
        return None
    if host in ("threads.net", "threads.com"):
        if seg.startswith("@") and HANDLE_RE.match(seg[1:]):
            h = seg[1:].lower()
            return SocialRef("threads", h, seg, f"https://www.threads.net/@{h}")
        return None
    if host in ("facebook.com", "fb.com"):
        if seg and seg.lower() not in _RESERVED["facebook"] and HANDLE_RE.match(seg):
            h = seg.lower()
            return SocialRef("facebook", h, seg, f"https://www.facebook.com/{h}")
        return None
    if host == "soundcloud.com":
        if seg and seg.lower() not in _RESERVED["soundcloud"] and HANDLE_RE.match(seg):
            h = seg.lower()
            return SocialRef("soundcloud", h, seg, f"https://soundcloud.com/{h}")
        return None
    if host == "open.spotify.com":
        segs = [s for s in path.split("/") if s]
        if segs and segs[0].startswith("intl-"):
            segs = segs[1:]
        if len(segs) >= 2 and segs[0] == "user":
            return SocialRef("spotify", segs[1], segs[1], f"https://open.spotify.com/user/{segs[1]}")
        return None
    if host in LINK_IN_BIO_HOSTS or any(host.endswith("." + h) for h in ("carrd.co", "bio.site")):
        if host.endswith(("carrd.co", "bio.site")) and host not in ("carrd.co", "bio.site"):
            return SocialRef("linkinbio", host, host, f"https://{host}/")
        if seg and HANDLE_RE.match(seg.lstrip("@")):
            v = f"{host}/{seg.lstrip('@').lower()}"
            return SocialRef("linkinbio", v, v, f"https://{v}")
        return None
    return None


def is_social_or_platform_host(host: str) -> bool:
    host = host.lower()
    platforms = {
        "tiktok.com", "instagram.com", "youtube.com", "youtu.be", "twitter.com", "x.com", "bsky.app",
        "threads.net", "threads.com", "facebook.com", "soundcloud.com", "spotify.com", "open.spotify.com",
        "bandcamp.com", "apple.com", "patreon.com", "twitch.tv", "discord.gg", "discord.com", "t.me",
        "medium.com", "substack.com", "linkedin.com", "pinterest.com", "reddit.com", "vimeo.com",
        "google.com", "goo.gl", "bit.ly", "wa.me", "whatsapp.com", "amazon.com", "paypal.me", "ko-fi.com",
    } | LINK_IN_BIO_HOSTS
    return domain_matches(host, platforms)


def site_identity(url: str) -> SocialRef | None:
    """A personal website: the host is the key (skip social platforms)."""
    host = host_of(url)
    if not host or is_social_or_platform_host(host) or "." not in host:
        return None
    return SocialRef("site", host, host, f"https://{host}/")


TIKTOK_VIDEO_RE = re.compile(r"tiktok\.com/@([A-Za-z0-9._-]+)/(?:video|photo)/(\d+)", re.I)
TIKTOK_MUSIC_RE = re.compile(r"tiktok\.com/music/([^/?#]+)", re.I)
SHORT_LINK_HOSTS = {"vm.tiktok.com", "vt.tiktok.com", "tiktok.com/t", "instagr.am", "bit.ly", "lnk.to"}


def is_short_link(url: str) -> bool:
    host = host_of(url)
    return host in ("vm.tiktok.com", "vt.tiktok.com", "bit.ly") or (
        host == "tiktok.com" and urlsplit(url if "://" in url else "https://" + url).path.startswith("/t/")
    )

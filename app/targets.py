"""Target selection for remote-viewing trials.

A target is a photograph picked from a curated set of themes that make good RV targets:
one dominant, distinctive gestalt (water, a tower, dunes, ice...) with strong shape, texture
and colour, and no people, text or distressing content. Two providers are supported:

  * wikimedia: Featured/Quality pictures on Wikimedia Commons. No API key, openly licensed.
  * pexels:    real stock photos via a free API key (RV_PEXELS_API_KEY).

Images are downloaded server-side, re-encoded (strips metadata, validates the bytes) and stored
with the trial, so the page the user sees never links to the source before the reveal.
"""
from __future__ import annotations

import hashlib
import html
import io
import logging
import random
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

import httpx
from PIL import Image

from . import config, tracing

log = logging.getLogger("rv.targets")

# category -> search terms. Categories double as the basis for decoy selection in judging mode
# (each decoy comes from a *different* category than the target and the other decoys, so the options are clearly distinct).
THEMES: dict[str, list[str]] = {
    "water": ["waterfall", "lake reflection", "ocean waves breaking", "fjord", "river rapids", "canal"],
    "mountain": ["alpine peak", "rock arch", "canyon", "sea cliffs", "volcano crater", "granite cliff"],
    "desert": ["sand dunes", "salt flat", "desert canyon", "badlands"],
    "vegetation": ["bamboo forest", "autumn forest", "lavender field", "rice terraces", "cherry blossoms",
                   "rainforest"],
    "ice": ["glacier", "iceberg", "frozen lake", "snow covered forest"],
    "tower": ["lighthouse", "skyscraper", "radio tower", "clock tower", "windmill"],
    "bridge": ["suspension bridge", "stone arch bridge", "railway viaduct"],
    "ancient": ["stone circle", "ancient ruins", "pyramid", "temple ruins", "castle ruins"],
    "transport": ["steam locomotive", "sailing ship", "cargo ship", "biplane", "tram"],
    "industrial": ["wind turbines", "dam", "oil refinery", "container port", "gantry crane",
                   "cooling towers"],
    "city": ["city skyline at night", "neon street", "old town street", "harbor at dusk"],
    "sky": ["lightning storm", "aurora", "sunset clouds", "geyser eruption", "lava flow"],
}

# Keep targets free of people, text, graphics and anything distressing.
_BLOCK = re.compile(
    r"(?<![\w-])(nude|naked|topless|erotic|sexual|porn\w*|corpse|dead|death|killed|victims?|murder\w*|gore|"
    r"blood\w*|wounded|massacre|war|battle|soldiers?|military|weapons?|guns?|bomb\w*|explosion|crash\w*|"
    r"accident|disaster|funeral|graves?|cemetery|skeleton|skull|surgery|portrait|selfie|woman|women|man|men|"
    r"girls?|boys?|child|children|baby|people|crowd\w*|group|team|wedding|poster|flags?|logo|maps?|diagram|"
    r"screenshot|paintings?|drawing|illustration|coat of arms|cartoon|text)(?![\w-])",
    re.I,
)
_CACHE_TTL = 6 * 3600
_cache: dict[tuple[str, str], tuple[float, list["Candidate"]]] = {}


class TargetError(RuntimeError):
    pass


@dataclass
class Candidate:
    provider: str
    source_id: str
    title: str
    image_url: str
    page_url: str
    category: str
    term: str
    creator: str = ""
    license: str = ""
    license_url: str = ""
    description: str = ""
    lat: float | None = None
    lon: float | None = None

    def public(self) -> dict:
        return asdict(self)


@dataclass
class Prepared:
    target: Candidate
    target_jpeg: bytes
    decoys: list[tuple[Candidate, bytes]]


def _ua() -> str:
    contact = config.TARGET_CONTACT or "https://github.com/Bagatron/rv-analyzer"
    return f"RV-Analyzer/1.0 ({contact}) python-httpx"


def _strip(s: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def blocked(*texts: str) -> bool:
    return bool(_BLOCK.search(" ".join(t for t in texts if t)))


def new_coordinate(existing: set[str]) -> str:
    """Random two-block tag (the classic RV 'coordinate'). It carries no information about the target."""
    for _ in range(1000):
        c = f"{secrets.randbelow(10000):04d}-{secrets.randbelow(10000):04d}"
        if c not in existing:
            return c
    raise TargetError("Could not allocate a unique coordinate.")


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
def _providers() -> list[str]:
    p = config.TARGET_PROVIDER
    if p == "wikimedia":
        return ["wikimedia"]
    if p == "pexels":
        return ["pexels"] if config.PEXELS_API_KEY else []
    return (["pexels"] if config.PEXELS_API_KEY else []) + ["wikimedia"]


# Curated first (Featured pictures / Quality images), then progressively broader. The size, licence
# and content filters below still apply to every tier, so the fallback only widens the pool.
_WM_TIERS = (
    '{term} incategory:"Featured pictures on Wikimedia Commons|Quality images" filetype:bitmap',
    '{term} deepcat:"Featured pictures on Wikimedia Commons" filetype:bitmap',
    '{term} filetype:bitmap',
)


def _search_wikimedia(term: str, category: str) -> list[Candidate]:
    for tier in _WM_TIERS:
        out = _search_wikimedia_q(tier.format(term=term), term, category)
        if out:
            return out
    return []


def _search_wikimedia_q(query: str, term: str, category: str) -> list[Candidate]:
    params = {
        "action": "query", "format": "json", "formatversion": "2", "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6", "gsrlimit": "30", "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "1280",
        "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|ImageDescription|ObjectName|"
                               "GPSLatitude|GPSLongitude|Restrictions|NonFree",
    }
    r = httpx.get(config.WIKIMEDIA_API, params=params, headers={"User-Agent": _ua()},
                  timeout=config.TARGET_TIMEOUT_S)
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or []
    if isinstance(pages, dict):  # formatversion=1 shape
        pages = list(pages.values())
    out: list[Candidate] = []
    for p in pages:
        try:
            ii = (p.get("imageinfo") or [None])[0]
            if not ii or ii.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if (ii.get("width") or 0) < 900 or (ii.get("height") or 0) < 600:
                continue
            md = ii.get("extmetadata") or {}
            val = lambda k: _strip((md.get(k) or {}).get("value"))  # noqa: E731
            if val("Restrictions") or val("NonFree").lower() in ("true", "1", "yes"):
                continue
            lic = val("LicenseShortName")
            if not lic:
                continue
            title = re.sub(r"^File:", "", p.get("title", "")).rsplit(".", 1)[0]
            desc = val("ImageDescription")
            if blocked(title, desc, val("ObjectName")):
                continue
            img = ii.get("thumburl") or ii.get("url")
            if not img:
                continue
            lat = lon = None
            try:
                lat, lon = float(val("GPSLatitude")), float(val("GPSLongitude"))
            except ValueError:
                pass
            out.append(Candidate(
                provider="wikimedia", source_id=f"wm:{p.get('pageid') or p.get('title')}",
                title=title, image_url=img, page_url=ii.get("descriptionurl") or "",
                category=category, term=term, creator=val("Artist")[:200], license=lic,
                license_url=val("LicenseUrl"), description=desc[:400], lat=lat, lon=lon,
            ))
        except (AttributeError, TypeError, KeyError):
            continue
    return out


def _search_pexels(term: str, category: str) -> list[Candidate]:
    r = httpx.get(
        config.PEXELS_API, params={"query": term, "per_page": 30, "orientation": "landscape"},
        headers={"Authorization": config.PEXELS_API_KEY, "User-Agent": _ua()},
        timeout=config.TARGET_TIMEOUT_S,
    )
    r.raise_for_status()
    out: list[Candidate] = []
    for p in r.json().get("photos") or []:
        src = p.get("src") or {}
        img = src.get("large2x") or src.get("large") or src.get("original")
        alt = _strip(p.get("alt"))
        if not img or (p.get("width") or 0) < 900 or blocked(alt, term):
            continue
        out.append(Candidate(
            provider="pexels", source_id=f"px:{p.get('id')}", title=alt or term, image_url=img,
            page_url=p.get("url") or "", category=category, term=term,
            creator=p.get("photographer") or "", license="Pexels License",
            license_url="https://www.pexels.com/license/", description=alt[:400],
        ))
    return out


_SEARCH = {"wikimedia": _search_wikimedia, "pexels": _search_pexels}


def _search(provider: str, term: str, category: str) -> list[Candidate]:
    key = (provider, term)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    res = _SEARCH[provider](term, category)
    if res:
        _cache[key] = (time.time(), res)
    return res


def pick_candidate(exclude_ids: set[str], categories: list[str] | None = None,
                   rng: random.Random | None = None) -> Candidate:
    rng = rng or random.SystemRandom()
    provs = _providers()
    if not provs:
        raise TargetError("RV_TARGET_PROVIDER=pexels needs RV_PEXELS_API_KEY.")
    cats = list(categories or THEMES)
    last: Exception | None = None
    for _ in range(8):
        cat = rng.choice(cats)
        term = rng.choice(THEMES[cat])
        for prov in provs:
            try:
                pool = [c for c in _search(prov, term, cat) if c.source_id not in exclude_ids]
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                log.warning("target search failed provider=%s: %s", prov, str(exc)[:200])
                log.debug("target search failed term=%r", term)   # the term reveals the theme: debug only
                continue
            if pool:
                return rng.choice(pool)
    raise TargetError(f"No usable target found ({'; '.join(provs)}). Last error: {last or 'no results'}")


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
_MAX_DOWNLOAD = 25 * 1024 * 1024


def fetch_image(c: Candidate) -> bytes:
    """Download, validate and normalize to a metadata-free JPEG (max 1600px)."""
    buf = bytearray()
    try:
        with httpx.stream("GET", c.image_url, headers={"User-Agent": _ua()},
                          timeout=config.TARGET_TIMEOUT_S, follow_redirects=True) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 16):
                buf += chunk
                if len(buf) > _MAX_DOWNLOAD:
                    raise TargetError("Image too large.")
    except httpx.HTTPError as exc:
        raise TargetError(f"Image download failed: {exc}") from None
    try:
        im = Image.open(io.BytesIO(bytes(buf)))
        im.load()
        im = im.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - any decode problem means "not a usable image"
        raise TargetError(f"Downloaded file is not a valid image: {exc}") from None
    if min(im.size) < 500:
        raise TargetError("Image too small.")
    im.thumbnail((1600, 1600))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _candidate_with_image(exclude: set[str], categories: list[str] | None, rng, tries: int = 3):
    last: Exception | None = None
    for _ in range(tries):
        c = pick_candidate(exclude, categories, rng)
        try:
            return c, fetch_image(c)
        except TargetError as exc:
            last = exc
            exclude = exclude | {c.source_id}
            log.warning("image fetch failed provider=%s: %s", c.provider, str(exc)[:200])
    raise TargetError(str(last))


def prepare(judging: bool, exclude_ids: set[str], rng: random.Random | None = None) -> Prepared:
    """Pick a target (and, for judging, JUDGING_OPTIONS-1 decoys from other categories) and download them."""
    with tracing.span("targets.prepare", judging=judging):    # never attach the target/theme to a span: blind protocol
        return _prepare(judging, exclude_ids, rng)


def _prepare(judging: bool, exclude_ids: set[str], rng: random.Random | None) -> Prepared:
    rng = rng or random.SystemRandom()
    target, tbytes = _candidate_with_image(set(exclude_ids), None, rng)
    decoys: list[tuple[Candidate, bytes]] = []
    if judging:
        cats = [c for c in THEMES if c != target.category]
        rng.shuffle(cats)
        used = set(exclude_ids) | {target.source_id}
        n_decoys = config.JUDGING_OPTIONS - 1
        with ThreadPoolExecutor(max_workers=n_decoys) as ex:
            futs = [ex.submit(_candidate_with_image, set(used), [cat], rng) for cat in cats[:n_decoys]]
            decoys = [f.result() for f in futs]
    return Prepared(target, tbytes, decoys)


def probe() -> dict:
    """Connectivity check for /api/rv/targets/probe. Never exposes the picked image."""
    out = {"providers": _providers(), "ok": False, "image_bytes": None, "error": None}
    try:
        c, img = _candidate_with_image(set(), None, random.SystemRandom(), tries=2)
        out.update(ok=True, image_bytes=len(img), provider_used=c.provider)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out

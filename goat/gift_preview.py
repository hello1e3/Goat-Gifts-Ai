import asyncio
from collections import OrderedDict
from html.parser import HTMLParser
import re
from time import monotonic
from urllib.parse import urlsplit

import httpx

SLUG = re.compile(r"[A-Za-z][A-Za-z0-9]{0,69}-[1-9][0-9]{0,9}", re.ASCII)
TELEGRAM_CDN = re.compile(r"cdn[0-9]+\.telesco\.pe", re.ASCII)


class GiftMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            item = dict(attrs)
            if item.get("property") in {"og:title", "og:image", "og:site_name", "og:description"}:
                self.values[item["property"]] = item.get("content") or ""


def parse_image(html, slug):
    metadata = GiftMetadata()
    metadata.feed(html)
    image = metadata.values.get("og:image", "")
    title = metadata.values.get("og:title", "")
    expected = slug.rsplit("-", 1)[-1]
    parts = urlsplit(image)
    if (metadata.values.get("og:site_name") == "Telegram" and title.endswith(f" #{expected}")
            and parts.scheme == "https" and TELEGRAM_CDN.fullmatch(parts.netloc)
            and re.fullmatch(r"/file/[A-Za-z0-9_-]+\.(?:jpg|png|webp)", parts.path)
            and not parts.query and not parts.fragment):
        return image
    return None


class GiftPreviews:
    def __init__(self):
        self.cache = OrderedDict()
        self.semaphore = asyncio.Semaphore(4)

    async def resolve(self, slug):
        if not SLUG.fullmatch(slug):
            return None
        slug = slug.lower()
        cached = self.cache.get(slug)
        if cached and cached[0] > monotonic():
            self.cache.move_to_end(slug)
            return cached[1]
        if self.semaphore.locked():
            return None
        async with self.semaphore:
            try:
                async with httpx.AsyncClient(timeout=4, follow_redirects=False,
                                             headers={"User-Agent": "Mozilla/5.0"}) as client:
                    async with client.stream("GET", f"https://t.me/nft/{slug}") as response:
                        response.raise_for_status()
                        if "text/html" not in response.headers.get("content-type", ""):
                            return None
                        html = bytearray()
                        async for chunk in response.aiter_bytes():
                            html.extend(chunk)
                            if len(html) > 256_000:
                                return None
                image = parse_image(html.decode("utf-8", errors="replace"), slug)
            except (httpx.HTTPError, ValueError):
                image = None
            self.cache[slug] = (monotonic() + (43200 if image else 60), image)
            self.cache.move_to_end(slug)
            if len(self.cache) > 512:
                self.cache.popitem(last=False)
            return image

"""Discover product IDs only from the storefront's published ranking *pages*.

This module intentionally never falls back to search/shop/catalogue: doing so
would silently violate a ranking-only watchlist. Ranking pages are unofficial
HTML sources and may change; missing or blocked pages are not 'zero products'.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from site_origin import PUBLIC_ORIGIN, PUBLIC_HOSTS

RANKING_PATH = re.compile(r'^/content/ranking_(?:all|\d+)-(?:rising|weekly|monthly)/?$')
PRODUCT_PATH = re.compile(r'^/products/([1-9]\d*)/?$')
HOSTS = PUBLIC_HOSTS


class RankingBlocked(RuntimeError):
    """A ranking page returned an explicit anti-automation HTTP status."""


def resolve_ranking_url(url: str) -> str:
    """Validate a ranking-only path or an existing HTTPS site URL.

    Path-only config avoids embedding an origin in monitor.json while old
    absolute URLs remain accepted for compatibility. External hosts and
    non-ranking pages are never accepted.
    """
    parsed = urlparse(url)
    relative = (not parsed.scheme and not parsed.netloc and url.startswith('/'))
    absolute = (parsed.scheme == 'https' and parsed.hostname in HOSTS
                and parsed.port is None and not parsed.username and not parsed.password)
    if (not (relative or absolute) or parsed.query or parsed.fragment
            or not RANKING_PATH.fullmatch(parsed.path)):
        raise ValueError(f'허용되지 않은 랭킹 페이지 주소: {str(url)[:100]}')
    return f'{PUBLIC_ORIGIN}{parsed.path}' if relative else url


def validate_ranking_url(url: str) -> None:
    resolve_ranking_url(url)


def parse_ranking_html(html: str, *, max_items: int = 200) -> list[int]:
    """Only product anchors on an identified ranking page; never Nuxt globals.

    Product IDs in __NUXT_DATA__ or recommended products outside <main> do not
    prove that a product appeared in the ranking and are intentionally ignored.
    """
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    headings = ' '.join(h.get_text(' ', strip=True) for h in soup.select('h1, h2'))
    if '랭킹' not in title and '랭킹' not in headings:
        raise ValueError('랭킹 페이지 식별 실패 (예기치 않은 페이지/리다이렉트)')
    root = soup.find('main') or soup.body or soup
    ids, seen = [], set()
    for link in root.select('a[href]'):
        raw = link.get('href', '')
        target = urlparse(urljoin(f'{PUBLIC_ORIGIN}/', str(raw)))
        if target.hostname not in HOSTS or target.scheme != 'https':
            continue
        match = PRODUCT_PATH.fullmatch(target.path)
        if not match:
            continue
        pid = int(match.group(1))
        if pid not in seen:
            seen.add(pid)
            ids.append(pid)
        if len(ids) >= max_items:
            break
    return ids


def _browser_pages(urls: list[str], *, steps: int, max_items: int) -> tuple[dict[str, list[int]], list[str]]:
    """Optional scrolling for lazy-loaded ranking cards, one browser per refresh."""
    from playwright.sync_api import sync_playwright

    output: dict[str, list[int]] = {}
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            context = browser.new_context(locale='ko-KR', viewport={'width': 1280, 'height': 900})
            for url in urls:
                page = context.new_page()
                try:
                    response = page.goto(url, wait_until='domcontentloaded', timeout=40000)
                    if response is None or response.status in (403, 429):
                        raise RankingBlocked(f'랭킹 브라우저 응답 거부: {url} HTTP {response.status if response else "none"}')
                    if response.status >= 400:
                        raise RuntimeError(f'랭킹 HTTP {response.status}: {url}')
                    page.wait_for_timeout(800)
                    seen, result = set(), []
                    for step in range(steps + 1):
                        for pid in parse_ranking_html(page.content(), max_items=max_items):
                            if pid not in seen and len(result) < max_items:
                                result.append(pid)
                                seen.add(pid)
                        if len(result) >= max_items or step == steps:
                            break
                        page.evaluate('window.scrollBy(0, Math.max(innerHeight * 1.5, 900))')
                        page.wait_for_timeout(650)
                    if not result:
                        raise ValueError('랭킹 상품 링크 없음')
                    output[url] = result
                except Exception as exc:
                    errors.append(f'browser {url}: {type(exc).__name__}: {str(exc)[:95]}')
                finally:
                    page.close()
        finally:
            browser.close()
    return output, errors


def collect_rankings(client, urls: list[str], *, scroll_steps: int = 0,
                     max_items_per_page: int = 200) -> tuple[list[int], list[str], list[str]]:
    """Return (distinct IDs, successfully read URLs, warnings).

    A ranking page counts as successful only if its ranking-scoped visible
    anchors produced at least one ID. Failure of every source raises an error.
    """
    if not isinstance(urls, list) or not urls or len(urls) > 30:
        raise ValueError('ranking.urls는 1~30개 공식 랭킹 URL 배열이어야 합니다')
    for url in urls:
        if not isinstance(url, str):
            raise ValueError('ranking.urls 항목은 문자열이어야 합니다')
    urls = [resolve_ranking_url(url) for url in urls]
    if not 0 <= scroll_steps <= 15 or not 1 <= max_items_per_page <= 1000:
        raise ValueError('랭킹 스크롤/페이지 상한 설정이 올바르지 않습니다')

    ids_by_url: dict[str, list[int]] = {}
    warnings: list[str] = []
    for url in urls:
        try:
            response = client.get(url)
            final_url = getattr(response, 'url', url)
            if urlparse(final_url).path.rstrip('/') != urlparse(url).path.rstrip('/'):
                raise ValueError('랭킹 페이지가 다른 경로로 리다이렉트됨')
            found = parse_ranking_html(response.text, max_items=max_items_per_page)
            if not found:
                raise ValueError('랭킹 목록에 상품 링크 없음')
            ids_by_url[url] = found
        except Exception as exc:
            if exc.__class__.__name__ == 'Blocked':
                raise
            warnings.append(f'html {url}: {type(exc).__name__}: {str(exc)[:90]}')
    if scroll_steps:
        try:
            browser_ids, browser_errors = _browser_pages(urls, steps=scroll_steps, max_items=max_items_per_page)
            warnings.extend(browser_errors)
            for url, new_ids in browser_ids.items():
                ids_by_url[url] = list(dict.fromkeys(ids_by_url.get(url, []) + new_ids))[:max_items_per_page]
        except Exception as exc:
            warnings.append(f'browser start: {type(exc).__name__}: {str(exc)[:90]}')

    ids, seen = [], set()
    for url in urls:
        for pid in ids_by_url.get(url, []):
            if pid not in seen:
                ids.append(pid)
                seen.add(pid)
    if not ids:
        raise RuntimeError('모든 사이트 랭킹 페이지 탐색 실패: ' + '; '.join(warnings[:4]))
    successful = [url for url in urls if ids_by_url.get(url)]
    return ids, successful, warnings

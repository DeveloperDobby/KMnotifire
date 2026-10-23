"""Discover ranked product IDs using only the storefront's ranking pages.

A failed/blocked ranking page must never be interpreted as an empty ranking.
The official bare/www hostnames are both supported; other storefront pages,
search results, and unverified API endpoints are never substituted.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from requests.exceptions import ConnectionError as HTTPConnectionError, Timeout as HTTPTimeout

from site_origin import PUBLIC_ORIGIN, PUBLIC_HOSTS, SITE_HOST

RANKING_PATH = re.compile(r'^/content/ranking_(?:all|\d+)-(?:rising|weekly|monthly)/?$')
PRODUCT_PATH = re.compile(r'^/products/([1-9]\d*)/?$')
HOSTS = PUBLIC_HOSTS
TRANSIENT_STATUSES = (500, 502, 503, 504)


class RankingBlocked(RuntimeError):
    """A ranking page was explicitly denied (403/429) and must not be retried."""


def resolve_ranking_url(url: str) -> str:
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


def alternate_ranking_url(url: str) -> str:
    """The *same* official ranking path on the other first-party host."""
    parsed = urlparse(resolve_ranking_url(url))
    other = f'www.{SITE_HOST}' if parsed.hostname == SITE_HOST else SITE_HOST
    return urlunparse(parsed._replace(netloc=other))


def parse_ranking_html(html: str, *, max_items: int = 200) -> list[int]:
    """Trust only product links inside a visibly identified ranking page.

    Embedded Nuxt data/recommendations are not proof a product appeared in the
    ranking and are excluded. A page lacking a title or a ranking heading fails
    rather than silently generating a generic catalogue.
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


def _status(exc: Exception) -> int | None:
    """Extract response.status_code from requests' HTTPError without importing requests."""
    response = getattr(exc, 'response', None)
    return getattr(response, 'status_code', None)


def _page_get(client, original_url: str, *, max_items: int) -> tuple[list[int], list[str]]:
    warnings = []
    for index, url in enumerate((original_url, alternate_ranking_url(original_url))):
        try:
            response = client.get(url)
            final = urlparse(getattr(response, 'url', url))
            requested = urlparse(url)
            if (final.hostname not in HOSTS or final.path.rstrip('/') != requested.path.rstrip('/')):
                raise ValueError('랭킹 페이지가 다른 경로로 이동함')
            found = parse_ranking_html(response.text, max_items=max_items)
            if not found:
                raise ValueError('랭킹 목록에 상품 링크 없음')
            if index:
                warnings.append(f'대체 공식 호스트에서 랭킹 수집 성공: {requested.path}')
            return found, warnings
        except Exception as exc:
            if exc.__class__.__name__ == 'Blocked':
                raise
            if _status(exc) in (403, 429):
                raise RankingBlocked(f'랭킹 접근 거부 HTTP {_status(exc)}: {url}') from exc
            status = _status(exc)
            warnings.append(f'html {urlparse(url).hostname}{urlparse(url).path}: '
                            + (f'HTTP {status}' if status else f'{type(exc).__name__}: {str(exc)[:70]}'))
            # Try the other official hostname only for transient server/network failures.
            # Malformed pages, 404s and unexpected redirects are not recoverable this way.
            if status not in TRANSIENT_STATUSES and not isinstance(exc, (HTTPConnectionError, HTTPTimeout)):
                break
    return [], warnings


def _browser_pages(urls: list[str], *, steps: int, max_items: int) -> tuple[dict[str, list[int]], list[str]]:
    """Browser fallback for JS/lazy loading and temporary HTML failures.

    A blocked status aborts instead of trying the alternative hostname. Browser
    failures are reported by source so the previous 180-char log truncation can
    no longer hide the browser's errors.
    """
    from playwright.sync_api import sync_playwright

    output: dict[str, list[int]] = {}
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            context = browser.new_context(locale='ko-KR', viewport={'width': 1280, 'height': 900})
            for original in urls:
                for candidate in (original, alternate_ranking_url(original)):
                    page = context.new_page()
                    try:
                        response = page.goto(candidate, wait_until='domcontentloaded', timeout=20000)
                        if response is None:
                            raise RuntimeError('페이지 응답 없음')
                        if response.status in (403, 429):
                            raise RankingBlocked(f'브라우저 랭킹 접근 거부 HTTP {response.status}: {candidate}')
                        if response.status >= 400:
                            raise RuntimeError(f'HTTP {response.status}')
                        page.wait_for_timeout(800)
                        seen, found = set(), []
                        for step in range(steps + 1):
                            for pid in parse_ranking_html(page.content(), max_items=max_items):
                                if pid not in seen and len(found) < max_items:
                                    found.append(pid)
                                    seen.add(pid)
                            if len(found) >= max_items or step == steps:
                                break
                            page.evaluate('window.scrollBy(0, Math.max(innerHeight * 1.5, 900))')
                            page.wait_for_timeout(650)
                        if not found:
                            raise ValueError('랭킹 상품 링크 없음')
                        output[original] = found
                        break
                    except RankingBlocked:
                        raise
                    except Exception as exc:
                        errors.append(f'browser {urlparse(candidate).hostname}{urlparse(candidate).path}: '
                                      f'{type(exc).__name__}: {str(exc)[:70]}')
                    finally:
                        page.close()
        finally:
            browser.close()
    return output, errors


def collect_rankings(client, urls: list[str], *, scroll_steps: int = 0,
                     max_items_per_page: int = 200,
                     scroll_on_html_success: bool = False) -> tuple[list[int], list[str], list[str]]:
    """Return (ranked IDs, successful sources, detailed source warnings)."""
    if not isinstance(urls, list) or not urls or len(urls) > 30:
        raise ValueError('ranking.urls는 1~30개 랭킹 URL 배열이어야 합니다')
    for url in urls:
        if not isinstance(url, str):
            raise ValueError('ranking.urls 항목은 문자열이어야 합니다')
    urls = [resolve_ranking_url(url) for url in urls]
    if not 0 <= scroll_steps <= 15 or not 1 <= max_items_per_page <= 1000:
        raise ValueError('랭킹 스크롤/페이지 상한 설정이 올바르지 않습니다')

    ids_by_url: dict[str, list[int]] = {}
    warnings: list[str] = []
    for url in urls:
        found, source_warnings = _page_get(client, url, max_items=max_items_per_page)
        warnings.extend(source_warnings)
        if found:
            ids_by_url[url] = found

    # HTML succeeded: don't spend the entire 60-second scan opening the
    # same ranking again in a browser. Allow an explicit opt-in for richer
    # JS-scroll discovery when the environment can serve browser traffic.
    browser_targets = (urls if scroll_on_html_success else
                       [url for url in urls if url not in ids_by_url])
    if scroll_steps and browser_targets:
        try:
            browser_ids, browser_errors = _browser_pages(browser_targets, steps=scroll_steps, max_items=max_items_per_page)
            warnings.extend(browser_errors)
            for url, new_ids in browser_ids.items():
                ids_by_url[url] = list(dict.fromkeys(ids_by_url.get(url, []) + new_ids))[:max_items_per_page]
        except RankingBlocked as exc:
            # Retain valid HTML rank data already gathered. Browser blocking
            # alone must not erase legitimate ranked items.
            warnings.append(f'browser blocked: {exc}')
        except Exception as exc:
            warnings.append(f'browser start: {type(exc).__name__}: {str(exc)[:95]}')

    ids, seen = [], set()
    for url in urls:
        for pid in ids_by_url.get(url, []):
            if pid not in seen:
                ids.append(pid)
                seen.add(pid)
    if not ids:
        # Explicitly show both HTML and browser failures, unlike the old log.
        raise RuntimeError('모든 랭킹 페이지 실패; ' + ' | '.join(warnings[:12]))
    successful = [url for url in urls if ids_by_url.get(url)]
    return ids, successful, warnings

import argparse
import json
import os
import re
import time
from typing import List, Optional, Tuple

from playwright.sync_api import sync_playwright
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timezone


def parse_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    t = text.strip().lower().replace(",", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([kmb]?)$", t)
    try:
        if not m:
            return int(t)
        num = float(m.group(1))
        suf = m.group(2)
        if suf == "k":
            num *= 1_000
        elif suf == "m":
            num *= 1_000_000
        elif suf == "b":
            num *= 1_000_000_000
        return int(num)
    except Exception:
        return None


# -------------------------
# Configuration (edit here)
# -------------------------
EMAIL = ""
PASSWORD = ""
USERS: List[str] = [
    "karimatiyeh",
]
HEADLESS = False  # Set True to run without opening a window


def login(page, email: str, password: str) -> None:
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    page.fill('input[name="session_key"]', email)
    page.fill('input[name="session_password"]', password)
    page.click('button[type="submit"]')
    # Avoid networkidle; wait for a post-login element (nav/feed) or URL change
    try:
        page.wait_for_selector(
            "nav.global-nav__content, header.global-nav, div.feed-shared-update-v2, article, a[href*='/feed/']",
            timeout=60000,
        )
    except Exception:
        # As a fallback, give the page a moment (e.g., during 2FA or captcha)
        time.sleep(3.0)


def open_user_posts(page, username: str) -> None:
    url = f"https://www.linkedin.com/in/{username}/recent-activity/all/"
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector("div.feed-shared-update-v2[data-urn^='urn:li:activity:'], [role='article'][data-urn^='urn:li:activity:']", timeout=60000)
    except Exception:
        # Fallback to more generic selectors before giving up
        page.wait_for_selector("div.feed-shared-update-v2, article", timeout=60000)
    time.sleep(1.0)


def scroll_to_end(page, max_idle_rounds: int = 8, pause_sec: float = 1.2) -> None:
    # Scroll until no new cards are added for several rounds; also click "show more" buttons if present
    def count_cards() -> int:
        try:
            return page.evaluate("() => document.querySelectorAll(\"div.feed-shared-update-v2[data-urn^='urn:li:activity:'], [role='article'][data-urn^='urn:li:activity:']\").length")
        except Exception:
            return 0
    last_count = -1
    idle_rounds = 0
    total_rounds = 0
    while idle_rounds < max_idle_rounds and total_rounds < 500:
        total_rounds += 1
        # Scroll down
        try:
            page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.9))")
        except Exception:
            pass
        time.sleep(pause_sec)
        # Count cards
        current = count_cards()
        if current <= last_count:
            idle_rounds += 1
        else:
            idle_rounds = 0
            last_count = current
        # Occasional small scroll up to trigger lazy loaders
        if total_rounds % 10 == 0:
            try:
                page.evaluate("window.scrollBy(0, -Math.floor(window.innerHeight * 0.3))")
            except Exception:
                pass
    # End of scrolling loop


def find_cards(page):
    # Only select top-level feed cards that represent a post, identified by activity URN
    cards = page.query_selector_all("div.feed-shared-update-v2[data-urn^='urn:li:activity:']")
    # Fallback: some builds use role='article' with data-urn on the element
    if not cards:
        cards = page.query_selector_all("[role='article'][data-urn^='urn:li:activity:']")
    return cards


def extract_counts(card) -> Tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    like_count = comment_count = repost_count = view_count = None
    try:
        # Prefer structured like count first (aria-label or dedicated span)
        try:
            like_btn = card.query_selector('li.social-details-social-counts__reactions button[aria-label]')
            if like_btn:
                al = like_btn.get_attribute('aria-label') or ''
                m = re.search(r'([0-9][\d,\.]*\s*[kmb]?)\s+reactions?', al, flags=re.I)
                if m:
                    like_count = parse_int(m.group(1))
            if like_count is None:
                like_span = card.query_selector('span.social-details-social-counts__reactions-count')
                if like_span:
                    like_count = parse_int((like_span.inner_text() or '').strip())
        except Exception:
            pass
        text = card.inner_text().lower()
        if like_count is None:
            m_like = re.search(r"([0-9][\d,\.]*\s*[kmb]?)\s+(likes|reactions?)\b", text)
            if m_like:
                like_count = parse_int(m_like.group(1))
        m_comment = re.search(r"([0-9][\d,\.]*\s*[kmb]?)\s+comments?\b", text)
        if m_comment:
            comment_count = parse_int(m_comment.group(1))
        m_repost = re.search(r"([0-9][\d,\.]*\s*[kmb]?)\s+reposts?\b", text)
        if m_repost:
            repost_count = parse_int(m_repost.group(1))
        m_views = re.search(r"([0-9][\d,\.]*\s*[kmb]?)\s+views?\b", text)
        if m_views:
            view_count = parse_int(m_views.group(1))
    except Exception:
        pass
    return like_count, comment_count, repost_count, view_count


def extract_media(card) -> Tuple[Optional[str], Optional[str]]:
    img_url = None
    video_url = None
    # Limit search to the content area, not headers/avatars
    content = card.query_selector("div.feed-shared-update-v2__content") or card.query_selector("div.update-components-entity__content-wrapper") or card
    try:
        vid = content.query_selector("video, video source")
        if vid:
            video_url = vid.get_attribute("src")
    except Exception:
        pass
    try:
        # Exclude avatars/profile images and icons
        img = content.query_selector("img:not([class*='avatar']):not([class*='EntityPhoto']):not([alt*='profile']):not([alt=''])")
        if img:
            src = img.get_attribute("src") or img.get_attribute("data-delayed-url")
            if src and "data:image" not in src:
                img_url = src
    except Exception:
        pass
    return img_url, video_url


def detect_type(card) -> str:
    # Priority: video > article > image > text
    try:
        content = card.query_selector("div.feed-shared-update-v2__content") or card.query_selector("div.update-components-entity__content-wrapper") or card
        if content.query_selector("video, video source"):
            return "video"
        # Only treat as article when an actual LinkedIn article is shared or post is an article
        if content.query_selector("a[href*='/pulse/'], a[href*='/articles/'], div.update-components-article, article.update-components-article"):
            return "article"
        # Exclude avatars/profile images
        if content.query_selector("img:not([class*='avatar']):not([class*='EntityPhoto']):not([alt*='profile']):not([alt=''])"):
            return "image"
    except Exception:
        pass
    return "text"


def extract_text(card) -> Optional[str]:
    selectors = [
        "div.update-components-text",
        "div.feed-shared-inline-show-more-text",
        "div.feed-shared-text",
        "span.break-words",
    ]
    for sel in selectors:
        try:
            el = card.query_selector(sel)
            if el:
                t = el.inner_text().strip()
                if t:
                    return t
        except Exception:
            continue
    return None


def extract_links(card) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    post_url = None
    author_url = None
    shared_post_url = None
    shared_job_url = None
    try:
        # Prefer canonical "posts/...-activity-<id>-<suffix>" permalinks, then activity/update links
        candidates = card.query_selector_all(
            "a[href*='/posts/'], "
            "a[href*='/feed/update/urn:li:activity:'], "
            "a[href*='/activity/'], "
            "a[href*='activity-'], "
            "a[href*='/feed/update/']"
        )
        # Rank candidates: posts with '-activity-' > activity/update > others
        ranked: List[str] = []
        for a in candidates:
            href = a.get_attribute("href")
            if not href:
                continue
            ranked.append(href)
        # Select best
        for href in ranked:
            if "/posts/" in href and "-activity-" in href:
                post_url = href
                break
        if not post_url:
            for href in ranked:
                if "activity" in href or "/feed/update/" in href:
                    post_url = href
                    break
        # Fallback: build from card's data-urn if no link found
        if not post_url:
            urn = card.get_attribute("data-urn")
            if urn:
                m = re.search(r"urn:li:activity:(\d+)", urn)
                if m:
                    post_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{m.group(1)}/"
    except Exception:
        pass
    # If we still don't have a shared_post_url, check for article/external links in content
    if not shared_post_url:
        try:
            content = card.query_selector("div.feed-shared-update-v2__content") or card.query_selector("div.update-components-entity__content-wrapper") or card
            # Prefer LinkedIn article links (pulse/articles)
            link = content.query_selector("a[href*='/pulse/'], a[href*='/articles/']")
            if link:
                shared_post_url = link.get_attribute("href")
            if not shared_post_url:
                # Any external http(s) link in content
                ext = content.query_selector("a[href^='http']")
                if ext:
                    href = ext.get_attribute("href")
                    # Ignore obvious profile/company/job links which are handled elsewhere
                    if href and ("linkedin.com/in/" not in href) and ("linkedin.com/company/" not in href) and ("linkedin.com/jobs/view/" not in href):
                        shared_post_url = href
        except Exception:
            pass
    try:
        al = card.query_selector("a[href*='/in/']")
        if al:
            author_url = al.get_attribute("href")
    except Exception:
        pass
    try:
        sp = card.query_selector("a[href*='/posts/'], a[href*='/feed/update/']")
        if sp:
            shared_post_url = sp.get_attribute("href")
    except Exception:
        pass
    try:
        sj = card.query_selector("a[href*='/jobs/view/']")
        if sj:
            shared_job_url = sj.get_attribute("href")
    except Exception:
        pass
    # Clean URL parameters/fragments
    def clean(u: Optional[str]) -> Optional[str]:
        if not u:
            return u
        try:
            parts = urlsplit(u)
            if not parts.scheme and not parts.netloc and parts.path.startswith("/"):
                # Make absolute to linkedin.com for relative paths
                return "https://www.linkedin.com" + parts.path
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except Exception:
            return u
    return clean(post_url), clean(author_url), clean(shared_post_url), clean(shared_job_url)


def extract_author(card) -> Optional[str]:
    try:
        selectors = [
            "span.update-components-actor__title span[dir='ltr']",
            "span.feed-shared-actor__title span[dir='ltr']",
            "span.update-components-actor__title",
            "span.feed-shared-actor__title",
        ]
        for sel in selectors:
            el = card.query_selector(sel)
            if not el:
                continue
            t = el.inner_text().strip()
            if not t:
                continue
            # keep first line, strip bullets like "• 1st"
            t = t.split("\n")[0].strip()
            t = re.sub(r"\s*•\s*.*$", "", t).strip()
            if t:
                return t
    except Exception:
        pass
    return None


def extract_date_and_action(card) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    # Returns (postDate, action, timestampText)
    post_date = None
    action = None
    ts_text = None
    try:
        # Collect possible timestamp containers
        els = card.query_selector_all(
            "span.update-components-actor__sub-description span.visually-hidden, "
            "span.feed-shared-actor__sub-description span.visually-hidden, "
            "span.update-components-actor__sub-description, "
            "span.feed-shared-actor__sub-description, time"
        )
        text = " ".join([e.inner_text().strip() for e in els if e])
        # Prefer relative time tokens like 4mo, 2w, 3d, 5h, 10yr
        # Normalize verbose forms like "10 years ago" -> "10yr"
        rel = None
        # 1) Compact tokens
        m_rel = re.search(r"\b(\d+)\s*(h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|mo|month|months|y|yr|yrs|year|years)\b", text, flags=re.I)
        if not m_rel:
            # Look for forms like "10 years ago"
            m_rel = re.search(r"\b(\d+)\s+(h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|mo|month|months|y|yr|yrs|year|years)\s+ago\b", text, flags=re.I)
        if m_rel:
            num = m_rel.group(1)
            unit = m_rel.group(2).lower()
            if unit in {"h", "hr", "hrs", "hour", "hours"}:
                rel = f"{num}h"
            elif unit in {"d", "day", "days"}:
                rel = f"{num}d"
            elif unit in {"w", "wk", "wks", "week", "weeks"}:
                rel = f"{num}w"
            elif unit in {"mo", "month", "months"}:
                rel = f"{num}mo"
            elif unit in {"y", "yr", "yrs", "year", "years"}:
                rel = f"{num}yr"
        if rel:
            ts_text = rel
            post_date = rel
        else:
            # Fallback to first non-empty snippet
            for e in els:
                raw = e.inner_text().strip()
                if raw:
                    ts_text = raw
                    post_date = ts_text
                    break
        m_act = re.search(r"(reposted|shared|commented|liked)", text, flags=re.I)
        if m_act:
            action = m_act.group(1).lower()
    except Exception:
        pass
    return post_date, action, ts_text


def iso_from_linkedin_id(id_str: str) -> Optional[str]:
    try:
        n = int(id_str)
        b = bin(n)[2:]
        if len(b) < 41:
            return None
        first41 = b[:41]
        ms = int(first41, 2)
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return None


def extract_iso_from_posturl(post_url: Optional[str]) -> Optional[str]:
    if not post_url:
        return None
    try:
        # Find a 19-digit number (activity id) in the URL
        m = re.search(r"(\d{19})", post_url)
        if not m:
            return None
        return iso_from_linkedin_id(m.group(1))
    except Exception:
        return None


def find_all_profile_links(card) -> List[str]:
    urls: List[str] = []
    try:
        links = card.query_selector_all("a[href*='/in/']")
        for l in links:
            href = l.get_attribute("href")
            if not href:
                continue
            # Clean using same clean logic as extract_links
            try:
                parts = urlsplit(href)
                if not parts.scheme and not parts.netloc and parts.path.startswith("/"):
                    href = "https://www.linkedin.com" + parts.path
                else:
                    href = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            except Exception:
                pass
            if href not in urls:
                urls.append(href)
    except Exception:
        pass
    return urls


def extract_author_for_url(card, profile_url: str) -> Optional[str]:
    # Try to find a title near the specific profile link
    try:
        # Find link element matching path
        path = urlsplit(profile_url).path
        link = card.query_selector(f"a[href*='{path}']")
        if not link:
            return None
        # Look upwards for a nearby actor title
        container = link
        for _ in range(4):
            container = container.evaluate_handle("el => el.parentElement")  # type: ignore
            if not container:
                break
            try:
                title = container.query_selector("span.update-components-actor__title span[dir='ltr'], span.feed-shared-actor__title span[dir='ltr']")
                if title:
                    t = title.inner_text().strip()
                    if t:
                        t = t.split("\n")[0].strip()
                        t = re.sub(r"\s*•\s*.*$", "", t).strip()
                        return t
            except Exception:
                continue
    except Exception:
        pass
    return None


def scrape_user(page, username: str):
    open_user_posts(page, username)
    scroll_to_end(page)
    items = []
    card_html_snippets: List[str] = []
    scraped_profile_url = f"https://www.linkedin.com/in/{username}"
    seen_urns = set()
    for c in find_cards(page):
        urn_val = c.get_attribute("data-urn") or ""
        if urn_val.startswith("urn:li:activity:"):
            if urn_val in seen_urns:
                continue
            seen_urns.add(urn_val)
        try:
            html = c.evaluate("el => el.outerHTML")
            if html:
                card_html_snippets.append(html)
        except Exception:
            pass
        postUrl, authorUrl, sharedPostUrl, sharedJobUrl = extract_links(c)
        author = extract_author(c)
        postContent = extract_text(c)
        imgUrl, videoUrl = extract_media(c)
        likeCount, commentCount, repostCount, viewCount = extract_counts(c)
        postDate, action, timestampText = extract_date_and_action(c)
        # Derive action:
        # - If LinkedIn text said "reposted/shared" keep that as repost
        # - Else, only treat as repost when a shared post URL exists
        # - Job shares (sharedJobUrl only) are treated as original posts
        derived_action = action
        if not derived_action:
            if sharedPostUrl:
                derived_action = "repost"
            else:
                derived_action = "post"
        # If there are other profile links inside card (not the scraped one), treat as repost
        try:
            all_profiles = find_all_profile_links(c)
            other_profiles = [u for u in all_profiles if not u.startswith(scraped_profile_url)]
            if other_profiles:
                derived_action = "repost"
                authorUrl = other_profiles[0]
                # Try to extract the original author's display name
                name_for_url = extract_author_for_url(c, authorUrl)
                if name_for_url:
                    author = name_for_url
        except Exception:
            pass
        iso_ts = extract_iso_from_posturl(postUrl)
        items.append({
            "postUrl": postUrl,
            "sharedJobUrl": sharedJobUrl,
            "imgUrl": imgUrl,
            "postContent": postContent,
            "type": detect_type(c),
            "likeCount": likeCount,
            "commentCount": commentCount,
            "repostCount": repostCount,
            "postDate": postDate,
            "action": derived_action,
            "author": author,
            "authorUrl": authorUrl,
            "profileUrl": scraped_profile_url,
            "postTimestamp": iso_ts,
            "videoUrl": videoUrl,
            "sharedPostUrl": sharedPostUrl,
        })
    # Save raw card HTML for analysis
    # The following code block saves the raw LinkedIn card HTML snippets for analysis.
    # try:
    #     with open(f"{username}.cards.html", "w", encoding="utf-8") as f:
    #         f.write("<!-- Saved LinkedIn card HTML snippets for analysis -->\n")
    #         for i, html in enumerate(card_html_snippets, start=1):
    #             f.write(f"\n<!-- CARD {i} -->\n")
    #             f.write(html)
    #             f.write("\n")
    # except Exception:
    #     pass
    return items


def main():
    parser = argparse.ArgumentParser(description="Simple LinkedIn posts scraper")
    parser.add_argument("--headless", action="store_true", help="Run headless (overrides HEADLESS)")
    args = parser.parse_args()

    if not EMAIL or not PASSWORD:
        raise SystemExit("Please set EMAIL and PASSWORD at the top of scraper.py")
    if not USERS:
        raise SystemExit("Please add at least one username to USERS at the top of scraper.py")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=(args.headless or HEADLESS))
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        login(page, EMAIL, PASSWORD)
        for u in USERS:
            print(f"Scraping {u} ...")
            data = scrape_user(page, u)
            with open(f"{u}.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"Wrote {len(data)} posts to {u}.json")
        browser.close()


if __name__ == "__main__":
    main()



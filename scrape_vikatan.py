import os
import sys
import time
import re
import urllib.parse
from datetime import datetime
import email.utils
import xml.dom.minidom as minidom
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.vikatan.com"
TARGET_PAGE_URL = "https://www.vikatan.com/juniorvikatan?pfrom=header-submenu"
SMRY_API_ENDPOINT = "https://r.smry.ai/api/v1/read"
OUTPUT_FILE = "feed.xml"
MAX_ITEMS = 100

# Pattern matching edition date slugs (e.g., 08-nov-2026, 2026-nov-08, 08-11-2026)
DATE_PATTERN = re.compile(
    r'^(?:\d{1,2}-[a-zA-Z]{3,9}-\d{4}|\d{4}-[a-zA-Z]{3,9}-\d{1,2}|\d{1,2}-\d{1,2}-\d{4})$',
    re.IGNORECASE
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def clean_url(url):
    """Strips query strings and trailing slashes for reliable GUIDs."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))

def get_text_content(element):
    """Safely extracts text or CDATA node contents."""
    return "".join(
        c.nodeValue for c in element.childNodes 
        if c.nodeType in (c.TEXT_NODE, c.CDATA_SECTION_NODE)
    ).strip()

def get_item_identifier(item):
    """Extracts unique GUID or link from an existing item."""
    for tag in ("guid", "link"):
        nodes = item.getElementsByTagName(tag)
        if nodes:
            val = get_text_content(nodes[0])
            if val:
                return clean_url(val)
    return None

def is_issue_index_link(clean_url_str):
    """
    Identifies issue cover indices like /juniorvikatan/08-nov-2026 (only 2 segments).
    Returns False for actual articles (e.g. /juniorvikatan/08-nov-2026/mister-kazhugar).
    """
    parsed = urllib.parse.urlparse(clean_url_str)
    segments = [s for s in parsed.path.strip("/").split("/") if s]
    if len(segments) == 2 and segments[0].lower() == "juniorvikatan" and DATE_PATTERN.match(segments[1]):
        return True
    return False

def fetch_articles_from_page():
    """Scrapes all valid article links directly from the target page."""
    try:
        response = requests.get(TARGET_PAGE_URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"Error loading target page: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    # Decompose only global site navigation and footers
    for tag in soup.find_all(["nav", "footer"]):
        tag.decompose()

    ignore_patterns = [
        "/tag", "/tags", "/author", "/authors", "/topic", "/topics",
        "/subscribe", "/login", "/register", "/terms", "/privacy",
        "/about-us", "/contact-us", "/newsletter", "/epaper"
    ]

    article_links = []
    seen = set()

    for a in soup.find_all("a", href=True):
        raw_href = a["href"].split("?")[0].strip()
        if not raw_href or raw_href.startswith("#") or raw_href.startswith("javascript:"):
            continue

        full_url = urllib.parse.urljoin(BASE_URL, raw_href)
        clean = clean_url(full_url)
        parsed = urllib.parse.urlparse(clean)

        if "vikatan.com" not in parsed.netloc:
            continue

        # Skip landing page roots
        if clean in (BASE_URL, f"{BASE_URL}/juniorvikatan", clean_url(TARGET_PAGE_URL)):
            continue

        path = parsed.path.lower()
        if any(pat in path for pat in ignore_patterns):
            continue

        # Only ignore the pure issue edition cover index (e.g. /juniorvikatan/08-nov-2026)
        if is_issue_index_link(clean):
            continue

        segments = [s for s in parsed.path.strip("/").split("/") if s]
        if len(segments) >= 2 and clean not in seen:
            seen.add(clean)
            article_links.append(clean)

    return article_links

def clean_and_format_content(text):
    """
    Formats SMRY markdown:
    - Removes bracketed promotional links [text](url).
    - Ensures subheadings are wrapped in explicit <h4><strong> tags.
    - Preserves consistent 1.25em bottom margins on all paragraphs and indented dash items.
    """
    if not text:
        return ""

    # 1. Remove nested image links like [![img](url)](url)
    text = re.sub(r'\[\s*!\[[^\]]*\]\([^\)]+\)\s*\](?:\([^\)]+\)|https?://\S+)?', '', text)

    # 2. Remove all [text] followed by a URL (e.g. [text](url), [text] url, [text]: url)
    link_pattern = re.compile(
        r'#{0,6}\s*!?\[[^\]]*\]\s*:?\s*(?:\([^\)]+\)|<[^>]+>|https?://\S+)', 
        re.IGNORECASE
    )
    prev = None
    while prev != text:
        prev = text
        text = link_pattern.sub('', text)

    # 3. Clean leftover brackets and bare URLs on their own lines
    text = re.sub(r'\[\s*\](?:\([^\)]*\)|<[^>]+>)?', '', text)
    text = re.sub(r'^\s*https?://\S+\s*$', '', text, flags=re.MULTILINE)

    # 4. Clean standalone hash lines
    cleaned_lines = []
    for line in text.split('\n'):
        if re.match(r'^#{1,6}\s*$', line.strip()):
            continue
        cleaned_lines.append(line)
    text = '\n'.join(cleaned_lines)

    # 5. Global Markdown bold & italics conversion BEFORE line splitting
    text = re.sub(r'\*\*\s*([\s\S]+?)\s*\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__\s*([\s\S]+?)\s*__', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)\s*([^\*\n]+?)\s*(?<!\*)\*(?!\*)', r'<em>\1</em>', text)

    # 6. Process line-by-line
    raw_lines = [line.strip() for line in text.split('\n') if line.strip()]

    html_out = []
    for line in raw_lines:
        # Check for Markdown headings (#, ##, ###)
        h_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if h_match:
            heading_content = h_match.group(2).strip()
            clean_heading = re.sub(r'</?strong>', '', heading_content)
            clean_heading = re.sub(r'[*_#]', '', clean_heading).strip()
            html_out.append(
                f'<h4 style="margin: 1.5em 0 0.8em 0; font-weight: bold; font-size: 1.15em; line-height: 1.5; color: #111;">'
                f'<strong>{clean_heading}</strong>'
                f'</h4>'
            )
            continue

        # Check for standalone bold subheading lines
        if re.match(r'^<strong>[^<]+</strong>$', line):
            inner = re.sub(r'</?strong>', '', line).strip()
            if len(inner) < 70 and not inner.endswith('.'):
                html_out.append(
                    f'<h4 style="margin: 1.5em 0 0.8em 0; font-weight: bold; font-size: 1.15em; line-height: 1.5; color: #111;">'
                    f'<strong>{inner}</strong>'
                    f'</h4>'
                )
                continue

        # Indented dash/bullet lines (-, *, •, –, —) with consistent bottom spacing
        if re.match(r'^[-*•–—]\s+', line):
            html_out.append(
                f'<p style="margin: 0 0 1.25em 1.5em; text-indent: -1.2em; line-height: 1.85; font-size: 1.05em; color: #222;">'
                f'{line}'
                f'</p>'
            )
            continue

        # Bold common Tamil interview/dialogue prefixes
        dialogue_pattern = r'^(?!\s*<strong[ >])((?:கேள்வி|பதில்|கழுகார் பதில்|கழுகார்|செய்தியாளர்)\s*[:：])\s*'
        line = re.sub(dialogue_pattern, r'<strong>\1</strong> ', line)

        # Standard paragraph
        html_out.append(f'<p style="margin: 0 0 1.25em 0; line-height: 1.85; font-size: 1.05em; color: #222;">{line}</p>')

    return "".join(html_out)

def fetch_content_from_smry(article_url):
    """Queries SMRY reader with nominal pacing, handling 202 status and fallbacks."""
    time.sleep(3.5)

    api_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json"
    }

    try:
        resp = requests.get(SMRY_API_ENDPOINT, params={"url": article_url}, headers=api_headers, timeout=20)
        if resp.status_code == 202:
            time.sleep(4)
            resp = requests.get(SMRY_API_ENDPOINT, params={"url": article_url}, headers=api_headers, timeout=20)

        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content") or data.get("text") or data.get("markdown") or ""
            if isinstance(content, str) and len(content.strip()) > 100:
                return clean_and_format_content(content)
    except Exception as e:
        print(f"SMRY API attempt failed: {e}")

    try:
        prefix_url = f"https://r.smry.ai/{article_url}"
        alt_resp = requests.get(prefix_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=20)
        if alt_resp.status_code == 200 and len(alt_resp.text.strip()) > 150:
            return clean_and_format_content(alt_resp.text)
    except Exception as e:
        print(f"SMRY prefix attempt failed: {e}")

    return None

def parse_article_details(url):
    """Fetches article metadata and full text with local preview fallback."""
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return None
    except Exception as e:
        print(f"Failed to fetch article {url}: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(res.text, "html.parser")

    title_meta = soup.find("meta", property="og:title")
    title = title_meta["content"].strip() if title_meta and title_meta.get("content") else ""
    if not title and soup.title:
        title = soup.title.string.strip()

    desc_meta = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    summary = desc_meta["content"].strip() if desc_meta and desc_meta.get("content") else ""

    img_meta = soup.find("meta", property="og:image")
    image_url = img_meta["content"].strip() if img_meta and img_meta.get("content") else ""

    date_meta = soup.find("meta", property="article:published_time")
    pub_date_str = email.utils.formatdate(usegmt=True)
    if date_meta and date_meta.get("content"):
        try:
            dt = datetime.fromisoformat(date_meta["content"].replace("Z", "+00:00"))
            pub_date_str = email.utils.format_datetime(dt)
        except Exception:
            pass

    section_meta = soup.find("meta", property="article:section")
    category_name = section_meta["content"].strip() if section_meta and section_meta.get("content") else "Junior Vikatan"

    print(f"Fetching SMRY text for: {url}")
    body_html = fetch_content_from_smry(url)

    if not body_html:
        paragraphs = []
        for p in soup.find_all("p"):
            parent_class = " ".join(p.parent.get("class", []))
            if any(term in parent_class.lower() for term in ("paywall", "subscribe", "footer", "modal")):
                break
            text = p.get_text().strip()
            if len(text) > 30:
                paragraphs.append(f'<p style="margin: 0 0 1.25em 0; line-height: 1.85;">{text}</p>')
            if len(paragraphs) >= 3:
                break
        body_html = "".join(paragraphs)

    return {
        "title": title,
        "link": clean_url(url),
        "summary": summary,
        "image_url": image_url,
        "pub_date": pub_date_str,
        "category": category_name,
        "body_html": body_html,
        "smry_link": f"https://smry.ai/{clean_url(url)}"
    }

def create_item_node(dom, article):
    """Creates an RSS 2.0 item with both <description> and rich <content:encoded>."""
    item = dom.createElement("item")

    title_elem = dom.createElement("title")
    title_elem.appendChild(dom.createCDATASection(article["title"]))
    item.appendChild(title_elem)

    link_elem = dom.createElement("link")
    link_elem.appendChild(dom.createTextNode(article["link"]))
    item.appendChild(link_elem)

    guid_elem = dom.createElement("guid")
    guid_elem.setAttribute("isPermaLink", "true")
    guid_elem.appendChild(dom.createTextNode(article["link"]))
    item.appendChild(guid_elem)

    author_elem = dom.createElement("dc:creator")
    author_elem.appendChild(dom.createCDATASection("ஜூனியர் விகடன் (Junior Vikatan)"))
    item.appendChild(author_elem)

    pub_elem = dom.createElement("pubDate")
    pub_elem.appendChild(dom.createTextNode(article["pub_date"]))
    item.appendChild(pub_elem)

    cat_elem = dom.createElement("category")
    cat_elem.appendChild(dom.createCDATASection(article["category"]))
    item.appendChild(cat_elem)

    if article["image_url"]:
        enclosure = dom.createElement("enclosure")
        enclosure.setAttribute("url", article["image_url"])
        enclosure.setAttribute("type", "image/jpeg")
        enclosure.setAttribute("length", "0")
        item.appendChild(enclosure)

    full_html = ""
    if article["image_url"]:
        full_html += f'<p style="margin-bottom: 1.25em;"><img src="{article["image_url"]}" style="max-width:100%; height:auto; border-radius: 6px;" /></p>'
    if article["summary"]:
        full_html += f'<p style="margin-bottom: 1.25em; font-size: 1.1em; color: #444;"><strong>{article["summary"]}</strong></p><hr style="border:0; border-top: 1px solid #eee; margin: 1.5em 0;"/>'
    if article["body_html"]:
        full_html += article["body_html"]
    full_html += f'<p style="margin-top: 2em; padding-top: 1em; border-top: 1px solid #eee;"><a href="{article["smry_link"]}" target="_blank" style="color: #0066cc; text-decoration: underline;">📖 Open in SMRY Reader</a></p>'

    desc_elem = dom.createElement("description")
    desc_summary = article["summary"] if article["summary"] else article["title"]
    desc_elem.appendChild(dom.createCDATASection(f"<p>{desc_summary}</p>"))
    item.appendChild(desc_elem)

    content_encoded_elem = dom.createElement("content:encoded")
    content_encoded_elem.appendChild(dom.createCDATASection(full_html))
    item.appendChild(content_encoded_elem)

    return item

def initialize_empty_feed():
    impl = minidom.getDOMImplementation()
    dom = impl.createDocument(None, "rss", None)
    rss = dom.documentElement
    rss.setAttribute("version", "2.0")
    rss.setAttribute("xmlns:content", "http://purl.org/rss/1.0/modules/content/")
    rss.setAttribute("xmlns:dc", "http://purl.org/dc/elements/1.1/")

    channel = dom.createElement("channel")
    rss.appendChild(channel)

    title = dom.createElement("title")
    title.appendChild(dom.createTextNode("Junior Vikatan Feed"))
    channel.appendChild(title)

    link = dom.createElement("link")
    link.appendChild(dom.createTextNode(TARGET_PAGE_URL))
    channel.appendChild(link)

    desc = dom.createElement("description")
    desc.appendChild(dom.createTextNode("Stories directly from Junior Vikatan"))
    channel.appendChild(desc)

    return dom, channel

def main():
    if os.path.exists(OUTPUT_FILE) and os.path.getsize(OUTPUT_FILE) > 0:
        try:
            dom = minidom.parse(OUTPUT_FILE)
            channel = dom.getElementsByTagName("channel")[0]
        except Exception:
            dom, channel = initialize_empty_feed()
    else:
        dom, channel = initialize_empty_feed()

    existing_items = list(channel.getElementsByTagName("item"))
    existing_ids = {get_item_identifier(it) for it in existing_items if get_item_identifier(it)}

    candidate_links = fetch_articles_from_page()
    print(f"Total stories found on target page: {len(candidate_links)}")

    new_links = [l for l in candidate_links if clean_url(l) not in existing_ids]
    print(f"New articles to add: {len(new_links)}")

    if not new_links:
        print("No new articles found. Exiting.")
        return

    new_item_nodes = []
    for link in new_links:
        article_data = parse_article_details(link)
        if article_data and article_data["title"]:
            item_node = create_item_node(dom, article_data)
            new_item_nodes.append(item_node)

    first_existing = existing_items[0] if existing_items else None
    for item_node in new_item_nodes:
        if first_existing:
            channel.insertBefore(item_node, first_existing)
            channel.insertBefore(dom.createTextNode("\n    "), first_existing)
        else:
            channel.appendChild(dom.createTextNode("\n    "))
            channel.appendChild(item_node)

    all_items = list(channel.getElementsByTagName("item"))
    if len(all_items) > MAX_ITEMS:
        for excess in all_items[MAX_ITEMS:]:
            prev = excess.previousSibling
            if prev and prev.nodeType == prev.TEXT_NODE and prev.nodeValue.strip() == "":
                channel.removeChild(prev)
            channel.removeChild(excess)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(dom.toxml(encoding="utf-8"))

    print(f"Successfully processed {len(new_item_nodes)} articles. Feed saved to {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()

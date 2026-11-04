import os
import sys
import time
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

def fetch_articles_from_page():
    """Scrapes only the article cards displayed on the Junior Vikatan landing page."""
    try:
        response = requests.get(TARGET_PAGE_URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"Error loading target page: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    # Decompose global header, navigation bar, and footer to prevent crawling site-wide menus
    for tag in soup(["header", "nav", "footer"]):
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

        # Must be internal to vikatan.com
        if "vikatan.com" not in parsed.netloc:
            continue

        # Skip landing page itself
        if clean in (BASE_URL, f"{BASE_URL}/juniorvikatan", clean_url(TARGET_PAGE_URL)):
            continue

        # Skip administrative/utility pages
        path = parsed.path.lower()
        if any(pat in path for pat in ignore_patterns):
            continue

        # Valid articles have at least 2 path segments (e.g. /category/story-slug)
        segments = [s for s in parsed.path.strip("/").split("/") if s]
        if len(segments) >= 2 and clean not in seen:
            seen.add(clean)
            article_links.append(clean)

    return article_links

def fetch_content_from_smry(article_url):
    """Queries SMRY reader with pacing, handling 202 status and fallbacks."""
    time.sleep(3.5)  # Stay strictly within SMRY burst limits

    api_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json"
    }

    # Method 1: SMRY JSON API
    try:
        resp = requests.get(SMRY_API_ENDPOINT, params={"url": article_url}, headers=api_headers, timeout=20)
        if resp.status_code == 202:
            time.sleep(4)
            resp = requests.get(SMRY_API_ENDPOINT, params={"url": article_url}, headers=api_headers, timeout=20)

        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content") or data.get("text") or data.get("markdown") or ""
            if isinstance(content, str) and len(content.strip()) > 100:
                return format_raw_paragraphs(content)
    except Exception as e:
        print(f"SMRY API attempt failed: {e}")

    # Method 2: SMRY Reader Web Endpoint Fallback
    try:
        prefix_url = f"https://r.smry.ai/{article_url}"
        alt_resp = requests.get(prefix_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=20)
        if alt_resp.status_code == 200 and len(alt_resp.text.strip()) > 150:
            return format_raw_paragraphs(alt_resp.text)
    except Exception as e:
        print(f"SMRY prefix attempt failed: {e}")

    return None

def format_raw_paragraphs(text):
    """Converts raw text lines into clean HTML paragraph markup."""
    blocks = [b.strip() for b in text.split("\n\n") if len(b.strip()) > 20]
    html_out = []
    for block in blocks:
        if block.startswith("#"):
            html_out.append(f"<h4>{block.lstrip('#').strip()}</h4>")
        else:
            html_out.append(f"<p>{block}</p>")
    return "".join(html_out)

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

    # 1. Headline
    title_meta = soup.find("meta", property="og:title")
    title = title_meta["content"].strip() if title_meta and title_meta.get("content") else ""
    if not title and soup.title:
        title = soup.title.string.strip()

    # 2. Summary
    desc_meta = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    summary = desc_meta["content"].strip() if desc_meta and desc_meta.get("content") else ""

    # 3. Image
    img_meta = soup.find("meta", property="og:image")
    image_url = img_meta["content"].strip() if img_meta and img_meta.get("content") else ""

    # 4. Publication Date
    date_meta = soup.find("meta", property="article:published_time")
    pub_date_str = email.utils.formatdate(usegmt=True)
    if date_meta and date_meta.get("content"):
        try:
            dt = datetime.fromisoformat(date_meta["content"].replace("Z", "+00:00"))
            pub_date_str = email.utils.format_datetime(dt)
        except Exception:
            pass

    # 5. Category
    section_meta = soup.find("meta", property="article:section")
    category_name = section_meta["content"].strip() if section_meta and section_meta.get("content") else "Junior Vikatan"

    # 6. SMRY Content
    print(f"Fetching SMRY text for: {url}")
    body_html = fetch_content_from_smry(url)

    # 7. Local preview fallback if SMRY returns empty
    if not body_html:
        paragraphs = []
        for p in soup.find_all("p"):
            parent_class = " ".join(p.parent.get("class", []))
            if any(term in parent_class.lower() for term in ("paywall", "subscribe", "footer", "modal")):
                break
            text = p.get_text().strip()
            if len(text) > 30:
                paragraphs.append(f"<p>{text}</p>")
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
    """Creates a standard RSS 2.0 item node."""
    item = dom.createElement("item")

    # Title
    title_elem = dom.createElement("title")
    title_elem.appendChild(dom.createCDATASection(article["title"]))
    item.appendChild(title_elem)

    # Link & GUID
    link_elem = dom.createElement("link")
    link_elem.appendChild(dom.createTextNode(article["link"]))
    item.appendChild(link_elem)

    guid_elem = dom.createElement("guid")
    guid_elem.setAttribute("isPermaLink", "true")
    guid_elem.appendChild(dom.createTextNode(article["link"]))
    item.appendChild(guid_elem)

    # Creator
    author_elem = dom.createElement("dc:creator")
    author_elem.appendChild(dom.createCDATASection("ஜூனியர் விகடன் (Junior Vikatan)"))
    item.appendChild(author_elem)

    # PubDate
    pub_elem = dom.createElement("pubDate")
    pub_elem.appendChild(dom.createTextNode(article["pub_date"]))
    item.appendChild(pub_elem)

    # Category
    cat_elem = dom.createElement("category")
    cat_elem.appendChild(dom.createCDATASection(article["category"]))
    item.appendChild(cat_elem)

    # Enclosure Image
    if article["image_url"]:
        enclosure = dom.createElement("enclosure")
        enclosure.setAttribute("url", article["image_url"])
        enclosure.setAttribute("type", "image/jpeg")
        enclosure.setAttribute("length", "0")
        item.appendChild(enclosure)

    # Description
    desc_content = ""
    if article["image_url"]:
        desc_content += f'<p><img src="{article["image_url"]}" style="max-width:100%;" /></p>'
    if article["summary"]:
        desc_content += f'<p><strong>சுருக்கம்: {article["summary"]}</strong></p><hr/>'
    if article["body_html"]:
        desc_content += article["body_html"]
    desc_content += f'<p><br/><a href="{article["smry_link"]}" target="_blank">📖 Read Full Article on SMRY</a></p>'

    desc_elem = dom.createElement("description")
    desc_elem.appendChild(dom.createCDATASection(desc_content))
    item.appendChild(desc_elem)

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

    # Scrape only the single page
    candidate_links = fetch_articles_from_page()
    print(f"Total stories found on Junior Vikatan page: {len(candidate_links)}")

    new_links = [l for l in candidate_links if clean_url(l) not in existing_ids]
    print(f"New stories to add: {len(new_links)}")

    if not new_links:
        print("No new stories found on the page. Exiting.")
        return

    new_item_nodes = []
    for link in new_links:
        article_data = parse_article_details(link)
        if article_data and article_data["title"]:
            item_node = create_item_node(dom, article_data)
            new_item_nodes.append(item_node)

    # Insert newest items at the top
    first_existing = existing_items[0] if existing_items else None
    for item_node in new_item_nodes:
        if first_existing:
            channel.insertBefore(item_node, first_existing)
            channel.insertBefore(dom.createTextNode("\n    "), first_existing)
        else:
            channel.appendChild(dom.createTextNode("\n    "))
            channel.appendChild(item_node)

    # Maintain 100 maximum entries
    all_items = list(channel.getElementsByTagName("item"))
    if len(all_items) > MAX_ITEMS:
        for excess in all_items[MAX_ITEMS:]:
            prev = excess.previousSibling
            if prev and prev.nodeType == prev.TEXT_NODE and prev.nodeValue.strip() == "":
                channel.removeChild(prev)
            channel.removeChild(excess)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(dom.toxml(encoding="utf-8"))

    print(f"Successfully processed {len(new_item_nodes)} new articles. Feed saved to {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()

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
MAIN_MENU_URL = "https://www.vikatan.com/juniorvikatan?pfrom=header-submenu"
SMRY_API_ENDPOINT = "https://r.smry.ai/api/v1/read"
OUTPUT_FILE = "feed.xml"
MAX_ITEMS = 100

# Hoverable category link texts
HOVER_CATEGORIES = [
    "கழுகார்",
    "அரசியல்",
    "தொடர்கள்",
    "அலசல்",
    "க்ரைம்",
    "சமூகம்"
]

# Non-link section label
COVER_STORY_LABEL = "கவர் ஸ்டோரி"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def clean_url(url):
    """Strips tracking query strings and trailing slashes for stable identifiers."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))

def get_text_content(element):
    """Safely extracts text or CDATA node contents."""
    return "".join(
        c.nodeValue for c in element.childNodes 
        if c.nodeType in (c.TEXT_NODE, c.CDATA_SECTION_NODE)
    ).strip()

def get_item_identifier(item):
    """Extracts unique GUID or Link from an XML item."""
    for tag in ("guid", "link"):
        nodes = item.getElementsByTagName(tag)
        if nodes:
            val = get_text_content(nodes[0])
            if val:
                return clean_url(val)
    return None

def fetch_cover_story_links(soup):
    """Extracts article links under the non-hoverable 'கவர் ஸ்டோரி' label."""
    cover_links = set()
    
    # Locate elements containing 'கவர் ஸ்டோரி'
    label_nodes = soup.find_all(lambda el: el.string and COVER_STORY_LABEL in el.string.strip())
    if not label_nodes:
        # Fallback to general text search if direct string doesn't match
        label_nodes = [el for el in soup.find_all(["h2", "h3", "h4", "div", "span", "p"]) if COVER_STORY_LABEL in el.get_text()]

    for node in label_nodes:
        # Check within parent card/container
        parent = node.find_parent(["div", "section", "article"])
        if parent:
            for a in parent.find_all("a", href=True):
                href = a["href"].split("?")[0].strip()
                full_url = urllib.parse.urljoin(BASE_URL, href)
                if len(urllib.parse.urlparse(full_url).path.strip("/").split("/")) >= 2:
                    cover_links.add(full_url)
                    
        # Also check immediately following sibling elements
        sib = node.find_next_sibling()
        if sib:
            for a in sib.find_all("a", href=True):
                href = a["href"].split("?")[0].strip()
                full_url = urllib.parse.urljoin(BASE_URL, href)
                if len(urllib.parse.urlparse(full_url).path.strip("/").split("/")) >= 2:
                    cover_links.add(full_url)

    print(f"Discovered {len(cover_links)} article link(s) under '{COVER_STORY_LABEL}'.")
    return cover_links

def fetch_all_candidate_links():
    """Scrapes category URLs, hover menus, and 'கவர் ஸ்டோரி'."""
    try:
        response = requests.get(MAIN_MENU_URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"Error loading main page: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    category_links = set()

    # 1. Collect URLs for the 6 hover categories
    for a in soup.find_all("a", href=True):
        text = a.get_text().strip()
        if any(target in text for target in HOVER_CATEGORIES):
            href = a["href"].split("?")[0].strip()
            category_links.add(urllib.parse.urljoin(BASE_URL, href))

    # 2. Collect Cover Story links from the page
    candidate_articles = set(fetch_cover_story_links(soup))

    # 3. Visit each hover category to gather its article links
    for cat_url in category_links:
        print(f"Scanning category: {cat_url}")
        try:
            res = requests.get(cat_url, headers=HEADERS, timeout=15)
            if res.status_code == 200:
                cat_soup = BeautifulSoup(res.text, "html.parser")
                for a in cat_soup.find_all("a", href=True):
                    href = a["href"].split("?")[0].strip()
                    full_url = urllib.parse.urljoin(BASE_URL, href)
                    if full_url != cat_url and full_url != BASE_URL:
                        if len(urllib.parse.urlparse(full_url).path.strip("/").split("/")) >= 2:
                            candidate_articles.add(full_url)
        except Exception as e:
            print(f"Error reading category {cat_url}: {e}")
        time.sleep(1.5)  # Nominal delay between section pages

    return list(candidate_articles)

def fetch_content_from_smry(article_url):
    """
    Fetches full article content from SMRY with:
    - 4-second nominal pacing
    - HTTP 202 retry handling
    - Prefix-reader fallback
    """
    time.sleep(4)  # Nominal delay to strictly stay under the 5 req/10s burst limit

    api_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json"
    }

    # Attempt 1: SMRY JSON API
    try:
        resp = requests.get(SMRY_API_ENDPOINT, params={"url": article_url}, headers=api_headers, timeout=25)
        
        # If SMRY is compiling an uncached page (HTTP 202 Accepted), wait and retry once
        if resp.status_code == 202:
            print("SMRY compiling uncached article, waiting 5 seconds...")
            time.sleep(5)
            resp = requests.get(SMRY_API_ENDPOINT, params={"url": article_url}, headers=api_headers, timeout=25)

        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content") or data.get("text") or data.get("markdown") or ""
            if isinstance(content, str) and len(content.strip()) > 100:
                return format_raw_paragraphs(content)
        else:
            print(f"SMRY API returned status {resp.status_code} for {article_url}")

    except Exception as e:
        print(f"SMRY API request error: {e}")

    # Attempt 2: Direct Prefix Reader Fallback (https://r.smry.ai/<url>)
    try:
        prefix_url = f"https://r.smry.ai/{article_url}"
        alt_resp = requests.get(prefix_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=20)
        if alt_resp.status_code == 200 and len(alt_resp.text.strip()) > 150:
            return format_raw_paragraphs(alt_resp.text)
    except Exception as e:
        print(f"Prefix reader fallback error: {e}")

    return None

def format_raw_paragraphs(text):
    """Formats text blocks into clean HTML paragraph markup."""
    blocks = [b.strip() for b in text.split("\n\n") if len(b.strip()) > 20]
    html_out = []
    for block in blocks:
        if block.startswith("#"):
            clean_head = block.lstrip("#").strip()
            html_out.append(f"<h4>{clean_head}</h4>")
        else:
            html_out.append(f"<p>{block}</p>")
    return "".join(html_out)

def parse_article_details(url):
    """Scrapes headline, summary, featured image, and invokes SMRY with fallback."""
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return None
    except Exception as e:
        print(f"Failed to fetch {url}: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(res.text, "html.parser")

    # 1. Title
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

    # 6. Full Text via SMRY
    print(f"Fetching SMRY content for: {url}")
    body_html = fetch_content_from_smry(url)

    # 7. Local preview fallback if SMRY returns no text
    if not body_html:
        print("SMRY returned empty/blocked. Using available local preview text.")
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

    # Image Enclosure
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
    link.appendChild(dom.createTextNode(MAIN_MENU_URL))
    channel.appendChild(link)

    desc = dom.createElement("description")
    desc.appendChild(dom.createTextNode("Junior Vikatan Cover Story and Featured Categories"))
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

    candidate_links = fetch_all_candidate_links()
    print(f"Total discovered candidate links: {len(candidate_links)}")

    new_links = [l for l in candidate_links if clean_url(l) not in existing_ids]
    print(f"New articles to process: {len(new_links)}")

    if not new_links:
        print("No new articles found. Exiting.")
        return

    new_item_nodes = []
    for link in new_links:
        article_data = parse_article_details(link)
        if article_data and article_data["title"]:
            item_node = create_item_node(dom, article_data)
            new_item_nodes.append(item_node)

    # Insert newest articles at the top
    first_existing = existing_items[0] if existing_items else None
    for item_node in new_item_nodes:
        if first_existing:
            channel.insertBefore(item_node, first_existing)
            channel.insertBefore(dom.createTextNode("\n    "), first_existing)
        else:
            channel.appendChild(dom.createTextNode("\n    "))
            channel.appendChild(item_node)

    # Maintain 100-item maximum
    all_items = list(channel.getElementsByTagName("item"))
    if len(all_items) > MAX_ITEMS:
        for excess in all_items[MAX_ITEMS:]:
            prev = excess.previousSibling
            if prev and prev.nodeType == prev.TEXT_NODE and prev.nodeValue.strip() == "":
                channel.removeChild(prev)
            channel.removeChild(excess)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(dom.toxml(encoding="utf-8"))

    print(f"Successfully added {len(new_item_nodes)} new articles to {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()

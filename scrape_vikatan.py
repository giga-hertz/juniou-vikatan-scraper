import os
import sys
import re
import urllib.parse
from datetime import datetime
import email.utils
import xml.dom.minidom as minidom
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.vikatan.com"
LISTING_URL = "https://www.vikatan.com/juniorvikatan?pfrom=header-submenu"
TARGET_SECTION = "government-and-politics"
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

def get_text_content(element):
    """Safely extracts text or CDATA content from an XML element."""
    return "".join(
        c.nodeValue for c in element.childNodes 
        if c.nodeType in (c.TEXT_NODE, c.CDATA_SECTION_NODE)
    ).strip()

def get_item_identifier(item):
    """Returns a unique identifier using <guid> or <link>."""
    for tag in ("guid", "link"):
        nodes = item.getElementsByTagName(tag)
        if nodes:
            val = get_text_content(nodes[0])
            if val:
                return val
    return None

def fetch_article_links():
    """Scrapes links from the listing page, focusing on government-and-politics stories."""
    try:
        response = requests.get(LISTING_URL, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"Error loading listing page: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    candidate_links = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].strip()
        if not href:
            continue

        # Look for links targeting government-and-politics or juniorvikatan articles
        if TARGET_SECTION in href or "/juniorvikatan/" in href:
            full_url = urllib.parse.urljoin(BASE_URL, href)
            path_segments = [p for p in urllib.parse.urlparse(full_url).path.split("/") if p]

            # Exclude landing pages, category indexes, or author tags
            if full_url in (LISTING_URL, f"{BASE_URL}/juniorvikatan", f"{BASE_URL}/{TARGET_SECTION}"):
                continue

            # Valid article paths typically have at least 2 path segments (e.g., /government-and-politics/slug)
            if len(path_segments) >= 2:
                candidate_links.add(full_url)

    return list(candidate_links)

def fetch_content_from_smry(article_url):
    """Queries SMRY's reader API to fetch parsed article text."""
    try:
        params = {"url": article_url}
        resp = requests.get(SMRY_API_ENDPOINT, params=params, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content") or data.get("text") or data.get("markdown") or ""
            if isinstance(content, str) and len(content.strip()) > 100:
                paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
                html_paras = []
                for p in paragraphs:
                    if p.startswith("#"):
                        html_paras.append(f"<h4>{p.lstrip('#').strip()}</h4>")
                    else:
                        html_paras.append(f"<p>{p}</p>")
                return "".join(html_paras)
    except Exception as e:
        print(f"SMRY reader query failed for {article_url}: {e}")
    return None

def parse_article_details(url):
    """Verifies that the article redirects to government-and-politics, then parses content."""
    try:
        # requests automatically follows redirects (res.url is the final destination)
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return None
    except Exception as e:
        print(f"Failed to fetch article {url}: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(res.text, "html.parser")
    final_url = res.url.split("?")[0]

    # Verify canonical URL
    canonical = soup.find("link", rel="canonical")
    canonical_url = canonical["href"].split("?")[0] if canonical and canonical.get("href") else ""

    # Check section meta tag
    section_meta = soup.find("meta", property="article:section")
    section_name = section_meta["content"].lower() if section_meta and section_meta.get("content") else ""

    # CONDITION: Must resolve/redirect to government-and-politics
    is_target_page = (
        TARGET_SECTION in url
        or TARGET_SECTION in final_url
        or TARGET_SECTION in canonical_url
        or "government" in section_name
        or "politics" in section_name
    )

    if not is_target_page:
        print(f"Skipping non-target article: {final_url}")
        return None

    # Use the resolved article URL
    resolved_url = canonical_url if canonical_url else final_url

    # 1. Headline
    title_meta = soup.find("meta", property="og:title")
    title = title_meta["content"].strip() if title_meta and title_meta.get("content") else ""
    if not title and soup.title:
        title = soup.title.string.strip()

    # 2. Summary
    desc_meta = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    summary = desc_meta["content"].strip() if desc_meta and desc_meta.get("content") else ""

    # 3. Featured Image
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

    # 5. Fetch full content via SMRY
    body_html = fetch_content_from_smry(resolved_url)

    # 6. Fallback to public preview paragraphs if SMRY returns empty
    if not body_html:
        fallback_paragraphs = []
        for p in soup.find_all("p"):
            parent_class = " ".join(p.parent.get("class", []))
            if any(term in parent_class.lower() for term in ("paywall", "subscribe", "footer", "modal")):
                break
            text = p.get_text().strip()
            if len(text) > 30:
                fallback_paragraphs.append(f"<p>{text}</p>")
            if len(fallback_paragraphs) >= 3:
                break
        body_html = "".join(fallback_paragraphs)

    return {
        "title": title,
        "link": resolved_url,
        "summary": summary,
        "image_url": image_url,
        "pub_date": pub_date_str,
        "body_html": body_html,
        "smry_link": f"https://smry.ai/{resolved_url}"
    }

def create_item_node(dom, article):
    """Creates a standard RSS <item> node."""
    item = dom.createElement("item")

    # Title
    title_elem = dom.createElement("title")
    title_elem.appendChild(dom.createCDATASection(article["title"]))
    item.appendChild(title_elem)

    # Link & Guid
    link_elem = dom.createElement("link")
    link_elem.appendChild(dom.createTextNode(article["link"]))
    item.appendChild(link_elem)

    guid_elem = dom.createElement("guid")
    guid_elem.setAttribute("isPermaLink", "true")
    guid_elem.appendChild(dom.createTextNode(article["link"]))
    item.appendChild(guid_elem)

    # PubDate
    pub_elem = dom.createElement("pubDate")
    pub_elem.appendChild(dom.createTextNode(article["pub_date"]))
    item.appendChild(pub_elem)

    # Category
    cat_elem = dom.createElement("category")
    cat_elem.appendChild(dom.createCDATASection("government-and-politics"))
    item.appendChild(cat_elem)

    # Description
    desc_content = ""
    if article["image_url"]:
        desc_content += f'<p><img src="{article["image_url"]}" style="max-width:100%;" /></p>'
    if article["summary"]:
        desc_content += f'<p><strong>சுருக்கம்: {article["summary"]}</strong></p><hr/>'
    if article["body_html"]:
        desc_content += article["body_html"]
    desc_content += f'<p><br/><a href="{article["smry_link"]}" target="_blank">📖 Open in SMRY Reader</a></p>'

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

    channel = dom.createElement("channel")
    rss.appendChild(channel)

    title = dom.createElement("title")
    title.appendChild(dom.createTextNode("Junior Vikatan - Government & Politics"))
    channel.appendChild(title)

    link = dom.createElement("link")
    link.appendChild(dom.createTextNode(LISTING_URL))
    channel.appendChild(link)

    desc = dom.createElement("description")
    desc.appendChild(dom.createTextNode("Junior Vikatan articles under Government and Politics"))
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

    candidate_links = fetch_article_links()
    print(f"Discovered {len(candidate_links)} candidate links.")

    new_links = [l for l in candidate_links if l not in existing_ids]
    print(f"Processing {len(new_links)} unindexed links...")

    new_item_nodes = []
    for link in new_links:
        article_data = parse_article_details(link)
        if article_data and article_data["title"]:
            item_node = create_item_node(dom, article_data)
            new_item_nodes.append(item_node)

    if not new_item_nodes:
        print("No new government-and-politics articles found.")
        return

    # Prepend newest articles to the top
    first_existing = existing_items[0] if existing_items else None
    for item_node in new_item_nodes:
        if first_existing:
            channel.insertBefore(item_node, first_existing)
            channel.insertBefore(dom.createTextNode("\n    "), first_existing)
        else:
            channel.appendChild(dom.createTextNode("\n    "))
            channel.appendChild(item_node)

    # Retain maximum 100 items
    all_items = list(channel.getElementsByTagName("item"))
    if len(all_items) > MAX_ITEMS:
        for excess in all_items[MAX_ITEMS:]:
            prev = excess.previousSibling
            if prev and prev.nodeType == prev.TEXT_NODE and prev.nodeValue.strip() == "":
                channel.removeChild(prev)
            channel.removeChild(excess)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(dom.toxml(encoding="utf-8"))

    total = len(channel.getElementsByTagName("item"))
    print(f"Successfully added {len(new_item_nodes)} government-and-politics articles. Total items: {total}")

if __name__ == "__main__":
    main()

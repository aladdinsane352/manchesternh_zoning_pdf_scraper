"""
NH Board Agenda & PDF Scraper

Monitors multiple NH municipal board pages for new agendas and PDFs.
Add new sources to the SOURCES list; supported types:
  node_agenda   — CivicEngage /node/NNN/agenda (Cloudflare, year subpath)
  agendacenter  — CivicPlus AgendaCenter /AgendaCenter/<Board>-<ID>
  archive       — CivicPlus Archive.aspx?AMID=NNN
  legistar      — Legistar REST API (webapi.legistar.com)
  civicclerk    — CivicClerk portal (React SPA with HTML fallback)
"""

import argparse
import datetime
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Manchester NH — PDF sources (tracked by filename)
# ---------------------------------------------------------------------------
BASE_URL = "https://www.manchesternh.gov"
PAGE_URLS = [
    f"{BASE_URL}/Departments/Planning-and-Comm-Dev/Zoning-Board/Project-Applications",
    f"{BASE_URL}/Departments/Planning-and-Comm-Dev/Planning-Board/Project-Applications",
]
PAGE_URL = PAGE_URLS[0]  # kept for backward compatibility

# ---------------------------------------------------------------------------
# All other agenda/document monitoring sources
# ---------------------------------------------------------------------------
SOURCES = [
    # CivicEngage node/agenda — Cloudflare-protected, year-as-subpath
    {"type": "node_agenda", "url": "https://www.merrimacknh.gov/node/2261/agenda",  "label": "Merrimack Planning Board"},
    {"type": "node_agenda", "url": "https://www.merrimacknh.gov/node/2296/agenda",  "label": "Merrimack Zoning Board"},
    {"type": "node_agenda", "url": "https://www.derrynh.gov/node/206/agenda",       "label": "Derry Planning Board"},
    {"type": "node_agenda", "url": "https://www.derrynh.gov/node/216/agenda",       "label": "Derry Zoning Board"},
    # CivicPlus AgendaCenter
    {"type": "agendacenter", "url": "https://www.salemnh.gov/AgendaCenter/Zoning-Board-of-Adjustment-14", "label": "Salem ZBA"},
    {"type": "agendacenter", "url": "https://www.salemnh.gov/AgendaCenter/Planning-Board-6",              "label": "Salem Planning Board"},
    {"type": "agendacenter", "url": "https://www.londonderrynh.gov/AgendaCenter/Planning-Board-16",       "label": "Londonderry Planning Board"},
    {"type": "agendacenter", "url": "https://www.londonderrynh.gov/AgendaCenter/Zoning-Board-of-Adjustment-24", "label": "Londonderry ZBA"},
    # CivicPlus Archive.aspx
    {"type": "archive", "url": "https://www.concordnh.gov/Archive.aspx?AMID=61&Type=&ADID=", "label": "Concord ZBA"},
    # Legistar REST API — Planning Board only
    {"type": "legistar", "url": "https://webapi.legistar.com/v1/ConcordNH/events",
     "label": "Concord Planning Board", "board_filter": "Planning Board"},
    # CivicClerk portal — Planning & Zoning categories 38 and 77
    {"type": "civicclerk", "url": "https://nashuanh.portal.civicclerk.com/?category_id=38,77",
     "label": "Nashua Planning/Zoning"},
]

# Keep for backward compatibility with existing manifest entries
MERRIMACK_URLS = [s["url"] for s in SOURCES if "merrimacknh.gov" in s["url"]]

_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_YEAR_ONLY_RE = re.compile(r"^(20\d{2})$")


# ---------------------------------------------------------------------------
# Manchester NH — PDF scraper
# ---------------------------------------------------------------------------

def fetch_pdf_links(url: str) -> list[dict]:
    """Fetch the page and extract all PDF links."""
    print(f"Fetching: {url}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; PDF-Scraper/1.0)"},
    )
    with urllib.request.urlopen(req) as response:
        html = response.read().decode("utf-8", errors="replace")

    soup = BeautifulSoup(html, "html.parser")
    module_div = soup.find("div", class_="ModManchesterDynamicFileListC") or soup
    pdfs = []
    for a_tag in module_div.find_all("a", href=True):
        href = a_tag["href"]
        if href.lower().endswith(".pdf"):
            full_url = href if href.startswith("http") else BASE_URL + href
            filename = a_tag.get_text(strip=True) or Path(urllib.parse.unquote(href)).name
            pdfs.append({"url": full_url, "filename": filename})
    return pdfs


def download_pdf(pdf_url: str, dest_path: Path) -> bool:
    """Download a single PDF to dest_path. Returns True on success."""
    if dest_path.exists():
        print(f"  [skip] {dest_path.name} (already exists)")
        return True
    parsed = urllib.parse.urlparse(pdf_url)
    encoded_url = parsed._replace(path=urllib.parse.quote(parsed.path, safe="/:")).geturl()
    try:
        req = urllib.request.Request(
            encoded_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PDF-Scraper/1.0)"},
        )
        with urllib.request.urlopen(req) as response:
            data = response.read()
        dest_path.write_bytes(data)
        print(f"  [ok]   {dest_path.name} ({len(data):,} bytes)")
        return True
    except urllib.error.HTTPError as e:
        print(f"  [err]  {dest_path.name} — HTTP {e.code}: {e.reason}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  [err]  {dest_path.name} — {e}", file=sys.stderr)
        return False


def sanitize_filename(name: str) -> str:
    """Remove characters not safe for filenames."""
    return "".join(c for c in name if c not in r'\/:*?"<>|')


# ---------------------------------------------------------------------------
# CivicEngage node/agenda scraper (Merrimack, Derry — Cloudflare)
# ---------------------------------------------------------------------------

def fetch_agenda_links(url: str) -> list[dict]:
    """Fetch agenda links from a CivicEngage /node/NNN/agenda page.

    Detects year-as-subpath navigation, follows the most recent year's URL,
    and extracts agenda items from <h3> links.  Uses cloudscraper to bypass
    Cloudflare.  Returns list of dicts: url, title, year, source_url.
    """
    import cloudscraper
    print(f"Fetching: {url}")
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    session = cloudscraper.create_scraper()

    resp = session.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    _path_year_re = re.compile(r"/(\d{4})/?$")
    subpath_years: dict[int, str] = {}
    for a_tag in soup.find_all("a", href=True):
        m = _path_year_re.search(a_tag["href"])
        if m:
            year = int(m.group(1))
            if 2000 <= year <= 2099:
                href = a_tag["href"]
                subpath_years[year] = href if href.startswith("http") else base + href

    if subpath_years:
        best_year = max(subpath_years)
        year_url = subpath_years[best_year]
        print(f"  Following {best_year} sub-page: {year_url}")
        resp2 = session.get(year_url)
        resp2.raise_for_status()
        soup = BeautifulSoup(resp2.text, "html.parser")
        return _extract_h3_agenda_links(soup, year_url, base, best_year)

    most_recent_year, container = _find_most_recent_year_section(soup)
    if container is None:
        print(f"  [warn] No year sections found; scraping all h3 links from {url}")
        container = soup
    else:
        print(f"  Found {most_recent_year} section")
    return _extract_h3_agenda_links(container, url, base, most_recent_year)


def _extract_h3_agenda_links(container, page_url: str, base: str, year) -> list[dict]:
    h3_links = [a for h in container.find_all("h3") for a in h.find_all("a", href=True)]
    source = h3_links if h3_links else container.find_all("a", href=True)
    links: list[dict] = []
    seen: set[str] = set()
    for a_tag in source:
        href = a_tag["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript"):
            continue
        full_url = href if href.startswith("http") else (
            base + href if href.startswith("/") else urllib.parse.urljoin(page_url, href)
        )
        if full_url in seen:
            continue
        seen.add(full_url)
        title = a_tag.get_text(strip=True) or Path(urllib.parse.unquote(href)).name
        links.append({"url": full_url, "title": title, "year": year, "source_url": page_url})
    return links


def _find_most_recent_year_section(soup):
    year_panes: dict[int, object] = {}
    for tag in soup.find_all(id=_YEAR_RE):
        m = _YEAR_RE.search(tag.get("id", ""))
        if m:
            year_panes[int(m.group(1))] = tag
    if year_panes:
        best = max(year_panes)
        return best, year_panes[best]

    year_headings: dict[int, object] = {}
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        text = tag.get_text(strip=True)
        if _YEAR_ONLY_RE.match(text):
            year_headings[int(text)] = tag
    if year_headings:
        best = max(year_headings)
        heading = year_headings[best]
        siblings_html: list[str] = []
        collecting = False
        for sibling in heading.parent.children:
            if sibling is heading:
                collecting = True
                continue
            if not collecting:
                continue
            if (hasattr(sibling, "name") and sibling.name in ("h1", "h2", "h3", "h4", "h5")
                    and _YEAR_ONLY_RE.match(sibling.get_text(strip=True))):
                break
            siblings_html.append(str(sibling))
        if siblings_html:
            return best, BeautifulSoup("".join(siblings_html), "html.parser")
        return best, heading.parent

    year_tabs: dict[int, object] = {}
    for tag in soup.find_all(["a", "button"]):
        text = tag.get_text(strip=True)
        if _YEAR_ONLY_RE.match(text):
            year_tabs[int(text)] = tag
    if year_tabs:
        best = max(year_tabs)
        tab = year_tabs[best]
        panel_id = tab.get("aria-controls") or tab.get("href", "").lstrip("#")
        if panel_id:
            panel = soup.find(id=panel_id)
            if panel:
                return best, panel
        return best, soup

    return None, None


# ---------------------------------------------------------------------------
# CivicPlus AgendaCenter scraper (Salem, Londonderry)
# ---------------------------------------------------------------------------

def fetch_agendacenter_links(url: str) -> list[dict]:
    """Fetch agenda links from a CivicPlus AgendaCenter page.

    The page HTML contains all years' agenda links; this function extracts
    /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID hrefs, groups by year, and
    returns only the most recent year's items.
    """
    import requests
    print(f"Fetching: {url}")
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    _ac_re = re.compile(r"/AgendaCenter/ViewFile/Agenda/_(\d{2})(\d{2})(\d{4})-(\d+)", re.I)
    by_year: dict[int, list] = {}
    seen: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        m = _ac_re.search(a_tag["href"])
        if not m:
            continue
        mm, dd, yyyy, item_id = m.groups()
        year = int(yyyy)
        canonical = f"{base}/AgendaCenter/ViewFile/Agenda/_{mm}{dd}{yyyy}-{item_id}"
        if canonical in seen:
            continue
        seen.add(canonical)
        title = a_tag.get_text(strip=True) or f"Agenda {mm}/{dd}/{yyyy}"
        by_year.setdefault(year, []).append(
            {"url": canonical, "title": title, "year": year, "source_url": url}
        )

    if not by_year:
        print(f"  [warn] No AgendaCenter agenda links found at {url}")
        return []

    best = max(by_year)
    print(f"  Found {len(by_year[best])} agendas for {best}")
    return by_year[best]


# ---------------------------------------------------------------------------
# CivicPlus Archive.aspx scraper (Concord)
# ---------------------------------------------------------------------------

def fetch_archive_links(url: str) -> list[dict]:
    """Fetch document links from a CivicPlus Archive.aspx page.

    Filters to the current calendar year to avoid flooding on first run.
    """
    import requests
    print(f"Fetching: {url}")
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    current_year = datetime.date.today().year

    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    _adid_re = re.compile(r"ADID=(\d+)", re.I)

    links: list[dict] = []
    seen: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        m = _adid_re.search(a_tag["href"])
        if not m:
            continue
        adid = m.group(1)
        canonical = f"{base}/Archive.aspx?ADID={adid}"
        if canonical in seen:
            continue

        # Look for a year in surrounding text (parent row / nearby siblings)
        ctx = ""
        for node in [a_tag.parent, a_tag.find_previous("td"), a_tag.find_previous("li")]:
            if node and hasattr(node, "get_text"):
                ctx += node.get_text(" ", strip=True) + " "
        ctx += a_tag.get_text(strip=True)
        year_m = _YEAR_RE.search(ctx)
        item_year = int(year_m.group(1)) if year_m else None

        # Only notify about current-year items
        if item_year and item_year != current_year:
            continue

        seen.add(canonical)
        title = a_tag.get_text(strip=True) or f"Document {adid}"
        links.append({"url": canonical, "title": title, "year": item_year, "source_url": url})

    print(f"  Found {len(links)} current-year archive documents")
    return links


# ---------------------------------------------------------------------------
# Legistar REST API scraper (Concord Planning Board)
# ---------------------------------------------------------------------------

def fetch_legistar_links(url: str, board_filter=None) -> list[dict]:
    """Fetch meetings from the Legistar REST API that have agendas posted.

    url      — Legistar API base, e.g. https://webapi.legistar.com/v1/ConcordNH/events
    board_filter — case-insensitive substring to match EventBodyName (e.g. "Planning Board")

    Only returns meetings from the current year that have EventAgendaFile set.
    """
    import requests
    current_year = datetime.date.today().year
    print(f"Fetching Legistar API: {url} (filter={board_filter!r})")

    params = {
        "$filter": f"EventDate ge datetime'{current_year}-01-01T00:00:00'",
        "$orderby": "EventDate desc",
        "$top": 200,
    }
    r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    events = r.json()

    links: list[dict] = []
    for event in events:
        body_name = event.get("EventBodyName", "")
        if board_filter and board_filter.lower() not in body_name.lower():
            continue
        agenda_file = event.get("EventAgendaFile") or ""
        if not agenda_file:
            continue
        date_str = (event.get("EventDate") or "")[:10]
        title = f"{body_name} — {date_str}"
        year = int(date_str[:4]) if date_str else current_year
        links.append({"url": agenda_file, "title": title, "year": year, "source_url": url})

    print(f"  Found {len(links)} events with agendas")
    return links


# ---------------------------------------------------------------------------
# CivicClerk scraper (Nashua)
# ---------------------------------------------------------------------------

def fetch_civicclerk_links(url: str) -> list[dict]:
    """Fetch published files from a CivicClerk portal via its OData API.

    The portal URL has the form https://{tenant}.portal.civicclerk.com/?category_id=38,77.
    The actual data lives at https://{tenant}.api.civicclerk.com/v1/Events.
    Published files (agendas, packets, minutes, notices) are in each event's
    publishedFiles array; download URLs are relative to the portal base.
    """
    import requests
    print(f"Fetching CivicClerk: {url}")
    parsed = urllib.parse.urlparse(url)
    portal_base = f"{parsed.scheme}://{parsed.netloc}"
    # Extract tenant from hostname: "nashuanh.portal.civicclerk.com" -> "nashuanh"
    tenant = parsed.hostname.split(".")[0]
    qs = urllib.parse.parse_qs(parsed.query)
    category_ids = [c.strip() for c in qs.get("category_id", [""])[0].split(",") if c.strip()]

    api_base = f"https://{tenant}.api.civicclerk.com/v1/Events"
    links = []

    for cat_id in category_ids:
        api_url = f"{api_base}?%24filter=categoryId+eq+{cat_id}&%24orderby=eventDate+desc"
        try:
            r = requests.get(
                api_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PDF-Scraper/1.0)", "Accept": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  [err] CivicClerk API failed for category {cat_id}: {e}", file=sys.stderr)
            continue

        for event in data.get("value", []):
            event_date = event.get("eventDate", "")[:10]
            event_name = event.get("eventName", "Meeting")
            year = int(event_date[:4]) if len(event_date) >= 4 and event_date[:4].isdigit() else None
            for f in event.get("publishedFiles", []):
                file_url = f.get("url", "")
                if not file_url:
                    continue
                full_url = f"{portal_base}/{file_url}"
                file_type = f.get("type", "")
                file_name = f.get("name", "")
                title = f"{event_name} — {file_name or file_type} ({event_date})"
                links.append({"url": full_url, "title": title, "year": year, "source_url": url})

    print(f"  Found {len(links)} published files via OData API")
    return links


# ---------------------------------------------------------------------------
# CLI entry point (Manchester PDFs only)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download PDFs from the Manchester NH Zoning Board Project Applications page."
    )
    parser.add_argument("-o", "--output", default="pdfs",
                        help="Output directory for downloaded PDFs (default: ./pdfs)")
    parser.add_argument("--list", action="store_true",
                        help="List available PDFs without downloading")
    parser.add_argument("--url", default=PAGE_URL,
                        help="Page URL to scrape")
    args = parser.parse_args()

    pdfs = fetch_pdf_links(args.url)
    if not pdfs:
        print("No PDFs found on the page.")
        sys.exit(1)

    print(f"\nFound {len(pdfs)} PDF(s):\n")
    if args.list:
        for pdf in pdfs:
            print(f"  {pdf['filename']}\n    {pdf['url']}")
        return

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading to: {output_dir.resolve()}\n")
    success = sum(
        download_pdf(pdf["url"], output_dir / (sanitize_filename(pdf["filename"])
                                               if pdf["filename"].lower().endswith(".pdf")
                                               else sanitize_filename(pdf["filename"]) + ".pdf"))
        for pdf in pdfs
    )
    print(f"\nDone: {success}/{len(pdfs)} PDFs downloaded to '{output_dir}'")


if __name__ == "__main__":
    main()

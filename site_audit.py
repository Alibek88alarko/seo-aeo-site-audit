#!/usr/bin/env python3
"""site_audit.py: single-page website audit for SEO, technical basics, local SEO and AEO.

Demo, version 0.1. Heuristic checks on one page, no JavaScript rendering.

Live:     python site_audit.py https://example.com --out output/
Offline:  python site_audit.py --html page.html --base-url https://demo.test --out output/
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

VERSION = "0.1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 site-audit-demo/0.1"
)
DEFAULT_TIMEOUT = 15.0

SECTIONS = {
    "seo": "SEO basics",
    "technical": "Technical",
    "local": "Local SEO",
    "aeo": "AEO (AI search readiness)",
}
STATUSES = ("pass", "warn", "fail", "not_checked")
CREDIT = {"pass": 1.0, "warn": 0.5, "fail": 0.0}

OK = "No action needed."
OFFLINE = "Not checked (offline): the page was read from a local file, so the live site was not contacted."
RUN_LIVE = "Run the audit against the live URL to check this."

# Thresholds (kept in one place so the README and tests can refer to them).
TITLE_MIN, TITLE_MAX = 30, 60
DESC_MIN, DESC_MAX = 70, 160
WORDS_PASS, WORDS_WARN = 300, 150
HTML_PASS_KB, HTML_WARN_KB = 300, 1024
RESPONSE_PASS_S, RESPONSE_WARN_S = 1.0, 2.5

ORG_TYPES = {
    "Organization", "Corporation", "LocalBusiness", "ProfessionalService", "NGO",
    "EducationalOrganization", "MedicalOrganization", "Dentist", "Physician", "MedicalClinic",
    "Hospital", "Pharmacy", "LegalService", "Attorney", "Notary", "Plumber", "Electrician",
    "HVACBusiness", "RoofingContractor", "GeneralContractor", "HousePainter", "Locksmith",
    "MovingCompany", "Restaurant", "FoodEstablishment", "CafeOrCoffeeShop", "Bakery", "BarOrPub",
    "AutoRepair", "AutoDealer", "BeautySalon", "HairSalon", "NailSalon", "DaySpa",
    "RealEstateAgent", "InsuranceAgency", "FinancialService", "AccountingService", "Hotel",
    "Motel", "ChildCare", "DryCleaningOrLaundry", "EmploymentAgency", "TravelAgency",
    "ExerciseGym", "SportsClub", "VeterinaryCare", "Optician", "Store",
}
GENERIC_TYPES = {"WebPage", "BreadcrumbList", "ListItem", "ImageObject", "SiteNavigationElement",
                 "WPHeader", "WPFooter", "SearchAction", "EntryPoint", "PropertyValueSpecification"}

PHONE_PATTERNS = [
    re.compile(r"\+\d{1,3}(?:[\s.\-]?\(?\d{1,4}\)?){2,5}"),                  # international
    re.compile(r"(?:\b1[\s.\-])?\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"),   # North American
]
STREET_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z0-9.'\-]+\s+){0,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|"
    r"Highway|Hwy|Parkway|Pkwy|Terrace|Circle|Square|Suite|Ste)\b\.?"
)
MAPS_RE = re.compile(
    r"(google\.[a-z.]+/maps|maps\.google\.|goo\.gl/maps|maps\.app\.goo\.gl|g\.page/|business\.google\.com)",
    re.I,
)


class AuditError(Exception):
    """Raised when the page cannot be audited honestly (network error, HTTP error page)."""


@dataclass
class Fetched:
    url: str            # final URL after redirects
    status: int
    content: bytes
    headers: dict = field(default_factory=dict)   # lower-case keys
    elapsed: float = 0.0                          # seconds, includes redirects and download


def http_get(url: str, timeout: float = DEFAULT_TIMEOUT) -> Fetched:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    t0 = time.perf_counter()
    r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    content = r.content
    elapsed = time.perf_counter() - t0
    return Fetched(r.url, r.status_code, content, {k.lower(): v for k, v in r.headers.items()}, elapsed)


# --------------------------------------------------------------------------- page facts

def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _schema_type_names(node: dict) -> list[str]:
    t = node.get("@type")
    names = t if isinstance(t, list) else [t]
    out = []
    for n in names:
        if isinstance(n, str) and n.strip():
            out.append(re.split(r"[/:#]", n.strip())[-1])   # "http://schema.org/Plumber" -> "Plumber"
    return out


def _walk_jsonld(obj, out: list) -> None:
    if isinstance(obj, list):
        for x in obj:
            _walk_jsonld(x, out)
    elif isinstance(obj, dict):
        if "@type" in obj:
            out.append(obj)
        for k, v in obj.items():
            if k != "@context":
                _walk_jsonld(v, out)


def find_phones(text: str) -> list[str]:
    found = []
    for pat in PHONE_PATTERNS:
        for m in pat.finditer(text):
            cand = m.group(0).strip()
            digits = re.sub(r"\D", "", cand)
            if not 10 <= len(digits) <= 15:
                continue
            if not (cand.startswith("+") or re.search(r"[\s.\-()]", cand)):
                continue
            if cand not in found:
                found.append(cand)
    return found


def is_org_type(t: str) -> bool:
    return t in ORG_TYPES or t.endswith(("Business", "Store", "Organization"))


def _format_address(addr) -> str:
    if isinstance(addr, str):
        return addr.strip()
    if isinstance(addr, list) and addr:
        return _format_address(addr[0])
    if isinstance(addr, dict):
        parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
        return ", ".join(str(p).strip() for p in parts if p)
    return ""


def extract_facts(html: bytes | str, base_url: str, headers: dict | None = None) -> dict:
    """Read everything the checks need from the HTML. No network access here."""
    headers = headers or {}
    raw = html.encode("utf-8") if isinstance(html, str) else html
    soup = BeautifulSoup(raw, "html.parser")

    def meta(attr: str, value: str):
        el = soup.find("meta", attrs={attr: re.compile(rf"^{re.escape(value)}$", re.I)})
        return el.get("content", "").strip() if el and el.get("content") is not None else None

    canonical = None
    for link in soup.find_all("link"):
        rels = [r.lower() for r in (link.get("rel") or [])]
        if "canonical" in rels and link.get("href"):
            canonical = urljoin(base_url, link["href"].strip())
            break

    robots_meta = [m.get("content", "") for m in soup.find_all(
        "meta", attrs={"name": re.compile(r"^(robots|googlebot)$", re.I)})]

    imgs = soup.find_all("img")
    img_total = len(imgs)
    img_no_alt = sum(1 for i in imgs if i.get("alt") is None)
    img_decorative = sum(1 for i in imgs if i.get("alt") is not None and not i["alt"].strip())

    tel_links = [a["href"][4:].strip() for a in soup.find_all("a", href=True)
                 if a["href"].lower().startswith("tel:")]

    maps_hits = []
    for el in soup.find_all(["iframe", "a"]):
        src = el.get("src") or el.get("href") or ""
        if MAPS_RE.search(src):
            maps_hits.append(("embed" if el.name == "iframe" else "link", src))

    # Structured data
    jsonld_ok, jsonld_bad, nodes = 0, 0, []
    for s in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        body = (s.string or s.get_text() or "").strip()
        body = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", body)
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            jsonld_bad += 1
            continue
        jsonld_ok += 1
        _walk_jsonld(data, nodes)
    jsonld_types: list[str] = []
    for n in nodes:
        for t in _schema_type_names(n):
            if t not in jsonld_types:
                jsonld_types.append(t)
    microdata_types: list[str] = []
    for el in soup.find_all(attrs={"itemtype": True}):
        for t in str(el["itemtype"]).split():
            name = re.split(r"[/:#]", t.rstrip("/"))[-1]
            if name and name not in microdata_types:
                microdata_types.append(name)

    org_nodes = [n for n in nodes if any(is_org_type(t) for t in _schema_type_names(n))]
    faq_nodes = [n for n in nodes if {"FAQPage", "QAPage"} & set(_schema_type_names(n))]
    faq_questions = 0
    for n in faq_nodes:
        me = n.get("mainEntity")
        faq_questions += len(me) if isinstance(me, list) else (1 if isinstance(me, dict) else 0)

    headings = [(h.name, _text(h)) for h in soup.find_all(["h2", "h3"])]
    title = _text(soup.find("title")) or None          # first <title>, read before <head> is removed
    h1s = [_text(h) for h in soup.find_all("h1")]
    metas = {"meta_description": meta("name", "description"), "viewport": meta("name", "viewport"),
             "og_title": meta("property", "og:title"), "og_image": meta("property", "og:image")}
    address_tags = [_text(a) for a in soup.find_all("address") if _text(a)]

    # Visible text: drop non-visible elements, then read the text.
    for el in soup(["script", "style", "noscript", "template", "svg", "head"]):
        el.decompose()
    visible = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    words = re.findall(r"[^\W\d_][\w'’\-]*", visible)

    return {
        "base_url": base_url,
        "html_bytes": len(raw),
        "title": title,
        "meta_description": metas["meta_description"],
        "h1": h1s,
        "canonical": canonical,
        "robots_meta": robots_meta,
        "x_robots_tag": headers.get("x-robots-tag"),
        "viewport": metas["viewport"],
        "og_title": metas["og_title"],
        "og_image": metas["og_image"],
        "img_total": img_total,
        "img_no_alt": img_no_alt,
        "img_decorative": img_decorative,
        "word_count": len(words),
        "tel_links": tel_links,
        "text_phones": find_phones(visible),
        "street_matches": [m.group(0) for m in STREET_RE.finditer(visible)][:3],
        "address_tags": address_tags,
        "maps_hits": maps_hits,
        "jsonld_ok": jsonld_ok,
        "jsonld_bad": jsonld_bad,
        "jsonld_types": jsonld_types,
        "microdata_types": microdata_types,
        "org_nodes": org_nodes,
        "faq_nodes": len(faq_nodes),
        "faq_questions": faq_questions,
        "question_headings": [t for name, t in headings if t.endswith("?")],
    }


# --------------------------------------------------------------------------- network probes

def _looks_like_html(f: Fetched) -> bool:
    head = f.content[:400].lstrip().lower()
    return "text/html" in f.headers.get("content-type", "").lower() or head.startswith((b"<!doctype html", b"<html"))


def _safe_get(fetcher, url: str, timeout: float):
    try:
        return fetcher(url, timeout), None
    except requests.RequestException as e:
        return None, f"could not connect ({e.__class__.__name__})"


def parse_robots(text: str) -> tuple[bool, list[str]]:
    """Return (blocks_everything_for_user_agent_*, sitemap_urls)."""
    sitemaps, disallow_all = [], False
    agents, in_rules = [], False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "sitemap":
            sitemaps.append(val)
        elif key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(val)
        elif key in ("disallow", "allow"):
            in_rules = True
            if key == "disallow" and val == "/" and "*" in agents:
                disallow_all = True
    return disallow_all, sitemaps


def probe_site_files(fetcher, origin: str, timeout: float) -> dict:
    out = {}
    # robots.txt
    f, err = _safe_get(fetcher, origin + "/robots.txt", timeout)
    robots = {"url": origin + "/robots.txt", "error": err, "found": False, "status": None,
              "disallow_all": False, "sitemaps": []}
    if f is not None:
        robots["status"] = f.status
        if f.status == 200 and not _looks_like_html(f):
            robots["found"] = True
            robots["disallow_all"], robots["sitemaps"] = parse_robots(f.content.decode("utf-8", "replace"))
    out["robots"] = robots

    # sitemap.xml (or the first sitemap listed in robots.txt)
    def valid_sitemap(resp):
        body = resp.content[:200000].decode("utf-8", "replace")
        return resp.status == 200 and ("<urlset" in body or "<sitemapindex" in body), body.count("<loc>")

    sm = {"url": origin + "/sitemap.xml", "error": None, "found": False, "status": None,
          "locs": 0, "via_robots": False, "listed": robots["sitemaps"][:1]}
    f, err = _safe_get(fetcher, sm["url"], timeout)
    if f is not None:
        sm["status"] = f.status
        sm["found"], sm["locs"] = valid_sitemap(f)
    if not sm["found"] and robots["sitemaps"]:
        f2, err2 = _safe_get(fetcher, robots["sitemaps"][0], timeout)
        if f2 is not None:
            ok, locs = valid_sitemap(f2)
            if ok:
                sm.update(found=True, locs=locs, via_robots=True, url=robots["sitemaps"][0], status=f2.status)
            else:
                sm["listed_status"] = f2.status
        else:
            sm["listed_status"] = err2
    if f is None and not sm["found"]:
        sm["error"] = err
    out["sitemap"] = sm

    # llms.txt
    f, err = _safe_get(fetcher, origin + "/llms.txt", timeout)
    llms = {"url": origin + "/llms.txt", "error": err, "found": False, "status": None}
    if f is not None:
        llms["status"] = f.status
        llms["found"] = f.status == 200 and not _looks_like_html(f) and bool(f.content.strip())
    out["llms"] = llms
    return out


# --------------------------------------------------------------------------- checks

def _c(cid, section, label, status, weight, detail, fix) -> dict:
    assert status in STATUSES and section in SECTIONS
    if status == "pass":
        fix = OK
    return {"id": cid, "section": section, "label": label, "status": status,
            "weight": weight, "detail": detail, "fix": fix}


def _q(s: str, n: int = 90) -> str:
    s = s or ""
    return '"' + (s if len(s) <= n else s[: n - 3] + "...") + '"'


def seo_checks(f: dict, net: dict) -> list[dict]:
    out = []
    label = "Secure connection (HTTPS)"
    if net["mode"] == "offline":
        out.append(_c("seo_https", "seo", label, "not_checked", 3, OFFLINE, RUN_LIVE))
    elif urlparse(net["final_url"]).scheme == "https":
        d = f"The page loads over HTTPS ({net['final_url']})."
        if net.get("requested_scheme") == "http":
            d += " The http:// address redirects to it."
        out.append(_c("seo_https", "seo", label, "pass", 3, d, ""))
    else:
        out.append(_c("seo_https", "seo", label, "fail", 3,
                      f"The page loads over plain HTTP ({net['final_url']}). Browsers label such pages 'Not secure'.",
                      "Install an SSL certificate (usually free from your host) and redirect all http:// traffic to https://."))

    label = "Page title shown in Google results"
    t = f["title"]
    if not t:
        out.append(_c("seo_title", "seo", label, "fail", 3, "No <title> tag found.",
                      "Add a 30 to 60 character title with the business name, main service and city."))
    else:
        n = len(t)
        if TITLE_MIN <= n <= TITLE_MAX:
            out.append(_c("seo_title", "seo", label, "pass", 3, f"Title is {n} characters: {_q(t)}.", ""))
        else:
            fix = ("Lengthen the title to 30 to 60 characters: business name, main service, city."
                   if n < TITLE_MIN else "Shorten the title to 60 characters or less so Google does not cut it off.")
            out.append(_c("seo_title", "seo", label, "warn", 3,
                          f"Title is {n} characters (aim for {TITLE_MIN} to {TITLE_MAX}): {_q(t)}.", fix))

    label = "Description snippet shown under the title in Google"
    d = f["meta_description"]
    if not d:
        out.append(_c("seo_meta_description", "seo", label, "fail", 2, "No meta description found.",
                      "Write a 70 to 160 character description that says what you offer, where, and why to choose you."))
    else:
        n = len(d)
        if DESC_MIN <= n <= DESC_MAX:
            out.append(_c("seo_meta_description", "seo", label, "pass", 2, f"Description is {n} characters.", ""))
        else:
            fix = ("Expand the description to 70 to 160 characters with your offer and a reason to click."
                   if n < DESC_MIN else "Trim the description to 160 characters or less so it is not cut off.")
            out.append(_c("seo_meta_description", "seo", label, "warn", 2,
                          f"Description is {n} characters (aim for {DESC_MIN} to {DESC_MAX}): {_q(d)}.", fix))

    label = "One clear main heading on the page"
    h1 = f["h1"]
    if not h1:
        out.append(_c("seo_h1", "seo", label, "fail", 2, "No H1 heading found.",
                      "Add one H1 heading that says what you do and where, for example 'Emergency Plumber in Austin'."))
    elif len(h1) == 1:
        out.append(_c("seo_h1", "seo", label, "pass", 2, f"One H1: {_q(h1[0])}.", ""))
    else:
        out.append(_c("seo_h1", "seo", label, "warn", 2,
                      f"{len(h1)} H1 headings found: " + "; ".join(_q(h, 50) for h in h1[:4]) + ".",
                      "Keep one H1 for the main topic and turn the others into H2 subheadings."))

    label = "Preferred address of the page (canonical tag)"
    can = f["canonical"]
    if not can:
        out.append(_c("seo_canonical", "seo", label, "warn", 1, "No canonical tag found.",
                      "Add a <link rel=\"canonical\"> tag that points to the preferred address of this page."))
    elif urlparse(can).netloc.lower() != urlparse(f["base_url"]).netloc.lower():
        out.append(_c("seo_canonical", "seo", label, "warn", 1,
                      f"Canonical points to a different site ({can}), so search engines may index that address instead.",
                      "Point the canonical tag at this page's own address unless the duplicate is intentional."))
    else:
        out.append(_c("seo_canonical", "seo", label, "pass", 1, f"Canonical tag points to {can}.", ""))

    label = "Page allows search engines to list it"
    directives = [("meta robots", v) for v in f["robots_meta"]]
    if f.get("x_robots_tag"):
        directives.append(("X-Robots-Tag header", f["x_robots_tag"]))
    blocked = [(src, v) for src, v in directives
               if {"noindex", "none"} & {tok.strip().lower() for tok in re.split(r"[,\s]+", v)}]
    if blocked:
        src, v = blocked[0]
        out.append(_c("seo_robots_meta", "seo", label, "fail", 3,
                      f"The page tells search engines not to list it ({src}: '{v}').",
                      "Remove 'noindex' (often left over from a staging site) unless this page should stay hidden."))
    else:
        d = "No noindex directive found."
        if directives:
            d += f" Robots directive: '{directives[0][1]}'."
        out.append(_c("seo_robots_meta", "seo", label, "pass", 3, d, ""))

    label = "Images have text descriptions (alt text)"
    total, dec, missing = f["img_total"], f["img_decorative"], f["img_no_alt"]
    considered = total - dec
    if total == 0:
        out.append(_c("seo_img_alt", "seo", label, "not_checked", 1,
                      "No images found on the page, so there is nothing to score.", "Nothing to fix here."))
    elif considered == 0:
        out.append(_c("seo_img_alt", "seo", label, "warn", 1,
                      f"All {total} images have empty alt text (marked decorative), so none are described.",
                      "Describe the meaningful images (team, work, products) with short alt text."))
    else:
        described = considered - missing
        ratio = described / considered
        status = "pass" if ratio >= 0.9 else "warn" if ratio >= 0.5 else "fail"
        d = f"{described} of {considered} images have alt text ({round(ratio * 100)}%)."
        if dec:
            d += f" {dec} more marked decorative (empty alt), which is fine."
        out.append(_c("seo_img_alt", "seo", label, status, 1, d,
                      "Add a short alt description to each meaningful image, for example 'Technician repairing a water heater'."))

    label = "Enough written content on the page"
    wc = f["word_count"]
    status = "pass" if wc >= WORDS_PASS else "warn" if wc >= WORDS_WARN else "fail"
    out.append(_c("seo_word_count", "seo", label, status, 2,
                  f"About {wc} words of visible text (aim for {WORDS_PASS} or more).",
                  "Expand the page to at least 300 words covering what you do, for whom, where, prices and common questions."))

    label = "Preview title and image when shared on social media"
    have = [n for n, v in (("og:title", f["og_title"]), ("og:image", f["og_image"])) if v]
    status = "pass" if len(have) == 2 else "warn" if have else "fail"
    d = {2: "og:title and og:image both found.",
         1: f"Only {have[0] if have else ''} found; the other Open Graph tag is missing.",
         0: "No og:title or og:image tags found."}[len(have)]
    out.append(_c("seo_open_graph", "seo", label, status, 1, d,
                  "Add og:title and og:image tags so links shared on Facebook, LinkedIn or WhatsApp show a proper preview."))
    return out


def technical_checks(f: dict, net: dict) -> list[dict]:
    out = []
    offline = net["mode"] == "offline"

    label = "Instructions file for search engine robots (robots.txt)"
    r = net.get("robots") or {}
    if offline:
        out.append(_c("tech_robots", "technical", label, "not_checked", 1, OFFLINE, RUN_LIVE))
    elif r.get("error"):
        out.append(_c("tech_robots", "technical", label, "not_checked", 1,
                      f"Not checked: robots.txt request failed, {r['error']}.", "Re-run the audit later."))
    elif r["found"] and r["disallow_all"]:
        out.append(_c("tech_robots", "technical", label, "fail", 1,
                      f"robots.txt at {r['url']} blocks all search engines (User-agent: * with Disallow: /).",
                      "Remove 'Disallow: /' for User-agent: * unless the whole site should stay out of search."))
    elif r["found"]:
        d = f"robots.txt found at {r['url']}"
        d += f" and lists {len(r['sitemaps'])} sitemap(s)." if r["sitemaps"] else "."
        out.append(_c("tech_robots", "technical", label, "pass", 1, d, ""))
    else:
        why = "returned a web page instead of a text file" if r.get("status") == 200 else f"HTTP {r.get('status')}"
        out.append(_c("tech_robots", "technical", label, "warn", 1,
                      f"No robots.txt ({why}). Not harmful by itself, since search engines then crawl everything, "
                      "but it is the usual place to point them to your sitemap.",
                      "Add a simple robots.txt that allows crawling and lists your sitemap address."))

    label = "List of pages for search engines (sitemap.xml)"
    s = net.get("sitemap") or {}
    if offline:
        out.append(_c("tech_sitemap", "technical", label, "not_checked", 2, OFFLINE, RUN_LIVE))
    elif s.get("found"):
        how = "listed in robots.txt and reachable" if s["via_robots"] else "found"
        out.append(_c("tech_sitemap", "technical", label, "pass", 2,
                      f"Sitemap {how} at {s['url']} ({s['locs']} <loc> entries in the first file).", ""))
    elif s.get("listed"):
        out.append(_c("tech_sitemap", "technical", label, "warn", 2,
                      f"robots.txt lists {s['listed'][0]}, but it could not be loaded as a sitemap "
                      f"({s.get('listed_status')}).",
                      "Fix the sitemap address in robots.txt or regenerate the sitemap."))
    elif s.get("error"):
        out.append(_c("tech_sitemap", "technical", label, "not_checked", 2,
                      f"Not checked: sitemap request failed, {s['error']}.", "Re-run the audit later."))
    else:
        out.append(_c("tech_sitemap", "technical", label, "fail", 2,
                      f"No sitemap at /sitemap.xml (HTTP {s.get('status')}) and none listed in robots.txt.",
                      "Generate a sitemap.xml (most site builders and SEO plugins do this) and list it in robots.txt."))

    label = "Page is set up for mobile phones"
    vp = f["viewport"]
    if vp is None:
        out.append(_c("tech_viewport", "technical", label, "fail", 3,
                      "No viewport tag, so phones may show a shrunken desktop page. "
                      "(This is the basic signal only, not a full mobile test.)",
                      "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> to the page head."))
    elif "width=device-width" in vp.replace(" ", "").lower():
        out.append(_c("tech_viewport", "technical", label, "pass", 3,
                      f"Viewport tag found: '{vp}'. (Basic signal only, not a full mobile test.)", ""))
    else:
        out.append(_c("tech_viewport", "technical", label, "warn", 3,
                      f"Viewport tag found but without width=device-width: '{vp}'.",
                      "Set the viewport to 'width=device-width, initial-scale=1'."))

    label = "Page code is a reasonable size"
    kb = f["html_bytes"] / 1024
    status = "pass" if kb <= HTML_PASS_KB else "warn" if kb <= HTML_WARN_KB else "fail"
    out.append(_c("tech_html_size", "technical", label, status, 1,
                  f"The HTML itself is {kb:.0f} KB (images, scripts and styles loaded separately are not counted).",
                  "Move inline scripts, styles and embedded data out of the HTML and remove unused page-builder code."))

    label = "Server answers quickly"
    if offline:
        out.append(_c("tech_response_time", "technical", label, "not_checked", 2, OFFLINE, RUN_LIVE))
    else:
        t = net["response_time"]
        status = "pass" if t <= RESPONSE_PASS_S else "warn" if t <= RESPONSE_WARN_S else "fail"
        out.append(_c("tech_response_time", "technical", label, status, 2,
                      f"The page took {t:.2f} s to download from the audit machine (one measurement, includes "
                      "redirects; not a full speed test).",
                      "Use faster hosting or a CDN with caching, then run Google PageSpeed Insights for a full speed report."))
    return out


def local_checks(f: dict, net: dict, skip: bool = False) -> list[dict]:
    labels = {
        "local_phone": ("Phone number customers can tap to call", 3),
        "local_address": ("Business address search engines can read", 3),
        "local_maps": ("Link or map to find you on Google Maps", 1),
    }
    if skip:
        return [_c(cid, "local", lab, "not_checked", w,
                   "Skipped (--skip-local): treated as a business without a local storefront or service area.",
                   "Run without --skip-local to include local checks.") for cid, (lab, w) in labels.items()]
    out = []
    lab, w = labels["local_phone"]
    schema_phones = [str(n["telephone"]) for n in f["org_nodes"] if n.get("telephone")]
    if f["tel_links"]:
        out.append(_c("local_phone", "local", lab, "pass", w, f"Tap-to-call link found: {f['tel_links'][0]}.", ""))
    elif f["text_phones"] or schema_phones:
        where = (f"in the page text ({f['text_phones'][0]})" if f["text_phones"]
                 else f"only in structured data ({schema_phones[0]})")
        out.append(_c("local_phone", "local", lab, "warn", w,
                      f"Phone number found {where}, but not as a tap-to-call link.",
                      "Wrap the phone number in a tap-to-call link (<a href=\"tel:+15125550100\">) near the top of the page."))
    else:
        out.append(_c("local_phone", "local", lab, "fail", w, "No phone number found on the page.",
                      "Show your phone number near the top of the page as a tap-to-call link."))

    lab, w = labels["local_address"]
    with_addr = [n for n in f["org_nodes"] if _format_address(n.get("address"))]
    if with_addr:
        n = with_addr[0]
        out.append(_c("local_address", "local", lab, "pass", w,
                      f"Address found in {_schema_type_names(n)[0]} structured data: {_format_address(n['address'])}.", ""))
    elif f["address_tags"] or f["street_matches"]:
        snippet = (f["address_tags"] or f["street_matches"])[0]
        out.append(_c("local_address", "local", lab, "warn", w,
                      f"An address appears in the page text ({_q(snippet, 70)}) but not in structured data "
                      "(text match is a heuristic).",
                      "Add LocalBusiness schema (JSON-LD) with your full address, matching your Google Business Profile."))
    else:
        out.append(_c("local_address", "local", lab, "fail", w,
                      "No business address found in the page text or structured data.",
                      "Show your address or service area on the page and add it to LocalBusiness schema."))

    lab, w = labels["local_maps"]
    if f["maps_hits"]:
        kind, src = f["maps_hits"][0]
        out.append(_c("local_maps", "local", lab, "pass", w, f"Google Maps {kind} found ({src[:80]}).", ""))
    else:
        out.append(_c("local_maps", "local", lab, "warn", w,
                      "No Google Maps embed or link found. This is a bonus item, so it only counts as 'needs work'.",
                      "Embed your Google Maps location or link to your Google Business Profile."))
    return out


def aeo_checks(f: dict, net: dict) -> list[dict]:
    out = []
    label = "Structured data for search engines and AI (JSON-LD)"
    ok, bad = f["jsonld_ok"], f["jsonld_bad"]
    fix = ("Add JSON-LD structured data (Organization or LocalBusiness at minimum) so search engines and "
           "AI tools can read your key facts reliably.")
    if ok and not bad:
        out.append(_c("aeo_jsonld", "aeo", label, "pass", 2, f"{ok} JSON-LD block(s) found and parsed.", ""))
    elif ok and bad:
        out.append(_c("aeo_jsonld", "aeo", label, "warn", 2,
                      f"{ok} JSON-LD block(s) parsed, {bad} could not be parsed (broken JSON).",
                      "Fix the broken JSON-LD block; test it with the Schema.org validator."))
    elif bad:
        out.append(_c("aeo_jsonld", "aeo", label, "fail", 2,
                      f"{bad} JSON-LD block(s) found but none could be parsed (broken JSON).",
                      "Fix the broken JSON-LD block; test it with the Schema.org validator."))
    else:
        out.append(_c("aeo_jsonld", "aeo", label, "fail", 2, "No JSON-LD structured data found.", fix))

    label = "Types of structured data found"
    found = [t for t in f["jsonld_types"]] + [f"{t} (microdata)" for t in f["microdata_types"]
                                              if t not in f["jsonld_types"]]
    useful = [t for t in f["jsonld_types"] + f["microdata_types"] if t not in GENERIC_TYPES]
    fix = "Describe the business with schema.org types such as LocalBusiness, Service and FAQPage."
    if not found:
        out.append(_c("aeo_schema_types", "aeo", label, "fail", 1,
                      "No schema.org types found (checked JSON-LD and microdata).", fix))
    elif useful:
        out.append(_c("aeo_schema_types", "aeo", label, "pass", 1, "Found: " + ", ".join(found) + ".", ""))
    else:
        out.append(_c("aeo_schema_types", "aeo", label, "warn", 1,
                      "Only generic page types found: " + ", ".join(found) + ". Nothing describes the business itself.",
                      fix))

    label = "FAQ marked up so AI tools can quote answers"
    note = (" Google shows FAQ rich results only for a small set of sites since 2023, but a marked-up "
            "question and answer block is still a clean, structured source for answer engines.")
    fix = "Add an FAQ section with 3 to 8 real customer questions and mark it up with FAQPage schema."
    if f["faq_nodes"]:
        out.append(_c("aeo_faq", "aeo", label, "pass", 2,
                      f"FAQPage/QAPage schema found with {f['faq_questions']} question(s).", ""))
    elif f["question_headings"] or "FAQPage" in f["microdata_types"]:
        out.append(_c("aeo_faq", "aeo", label, "warn", 2,
                      "The page has question-style content but no FAQPage JSON-LD markup." + note, fix))
    else:
        out.append(_c("aeo_faq", "aeo", label, "fail", 2, "No FAQPage or QAPage schema found." + note, fix))

    label = "Headings phrased as customer questions"
    qh = f["question_headings"]
    status = "pass" if len(qh) >= 2 else "warn" if qh else "fail"
    d = (f"{len(qh)} H2/H3 heading(s) end with '?': " + "; ".join(_q(h, 60) for h in qh[:3]) + "."
         if qh else "No H2/H3 headings end with a question mark.")
    out.append(_c("aeo_question_headings", "aeo", label, status, 2, d,
                  "Rewrite some subheadings as the questions customers ask, for example 'How much does a drain "
                  "cleaning cost?', and answer each one right below."))

    label = "Business identity for AI tools (name and social profiles)"
    fix = ("Add Organization or LocalBusiness schema with your exact business name and sameAs links to your "
           "Google Business Profile, Facebook, LinkedIn and Yelp pages.")
    orgs = f["org_nodes"]
    if not orgs:
        extra = (" An organization type appears in microdata, but this check reads JSON-LD only."
                 if any(is_org_type(t) for t in f["microdata_types"]) else "")
        out.append(_c("aeo_entity", "aeo", label, "fail", 3,
                      "No Organization or LocalBusiness schema found in JSON-LD." + extra, fix))
    else:
        def sameas(n):
            v = n.get("sameAs")
            return [v] if isinstance(v, str) else list(v or [])
        best = max(orgs, key=lambda n: (bool(n.get("name")), len(sameas(n))))
        t, name, links = _schema_type_names(best)[0], best.get("name"), sameas(best)
        if name and links:
            out.append(_c("aeo_entity", "aeo", label, "pass", 3,
                          f"{t} schema for {_q(str(name), 60)} with {len(links)} sameAs profile link(s).", ""))
        elif name:
            out.append(_c("aeo_entity", "aeo", label, "warn", 3,
                          f"{t} schema for {_q(str(name), 60)} found, but no sameAs links to your other profiles.", fix))
        else:
            out.append(_c("aeo_entity", "aeo", label, "warn", 3, f"{t} schema found, but without a business name.", fix))

    label = "Guide file for AI assistants (llms.txt)"
    note = ("llms.txt is an emerging, voluntary convention proposed in 2024; no major search engine has "
            "confirmed using it, so it is only scored as 'needs work' when missing.")
    l = net.get("llms") or {}
    if net["mode"] == "offline":
        out.append(_c("aeo_llms_txt", "aeo", label, "not_checked", 1, OFFLINE, RUN_LIVE))
    elif l.get("error"):
        out.append(_c("aeo_llms_txt", "aeo", label, "not_checked", 1,
                      f"Not checked: llms.txt request failed, {l['error']}.", "Re-run the audit later."))
    elif l["found"]:
        out.append(_c("aeo_llms_txt", "aeo", label, "pass", 1, f"llms.txt found at {l['url']}. " + note, ""))
    else:
        why = "the address returned a web page" if l.get("status") == 200 else f"HTTP {l.get('status')}"
        out.append(_c("aeo_llms_txt", "aeo", label, "warn", 1, f"No llms.txt found ({why}). " + note,
                      "Optionally add /llms.txt with a short plain-text summary of the business and links to key pages."))
    return out


def run_checks(facts: dict, net: dict, skip_local: bool = False) -> list[dict]:
    return (seo_checks(facts, net) + technical_checks(facts, net)
            + local_checks(facts, net, skip_local) + aeo_checks(facts, net))


# --------------------------------------------------------------------------- scoring

def weighted_score(checks: list[dict]) -> int | None:
    scored = [c for c in checks if c["status"] in CREDIT]
    total = sum(c["weight"] for c in scored)
    if not total:
        return None
    return round(100 * sum(c["weight"] * CREDIT[c["status"]] for c in scored) / total)


def grade(score: int | None) -> str | None:
    if score is None:
        return None
    for g, cut in (("A", 90), ("B", 80), ("C", 70), ("D", 60)):
        if score >= cut:
            return g
    return "F"


def top_fixes(checks: list[dict], n: int = 5) -> list[dict]:
    """Fails and warnings ordered by points lost (weight x missing credit); fails win ties."""
    bad = [(i, c) for i, c in enumerate(checks) if c["status"] in ("fail", "warn")]
    bad.sort(key=lambda ic: (-ic[1]["weight"] * (1 - CREDIT[ic[1]["status"]]),
                             ic[1]["status"] != "fail", ic[0]))
    return [c for _, c in bad[:n]]


def build_result(url: str, final_url: str, mode: str, facts: dict, net: dict, skip_local: bool) -> dict:
    checks = run_checks(facts, net, skip_local)
    sections = {}
    for key, name in SECTIONS.items():
        cs = [c for c in checks if c["section"] == key]
        s = weighted_score(cs)
        sections[key] = {"name": name, "score": s, "grade": grade(s),
                         "checked": sum(c["status"] != "not_checked" for c in cs), "total": len(cs)}
    overall = weighted_score(checks)
    counts = {st: sum(c["status"] == st for c in checks) for st in STATUSES}
    public_facts = {k: v for k, v in facts.items() if k != "org_nodes"}
    public_facts["org_names"] = [n.get("name") for n in facts["org_nodes"] if n.get("name")]
    return {
        "tool": "site_audit.py",
        "version": VERSION,
        "audited_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "mode": mode,
        "url": url,
        "final_url": final_url,
        "fetch": {k: net.get(k) for k in ("status_code", "response_time")} if mode == "live" else None,
        "site_files": {k: net.get(k) for k in ("robots", "sitemap", "llms")} if mode == "live" else None,
        "score": {"overall": overall, "grade": grade(overall), "sections": sections},
        "counts": counts,
        "top_fixes": [c["id"] for c in top_fixes(checks)],
        "checks": checks,
        "facts": public_facts,
    }


def audit_live(url: str, fetcher=http_get, timeout: float = DEFAULT_TIMEOUT, skip_local: bool = False) -> dict:
    if "://" not in url:
        url = "https://" + url
    try:
        main = fetcher(url, timeout)
    except requests.RequestException as e:
        raise AuditError(f"Could not fetch {url}: {e.__class__.__name__}: {e}") from e
    if main.status >= 400:
        raise AuditError(f"{url} returned HTTP {main.status}. Not scoring an error page.")
    p = urlparse(main.url)
    origin = f"{p.scheme}://{p.netloc}"
    net = {"mode": "live", "final_url": main.url, "requested_scheme": urlparse(url).scheme,
           "status_code": main.status, "response_time": round(main.elapsed, 3)}
    net.update(probe_site_files(fetcher, origin, timeout))
    facts = extract_facts(main.content, main.url, main.headers)
    return build_result(url, main.url, "live", facts, net, skip_local)


def audit_html(html: bytes | str, base_url: str, skip_local: bool = False) -> dict:
    facts = extract_facts(html, base_url)
    net = {"mode": "offline", "final_url": base_url}
    return build_result(base_url, base_url, "offline", facts, net, skip_local)


# --------------------------------------------------------------------------- outputs

def ghl_payload(result: dict, email: str | None = None) -> dict:
    """Example body for a GoHighLevel Inbound Webhook trigger (any JSON is accepted there),
    plus the same data shaped like the API v2 POST /contacts/upsert body."""
    sec = result["score"]["sections"]
    tops = [c for c in result["checks"] if c["id"] in result["top_fixes"]]
    tops.sort(key=lambda c: result["top_fixes"].index(c["id"]))
    overall = result["score"]["overall"]
    custom = {
        "audit_score": overall,
        "audit_grade": result["score"]["grade"],
        "seo_score": sec["seo"]["score"],
        "technical_score": sec["technical"]["score"],
        "local_score": sec["local"]["score"],
        "aeo_score": sec["aeo"]["score"],
    }
    for i in range(3):
        custom[f"top_issue_{i + 1}"] = (f"{STATUS_WORD[tops[i]['status']]}: {tops[i]['label']}. Fix: {tops[i]['fix']}"
                                        if i < len(tops) else "")
    custom["audit_date"] = result["audited_at"][:10]
    custom["audit_mode"] = result["mode"]
    tags = ["website-audit-done", f"audit-grade-{(result['score']['grade'] or 'na').lower()}"]
    if overall is not None and overall < 60:
        tags.append("audit-score-below-60")
    website = result["final_url"]
    contact = {"website": website, "email": email}
    upsert = {"locationId": "YOUR_LOCATION_ID", "website": website, "tags": tags,
              "customFields": [{"key": k, "field_value": str(v)} for k, v in custom.items() if v not in (None, "")]}
    if email:
        upsert["email"] = email
    return {
        "_comment": ("Example payload generated by site_audit.py. Field keys must match custom fields created in "
                     "your GoHighLevel sub-account. Not tested against a live GHL account."),
        "event": "website_audit_completed",
        "contact": contact,
        "customFields": custom,
        "tags": tags,
        "api_v2_contacts_upsert_body": upsert,
    }


STATUS_WORD = {"pass": "Good", "warn": "Needs work", "fail": "Problem", "not_checked": "Not checked"}
IMPORTANCE = {3: "High", 2: "Medium", 1: "Low"}


def _color(score):
    if score is None:
        return "#6b7280"
    return "#15803d" if score >= 80 else "#b45309" if score >= 60 else "#b91c1c"


def render_report(result: dict) -> str:
    e = htmllib.escape
    sc = result["score"]
    overall = sc["overall"]
    sections = sc["sections"]
    scored = {k: v for k, v in sections.items() if v["score"] is not None}
    summary = f"This page scored {overall} out of 100 (grade {sc['grade']})."
    if len(scored) >= 2:
        best = max(scored.values(), key=lambda v: v["score"])
        worst = min(scored.values(), key=lambda v: v["score"])
        summary += (f" Strongest area: {best['name']}, {best['score']}/100. "
                    f"Biggest opportunity: {worst['name']}, {worst['score']}/100.")
    cnt = result["counts"]
    summary += (f" Of {len(result['checks'])} checks: {cnt['pass']} good, {cnt['warn']} need work,"
                f" {cnt['fail']} problems, {cnt['not_checked']} not checked.")
    mode_note = ("Live check of the page as the public sees it." if result["mode"] == "live" else
                 "Offline check of a saved HTML file. Items that need the live site are marked 'Not checked'.")

    cards = []
    for v in sections.values():
        s = v["score"]
        pct = s if s is not None else 0
        cards.append(
            f'<div class="card"><div class="cname">{e(v["name"])}</div>'
            f'<div class="cscore" style="color:{_color(s)}">{s if s is not None else "n/a"}'
            f'<span>/100</span></div><div class="bar"><i style="width:{pct}%;background:{_color(s)}"></i></div>'
            f'<div class="muted">{v["checked"]} of {v["total"]} checks scored</div></div>')

    tops = [c for c in result["checks"] if c["id"] in result["top_fixes"]]
    tops.sort(key=lambda c: result["top_fixes"].index(c["id"]))
    top_html = "".join(
        f'<li><div><span class="b {c["status"]}">{STATUS_WORD[c["status"]]}</span> '
        f'<strong>{e(c["fix"])}</strong></div>'
        f'<div class="muted">Check: {e(c["label"])} ({IMPORTANCE[c["weight"]]} importance, '
        f'{e(SECTIONS[c["section"]])}). Found: {e(c["detail"])}</div></li>' for c in tops
    ) or "<li>No problems found by these checks.</li>"

    rows = []
    for key, name in SECTIONS.items():
        rows.append(f'<tr class="sec"><td colspan="5">{e(name)}'
                    f' <span class="muted">score {sections[key]["score"] if sections[key]["score"] is not None else "n/a"}'
                    f'</span></td></tr>')
        for c in (c for c in result["checks"] if c["section"] == key):
            rows.append(
                f'<tr><td><span class="b {c["status"]}">{STATUS_WORD[c["status"]]}</span></td>'
                f'<td><strong>{e(c["label"])}</strong></td><td>{e(c["detail"])}</td>'
                f'<td>{e(c["fix"])}</td><td>{IMPORTANCE[c["weight"]]}</td></tr>')

    date = result["audited_at"][:10]
    url = result["final_url"]
    out = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Website audit: {e(urlparse(url).netloc or url)}</title>
<style>
:root{{--bg:#f5f6f8;--card:#fff;--ink:#1f2933;--muted:#6b7280;--line:#e5e7eb}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}}
.wrap{{max-width:980px;margin:0 auto;padding:24px 16px 48px}}
header{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}}
.ring{{width:120px;height:120px;border-radius:50%;flex:none;display:grid;place-items:center;
background:conic-gradient({_color(overall)} {overall or 0}%,var(--line) 0)}}
.ring>div{{width:94px;height:94px;border-radius:50%;background:var(--card);display:grid;place-items:center;text-align:center}}
.ring b{{font-size:30px;line-height:1;color:{_color(overall)}}} .ring small{{display:block;color:var(--muted)}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 10px}}
.muted{{color:var(--muted);font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}}
.cname{{font-weight:600}} .cscore{{font-size:28px;font-weight:700}} .cscore span{{font-size:14px;color:var(--muted);font-weight:400}}
.bar{{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:6px 0}} .bar i{{display:block;height:100%}}
ol.top{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px 12px 36px;margin:0}}
ol.top li{{padding:10px 0;border-bottom:1px solid var(--line)}} ol.top li:last-child{{border-bottom:0}}
.fix{{margin:4px 0}}
.b{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;white-space:nowrap}}
.b.pass{{background:#dcfce7;color:#166534}} .b.warn{{background:#fef3c7;color:#92400e}}
.b.fail{{background:#fee2e2;color:#991b1b}} .b.not_checked{{background:#e5e7eb;color:#374151}}
.tw{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}}
table{{border-collapse:collapse;width:100%;min-width:720px}}
th,td{{text-align:left;vertical-align:top;padding:9px 10px;border-bottom:1px solid var(--line);font-size:14px}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}
tr.sec td{{background:#f9fafb;font-weight:700}}
footer{{margin-top:28px}}
@media print{{body{{background:#fff}} .tw,ol.top,.card,header{{border-color:#ccc}}}}
</style></head><body><div class="wrap">
<header>
  <div class="ring"><div><div><b>{overall if overall is not None else "n/a"}</b><small>grade {e(str(sc["grade"]))}</small></div></div></div>
  <div style="flex:1;min-width:240px">
    <h1>Website audit for {e(url)}</h1>
    <div class="muted">Date: {date} &middot; {e(mode_note)}</div>
    <p>{e(summary)}</p>
  </div>
</header>
<div class="grid">{''.join(cards)}</div>
<h2>Top 5 fixes</h2>
<ol class="top">{top_html}</ol>
<h2>All checks</h2>
<div class="tw"><table>
<thead><tr><th>Status</th><th>What we checked</th><th>What we found</th><th>How to fix</th><th>Importance</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<footer class="muted">
<p><strong>How the score works.</strong> Each check has an importance: High counts 3 points, Medium 2, Low 1.
Good earns full points, Needs work earns half, Problem earns zero. Checks that could not be run are left out
of the total instead of counting against you. Grades: A 90+, B 80+, C 70+, D 60+, F below 60.</p>
<p><strong>What this is.</strong> An automated check of one page using simple, transparent rules. It does not run
JavaScript, crawl other pages or measure full page speed. Generated by site_audit.py version {VERSION}.</p>
</footer>
</div></body></html>
"""
    return out.replace("\u2014", "-")   # quoted page text may contain em dashes; keep the report free of them


def write_outputs(result: dict, out_dir: Path, email: str | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"report": out_dir / "report.html", "audit": out_dir / "audit.json", "ghl": out_dir / "ghl_payload.json"}
    paths["report"].write_text(render_report(result), encoding="utf-8")
    paths["audit"].write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["ghl"].write_text(json.dumps(ghl_payload(result, email), indent=2, ensure_ascii=False), encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Single-page SEO / local SEO / AEO audit with a plain-English report.")
    ap.add_argument("url", nargs="?", help="page to audit live, e.g. https://example.com")
    ap.add_argument("--html", help="audit a local HTML file instead (offline mode)")
    ap.add_argument("--base-url", help="the address the local HTML file belongs to (required with --html)")
    ap.add_argument("--out", default="output", help="folder for report.html, audit.json, ghl_payload.json")
    ap.add_argument("--email", help="prospect email to include in the GoHighLevel payload")
    ap.add_argument("--skip-local", action="store_true", help="mark local SEO checks as not applicable")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per request (default 15)")
    a = ap.parse_args(argv)

    if bool(a.url) == bool(a.html):
        ap.error("give either a URL or --html FILE, not both")
    try:
        if a.html:
            if not a.base_url:
                ap.error("--base-url is required with --html")
            result = audit_html(Path(a.html).read_bytes(), a.base_url, a.skip_local)
        else:
            result = audit_live(a.url, timeout=a.timeout, skip_local=a.skip_local)
    except AuditError as err:
        print(f"Audit failed: {err}", file=sys.stderr)
        return 2

    paths = write_outputs(result, Path(a.out), a.email)
    sc = result["score"]
    print(f"{result['final_url']}  score {sc['overall']}/100  grade {sc['grade']}  ({result['mode']})")
    for v in sc["sections"].values():
        print(f"  {v['name']:<28} {v['score'] if v['score'] is not None else 'n/a'}")
    print("Wrote: " + ", ".join(str(p) for p in paths.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

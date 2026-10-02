"""Tests for site_audit.py. No network: offline fixtures plus a fake fetcher for live mode."""
import json
import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import site_audit as sa  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def audit(name, base="https://demo.test/", **kw):
    return sa.audit_html((FIX / name).read_bytes(), base, **kw)


def by_id(result):
    return {c["id"]: c for c in result["checks"]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("tests must not touch the network")
    monkeypatch.setattr(requests, "get", blocked)


def test_every_check_has_required_fields_and_valid_values():
    r = audit("good_page.html")
    assert len(r["checks"]) == 23
    for c in r["checks"]:
        assert set(c) == {"id", "section", "label", "status", "weight", "detail", "fix"}
        assert c["section"] in sa.SECTIONS
        assert c["status"] in sa.STATUSES
        assert c["weight"] in (1, 2, 3)
        assert c["detail"] and c["fix"]


def test_good_page_scores_high():
    r = audit("good_page.html", "https://northwind-books.test/")
    assert r["score"]["overall"] >= 90
    assert r["score"]["grade"] == "A"
    c = by_id(r)
    for cid in ("seo_title", "seo_meta_description", "seo_h1", "seo_canonical", "tech_viewport",
                "local_phone", "local_address", "aeo_faq", "aeo_entity"):
        assert c[cid]["status"] == "pass", cid
    assert c["local_maps"]["status"] == "warn"          # bonus item: missing is only a warning


def test_weak_page_scores_low_and_noindex_is_top_fix():
    r = audit("weak_page.html", "https://joes-roofing.test/")
    assert r["score"]["overall"] < 40
    assert r["score"]["grade"] == "F"
    c = by_id(r)
    assert c["seo_robots_meta"]["status"] == "fail"
    assert c["seo_h1"]["status"] == "warn" and "2 H1" in c["seo_h1"]["detail"]
    assert c["seo_img_alt"]["status"] == "fail"
    assert c["aeo_jsonld"]["status"] == "fail"
    assert c["local_phone"]["status"] == "warn"          # phone in text, no tel: link
    assert r["top_fixes"][0] == "seo_robots_meta"
    assert len(r["top_fixes"]) == 5


def test_local_business_page_with_faq_schema():
    r = audit("local_faq_page.html", "https://rapid-rooter.test/")
    c = by_id(r)
    assert c["aeo_faq"]["status"] == "pass" and "3 question" in c["aeo_faq"]["detail"]
    assert c["local_address"]["status"] == "pass" and "Round Rock" in c["local_address"]["detail"]
    assert c["local_maps"]["status"] == "pass"
    assert c["aeo_entity"]["status"] == "pass"
    assert "Plumber" in c["aeo_schema_types"]["detail"] and "FAQPage" in c["aeo_schema_types"]["detail"]
    assert c["seo_title"]["status"] == "warn"            # 97 characters
    assert c["local_phone"]["status"] == "warn"          # shown as text only


def test_offline_mode_marks_network_checks_not_checked_and_excludes_them():
    r = audit("good_page.html")
    c = by_id(r)
    for cid in ("seo_https", "tech_robots", "tech_sitemap", "tech_response_time", "aeo_llms_txt"):
        assert c[cid]["status"] == "not_checked", cid
        assert "offline" in c[cid]["detail"].lower()
    tech = r["score"]["sections"]["technical"]
    assert tech["checked"] == 2 and tech["total"] == 5
    assert tech["score"] == 100                          # not_checked items are not counted as failures


def test_scoring_math_and_grades():
    checks = [
        {"status": "pass", "weight": 3},
        {"status": "warn", "weight": 2},
        {"status": "fail", "weight": 1},
        {"status": "not_checked", "weight": 3},
    ]
    assert sa.weighted_score(checks) == round(100 * (3 + 1) / 6)   # 67
    assert sa.weighted_score([{"status": "not_checked", "weight": 2}]) is None
    assert [sa.grade(s) for s in (95, 90, 89, 80, 70, 60, 59, 0)] == ["A", "A", "B", "B", "C", "D", "F", "F"]
    assert sa.grade(None) is None


def test_title_length_boundaries():
    def title_status(n):
        html = f"<html><head><title>{'x' * n}</title></head><body></body></html>"
        return by_id(sa.audit_html(html, "https://demo.test/"))["seo_title"]["status"]
    assert [title_status(n) for n in (29, 30, 60, 61)] == ["warn", "pass", "pass", "warn"]
    assert by_id(sa.audit_html("<html><body></body></html>", "https://demo.test/"))["seo_title"]["status"] == "fail"


def test_phone_detection_ignores_years_and_prices():
    assert sa.find_phones("Call (512) 555-0182 today") == ["(512) 555-0182"]
    assert sa.find_phones("Reach us at +44 20 7946 0958.") == ["+44 20 7946 0958"]
    assert sa.find_phones("Copyright 2019-2024. Prices from 1,250 USD. Order 1234567890.") == []


def test_outputs_files_ghl_tags_and_no_em_dash(tmp_path):
    weak = audit("weak_page.html", "https://joes-roofing.test/")
    paths = sa.write_outputs(weak, tmp_path / "weak", email="owner@joes-roofing.test")
    report = paths["report"].read_text(encoding="utf-8")
    assert "\u2014" not in report
    assert "Top 5 fixes" in report and "https://joes-roofing.test/" in report
    assert "<link" not in report and "<script" not in report     # self-contained
    data = json.loads(paths["audit"].read_text())
    assert data["score"]["overall"] == weak["score"]["overall"]
    ghl = json.loads(paths["ghl"].read_text())
    assert ghl["contact"] == {"website": "https://joes-roofing.test/", "email": "owner@joes-roofing.test"}
    assert ghl["customFields"]["audit_score"] == weak["score"]["overall"]
    assert ghl["customFields"]["top_issue_1"].startswith("Problem: Page allows search engines to list it. Fix:")
    assert "audit-score-below-60" in ghl["tags"]
    keys = {f["key"] for f in ghl["api_v2_contacts_upsert_body"]["customFields"]}
    assert {"audit_score", "aeo_score", "audit_date"} <= keys

    good = audit("good_page.html", "https://northwind-books.test/")
    ghl_good = sa.ghl_payload(good)
    assert "audit-score-below-60" not in ghl_good["tags"]
    assert "audit-grade-a" in ghl_good["tags"]


def test_em_dash_in_page_title_does_not_reach_report():
    html = "<html><head><title>Acme Plumbing \u2014 Emergency Plumber in Austin</title></head><body></body></html>"
    r = sa.audit_html(html, "https://demo.test/")
    assert "\u2014" in r["facts"]["title"]               # raw data kept in audit.json
    assert "\u2014" not in sa.render_report(r)


def test_live_mode_with_fake_fetcher():
    page = (FIX / "local_faq_page.html").read_bytes()
    responses = {
        "http://rapid-rooter.test": sa.Fetched("https://rapid-rooter.test/", 200, page,
                                               {"content-type": "text/html"}, 0.42),
        "https://rapid-rooter.test/robots.txt": sa.Fetched(
            "https://rapid-rooter.test/robots.txt", 200,
            b"User-agent: *\nDisallow: /\nSitemap: https://rapid-rooter.test/sm.xml\n", {"content-type": "text/plain"}),
        "https://rapid-rooter.test/sitemap.xml": sa.Fetched("https://rapid-rooter.test/sitemap.xml", 404, b"", {}),
        "https://rapid-rooter.test/sm.xml": sa.Fetched(
            "https://rapid-rooter.test/sm.xml", 200,
            b"<?xml version='1.0'?><urlset><url><loc>https://rapid-rooter.test/</loc></url></urlset>",
            {"content-type": "application/xml"}),
        # soft 404: the server answers 200 with an HTML page
        "https://rapid-rooter.test/llms.txt": sa.Fetched(
            "https://rapid-rooter.test/llms.txt", 200, b"<!doctype html><html>Not found</html>",
            {"content-type": "text/html"}),
    }

    def fake(url, timeout):
        return responses[url]

    r = sa.audit_live("http://rapid-rooter.test", fetcher=fake)
    c = by_id(r)
    assert r["mode"] == "live"
    assert c["seo_https"]["status"] == "pass" and "redirects" in c["seo_https"]["detail"]
    assert c["tech_robots"]["status"] == "fail"                  # Disallow: / for everyone
    assert c["tech_sitemap"]["status"] == "pass" and "robots.txt" in c["tech_sitemap"]["detail"]
    assert c["aeo_llms_txt"]["status"] == "warn" and "emerging" in c["aeo_llms_txt"]["detail"]
    assert c["tech_response_time"]["status"] == "pass"


def test_live_mode_network_failures_are_not_checked_not_failed():
    page = (FIX / "good_page.html").read_bytes()

    def fake(url, timeout):
        if url.endswith("/"):
            return sa.Fetched(url, 200, page, {"content-type": "text/html"}, 3.1)
        raise requests.ConnectionError("boom")

    r = sa.audit_live("https://northwind-books.test/", fetcher=fake)
    c = by_id(r)
    for cid in ("tech_robots", "tech_sitemap", "aeo_llms_txt"):
        assert c[cid]["status"] == "not_checked", cid
    assert c["tech_response_time"]["status"] == "fail"           # 3.1 s


def test_live_mode_refuses_to_score_error_pages():
    def fake(url, timeout):
        return sa.Fetched(url, 403, b"<html>Forbidden</html>", {"content-type": "text/html"})
    with pytest.raises(sa.AuditError):
        sa.audit_live("https://blocked.test/", fetcher=fake)

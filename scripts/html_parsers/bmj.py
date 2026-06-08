"""BMJ Publishing Group (bmj) HTML parser."""

import json
import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    format_author_name,
    format_doi,
    get_meta,
    neutralize_media_queries,
    parse_meta_authors,
    remove_elements_by_id,
    remove_elements_by_selector,
    strip_common,
    strip_tags,
    tags_to_text,
)

# Publisher-specific noise strings removed from main_text.
_NOISE = (
    "Open in a new tab",
    "Previous Section",
    "Next Section",
    "View this table:",
    "View inline",
    "View popup",
    "In this window",
    "In a new window",
    "In this page",
    "Download as PowerPoint",
    "View larger version:",
    "Download figure",
    "Download powerpoint",
)

# h2 headings that are reference sections (removed from main_text)
_REF_RE = re.compile(r"\breferences\b", re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r"supplement|extended data|source data|appendix",
    re.IGNORECASE,
)

# Site chrome (end boundary). BMJ trails the article with "Other content
# recommended for you" and "Read the full text or download the PDF:" panels.
_CHROME_RE = re.compile(
    r"^other content recommended|^read the full text|^linked articles",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

# CSS injected before </head> to lock the rendered article to a 720-px-wide
# native column, hide BMJ Highwire2 fixed/sticky chrome, collapse the
# Highwire panel-region right rail, and resolve figures to a single
# block-level image above its caption.
_OVERRIDE_CSS = (
    "<style>"
    # Step 1 / Step 6 — lock layout to 752 px wide, white background.
    "html{margin:0!important;padding:0!important;"
    "background:#fff!important;}"
    "body{max-width:752px!important;width:auto!important;"
    "min-width:0!important;"
    "margin:0 auto!important;padding:0 16px!important;"
    "box-sizing:border-box!important;"
    "background:#fff!important;"
    "overflow-wrap:break-word!important;word-wrap:break-word!important;}"
    # BMJ ships fixed-pixel `.container` widths from Bootstrap; let
    # them shrink to body so the column-margin scan stays clean.
    ".container,.container-fluid,#page,#main-wrapper,"
    ".main-container,.region-inner"
    "{width:auto!important;max-width:100%!important;"
    "margin-left:auto!important;margin-right:auto!important;"
    "padding-left:0!important;padding-right:0!important;}"
    # Step 3 — hide sticky chrome (sticky back-to-top affix, scroll-locked
    # header bar, mobile toolbox if any).
    ".pane-bmjj-back-to-top,.article-go-to-top,"
    ".affix,.affix-top,.affix-bottom,"
    "#mobile-tab-title,#mobile-article-tab-container,"
    ".panel-pane.pane-bmjj-mob-tab-title,"
    ".panel-pane.pane-bmjj-mobile-article-menu"
    "{display:none!important;position:static!important;}"
    # Step 4 — hide BMJ's article-side noise panes (jumplinks, social
    # icons, society logins, branding, mobile menus) by their explicit
    # pane class names, NOT by `panel-region-*` (BMJ overloads
    # `panel-region-right` to also wrap the article content body, so a
    # blanket region hide kills the article). Also hide the in-article
    # tools tab strip and email-alerts widget.
    ".pane-bmjj-jumplinks,.pane-bmjj-social-icons,"
    ".pane-bmjj-societies-logos,.pane-bmjj-society-logins,"
    ".pane-bmjj-highwire-inst-branding,"
    ".articleToolbar,.email-alerts,"
    ".pane-highwire-panel-tabs.nav-tabs,"
    ".pane-bmjj-mobile-article-menu,#mobile-article-tab-container,"
    ".pane-bmjj-mob-tab-title,.bmjj-mobile-article-menu,"
    ".panel-pane.pane-add-to-cart,.permissions-box,"
    ".pane-highwire-altmetrics,"
    ".pane-highwire-opportunity-challenge,"
    ".pane-bmjj-article-subscribe-button,"
    ".pane-bmjj-back-to-top,.article-go-to-top,"
    ".pane-menu,.pane-menu-tree.pane-main-menu,"
    ".pane-bmjj-highwire-seach-quicksearch,"
    ".pane-highwire-seach-quicksearch"
    "{display:none!important;}"
    # Force the article tab container and the abstract / fulltext views
    # to render at any viewport. Highwire's narrow-form CSS sets these
    # to display:none and AJAX-loads a mobile accordion in their
    # place; we want the static full-text HTML rendered directly.
    ".pane-highwire-panel-tabs-container,"
    "#panels-ajax-tab-container-highwire_article_tabs,"
    ".panels-ajax-tab-wrap-jnl_template_bmjj_tab_art,"
    ".panels-ajax-tab-container,"
    ".panel-display.panel-1col,"
    ".panel-display.two-layout,"
    ".panel-panel.panel-col,"
    ".panel-pane.pane-highwire-markup,"
    ".panel-region-main,"
    ".right-wrapper,.right-wrapper.article-pane,"
    ".pane-content,"
    ".highwire-markup,.content-block-markup,"
    ".article.fulltext-view,.article.abstract-view,"
    ".panel-pane.pane-highwire-markup.abstract-with-bc"
    "{display:block!important;width:auto!important;"
    "max-width:100%!important;visibility:visible!important;}"
    # The `.panel-region-right` inside `.right-wrapper.panel-region-main`
    # is the article container (text_len ≈ 43k); BMJ's narrow CSS sets
    # it to display:none. Force it visible. The other `.panel-region-*`
    # siblings (left rail, top, bottom, login pane) stay collapsed by
    # the publisher's narrow CSS.
    ".right-wrapper.article-pane > .panel-region-right,"
    ".right-wrapper.panel-region-main > .panel-region-right"
    "{display:block!important;width:auto!important;"
    "max-width:100%!important;}"
    # Step 5 — hide remaining DFP ad slots and sponsorship/marketing
    # panels surfacing at footer or in-article positions.
    ".pane-dfp-pane,.oas-ads,.oas-ads-mid,"
    ".pane-footer-marketing-slots,.footer-marketing-slots,"
    "[id^=div-gpt-ad-],#block-panels-mini-bottom-ad"
    "{display:none!important;}"
    # Step 4 — make the central article column span the cap; the publisher
    # uses Highwire panel-region-middle as the article container. Strip
    # column-grid widths so the article reflows full-width inside the cap.
    ".panel-region-middle,.panel-panel.panel-region-middle,"
    ".col-narrow-12,.col-narrow-9,.col-narrow-8,"
    ".col-normal-9,.col-normal-8,"
    ".col-wide-9,.col-wide-8"
    "{width:100%!important;max-width:100%!important;"
    "float:none!important;margin-left:0!important;"
    "margin-right:0!important;flex:0 0 100%!important;"
    "padding-left:0!important;padding-right:0!important;}"
    # Step 2 — OneTrust hidden filter overlay leftover.
    ".onetrust-pc-dark-filter,#onetrust-consent-sdk,"
    "#onetrust-banner-sdk,#onetrust-pc-sdk"
    "{display:none!important;}"
    # Step 8 — figures: image above caption, both fill column.
    "div.fig.image,div.fig.table-expand-inline,"
    "div[class*='fig pos-']"
    "{display:block!important;width:100%!important;"
    "max-width:100%!important;float:none!important;"
    "margin:0 0 16px 0!important;padding:0!important;"
    "box-sizing:border-box!important;}"
    "div.fig img,div[class*='fig pos-'] img"
    "{display:block!important;width:100%!important;"
    "max-width:100%!important;height:auto!important;"
    "margin:0 0 12px 0!important;"
    "box-sizing:border-box!important;}"
    "div.fig-caption,div.table-caption"
    "{display:block!important;width:100%!important;"
    "margin:0!important;}"
    # Step 9 — no in-place push-down expansion to perform on BMJ.
    # The author affiliations open as a floating tooltip popover
    # (position:absolute, bordered card with box-shadow, fixed pixel
    # width) keyed off `<a class=xref-aff>`; replicating it as
    # push-down would violate Step 9. Affiliations are already
    # extracted from the HTML source by `_parse_authors`.
    #
    # New BMJ Next.js frontend (jitc.bmj.com etc.) wraps the inline
    # author byline in a clamped `<div class="max-h-[95px] md:h-[30px]
    # ... overflow-hidden">` so only the first line of names shows
    # until the user clicks "Show all authors". The static capture
    # has no JS to expand the clamp — release the height/overflow on
    # the byline container so every <button>...<p id=author-list>...
    # </p></button> chip is visible at any viewport. Use a descendant
    # selector (not direct-child `>`) so any overflow-hidden wrapper
    # nested inside the affiliations-list ancestor (current and any
    # future shape) gets unclamped. Then hide the now-redundant
    # "Show all authors" badge whose only purpose was to JS-expand
    # the clamp; with the clamp released, every author already shows
    # inline so the badge is misleading. The badge's stable signature
    # is the wrapping `<span class="bg-bmj-blue-10 ...">` (the only
    # `bg-bmj-blue-10` use on the page).
    "[data-testid=author-affiliations-list]"
    " [class*=overflow-hidden]{"
    "max-height:none!important;height:auto!important;"
    "overflow:visible!important;}"
    "[data-testid=author-affiliations-list]"
    " span[class*=bg-bmj-blue-10]{display:none!important;}"
    "</style>"
)


def remove_banners(html):
    """Apply Phase 2 layout rules for bmj.com (Highwire2 / Drupal panels).

    Step 1: cap body width at 752 px, center, neutralize @media queries
            so the publisher's narrow CSS branch always applies. BMJ's
            wide-viewport CSS lays out a panel-region-right rail
            (article tools, social icons, society logins, jumplinks)
            beside the main column.
    Step 2: OneTrust cookie consent — BMJ ships the
            `#onetrust-consent-sdk` wrapper holding the
            `.onetrust-pc-dark-filter` overlay and `#onetrust-banner-sdk`
            banner. Both render as `position: fixed` overlays on first
            page load and remain in the DOM after capture even when
            dismissed. CSS hides them; DOM removal applied as backup
            in case display:none is overridden by inline styles.
    Step 3: sticky elements — `.pane-bmjj-back-to-top` (back-to-top
            button affixed to the right gutter via Bootstrap affix),
            `.affix-top` ancestor wrappers that re-position on scroll,
            and the mobile sticky tab title. CSS forces `position:
            static; display:none`.
    Step 4: hide left/right Highwire panel regions
            (`.panel-region-right`, `.panel-region-left`) — these hold
            the article-tools rail, society logos, jumplinks, alerts.
            The middle region (`.panel-region-middle`) is forced to
            full width so the article column spans the cap.
    Step 5: ad slots — `.pane-dfp-pane` / `.oas-ads` ad-slot wrappers
            inside the middle column and the bottom ad block
            (`#block-panels-mini-bottom-ad`) reserve vertical space
            even when no ad loads. Hidden via CSS; the
            `.pane-footer-marketing-slots` snippet at the page foot is
            also hidden.
    Step 6: page background already white; html/body forced to white
            for symmetry.
    Step 8: figures — BMJ uses `<div class="fig image ...">` carrying
            an `<img>` with sibling `<div class=fig-caption>`. Force
            block layout, image full column width above caption with
            12 px gap.
    Step 9: no in-place push-down expansion to perform. The only
            collapsed item, the author-affiliation tooltip, opens as
            a floating overlay (position:absolute, bordered card with
            box-shadow, fixed pixel width) — Step 9 forbids replicating
            overlays as push-down. Affiliation text is already extracted
            from the HTML source by `_parse_authors`.
    """
    html = neutralize_media_queries(html)

    # Step 2 — OneTrust cookie consent banner + overlay. Backup DOM
    # removal in case publishers set inline styles that override CSS.
    for eid in ("onetrust-consent-sdk", "onetrust-banner-sdk",
                "onetrust-pc-sdk"):
        while True:
            new = remove_elements_by_id(html, eid)
            if new == html:
                break
            html = new
    while True:
        prev = html
        html = remove_elements_by_selector(html, "onetrust-pc-dark-filter")
        if html == prev:
            break

    if "</head>" in html:
        html = html.replace("</head>", _OVERRIDE_CSS + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", _OVERRIDE_CSS + r"\1", html, count=1)
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata from the BMJ Highwire2 citation_* meta tags.

    Returns dict with title/journal/year/volume/issue/pages/doi. early-access
    BMJ articles (e.g. emj /content/early/...) lack volume/issue/firstpage/
    lastpage tags — those fields fall back to "".
    """
    title = get_meta(html, "citation_title")
    journal = (get_meta(html, "citation_journal_abbrev")
               or get_meta(html, "citation_journal_title"))
    volume = get_meta(html, "citation_volume")
    issue = get_meta(html, "citation_issue")
    doi = format_doi(get_meta(html, "citation_doi"))

    date = (get_meta(html, "citation_publication_date")
            or get_meta(html, "citation_date")
            or get_meta(html, "DC.Date"))
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage
    if not pages:
        # New BMJ Next.js frontend (e.g. jitc.bmj.com) doesn't emit
        # citation_firstpage / citation_lastpage; the article-number
        # e-locator (`e010179`) is the last segment of citation_id
        # (`<vol>/<issue>/<elocator>`) or the saved-from URL path.
        cid = get_meta(html, "citation_id")
        if cid and "/" in cid:
            pages = cid.rsplit("/", 1)[-1]
        if not pages:
            url_m = re.search(
                r"Page saved with SingleFile\s+url:\s*(\S+)", html[:2000],
            )
            if url_m:
                tail = url_m.group(1).rstrip("/").rsplit("/", 1)[-1]
                if re.match(r"^[a-zA-Z]?\d+$", tail):
                    pages = tail

    return {
        "title": title,
        "journal": journal.rstrip(".") if journal else "",
        "year": year,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_body_affiliations(html, authors):
    """Map body-side <li class=contributor> xref-aff links to affiliations.

    BMJ's body affiliation list is <li class=aff><a id=aff-N></a><address>...
    </address>, with each contributor referencing its affiliations via
    <a class=xref-aff href=#aff-N> links inside <li class=contributor
    id=contrib-N><span class=name>Full Name</span> ...>.
    """
    aff_map = {}
    for am in re.finditer(
        r"<li class=aff><a id=(aff-\d+)[^>]*></a>\s*<address>(.*?)</address>",
        html, re.DOTALL,
    ):
        aff_id = am.group(1)
        text = strip_tags(am.group(2)).strip()
        text = re.sub(r"^\d+\s*", "", text).strip().rstrip(";")
        if text:
            aff_map[aff_id] = text

    if not aff_map:
        return authors

    # BMJ does not close <li class=contributor> tags; iterate by next-li or
    # </ol> as a sentinel.
    contribs = list(re.finditer(
        r"<li class=contributor id=contrib-\d+>(.*?)(?=<li class=contributor|</ol>)",
        html, re.DOTALL,
    ))
    for i, contrib in enumerate(contribs):
        if i >= len(authors):
            break
        entry = contrib.group(1)
        aff_ids = re.findall(r"class=xref-aff href=#(aff-\d+)", entry)
        affs = [aff_map[aid] for aid in aff_ids if aid in aff_map]
        if not affs and len(aff_map) == 1:
            affs = list(aff_map.values())
        if affs:
            authors[i]["affiliation"] = affs

    return authors


def _parse_jsonld_authors(html):
    """Extract authors+affiliations from a `<script type=application/ld+json>`
    block on the new BMJ Next.js frontend (jitc.bmj.com etc.).

    Returns list of {"name": str, "affiliations": [str, ...]}, or [] when
    no JSON-LD ScholarlyArticle / NewsArticle / MedicalScholarlyArticle
    block carries a populated `author` array.
    """
    def _extract_aff_names(aff):
        if isinstance(aff, list):
            out = []
            for x in aff:
                if isinstance(x, dict) and x.get("name"):
                    out.append(x["name"])
                elif isinstance(x, str):
                    out.append(x)
            return out
        if isinstance(aff, dict) and aff.get("name"):
            return [aff["name"]]
        if isinstance(aff, str):
            return [aff]
        return []

    def _walk(node):
        if isinstance(node, dict):
            v = node.get("author")
            if (
                isinstance(v, list) and v
                and isinstance(v[0], dict) and v[0].get("name")
            ):
                return v
            for sub in node.values():
                found = _walk(sub)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _walk(item)
                if found:
                    return found
        return None

    for sm in re.finditer(
        r'<script[^>]*type=["\']?application/ld\+json["\']?[^>]*>(.+?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(sm.group(1))
        except Exception:
            continue
        raw = _walk(data)
        if not raw:
            continue
        out = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            name = a.get("name")
            if not name:
                continue
            out.append({
                "name": name,
                "affiliations": _extract_aff_names(a.get("affiliation")),
            })
        if out:
            return out
    return []


def _parse_authors(html):
    """Extract authors with affiliations.

    Primary source is the citation_author / citation_author_institution meta
    block (parse_meta_authors). When meta affiliations are absent or empty,
    fall back to mapping body contributor xref-aff links to <li class=aff>
    addresses. Author display names are routed through format_author_name
    (which delegates to parse_combined_name + format_name in _helpers) to
    preserve compound surnames and hyphenated initials.

    Newer BMJ properties (e.g. jitc.bmj.com on Next.js) emit no
    citation_author meta and no Highwire2 contributor list. They instead
    ship a JSON-LD ScholarlyArticle node whose `author` array carries
    each `{name, affiliation: [...]}` pair — used as the second-tier
    fallback before failing back to nameless DC.Contributor list.
    """
    meta_authors = parse_meta_authors(html)
    authors = [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in meta_authors
    ]
    if authors:
        if any(a["affiliation"] for a in authors):
            return authors
        return _parse_body_affiliations(html, authors)

    jsonld = _parse_jsonld_authors(html)
    if jsonld:
        return [
            {
                "author": format_author_name(a["name"]),
                "affiliation": a.get("affiliations", []),
            }
            for a in jsonld
        ]

    # Last resort: DC.Contributor names with no affiliation data — the
    # legacy Highwire2 and modern Next.js frontends both emit these.
    dc_names = re.findall(
        r'<meta\b[^>]*\bname=(?:"DC\.Contributor"|\'DC\.Contributor\'|DC\.Contributor)'
        r'[^>]*\bcontent=("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
        html, re.IGNORECASE,
    )
    if dc_names:
        return [
            {
                "author": format_author_name(unescape(g[1] or g[2] or g[3])),
                "affiliation": [],
            }
            for g in dc_names
        ]
    return authors


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _parse_nextjs_references(html):
    """Extract references from BMJ's Next.js frontend (jitc.bmj.com etc.).

    Each entry is `<li data-testid=reference-item-ref-N>` containing:
      - One or more `<span data-testid=author-K>Last IN, </span>` spans
        (followed by `<span data-testid=authors-etal>et al. </span>` for
        long author lists).
      - `<span data-testid=reference-title>Title. </span>`.
      - A trailing `<span class=text-bmj-silver-800>` whose nested
        `<span>` siblings hold the journal, year (`YYYY; `), and a
        `vol:firstpage[–lastpage]` segment in that order.
      - `<a data-testid=link-doi href=…>doi:…</a>` (when present).
    """
    refs = []
    starts = list(re.finditer(
        r'data-testid="?reference-item-ref-(\d+)\b', html,
    ))
    if not starts:
        return refs
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else m.start() + 8000
        entry = html[m.start():end]

        # Authors: data-testid=author-N spans, comma-and-space-trimmed.
        authors = []
        for am in re.finditer(
            r'data-testid="?author-\d+"?[^>]*>([^<]+)</span>',
            entry,
        ):
            name = unescape(am.group(1)).strip().rstrip(",").strip()
            if name:
                authors.append(format_author_name(name))

        # Title: data-testid=reference-title (may contain inline tags).
        title = ""
        tm = re.search(
            r'data-testid="?reference-title"?[^>]*>(.*?)</span>',
            entry, re.DOTALL,
        )
        if tm:
            title = strip_tags(tm.group(1)).strip().rstrip(".").strip()
            title = re.sub(r"\s+", " ", title)

        # Journal / year / vol:pages — three sibling <span> blocks
        # inside the metadata wrapper that appears AFTER the title.
        journal = year = volume = pages = ""
        meta_block = ""
        if tm:
            tail = entry[tm.end():]
            mb = re.search(
                r'<span\s+class=["\']?text-bmj-silver-800["\']?[^>]*>(.*?)</span>\s*</section>',
                tail, re.DOTALL,
            )
            if not mb:
                mb = re.search(
                    r'<span\s+class=["\']?text-bmj-silver-800["\']?[^>]*>(.*?)</span>\s*<div',
                    tail, re.DOTALL,
                )
            if mb:
                meta_block = mb.group(1)
        if meta_block:
            # Strip tags and parse the plain-text trail. Some references
            # nest the journal/year/vol:pages each in their own <span>
            # (`<span>N Engl J Med</span> <span>2005; </span>
            # <span>353:2747–57</span>.`); others inline the vol:pages
            # as bare text outside the spans (`<span>JAMA Netw Open
            # </span> <span>2020; </span>3.`). A flat-text parse handles
            # both, letting the journal name include all chars before
            # the 4-digit year.
            plain = re.sub(r'<[^>]+>', ' ', meta_block)
            plain = re.sub(r'\s+', ' ', unescape(plain)).strip()
            # Collapse the " , " / " ;" sequences that the inter-tag
            # whitespace introduces.
            plain = re.sub(r'\s+([,;:.])', r'\1', plain)
            ym = re.search(r'\b(\d{4})\b', plain)
            if ym:
                year = ym.group(1)
                journal = plain[:ym.start()].rstrip(" ;,.").strip()
                tail = plain[ym.end():].lstrip(" ;,").strip()
                vm = re.match(
                    r'^(\d+)\s*:\s*([\dA-Za-z]+(?:\s*[-–—]\s*[\dA-Za-z]+)?)',
                    tail,
                )
                if vm:
                    volume = vm.group(1)
                    pages = re.sub(r'[–—]', '-', vm.group(2)).strip()
                else:
                    # Volume-only (no pages) form: "; 3." (online-first
                    # journals not yet paginated).
                    only_vol = re.match(r'^(\d+)\b', tail)
                    if only_vol:
                        volume = only_vol.group(1)
            else:
                # No 4-digit year — likely a non-journal citation
                # (book / report). Treat the first chunk before the
                # comma as the publisher/source.
                journal = plain.split(',', 1)[0].strip().rstrip(".")

        # DOI from data-testid=link-doi (anchor attributes appear in
        # either order: data-testid first or href first).
        doi = ""
        am = re.search(
            r'<a\b[^>]*\bdata-testid=["\']?link-doi["\']?[^>]*>', entry,
        )
        if am:
            hm = re.search(
                r'\bhref=("([^"]+)"|\'([^\']+)\'|([^\s>]+))', am.group(0),
            )
            if hm:
                href = unescape(hm.group(2) or hm.group(3) or hm.group(4))
                # The link-doi anchor uses `http://dx.doi.org/<id>`;
                # format_doi only normalizes bare DOIs and `https://
                # doi.org/...` URLs, so strip the dx-prefixed URL form
                # down to the bare DOI before normalising.
                href = re.sub(
                    r'^https?://(?:dx\.)?doi\.org/', '', href,
                )
                doi = format_doi(href)

        if not title and not authors and not doi:
            # Skip empties (the regex can match a stray fragment).
            continue

        refs.append({"": {
            "title": title,
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": "",
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


def _parse_references(html):
    """Extract the reference list from BMJ's Highwire2 cit-list block.

    Each entry: <div class="cit ref-cit ..." data-doi=...>
        <ol class=cit-auth-list>
            <li><span class=cit-auth>
                <span class=cit-name-surname>Last</span>
                <span class=cit-name-given-names>Initials</span>
            </span>
            ...
        </ol>
        <cite>. <span class=cit-article-title>Title</span>.
            <abbr class=cit-jnl-abbrev>Journal</abbr>
            <span class=cit-pub-date>Year</span>;
            <span class=cit-vol>Vol</span>:
            <span class=cit-fpage>FP</span>-<span class=cit-lpage>LP</span>.
            <a href=...>doi:...</a>
        </cite>

    Falls back to the Next.js frontend's `data-testid=reference-item-ref-N`
    blocks (jitc.bmj.com etc.) when the Highwire2 markers are absent.
    """
    refs = []
    m = re.search(r'class="?cit-list\b', html)
    if not m:
        return _parse_nextjs_references(html)

    ref_html = html[m.start():]
    ref_starts = [
        rm.start() for rm in re.finditer(r'<div class="?cit ref-cit\b', ref_html)
    ]
    if not ref_starts:
        return refs

    for i, start in enumerate(ref_starts):
        end = ref_starts[i + 1] if i + 1 < len(ref_starts) else start + 8000
        entry = ref_html[start:end]

        # --- Authors (structured surname/given-names spans) ---
        authors = []
        for am in re.finditer(
            r"<span class=cit-name-surname>([^<]*)</span>\s*"
            r"<span class=cit-name-given-names>([^<]*)</span>",
            entry,
        ):
            surname = unescape(am.group(1)).strip().rstrip(",")
            given = unescape(am.group(2)).strip().rstrip(".")
            initials = given.replace(".", "").replace(" ", "")
            authors.append(f"{surname} {initials}" if initials else surname)

        def _cit_field(cls):
            """Return text inside the first <span/abbr class=CLS> in entry."""
            fm = re.search(rf'class="?{cls}"?[^>]*>([^<]*)', entry)
            return unescape(fm.group(1)).strip() if fm else ""

        # --- Title (cit-article-title may carry inline tags like <em>) ---
        title = ""
        title_span = re.search(
            r'class="?cit-article-title"?[^>]*>(.*?)</span>',
            entry, re.DOTALL,
        )
        if title_span:
            title = strip_tags(title_span.group(1)).strip()
            title = re.sub(r"\s+", " ", title)

        # --- Journal (abbr.cit-jnl-abbrev preferred, span.cit-source fallback) ---
        journal = _cit_field("cit-jnl-abbrev") or _cit_field("cit-source")
        journal = journal.rstrip(".")

        # --- Year, volume, pages ---
        year = _cit_field("cit-pub-date").rstrip(".")
        volume = _cit_field("cit-vol")
        fpage = _cit_field("cit-fpage")
        lpage = _cit_field("cit-lpage")
        pages = f"{fpage}-{lpage}" if fpage and lpage else fpage
        if not pages and lpage:
            pages = lpage

        # --- DOI ---
        doi = ""
        dm = re.search(r"data-doi=([^\s>]+)", entry)
        if dm:
            doi = format_doi(unescape(dm.group(1).strip('"')))

        if not title and not authors:
            title = strip_tags(entry).strip()

        refs.append({"": {
            "title": title,
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": "",
            "pages": pages,
            "doi": doi,
            "authors": authors,
        }})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _find_h2_headings(html):
    """Return [(start_pos, text), ...] for all <h2> elements in html."""
    entries = []
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL):
        text = strip_tags(m.group(1)).strip()
        if text:
            entries.append((m.start(), text))
    return entries


def _bmj_extract_captions(html):
    """Replace BMJ fig/table wrappers with their caption text.

    BMJ uses <div id=F\\d+ class="fig ...">...<div class=fig-caption>CAP</div>
    ...</div> for figures and <div id=T\\d+ class="table ...">...<div
    class=table-caption>CAP</div></div> for tables (table content is loaded
    via AJAX so the static HTML carries only the caption). The wrapper also
    holds an <a> tag whose data-figure-caption attribute embeds raw HTML
    (<sup>2</sup>>0.4), so leaving the block for tags_to_text would leak
    attribute soup once the regex tag matcher hits the inner '>'. Pulling
    the clean fig-caption / table-caption div before main text processing
    avoids that.
    """
    def _replace_wrapper(html, start_re, caption_class):
        out = []
        pos = 0
        for m in re.finditer(start_re, html):
            if m.start() < pos:
                continue
            depth = 1
            scan = m.end()
            while depth > 0 and scan < len(html):
                no = re.search(r"<div\b", html[scan:])
                nc = re.search(r"</div>", html[scan:])
                if nc is None:
                    break
                if no and no.start() < nc.start():
                    depth += 1
                    scan += no.end()
                else:
                    depth -= 1
                    scan += nc.end()
            block = html[m.start():scan]
            cap_m = re.search(
                rf'<div class={caption_class}\b[^>]*>(.*?)</div>',
                block, re.DOTALL,
            )
            cap_text = ""
            if cap_m:
                inner = cap_m.group(1)
                # Drop the trailing <div class="sb-div caption-clear"> open
                inner = re.sub(r'<div class="sb-div[^"]*"\s*>\s*$', "", inner)
                cap_text = "\n" + inner + "\n"
            out.append(html[pos:m.start()])
            out.append(cap_text)
            pos = scan
        out.append(html[pos:])
        return "".join(out)

    html = _replace_wrapper(
        html, r'<div id=F\d+\s+class="fig\b', "fig-caption",
    )
    html = _replace_wrapper(
        html, r'<div id=T\d+\s+class="table\b', "table-caption",
    )
    return html


def _parse_main_text(html):
    """Extract body text from the BMJ abstract-view + fulltext-view containers.

    BMJ splits the article DOM into two sibling <div class="article ..."> blocks:
    abstract-view (structured Abstract + keywords) followed by fulltext-view
    (boxed-text "WHAT IS ALREADY KNOWN" + Introduction through Acknowledgments
    + supplementary). Slice from the start of abstract-view through the first
    References h2, dropping references and keeping supplementary sections that
    follow. BMJ-specific caption extraction replaces fig/table wrappers with
    their fig-caption / table-caption text before strip_common (which would
    erase the caption along with the rest of the wrapper HTML). Pipeline:
    _bmj_extract_captions -> strip_common -> tags_to_text -> drop_noise.
    """
    abs_m = re.search(r'<div class="article[^"]*abstract-view[^"]*"[^>]*>', html)
    full_m = re.search(r'<div class="article[^"]*fulltext-view[^"]*"[^>]*>', html)
    if not full_m and not abs_m:
        m = re.search(r'class="article[^"]*"[^>]*>', html)
        if not m:
            return ""
        content = html[m.end():]
        content_offset = 0
    else:
        start_pos = abs_m.start() if abs_m else full_m.start()
        content = html[start_pos:]
        content_offset = 0

    h2s = _find_h2_headings(content)
    if not h2s:
        return ""

    start = 0

    first_ref_idx = None
    for i, (pos, text) in enumerate(h2s):
        if _REF_RE.search(text) and pos >= start:
            first_ref_idx = i
            break

    parts = []
    first_body_h2 = h2s[0][0]
    if first_body_h2 > start:
        parts.append((start, first_body_h2))

    for i, (pos, text) in enumerate(h2s):
        if pos < start:
            continue
        if _REF_RE.search(text):
            continue
        if _CHROME_RE.search(text.strip()):
            continue
        end = h2s[i + 1][0] if i + 1 < len(h2s) else len(content)
        if first_ref_idx is None or i < first_ref_idx:
            parts.append((pos, end))
        else:
            if _SUPP_RE.search(text):
                parts.append((pos, end))

    if not parts:
        return ""

    body_html = ""
    for s, e in parts:
        body_html += content[s:e]

    body_html = _bmj_extract_captions(body_html)
    body_html = strip_common(body_html)
    text = tags_to_text(body_html)
    return drop_noise(text, _NOISE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse BMJ HTML into a papers/*.json-format dict."""
    meta = _parse_metadata(html)
    return {
        "title": meta["title"],
        "journal": meta["journal"],
        "year": meta["year"],
        "volume": meta["volume"],
        "issue": meta["issue"],
        "pages": meta["pages"],
        "doi": meta["doi"],
        "authors": _parse_authors(html),
        "main_text": _parse_main_text(html),
        "references": _parse_references(html),
    }

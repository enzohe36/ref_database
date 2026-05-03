"""Royal Society of Chemistry (rsc.org) HTML parser."""

import re
from html import unescape

from ._helpers import (
    _remove_nested_element,
    drop_noise,
    extract_captions,
    format_author_name,
    format_doi,
    get_meta,
    parse_meta_authors,
    remove_elements_by_id,
    strip_common,
    strip_tags,
    tags_to_text,
    remove_elements_by_selector,
)

# Publisher-specific noise strings removed from main_text
_NOISE = ()

# Reference section title pattern
_REF_RE = re.compile(r"\breferences\b", re.IGNORECASE)

# Supplementary section patterns (kept after first references)
_SUPP_RE = re.compile(
    r"supplement|extended data|source data|expanded view|powerpoint|appendix",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Banner removal
# ---------------------------------------------------------------------------

def remove_banners(html):
    """Normalize RSC HTML to a single centered text column.

    Chrome stripped (Step 3):
      - <header class=pubs-header> (top navigation) and
        <footer class=rsc-footer> (site footer).
      - OneTrust cookie banner "This site uses cookies"
        (id=onetrust-consent-sdk and sibling banner/pc/cookie-footer
        wrappers).
      - Accessibility skip-to-content affordance (class=skipto-control),
        "From the journal" article-nav banner (class=article-nav),
        sliding site-menu drawer (class=pubs-nav-drawer).
      - Ad slot below the article (id=pbgrd-mpur-c) and the secondary
        layout panel (class=layout__panel--secondary, which holds
        Download/Citation/BibTex/Permissions/Cited-by).
      - Stray <br> between the a11y-announcer and the article content
        (#maincontent > br), which renders as an 18-px blank line.

    Reading column: <article class=article-control> wraps the landing
    page's title, authors, capsule image, and abstract. Cap at 752 px
    with 56 px top/bottom + 16 px side padding.

    Layout quirks:
      - RSC's stylesheet reserves 16 px for a non-overlay scrollbar,
        shrinking body to 704 px at vw=720. Force
        `overflow-y: overlay` + zero-width WebKit scrollbar to reclaim
        those pixels.
      - `<div class=layout-control>` uses `display:flex` with a 60/40
        split; the primary column itself floats. Collapse every layout
        ancestor (main/.viewport/.layout-control/.layout__panel--primary/
        .layout__content*) to plain block + 100% width + zero padding,
        so the article wrapper's own padding is the only contributor to
        the reading margins.
      - Several elements have unquoted class= attributes (from
        SingleFile minification), so `remove_elements_by_selector`
        misses them. Strip via `_remove_nested_element` with a regex
        that tolerates both quoted and unquoted class=.
    """
    # -------------------------------------------------------------------
    # Step 3 — strip chrome.
    # -------------------------------------------------------------------
    html = _remove_nested_element(html, r"<header\b[^>]*>")
    html = _remove_nested_element(html, r"<footer\b[^>]*>")
    html = remove_elements_by_id(
        html,
        "onetrust-consent-sdk",
        "onetrust-banner-sdk",
        "onetrust-pc-sdk",
    )
    # Unquoted class attrs — tolerate both forms. `article-nav` is the
    # "From the journal: <Journal Name>" banner + journal cover image
    # that sits at the top of the reading column; keep it inside the
    # wrapper rather than strip.
    for cls in (
        "rsc-onetrust-cookie-footer",
        "skipto-control",
        "pubs-nav-drawer",
    ):
        html = _remove_nested_element(
            html,
            rf'<div\b[^>]*\bclass=["\']?{re.escape(cls)}\b[^>]*>',
        )

    # Strip the entire `<div class="c fixpadv--l">` grid that wraps
    # the "Social activity" heading + altmetric badge. This is the
    # last fixpadv--l in #divAbout; my previous strip cut from the
    # c__10 (Social activity) heading through the next `</div>` but
    # left the empty grid wrapper behind contributing 16 px to B.
    # The opening tag matches `<div class="c fixpadv--l">` followed
    # (after whitespace) by `<div class="c__10"><h3>Social activity`.
    html = re.sub(
        r'<div\b[^>]*\bclass=["\']?c fixpadv--l["\']?[^>]*>\s*'
        r'<div\b[^>]*\bclass=["\']?c__10["\']?[^>]*>\s*'
        r'<h3[^>]*>\s*Social activity\s*</h3>'
        r'.*?</div>\s*</div>\s*</div>',
        '', html, count=1, flags=re.DOTALL | re.IGNORECASE,
    )
    # Strip Tweet/Share buttons row (`<div class="c c--gap-xs
    # fixpadb--xl">` directly under the About tab).
    for _ in range(3):
        before = html
        html = _remove_nested_element(
            html,
            r'<div\b[^>]*\bclass=["\']?c c--gap-xs[^"\'>]*\bfixpadb--xl\b',
        )
        if html == before:
            break
    # Strip "Search articles by author" form rail.
    html = _remove_nested_element(
        html, r'<form\b[^>]*\bid=["\']?SearchByAuthor\b',
    )
    # Strip Spotlight + Advertisements sections that sit between the
    # `.layout-control` article wrapper and the page bottom (siblings
    # of `.layout-control` inside `.viewport`). Each is a
    # `<section class="layout__content layout__content--padded ...">`
    # whose first heading is "Spotlight" or "Advertisements".
    html = re.sub(
        r'<section\b[^>]*\bclass=["\']?[^"\'>]*\blayout__content--padded\b[^>]*>'
        r'\s*<h3[^>]*>\s*(?:Spotlight|Advertisements)\s*</h3>'
        r'.*?</section>',
        '', html, flags=re.DOTALL | re.IGNORECASE,
    )

    # -------------------------------------------------------------------
    # Steps 2 + 4 — layout freeze and reading-column cap.
    # -------------------------------------------------------------------
    override = (
        "<style>"
        # Reclaim the 16 px non-overlay scrollbar gutter so body renders
        # 720 at vw=720 instead of 704.
        "html{overflow-y:overlay !important}"
        "html::-webkit-scrollbar{width:0 !important;height:0 !important}"
        # Layout freeze (Step 2).
        "html{width:100% !important;max-width:100% !important;"
        "margin:0 !important;background:#fff !important}"
        "body{width:100% !important;min-width:0 !important;"
        "max-width:752px !important;margin:0 auto !important;"
        "background:#fff !important}"
        # Hide the stray <br> between the a11y-announcer and the
        # .viewport wrapper — it renders as an 18-px blank line above
        # the article.
        "#maincontent>br{display:none !important}"
        # Collapse layout ancestors (RSC ships <div class=layout-control>
        # as a flex row with a 60/40 primary/secondary split). Force
        # plain block + 100% width so the wrapper's own padding
        # is the only contributor to the reading margins.
        "main,#maincontent,.viewport,.layout-control,"
        ".layout__content,.layout__content--padded{"
        "display:block !important;float:none !important;"
        "width:100% !important;max-width:100% !important;"
        "min-width:0 !important;flex:0 0 auto !important;"
        "margin:0 !important;padding:0 !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        # Hide the ad slot trailing the article (kept the secondary
        # panel — it holds the About / Cited by / Related tabs +
        # Download / Citation / Permissions metadata block, which is
        # article metadata, not chrome). With the layout-control flex
        # collapsed to plain block above, the secondary panel now
        # stacks vertically below the primary instead of sitting in
        # a sidebar.
        # `.layout__content--padded.text--centered` sections at the
        # bottom of the secondary panel host "Spotlight" + Google
        # ad slots ("Advertisements"). Site-chrome.
        "section#pbgrd-mpur-c,"
        ".layout__content--padded.text--centered{display:none !important}"
        # Non-active tab panels (Cited by / Related) — JS would show
        # them on click, but in the static capture they render as
        # leftover blocks below the visible About panel. Force-hide
        # any tab__panel without `.open`. Specificity: the universal
        # `div{display:block}` rule has 0,0,1; ours is 0,1,1.
        ".tab__panel:not(.open){display:none !important}"
        # Capped reading column (Step 4) on the primary layout panel —
        # it wraps the `article-nav` "From the journal: ..." banner and
        # the `article.article-control` block (title, authors, capsule
        # image, abstract) as siblings inside
        # `section.layout__content--padded`. The secondary panel
        # (About / Cited by / Related tabs + DOI / Submitted /
        # Accepted / DownloadCitation / Permissions metadata) stacks
        # below; share the same 752/16 cap and zero its top padding so
        # primary's 56-px pb is the only gap between them. Keep
        # secondary's pb=56 as the document's bottom margin.
        ".layout__panel--primary{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        ".layout__panel--secondary{"
        "float:none !important;display:block !important;"
        "width:auto !important;max-width:752px !important;"
        "margin:0 auto !important;padding:0 16px 56px 16px !important;"
        "box-sizing:border-box !important;background:#fff !important}"
        ".layout__panel--primary *,.layout__panel--secondary *{"
        "max-width:100% !important;min-width:0 !important}"
        # Direct-child scope only. The descendant form zeroed padding
        # on nested children — notably inside `.article-nav`'s journal
        # list item, compressing the "From the journal: Nanoscale"
        # block from 125 px to 87 px and narrowing the gap below
        # "Issue 17, 2014".
        ".layout__panel--primary > *:first-child{"
        "margin-top:0 !important;padding-top:0 !important}"
        ".layout__panel--primary > *:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        # The secondary panel's last visible block is the
        # `<a id=requestPermission>` "Request permissions" button inside
        # `<div class="alp-request-permissions">`. Cascading bottom
        # padding/margin (c.fixpadt--l, fixpadv--m, the alp wrapper)
        # plus the panel's pb=56 used to give B≈100. Zero the
        # descendant trailing margins inside the secondary panel
        # (descendant margin-only — SKILL.md pitfall #1: descendant
        # padding-zero would kill `tab__btn`'s native pb=20 and
        # collapse the tab nav row from 58 → 38 px).
        ".layout__panel--secondary > *:last-child{"
        "margin-bottom:0 !important;padding-bottom:0 !important}"
        ".layout__panel--secondary *:last-child{"
        "margin-bottom:0 !important}"
        # `fixpadv--m` (the wrapper around `#divAbout`) ships with
        # `padding:12px 0` — the bottom 12 px would otherwise add to
        # the panel's 56-px pb, pushing B to ~68. Zero its pb only
        # (keep pt=12 to preserve the gap above the article-info
        # block).
        ".layout__panel--secondary .fixpadv--m{"
        "padding-bottom:0 !important}"
        # `autopad--h` (the inner wrapper inside `#divAbout`) ships
        # with `padding:0 12px`, indenting article-info content 12 px
        # right of the panel's 16-px content edge — making text sit
        # at L=28 instead of L=16 (where tab_nav and primary-panel
        # text live). Zero so secondary content lines up with primary.
        ".layout__panel--secondary .autopad--h{"
        "padding-left:0 !important;padding-right:0 !important}"
        # The capsule article image and crossmark button float right on
        # native — un-float so they don't push text off-axis.
        ".layout__panel--primary .capsule__article-image,"
        ".layout__panel--primary .crossmark-button{float:none !important}"
        # The `* { min-width:0; max-width:100% }` rule above collapses
        # the journal-thumbnail table-cell + img to 16 px because the
        # min-width:0 lets table-cell shrink-to-fit ignore the img's
        # intrinsic size. Restore native sizing by undoing the
        # universal min/max-width on the cell and img — they then
        # render at the publisher's natural dimensions: cell sized by
        # the img's HTML `height=88` attribute + 16-px padding-right
        # (~83 px wide).
        ":root .layout__panel--primary .list__image-col,"
        ":root .layout__panel--primary .list__item-img{"
        "min-width:auto !important;max-width:none !important}"
        # Figures: rsc wraps each figure in
        #   <div class=img-tbl id=fig<N>>
        #     <figure class=img-tbl__image>
        #       <a href=https://pubs.rsc.org/image/article/.../<id>-f<N>_hi-res.gif>
        #         <img src=data:image/gif (thumbnail) data-original=<medium url>>
        #       </a>
        #       <figcaption class=img-tbl__caption>...</figcaption>
        #     </figure>
        #   </div>
        # Native order is image above caption (correct). The img is a
        # tiny placeholder (~0-50 KB); the high-res GIF URL is on the
        # parent <a href>. get_refs.py uses `_RSC_FIGURES_FIX_JS` to
        # swap <img src> ← <a href> at capture time. Force the wrapper
        # and img to fill the column width with the standard 5-px gap
        # between img and figcaption.
        ":root .layout__panel--primary .img-tbl{"
        "margin:1rem 0 !important;padding:0 !important;"
        "width:100% !important;max-width:100% !important}"
        ":root .layout__panel--primary figure.img-tbl__image{"
        "display:block !important;margin:0 !important;padding:0 !important}"
        ":root .layout__panel--primary figure.img-tbl__image > a{"
        "display:block !important;margin:0 !important;padding:0 !important}"
        ":root .layout__panel--primary figure.img-tbl__image > a > img{"
        "display:block !important;width:100% !important;"
        "height:auto !important;max-width:100% !important;"
        "margin:0 0 5px 0 !important}"
        "</style>"
    )
    if "</head>" in html:
        html = html.replace("</head>", override + "</head>", 1)
    else:
        html = re.sub(r"(<body\b)", override + r"\1", html, count=1)
    return html


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def _parse_metadata(html):
    """Extract bundled metadata: title, journal, year, volume, issue, pages, doi.

    Returns dict with those 7 keys. Each field's output format:
      - title: str
      - journal: ISO abbreviation without trailing period
      - year: 4-digit string
      - volume, issue: str (may be empty)
      - pages: "firstpage-lastpage" or firstpage alone
      - doi: "https://doi.org/..." URL
    """
    date = get_meta(html, "citation_publication_date") or get_meta(html, "citation_online_date")
    year = ""
    if date:
        m = re.search(r"(\d{4})", date)
        if m:
            year = m.group(1)

    firstpage = get_meta(html, "citation_firstpage")
    lastpage = get_meta(html, "citation_lastpage") if firstpage else ""
    pages = f"{firstpage}-{lastpage}" if lastpage else firstpage

    journal = get_meta(html, "citation_journal_abbrev") or get_meta(html, "citation_journal_title")
    if journal:
        journal = re.sub(r"  +", " ", journal.replace(".", "")).strip()
    else:
        journal = ""

    return {
        "title": get_meta(html, "citation_title"),
        "journal": journal,
        "year": year,
        "volume": get_meta(html, "citation_volume"),
        "issue": get_meta(html, "citation_issue"),
        "pages": pages,
        "doi": format_doi(get_meta(html, "citation_doi")),
    }


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------

def _parse_authors(html):
    """Extract authors with affiliations.

    Returns list of {"author": "LastName IN", "affiliation": [str, ...]}.
    RSC citation_author meta tags use 'Given Last' form; format_author_name
    handles the flip via parse_combined_name + format_name.
    """
    return [
        {
            "author": format_author_name(a["name"]),
            "affiliation": a.get("affiliations", []),
        }
        for a in parse_meta_authors(html)
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

def _flip_initials_first(name):
    """Convert 'F. M. LastName' to 'LastName FM' via shared helpers."""
    return format_author_name(name)


def _parse_citation_reference(content):
    """Parse a single citation_reference meta tag content string.

    RSC format: 'citation_title=...; citation_author=A; citation_author=B;
    citation_journal_title=X; citation_volume=Y; citation_pages=FP-LP;
    citation_publication_date=YYYY;'

    Field separators are ';' optionally followed by whitespace/newlines.
    Returns dict {title, journal, year, volume, issue, pages, doi, authors}.
    """
    fields = {}
    author_parts = []
    for part in re.split(r";\s*", content):
        part = part.strip()
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "citation_author":
            author_parts.append(val)
        else:
            fields[key] = val

    if not fields and not author_parts:
        # Freeform fallback: store full text as title
        return {
            "title": content.strip(),
            "journal": "",
            "year": "",
            "volume": "",
            "issue": "",
            "pages": "",
            "doi": "",
            "authors": [],
        }

    authors = [_flip_initials_first(a) for a in author_parts if a]

    pages = fields.get("citation_pages", "")
    if not pages:
        fp = fields.get("citation_first_page", "")
        lp = fields.get("citation_last_page", "")
        pages = f"{fp}-{lp}" if lp else fp
    pages = pages.replace("\u2013", "-").replace("\u2014", "-")

    journal = fields.get("citation_journal_title", "")
    journal = re.sub(r"\s+", " ", journal).strip().rstrip(".")

    year = fields.get("citation_publication_date", "")
    if year:
        m = re.search(r"(\d{4})", year)
        year = m.group(1) if m else year

    return {
        "title": fields.get("citation_title", "").strip(),
        "journal": journal,
        "year": year,
        "volume": fields.get("citation_volume", ""),
        "issue": fields.get("citation_issue", ""),
        "pages": pages,
        "doi": format_doi(fields.get("citation_doi", "")),
        "authors": authors,
    }


def _parse_references(html):
    """Extract the reference list from citation_reference meta tags."""
    refs = []
    for m in re.finditer(
        r'<meta[^>]*name=["\']?citation_reference["\']?'
        r'[^>]*content="([^"]*)"'
        r'|<meta[^>]*content="([^"]*)"'
        r'[^>]*name=["\']?citation_reference["\']?',
        html,
    ):
        content = unescape(m.group(1) or m.group(2) or "")
        ref = _parse_citation_reference(content)
        refs.append({"": ref})
    return refs


# ---------------------------------------------------------------------------
# Main text
# ---------------------------------------------------------------------------

def _parse_abstract(html):
    """Extract abstract from <div class=capsule__text>.

    The capsule__text div sits inside capsule__column-wrapper and contains
    the article abstract as one or more <p> elements.
    """
    m = re.search(
        r'<div[^>]*class=["\']?capsule__text[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    if not m:
        return ""
    return strip_tags(m.group(1)).strip()


def _parse_main_text(html):
    """Extract body text.

    RSC HTML landing pages contain only the abstract; full body text is
    paywalled. The convert_html.py pipeline will fall back to PMC when
    main_text is short. We still emit the abstract so that papers without
    PMC fallbacks have at least the summary content available.
    """
    abstract = _parse_abstract(html)
    if not abstract:
        return ""
    return f"## Abstract\n\n{abstract}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_article(html):
    """Parse RSC HTML into a papers/*.json-format dict."""
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

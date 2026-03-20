import datetime
import html
import math
import smtplib
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from loguru import logger
from tqdm import tqdm

from paper import ArxivPaper, BiorxivPaper, JournalPaper

CARD_BACKGROUNDS = ("#f9f9f9", "#f1f1f1")

framework = """
<!DOCTYPE HTML>
<html>
<head>
  <style>
    body {
      font-family: Arial, sans-serif;
      color: #333;
    }
    .star-wrapper {
      font-size: 1.15em;
      line-height: 1;
      display: inline-flex;
      align-items: center;
      vertical-align: middle;
    }
    .half-star {
      display: inline-block;
      width: 0.5em;
      overflow: hidden;
      white-space: nowrap;
      vertical-align: middle;
    }
    .full-star {
      vertical-align: middle;
    }
    .section-title {
      font-size: 28px;
      margin: 24px 0 12px 0;
    }
    .outline-title {
      font-size: 28px;
      margin: 0 0 12px 0;
    }
    .outline-group-title {
      font-size: 18px;
      font-weight: bold;
      margin: 14px 0 6px 0;
    }
  </style>
</head>
<body>

<div style="margin-bottom: 24px;">
  <div class="outline-title">Outline</div>
  __OUTLINE__
</div>

<div class="section-title">Arxiv Papers</div>
<div>
    __CONTENT-ARXIV__
</div>

<div class="section-title">BioRxiv Papers</div>
<div>
    __CONTENT-BIORXIV__
</div>

<div class="section-title">Journal Papers</div>
<div>
    __CONTENT-JOURNAL__
</div>

<br><br>
<div>
To unsubscribe, remove your email in your Github Action setting.
</div>

</body>
</html>
"""


def _escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def _anchor_id(section: str, index: int) -> str:
    return f"{section}-{index + 1}"


def get_empty_html():
    return """
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9; margin: 0;">
  <tr>
    <td style="font-size: 20px; font-weight: bold; color: #333;">
        No new papers matched your query today.
    </td>
  </tr>
  </table>
  """


def get_stars(score: float | None):
    if score is None:
        return ""
    full_star = '<span class="full-star">⭐</span>'
    half_star = '<span class="half-star">⭐</span>'
    low = 6
    high = 8
    if score <= low:
        return ""
    if score >= high:
        return f'<span class="star-wrapper">{full_star * 5}</span>'
    interval = (high - low) / 10
    star_num = math.ceil((score - low) / interval)
    full_star_num = int(star_num / 2)
    half_star_num = star_num - full_star_num * 2
    return f'<span class="star-wrapper">{full_star * full_star_num}{half_star * half_star_num}</span>'


def _join_authors(author_list: list[str]) -> str:
    if len(author_list) <= 5:
        return ", ".join(author_list)
    return ", ".join(author_list[:3] + ["..."] + author_list[-2:])


def _build_meta_line(parts: list[str]) -> str:
    return " | ".join(part for part in parts if part)


def _build_id_link(label: str, value: str, href: str) -> str:
    return f'{_escape(label)}: <a href="{_escape(href)}" style="color: #333; text-decoration: underline;">{_escape(value)}</a>'


def get_block_html(
    *,
    anchor_id: str,
    title: str,
    title_url: str,
    authors: str,
    affiliation: str,
    meta_line: str,
    tldr_zh: str,
    background_color: str,
):
    tldr_html = ""
    if tldr_zh:
        tldr_html = f"""
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0 0 0; line-height: 1.6;">
            {_escape(tldr_zh)}
        </td>
    </tr>
"""
    return f"""
    <a id="{_escape(anchor_id)}"></a>
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: {background_color}; margin: 0;">
    <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333; padding: 0;">
            <a href="{_escape(title_url)}" style="color: #333; text-decoration: none;">{_escape(title)}</a>
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #666; padding: 8px 0 0 0; line-height: 1.6;">
            {_escape(authors)}
            <br><i>{_escape(affiliation)}</i>
            <br><i>{meta_line}</i>
        </td>
    </tr>
    <!-- English TLDR intentionally hidden -->
    {tldr_html}
</table>
"""


def _format_arxiv_block(paper: ArxivPaper, anchor_id: str, background_color: str) -> str:
    authors = _join_authors([author.name for author in paper.authors])
    meta_parts = []
    stars = get_stars(paper.score)
    if stars:
        meta_parts.append(f"Relevance: {stars}")
    meta_parts.append(
        _build_id_link("arXiv ID", paper.arxiv_id, f"https://arxiv.org/abs/{paper.arxiv_id}")
    )
    return get_block_html(
        anchor_id=anchor_id,
        title=paper.title,
        title_url=f"https://arxiv.org/abs/{paper.arxiv_id}",
        authors=authors,
        affiliation=paper.primary_affiliation,
        meta_line=_build_meta_line(meta_parts),
        tldr_zh=paper.tldr_zh,
        background_color=background_color,
    )


def _format_biorxiv_block(paper: BiorxivPaper, anchor_id: str, background_color: str) -> str:
    authors = _join_authors(paper.authors)
    meta_parts = []
    stars = get_stars(paper.score)
    if stars:
        meta_parts.append(f"Relevance: {stars}")
    meta_parts.append(_build_id_link("DOI", paper.biorxiv_id, f"https://doi.org/{paper.biorxiv_id}"))
    return get_block_html(
        anchor_id=anchor_id,
        title=paper.title,
        title_url=paper.paper_url,
        authors=authors,
        affiliation=paper.primary_affiliation,
        meta_line=_build_meta_line(meta_parts),
        tldr_zh=paper.tldr_zh,
        background_color=background_color,
    )


def _format_journal_block(paper: JournalPaper, anchor_id: str, background_color: str) -> str:
    authors = _join_authors(paper.authors)
    meta_parts = [paper.journal]
    if paper.published_at:
        meta_parts.append(paper.published_at)
    stars = get_stars(paper.score)
    if stars:
        meta_parts.append(f"Relevance: {stars}")
    if "/" in paper.paper_id:
        meta_parts.append(_build_id_link("DOI", paper.paper_id, paper.paper_url))
    else:
        meta_parts.append(_build_id_link("PMID", paper.paper_id, paper.paper_url))
    return get_block_html(
        anchor_id=anchor_id,
        title=paper.title,
        title_url=paper.paper_url,
        authors=authors,
        affiliation=paper.primary_affiliation,
        meta_line=_build_meta_line(meta_parts),
        tldr_zh=paper.tldr_zh,
        background_color=background_color,
    )


def _build_outline_section(title: str, papers, section_key: str) -> str:
    if not papers:
        return ""
    items = []
    for index, paper in enumerate(papers):
        stars = get_stars(getattr(paper, "score", None))
        stars_html = f' <span style="margin-left: 6px;">{stars}</span>' if stars else ""
        items.append(
            f'<li style="margin: 4px 0;"><a href="#{_escape(_anchor_id(section_key, index))}" style="color: #333; text-decoration: none;">{_escape(getattr(paper, "title", ""))}</a>{stars_html}</li>'
        )
    return (
        f'<div class="outline-group-title">{_escape(title)}</div>'
        f'<ul style="margin: 0 0 8px 20px; padding: 0; line-height: 1.6;">{"".join(items)}</ul>'
    )


def _build_outline(
    papers: list[ArxivPaper],
    papers_biorxiv: list[BiorxivPaper],
    papers_journal: list[JournalPaper],
) -> str:
    sections = [
        _build_outline_section("Arxiv Papers", papers, "arxiv"),
        _build_outline_section("BioRxiv Papers", papers_biorxiv, "biorxiv"),
        _build_outline_section("Journal Papers", papers_journal, "journal"),
    ]
    content = "".join(section for section in sections if section)
    if content:
        return content
    return '<div style="font-size: 16px; color: #666;">No papers today.</div>'


def _render_section(papers, formatter, desc: str, section_key: str) -> str:
    if len(papers) == 0:
        return get_empty_html()
    started = perf_counter()
    total = len(papers)
    max_workers = min(6, total)

    def _render_one(index: int, paper):
        paper_started = perf_counter()
        anchor_id = _anchor_id(section_key, index)
        background_color = CARD_BACKGROUNDS[index % len(CARD_BACKGROUNDS)]
        html_block = formatter(paper, anchor_id, background_color)
        return index, html_block, getattr(paper, "title", "<unknown title>"), perf_counter() - paper_started

    parts = [None] * total
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_render_one, index, paper) for index, paper in enumerate(papers)]
        for future in tqdm(as_completed(futures), total=total, desc=desc):
            index, html_block, title, elapsed = future.result()
            parts[index] = html_block
            logger.info(
                "{} {}/{} finished in {:.2f}s: {}",
                desc,
                index + 1,
                total,
                elapsed,
                title,
            )
    logger.info("{} completed in {:.2f}s for {} papers.", desc, perf_counter() - started, total)
    return "".join(part for part in parts if part is not None)


def render_email(
    papers: list[ArxivPaper],
    papers_biorxiv: list[BiorxivPaper],
    papers_journal: list[JournalPaper],
):
    html_doc = framework.replace(
        "__OUTLINE__", _build_outline(papers, papers_biorxiv, papers_journal)
    )
    html_doc = html_doc.replace(
        "__CONTENT-ARXIV__", _render_section(papers, _format_arxiv_block, "Rendering arXiv email", "arxiv")
    )
    html_doc = html_doc.replace(
        "__CONTENT-BIORXIV__",
        _render_section(papers_biorxiv, _format_biorxiv_block, "Rendering bioRxiv email", "biorxiv"),
    )
    html_doc = html_doc.replace(
        "__CONTENT-JOURNAL__",
        _render_section(papers_journal, _format_journal_block, "Rendering journal email", "journal"),
    )
    return html_doc


def send_email(sender: str, receiver: str, password: str, smtp_server: str, smtp_port: int, html: str):
    def _format_addr(s):
        name, addr = parseaddr(s)
        return formataddr((Header(name, "utf-8").encode(), addr))

    msg = MIMEText(html, "html", "utf-8")
    msg["From"] = _format_addr("Github Action <%s>" % sender)
    msg["To"] = _format_addr("You <%s>" % receiver)
    today = datetime.datetime.now().strftime("%Y/%m/%d")
    msg["Subject"] = Header(f"Daily Papers {today}", "utf-8").encode()

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
    except Exception:
        server = smtplib.SMTP_SSL(smtp_server, smtp_port)

    server.login(sender, password)
    server.sendmail(sender, [receiver], msg.as_string())
    server.quit()

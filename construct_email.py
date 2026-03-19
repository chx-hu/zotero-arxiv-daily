import datetime
import smtplib
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import perf_counter
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from loguru import logger
from tqdm import tqdm

from paper import ArxivPaper, BiorxivPaper, JournalPaper

framework = """
<!DOCTYPE HTML>
<html>
<head>
</head>
<body>

<h1>Arxiv Papers</h1>
<div>
    __CONTENT-ARXIV__
</div>

<h1>BioRxiv Papers</h1>
<div>
    __CONTENT-BIORXIV__
</div>

<h1>Journal Papers</h1>
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


def get_empty_html():
    return """
  <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
  <tr>
    <td style="font-size: 20px; font-weight: bold; color: #333;">
        No new papers matched your query today.
    </td>
  </tr>
  </table>
  """


def get_block_html(
    title: str,
    authors: str,
    submeta: str,
    tldr_en: str,
    tldr_zh: str,
):
    return f"""
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="font-family: Arial, sans-serif; border: 1px solid #ddd; border-radius: 8px; padding: 16px; background-color: #f9f9f9;">
    <tr>
        <td style="font-size: 20px; font-weight: bold; color: #333;">
            {title}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #666; padding: 8px 0;">
            {authors}
            <br><i>{submeta}</i>
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>English TLDR:</strong> {tldr_en}
        </td>
    </tr>
    <tr>
        <td style="font-size: 14px; color: #333; padding: 8px 0;">
            <strong>Chinese TLDR:</strong> {tldr_zh}
        </td>
    </tr>
</table>
"""


def _join_authors(author_list: list[str]) -> str:
    if len(author_list) <= 5:
        return ", ".join(author_list)
    return ", ".join(author_list[:3] + ["..."] + author_list[-2:])


def _format_arxiv_block(paper: ArxivPaper) -> str:
    authors = _join_authors([author.name for author in paper.authors])
    meta_parts = ["Unknown Affiliation"]
    if paper.score is not None:
        meta_parts.append(f"Relevance {paper.score:.1f}/10")
    meta_parts.append(f"arXiv ID {paper.arxiv_id}")
    return get_block_html(
        title=paper.title,
        authors=authors,
        submeta="<br>".join(meta_parts),
        tldr_en=paper.tldr_en,
        tldr_zh=paper.tldr_zh,
    )


def _format_biorxiv_block(paper: BiorxivPaper) -> str:
    authors = _join_authors(paper.authors)
    meta_parts = [paper.institution or "Unknown Affiliation"]
    if paper.score is not None:
        meta_parts.append(f"Relevance {paper.score:.1f}/10")
    meta_parts.append(f"DOI {paper.biorxiv_id}")
    return get_block_html(
        title=paper.title,
        authors=authors,
        submeta="<br>".join(meta_parts),
        tldr_en=paper.tldr_en,
        tldr_zh=paper.tldr_zh,
    )


def _format_journal_block(paper: JournalPaper) -> str:
    authors = _join_authors(paper.authors)
    meta_parts = [paper.journal]
    if paper.published_at:
        meta_parts.append(paper.published_at)
    if paper.score is not None:
        meta_parts.append(f"Relevance {paper.score:.1f}/10")
    if "/" in paper.paper_id:
        meta_parts.append(f"DOI {paper.paper_id}")
    else:
        meta_parts.append(f"PMID {paper.paper_id}")
    return get_block_html(
        title=paper.title,
        authors=authors,
        submeta="<br>".join(meta_parts),
        tldr_en=paper.tldr_en,
        tldr_zh=paper.tldr_zh,
    )


def _render_section(papers, formatter, desc: str) -> str:
    if len(papers) == 0:
        return get_empty_html()
    started = perf_counter()
    total = len(papers)
    max_workers = min(6, total)

    def _render_one(index: int, paper):
        paper_started = perf_counter()
        html = formatter(paper)
        return index, html, getattr(paper, "title", "<unknown title>"), perf_counter() - paper_started

    parts = [None] * total
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_render_one, index, paper) for index, paper in enumerate(papers)]
        for future in tqdm(as_completed(futures), total=total, desc=desc):
            index, html, title, elapsed = future.result()
            parts[index] = html
            logger.info(
                "{} {}/{} finished in {:.2f}s: {}",
                desc,
                index + 1,
                total,
                elapsed,
                title,
            )
    logger.info("{} completed in {:.2f}s for {} papers.", desc, perf_counter() - started, total)
    return "<br>" + "</br><br>".join(part for part in parts if part is not None) + "</br>"


def render_email(
    papers: list[ArxivPaper],
    papers_biorxiv: list[BiorxivPaper],
    papers_journal: list[JournalPaper],
):
    html = framework.replace(
        "__CONTENT-ARXIV__", _render_section(papers, _format_arxiv_block, "Rendering arXiv email")
    )
    html = html.replace(
        "__CONTENT-BIORXIV__",
        _render_section(papers_biorxiv, _format_biorxiv_block, "Rendering bioRxiv email"),
    )
    html = html.replace(
        "__CONTENT-JOURNAL__",
        _render_section(papers_journal, _format_journal_block, "Rendering journal email"),
    )
    return html


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

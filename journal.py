from dataclasses import dataclass
import datetime
import html as html_lib
import os
import re
from urllib.parse import urlencode, urljoin, urlparse

import feedparser
import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from paper import JournalPaper

CROSSREF_WORKS_URL = "https://api.crossref.org/works"
ELSEVIER_ARTICLE_URL = "https://api.elsevier.com/content/article/doi/{doi}"
REQUEST_CONNECT_TIMEOUT_SECONDS = 20
REQUEST_READ_TIMEOUT_SECONDS = 60
REQUEST_MAX_RETRIES = 5
REQUEST_BACKOFF_FACTOR = 1.0
DEFAULT_LOOKBACK_DAYS = 1
DEFAULT_FETCH_PER_JOURNAL = 10
DEBUG_FETCH_LIMIT = 5


@dataclass(frozen=True)
class JournalConfig:
    key: str
    name: str
    pubmed_query: str
    strategy: str
    source_url: str | None = None


SUPPORTED_JOURNALS: list[JournalConfig] = [
    JournalConfig("nature", "Nature", "Nature", "direct", "https://www.nature.com/nature/research-articles"),
    JournalConfig("science", "Science", "Science", "direct", "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science"),
    JournalConfig("cell", "Cell", "Cell", "sciencedirect"),
    JournalConfig("nature_biotechnology", "Nature Biotechnology", "Nature Biotechnology", "direct", "https://www.nature.com/nbt/research-articles"),
    JournalConfig("nature_methods", "Nature Methods", "Nature Methods", "direct", "https://www.nature.com/nmeth/research-articles"),
    JournalConfig("nature_chemical_biology", "Nature Chemical Biology", "Nature Chemical Biology", "direct", "https://www.nature.com/nchembio/research-articles"),
    JournalConfig("nature_structural_molecular_biology", "Nature Structural & Molecular Biology", "Nature Structural & Molecular Biology", "direct", "https://www.nature.com/nsmb/research-articles"),
    JournalConfig("nature_machine_intelligence", "Nature Machine Intelligence", "Nature Machine Intelligence", "direct", "https://www.nature.com/natmachintell/research-articles"),
    JournalConfig("nature_computational_science", "Nature Computational Science", "Nature Computational Science", "direct", "https://www.nature.com/natcomputsci/research-articles"),
    JournalConfig("science_advances", "Science Advances", "Science Advances", "direct", "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv"),
    JournalConfig("cell_systems", "Cell Systems", "Cell Systems", "sciencedirect"),
    JournalConfig("cell_genomics", "Cell Genomics", "Cell Genomics", "sciencedirect"),
    JournalConfig("neuron", "Neuron", "Neuron", "sciencedirect"),
    JournalConfig("ajhg", "American Journal of Human Genetics", "American Journal of Human Genetics", "sciencedirect"),
    JournalConfig("trends_in_genetics", "Trends in Genetics", "Trends in Genetics", "sciencedirect"),
    JournalConfig("bioinformatics", "Bioinformatics", "Bioinformatics", "direct", "https://academic.oup.com/rss/site_5139/advanceAccess_3001.xml"),
    JournalConfig("briefings_in_bioinformatics", "Briefings in Bioinformatics", "Briefings in Bioinformatics", "direct", "https://academic.oup.com/rss/site_5143/3005.xml"),
    JournalConfig("nucleic_acids_research", "Nucleic Acids Research", "Nucleic Acids Research", "direct", "https://academic.oup.com/rss/site_5127/advanceAccess_3091.xml"),
    JournalConfig("genome_biology", "Genome Biology", "Genome Biology", "direct", "https://link.springer.com/journal/13059"),
    JournalConfig("genome_research", "Genome Research", "Genome Research", "direct", "https://genome.cshlp.org/"),
    JournalConfig("genome_medicine", "Genome Medicine", "Genome Medicine", "direct", "https://link.springer.com/journal/13073/articles"),
    JournalConfig("nature_communications", "Nature Communications", "Nature Communications", "direct", "https://www.nature.com/ncomms/research-articles"),
    JournalConfig("nature_genetics", "Nature Genetics", "Nature Genetics", "direct", "https://www.nature.com/ng/research-articles"),
    JournalConfig("genetics", "GENETICS", "Genetics", "direct", "https://academic.oup.com/rss/site_6327/advanceAccess_4082.xml"),
    JournalConfig("human_molecular_genetics", "Human Molecular Genetics", "Human Molecular Genetics", "direct", "https://academic.oup.com/rss/site_5124/advanceAccess_3030.xml"),
    JournalConfig("brain", "Brain", "Brain", "direct", "https://academic.oup.com/rss/site_5367/advanceAccess_3228.xml"),
    JournalConfig("nature_neuroscience", "Nature Neuroscience", "Nature Neuroscience", "direct", "https://www.nature.com/neuro/research-articles"),
    JournalConfig("molecular_psychiatry", "Molecular Psychiatry", "Molecular Psychiatry", "direct", "https://www.nature.com/mp/articles"),
    JournalConfig("biological_psychiatry", "Biological Psychiatry", "Biological Psychiatry", "sciencedirect"),
    JournalConfig("translational_psychiatry", "Translational Psychiatry", "Translational Psychiatry", "direct", "https://www.nature.com/tp/articles"),
]


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


JOURNAL_GROUPS: dict[str, list[str]] = {
    "all": [cfg.key for cfg in SUPPORTED_JOURNALS],
    "xx": [
        "nature",
        "science",
        "cell",
        "nature_biotechnology",
        "nature_methods",
        "nature_chemical_biology",
        "nature_structural_molecular_biology",
        "nature_machine_intelligence",
        "nature_computational_science",
        "science_advances",
        "cell_systems",
        "bioinformatics",
        "briefings_in_bioinformatics",
        "nucleic_acids_research",
        "nature_communications",
    ],
    "rr": [
        "nature",
        "science",
        "cell",
        "nature_methods",
        "science_advances",
        "cell_genomics",
        "neuron",
        "ajhg",
        "trends_in_genetics",
        "bioinformatics",
        "genome_biology",
        "genome_research",
        "genome_medicine",
        "nature_communications",
        "nature_genetics",
        "genetics",
        "human_molecular_genetics",
        "brain",
        "nature_neuroscience",
        "molecular_psychiatry",
        "biological_psychiatry",
        "translational_psychiatry",
    ],
}


def _build_user_agent() -> str:
    mailto = (
        os.getenv("CROSSREF_MAILTO")
        or os.getenv("EMAIL_RECEIVER")
        or os.getenv("EMAIL_SENDER")
    )
    if mailto:
        return f"zotero-arxiv-daily/1.0 (mailto:{mailto})"
    return "zotero-arxiv-daily/1.0 (https://github.com/chx-hu/zotero-arxiv-daily)"


def _build_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=REQUEST_MAX_RETRIES,
        connect=REQUEST_MAX_RETRIES,
        read=REQUEST_MAX_RETRIES,
        status=REQUEST_MAX_RETRIES,
        backoff_factor=REQUEST_BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {
            "User-Agent": _build_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
    )
    return session


def _configs_from_group(group: str) -> list[JournalConfig]:
    group_key = _normalize_token(group or "all") or "all"
    if group_key not in JOURNAL_GROUPS:
        logger.warning("Unknown journal group '{}'. Falling back to all.", group)
        group_key = "all"
    group_keys = set(JOURNAL_GROUPS[group_key])
    return [cfg for cfg in SUPPORTED_JOURNALS if cfg.key in group_keys]


def _lookback_start_date(lookback_days: int) -> datetime.date:
    return datetime.date.today() - datetime.timedelta(days=max(lookback_days, 1))


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _clean_markup_text(text: str) -> str:
    cleaned = html_lib.unescape(text or "")
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return _normalize_whitespace(cleaned)


def _parse_date_parts(date_parts: list | None) -> str:
    if not date_parts:
        return ""
    parts = date_parts[0] if isinstance(date_parts[0], list) else date_parts
    if not parts:
        return ""
    year = str(parts[0])
    month = str(parts[1] if len(parts) > 1 else 1).zfill(2)
    day = str(parts[2] if len(parts) > 2 else 1).zfill(2)
    return f"{year}-{month}-{day}"


def _parse_crossref_published_at(item: dict) -> str:
    for key in ("published-online", "published-print", "issued", "created"):
        date_obj = item.get(key) or {}
        date_parts = date_obj.get("date-parts")
        published_at = _parse_date_parts(date_parts)
        if published_at:
            return published_at
    return ""


def _crossref_container_title(config: JournalConfig) -> str:
    if config.key == "ajhg":
        return "The American Journal of Human Genetics"
    return config.name


def _is_within_lookback(published_at: str, lookback_days: int) -> bool:
    if not published_at:
        return False
    try:
        published_date = datetime.date.fromisoformat(published_at)
    except ValueError:
        return False
    return published_date >= _lookback_start_date(lookback_days)


def _extract_link_tags(page_html: str) -> list[str]:
    return re.findall(r"<link\b[^>]*>", page_html, flags=re.IGNORECASE)


def _extract_anchor_tags(page_html: str) -> list[str]:
    return re.findall(r"<a\b[^>]*>", page_html, flags=re.IGNORECASE)


def _extract_attr(tag: str, attr_name: str) -> str:
    match = re.search(
        rf"""{attr_name}\s*=\s*["']([^"']+)["']""",
        tag,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _discover_feed_urls(page_html: str, source_url: str) -> list[str]:
    discovered = []
    seen = set()
    for tag in _extract_link_tags(page_html):
        tag_type = _extract_attr(tag, "type").lower()
        href = _extract_attr(tag, "href")
        if tag_type not in {"application/rss+xml", "application/atom+xml"} or not href:
            continue
        resolved = urljoin(source_url, href)
        if resolved in seen:
            continue
        seen.add(resolved)
        discovered.append(resolved)
    for tag in _extract_anchor_tags(page_html):
        href = _extract_attr(tag, "href")
        if not href:
            continue
        if not re.search(r"(rss|atom|feed)", href, flags=re.IGNORECASE):
            continue
        resolved = urljoin(source_url, href)
        if resolved in seen:
            continue
        seen.add(resolved)
        discovered.append(resolved)
    return discovered


def _parse_meta_tags(page_html: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for tag in re.findall(r"<meta\b[^>]*>", page_html, flags=re.IGNORECASE):
        attrs = {
            key.lower(): value
            for key, value in re.findall(
                r"""([A-Za-z_:.-]+)\s*=\s*["']([^"']*)["']""",
                tag,
            )
        }
        key = attrs.get("name") or attrs.get("property")
        content = attrs.get("content")
        if not key or content is None:
            continue
        values.setdefault(key.lower(), []).append(content)
    return values


def _fetch_html(session: requests.Session, url: str) -> str:
    response = session.get(
        url,
        timeout=(REQUEST_CONNECT_TIMEOUT_SECONDS, REQUEST_READ_TIMEOUT_SECONDS),
    )
    response.raise_for_status()
    return response.text


def _supplement_article_metadata(
    session: requests.Session,
    paper_url: str,
) -> dict[str, str | list[str]]:
    try:
        page_html = _fetch_html(session, paper_url)
    except Exception as exc:
        logger.debug("Failed to fetch article page {} with {}", paper_url, exc)
        return {}
    meta = _parse_meta_tags(page_html)
    title = ""
    for key in ("citation_title", "og:title", "dc.title"):
        for value in meta.get(key, []):
            normalized = _normalize_whitespace(value)
            if normalized:
                title = normalized
                break
        if title:
            break
    authors = [_normalize_whitespace(value) for value in meta.get("citation_author", []) if _normalize_whitespace(value)]
    affiliation = ""
    for value in meta.get("citation_author_institution", []):
        normalized = _normalize_whitespace(value)
        if normalized:
            affiliation = normalized
            break
    abstract = ""
    for key in ("citation_abstract", "description", "dc.description"):
        for value in meta.get(key, []):
            cleaned = _clean_markup_text(value)
            if cleaned:
                abstract = cleaned
                break
        if abstract:
            break
    doi = ""
    for key in ("citation_doi", "dc.identifier"):
        for value in meta.get(key, []):
            normalized = _normalize_whitespace(value)
            if normalized.lower().startswith("doi:"):
                normalized = normalized[4:].strip()
            if normalized:
                doi = normalized
                break
        if doi:
            break
    journal = ""
    for key in ("citation_journal_title", "og:site_name"):
        for value in meta.get(key, []):
            normalized = _normalize_whitespace(value)
            if normalized:
                journal = normalized
                break
        if journal:
            break
    published_at = ""
    for key in ("citation_online_date", "citation_publication_date", "article:published_time"):
        for value in meta.get(key, []):
            match = re.search(r"\d{4}-\d{2}-\d{2}", value)
            if match:
                published_at = match.group(0)
                break
        if published_at:
            break
    return {
        "title": title,
        "authors": authors,
        "affiliation": affiliation,
        "abstract": abstract,
        "doi": doi,
        "journal": journal,
        "published_at": published_at,
    }


def _paper_from_payload(payload: dict) -> JournalPaper | None:
    title = _normalize_whitespace(payload.get("title", ""))
    abstract = _normalize_whitespace(payload.get("abstract", ""))
    authors = [author for author in payload.get("authors", []) if _normalize_whitespace(author)]
    paper_url = payload.get("paper_url", "")
    paper_id = payload.get("paper_id", "")
    if not authors:
        authors = ["Unknown Authors"]
    if not title or not abstract or not paper_url or not paper_id:
        return None
    return JournalPaper(
        {
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "paper_id": paper_id,
            "paper_url": paper_url,
            "journal": payload.get("journal", ""),
            "published_at": payload.get("published_at", ""),
            "affiliation": payload.get("affiliation", ""),
        }
    )


def _paper_from_feed_entry(
    session: requests.Session,
    entry,
    config: JournalConfig,
    lookback_days: int,
    debug: bool,
) -> JournalPaper | None:
    published_at = ""
    for key in ("published", "updated"):
        value = getattr(entry, key, "")
        match = re.search(r"\d{4}-\d{2}-\d{2}", value)
        if match:
            published_at = match.group(0)
            break
    if not published_at and getattr(entry, "published_parsed", None):
        published_at = f"{entry.published_parsed.tm_year:04d}-{entry.published_parsed.tm_mon:02d}-{entry.published_parsed.tm_mday:02d}"
    if not published_at and getattr(entry, "updated_parsed", None):
        published_at = f"{entry.updated_parsed.tm_year:04d}-{entry.updated_parsed.tm_mon:02d}-{entry.updated_parsed.tm_mday:02d}"
    if not debug and published_at and not _is_within_lookback(published_at, lookback_days):
        return None

    paper_url = getattr(entry, "link", "") or ""
    authors = []
    if getattr(entry, "authors", None):
        authors = [_normalize_whitespace(author.get("name", "")) for author in entry.authors if _normalize_whitespace(author.get("name", ""))]

    abstract = _clean_markup_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
    doi = ""
    for candidate in (
        getattr(entry, "dc_identifier", ""),
        getattr(entry, "prism_doi", ""),
        getattr(entry, "id", ""),
    ):
        match = re.search(r"10\.\d{4,9}/\S+", candidate)
        if match:
            doi = match.group(0).rstrip(" .")
            break

    supplemented = {}
    if paper_url and (not abstract or not doi or not published_at):
        supplemented = _supplement_article_metadata(session, paper_url)
        authors = authors or supplemented.get("authors", [])
        abstract = abstract or supplemented.get("abstract", "")
        doi = doi or supplemented.get("doi", "")
        published_at = published_at or supplemented.get("published_at", "")

    paper_id = doi or paper_url
    return _paper_from_payload(
        {
            "title": getattr(entry, "title", ""),
            "abstract": abstract,
            "authors": authors,
            "paper_id": paper_id,
            "paper_url": f"https://doi.org/{doi}" if doi else paper_url,
            "journal": supplemented.get("journal", "") or config.name,
            "published_at": published_at,
            "affiliation": supplemented.get("affiliation", ""),
        }
    )


def _discover_article_urls(page_html: str, source_url: str) -> list[str]:
    hostname = urlparse(source_url).netloc.lower()
    patterns: list[str] = []
    if "nature.com" in hostname or "biomedcentral.com" in hostname:
        patterns.append(r"/articles/")
    if "link.springer.com" in hostname:
        patterns.append(r"/article/")
    if "science.org" in hostname:
        patterns.append(r"/doi/(?:abs|full)/10\.")
    if "academic.oup.com" in hostname:
        patterns.extend([r"/article/", r"/advance-article/"])
    if "genome.cshlp.org" in hostname:
        patterns.append(r"/content/")
    if "jamanetwork.com" in hostname:
        patterns.append(r"/journals/.+/fullarticle/")
    if "sciencedirect.com" in hostname:
        patterns.append(r"/science/article/pii/")

    discovered = []
    seen = set()
    for tag in _extract_anchor_tags(page_html):
        href = _extract_attr(tag, "href")
        if not href:
            continue
        resolved = urljoin(source_url, href)
        if resolved in seen:
            continue
        if urlparse(resolved).netloc.lower() != hostname:
            continue
        if patterns and not any(re.search(pattern, resolved, flags=re.IGNORECASE) for pattern in patterns):
            continue
        if any(token in resolved.lower() for token in ("/pdf", ".pdf", "/epdf", "/supplementary-information")):
            continue
        seen.add(resolved)
        discovered.append(resolved)
    return discovered


def _paper_from_article_url(
    session: requests.Session,
    paper_url: str,
    config: JournalConfig,
    lookback_days: int,
    debug: bool,
) -> JournalPaper | None:
    supplemented = _supplement_article_metadata(session, paper_url)
    published_at = str(supplemented.get("published_at", ""))
    if not debug and published_at and not _is_within_lookback(published_at, lookback_days):
        return None
    doi = _normalize_whitespace(str(supplemented.get("doi", "")))
    return _paper_from_payload(
        {
            "title": supplemented.get("title", ""),
            "abstract": supplemented.get("abstract", ""),
            "authors": supplemented.get("authors", []),
            "paper_id": doi or paper_url,
            "paper_url": f"https://doi.org/{doi}" if doi else paper_url,
            "journal": supplemented.get("journal", "") or config.name,
            "published_at": published_at,
            "affiliation": supplemented.get("affiliation", ""),
        }
    )


def _direct_fetch(
    session: requests.Session,
    config: JournalConfig,
    lookback_days: int,
    retmax: int,
    debug: bool,
) -> list[JournalPaper]:
    if not config.source_url:
        return []
    if (
        config.source_url.endswith(".xml")
        or "/rss/" in config.source_url
        or "showFeed" in config.source_url
    ):
        feed_urls = [config.source_url]
        article_urls = []
    else:
        page_html = _fetch_html(session, config.source_url)
        feed_urls = _discover_feed_urls(page_html, config.source_url)
        article_urls = _discover_article_urls(page_html, config.source_url)

    papers = []
    seen = set()
    for feed_url in feed_urls:
        try:
            feed_text = _fetch_html(session, feed_url)
            feed = feedparser.parse(feed_text)
        except Exception as exc:
            logger.debug("Failed to read feed {} for {} with {}", feed_url, config.name, exc)
            continue
        for entry in getattr(feed, "entries", []):
            paper = _paper_from_feed_entry(session, entry, config, lookback_days, debug)
            if paper is None:
                continue
            dedupe_key = paper.paper_url.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            papers.append(paper)
            if len(papers) >= retmax:
                return papers
    if not feed_urls and not article_urls:
        raise ValueError(f"No feed or article links discovered for {config.name}")
    for article_url in article_urls:
        try:
            paper = _paper_from_article_url(session, article_url, config, lookback_days, debug)
        except Exception as exc:
            logger.debug("Failed to read article {} for {} with {}", article_url, config.name, exc)
            continue
        if paper is None:
            continue
        dedupe_key = paper.paper_url.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        papers.append(paper)
        if len(papers) >= retmax:
            return papers
    return papers


def _crossref_discovery_items(
    session: requests.Session,
    config: JournalConfig,
    lookback_days: int,
    retmax: int,
    debug: bool,
) -> list[dict]:
    filters = [
        f"container-title:{_crossref_container_title(config)}",
        "type:journal-article",
    ]
    if not debug:
        filters.append(f"from-pub-date:{_lookback_start_date(lookback_days).isoformat()}")
    params = {
        "filter": ",".join(filters),
        "rows": max(retmax * 8, 20),
        "sort": "published",
        "order": "desc",
        "select": "DOI,title,author,URL,abstract,container-title,published-online,published-print,issued,created",
    }
    response = session.get(
        f"{CROSSREF_WORKS_URL}?{urlencode(params)}",
        timeout=(REQUEST_CONNECT_TIMEOUT_SECONDS, REQUEST_READ_TIMEOUT_SECONDS),
    )
    response.raise_for_status()
    data = response.json()
    return data.get("message", {}).get("items", [])


def _parse_sciencedirect_authors(payload: dict) -> tuple[list[str], str]:
    authors = []
    affiliation = ""
    author_entries = ((payload.get("authors") or {}).get("author") or [])
    if isinstance(author_entries, dict):
        author_entries = [author_entries]
    for author in author_entries:
        if not isinstance(author, dict):
            continue
        name = _normalize_whitespace(
            author.get("$", "")
            or author.get("ce:indexed-name", "")
            or " ".join(
                part
                for part in [author.get("ce:given-name", ""), author.get("ce:surname", "")]
                if part
            )
        )
        if name:
            authors.append(name)
        if affiliation:
            continue
        raw_affiliations = author.get("affiliation", [])
        if isinstance(raw_affiliations, dict):
            raw_affiliations = [raw_affiliations]
        for item in raw_affiliations:
            if isinstance(item, dict):
                aff_name = _normalize_whitespace(
                    item.get("$", "")
                    or item.get("@_fa", "")
                    or item.get("ce:textfn", "")
                )
            else:
                aff_name = _normalize_whitespace(str(item))
            if aff_name:
                affiliation = aff_name
                break
    creator = payload.get("coredata", {}).get("dc:creator")
    if not authors:
        if isinstance(creator, str):
            authors = [name.strip() for name in creator.split("|") if name.strip()]
        elif isinstance(creator, list):
            authors = [
                _normalize_whitespace(item.get("$", "") if isinstance(item, dict) else str(item))
                for item in creator
            ]
            authors = [name for name in authors if name]
    return authors, affiliation


def _sciencedirect_paper_from_doi(
    session: requests.Session,
    config: JournalConfig,
    doi: str,
) -> JournalPaper | None:
    api_key = os.getenv("ELSEVIER_API_KEY", "").strip()
    if not api_key:
        logger.warning("ELSEVIER_API_KEY is not set. Skipping ScienceDirect fetch for {}.", config.name)
        return None
    response = session.get(
        ELSEVIER_ARTICLE_URL.format(doi=doi),
        headers={
            "X-ELS-APIKey": api_key,
            "Accept": "application/json",
            "User-Agent": _build_user_agent(),
        },
        params={"httpAccept": "application/json"},
        timeout=(REQUEST_CONNECT_TIMEOUT_SECONDS, REQUEST_READ_TIMEOUT_SECONDS),
    )
    response.raise_for_status()
    payload = response.json().get("full-text-retrieval-response", {})
    coredata = payload.get("coredata", {})
    authors, affiliation = _parse_sciencedirect_authors(payload)
    resolved_doi = _normalize_whitespace(coredata.get("prism:doi", "")) or doi
    return _paper_from_payload(
        {
            "title": coredata.get("dc:title", ""),
            "abstract": _clean_markup_text(coredata.get("dc:description", "")),
            "authors": authors,
            "paper_id": resolved_doi,
            "paper_url": f"https://doi.org/{resolved_doi}",
            "journal": _normalize_whitespace(coredata.get("prism:publicationName", "")) or config.name,
            "published_at": _normalize_whitespace(coredata.get("prism:coverDate", "")),
            "affiliation": affiliation,
        }
    )


def _sciencedirect_fetch(
    session: requests.Session,
    config: JournalConfig,
    lookback_days: int,
    retmax: int,
    debug: bool,
) -> list[JournalPaper]:
    papers = []
    seen = set()
    for item in _crossref_discovery_items(session, config, lookback_days, retmax, debug):
        doi = _normalize_whitespace(item.get("DOI", ""))
        if not doi or doi.casefold() in seen:
            continue
        seen.add(doi.casefold())
        try:
            paper = _sciencedirect_paper_from_doi(session, config, doi)
        except Exception as exc:
            logger.warning("ScienceDirect article fetch failed for {} {} with {}", config.name, doi, exc)
            continue
        if paper is None:
            continue
        if not debug and paper.published_at and not _is_within_lookback(paper.published_at, lookback_days):
            continue
        papers.append(paper)
        if len(papers) >= retmax:
            break
    return papers


def _retrieve_journal_papers(
    session: requests.Session,
    config: JournalConfig,
    lookback_days: int,
    retmax: int,
    debug: bool,
) -> list[JournalPaper]:
    papers: list[JournalPaper] = []
    try:
        if config.strategy == "direct":
            papers = _direct_fetch(session, config, lookback_days, retmax, debug)
        elif config.strategy == "sciencedirect":
            papers = _sciencedirect_fetch(session, config, lookback_days, retmax, debug)
        else:
            logger.warning("Unsupported journal strategy '{}' for {}. Skipping.", config.strategy, config.name)
    except Exception as exc:
        logger.warning("Primary {} fetch failed for {} with {}", config.strategy, config.name, exc)
    if len(papers) == 0:
        logger.warning("No direct journal papers retrieved for {}. Continuing.", config.name)
    return papers


def get_journal_paper(
    journal_group: str = "all",
    debug: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    fetch_per_journal: int = DEFAULT_FETCH_PER_JOURNAL,
) -> list[JournalPaper]:
    configs = _configs_from_group(journal_group)
    if len(configs) == 0:
        logger.info("No journal sources configured.")
        return []

    session = _build_session()
    papers = []
    seen = set()
    per_journal_limit = 1 if debug else fetch_per_journal
    debug_total_limit = DEBUG_FETCH_LIMIT if debug else None
    for config in configs:
        logger.info("Retrieving journal papers from {} via {}...", config.name, config.strategy)
        journal_papers = _retrieve_journal_papers(
            session,
            config,
            lookback_days,
            per_journal_limit,
            debug,
        )
        for paper in journal_papers:
            dedupe_key = paper.paper_url.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            papers.append(paper)
            if debug_total_limit is not None and len(papers) >= debug_total_limit:
                logger.debug("Debug mode reached journal paper limit of {}. Stopping early.", debug_total_limit)
                papers.sort(key=lambda paper: paper.published_at or "", reverse=True)
                return papers
    papers.sort(key=lambda paper: paper.published_at or "", reverse=True)
    return papers

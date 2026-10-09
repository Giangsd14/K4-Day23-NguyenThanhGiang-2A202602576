"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET

import httpx
from dotenv import load_dotenv
from langchain_core.tools import tool

load_dotenv()

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"

_last_arxiv_time = 0.0


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _redact(text: str) -> str:
    """Never leak API keys in error strings or output."""
    key = (os.getenv("EXA_API_KEY") or "").strip()
    if key and key in text:
        text = text.replace(key, "[REDACTED]")
    return re.sub(r"exaApiKey=[A-Za-z0-9_\-]+", "exaApiKey=[REDACTED]", text)


# ---- TODO 1: retry helper ----
def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(); when it raises RetryableError, wait and call it again."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            retry_after = None
            is_retryable = False

            if isinstance(e, RetryableError):
                is_retryable = True
                retry_after = e.retry_after
            elif isinstance(e, httpx.HTTPStatusError):
                if e.response.status_code in (429, 500, 502, 503, 504):
                    is_retryable = True
                    hdr = e.response.headers.get("retry-after")
                    if hdr:
                        try:
                            retry_after = float(hdr)
                        except (ValueError, TypeError):
                            pass
            elif isinstance(e, httpx.TransportError):
                is_retryable = True

            if not is_retryable:
                raise

            if attempt == attempts - 1:
                raise

            if retry_after is not None:
                delay = float(retry_after)
            else:
                delay = min(cap, base * (2 ** attempt)) + random.uniform(0.0, 1.0)
            delay = min(delay, cap)
            time.sleep(delay)


# ---- TODO 2: arXiv ----
@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary, source: 'arxiv'}."""
    global _last_arxiv_time
    try:
        terms = re.findall(r"[\w\-]+", query)
        if not terms:
            return "NO RESULTS"
        search_query = " AND ".join(f"all:{t}" for t in terms)

        now = time.time()
        elapsed = now - _last_arxiv_time
        if elapsed < 3.0:
            time.sleep(3.0 - elapsed)
        _last_arxiv_time = time.time()

        clamped_max = max(1, min(int(max_results), 30))

        def _call():
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(
                    ARXIV_URL,
                    params={
                        "search_query": search_query,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                        "max_results": clamped_max,
                    },
                )
                resp.raise_for_status()
                return resp.text

        xml_text = with_retry(_call, attempts=5, base=2.0, cap=60.0)

        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        if not entries:
            return "NO RESULTS"

        records = []
        for entry in entries:
            id_el = entry.find("atom:id", ns)
            raw_id = id_el.text.strip() if id_el is not None and id_el.text else ""
            paper_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else raw_id.split("/")[-1]
            paper_id = re.sub(r"v\d+$", "", paper_id)

            pub_el = entry.find("atom:published", ns)
            published = pub_el.text.strip()[:10] if pub_el is not None and pub_el.text else ""

            title_el = entry.find("atom:title", ns)
            title = " ".join(title_el.text.split()) if title_el is not None and title_el.text else ""

            sum_el = entry.find("atom:summary", ns)
            summary = " ".join(sum_el.text.split()) if sum_el is not None and sum_el.text else ""
            if len(summary) > 600:
                summary = summary[:600] + "..."

            records.append({
                "id": paper_id,
                "url": f"https://arxiv.org/abs/{paper_id}",
                "published": published,
                "title": title,
                "summary": summary,
                "source": "arxiv",
            })

        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_redact(str(exc))}"


# ---- TODO 3: Hugging Face ----
@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars, source: 'hf-daily'} sorted by upvotes.
    `date` is YYYY-MM-DD (empty = latest). `keyword` filters title/summary; there is no topic search on this endpoint."""
    try:
        clamped_limit = max(1, min(int(limit), 100))
        params = {"limit": clamped_limit}
        if date and date.strip():
            params["date"] = date.strip()

        def _call():
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(HF_DAILY_URL, params=params)
                resp.raise_for_status()
                return resp.json()

        items = with_retry(_call, attempts=5, base=1.0, cap=30.0)
        if not items or not isinstance(items, list):
            return "NO RESULTS"

        records = []
        kw = keyword.strip().lower() if keyword else ""
        for item in items:
            paper = item.get("paper") if isinstance(item, dict) else None
            if not paper or not isinstance(paper, dict):
                continue
            paper_id = paper.get("id")
            if not paper_id:
                continue

            title = paper.get("title") or item.get("title") or ""
            summary = paper.get("summary") or item.get("summary") or ""
            summary = " ".join(summary.split())
            if len(summary) > 600:
                summary = summary[:600] + "..."

            if kw and (kw not in title.lower() and kw not in summary.lower()):
                continue

            published = (paper.get("publishedAt") or item.get("publishedAt") or "")[:10]
            upvotes = paper.get("upvotes") or 0
            github = paper.get("githubRepo") or ""
            stars = paper.get("githubStars") or 0

            records.append({
                "id": str(paper_id),
                "url": f"https://huggingface.co/papers/{paper_id}",
                "published": published,
                "title": title,
                "summary": summary,
                "upvotes": upvotes,
                "github": github,
                "stars": stars,
                "source": "hf-daily",
            })

        records.sort(key=lambda x: x.get("upvotes") or 0, reverse=True)
        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_redact(str(exc))}"


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars, source: 'hf-search'}."""
    try:
        q = query.strip()
        if not q:
            return "NO RESULTS"
        clamped_limit = max(1, min(int(limit), 50))

        def _call():
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(HF_SEARCH_URL, params={"q": q, "limit": clamped_limit})
                resp.raise_for_status()
                return resp.json()

        items = with_retry(_call, attempts=5, base=1.0, cap=30.0)
        if not items or not isinstance(items, list):
            return "NO RESULTS"

        records = []
        for item in items:
            paper = item.get("paper") if isinstance(item, dict) else None
            if not paper or not isinstance(paper, dict):
                continue
            paper_id = paper.get("id")
            if not paper_id:
                continue

            title = paper.get("title") or item.get("title") or ""
            summary = paper.get("ai_summary") or paper.get("summary") or item.get("summary") or ""
            summary = " ".join(summary.split())
            if len(summary) > 600:
                summary = summary[:600] + "..."

            published = (paper.get("publishedAt") or item.get("publishedAt") or "")[:10]
            upvotes = paper.get("upvotes") or 0
            github = paper.get("githubRepo") or ""
            stars = paper.get("githubStars") or 0

            records.append({
                "id": str(paper_id),
                "url": f"https://huggingface.co/papers/{paper_id}",
                "published": published,
                "title": title,
                "summary": summary,
                "upvotes": upvotes,
                "github": github,
                "stars": stars,
                "source": "hf-search",
            })

        return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_redact(str(exc))}"


# ---- TODO 4: web search / fetch through the Exa MCP endpoint ----
def _call_exa_mcp(tool_name: str, arguments: dict) -> str:
    """Helper to call Exa MCP tools over plain HTTP with SSE parsing, rate-limit detection and retries."""
    key = (os.getenv("EXA_API_KEY") or "").strip()
    endpoint = f"{EXA_URL}?exaApiKey={key}" if key else EXA_URL

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    def _call():
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(
                endpoint,
                json=payload,
                headers={"Accept": "application/json, text/event-stream"},
            )
            resp.raise_for_status()

            content_text = ""
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "error" in data:
                        err_msg = str(data["error"])
                        if "rate limit" in err_msg.lower():
                            raise RetryableError(f"Exa rate limited: {err_msg}", retry_after=20.0)
                        raise RuntimeError(f"Exa JSON-RPC error: {err_msg}")

                    result = data.get("result", {})
                    meta = result.get("_meta") or {}
                    if meta.get("rate_limited") or "rate limit" in str(meta).lower():
                        raise RetryableError("Exa rate limited via _meta", retry_after=20.0)

                    content_list = result.get("content", [])
                    texts = [item.get("text", "") for item in content_list if isinstance(item, dict) and item.get("type") == "text"]
                    chunk = "\n\n".join(t for t in texts if t)
                    if "rate limit exceeded" in chunk.lower() or "too many requests" in chunk.lower():
                        raise RetryableError("Exa rate limited in text", retry_after=20.0)
                    content_text += chunk

            return content_text.strip()

    return with_retry(_call, attempts=5, base=2.0, cap=60.0)


@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text with URLs, tagged as source: 'web'."""
    try:
        q = query.strip()
        if not q:
            return "NO RESULTS"
        obj = objective.strip() if objective else f"Find comprehensive research and survey articles on {q}"
        num = max(1, min(int(num_results), 10))

        text = _call_exa_mcp("web_search_exa", {"query": q, "objective": obj, "numResults": num})
        return _redact(text) if text else "NO RESULTS"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_redact(str(exc))}"


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""
    try:
        u = url.strip()
        if not u:
            return "NO RESULTS"
        text = _call_exa_mcp("web_fetch_exa", {"urls": [u]})
        if not text:
            return "NO RESULTS"
        if len(text) > 12000:
            text = text[:12000] + "\n\n[Content truncated]"
        return _redact(text)
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {_redact(str(exc))}"


# ---- TODO 5: registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
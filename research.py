"""research.py - STUDENT IMPLEMENTS.  The main script.   Guide: GUIDE.md, part 3.

Usage:  python research.py "survey about world model"
Result: reports/<slug>.md   reports/<slug>.sources.json   reports/<slug>.meta.json
"""
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

from agents import FINALIZER_PATH, REPORT_PATH, SOURCES_PATH, VALIDATOR_PATH, WORKDIR, build_lead_agent
from model import make_model
from sandbox import download, open_sandbox, upload

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
VALIDATOR_SOURCE = ROOT / "check_citations.py"
FINALIZER_SOURCE = ROOT / "finalize_citations.py"   # provided: uploaded next to your validator


def slugify(topic):
    """Turn a topic into a safe file name: lower case, runs of non-word characters become one "-", max 60 chars,
    never empty (fall back to "topic"). The topic is user input: "../../x" must not escape reports/."""
    s = re.sub(r"[^\w\s-]", "", str(topic).lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    if len(s) > 60:
        s = s[:60].rstrip("-")
    return s or "topic"


def build_prompt(topic):
    """The user message sent to the lead agent."""
    return (
        f"Please conduct an academic deep research survey on the topic: '{topic}'.\n\n"
        "Remember to follow your system instructions carefully:\n"
        "1. Plan with write_todos and deconstruct the topic into 3 independent sub-questions.\n"
        "2. Make AT LEAST 3 separate delegations to the researcher subagent (using task tool):\n"
        "   - Delegation 1: Investigate Foundations & Theory using arxiv_search (`arxiv`).\n"
        "   - Delegation 2: Investigate Models & Architectures using hf_search_papers (`hf-search`).\n"
        "   - Delegation 3: Investigate Trending Benchmarks & Surveys using hf_daily_papers (`hf-daily`) and web_search (`web`).\n"
        "3. Merge sources into /tmp/work/research/sources.json.\n"
        "   - MANDATORY (RUBRIC 2.2): sources.json MUST contain at least 3 distinct source families (arxiv, hf-search, hf-daily, web).\n"
        "4. Write the survey report body into /tmp/work/report/report.md following REPORT_TEMPLATE.md.\n"
        "   - DO NOT write the `## References` section yourself: the finalizer script generates it automatically.\n"
        "   - You MUST cite sources from AT LEAST 3 DISTINCT FAMILIES in the report body (at least one `arxiv`, at least one `hf-search`, and at least one `hf-daily` or `web` source) so that none are dropped by the finalizer!\n"
        "5. Execute `python3 /tmp/work/research/finalize_citations.py` inside the sandbox to generate `## References` and renumber citations.\n"
        "6. Execute `python3 /tmp/work/research/check_citations.py` inside the sandbox until it prints OK.\n"
        "7. Have citation-checker spot-check key claims."
    )


def summarize(messages, elapsed, model_name):
    """Return {"model", "elapsed_s", "subagent_calls", "tool_calls": {name: count}, "tokens": {"input", "output"}}."""
    tool_counts = Counter()
    subagent_calls = 0
    in_tokens = 0
    out_tokens = 0

    for msg in messages:
        calls = getattr(msg, "tool_calls", None)
        if calls and isinstance(calls, list):
            for c in calls:
                name = c.get("name") if isinstance(c, dict) else getattr(c, "name", str(c))
                if name:
                    tool_counts[name] += 1
                    if name == "task":
                        subagent_calls += 1

        usage = getattr(msg, "usage_metadata", None)
        if usage and isinstance(usage, dict):
            in_tokens += usage.get("input_tokens", 0)
            out_tokens += usage.get("output_tokens", 0)

    return {
        "model": model_name,
        "elapsed_s": round(elapsed, 1),
        "subagent_calls": subagent_calls,
        "tool_calls": dict(tool_counts),
        "tokens": {
            "input": in_tokens,
            "output": out_tokens,
        },
    }


def save_outputs(backend, topic, messages, elapsed, model_name, reports_dir=REPORTS):
    """Download the report from the sandbox and write the three files into reports_dir. Return the report path."""
    files = download(backend, [REPORT_PATH, SOURCES_PATH])
    report_bytes = files.get(REPORT_PATH)
    sources_bytes = files.get(SOURCES_PATH)

    if not report_bytes or not report_bytes.decode("utf-8", errors="replace").strip():
        raise RuntimeError("Report is missing or empty in sandbox")
    if not sources_bytes:
        raise RuntimeError("Sources file is missing in sandbox")

    try:
        sources_list = json.loads(sources_bytes.decode("utf-8"))
        if not isinstance(sources_list, list):
            raise ValueError("sources.json must be a JSON array")
    except Exception as exc:
        raise RuntimeError(f"sources.json is invalid JSON: {exc}")

    # Canonical normalization of source families
    families = set()
    for s in sources_list:
        if isinstance(s, dict):
            url = s.get("url", "")
            src = s.get("source", "")
            if url.startswith("https://arxiv.org/abs/"):
                src = "arxiv"
            elif url.startswith("https://huggingface.co/papers/"):
                if src != "hf-daily":
                    src = "hf-search"
            elif url.startswith("http://") or url.startswith("https://"):
                src = "web"
            if src in {"arxiv", "hf-daily", "hf-search", "web"}:
                s["source"] = src
                families.add(src)

    families = sorted(list(families))
    clean_sources_bytes = json.dumps(sources_list, indent=2, ensure_ascii=False).encode("utf-8")

    slug = slugify(topic)
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    meta = summarize(messages, elapsed, model_name)
    meta["topic"] = topic
    meta["n_sources"] = len(sources_list)
    meta["source_families"] = families

    report_file = reports_dir / f"{slug}.md"
    sources_file = reports_dir / f"{slug}.sources.json"
    meta_file = reports_dir / f"{slug}.meta.json"

    sources_file.write_bytes(clean_sources_bytes)
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    report_file.write_bytes(report_bytes)

    return report_file


def main(topic):
    """Return the process exit code (0 ok, 1 failed run, 2 no topic)."""
    if not topic or not topic.strip():
        print("Usage: python research.py \"<topic>\"", file=sys.stderr)
        return 2

    model = make_model()
    model_name = getattr(model, "model_name", None) or getattr(model, "model", None) or os.getenv("LAB_MODEL", "model")
    start = time.monotonic()

    try:
        with open_sandbox() as backend:
            backend.execute(f"mkdir -p {WORKDIR}/research/notes {WORKDIR}/report")
            upload(backend, {VALIDATOR_PATH: VALIDATOR_SOURCE.read_bytes(), FINALIZER_PATH: FINALIZER_SOURCE.read_bytes()})
            agent = build_lead_agent(backend, model)
            result = agent.invoke(
                {"messages": [{"role": "user", "content": build_prompt(topic)}]},
                config={"recursion_limit": 1000},
            )
            messages = result.get("messages", []) if isinstance(result, dict) else []

            # Execute finalizer and validator in sandbox before downloading
            backend.execute(f"python3 {FINALIZER_PATH}")
            backend.execute(f"python3 {VALIDATOR_PATH}")

            elapsed = time.monotonic() - start
            report_path = save_outputs(backend, topic, messages, elapsed, model_name)
            print(f"REPORT SAVED: {report_path}")
            return 0
    except RuntimeError as err:
        print(f"FAILED: {err}", file=sys.stderr)
        return 1
    except Exception as err:
        print(f"UNEXPECTED ERROR: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:])))
"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import re
import sys

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"

_GROUP = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\](?!\()")   # [3]  [1, 2]  [1-3]  [2-3]; not [3](link)
_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_REF_HEADING = re.compile(r"(?m)^##[ \t]+References[ \t]*$")
_URL = re.compile(r"https?://\S+")


def _group_numbers(group):
    numbers = []
    for part in re.split(r"\s*,\s*", group):
        span = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if span:
            a, b = int(span.group(1)), int(span.group(2))
            numbers.extend(range(a, b + 1) if 0 <= b - a <= 200 else [a, b])
        else:
            try:
                numbers.append(int(part))
            except ValueError:
                pass
    return numbers


def _clean_url(url):
    return re.sub(r"[),.;]+$", "", url)


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK)."""
    problems = []
    if not isinstance(sources, list) or len(sources) == 0:
        return ["no sources in sources.json"]

    source_numbers = set()
    seen_urls = set()
    sources_by_n = {}

    for entry in sources:
        if not isinstance(entry, dict):
            problems.append(f"source entry {entry!r} is not an object")
            continue
        n = entry.get("n")
        if not isinstance(n, int):
            problems.append(f"source n={n!r} is not an integer")
        elif n in source_numbers:
            problems.append(f"source n={n} appears multiple times in sources.json")
        else:
            source_numbers.add(n)
            sources_by_n[n] = entry

        url = entry.get("url")
        if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
            problems.append(f"source n={n!r} url {url!r} must start with http:// or https://")
        elif url in seen_urls:
            problems.append(f"duplicate url in sources.json: {url}")
        else:
            seen_urls.add(url)

    matches = list(_REF_HEADING.finditer(report_text))
    if not matches:
        problems.append("missing heading '## References'")
        body = report_text
        ref_section = ""
    else:
        body = report_text[:matches[-1].start()]
        ref_section = report_text[matches[-1].end():]

    # Find citations in body only (ignoring code blocks)
    segments = _CODE.split(body)
    cited_numbers = set()
    for i, segment in enumerate(segments):
        if i % 2:  # odd indices are code blocks or inline code
            continue
        for match in _GROUP.finditer(segment):
            for num in _group_numbers(match.group(1)):
                cited_numbers.add(num)

    for n in sorted(cited_numbers):
        if n not in source_numbers:
            problems.append(f"[{n}] cited but missing from sources.json")

    for n in sorted(source_numbers):
        if n not in cited_numbers:
            problems.append(f"source [{n}] never cited")

    # Check References section
    ref_line_re = re.compile(r"^\s*\[(\d+)\]\s*(.*)$")
    ref_lines_by_n = {}
    seen_ref_numbers = set()

    for line in ref_section.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = ref_line_re.match(line)
        if m:
            n = int(m.group(1))
            if n in seen_ref_numbers:
                problems.append(f"reference [{n}] appears more than once in ## References")
            seen_ref_numbers.add(n)
            ref_lines_by_n[n] = line

    for n in sorted(source_numbers):
        if n not in seen_ref_numbers:
            problems.append(f"missing reference line for source [{n}] in ## References")

    for n in sorted(seen_ref_numbers):
        if n not in source_numbers:
            problems.append(f"reference [{n}] in ## References is not in sources.json")

    for n, line in ref_lines_by_n.items():
        if n in sources_by_n:
            expected_url = sources_by_n[n].get("url")
            raw_urls = _URL.findall(line)
            urls = [_clean_url(u) for u in raw_urls]
            if len(urls) == 0:
                problems.append(f"reference [{n}] contains no URL")
            elif len(urls) > 1:
                problems.append(f"reference [{n}] bundles several sources under one number: {urls}")
            elif urls[0] != expected_url:
                problems.append(f"reference [{n}] URL {urls[0]!r} does not match sources.json URL {expected_url!r}")

    return problems


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
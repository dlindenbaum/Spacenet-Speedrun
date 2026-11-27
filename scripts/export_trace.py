"""Export the Claude Code session transcript(s) that produced this repo, with personal details scrubbed.

Writes
  trace/session.jsonl.gz            the full main-session transcript (every message, tool call and result)
  trace/subagents/<id>.jsonl.gz     transcripts of the helper agents
  trace/TRACE.md                    a readable rendering: user prompts, Claude's replies, tool calls and
                                    (truncated) tool results, in order

Scrubbing (applied to every string): e-mail addresses, the Hugging Face whoami response (name, e-mail,
orgs, billing), and the claude.ai organisation id. No secrets were present (auth was proxy-injected;
the only token-like strings are placeholders such as `hf_dummy`).

  python scripts/export_trace.py ~/.claude/projects/<project>/<session>.jsonl trace/
"""
import gzip
import json
import re
import sys
from pathlib import Path

SCRUB = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[redacted-email]"),
    (re.compile(r'\{"type":"user","id":"[0-9a-f]+","name":"[^"]+","fullname".*', re.S), "{hf whoami response redacted}"),
    (re.compile(r'(fullname|avatarUrl|billingMode|canPay|periodEnd|isPro|emailVerified)(\\*")\s*:\s*\\*"?[^,\\}"]*'),
     r'\1\2:[redacted]'),  # also matches escaped copies inside nested JSON strings
    (re.compile(r"/avatars/[0-9a-f]+\.svg"), "[redacted]"),
    (re.compile(r"David Lindenbaum"), "[redacted-name]"),
    (re.compile(r"2e2c6275-[0-9a-f-]{27,}"), "[redacted-org-id]"),
]
MAX_RESULT = 1500  # chars of each tool result shown in TRACE.md


def scrub(x):
    if isinstance(x, str):
        for pat, rep in SCRUB:
            x = pat.sub(rep, x)
        return x
    if isinstance(x, list):
        return [scrub(v) for v in x]
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items()}
    return x


def load(path):
    out = []
    for line in open(path):
        try:
            out.append(scrub(json.loads(line)))
        except json.JSONDecodeError:
            pass
    return out


def write_gz(entries, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if b.get("type") == "text":
            parts.append(b["text"])
        elif b.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def render(entries):
    md = ["# Session trace", "",
          "Readable rendering of the Claude Code session that built this repo (scrubbed; tool results truncated to "
          f"{MAX_RESULT} characters). The complete record is `trace/session.jsonl.gz`.", ""]
    for e in entries:
        t, m, ts = e.get("type"), e.get("message") or {}, (e.get("timestamp") or "")[:19].replace("T", " ")
        if e.get("isCompactSummary"):
            md += [f"### [{ts}] Context compaction summary", "", "<details><summary>summary</summary>", "",
                   text_of(m.get("content")), "", "</details>", ""]
            continue
        if t == "user" and isinstance(m, dict):
            c = m.get("content")
            if isinstance(c, str) or any(b.get("type") == "text" for b in c or []):
                txt = text_of(c).strip()
                if not txt or txt.startswith("<system-reminder>") or e.get("isMeta"):
                    continue
                md += [f"## [{ts}] User", "", txt, ""]
            for b in c if isinstance(c, list) else []:
                if b.get("type") == "tool_result":
                    r = text_of(b.get("content")) if not isinstance(b.get("content"), str) else b["content"]
                    r = r if len(r) <= MAX_RESULT else r[:MAX_RESULT] + f"\n… [{len(r) - MAX_RESULT} more chars]"
                    md += ["<details><summary>tool result</summary>", "", "```", r.replace("```", "ʼʼʼ"), "```", "",
                           "</details>", ""]
        elif t == "assistant" and isinstance(m, dict):
            for b in m.get("content") or []:
                if b.get("type") == "text" and b["text"].strip():
                    md += [f"### [{ts}] Claude", "", b["text"].strip(), ""]
                elif b.get("type") == "tool_use":
                    inp = b.get("input", {})
                    desc = inp.get("description") or ""
                    body = inp.get("command") or inp.get("content") or json.dumps(inp, ensure_ascii=False, indent=1)
                    body = body if len(body) <= 4000 else body[:4000] + "\n… [truncated]"
                    md += [f"**Tool: {b.get('name')}**" + (f" — {desc}" if desc else ""), "", "```",
                           body.replace("```", "ʼʼʼ"), "```", ""]
    return "\n".join(md)


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    main_entries = load(src)
    write_gz(main_entries, out / "session.jsonl.gz")
    for sub in sorted((src.parent / src.stem / "subagents").glob("*.jsonl")):
        write_gz(load(sub), out / "subagents" / f"{sub.stem}.jsonl.gz")
    (out / "TRACE.md").write_text(render(main_entries))
    leftover = sum(1 for e in main_entries if re.search(r"@gmail\.com", json.dumps(e)))
    print("entries", len(main_entries), "leftover e-mail matches", leftover)


if __name__ == "__main__":
    main()

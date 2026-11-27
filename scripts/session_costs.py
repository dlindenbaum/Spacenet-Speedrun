"""What this project cost: Claude (the agent that did the work) and Hugging Face (the compute).

Claude: token usage of every API request recorded in the session transcript(s), de-duplicated by message id,
priced at Anthropic list prices (API-equivalent; a Claude subscription is billed differently). Also reports
the running total the Claude Code harness itself recorded (`cost-state` entries).

Hugging Face: every HF Job on the account (huggingface_hub `list_jobs`), billed running time x the flavor's
per-minute price from `list_jobs_hardware`. Inference Providers spend for the VLM benchmark is taken from the
per-call costs recorded at the time (results/vlm_*.json tables in the README).

  python scripts/session_costs.py ~/.claude/projects/<project>/<session>.jsonl results/costs.json
"""
import collections
import json
import sys
from pathlib import Path

# $ per million tokens: input, output, cache read, cache write (5 min), cache write (1 h)
PRICES = {
    "claude-opus-5-5": dict(inp=4.00, out=20.00, read=0.20, w5m=5.00, w1h=8.00),
    "claude-haiku-5-5": dict(inp=0.10, out=0.50, read=0.01, w5m=0.125, w1h=0.20),
}
# Cancelled jobs report neither running_secs nor finished_at. End times from the session record:
# my `hf jobs cancel` calls, or (two jobs) the last log write when prepaid credit ran out and the platform
# cancelled them; the two earliest are upper bounds (the next launch).
CANCEL_END = {
    "6b08e4d954072e8b7168b64b": "2026-11-27T04:05:00", "6b08e4d96c5d61fff1880e44": "2026-11-27T04:05:00",  # XD_XD Shanghai/Vegas A100
    "6b08937c54072e8b7167762e": "2026-11-26T20:44:10", "6b08937b54072e8b71677628": "2026-11-26T20:44:12",  # credit ran out
    "6b07a2e5b6a90bbe997f0a28": "2026-11-26T03:49:17",  # generalist incl. SN1, cancelled
    "6b0788cfd91f621b93096933": "2026-11-26T01:34:00", "6b078642d91f621b930962b7": "2026-11-26T01:34:00",  # upper bounds
    "6b08612cd91f621b930bb80a": "2026-11-26T16:54:31", "6b085f79d91f621b930bb1b1": "2026-11-26T16:41:11",
    "6b086161b6a90bbe998107cd": "2026-11-26T16:42:15",
}
INFERENCE_PROVIDERS_USD = 0.70  # VLM benchmark (README, section 1: per-model API cost incl. prompt iterations)


def claude_usage(paths):
    per_model = collections.defaultdict(collections.Counter)
    harness = None
    span = [None, None]
    for p in paths:
        seen = {}
        for line in open(p):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = e.get("timestamp")
            if ts:
                span[0] = min(span[0] or ts, ts)
                span[1] = max(span[1] or ts, ts)
            if e.get("type") == "cost-state":
                harness = e
            m = e.get("message")
            if e.get("type") == "assistant" and isinstance(m, dict) and m.get("usage"):
                seen[m.get("id") or e.get("uuid")] = (m.get("model"), m["usage"])
        for model, u in seen.values():
            c = per_model[model]
            c["requests"] += 1
            c["input"] += u.get("input_tokens") or 0
            c["output"] += u.get("output_tokens") or 0
            c["cache_read"] += u.get("cache_read_input_tokens") or 0
            cc = u.get("cache_creation") or {}
            w5, w1 = cc.get("ephemeral_5m_input_tokens") or 0, cc.get("ephemeral_1h_input_tokens") or 0
            if not cc:
                w5 = u.get("cache_creation_input_tokens") or 0
            c["cache_write_5m"] += w5
            c["cache_write_1h"] += w1
    rows = {}
    for model, c in per_model.items():
        p = PRICES[model]
        cost = (c["input"] * p["inp"] + c["output"] * p["out"] + c["cache_read"] * p["read"]
                + c["cache_write_5m"] * p["w5m"] + c["cache_write_1h"] * p["w1h"]) / 1e6
        rows[model] = dict(c, usd=round(cost, 2),
                           usd_breakdown={k: round(v, 2) for k, v in dict(
                               input=c["input"] * p["inp"] / 1e6, output=c["output"] * p["out"] / 1e6,
                               cache_read=c["cache_read"] * p["read"] / 1e6,
                               cache_write=(c["cache_write_5m"] * p["w5m"] + c["cache_write_1h"] * p["w1h"]) / 1e6).items()})
    h = None
    if harness:
        h = dict(total_usd=round(harness["totalCostUSD"], 2),
                 api_time_h=round(harness["totalAPIDuration"] / 3.6e6, 2),
                 tool_time_h=round(harness["totalToolDuration"] / 3.6e6, 2),
                 lines_added=harness["totalLinesAdded"])
    return rows, h, span


def hf_jobs():
    from huggingface_hub import HfApi

    api = HfApi()
    price = {h.name: h.unit_cost_usd for h in api.list_jobs_hardware()}  # $ per minute
    jobs = []
    for j in api.list_jobs():
        run = (j.durations.running_secs or 0) if j.durations else 0
        if not run and j.started_at and (j.finished_at or j.id in CANCEL_END):  # cancelled: no running_secs
            import datetime as _dt
            end = j.finished_at or _dt.datetime.fromisoformat(CANCEL_END[j.id]).replace(tzinfo=_dt.timezone.utc)
            run = max(0.0, (end - j.started_at).total_seconds())
        jobs.append(dict(id=j.id, name=(j.labels or {}).get("name", ""), flavor=j.flavor,
                         created=j.created_at.isoformat()[:16], stage=j.status.stage, running_min=round(run / 60, 1),
                         usd=round(run / 60 * price.get(j.flavor, 0), 2)))
    jobs.sort(key=lambda r: r["created"])
    return jobs, price


def group(name):
    n = name.lower()
    for key, label in (("xdxd", "XD_XD 2017 reproduction"), ("osm", "XD_XD 2017 reproduction"),
                       ("cudatest", "XD_XD 2017 reproduction"), ("sam3", "SAM 3"), ("sam", "SAM 2.1"),
                       ("xfer", "Transfer / generalisation evals"), ("prep-xds", "Generalisation data prep"),
                       ("xds", "Generalisation data prep"), ("generalist", "Generalist training"),
                       ("nosn2", "No-SN2 training"), ("ms", "Multispectral ablation"), ("prep-ms", "Multispectral ablation"),
                       ("vscan", "Multispectral ablation"), ("vcheck", "Multispectral ablation"),
                       ("dino", "DINOv3"), ("convnext", "Supervised SN2 models"), ("effv2", "Supervised SN2 models"),
                       ("ens", "Supervised SN2 models"), ("seg", "Supervised SN2 models")):
        if key in n:
            return label
    return "Other (smoke tests, checks)"


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    paths = [src] + sorted((src.parent / src.stem / "subagents").glob("*.jsonl"))
    claude, harness, span = claude_usage(paths)
    jobs, price = hf_jobs()
    by_group = collections.defaultdict(lambda: dict(jobs=0, running_min=0.0, usd=0.0))
    by_flavor = collections.defaultdict(lambda: dict(jobs=0, running_min=0.0, usd=0.0))
    for j in jobs:
        for d, k in ((by_group, group(j["name"])), (by_flavor, j["flavor"])):
            d[k]["jobs"] += 1
            d[k]["running_min"] = round(d[k]["running_min"] + j["running_min"], 1)
            d[k]["usd"] = round(d[k]["usd"] + j["usd"], 2)
    hf_total = round(sum(j["usd"] for j in jobs), 2)
    claude_total = round(sum(r["usd"] for r in claude.values()), 2)
    res = dict(
        session_span_utc=span,
        claude=dict(per_model=claude, total_usd=claude_total, harness_reported=harness,
                    prices_per_mtok=PRICES,
                    note="API list-price equivalent from per-request token usage; a subscription bills differently"),
        huggingface=dict(jobs_total_usd=hf_total, inference_providers_usd=INFERENCE_PROVIDERS_USD,
                         total_usd=round(hf_total + INFERENCE_PROVIDERS_USD, 2), n_jobs=len(jobs),
                         by_group=dict(sorted(by_group.items(), key=lambda kv: -kv[1]["usd"])),
                         by_flavor=dict(sorted(by_flavor.items(), key=lambda kv: -kv[1]["usd"])),
                         price_per_min=price, jobs=jobs,
                         note="running time x list price; storage of the private dataset/bucket not included"),
        grand_total_usd=round(claude_total + hf_total + INFERENCE_PROVIDERS_USD, 2),
    )
    out.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "huggingface"}, indent=1, default=str)[:3000])
    print("HF jobs", len(jobs), "total $", hf_total)
    for k, v in res["huggingface"]["by_group"].items():
        print(f"  {k:36s} {v}")
    for k, v in res["huggingface"]["by_flavor"].items():
        print(f"  {k:20s} {v}")


if __name__ == "__main__":
    main()

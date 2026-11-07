"""Writes REVIEW.md: every case in cases.jsonl with its expected result in plain words, for a
person to check before the cases are trusted.  python evals/entry_actions/render_review.py"""

import collections
import json
from pathlib import Path

HERE = Path(__file__).parent


def ref(v):
    if isinstance(v, str) and "|" in v:
        return " or ".join(ref(x) for x in v.split("|"))
    if isinstance(v, str) and v.startswith("@"):
        return v.split(":", 1)[1]
    if isinstance(v, str) and v.startswith("new:"):
        return "new person " + v[4:]
    return v


def say(a: dict) -> str:
    t, f = a["tool"], a.get("fields", {})
    title = "/".join(a["title"]) if a.get("title") else None
    if t == "add_transaction":
        s = f'{f.get("type", "money")} {f["amount"]:,} {f.get("currency", "USD")}' + (f" ({title})" if title else "")
        return s + (f', dated {f["date"]}' if f.get("date") else "")
    if t == "log_habit":
        return f'check off {ref(f["id"])}' + (f' ×{f["count"]}' if "count" in f else "") + (f' on {f["date"]}' if f.get("date") else "")
    if t == "add_gift":
        given = ", given" if f.get("given") else ", idea" if f.get("given") is False else ""
        return f'gift for {ref(f["person_id"])} ({title}){given}'
    if t == "delete_item":
        return f'delete {f["type"]} {ref(f["id"])}' + (" (or archive it)" if a.get("alt") else "")
    rest = ", ".join(f"{k}={ref(v)}" for k, v in f.items())
    return t + (f" ({title})" if title else "") + (f": {rest}" if rest else "")


def main() -> None:
    rows = [json.loads(line) for line in (HERE / "cases.jsonl").read_text().splitlines() if line.strip()]
    out = [
        "# Test recordings for recording → AI actions", "",
        f"{len(rows)} cases. Each runs as a recording from the test account in `fixture.json` (Ali, Sara, Rami, Nour, Mom; "
        "habits Gym, Read, Meditation, Walk; projects Website redesign, Kitchen renovation; goals Run a 10k, Save 5000; "
        "Rent, Netflix and yesterday's Lunch with Rami; Rami owes 50).", "",
        "A case passes when vournal AI proposes exactly the listed changes (reading/searching is always fine), with the right "
        "amounts, people and dates, asks when the row says it must, and proposes nothing else. To-dos are not listed: those "
        "come from the normal reading, not from AI actions.", "",
        "| category | cases | en | lb | az | ar |", "|---|---|---|---|---|---|",
    ]
    cats: dict[str, list] = {}
    for r in rows:
        cats.setdefault(r["tags"][0], []).append(r)
    for c, rs in cats.items():
        n = collections.Counter(r["lang"] for r in rs)
        out.append(f"| {c} | {len(rs)} | {n['en']} | {n['lb']} | {n['az']} | {n['ar']} |")
    for c, rs in cats.items():
        out += ["", f"## {c}", ""]
        for r in rs:
            e = r["expect"]
            acts = [say(a) for a in e["actions"]]
            if e["question"] == "yes":
                acts.append("**ask a question** (don't guess)")
            elif e["question"] == "ok" and not acts:
                acts.append("ask a question")
            acts = acts or ["nothing"]
            acts += [say(a) + " *(optional)*" for a in e.get("optional", [])]
            out.append(f"- **{r['id']}** — {r['text']}")
            out.append("  - expected: " + "; ".join(acts) + ("" if e["wantsActions"] else " · *(no AI actions step)*"))
            if r.get("note"):
                out.append(f"  - note: {r['note']}")
    (HERE / "REVIEW.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

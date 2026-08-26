#!/usr/bin/env python3
"""Reconstruct the exact text Surge sent to the model for one iteration.

Surge deliberately does not store prompt or payload text. `receipts` keeps a
HASH of each — `prompt_hash` over the system message, `input_hash` over the
built payload — and `GET /v1/alerts/{id}/evidence` exposes the hash and never
the wording. That is the design: a reader needs to know two judgements were made
under the same criteria and to detect when the criteria moved, which a hash
gives them, while the wording itself is screening tradecraft.

The text is nonetheless recoverable, because everything it was built from is
durable: the stored vendor payloads, the `triage_decisions` rows that record
which post went to which call, the correlations and their linked signals, and a
deterministic `item_id` derived from `(iteration_id, raw_id, url)`.

**This script rebuilds the messages and then re-hashes them with the agents' own
functions.** Nothing is emitted as "what was sent" unless its hash matches the
receipt written at the time of the call — which is a stronger guarantee than
storing a copy would give, since a stored copy can drift from what was actually
transmitted and a matching hash cannot.

Where a hash does NOT match, the script says so and explains why rather than
printing text that might be wrong. The common cause is a prompt edited since the
run: the hash proves a reconstruction, it cannot regenerate wording that no
longer exists in the source. The receipt's `code_revision` names the commit that
does have it.

    python scripts/reconstruct_prompts.py test.db 4
    python scripts/reconstruct_prompts.py test.db 4 --out iteration-4.md
    python scripts/reconstruct_prompts.py test.db 4 --kind triage
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from surge_iw.agents import alerting                      # noqa: E402
from surge_iw.agents.triage import _representative, build_system_prompt     # noqa: E402
from surge_iw.config import load_with_mission              # noqa: E402
from surge_iw.agents.triage_schema import build_request    # noqa: E402
from surge_iw.services import receipts                     # noqa: E402
from surge_iw.services.calendar import context_block       # noqa: E402

#: Appended by `LLMAgent._call_llm_json`, not by either prompt builder, and part
#: of the system message that was actually transmitted.
JSON_SUFFIX = (
    "\n\nIMPORTANT: respond with a single valid JSON value and nothing "
    "else. No markdown fences, no commentary, no text outside the JSON."
)


class Unverified(Exception):
    """A reconstruction that does not match its receipt. Never printed as sent."""


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def connect(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        raise SystemExit(f"error: no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def receipts_for(conn: sqlite3.Connection, iteration_id: int, kind: str):
    return list(conn.execute(
        "SELECT * FROM receipts WHERE kind = ? AND iteration_id = ? "
        "ORDER BY receipt_id", (kind, iteration_id)))


def fence(text: str) -> str:
    """Fence a block, widening the delimiter if the text contains one."""
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}text\n{text}\n{ticks}"


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


def triage_leg(prompt_hash: str) -> tuple[str, str] | None:
    """Which relevance leg produced this hash.

    Derived from the hash rather than assumed from current config: the setting
    may have changed since the run, and the receipt is the only record of what
    was actually in force.
    """
    mission = _mission()
    candidates: list[Any] = [None] + list(getattr(mission, "streams", ()))
    for stream in candidates:
        for nexus in (True, False):
            prompt, version = build_system_prompt(
                mission, {"triage": {"require_nexus": nexus}}, stream)
            if receipts.sha256_hex(prompt) == prompt_hash:
                return prompt, version
    return None


_MISSION_CACHE: list = []


def use_mission_of(conn: sqlite3.Connection, iteration_id: int) -> None:
    """Load the pack the ITERATION ran under, not the one configured now.

    The receipts record `mission_id`, so the run names its own definition. Using
    the ambient configuration instead would reconstruct against whatever pack
    happens to be selected today — and since a mismatch is reported as "the
    prompt was edited after this run", pointing at the wrong pack would produce
    a confident, wrong explanation for a hash that never disagreed.

    Falls back to the configured mission for receipts written before packs
    existed, which is the best that can be done for them.
    """
    from surge_iw.services import mission as mission_service

    _MISSION_CACHE.clear()
    rows = conn.execute(
        "SELECT DISTINCT mission_id FROM receipts "
        "WHERE iteration_id = ? AND mission_id IS NOT NULL",
        (iteration_id,)).fetchall()
    names = {str(r[0]).split("/")[0] for r in rows}
    if len(names) > 1:
        raise SystemExit(
            f"iteration {iteration_id} has receipts from more than one "
            f"mission ({', '.join(sorted(names))}), which cannot happen in a "
            f"single run and means the record is inconsistent.")
    config, configured = load_with_mission()
    if names:
        name = names.pop()
        loaded = (configured if configured and configured.identifier == name
                  else mission_service.load(name, config=config))
        _MISSION_CACHE.append(loaded)
    else:
        _MISSION_CACHE.append(configured)


def _mission():
    """The pack this reconstruction is running against.

    The prompts live in a pack rather than in this repository, so a
    reconstruction is only possible against the pack that produced the run.
    The receipt records `mission_id` and `mission_hash` for exactly that
    reason: if they do not match, the mismatch is the answer, not an error to
    work around.
    """
    if not _MISSION_CACHE:
        _MISSION_CACHE.append(load_with_mission()[1])
    if _MISSION_CACHE[0] is None:
        raise SystemExit(
            "No mission is configured and the receipts name none, so there "
            "are no prompts to reconstruct from. Set `mission.name`.")
    return _MISSION_CACHE[0]


def gather_order(conn: sqlite3.Connection, iteration_id: int) -> dict[str, int]:
    """Position of every collected post, in the order triage would have seen it.

    `_gather` walks `raw_results` by `raw_id` and each payload in list order,
    keeping the first occurrence of a URL. Rebuilding that ordering is what lets
    a batch be put back in its original sequence — and order matters, because
    `batch_key` hashes the item ids as a sequence.

    Derived from the payloads rather than from `triage_skips`, so it works for
    iterations that ran before the skip record existed.
    """
    position: dict[tuple[str | None, str], int] = {}
    for raw in conn.execute(
        "SELECT r.raw_id, r.payload_json, q.stream AS stream "
        "FROM raw_results r LEFT JOIN query_queue q ON q.query_id = r.query_id "
        "WHERE r.iteration_id = ? AND r.source_type = 'SOCIAL' "
        "ORDER BY r.raw_id",
        (iteration_id,),
    ):
        try:
            payload = json.loads(raw["payload_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            url = str(item.get("url") or "").strip()
            if url and (raw["stream"], url) not in position:
                position[(raw["stream"], url)] = len(position)
    return position


def post_by_url(conn: sqlite3.Connection, iteration_id: int) -> dict[str, dict]:
    """The stored post for each URL, with its `raw_id`, as triage saw it."""
    copies: dict[tuple[str | None, str], list[dict]] = {}
    for raw in conn.execute(
        "SELECT r.raw_id, r.payload_json, q.stream AS stream "
        "FROM raw_results r LEFT JOIN query_queue q ON q.query_id = r.query_id "
        "WHERE r.iteration_id = ? AND r.source_type = 'SOCIAL' "
        "ORDER BY r.raw_id",
        (iteration_id,),
    ):
        try:
            payload = json.loads(raw["payload_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            url = str(item.get("url") or "").strip()
            if url:
                copies.setdefault((raw["stream"], url), []).append(
                    {**item, "raw_id": int(raw["raw_id"]), "url": url,
                     "stream": raw["stream"]})
    # The copy triage actually judged, chosen by the SAME rule `_gather`
    # applies — dated first, freshest, then first seen. First-occurrence was
    # good enough while copies were interchangeable; the representative rule
    # made which copy wins part of what the receipt hashed.
    return {key: _representative(items) for key, items in copies.items()}


def check_accepted(entry: dict[str, Any], receipt: Any) -> None:
    """Hold the rebuilt user message against the request that was ACCEPTED.

    `_call_llm_json` rewrites the user message between attempts, feeding back
    the parse error and the failed reply. Every other field on a receipt —
    `prompt_hash`, `input_hash`, `batch_key` — describes the FIRST variant, so
    a rebuild of the original request could be reported as byte-exact when the
    answer came from a request that no longer existed anywhere.

    Version 14 records `prompt_user_hash`, so the claim can be CHECKED rather
    than assumed. Older receipts cannot be: for those the honest answer is to
    refuse the byte-exact claim whenever `attempts > 1`, which is exactly the
    case where the two differ.
    """
    accepted = (receipt["prompt_user_hash"]
                if "prompt_user_hash" in receipt.keys() else None)
    if accepted:
        entry["accepted_ok"] = receipts.sha256_hex(entry["user"]) == accepted
        if not entry["accepted_ok"]:
            entry["problems"].append(
                "the rebuilt user message does not hash to the recorded "
                "`prompt_user_hash`. That hash covers the request the model "
                "ACTUALLY answered, so the text below is not it — a retry "
                "rewrote the request, or the inputs have changed since.")
    elif int(receipt["attempts"] or 1) > 1:
        entry["problems"].append(
            f"attempts {receipt['attempts']} and no `prompt_user_hash` on this "
            "receipt (it predates version 14). The retry loop rewrote the "
            "request between attempts and nothing recorded what it became, so "
            "the text below is the FIRST variant and cannot be claimed "
            "byte-exact.")


def calendar_suffix(conn: sqlite3.Connection, iteration_id: int) -> str:
    """The operator-calendar block exactly as triage appended it, or "".

    The same cut the agent used: events with `added_at <= started_at`, in
    `(added_at, event_id)` order, rendered by the same pure function. Events
    appended AFTER this iteration started are excluded — which is why the
    block stays byte-reconstructible however much the calendar has grown
    since: rows are immutable and the filter is a stored timestamp.
    """
    iteration = conn.execute(
        "SELECT session_id, started_at FROM iterations "
        "WHERE iteration_id = ?", (iteration_id,)).fetchone()
    if iteration is None:
        return ""
    rows = conn.execute(
        "SELECT * FROM calendar_events WHERE session_id = ? "
        "AND added_at <= ? ORDER BY added_at, event_id",
        (iteration["session_id"], iteration["started_at"])).fetchall()
    block = context_block(rows)
    return f"\n{block}" if block else ""


def rebuild_triage(conn: sqlite3.Connection, iteration_id: int) -> list[dict]:
    """One entry per triage call, verified against its receipt."""
    # Bind to the pack this ITERATION ran under, not the one
    # configured now. Called here rather than only in main() so a
    # caller using this module directly cannot skip it.
    use_mission_of(conn, iteration_id)
    out: list[dict] = []
    order = gather_order(conn, iteration_id)
    posts = post_by_url(conn, iteration_id)
    calendar = calendar_suffix(conn, iteration_id)

    for receipt in receipts_for(conn, iteration_id, "TRIAGE"):
        entry: dict[str, Any] = {"receipt": receipt, "problems": []}

        leg = triage_leg(receipt["prompt_hash"])
        if leg is None:
            entry["problems"].append(
                f"the system prompt recorded as {receipt['prompt_version']!r} "
                f"(hash {receipt['prompt_hash']}) does not match either "
                f"relevance leg in the current source. The wording was edited "
                f"after this run; recover it from code_revision "
                f"{receipt['code_revision'] or 'unrecorded'}.")
        else:
            entry["system"], entry["version"] = leg[0] + JSON_SUFFIX, leg[1]

        rows = list(conn.execute(
            "SELECT url, raw_id, stream FROM triage_decisions "
            "WHERE receipt_id = ? ORDER BY triage_id", (receipt["receipt_id"],)))
        if not rows:
            entry["problems"].append(
                "no triage_decisions reference this receipt, so the batch "
                "membership cannot be recovered.")
            out.append(entry)
            continue

        missing = [r["url"] for r in rows
                   if (r["stream"], r["url"]) not in posts]
        if missing:
            entry["problems"].append(
                f"{len(missing)} post(s) in this batch are no longer in any "
                f"stored payload — retention has purged them, and the text sent "
                f"for them is gone. First: {missing[0]}")
            out.append(entry)
            continue

        batch = sorted(
            (posts[(r["stream"], r["url"])] for r in rows),
            key=lambda p: order.get((p.get("stream"), p["url"]), 1 << 30))
        payload, _index = build_request(batch, iteration_id)
        entry["payload"] = payload
        entry["user"] = (f"Screen these {len(payload)} items.\n\n"
                         f"{json.dumps(list(payload), indent=1)}"
                         f"{calendar}")
        entry["input_ok"] = receipts.evidence_hash(payload) == receipt["input_hash"]
        entry["batch_ok"] = receipts.sha256_hex(
            ",".join(str(p["item_id"]) for p in payload), length=16
        ) == receipt["batch_key"]
        if not entry["input_ok"] or not entry["batch_ok"]:
            entry["problems"].append(
                "the rebuilt payload does not hash to the recorded "
                f"{'input_hash' if not entry['input_ok'] else 'batch_key'}. "
                "The stored posts or the request shape have changed since the "
                "run, so the text below is NOT what was sent.")
        check_accepted(entry, receipt)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------


def rebuild_alerts(conn: sqlite3.Connection, iteration_id: int) -> list[dict]:
    """One entry per alert call, verified against its receipt.

    `AlertAgent._brief` is pure over the correlation, its city and its linked
    signals, so it is reused directly rather than reimplemented — a second copy
    would drift from the one that ran.
    """
    # Bind to the pack this ITERATION ran under, not the one
    # configured now. Called here rather than only in main() so a
    # caller using this module directly cannot skip it.
    use_mission_of(conn, iteration_id)
    from surge_iw.db.database import SurgeDB

    out: list[dict] = []
    alert_prompt = _mission().prompts["alert"]
    alert_version = _mission().prompt_versions["alert"]
    system_ok = receipts.sha256_hex(alert_prompt)
    agent = alerting.AlertAgent.__new__(alerting.AlertAgent)   # no client needed

    for receipt in receipts_for(conn, iteration_id, "ALERT"):
        entry: dict[str, Any] = {"receipt": receipt, "problems": []}

        if receipt["prompt_hash"] == system_ok:
            entry["system"] = alert_prompt + JSON_SUFFIX
            entry["version"] = alert_version
        else:
            entry["problems"].append(
                f"the system prompt recorded as {receipt['prompt_version']!r} "
                f"(hash {receipt['prompt_hash']}) is not the one in the current "
                f"loaded mission ({alert_version}, hash {system_ok}). The "
                f"pack was edited after this run, or a different pack is "
                f"configured; the receipt names mission "
                f"{receipt['mission_id'] or 'unrecorded'} at digest "
                f"{(receipt['mission_hash'] or 'unrecorded')[:12]}.")

        key = str(receipt["batch_key"] or "")
        if not key.startswith("correlation:"):
            entry["problems"].append(
                f"batch_key {key!r} does not name a correlation.")
            out.append(entry)
            continue
        correlation_id = int(key.split(":", 1)[1])
        entry["correlation_id"] = correlation_id

        correlation = conn.execute(
            "SELECT * FROM correlations WHERE correlation_id = ?",
            (correlation_id,)).fetchone()
        if correlation is None:
            entry["problems"].append(
                f"correlation {correlation_id} no longer exists, so the brief "
                f"cannot be rebuilt.")
            out.append(entry)
            continue

        city = conn.execute("SELECT * FROM cities WHERE city_id = ?",
                            (correlation["city_id"],)).fetchone()
        evidence = list(conn.execute(
            "SELECT s.*, cs.contribution FROM correlation_signals cs "
            "JOIN signals s USING (signal_id) WHERE cs.correlation_id = ? "
            "ORDER BY s.signal_type, s.signal_id", (correlation_id,)))

        brief = agent._brief(correlation, city, evidence)
        entry["user"] = json.dumps(brief, indent=1)
        entry["input_ok"] = receipts.evidence_hash([brief]) == receipt["input_hash"]
        if not entry["input_ok"]:
            entry["problems"].append(
                "the rebuilt brief does not hash to the recorded input_hash. "
                "The linked signals or the brief's shape have changed since the "
                "run, so the text below is NOT what was sent.")
        check_accepted(entry, receipt)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(conn, iteration_id: int, triage: list[dict], alerts: list[dict],
           kinds: Sequence[str]) -> str:
    it = conn.execute("SELECT * FROM iterations WHERE iteration_id = ?",
                      (iteration_id,)).fetchone()
    lines: list[str] = []
    w = lines.append

    w(f"# Model messages sent in iteration {iteration_id}\n")
    w("Reconstructed from durable records and **verified against the "
      "classification receipts**. Surge stores a hash of each prompt and "
      "payload, never the text; a section is only presented as what was sent "
      "when its hash matches.\n")
    w("| | |")
    w("|---|---|")
    w(f"| Iteration | {iteration_id} (session {it['session_id']}, "
      f"seq {it['seq']}) |")
    w(f"| Started | {it['started_at']} |")
    w(f"| Outcome | {it['outcome']} |")
    if "triage" in kinds:
        w(f"| Triage calls | {len(triage)} |")
    if "alert" in kinds:
        w(f"| Alert calls | {len(alerts)} |")
    w("")

    everything = ([("TRIAGE", e) for e in triage] if "triage" in kinds else []) \
        + ([("ALERT", e) for e in alerts] if "alert" in kinds else [])
    clean = [e for _k, e in everything if not e["problems"]]
    w(f"**{len(clean)} of {len(everything)} call(s) verified byte-exact.**"
      + ("" if len(clean) == len(everything) else
         " The rest are reported below with the reason; their text is withheld "
         "or flagged rather than presented as authoritative.") + "\n")

    for kind, entries in (("Triage", triage if "triage" in kinds else []),
                          ("Alerting", alerts if "alert" in kinds else [])):
        if not entries:
            continue
        w("---\n")
        w(f"# {kind}\n")

        shared = {e.get("system") for e in entries if e.get("system")}
        if len(shared) == 1 and len(entries) > 1:
            version = next(e["version"] for e in entries if e.get("system"))
            w(f"## System message\n")
            w(f"Identical on all {len(entries)} calls · `{version}` · "
              f"verified against `prompt_hash`.\n")
            w(fence(next(iter(shared))))
            w("")
            shared_shown = True
        else:
            shared_shown = False

        for n, entry in enumerate(entries, 1):
            r = entry["receipt"]
            w(f"## {kind} call {n} of {len(entries)}\n")
            bits = [f"receipt `{r['receipt_id']}`",
                    f"model `{r['model_requested']}`"]
            if r["model_served"]:
                bits.append(f"served `{r['model_served']}`")
            if r["tokens_out"] is not None:
                bits.append(f"{r['tokens_out']} output tokens")
            bits.append(f"attempts {r['attempts']}")
            if entry.get("correlation_id"):
                bits.append(f"correlation `{entry['correlation_id']}`")
            w(" · ".join(bits) + "\n")

            if int(r["attempts"] or 1) > 1:
                w("> **`attempts > 1`.** The request was rewritten between "
                  "attempts, so the first variant and the accepted one are "
                  "different text. Receipts from version 14 record "
                  "`prompt_user_hash` and the check below is against that; "
                  "older ones cannot be verified and are refused rather than "
                  "reported byte-exact.\n")

            for problem in entry["problems"]:
                w(f"> ⚠️ **Not verified.** {problem}\n")

            if entry.get("system") and not shared_shown:
                w(f"### System message · `{entry['version']}`\n")
                w(fence(entry["system"]))
                w("")
            if entry.get("user"):
                status = "verified byte-exact" if not entry["problems"] \
                    else "UNVERIFIED — see the warning above"
                w(f"### User message · {status}\n")
                w(fence(entry["user"]))
                w("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconstruct the model messages Surge sent for one "
                    "iteration, verified against the classification receipts.")
    parser.add_argument("database", help="Path to the Surge SQLite database.")
    parser.add_argument("iteration", type=int, help="Iteration id.")
    parser.add_argument("--kind", choices=("triage", "alert", "all"),
                        default="all", help="Which calls to reconstruct.")
    parser.add_argument("--out", metavar="FILE",
                        help="Write markdown here instead of stdout.")
    args = parser.parse_args(argv)

    conn = connect(args.database)
    if not table_exists(conn, "receipts"):
        print("error: this database has no `receipts` table, so no call can be "
              "verified. Prompts are only reconstructible for iterations run "
              "after classification receipts were added.", file=sys.stderr)
        return 2
    if conn.execute("SELECT 1 FROM iterations WHERE iteration_id = ?",
                    (args.iteration,)).fetchone() is None:
        print(f"error: no iteration {args.iteration} in {args.database}",
              file=sys.stderr)
        return 1

    kinds = ("triage", "alert") if args.kind == "all" else (args.kind,)
    triage = rebuild_triage(conn, args.iteration) if "triage" in kinds else []
    alerts = rebuild_alerts(conn, args.iteration) if "alert" in kinds else []

    if not triage and not alerts:
        print(f"note: iteration {args.iteration} has no {args.kind} receipts. "
              f"Either the stage did not run, or it ran before receipts "
              f"existed.", file=sys.stderr)
        return 3

    text = render(conn, args.iteration, triage, alerts, kinds)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        problems = sum(1 for e in triage + alerts if e["problems"])
        print(f"wrote {args.out}: {len(triage)} triage call(s), "
              f"{len(alerts)} alert call(s), {problems} unverified")
    else:
        print(text)
    # Non-zero when anything could not be verified, so a script can tell.
    return 4 if any(e["problems"] for e in triage + alerts) else 0


if __name__ == "__main__":
    sys.exit(main())

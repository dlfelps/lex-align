from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import Provenance, Status
from .store import DecisionStore


def parse_since(since_str: str) -> Optional[datetime]:
    """Parse a 'since' string like '2 weeks ago' or '2026-01-01' into a datetime."""
    since_str = since_str.strip().lower()
    now = datetime.now(tz=timezone.utc)

    # Relative: "N days/weeks/months ago"
    parts = since_str.split()
    if len(parts) == 3 and parts[2] == "ago":
        try:
            n = int(parts[0])
            unit = parts[1].rstrip("s")
            if unit == "day":
                return now - timedelta(days=n)
            elif unit == "week":
                return now - timedelta(weeks=n)
            elif unit == "month":
                return now - timedelta(days=n * 30)
        except ValueError:
            pass

    # Absolute ISO date
    try:
        return datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    return None


def load_events(sessions_dir: Path, since: Optional[datetime] = None) -> list[dict]:
    if not sessions_dir.exists():
        return []
    events = []
    for log_file in sessions_dir.glob("*.jsonl"):
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if since:
                        ts = datetime.fromisoformat(entry.get("timestamp", ""))
                        if ts < since:
                            continue
                    events.append(entry)
                except Exception:
                    pass
    return events


def generate_report(sessions_dir: Path, store: DecisionStore, since_str: Optional[str] = None) -> str:
    since = parse_since(since_str) if since_str else None
    events = load_events(sessions_dir, since)

    voluntary = [e for e in events if e.get("event_type") == "voluntary"]
    automated = [e for e in events if e.get("event_type") == "automated"]

    retrieval_cmds = {"show", "plan", "history", "check-constraint"}
    retrieval_events = [e for e in voluntary if e.get("command") in retrieval_cmds]
    retrieval_counts: Counter = Counter(e.get("command") for e in retrieval_events)

    most_viewed = _most_common_targets(retrieval_events, "show")
    most_queried = _most_common_targets(retrieval_events, "plan")

    # Derive write counts from decision files (authoritative ground truth).
    # Session logs are gitignored and may not reflect promotions done outside
    # an active Claude Code session.
    all_decisions = store.load_all()
    since_date = since.date() if since else None
    written = [
        d for d in all_decisions
        if d.status in (Status.ACCEPTED, Status.SUPERSEDED, Status.REJECTED)
        and (since_date is None or d.created >= since_date)
    ]
    promote_count = sum(1 for d in written if d.provenance is not None)
    propose_count = sum(1 for d in written if d.provenance is None)
    total_writes = promote_count + propose_count

    reconciliation_events = [e for e in automated if e.get("command") == "reconciliation"]
    session_start_recon = len([e for e in reconciliation_events if "session" in e.get("targets", [])])
    post_edit_recon = len(reconciliation_events) - session_start_recon

    promotion_opportunities = [e for e in automated if e.get("command") == "pre-tool-use"]
    opportunity_count = len(promotion_opportunities)
    promotion_ratio = f"{int(promote_count/opportunity_count*100)}%" if opportunity_count else "N/A"

    observed = [d for d in all_decisions if d.status == Status.OBSERVED]
    recon_count = sum(1 for d in observed if d.provenance == Provenance.RECONCILIATION)
    manual_count = sum(1 for d in observed if d.provenance == Provenance.MANUAL)

    since_label = f" --since \"{since_str}\"" if since_str else ""
    lines = [f"$ lex-align report{since_label}"]
    lines.append("")

    total_retrieval = sum(retrieval_counts.values())
    lines.append(f"Retrieval ({total_retrieval} voluntary queries)")
    for cmd in ("show", "plan", "history", "check-constraint"):
        count = retrieval_counts.get(cmd, 0)
        extra = ""
        if cmd == "show" and most_viewed:
            extra = f"   most-viewed: {', '.join(most_viewed)}"
        elif cmd == "plan" and most_queried:
            truncated = [t[:40] + "..." if len(t) > 40 else t for t in most_queried]
            extra = f"   most-queried topics: {', '.join(truncated)}"
        lines.append(f"  {cmd:<20} {count}{extra}")

    lines.append("")
    lines.append(f"Writes ({total_writes} records created)")
    lines.append(f"  {'propose':<20} {propose_count}")
    lines.append(f"  {'promote':<20} {promote_count}   observed -> accepted")

    lines.append("")
    lines.append("Integrity")
    lines.append(f"  Reconciliation events:        {len(reconciliation_events)}")
    lines.append(f"    via session start:          {session_start_recon}")
    lines.append(f"    via post-edit hook:         {post_edit_recon}")
    lines.append(f"  Promotion opportunities:     {opportunity_count}   (edit-time prompts fired)")
    lines.append(f"  Promotions resulting:         {promote_count}   ({promotion_ratio} of opportunities)")

    lines.append("")
    lines.append(f"Observed entries: {len(observed)} total")
    lines.append(f"  via reconciliation: {recon_count:>3}   (created when propose was skipped)")
    lines.append(f"  via manual:         {manual_count:>3}")

    return "\n".join(lines)


def _most_common_targets(events: list[dict], command: str, n: int = 3) -> list[str]:
    counter: Counter = Counter()
    for e in events:
        if e.get("command") == command:
            counter.update(e.get("targets", []))
    return [item for item, _ in counter.most_common(n)]

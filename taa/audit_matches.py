"""Interactive curation tool for unknown interventions.

Reads `data/_unknowns-*.txt` files (written by the refresh pipeline whenever
a CT.gov intervention doesn't match any drug in `data/drug_modality.yaml`),
aggregates by frequency across antigens, and presents one-keypress flagging:

  a = ADC          m = mAb          b = bispecific
  c = CAR-T        v = vaccine      r = radioligand
  o = other        s = skip (write to _skipped.yaml)
  u = undo last    q = quit + summary

Categorized drugs are appended to the appropriate modality bucket in
drug_modality.yaml (preserves comments via ruamel.yaml). Skipped items go
to _skipped.yaml so they don't reappear next run.

This is the bottleneck for scaling antigens past v0.2 — the per-antigen
exclude_terms + drug_modality.yaml are the trust layer.
"""

import sys
import termios
import tty
from collections import Counter
from datetime import date
from pathlib import Path

from ruamel.yaml import YAML

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DRUG_MODALITY_FILE = DATA_DIR / "drug_modality.yaml"
SKIPPED_FILE = DATA_DIR / "_skipped.yaml"

KEY_TO_MODALITY: dict[str, str] = {
    "a": "adc",
    "m": "mab",
    "b": "bispecific",
    "c": "car-t",
    "v": "vaccine",
    "r": "radioligand",
    "o": "other",
}

ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_YELLOW = "\033[33m"


def _getch() -> str:
    """Read a single character from stdin without waiting for Enter."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


def _load_unknowns() -> Counter[str]:
    """Aggregate _unknowns-*.txt files; return Counter[intervention] = count."""
    counts: Counter[str] = Counter()
    for path in DATA_DIR.glob("_unknowns-*.txt"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                counts[line] += 1
    return counts


def _load_known_drugs(yaml: YAML) -> set[str]:
    """All drug strings already in drug_modality.yaml, lowercased for matching."""
    if not DRUG_MODALITY_FILE.exists():
        return set()
    raw = yaml.load(DRUG_MODALITY_FILE.read_text())
    known: set[str] = set()
    for drugs in (raw or {}).values():
        for d in drugs or []:
            known.add(d.lower())
    return known


def _load_skipped(yaml: YAML) -> set[str]:
    if not SKIPPED_FILE.exists():
        return set()
    raw = yaml.load(SKIPPED_FILE.read_text()) or []
    return {entry.get("intervention", "").lower() for entry in raw}


def _intervention_already_known(intervention: str, known: set[str]) -> bool:
    """Substring match against known drug names (matches the runtime logic)."""
    iv_lc = intervention.lower()
    return any(known_name in iv_lc for known_name in known)


def _append_to_modality(yaml: YAML, intervention: str, modality: str) -> None:
    """Append intervention to drug_modality.yaml under the given modality bucket."""
    raw = yaml.load(DRUG_MODALITY_FILE.read_text())
    if modality not in raw:
        raw[modality] = []
    if raw[modality] is None:
        raw[modality] = []
    raw[modality].append(intervention)
    with DRUG_MODALITY_FILE.open("w") as f:
        yaml.dump(raw, f)


def _undo_modality(yaml: YAML, intervention: str, modality: str) -> None:
    """Remove the most recent appearance of intervention from a modality bucket."""
    raw = yaml.load(DRUG_MODALITY_FILE.read_text())
    if modality in raw and raw[modality]:
        for i in range(len(raw[modality]) - 1, -1, -1):
            if raw[modality][i] == intervention:
                raw[modality].pop(i)
                break
        with DRUG_MODALITY_FILE.open("w") as f:
            yaml.dump(raw, f)


def _append_to_skipped(yaml: YAML, intervention: str) -> None:
    raw = []
    if SKIPPED_FILE.exists():
        raw = yaml.load(SKIPPED_FILE.read_text()) or []
    raw.append(
        {
            "intervention": intervention,
            "skipped_on": date.today().isoformat(),
            "reason": "marked-not-a-drug or out-of-scope by audit-matches",
        }
    )
    with SKIPPED_FILE.open("w") as f:
        yaml.dump(raw, f)


def _undo_skip(yaml: YAML, intervention: str) -> None:
    if not SKIPPED_FILE.exists():
        return
    raw = yaml.load(SKIPPED_FILE.read_text()) or []
    raw = [r for r in raw if r.get("intervention") != intervention]
    with SKIPPED_FILE.open("w") as f:
        yaml.dump(raw, f)


def _print_help() -> None:
    print()
    print(f"  {ANSI_BOLD}Keys:{ANSI_RESET}")
    print(f"    {ANSI_GREEN}a{ANSI_RESET} = ADC          {ANSI_GREEN}m{ANSI_RESET} = mAb          {ANSI_GREEN}b{ANSI_RESET} = bispecific")
    print(f"    {ANSI_GREEN}c{ANSI_RESET} = CAR-T        {ANSI_GREEN}v{ANSI_RESET} = vaccine      {ANSI_GREEN}r{ANSI_RESET} = radioligand")
    print(f"    {ANSI_GREEN}o{ANSI_RESET} = other        {ANSI_YELLOW}s{ANSI_RESET} = skip         {ANSI_YELLOW}u{ANSI_RESET} = undo last")
    print(f"    {ANSI_RED}q{ANSI_RESET} = quit + summary")
    print()


def run() -> None:
    """Entry point for `taa-audit-matches`."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=2, offset=0)
    yaml.width = 200

    counts = _load_unknowns()
    known = _load_known_drugs(yaml)
    skipped = _load_skipped(yaml)

    # Filter: drop items already known or already skipped
    queue = [
        (iv, n)
        for iv, n in counts.most_common()
        if not _intervention_already_known(iv, known) and iv.lower() not in skipped
    ]

    if not queue:
        print("Nothing to curate. All unknowns are either categorized or skipped.")
        return

    print(f"\n{ANSI_BOLD}TAA Tracker · audit-matches{ANSI_RESET}")
    print(f"{len(queue)} unknown interventions to review (sorted by frequency).")
    _print_help()

    history: list[tuple[str, str]] = []  # (intervention, action_key) for undo
    summary: Counter[str] = Counter()
    idx = 0

    while idx < len(queue):
        intervention, count = queue[idx]
        print(
            f"{ANSI_DIM}[{idx + 1}/{len(queue)}]{ANSI_RESET} "
            f"{ANSI_BOLD}{intervention}{ANSI_RESET} "
            f"{ANSI_DIM}({count} occurrence{'s' if count > 1 else ''}){ANSI_RESET}"
        )
        sys.stdout.write(f"  → {ANSI_GREEN}[a/m/b/c/v/r/o]{ANSI_RESET} {ANSI_YELLOW}[s skip / u undo / q quit]{ANSI_RESET}: ")
        sys.stdout.flush()

        ch = _getch()
        sys.stdout.write(ch + "\n")
        sys.stdout.flush()

        if ch == "q":
            break

        if ch == "u":
            if not history:
                print(f"  {ANSI_DIM}(nothing to undo){ANSI_RESET}\n")
                continue
            last_iv, last_action = history.pop()
            if last_action in KEY_TO_MODALITY:
                _undo_modality(yaml, last_iv, KEY_TO_MODALITY[last_action])
                summary[KEY_TO_MODALITY[last_action]] -= 1
            elif last_action == "s":
                _undo_skip(yaml, last_iv)
                summary["skipped"] -= 1
            print(f"  {ANSI_YELLOW}↶ undid: {last_iv}{ANSI_RESET}\n")
            idx -= 1  # back up so user re-sees the undone item
            continue

        if ch == "s":
            _append_to_skipped(yaml, intervention)
            history.append((intervention, "s"))
            summary["skipped"] += 1
            print(f"  {ANSI_DIM}skipped → _skipped.yaml{ANSI_RESET}\n")
            idx += 1
            continue

        if ch in KEY_TO_MODALITY:
            modality = KEY_TO_MODALITY[ch]
            _append_to_modality(yaml, intervention, modality)
            history.append((intervention, ch))
            summary[modality] += 1
            print(f"  {ANSI_GREEN}→ {modality}{ANSI_RESET} (drug_modality.yaml)\n")
            idx += 1
            continue

        # Unknown key
        print(f"  {ANSI_RED}? unknown key{ANSI_RESET}")
        _print_help()

    # Summary
    print(f"\n{ANSI_BOLD}Session summary{ANSI_RESET}")
    total_added = sum(c for k, c in summary.items() if k != "skipped")
    print(f"  Added {total_added} drug{'s' if total_added != 1 else ''} to drug_modality.yaml")
    for modality, n in sorted(summary.items()):
        if modality != "skipped" and n:
            print(f"    · {modality}: {n}")
    if summary.get("skipped"):
        print(f"  Skipped {summary['skipped']} item{'s' if summary['skipped'] != 1 else ''} → _skipped.yaml")
    print(f"  {len(queue) - idx if idx < len(queue) else 0} item{'s' if len(queue) - idx != 1 else ''} remaining for next run")
    print()
    print(f"  {ANSI_DIM}Tip: run `taa-refresh` to see the new categorizations applied.{ANSI_RESET}")

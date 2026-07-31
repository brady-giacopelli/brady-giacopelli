#!/usr/bin/env python3
"""Plan and apply contribution commits that make the profile calendar spell BRADY.

The calendar is the source of truth for the animation. This script only adds
commits to this repository; it never edits or rewrites commits in another
repository. The first run boosts the BRADY bitmap, then later runs only add a
one-commit baseline for dates that newly enter the 52-week window. Because the
commits retain their authored dates, GitHub's calendar naturally shifts them
left as weeks roll forward.

Use ``--apply`` only from a clean checkout. Without it, the script is a dry
run and prints the exact mode, target, and number of commits it would create.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ACTIVE_COLUMNS = 52
ROWS = 7
PIXEL_MINIMUM = 100
PIXEL_MARGIN = 25
LEDGER_SCHEMA = "brady-seed-v1"
SEED_PREFIX = "[brady-seed]"
REPO_ROOT = Path(__file__).resolve().parents[1]
PATTERN_PATH = REPO_ROOT / "assets" / "brady-pattern-v1.json"
LEDGER_PATH = REPO_ROOT / "data" / "brady-seed-ledger.jsonl"

GRAPHQL_QUERY = """
query ($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        weeks {
          contributionDays {
            contributionCount
            date
            weekday
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start.weekday() != 6 or self.end.weekday() != 5:
            raise ValueError("seed window must start Sunday and end Saturday")
        if (self.end - self.start).days + 1 != ACTIVE_COLUMNS * ROWS:
            raise ValueError("seed window must contain exactly 52 complete weeks")


@dataclass(frozen=True)
class SeedCommit:
    cell_date: date
    role: str


@dataclass(frozen=True)
class SeedPlan:
    cutoff: date
    window: DateWindow
    mode: str
    target_count: int | None
    maximum_real_count: int
    baseline_commits: int
    pixel_commits: int
    commits: tuple[SeedCommit, ...]


def _as_utc_date(value: date | datetime | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime cutoffs must include a timezone")
        return value.astimezone(timezone.utc).date()
    return value


def window_for_utc(value: date | datetime | None = None) -> DateWindow:
    cutoff = _as_utc_date(value)
    days_since_sunday = (cutoff.weekday() + 1) % 7
    current_week_start = cutoff - timedelta(days=days_since_sunday)
    end = current_week_start - timedelta(days=1)
    start = end - timedelta(days=ACTIVE_COLUMNS * ROWS - 1)
    return DateWindow(start=start, end=end)


def window_dates(window: DateWindow) -> tuple[date, ...]:
    return tuple(
        window.start + timedelta(days=offset)
        for offset in range(ACTIVE_COLUMNS * ROWS)
    )


def load_pattern(path: Path = PATTERN_PATH) -> tuple[tuple[int, ...], ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    word = str(raw["word"])
    glyph_width = int(raw["glyph_width"])
    separator_width = int(raw["separator_width"])
    glyphs = raw["glyphs"]

    if int(raw["glyph_height"]) != ROWS or not word:
        raise ValueError("BRADY pattern must contain a non-empty seven-row word")
    if any(letter not in glyphs for letter in word):
        raise ValueError("BRADY pattern contains an undefined glyph")

    word_width = len(word) * glyph_width + (len(word) - 1) * separator_width
    if word_width != 29 or word_width > ACTIVE_COLUMNS:
        raise ValueError("BRADY pattern must occupy 29 columns and fit the grid")

    for letter, glyph in glyphs.items():
        if len(glyph) != ROWS or any(
            len(row) != glyph_width or set(row) - {"0", "1"} for row in glyph
        ):
            raise ValueError(f"glyph {letter} must be a binary {glyph_width}x{ROWS} bitmap")

    left_padding = (ACTIVE_COLUMNS - word_width) // 2
    right_padding = ACTIVE_COLUMNS - word_width - left_padding
    bitmap: list[list[int]] = []
    for row_index in range(ROWS):
        row: list[int] = [0] * left_padding
        for letter_index, letter in enumerate(word):
            row.extend(int(bit) for bit in glyphs[letter][row_index])
            if letter_index < len(word) - 1:
                row.extend([0] * separator_width)
        row.extend([0] * right_padding)
        bitmap.append(row)
    if any(len(row) != ACTIVE_COLUMNS for row in bitmap):
        raise ValueError("composed BRADY pattern is not 52 columns wide")
    return tuple(tuple(row) for row in bitmap)


def pattern_dates(window: DateWindow, bitmap: Sequence[Sequence[int]]) -> frozenset[date]:
    if len(bitmap) != ROWS or any(len(row) != ACTIVE_COLUMNS for row in bitmap):
        raise ValueError("pattern must be a 52-column by seven-row bitmap")
    return frozenset(
        window.start + timedelta(days=column * ROWS + row)
        for row in range(ROWS)
        for column in range(ACTIVE_COLUMNS)
        if bitmap[row][column]
    )


def normalize_calendar(calendar: Mapping[date | str, int] | Sequence[Mapping[str, object]]) -> dict[date, int]:
    """Normalize a test mapping or GitHub-style contribution-day sequence."""

    if isinstance(calendar, Mapping):
        result = {date.fromisoformat(str(key)): int(value) for key, value in calendar.items()}
    else:
        result = {}
        for item in calendar:
            item_date = date.fromisoformat(str(item["date"]))
            count_value = item.get("contributionCount", item.get("count", 0))
            result[item_date] = int(count_value or 0)
    if any(value < 0 for value in result.values()):
        raise ValueError("contribution counts cannot be negative")
    return result


def fetch_profile_calendar(token: str, username: str, endpoint: str = "https://api.github.com/graphql") -> dict[date, int]:
    if not token:
        raise ValueError("a GitHub token is required to read the contribution calendar")
    request = Request(
        endpoint,
        data=json.dumps({"query": GRAPHQL_QUERY, "variables": {"login": username}}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "brady-giacopelli-brady-seed",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (HTTPError, URLError) as error:
        raise RuntimeError(f"GitHub contribution query failed: {error}") from error

    if payload.get("errors"):
        raise RuntimeError("GitHub contribution query failed: " + "; ".join(
            str(error.get("message", error)) for error in payload["errors"]
        ))
    try:
        days = [
            day
            for week in payload["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
            for day in week["contributionDays"]
        ]
    except (KeyError, TypeError) as error:
        raise RuntimeError("GitHub returned an unexpected contribution calendar") from error
    return normalize_calendar(days)


def load_ledger(path: Path = LEDGER_PATH) -> tuple[Counter[date], frozenset[date]]:
    counts: Counter[date] = Counter()
    pixel_dates: set[date] = set()
    if not path.exists():
        return counts, frozenset()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if record.get("schema") != LEDGER_SCHEMA:
                raise ValueError("wrong schema")
            item_date = date.fromisoformat(record["date"])
            role = record["role"]
            if role not in {"baseline", "pixel"}:
                raise ValueError("wrong role")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid seed ledger entry on line {line_number}") from error
        counts[item_date] += 1
        if role == "pixel":
            pixel_dates.add(item_date)
    return counts, frozenset(pixel_dates)


def build_seed_plan(
    calendar: Mapping[date | str, int] | Sequence[Mapping[str, object]],
    *,
    as_of: date | datetime | None = None,
    ledger_counts: Mapping[date, int] | None = None,
    pixel_dates: Iterable[date] = (),
    bitmap: Sequence[Sequence[int]] | None = None,
) -> SeedPlan:
    cutoff = _as_utc_date(as_of)
    window = window_for_utc(cutoff)
    dates = window_dates(window)
    counts = normalize_calendar(calendar)
    missing = [item_date for item_date in dates if item_date not in counts]
    if missing:
        raise ValueError(
            "contribution calendar is missing dates in the 52-week window: "
            + ", ".join(item.isoformat() for item in missing[:3])
        )

    seeded = Counter(ledger_counts or {})
    real_counts = {
        item_date: max(0, counts[item_date] - seeded[item_date])
        for item_date in dates
    }
    maximum_real_count = max(real_counts.values(), default=0)
    active_pixels = pattern_dates(window, bitmap or load_pattern())
    pixel_dates_in_window = set(pixel_dates).intersection(dates)
    mode = "bootstrap" if not pixel_dates_in_window else "rollover"
    target_count = (
        max(PIXEL_MINIMUM, maximum_real_count + PIXEL_MARGIN)
        if mode == "bootstrap"
        else None
    )

    planned: list[SeedCommit] = []
    for item_date in dates:
        current_total = real_counts[item_date] + seeded[item_date]
        if mode == "bootstrap" and item_date in active_pixels:
            desired = target_count or PIXEL_MINIMUM
            role = "pixel"
        else:
            desired = 1
            role = "baseline"
        for _ in range(max(0, desired - current_total)):
            planned.append(SeedCommit(item_date, role))
            current_total += 1

    return SeedPlan(
        cutoff=cutoff,
        window=window,
        mode=mode,
        target_count=target_count,
        maximum_real_count=maximum_real_count,
        baseline_commits=sum(commit.role == "baseline" for commit in planned),
        pixel_commits=sum(commit.role == "pixel" for commit in planned),
        commits=tuple(planned),
    )


def _git(*args: str, capture: bool = True, env: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=capture,
        env=dict(env) if env else None,
    )
    return result.stdout.strip() if capture else ""


def _identity() -> tuple[str, str]:
    name = os.environ.get("BRADY_COMMIT_NAME")
    email = os.environ.get("BRADY_COMMIT_EMAIL")
    authors = [line for line in _git("log", "-n", "200", "--format=%an%x09%ae").splitlines() if line]
    for author in authors:
        candidate_name, candidate_email = author.split("\t", 1)
        if not email and "github-actions" not in candidate_email.lower() and "noreply" not in candidate_email.lower():
            email = candidate_email
        if not name and "github-actions" not in candidate_name.lower():
            name = candidate_name
        if name and email:
            break
    if not name or not email:
        raise RuntimeError(
            "could not determine the contribution author identity; set "
            "BRADY_COMMIT_NAME and BRADY_COMMIT_EMAIL"
        )
    return name, email


def apply_seed_plan(plan: SeedPlan, ledger_path: Path = LEDGER_PATH) -> None:
    if not plan.commits:
        return
    if _git("status", "--porcelain"):
        raise RuntimeError("refusing to seed a dirty working tree")
    name, email = _identity()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    committer_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    with ledger_path.open("a", encoding="utf-8") as ledger:
        for sequence, commit in enumerate(plan.commits, 1):
            ledger.write(json.dumps({
                "schema": LEDGER_SCHEMA,
                "date": commit.cell_date.isoformat(),
                "role": commit.role,
                "sequence": sequence,
            }, sort_keys=True) + "\n")
            ledger.flush()
            environment = os.environ.copy()
            environment.update({
                "GIT_AUTHOR_NAME": name,
                "GIT_AUTHOR_EMAIL": email,
                "GIT_AUTHOR_DATE": f"{commit.cell_date.isoformat()}T12:00:00+0000",
                "GIT_COMMITTER_NAME": name,
                "GIT_COMMITTER_EMAIL": email,
                "GIT_COMMITTER_DATE": committer_now,
            })
            _git("add", str(ledger_path.relative_to(REPO_ROOT)), capture=False)
            _git(
                "commit",
                "--no-verify",
                "-m",
                f"{SEED_PREFIX} {commit.role} date={commit.cell_date.isoformat()}",
                capture=False,
                env=environment,
            )


def plan_summary(plan: SeedPlan) -> str:
    target = str(plan.target_count) if plan.target_count is not None else "unchanged"
    return (
        "## BRADY contribution seed\n"
        f"- Mode: `{plan.mode}`\n"
        f"- UTC window: `{plan.window.start.isoformat()}` → `{plan.window.end.isoformat()}`\n"
        f"- Maximum non-synthetic count: `{plan.maximum_real_count}`\n"
        f"- BRADY target count: `{target}`\n"
        f"- Planned commits: `{len(plan.commits)}` "
        f"(`{plan.pixel_commits}` pixel, `{plan.baseline_commits}` baseline)\n"
    )


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="create the planned commits")
    parser.add_argument("--as-of", type=_parse_date, help="UTC date for deterministic planning")
    parser.add_argument("--calendar-file", type=Path, help="JSON fixture instead of the GitHub API")
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    parser.add_argument("--summary-file", type=Path)
    parser.add_argument("--user", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "brady-giacopelli"))
    parser.add_argument("--token", default=os.environ.get("PROFILE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    args = parser.parse_args(argv)

    if args.calendar_file:
        calendar = json.loads(args.calendar_file.read_text(encoding="utf-8"))
        if isinstance(calendar, dict) and "days" in calendar:
            calendar = calendar["days"]
    else:
        calendar = fetch_profile_calendar(args.token or "", args.user)
    ledger_counts, pixel_dates = load_ledger(args.ledger)
    plan = build_seed_plan(
        calendar,
        as_of=args.as_of,
        ledger_counts=ledger_counts,
        pixel_dates=pixel_dates,
    )
    summary = plan_summary(plan)
    print(summary, end="")
    if args.summary_file:
        with args.summary_file.open("a", encoding="utf-8") as handle:
            handle.write(summary)
    if args.apply:
        apply_seed_plan(plan, args.ledger)
        print(f"Applied {len(plan.commits)} seed commits")
    else:
        print("Dry run: no files or commits were changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

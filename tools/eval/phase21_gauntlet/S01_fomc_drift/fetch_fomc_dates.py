"""Phase-21 S01 DATA SOURCING — FOMC announcement dates 1994->now, from the source.

Builds ``research/data/phase21/fomc_dates.csv`` from federalreserve.gov ONLY (no
third-party compilations):

  * 1994-2020: ``fomchistorical<YYYY>.htm`` — panel headings like
      ``<h5>February 3-4 Meeting - 1994</h5>``
      ``<h5>February 28 Conference Call - 1994</h5>``
      ``<h5 ...>March 15 (unscheduled) Meeting - 2020</h5>``
      ``<h5 ...>March 17-18 (cancelled) Meeting - 2020</h5>``
      ``<h5 ...>March 19 (notation vote) - 2020</h5>``
  * 2021->now: ``fomccalendars.htm`` — per-year "<YYYY> FOMC Meetings" sections with
    ``fomc-meeting__month`` (may span months: "Apr/May") + ``fomc-meeting__date`` divs.

Classification (pre-registered):
  * scheduled   = a plain "Meeting" with no parenthetical qualifier.
  * unscheduled = "(unscheduled) Meeting" or "Conference Call".
  * EXCLUDED    = "(cancelled)" (never happened) and "(notation vote)" (not a meeting;
    votes circulated by notation — e.g. 2020-03-19/23/31 — are excluded from BOTH
    classes; documented limitation of the unscheduled robustness leg).

The ANNOUNCEMENT day is the LAST day of the meeting range (post-meeting statement,
~14:00 ET — captured by that day's close). Since Feb-1994 the FOMC has announced policy
after every scheduled meeting, which is why the sample starts 1994 (Lucca-Moench).

Validation (§0.5.97 — fail loudly, never write a partial CSV):
  * anchor dates that MUST be present: 1994-02-04 (first announcement-era meeting),
    2008-12-16 (ZIRP), 2017-02-01 (a Jan-31->Feb-1 cross-month span), and
    2020-03-15 as UNSCHEDULED; 2020-03-18 (cancelled) must NOT be present.
  * scheduled meetings per FULL year must be 7..9 (the Fed's cadence is 8).

Run (network egress; ~30 polite requests, 0.5s apart):
    .venv/bin/python -m tools.eval.phase21_gauntlet.S01_fomc_drift.fetch_fomc_dates
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
import urllib.request

OUT = "/home/tradeflow/tradeflow/research/data/phase21/fomc_dates.csv"
_UA = "TradeFlow research ohador@gmail.com"
_BASE = "https://www.federalreserve.gov/monetarypolicy"
HIST_YEARS = range(1994, 2021)  # fomchistorical1994.htm .. fomchistorical2020.htm
TODAY = dt.date(2026, 6, 13)  # run date — future meetings are excluded

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}
_MON_ABBR = {m[:3]: i for m, i in _MONTHS.items()}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _month_num(name: str) -> int:
    name = name.strip()
    if name in _MONTHS:
        return _MONTHS[name]
    if name[:3] in _MON_ABBR:
        return _MON_ABBR[name[:3]]
    raise ValueError(f"unknown month: {name!r}")


# --------------------------------------------------------------------------- #
# Historical pages (1994-2020): <h5 ...>HEADING - YYYY</h5>
# --------------------------------------------------------------------------- #
# date part examples: "February 3-4", "March 22", "January 31-February 1"
_H5 = re.compile(r"<h5[^>]*>([^<]+?)\s*-\s*(\d{4})\s*</h5>")
_DATE_PART = re.compile(
    r"^(?P<m1>[A-Z][a-z]+)\s+(?P<d1>\d{1,2})"
    r"(?:\s*-\s*(?:(?P<m2>[A-Z][a-z]+)\s+)?(?P<d2>\d{1,2}))?$"
)
# slash form "April/May 30-1" (seen on 2013+ historical pages)
_DATE_PART_SLASH = re.compile(
    r"^(?P<m1>[A-Z][a-z]+)/(?P<m2>[A-Z][a-z]+)\s+(?P<d1>\d{1,2})\s*-\s*(?P<d2>\d{1,2})$"
)


def _parse_heading(text: str, year: int) -> tuple[dt.date, str] | None:
    """Heading text (no year) -> (announcement_date, kind) or None if excluded."""
    t = " ".join(text.split())
    if "(cancelled)" in t or "(notation vote)" in t:
        return None
    if "Conference Call" in t:
        kind = "unscheduled"
        datepart = t.replace("Conference Call", "").strip()
    elif "(unscheduled)" in t and "Meeting" in t:
        kind = "unscheduled"
        datepart = t.replace("(unscheduled)", "").replace("Meeting", "").strip()
    elif "Meeting" in t:
        kind = "scheduled"
        datepart = t.replace("Meeting", "").strip()
    else:
        return None  # non-meeting panel (e.g. statement-only rows do not occur in h5)
    ms = _DATE_PART_SLASH.match(datepart)
    if ms:  # "April/May 30-1" -> announcement = May 1
        return dt.date(year, _month_num(ms.group("m2")), int(ms.group("d2"))), kind
    m = _DATE_PART.match(datepart)
    if not m:
        raise ValueError(f"unparseable heading date {datepart!r} ({year})")
    if m.group("d2"):  # range: announcement = LAST day (may cross months)
        mon = _month_num(m.group("m2") or m.group("m1"))
        day = int(m.group("d2"))
    else:
        mon = _month_num(m.group("m1"))
        day = int(m.group("d1"))
    return dt.date(year, mon, day), kind


def parse_historical(html: str, year: int) -> list[tuple[dt.date, str]]:
    out = []
    for text, y in _H5.findall(html):
        if int(y) != year:
            continue
        parsed = _parse_heading(text, year)
        if parsed:
            out.append(parsed)
    if not out:
        raise RuntimeError(f"historical {year}: zero meetings parsed — format drift?")
    return out


# --------------------------------------------------------------------------- #
# Current page (2021->): "<YYYY> FOMC Meetings" sections, month+date divs
# --------------------------------------------------------------------------- #
_YEAR_SECT = re.compile(r"(\d{4}) FOMC Meetings")
_MONTH_DIV = re.compile(r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>", re.S)
_DATE_DIV = re.compile(r"fomc-meeting__date[^>]*>(.*?)</div>", re.S)


def parse_current(html: str) -> list[tuple[dt.date, str]]:
    """Walk year sections; within each, pair month/date divs in document order."""
    out: list[tuple[dt.date, str]] = []
    sections = list(_YEAR_SECT.finditer(html))
    for i, sec in enumerate(sections):
        year = int(sec.group(1))
        chunk = html[sec.end() : sections[i + 1].start() if i + 1 < len(sections) else len(html)]
        months = [(m.start(), m.group(1)) for m in _MONTH_DIV.finditer(chunk)]
        dates = [(m.start(), m.group(1)) for m in _DATE_DIV.finditer(chunk)]
        if len(months) != len(dates):
            raise RuntimeError(
                f"current {year}: month/date div count mismatch {len(months)} vs {len(dates)}"
            )
        for (_, mon_txt), (_, date_html) in zip(months, dates, strict=True):
            date_txt = re.sub(r"<[^>]+>", " ", date_html)
            date_txt = " ".join(date_txt.split())
            if "cancelled" in date_txt.lower() or "notation" in date_txt.lower():
                continue
            kind = "unscheduled" if "unscheduled" in date_txt.lower() else "scheduled"
            nums = re.findall(r"\d{1,2}", date_txt)
            if not nums:
                continue  # e.g. a TBD row
            last_day = int(nums[-1] if len(nums) >= 2 else nums[0])
            # month cell may span: "Apr/May", "Jan/Feb", "Oct/Nov" -> last month
            mon_name = mon_txt.split("/")[-1].strip()
            mon = _month_num(mon_name)
            try:
                d = dt.date(year, mon, last_day)
            except ValueError as exc:
                raise RuntimeError(f"current {year}: bad date {mon_txt!r} {date_txt!r}") from exc
            if d <= TODAY:
                out.append((d, kind))
    if not out:
        raise RuntimeError("current page: zero meetings parsed — format drift?")
    return out


# --------------------------------------------------------------------------- #
def main() -> int:
    rows: list[tuple[dt.date, str]] = []
    for year in HIST_YEARS:
        url = f"{_BASE}/fomchistorical{year}.htm"
        html = _get(url)
        got = parse_historical(html, year)
        rows.extend(got)
        n_sched = sum(1 for _, k in got if k == "scheduled")
        print(f"[DATA] {year}: {len(got)} meetings ({n_sched} scheduled)")
        time.sleep(0.5)
    cur = parse_current(_get(f"{_BASE}/fomccalendars.htm"))
    by_year: dict[int, int] = {}
    for d, _k in cur:
        by_year[d.year] = by_year.get(d.year, 0) + 1
    print(f"[DATA] current page: {len(cur)} meetings {dict(sorted(by_year.items()))}")
    rows.extend(cur)

    rows = sorted(set(rows))
    dates = {d for d, _ in rows}

    # ---- validation anchors (§0.5.97) ------------------------------------ #
    anchors_present = [dt.date(1994, 2, 4), dt.date(2008, 12, 16), dt.date(2017, 2, 1)]
    for a in anchors_present:
        if a not in dates:
            raise RuntimeError(f"anchor {a} MISSING — parse is wrong")
    if (dt.date(2020, 3, 15), "unscheduled") not in rows:
        raise RuntimeError("2020-03-15 must be present as UNSCHEDULED")
    if dt.date(2020, 3, 18) in dates:
        raise RuntimeError("2020-03-18 (cancelled meeting) must NOT be present")
    sched_per_year: dict[int, int] = {}
    for d, k in rows:
        if k == "scheduled":
            sched_per_year[d.year] = sched_per_year.get(d.year, 0) + 1
    for y in range(1994, TODAY.year):  # full years only
        n = sched_per_year.get(y, 0)
        if not 7 <= n <= 9:
            raise RuntimeError(f"{y}: {n} scheduled meetings (expected 7-9) — parse is wrong")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        fh.write("announcement_date,kind\n")
        for d, k in rows:
            fh.write(f"{d.isoformat()},{k}\n")
    n_s = sum(1 for _, k in rows if k == "scheduled")
    n_u = len(rows) - n_s
    print(f"[DATA] wrote {OUT}: {len(rows)} rows ({n_s} scheduled, {n_u} unscheduled)")
    print(f"[DATA] span {rows[0][0]} .. {rows[-1][0]}; anchors OK; per-year cadence OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

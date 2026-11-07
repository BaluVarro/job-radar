"""Scores each posting 0-100 against config/profile.yaml and explains why.

The score is a sum of transparent parts, so you can always see why a job ranked high:

  title       0..30   best-matching target job title
  skills      0..35   your skills mentioned in the title/description (weights add up)
  level     -60..+10  entry-level wording (+), senior title or too many years required (-)
  location    0..10   Budapest/Hungary or remote/EU
  languages -30..+6   other languages marked as required (-), your bonus languages (+)
  avoid     -40       a field you excluded (tax, audit, ...) in the title

Descriptions are fetched once per posting (see fetch_description) and cached in the database.
Postings whose page couldn't be read are scored from the title only, and say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from . import http

TITLE_MAX, SKILLS_MAX = 30, 35
DESCRIPTION_MAX_CHARS = 20_000

# Other languages, in English and Hungarian, that a posting might require.
OTHER_LANGUAGES = {
    "French": "french|francia", "Dutch": "dutch|holland", "Italian": "italian|olasz",
    "Polish": "polish|lengyel", "Czech": "czech|cseh", "Slovak": "slovak|szlovák",
    "Romanian": "romanian|román", "Croatian": "croatian|horvát", "Serbian": "serbian|szerb",
    "Danish": "danish|dán", "Swedish": "swedish|svéd", "Portuguese": "portuguese|portugál",
    "Russian": "russian|orosz", "Turkish": "turkish|török", "Chinese": "chinese|mandarin|kínai",
    "Japanese": "japanese|japán", "Arabic": "arabic|arab", "Greek": "greek|görög",
    "Spanish": "spanish|spanyol", "German": "german|deutsch|német",
}
# Words that make a language mention a requirement rather than a nice-to-have.
REQUIRED_WORDS = re.compile(
    r"fluen|native|proficien|required|must|excellent|mother tongue|business level|\bc1\b|\bc2\b|"
    r"kiváló|anyanyelv|tárgyalóképes|felsőfok|elvárás|szükséges", re.IGNORECASE)
OPTIONAL_WORDS = re.compile(r"advantage|asset|plus|nice to have|előny", re.IGNORECASE)

YEARS_RE = re.compile(
    r"(?:at least|minimum|min\.?|legalább)?\s*(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*\d{0,2}\s*\+?\s*"
    r"(?:years?|yrs?|év)(?:[^.\n]{0,40}?)(?:experience|tapasztalat|szakmai)", re.IGNORECASE)


@dataclass
class Score:
    total: int
    reasons: list[str] = field(default_factory=list)
    parts: dict[str, int] = field(default_factory=dict)


class Scorer:
    def __init__(self, profile: dict):
        def compile_list(items):
            return [(i["name"], int(i["weight"]), re.compile(i["pattern"], re.IGNORECASE))
                    for i in items or []]
        self.titles = compile_list(profile.get("target_titles"))
        self.skills = compile_list(profile.get("skills"))
        self.locations = compile_list(profile.get("locations"))
        self.bonus_languages = compile_list(profile.get("bonus_languages"))
        self.entry = re.compile(profile.get("entry_signals") or r"(?!)", re.IGNORECASE)
        self.avoid = re.compile(profile.get("avoid_titles") or r"(?!)", re.IGNORECASE)
        self.senior = re.compile(profile.get("senior_titles") or r"(?!)", re.IGNORECASE)
        self.max_years = int(profile.get("max_years_experience", 2))
        spoken = {s.lower() for s in profile.get("languages_spoken") or []}
        self.other_languages = {name: re.compile(rf"\b(?:{pat})", re.IGNORECASE)
                                for name, pat in OTHER_LANGUAGES.items()
                                if name.lower() not in spoken}

    def score(self, title: str, description: str = "", location: str = "", url: str = "") -> Score:
        parts: dict[str, int] = {}
        plus: list[str] = []
        minus: list[str] = []
        text = f"{title}\n{description}"

        # Title: best single match.
        best = max(((w, n) for n, w, p in self.titles if p.search(title)), default=(0, ""))
        parts["title"] = min(best[0], TITLE_MAX)
        if best[0]:
            plus.append(f"title: {best[1]}")

        # Skills: every profile skill found anywhere in title or description.
        found = [(n, w) for n, w, p in self.skills if p.search(text)]
        parts["skills"] = min(sum(w for _, w in found), SKILLS_MAX)
        if found:
            top = [n for n, _ in sorted(found, key=lambda x: -x[1])[:4]]
            plus.append("skills: " + ", ".join(top) + (f" +{len(found) - 4}" if len(found) > 4 else ""))

        # Level: entry-level wording vs. years of experience.
        level = 0
        if self.senior.search(title):
            level -= 30
            minus.append("senior title")
        elif self.entry.search(title):
            level += 10
            plus.append("entry-level")
        elif self.entry.search(description):
            level += 6
            plus.append("entry-level (in description)")
        years = [int(m.group(1)) for m in YEARS_RE.finditer(description)]
        years = [y for y in years if 0 < y < 30]
        if years:
            required = max(years)
            if required >= self.max_years + 3:
                level -= 30
                minus.append(f"{required}+ years experience")
            elif required > self.max_years:
                level -= 15
                minus.append(f"{required} years experience")
        parts["level"] = level

        # Location: best match in location text, URL or title.
        where = f"{location} {url} {title}"
        loc = max(((w, n) for n, w, p in self.locations if p.search(where)), default=(0, ""))
        parts["location"] = loc[0]
        if loc[0]:
            plus.append(loc[1])

        # Languages: required languages you don't speak, and bonus languages.
        lang = 0
        for sentence in re.split(r"(?<=[.!?;\n])\s+", description):
            for name, pat in self.other_languages.items():
                if pat.search(sentence) and REQUIRED_WORDS.search(sentence) \
                        and not OPTIONAL_WORDS.search(sentence):
                    if f"requires {name}" not in minus:
                        minus.append(f"requires {name}")
                        lang -= 20
        lang = max(lang, -30)
        for name, w, p in self.bonus_languages:
            if p.search(description):
                lang += w
                plus.append(f"{name} valued")
        parts["languages"] = lang

        # Fields you excluded.
        parts["avoid"] = -40 if self.avoid.search(title) else 0
        if parts["avoid"]:
            minus.append("field you excluded")

        if not description:
            minus.append("title only (page not read)")

        total = max(0, min(100, sum(parts.values())))
        return Score(total=total, reasons=[f"+ {r}" for r in plus] + [f"− {r}" for r in minus],
                     parts=parts)


def extract_text(html: str) -> str:
    """Main readable text of a job page, without menus, scripts and footers."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"}) or soup.body
    if main is None:
        return ""
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    return text[:DESCRIPTION_MAX_CHARS]


def fetch_description(session, url: str) -> str:
    resp = http.get(session, url)
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype and "xml" not in ctype:
        return ""      # e.g. a PDF notice – scored from the title
    return extract_text(resp.text)

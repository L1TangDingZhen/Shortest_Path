#!/usr/bin/env python3
"""Refuse to commit anything that names the real round.

The repository is a generic tool; the maintainer's actual delivery area
is private (see the privacy rules in the docs). Remembering that is not
enough -- it has been forgotten once, and the names reached a public
commit message -- so this checks mechanically.

    python tools/privacy_guard.py --staged            # what a commit would add
    python tools/privacy_guard.py --message FILE      # a commit message
    python tools/privacy_guard.py --tracked           # every tracked file
    python tools/privacy_guard.py --history           # every commit, ever

The list of names is never written down here. It is read at run time
from the gitignored round data, so the guard always matches the real
round and this file stays safe to publish. With no such data present
the guard passes: there is nothing to protect.

Installed as .git/hooks/pre-commit and commit-msg by
tools/install_hooks.sh. Bypass a false positive with --no-verify.
"""

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Where private round data lives. Anything here is gitignored.
DATA_DIRS = ["data", "round.local"]
# Street names too generic to be identifying: flagging them would cry
# wolf on ordinary prose and train the user to pass --no-verify.
GENERIC = {
    # articles and joining words
    "the", "and", "of", "for", "to", "in", "on", "at",
    # what a street is called, rather than which street it is
    "street", "road", "avenue", "drive", "lane", "way", "place",
    "court", "close", "terrace", "parade", "circuit", "crescent",
    "boulevard", "highway", "grove", "rise", "walk", "row", "mall",
    "north", "south", "east", "west", "central", "main", "park",
    "hill", "view", "bridge", "station", "square", "gardens", "point",
    # OSM road classes, which appear in placeholder names and all over
    # the source: flagging them would make the guard cry wolf
    "primary", "secondary", "tertiary", "trunk", "link", "residential",
    "unclassified", "motorway", "cycleway", "pedestrian", "living",
    "unnamed", "service", "footway", "path", "track", "steps", "none",
}
MIN_LENGTH = 4          # shorter tokens match too much ordinary text


def private_names():
    """Street names of the real round, from the gitignored data."""
    names = set()
    for folder in DATA_DIRS:
        for path in (ROOT / folder).rglob("*.csv"):
            try:
                with open(path, newline="", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    if not reader.fieldnames or "name" not in reader.fieldnames:
                        continue
                    for row in reader:
                        # only streets that are actually part of the
                        # round: a connector the solver merely rides
                        # through says nothing about the territory
                        if (row.get("service") or "").strip() in ("1", "2"):
                            names.add(row.get("name") or "")
            except (OSError, csv.Error, UnicodeDecodeError):
                continue
    tokens = set()
    for name in names:
        # "(unnamed primary_link)" is a placeholder the extraction
        # invents, not a name anyone could recognise the round from
        if name.strip().startswith("("):
            continue
        for word in re.split(r"[^A-Za-z]+", name):
            if len(word) >= MIN_LENGTH and word.lower() not in GENERIC:
                tokens.add(word.lower())
    return tokens


def scan(text, tokens):
    """Which private names appear, and on which lines."""
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        for word in set(re.findall(r"[A-Za-z]{%d,}" % MIN_LENGTH, line)):
            if word.lower() in tokens:
                hits.append((number, word, line.strip()[:70]))
    return hits


def staged_additions():
    """{path: text} of the lines a commit would ADD."""
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, cwd=ROOT).stdout.split()
    out = {}
    for path in names:
        diff = subprocess.run(
            ["git", "diff", "--cached", "--unified=0", "--", path],
            capture_output=True, text=True, cwd=ROOT).stdout
        added = [line[1:] for line in diff.splitlines()
                 if line.startswith("+") and not line.startswith("+++")]
        if added:
            out[path] = "\n".join(added)
    return out


def report(found, tokens):
    print("\nBLOCKED: this would publish the name of a street on your "
          "round.\n", file=sys.stderr)
    for where, hits in found.items():
        for number, word, line in hits:
            place = f"{where}:{number}" if number else where
            print(f"  {place}\n      {word}  |  {line}", file=sys.stderr)
    print(f"\nThe {len(tokens)} names checked come from your gitignored "
          f"round data;\nthis file lists none of them. Reword the text, "
          f"or use\n  git commit --no-verify\nif it is genuinely a false "
          f"positive.\n", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--message", metavar="FILE")
    ap.add_argument("--tracked", action="store_true")
    ap.add_argument("--history", action="store_true")
    args = ap.parse_args()

    tokens = private_names()
    if not tokens:
        return 0            # no private data here, nothing to protect

    found = {}
    if args.staged:
        for path, text in staged_additions().items():
            hits = scan(text, tokens)
            if hits:
                found[path] = [(0, w, l) for _, w, l in hits]
    if args.message:
        text = Path(args.message).read_text(encoding="utf-8")
        # ignore the comment block git appends to the template
        text = "\n".join(l for l in text.splitlines()
                         if not l.startswith("#"))
        hits = scan(text, tokens)
        if hits:
            found["commit message"] = [(0, w, l) for _, w, l in hits]
    if args.tracked:
        files = subprocess.run(["git", "ls-files"], capture_output=True,
                               text=True, cwd=ROOT).stdout.split()
        for path in files:
            try:
                text = (ROOT / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hits = scan(text, tokens)
            if hits:
                found[path] = hits
    if args.history:
        commits = subprocess.run(["git", "rev-list", "--all"],
                                 capture_output=True, text=True,
                                 cwd=ROOT).stdout.split()
        for commit in commits:
            body = subprocess.run(["git", "log", "-1", "--format=%B", commit],
                                  capture_output=True, text=True,
                                  cwd=ROOT).stdout
            hits = scan(body, tokens)
            if hits:
                found[f"message of {commit[:8]}"] = [(0, w, l)
                                                     for _, w, l in hits]
            listing = subprocess.run(["git", "ls-tree", "-r", "--name-only",
                                      commit], capture_output=True,
                                     text=True, cwd=ROOT).stdout.split()
            for path in listing:
                blob = subprocess.run(["git", "show", f"{commit}:{path}"],
                                      capture_output=True, text=True,
                                      cwd=ROOT).stdout
                hits = scan(blob, tokens)
                if hits:
                    found[f"{commit[:8]}:{path}"] = hits

    if found:
        report(found, tokens)
        return 1
    if not (args.staged or args.message):
        print(f"clean: no private street name found "
              f"({len(tokens)} names checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Refusals that are meant to be read.

There are two completely different things a `ValueError` can be in this project.

One is a rule saying no: "a tournament needs at least two entrants", "'0' does
not play in R1M1". Those sentences are written for the person who tripped over
them, and showing them is most of what makes the pages usable — a bare 400 tells
somebody nothing about what to do next.

The other is the inside of the program leaking out. `json.JSONDecodeError` is a
subclass of `ValueError`, so a column that is no longer valid JSON produces
"Expecting value: line 1 column 1 (char 0)"; `math.log2(0)` produces "math domain
error". Neither says anything a player can act on, both describe implementation,
and a page that prints them is doing the thing CodeQL's stack-trace-exposure rule
warns about.

Telling them apart by catching `ValueError` cannot work, because both *are*
`ValueError`. So the ones meant to be read carry a type that says so. Everything
else gets a fixed sentence at the boundary.

Deliberately a subclass of `ValueError`: every existing `except ValueError`
around the rules keeps working, and callers that only care that something was
refused do not have to learn a new name.
"""
from __future__ import annotations


class RuleError(ValueError):
    """A rule refusing, in words meant for whoever hit it."""

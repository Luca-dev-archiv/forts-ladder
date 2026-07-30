"""Refusals that are meant to be read, and the codes that name them.

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

Catching `ValueError` cannot tell them apart, because both *are* `ValueError`.
So a refusal carries a **code**, and the sentence shown to anybody is looked up
from `RULE_TEXT` by that code — a literal written here, never text taken off an
exception. The exception keeps a detailed message as well, with the ids and
numbers in it, because that is what belongs in a log and in a test.

Two audiences, then, and they get different things: a log gets
`R1M1 is already decided (Alice)`, and a page gets `[FL-502] That match already
has a result.` plus whatever the route itself knows.
"""
from __future__ import annotations


class RuleError(ValueError):
    """A rule refusing.

    Deliberately a subclass of `ValueError`: every existing `except ValueError`
    around the rules keeps working, and callers that only care that something was
    refused do not have to learn a new name.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        #: Which refusal this is. The only thing a page is allowed to read.
        self.code = code


#: Code -> the sentence shown to whoever hit the rule.
#:
#: No interpolation, on purpose. The moment a message is built out of values
#: carried on the exception, the exception is back in the output — and then the
#: only thing separating a rule's wording from a parser's is a promise. A route
#: that wants to name the match or the entrant has those in its own variables.
RULE_TEXT: dict[str, str] = {
    # --- 5xx: a tournament rule
    "FL-500": "A tournament needs at least two entrants.",
    "FL-501": "A rating has to be a number.",
    "FL-502": "That match already has a result.",
    "FL-503": "That match does not have both entrants yet.",
    "FL-504": "That player is not in that match.",
    "FL-505": "That score does not decide the series.",
    "FL-506": "A result has been reported, so the names are fixed now.",
    "FL-507": "An entrant needs a name.",
    "FL-508": "Somebody with that name is already in this tournament.",
    "FL-509": "Give the tournament a name.",
    "FL-510": "There is no entrant with that number here.",
    "FL-511": "There is no such match in this bracket.",
    # --- 599: everything that was not a rule refusing
    "FL-599": "That did not work. Nothing was changed — ask an admin to look.",
}

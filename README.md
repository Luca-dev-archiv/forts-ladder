# Forts Ladder

**Automatic match tracking and a competitive ladder for [Forts](https://store.steampowered.com/app/410900/Forts/).**

The official ranked leaderboard only covers the ranked queue. Everything played
in custom lobbies — duels, brawls, league nights, tournaments — leaves no
machine-readable trace anywhere. That is why the competitive scene still keeps
its rating in a hand-maintained spreadsheet.

Forts Ladder reads the match data the game already writes, turns it into a
record, and adds the pieces the game has no concept of: map and commander
drafting, tournament brackets, spectator slot management.

---

## It only reads a text file

A tool that "reads along with the game" deserves scrutiny in a competitive
scene, so this comes first.

Forts writes every match to `users/<steamid>/log.txt`: the map, the game mode,
the roster with Steam IDs and sides, both commanders, who lost and when, and
the replay filename. **That file is the entire data source** — the game is
never running as far as this project is concerned, it just leaves a log behind.

There is one write, and it is worth naming: `ladder/lobby_config.py` edits the
host screen's **menu settings file** (`multiplayer.lua`) while the game is
closed, setting the same values you would otherwise click. It backs the file up
first and can restore it.

The dependency list is the short version of all of this: standard library,
`openpyxl` for the spreadsheet import, FastAPI for the optional server. Nothing
that could talk to a running process is in here, and `ladder/` is small enough
to read end to end in an afternoon.

---

## What counts, and who is in it

Two rules decide whether a result enters the ladder. Both are enforced in one
place — [`ladder/eligibility.py`](ladder/eligibility.py) — and pinned down by
[`tests/test_eligibility.py`](tests/test_eligibility.py), so they can be
checked rather than taken on trust.

**1. Only matches this ladder set up.** An allowlist, not "everything except
ranked". Detecting ranked games and excluding them would mean a gap in the
detection silently *counts* a ranked match; an allowlist fails the other way,
silently ignoring anything it did not create. The official ranked queue is
covered by the in-game leaderboard and is none of this project's business.

**2. Only people who opted in.** A result is rated only if every participant
has agreed to be tracked. Play someone who has not registered and the match
does not count — for either side. Their name does not go into a report line,
the live list, or the standings.

Opting in is a separate act from signing in: an account made to watch a
stream is not a request to be rated. Withdrawal works the same way round and
is retroactive — the rating is recomputed from the event list on every read, so
events involving a withdrawn player simply stop being produced.

A series that fails either rule is still stored, marked with the reason, and
left out of the maths. Dropping it silently would make "why did my game not
count?" impossible to answer.

Reading the log is deliberately *not* gated. Your own log file on your own
disk is not someone else's data; a message with their name in it is. So the
recorder always works, and the gate sits where something leaves the machine.

A lobby is marked before it exists, because its id is only created by Steam
when you host — the client declares intent, and the recorder matches that
against the id when the log reports it. The declaration expires, covers one
lobby, and is consumed on use.

```bash
python -m ladder.eligibility opt-in <SteamID64>   # once, per player
python -m ladder.eligibility arm                  # this lobby is a ladder match
python -m ladder.recorder --watch                 # must run: the id is in the log
python -m ladder.eligibility status
```

A guest's machine never saw the lobby being set up, so with a server running
both sides pull the same picture instead of disagreeing:

```bash
python -m ladder.eligibility sync --url https://your-instance.example
```

Without any of this the allowlist is empty and nothing is rated. That is the
intended default: the ladder counts nothing until someone says it should.

---

## Features

| | |
|---|---|
| **Match recorder** | Follows the game log live and writes one JSON record per match — map, roster with Steam IDs, sides, commanders, duration, winner, replay file |
| **UFER-compatible rating** | Reproduces the community's existing formula to the decimal, verified against the real spreadsheet |
| **Open ladder** | A second, always-playable rating with re-tuned constants — never mixed with the first |
| **Results that accumulate** | A finished series is reported to the server, and the shared ranking is recomputed from every reported series — so the standings grow with what is played |
| **Map & commander draft** | Symmetric veto with blind commander picks, a turn timer, and after-the-fact verification against the log |
| **Tournaments** | Single elimination with byes, seeded by rating, by the listed order or by draw; Bo1 to Bo7; persisted in SQLite |
| **Live matches** | See what is being played and ask the host for a spectator slot |
| **Slot management** | Nine clients, spectators included — planned correctly instead of discovered at the tournament |
| **Rule checks** | Map pools, monthly quotas, opponent eligibility, commander reuse |

---

## Download, and why Windows warns about it

The client is one `.exe` on the [releases
page](https://github.com/Luca-dev-archiv/forts-ladder/releases). Nothing to
install first.

**It is not code-signed, so Windows SmartScreen will warn on first run.** That
is worth stating plainly rather than leaving you to discover it: in a scene
where people are rightly wary of third-party programs, an unexpected warning is
exactly the wrong first impression. A certificate costs a few hundred euro a
year and this is a free community tool, so there is no signature to show you.

What there is instead is a checksum published with every release, which you can
check before running anything:

```powershell
Get-FileHash .\FortsLadder-0.1.1-win-x64.exe -Algorithm SHA256
```

Compare it against `SHA256SUMS.txt` from the same release. If the two differ,
do not run the file. The built-in updater performs exactly this check for you
and refuses any download that does not match — a mismatch is reported, not
retried.

If you would rather not trust a binary at all, the client builds from source
with `dotnet build ui/FortsLadder.csproj`, and the recorder and rating are
plain Python you can read.

---

## Requirements

- Windows, Forts installed via Steam (found automatically, including second
  library drives)
- Python 3.11+ for the tools and the server
- .NET 8 SDK for the desktop client

---

## Quick start

```bash
git clone https://github.com/<you>/forts-ladder
cd forts-ladder
pip install -r requirements.txt
```

Record your matches — **keep it running while you play**, because Forts clears
its log on every start:

```bash
python -m ladder.recorder --watch
```

Turn a recorded series into the report the league rules ask for, replays
included:

```bash
python -m ladder.report list
python -m ladder.report collect 1 --out ./report
```

Desktop client:

```bash
cd ui && dotnet run
```

### The server is optional, and it is not a website

Everything above works with no server at all: the recorder, the ranking, the
draft and the report line are local. The API exists so that **clients can
agree with each other** — who opted in, and which lobby was set up for a
ladder match. What it serves a human is three pages, all of them forms:

| Page | Who | What for |
|---|---|---|
| `/` | anyone signed in | Link Steam, agree to be tracked, get the code that connects the client |
| `/admin` | admins | Roles and grants, ladder names waiting to be confirmed, and whether the map pool has been published |
| `/manage/tournaments` | tournament hosts | Build a bracket from a pasted list of entrants, then report results |

There is no ladder to browse and no profile to look up: the ranking lives in
the client, and a bracket is only readable by someone signed in.

Run a server if you want live matches and tournaments:

```bash
pip install -r requirements.txt
uvicorn server.app:app
```

Then point the client at it in the Live view. Whoever runs it decides where it
lives and who can reach it; nothing about that belongs in this repository.

See [docs/setup.md](docs/setup.md) for Discord and Steam login.

---

## How the rating works

The community's system was never written down. It was reconstructed from the
spreadsheet and verified against four real rows, which
[`tests/test_ratings.py`](tests/test_ratings.py) keeps as golden tests. If one
of them fails, the ladder is incompatible — no matter how clean the rest is.

The person who maintains the spreadsheet has since confirmed the
reconstruction is correct, so the numbers below are the real ones rather than
a best guess.

| | UFER (compatible) | Open ladder |
|---|---|---|
| Expected score | Elo logistic, scale **500** (1v1) / 600 (team) | same |
| Unit of rating | the **series**: Δ = K × (games won − E × games played) | same |
| K factor | by title *and* mode (1v1 48→9) | a quarter of it |
| Cadence | one series per calendar month | any time |
| Opponent choice | own group ±1, GM only vs GM | free |
| Abuse limit | the monthly cap | 12 rated games per **pairing** per week |

The monthly cap is an administrative limit, not a rating rule: every series is
declared, checked against replays and typed in by hand. Automating that removes
the reason for the cap — but not the reason for the K factor, which is tuned
for twelve series a year. A Bo5 sweep moves **+90 points**; sensible monthly,
a random number generator at five a week.

A simulated year (16 players of known strength, all starting at 1500) ranks
players **more** accurately after 1248 open-ladder series than after 96 monthly
ones: mean rank error 0.38 versus 0.75 places.

**Ratings are derived, not stored.** Match records are the source of truth; the
table is the output of a script anyone can run over the same files and get the
same numbers. "Trust the operator" becomes "recompute it yourself".

---

## Draft

Forts has no pick/ban of any kind, so this is designed from scratch — and it is
enforceable, because the log records both the map and both commanders.

```
ban     ban    |   pick     pick   |   remainder
 A       B     |    B        A     |    decider
```

Each side bans and picks equally often. Whoever bans first picks second. If the
pool size makes the ban count odd, one map is struck **neutrally by lot** with a
reproducible seed — fair to both, unlike letting one side ban twice.

Commanders run in two stages: global bans for the whole series, then a blind
simultaneous pick per game. Sequential picking would hand the second chooser a
counter-pick advantage that has nothing to do with skill. The community rule
that a commander is spent after a win with it carries over unchanged.

`Draft.verify()` holds the plan against the recorded matches afterwards, so a
deviation is provable rather than arguable.

---

## Lobby slots

Forts allows **nine clients, and spectators count against that limit** — both
verified in game. The host screen stops at eight, but the value itself can go
higher.

That means a 4v4 with a dedicated host leaves **zero** commentator slots, and a
1v1 leaves seven. `ladder/slots.py` plans this and refuses with a reason
instead of quietly dropping someone.

---

## Project layout

```
ladder/     match recording, ratings, rules, draft, tournaments  (Python)
server/     REST API, accounts, permissions, persistence         (FastAPI + SQLite)
ui/         desktop client                                       (C# / WPF)
tests/      145 tests — no network, no game required
docs/       log format, setup guide
```

Run the tests:

```bash
python -m pytest tests/ -q
```

---

## Translations

English is the source language. Catalogs live in `ui/Locales/*.json`; adding a
language means dropping in a file — no rebuild.

```bash
FortsLadder.exe --lang de
```

The server sends language-neutral keys (`"round_key": "semi"`) and the client
translates, so a German server never puts German round names into an English
client.

---

## Status

Early, but not speculative. Verified against real data:

- the rating formula, against four rows of the real spreadsheet
- match extraction, against 63 archived game logs
- lobby pre-configuration and reading the Steam lobby ID
- the nine-client limit, and spectators counting towards it

Not done yet: Discord and Steam token exchange (both endpoints return `501`
with an explanation rather than pretending), double elimination, group stages,
and validation of the recorder against a freshly played match.

---

## Contributing

Issues and pull requests welcome. Two house rules:

1. **The log file stays the only data source.** Anything that needs to reach
   into the running game belongs in a different project — the boundary at the
   top of this file is the reason anyone installs this one.
2. **Comments explain *why*, not *what*.** Most non-obvious code here exists
   because something surprising turned up in the game's data — that reason is
   worth more than a description of the loop.

---

## License

MIT — see [LICENSE](LICENSE).

Forts is a game by EarthWork Games. This project is not affiliated with or
endorsed by EarthWork Games.

The rating system reproduced here is the community-maintained *Unofficial
Forts Elo Ranking*. This project aims to be compatible with it, not to replace
it. Credit to its maintainer is deliberately left to them to claim — being
named in someone else's repository is not the same as agreeing to be
associated with it.

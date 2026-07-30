# Error codes

Every failure the client shows carries a code like `FL-100`. Quote it — in
Discord, in a bug report, to a referee. "Something went wrong" cannot be
answered; `FL-202` can be answered in one message.

The numbers here are the numbers in `ui/Core/ErrorCodes.cs` and in
`ladder/errors.py` plus `REFUSAL_TEXT` in `server/app.py`. A code that means two
different things in two places is worse than no code at all, so a test checks
that every code the server can raise has a sentence and appears here.

The client owns 1xx–4xx; the website owns 5xx and 6xx. One list, because a
player quoting a code should not have to know which half of the project produced
it.

- **1xx** — the connection and your account (client)
- **2xx** — a match or a series (client)
- **3xx** — the game and its files (client)
- **4xx** — this program (client)
- **5xx** — a tournament rule (website)
- **6xx** — permission, account and form (website)

## 1xx — connection and account

| Code | What happened | What to do |
|---|---|---|
| `FL-100` | Your session is no longer valid. It expired, or somebody signed it out. | Sign in again. The client has already forgotten the dead session, so this will not repeat. |
| `FL-101` | The ladder server did not answer. | Check that you are online. If the address in **Live → server** is not the ladder's, put it back. If it is, the server is down — nothing you can fix from here. |
| `FL-102` | The server understood and refused. You do not have the permission this needs. | The message says which permission. Ask an admin for it; roles are granted, not requested. |
| `FL-103` | The server hit an error of its own. | Not your fault and not fixable from the client. Report it with what you were doing. |
| `FL-104` | Whatever you asked about is not there — usually a match or a draft that has ended. | Refresh the view. |
| `FL-105` | The server rejected the request as impossible. | The message says why. This is the normal answer to a rule, not a bug. |
| `FL-106` | Two things happened at once and the server kept the first. | Look at the current state before trying again; your change may already be in. |
| `FL-109` | Something the client has no specific code for. | Report it with the code and the message. |
| `FL-110` | No Steam account is linked. | **Queue → Link Steam.** Results cannot be recorded without it: a match has to be provably yours. |
| `FL-111` | You have not agreed to be tracked. | **Queue → Agree.** Nothing is recorded about you until you do, which is the point. |

## 2xx — a match or a series

| Code | What happened | What to do |
|---|---|---|
| `FL-200` | The lobby does not exist yet, so there is nothing to join. | If you are the host, open the lobby in Forts. If you are not, wait — the client picks it up within a second of it existing. |
| `FL-201` | You are playing in this match, so you cannot watch it. | Nothing to do. A spectator sees both forts, which is exactly what the blind pick hides. |
| `FL-202` | The three minutes for opening or joining the lobby ran out. | Ask your opponent for more time in the series panel, or agree a void and start again. Neither costs anything. |
| `FL-203` | You are still in a series that is not finished. | Play it out and press **Finish the series**, or agree a void. Your opponent is waiting for you specifically. |
| `FL-204` | You left a match early, so the queue is closed to you for a while. | Wait it out. It is two minutes the first time and three more each repeat, and it is forgotten after a clean day. |
| `FL-210` | The map played was not the map drafted. | Nothing to agree: that game was not counted and comes back under the same number. Play it on the drafted map. Both clients say which map was expected. |
| `FL-211` | A commander played was not the commander drafted. | Same: not counted, played again. This is the commonest honest mistake, because Forts remembers the last commander used — check it on the loadout screen before the game starts. |
| `FL-212` | The lobby settings differ from what the ladder set. | The host can put them back. The deviation is recorded either way. |
| `FL-213` | Somebody in the lobby is not one of the two who drafted. | A spectator is fine. A *player* who did not draft is not, and the series is aborted rather than rated. |
| `FL-230` | A finished game could not be matched to any series. | It stays in **Replays** unrated so you can point a referee at it. Usually means the lobby was opened outside the ladder. |
| `FL-232` | This series was not arranged by the ladder. | Nothing is wrong: it is a game you played outside the queue, recorded here for you. A referee has nothing to look at for it, which is why **ask a referee to check this** is greyed out. Rows say `LADDER` when the ladder set the lobby up. |
| `FL-231` | A game or series was matched but not counted. | The reason is on the row: usually the map or a commander was not the drafted one, in which case that game comes back for a replay. If you disagree, **ask a referee to check this** in Replays — the series is stored either way, which is why an unrated one is still shown. |

## 3xx — the game and its files

| Code | What happened | What to do |
|---|---|---|
| `FL-300` | Forts was not found. | Start it once with the client running, or set the path in **Settings**. |
| `FL-301` | No game log yet. | Forts writes it on start. Nothing to do until it has run once. |
| `FL-302` | Forts has not been restarted, so it is still using the old lobby settings. | Restart Forts. The settings file is only read at start, so the ladder's password and slot count do not exist in the running game — which is why your opponent gets no password from you. |
| `FL-303` | The lobby settings could not be written. | Usually Forts is running and holding the file, or the folder is read-only. Close Forts and try again. |

## 4xx — this program

| Code | What happened | What to do |
|---|---|---|
| `FL-400` | The client is already running. | Use the window that is open — check the tray. Two clients read the same log and would report the same game twice as the same player. |
| `FL-401` | Windows would not accept the startup entry. | The box in **Settings → In the background** goes back to showing what is actually set. Anti-virus or a managed machine can block the per-user Run key; adding a shortcut to the Startup folder by hand does the same job. |

When you ask a referee to check a series, they get a page with everything the
server holds about it: who played, the lobby, why it was not rated, what deviated
from the draft, and the replays your clients uploaded. They can take the rating
back, with a reason that stays on the record. Replays are deleted after seven
days, so ask while it still matters.

## Running in the background

Forts clears its log when the game starts, so a match nobody was watching cannot
be recovered afterwards — which makes "I forgot to open the client" an evening's
results lost. Three separate switches in **Settings → In the background**, all off
until you turn them on:

- **Closing the window keeps it in the tray** — the window closes, the program
  does not.
- **Keep recording matches while the window is closed** — the log watcher carries
  on. The tray icon is filled while it is watching and hollow while it is not, so
  the answer is one glance.
- **Start with Windows** — a per-user startup entry that launches it hidden in the
  tray. Removable from Task Manager's Startup tab like anything else.

Without the second one, closing the window stops the watching, and the tray
balloon says so the first time.

## 5xx — a tournament rule said no

These come from the website, not the client: a tournament host or a referee sees
them next to the form they were filling in. The sentence is the same every time;
whatever the page adds in brackets after it — a seat number, a match id — is what
that route knew about your request.

| Code | What happened | What to do |
|---|---|---|
| `FL-500` | A tournament needs at least two entrants. | Add people in the planner. One name is a normal stage of building a cup, but starting needs two. |
| `FL-501` | A rating has to be a number. | `nan` and `1e999` both parse and neither can be stored or sorted. Leave it blank for 1000. |
| `FL-502` | That match already has a result. | Look at the bracket. If the result is wrong, it has to be undone by whoever runs the event. |
| `FL-503` | That match does not have both entrants yet. | An earlier round has to finish first. |
| `FL-504` | That player is not in that match. | Check the name against the two shown on the match. |
| `FL-505` | That score does not decide the series. | A Bo5 needs three wins. Report the score that ended it. |
| `FL-506` | A result has been reported, so the names are fixed. | Correct names before the first result. Afterwards a stored result would come loose from the player who earned it. |
| `FL-507` | An entrant needs a name. | Type one. |
| `FL-508` | Somebody with that name is already in this tournament. | Two entrants cannot share a name — the bracket refers to them by it. |
| `FL-509` | Give the tournament a name. | Type one. |
| `FL-510` | There is no entrant with that number here. | The list changed while you had the page open. Reload it. |
| `FL-511` | There is no such match in this bracket. | Same — reload. |
| `FL-599` | Something was refused that is not one of the above. | Nothing was changed. This is the fallback for a failure that is not a rule, so it is worth reporting. |

## 6xx — permission, account and form

| Code | What happened | What to do |
|---|---|---|
| `FL-600` | Refused: the permission is missing, or it no longer applies. | The page says what is needed in brackets, e.g. `(needs Owner)`. Roles are granted, not requested. |
| `FL-601` | That ladder name is already held by somebody else. | Two accounts cannot hold one name — it is what ties a result to a person. |
| `FL-602` | Nothing to do — the form did not ask for anything. | Harmless. Usually a double submit. |
| `FL-603` | Unknown role or grant in the form. | Reload the page; it was rendered against an older list. |
| `FL-604` | You cannot change your own row. | The one mistake on that page that cannot be undone from that page. Ask another owner. |
| `FL-605` | Only an owner can change roles. | By design: an admin cannot promote anyone to their own level or above. |

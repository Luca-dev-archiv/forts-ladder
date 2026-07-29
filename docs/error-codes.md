# Error codes

Every failure the client shows carries a code like `FL-100`. Quote it — in
Discord, in a bug report, to a referee. "Something went wrong" cannot be
answered; `FL-202` can be answered in one message.

The numbers here and the constants in `ui/Core/ErrorCodes.cs` are the same
numbers. A code that means two different things in two places is worse than no
code at all, so if you add one, add it in both.

- **1xx** — the connection and your account
- **2xx** — a match or a series
- **3xx** — the game and its files
- **4xx** — this program

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
| `FL-210` | The map played was not the map drafted. | If it was a mistake, agree a void for that game and replay it. If it was not, the series is flagged for a human. |
| `FL-211` | A commander played was not the commander drafted. | Same as above. This is the commonest honest mistake: the game remembers the last commander used. |
| `FL-212` | The lobby settings differ from what the ladder set. | The host can put them back. The deviation is recorded either way. |
| `FL-213` | Somebody in the lobby is not one of the two who drafted. | A spectator is fine. A *player* who did not draft is not, and the series is aborted rather than rated. |
| `FL-230` | A finished game could not be matched to any series. | It stays in **Replays** unrated so you can point a referee at it. Usually means the lobby was opened outside the ladder. |
| `FL-231` | A game was matched but not counted. | The reason is on the row. Report it if you disagree — an unrated match is still shown, which is why. |

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
| `FL-400` | The client is already running. | Use the window that is open. Two clients read the same log and would report the same game twice. |

# What the Forts log reveals about a match

Everything here was verified against real logs (1v1 multiplayer from both
sides, 3v3/4v4 lobbies, skirmish against the built-in AI). Game version
1.38.2.

## Where it lives, and in what shape

`<Forts>/users/<steamid64>/log.txt`, **UTF-16 LE with BOM**.

Three properties you have to know, or you build something wrong on day one:

1. **The file is cleared on every game start.** If you are not reading along
   during the session, the match is gone. That is the only reason the
   recorder is a running process rather than a script you start afterwards.
2. Size and timestamp have to be read through an **open handle**
   (`os.fstat`) — NTFS updates the directory entry of open files lazily.
3. Lines can be cut off mid-write, and UTF-16 only decodes on an even byte
   count. So read by offset and buffer the remainder.

Forts also keeps copies that are *not* cleared:
`users/<steamid>/desyncs/*/` holds, on a desync, the complete session log of
**both clients** plus the replay. That is the only source for after-the-fact
analysis — and conveniently also the best test data set.

## The lines a match is built from

### Identity and lobby

```
Logged into Steam as ExamplePlayer (76561190000000001)
Setting lobby 109775240000000001 game server 90289715748830217
OnMultiStart host 1, players 2
```

`host 1` means *this* client is hosting. The lobby id is a real Steam lobby
id (universe 1, type 8 = chat, lobby instance flag) and also appears as 8
bytes little-endian in `users/<steamid>/lobby.dat`.

### Roster — this is where the Steam IDs are

```
0: ExamplePlayer, Id 1, Team 1,   PlayerLoading, join at 0, (Steam), SteamID 76561190000000001, , Local 1, ping 0.000
1: Rival, Id 2, Team 2,   PlayerLoading, join at 0, (Steam), SteamID 76561190000000003, , Local 0, ping 0.118
0: ExamplePlayer, Id 1, Team 101, PlayerPlaying, join at 0, (Steam), SteamID 76561190000000001, , Local 1, ping 0.000
```

Two traps:

- **`Team` switches number base.** In the lobby it reads 1/2, in the running
  game 101/102. Forts computes `side = teamId % 100`. Miss that and you
  count the same player twice, on two "different" sides.
- **`Local 1` is relative to the log file.** In the opponent's log a
  different player is `Local 1`. That is not a bug but the lever for
  double-reporting: both sides produce the same match key, from opposite
  perspectives.

Supplementary lines, with side and fort assignment:

```
Client Connected: Rival, index 1, id 2, side 2
Client ExamplePlayer, id 1, side 1, fortId -1
Fort Select: Client Opponent added - fort 1 on side 1 (Allowed: 1, Type: 0, IsHost: 0)
```

The `Fort Select` lines appear **during the lobby phase**, so *before* the
start. That makes it possible to check a line-up before play, not only
afterwards.

### Map, mode, commanders

```
Game mode: Team Death Match
Loading map maps/Vanilla/Vanilla.fwe
Loading map 2961610242\Microsize 3v3.fwe        <- workshop map: id only
Team1 commander: commander-da-overclocker
Team2 commander: commander-iba-spy
```

Workshop maps appear as `<workshop-id>\<name>.fwe`. Anything checking map
pools has to handle both forms.

The per-side commander is why the ladder rule "no commander twice after a
win" is machine-checkable at all.

### Result

There is **no `Winner` line.** The game does produce that text — `Winner is
team %d (%d vs %d damage inflicted)`, `Actual winner %d`, `Technical winner
%d` — but it goes to the console and never reaches the log file.

What the log does contain is each loser individually, with the in-game time:

```
7:23 ExamplePlayer has been defeated!
17:22 Opponent has been defeated!
```

The winner is derived from that: the side that still has someone standing.
The timestamp comes from the simulation and is identical on both clients —
so it is usable as part of the match key, unlike the wall clock.

Cases that cannot be derived (desync abort, no defeat logged, several sides
with survivors) are marked `unclear` rather than guessed. In a league a
wrongly scored game costs more than a missing one.

### End and replay

```
World::Execute mDone detected
Replay saved as replays/v1.38.2_Vanilla_20260719_135021.fwr
```

The replay name carries version, map and timestamp — the only wall-clock
time in the whole log, and therefore the date source for matches parsed
after the fact.

Replay sizes, for planning: a 1v1 runs about 0.8–4 MB, a 4v4 up to 80 MB.

## Limits

- **Over-segmentation:** `Loading map` marks the start of a match but also
  fires on desync recovery and on a rematch of the same map. A session log
  can therefore yield more entries than games actually played. Not yet
  validated against a freshly played match.
- **No damage timeline, no shots, no resources.** The scoring systems some
  leagues use (sniper hits, APM check, metal float) cannot be recovered from
  the log — that needs replay analysis or a mod running in the match.
- **Controlled observation only goes so far:** `Filter Lobby Name`,
  `Rejecting lobby with avoided host` and relatives do appear in the log,
  but whether avoid lists block a join via direct link is still unanswered.

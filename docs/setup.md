# Setting up Discord and Steam login

Step-by-step, so you can register everything yourself. **You create the
applications and hold the secrets — nobody else needs them, and they must
never end up in the repository.**

---

## 1. Discord application

Discord is the identity anchor: the UFER sheet lists Discord names, so a
Discord login proves the very name a player is listed under.

1. Open <https://discord.com/developers/applications> and sign in.
2. **New Application** → name it (e.g. `Forts Ladder`) → **Create**.
3. Left sidebar → **OAuth2**.
4. Copy **Client ID**. Under **Client Secret** press **Reset Secret** and copy
   the value. *It is shown once.* Store it in a password manager, not in a
   file next to the code.
5. Under **Redirects** add exactly this URL and press **Save Changes**:

   ```
   http://localhost:8000/auth/discord/callback
   ```

   For a public server, add the live URL as a **second** entry — do not
   replace the local one, or you cannot test locally any more:

   ```
   https://your-domain.example/auth/discord/callback
   ```

6. Scopes: the code requests **`identify`** only. That returns user ID and
   display name — no email, no server list, no messages. Do not add more; a
   ladder that asks for message access will not be installed by anyone.

> **What can go wrong:** the redirect URL must match *character for
> character*, including `http` vs `https` and any trailing slash. A mismatch
> gives `invalid_redirect_uri` and looks like a code bug when it is a typo in
> the dashboard.

---

## 2. Steam

Steam needs **no registration** for login. Steam OpenID is public — the
`return_to` URL is enough, and it is already built in.

A Web API key is only needed if you want to read player names or profile
pictures from Steam:

1. <https://steamcommunity.com/dev/apikey>
2. Enter the domain (`localhost` works for testing) and confirm.
3. Copy the key.

The ladder works without it. Nothing in this repository requires the key
today.

---

## 3. Environment variables

Create a file called `.env` **next to the repository, not inside it**, or set
the variables in your shell. `.gitignore` covers `.env`, but the safest secret
is one that was never in the folder.

```bash
DISCORD_CLIENT_ID=123456789012345678
DISCORD_CLIENT_SECRET=your-secret-here
LADDER_BASE_URL=http://localhost:8000
LADDER_DB=data/ladder.sqlite
# optional, only for Steam profile lookups:
STEAM_API_KEY=your-key-here
```

Windows, current shell only:

```bash
set DISCORD_CLIENT_ID=123456789012345678
```

---

## 4. Start it

```bash
pip install fastapi uvicorn
python -m uvicorn server.app:app --reload
```

Then <http://localhost:8000/docs> shows every endpoint with a try-it button.

`GET /health` should answer `{"ok": true, ...}`.

---

## 5. Make yourself the owner

The first account is a normal player — there is no built-in super user,
because a default administrator is a default way in.

1. Log in once via Discord so the account exists.
2. Promote yourself directly in the database, once:

```bash
python -c "from server.store import Store; s=Store('data/ladder.sqlite'); import sys; s.db.execute('UPDATE accounts SET role=4 WHERE discord_name=?', ('YOUR_DISCORD_NAME',)); s.db.commit(); print(s.db.execute('SELECT discord_name, role FROM accounts').fetchall())"
```

Role `4` is owner. From then on you can hand out everything through the API
and never need to touch the database again.

---

## 6. Roles and permissions

Two separate concepts, deliberately:

**Rank** — authority. Guest → Player → Caster → Admin → Owner.

**Permission** — a single capability, without a promotion. This mirrors the
official Discord, where Map Creator and Mod Maker are badges rather than
moderation levels.

| Permission | Unlocks |
|---|---|
| `tournament_host` | create and run tournaments |
| `referee` | correct results, observe any match |
| `caster` | observe even when the host closed requests |
| `map_maker`, `mod_maker`, `content_creator` | nothing — recognition only |

```bash
curl -X POST http://localhost:8000/admin/grant \
     -H "Content-Type: application/json" \
     -d '{"target_id": "<account id>", "grant": "tournament_host"}' \
     --cookie "ladder_session=<your session>"
```

A tournament host can create tournaments but cannot read other people's
accounts. That separation is the point.

---

## 7. What is still missing before this is live

Two functions deliberately answer `501` instead of pretending to work:

- **`/auth/discord/callback`** — the `code` still has to be exchanged for a
  token and `GET /users/@me` called with it.
- **`/auth/steam/callback`** — Steam OpenID requires a `check_authentication`
  call back to Steam.

Both are short, but shipping them unverified would mean anyone could claim to
be anyone. A login where that is possible is worse than no login at all —
which is why they refuse loudly rather than fail quietly.

---

## Security checklist before going public

- [ ] `DISCORD_CLIENT_SECRET` never committed — check with
      `git log -S "your-secret"` after the first push
- [ ] `data/` is in `.gitignore` (recorded matches contain **both** players'
      SteamIDs)
- [ ] `LADDER_BASE_URL` uses `https` in production; session cookies belong
      behind TLS
- [ ] Session cookies set with `Secure` and `HttpOnly` once you are on a real
      domain
- [ ] Rotate the Discord secret if it ever appeared in a screenshot or a chat
      message

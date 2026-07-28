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
# Your own Discord user id. Promoted to owner at startup — see step 5.
LADDER_OWNER_DISCORD_ID=123456789012345678
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
pip install -r requirements.txt
python -m uvicorn server.app:app --reload
```

`GET /health` should answer `{"ok": true, ...}`.

The interactive docs are off by default — publishing the full route list,
admin routes included, hands anyone the map for free. `LADDER_DOCS=1` turns
them back on at <http://localhost:8000/docs> while you are developing.

---

## 5. Make yourself the owner

Every account starts as a player, and owner can only be granted by an owner —
so on a fresh server nobody could ever become the first one. Name yourself in
the environment instead:

```bash
LADDER_OWNER_DISCORD_ID=123456789012345678
```

That is your Discord **user** id, not the application id: in Discord, enable
Developer Mode (Settings → Advanced), then right-click your own name → **Copy
User ID**.

Whoever controls the machine decides who the owner is. The alternative —
"the first account to log in wins" — hands a public server to whichever
stranger arrives first.

It is applied at every startup, over accounts that already exist too, so it
works whether you set it before or after your first login. Restart the server
once after setting it, then open `/admin`.

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

Hand them out on **`/admin`**: one row per account, one Save per person, so a
mistake affects one person rather than the whole table. Only an owner sees the
rank picker — an admin can grant permissions but not promote.

A tournament host can then build brackets on **`/manage/tournaments`**: paste
the entrants one per line, optionally `name, rating` to set the seeding, and
report each result as it comes in. They still cannot read other people's
accounts. That separation is the point.

The same grant over the API, if you prefer:

```bash
curl -X POST http://localhost:8000/admin/grant \
     -H "Content-Type: application/json" \
     -d '{"target_id": "<account id>", "grant": "tournament_host"}' \
     --cookie "ladder_session=<your session>"
```

---

## 7. Checking the login actually works

Both callbacks are implemented: the Discord `code` is exchanged for a token
and `GET /users/@me` is called with it, and Steam's OpenID assertion is
verified with a `check_authentication` call back to Steam. Neither trusts what
the browser hands it — without that check, anyone could claim to be anyone.

Two things that fail quietly if the setup is wrong:

- Discord's edge answers **403** to a request without a `User-Agent`. One is
  sent on every outbound call; if you see 403 on the token exchange, something
  is stripping headers.
- `LADDER_BASE_URL` has to match the redirect you registered exactly. It is
  what the callback URL is built from, so a mismatch here looks like Discord
  rejecting the app.

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

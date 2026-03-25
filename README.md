# CovasCast v3.0.0

> ⚠️ **Note:** This plugin was built with AI assistance (Claude). I'm not a Python expert — there may be bugs or rough edges. Feedback welcome!

Real-time Twitch integration for [COVAS:NEXT](https://ratherrude.github.io/Elite-Dangerous-AI-Integration/). The AI listens to your Twitch chat, responds verbally to mentions, reacts to channel events (follows, subs, bits, raids and more), and can display a live chat overlay on the GenUI HUD — all without interrupting your stream.

> **Looking for the simpler version?** [CovasCast v2.x](https://github.com/Lag0matic/CovasCast) uses TwitchIO 2.x with a single OAuth token and no Twitch application setup. It supports chat and moderation but not channel events or the GenUI HUD.

Built for streamers who want their AI to feel like a genuine part of the broadcast.

## What It Does

- **`@covas` mentions** — the AI responds verbally when chat tags it directly
- **Background chat awareness** — chat is passively fed into context so the AI always knows what's happening
- **Channel events** — follows, subs, resubscriptions, gift subs, bits, raids, and channel point redemptions all trigger verbal responses
- **Live chat HUD** — ask the AI to show Twitch chat on the GenUI overlay; updates in real time
- **Bot chat posting** — optionally allow the AI to post messages to chat (toggle)
- **Moderation actions** — timeout, ban, unban, delete message (individual toggles, all off by default)
- **Optional content moderation** — filter chat through OpenAI's moderation API with per-category toggles

## How It Works

CovasCast connects to Twitch via EventSub WebSocket — the modern, officially supported method. No public-facing server required. The bot subscribes to chat and channel events on startup and feeds everything into the AI's context in real time.

> ### ⚠️ Designed for a Dedicated Bot Account
> CovasCast is intended to run on a **separate Twitch account**, not your personal broadcaster account. Create a second account for the bot (e.g. `MyBotName`), generate tokens for it, and mod it in your channel with `/mod MyBotName`.
>
> This is the standard approach used by all legitimate Twitch bots and is **fully ToS compliant**.

> ### ⚠️ Requires Python 3.11+
> TwitchIO 3.x requires Python 3.11 or higher.

---

## Setup

This version requires more initial setup than v2.x, but you only need to do it once. The payoff is proper channel events and a live chat HUD.

### Step 1 — Create a Twitch Application

1. Go to the [Twitch Developer Console](https://dev.twitch.tv/console)
2. Click **Register Your Application**
3. Fill in:
   - **Name**: anything (e.g. `CovasCast`)
   - **OAuth Redirect URLs**: `http://localhost` and `https://twitchtokengenerator.com`
   - **Category**: Chat Bot
   - **Client Type**: Confidential
4. Click **Create** → **Manage**
5. Note your **Client ID** and click **New Secret** for your **Client Secret**

### Step 2 — Get Your Numeric User IDs

You need the numeric Twitch ID for both your broadcaster account and bot account. Use [StreamWeasels ID Lookup](https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/) — paste a username, get the ID.

### Step 3 — Generate Tokens

Go to [TwitchTokenGenerator](https://twitchtokengenerator.com/), enter your **Client ID** and **Client Secret** in the custom credentials section, then generate tokens for each account separately.

> ⚠️ Make sure you are **logged into the correct Twitch account** before generating each token. Generate the bot token while logged in as the bot, and the broadcaster token while logged in as your broadcaster account.

**Bot account scopes:**
- `chat:read`
- `chat:edit`
- `user:read:chat`
- `user:write:chat`
- `user:bot`
- `moderator:read:followers`
- `moderator:manage:banned_users` *(if enabling timeout/ban/unban)*
- `moderator:manage:chat_messages` *(if enabling delete messages)*

**Broadcaster account scopes:**
- `channel:bot`
- `channel:read:subscriptions`
- `bits:read`
- `channel:read:redemptions`

Note the **Access Token** and **Refresh Token** for each account. Do **not** add the `oauth:` prefix.

### Step 4 — Install the Plugin

> ⚠️ **GitHub extraction note:** When downloading from GitHub, the zip extracts to a folder like `CovasCast3-v3.0.0`. Rename it to `CovasCast3` before placing it in your plugins directory.

1. Download the latest release and extract it
2. Rename the folder to `CovasCast3`
3. Place it in:
   ```
   %appdata%\com.covas-next.ui\plugins\
   ```
4. Dependencies are bundled — no installation step needed
5. Restart COVAS:NEXT
6. Open the COVAS:NEXT menu → navigate to **CovasCast** settings
7. Fill in all fields across the three settings sections
8. Start your COVAS chat session — the bot connects automatically

### Step 5 — Mod the Bot

```
/mod MyBotName
```

Required for moderation actions (timeout, ban, delete). Not required for chat reading or posting.

---

## Settings

### Twitch Application
| Field | Description |
|---|---|
| Client ID | From the Twitch Developer Console |
| Client Secret | From the Twitch Developer Console |

### Bot Account
| Field | Description |
|---|---|
| Bot User ID | Numeric ID of the bot account |
| Bot Access Token | Access token generated while logged in as the bot |
| Bot Refresh Token | Refresh token generated while logged in as the bot |

### Broadcaster Account
| Field | Description |
|---|---|
| Channel Name | Your channel name without `#` |
| Broadcaster User ID | Numeric ID of your broadcaster account |
| Broadcaster Access Token | Access token generated while logged in as your broadcaster account |
| Broadcaster Refresh Token | Refresh token generated while logged in as your broadcaster account |
| Mention Trigger | Text that triggers a verbal response (default: `@covas`) |

---

## How The AI Responds

### Direct mentions
```
Chat:  viewer: @covas what do you think of this build?
AI:    [responds verbally on stream audio]
```

### Channel events
Follows, subs, resubscriptions, gift subs, bits, raids, and channel point redemptions all trigger an automatic verbal response.

### Background chat awareness
All other messages are passively fed into context (rate limited to one update per 10 seconds).

### On-demand status
```
"Check Twitch chat"
"Any messages from chat?"
```

---

## Live Chat HUD (GenUI)

CovasCast provides a live chat projection to the COVAS:NEXT GenUI overlay. Once connected, ask the AI to display it:

```
"Show Twitch chat on the HUD"
"Add a chat overlay to the display"
```

The overlay updates in real time as messages arrive. Mentions are flagged separately. The most recent channel alert is also shown. The AI will design the overlay to blend with any other HUD elements you have.

---

## Bot Capabilities (Optional)

All off by default. Moderation actions require the bot to be modded.

| Toggle | What it enables | Requires mod |
|---|---|---|
| Allow: Post messages to chat | AI can send messages to chat | No |
| Allow: Delete messages | AI can delete specific messages | Yes |
| Allow: Timeout users | AI can temporarily mute users | Yes |
| Allow: Ban users | AI can permanently ban users | Yes |
| Allow: Unban / untimeout users | AI can lift bans and timeouts | Yes |

> ⚠️ Bans are permanent. Use timeout for anything you might want to reverse.

### Voice Commands

```
"Tell chat we're taking a short break"
"Timeout SomeBadActor for 10 minutes"
"Ban SomeBadActor"
"Unban SomeBadActor"
```

---

## Content Moderation (Optional)

| Setting | Description |
|---|---|
| Enable OpenAI Content Moderation | Master on/off switch |
| Announce filtered messages | On = AI verbally flags filtered messages. Off = silent drop |
| OpenAI API Key | Requires billing set up at platform.openai.com |

### Category toggles (defaults tuned for gaming chat)

| Toggle | Default | Notes |
|---|---|---|
| Filter: Harassment | Off | High false positive rate in gaming chat |
| Filter: Harassment / Threatening | Off | |
| Filter: Hate | **On** | |
| Filter: Hate / Threatening | **On** | |
| Filter: Sexual | **On** | |
| Filter: Sexual / Minors | **On** | |
| Filter: Violence | Off | Will catch "kill those pirates" |
| Filter: Violence / Graphic | Off | |
| Filter: Self-harm | **On** | |
| Filter: Self-harm / Intent | **On** | |
| Filter: Self-harm / Instructions | **On** | |
| Filter: Illicit | Off | |
| Filter: Illicit / Violent | Off | |

---

## Troubleshooting

**Bot doesn't connect**
- Check all token fields are filled with no extra spaces and no `oauth:` prefix
- Make sure bot and broadcaster tokens were generated while logged into the correct accounts
- Restart COVAS:NEXT after saving settings

**Channel events not firing**
- Check the broadcaster token has the correct scopes
- Confirm the Broadcaster User ID matches your broadcaster account, not the bot

**Follow events not working specifically**
- The bot token needs `moderator:read:followers` — regenerate with that scope added

**Moderation actions not working**
- Run `/mod MyBotName` in your chat
- Check the bot token includes the relevant moderator scopes

**Mentions not triggering**
- Check the Mention Trigger field matches what chatters are typing (case insensitive)

**Moderation not catching anything**
- Check OpenAI billing is set up at platform.openai.com
- Confirm at least one category toggle is enabled

---

## Files

```
CovasCast3/
  CovasCast.py         # Main plugin
  manifest.json        # Plugin metadata
  requirements.txt     # Python dependencies (reference only — deps are bundled)
  deps/                # Bundled Python dependencies
```

---

## Version History

**v3.0.0** — Complete rewrite
- TwitchIO 3.x with EventSub WebSocket — no public server required
- Full channel event support: follows, subs, resubscriptions, gift subs, bits, raids, channel point redemptions
- Live chat HUD via GenUI projection — updates in real time
- Separate bot and broadcaster credentials for full EventSub access
- Automatic token refresh managed by TwitchIO
- Requires Python 3.11+

---

## Credits

**Author**: Lag0matic  
**Original concept**: [COVAS-Labs/COVAS-NEXT-Twitch-Integration](https://github.com/COVAS-Labs/COVAS-NEXT-Twitch-Integration)  
**COVAS:NEXT**: https://ratherrude.github.io/Elite-Dangerous-AI-Integration/  
**Twitch API**: TwitchIO library

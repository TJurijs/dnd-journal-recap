# D&D Session Recap Bot

A self-hosted Discord bot that ingests a Twitch VOD of a D&D session and posts a structured journal entry into the channel where it was invoked.

## Features

- **Twitch VOD → Journal**: `/recap <twitch_url>` downloads the VOD, transcribes the audio with Gemini, summarizes into a `chapters`-style markdown journal, and posts it in-channel.
- **Per-channel campaigns**: Each Discord channel is an isolated campaign with its own roster, scratchpad, style, and per-recap history.
- **Roster + scratchpad chained per recap**: every `/recap` reads the previous snapshot, runs incremental updates, and stores a fresh full snapshot — so you can see how the campaign evolved.
- **Self-hosted, single-tenant**: Run your own copy on a small VPS. No SaaS, no data sharing.
- **BYOK**: Bring your own Gemini API key and Discord bot token.

## Quick Start

1. **Get a Gemini API key** at [Google AI Studio](https://aistudio.google.com/apikey).
2. **Create a Discord bot** at the [Discord Developer Portal](https://discord.com/developers/applications):
   - OAuth2 → URL Generator: scopes `bot` + `applications.commands`.
   - Bot permissions: `Send Messages`, `Embed Links`, `Attach Files`, `Use Slash Commands`, `Read Message History`.
   - Privileged Gateway Intents: enable **Message Content**.
3. **Clone and configure**:
   ```bash
   git clone https://github.com/<owner>/dnd-recap-bot.git
   cd dnd-recap-bot
   cp .env.example .env
   # Edit .env: DISCORD_BOT_TOKEN, GEMINI_API_KEY (and DISCORD_GUILD_ID for instant sync).
   ```
4. **Run**:
   ```bash
   docker compose up -d
   docker logs -f dnd-recap-bot
   ```

## Workflow

1. **First time on a channel**: `/initialize` scans the channel's existing journal records and builds the baseline roster + scratchpad.
2. **Every session**: `/recap <twitch_url>` downloads/transcribes/summarizes, posts the journal, and writes a new snapshot of roster + scratchpad into that recap's folder.
3. **Re-recap a VOD**: just `/recap <same url>` again — same folder is reused, audio is cached, only the LLM steps re-run. Use `force:true` to wipe the cache and redo from scratch.
4. **Fix something**: edit `roster.md` / `scratchpad.md` / `journal.md` directly via `/roster action:edit file:...` (and friends) or by editing the file on disk.

## Commands

### Channel commands

| Command | Permission | Description |
|---|---|---|
| `/initialize` | Manage Channels | Build roster + scratchpad from channel history (one-shot, ~$0.20). DM live progress. Confirmation buttons if context exists. |
| `/recap <twitch_url> [style] [force]` | Anyone | Run a recap. `style` overrides the default per-recap; `force:true` wipes cached audio/chunks for this VOD. |
| `/recap_edit <vod_id> <file>` | Manage Channels | Replace a specific recap's `journal.md` with the uploaded file. |
| `/roster [action] [file] [vod_id]` | edit/delete: Manage Channels | `show` (default) / `delete` / `edit`. With `action:edit` and a `file:`, replaces the latest (or specified) roster.md. |
| `/scratchpad [action] [file] [vod_id]` (alias `/pad`) | edit/delete: Manage Channels | Same pattern as `/roster`. |

Each `show` and channel recap post carries a `✏️ Edit (upload .md)` button that prints the exact slash command you need.

### DM-only commands

| Command | Description |
|---|---|
| `/jobs` | List every active recap/init job with per-job **Cancel** button. |
| `/check` | Sanity-check setup: Discord login, guild visibility, data dir, ffmpeg/yt-dlp on PATH, one live Gemini call. |

## Styles

- **chapters** (default): one `## Chapter Title` per major event, with bullets
- **bullets**: nested bullets by scene
- **narrative**: prose, past tense, third-person
- **structured**: fixed sections (Combat / Roleplay / Exploration / Loot / Cliffhangers)
- **terse**: 10–20 fact-only bullets

## Storage

The bot is file-backed — no database. Per-channel data lives under `data/channels/{channel_id}/`:

```
data/channels/{channel_id}/
├── meta.yaml                  # name, premise, style, guild_id, journals_synced
├── journals_cache/            # cached fetches from Discord history (read-side)
├── initialize/
│   ├── roster.md              # baseline from /initialize
│   └── scratchpad.md
└── recaps/
    ├── 0001_<vod_id>/         # first /recap run
    │   ├── source.mp4         # downloaded VOD (~250 MB for a 4-hour stream)
    │   ├── audio.mp3          # converted mono 16 kHz
    │   ├── chunks/chunk_*.mp3
    │   ├── transcript.txt
    │   ├── journal.md
    │   ├── roster.md          # roster snapshot AFTER this recap
    │   └── scratchpad.md
    └── 0002_<vod_id>/...
```

The next `/recap` reads `roster.md` + `scratchpad.md` from the most recent recap folder (or `initialize/` if there are no recaps yet). Re-recapping a VOD reuses the same folder; re-recapping an earlier one reads from the recap immediately before it in chronology.

Journals are also posted to the Discord channel — that's the user-visible artifact. The bot scans channel history during `/initialize` to learn from past sessions.

## Per-step model configuration

`models.yaml` assigns a Gemini model to each pipeline step. Defaults:

```yaml
models:
  roster_build: gemini-3.1-pro-preview      # full-history scan, strong model
  scratchpad_build: gemini-3.1-pro-preview  # same
  transcribe: gemini-3.1-flash-lite         # cheap, 20× parallel
  summarize: gemini-3.1-pro-preview         # session-to-journal
  update_roster: gemini-3-flash-preview     # incremental, per-recap
  update_scratchpad: gemini-3-flash-preview
```

## Deployment

### Hetzner Cloud

1. Create a CX22 server (or larger if you expect many recaps — see Disk usage below). Paste `deploy/hetzner-cloud-init.yaml` as user-data, with `<owner>` replaced by your repo path.
2. SSH in, edit `/opt/dnd-recap-bot/.env`.
3. `cd /opt/dnd-recap-bot && docker compose up -d`.

### Updates

```bash
cd /opt/dnd-recap-bot
git pull
docker compose up -d --build
```

State in `./data/` persists across rebuilds.

### Disk usage

Each `/recap` keeps `source.mp4` + `audio.mp3` + 20 chunks in its recap folder — roughly **250–400 MB per recap**. Plan for ~25–40 GB after 100 recaps; either prune older recap folders or upgrade past CX22's 40 GB.

The text artifacts (`roster.md`, `scratchpad.md`, `journal.md`, `transcript.txt`) are ~1–2 MB per recap — backing those up is cheap:

```bash
tar -czf backup.tar.gz \
  data/channels/*/meta.yaml \
  data/channels/*/initialize \
  data/channels/*/recaps/*/{roster,scratchpad,journal}.md \
  data/channels/*/recaps/*/transcript.txt
```

## Cost

Initialization on a 100-record channel: ~$0.20 (two Pro calls with full journal context). Each recap: roughly $0.05–0.15 depending on session length (transcription on Flash-Lite + summary on Pro). Verify current Gemini pricing.

## Troubleshooting

- **Commands not appearing in DM**: global commands (`/jobs`, `/check`) take up to an hour to propagate on first registration. Force-refresh your Discord client (Ctrl+R) once that hour has passed.
- **Commands not appearing in channel**: set `DISCORD_GUILD_ID` for instant guild-scoped sync.
- **`/recap` says "channel hasn't been initialized"**: run `/initialize` first, or it means you have journal entries in the channel that haven't been incorporated yet.
- **Slow download / "stuck at 1%"**: check `docker logs` for the actual progress %. Twitch HLS audio downloads run with 8 parallel fragments; a 4-hour VOD typically completes in ~2-5 min.

## License

MIT

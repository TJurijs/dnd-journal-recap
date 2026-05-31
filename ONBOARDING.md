# Knowledge Transfer — D&D Discord Recap Bot

Handoff doc for the next agent/engineer taking over this project. The `README.md`
covers the user-facing pitch + quick start; **this file is the operational +
architectural brain dump** — how it really works, how to run/deploy/debug it,
the non-obvious decisions, and what's left to do.

> Status as of last session: **live and healthy** in 2 Discord servers. All CI
> green (118 tests). Last commit `2aa5440` — richer `/admin log`.

---

## 1. What it is (in one paragraph)

A self-hosted Discord bot. A user runs `/recap <twitch-or-youtube-url>` in a
campaign channel; the bot downloads the VOD audio, splits it into 20 chunks,
transcribes them in parallel with Gemini (which also diarizes + attributes
speakers/characters), summarizes the transcript into a Markdown "session
journal" (using the campaign's accumulated **roster** + **scratchpad** as
context), updates that roster/scratchpad, and posts the journal as a Discord
**embed**. It DMs the requester live progress the whole time. `/initialize`
bootstraps the roster/scratchpad from a channel's existing journal history.

---

## 2. Infrastructure & access

| thing | value |
|---|---|
| GitHub repo | `github.com/TJurijs/dnd-journal-recap` |
| Server | Hetzner VPS (IP is in your local `~/.ssh/config` under the `dnd-recap-bot` alias, and in the Hetzner console — kept out of this public repo) |
| Server specs | 2 vCPU (AMD EPYC-Rome), **3.7 GB RAM, no swap**, 38 GB disk, **no GPU** |
| SSH | local alias **`dnd-recap-bot`** (in `~/.ssh/config`) → `ssh dnd-recap-bot` |
| App dir on server | `/opt/dnd-recap-bot` (git checkout + docker compose) |
| Data dir | `/opt/dnd-recap-bot/data` → bind-mounted to `/data` in the container |
| Container name | `dnd-recap-bot` |
| Discord servers | Yoshkeen's server `1505296799982682262` · The World of Gaian `698300323143352400` |
| Bot app owner | `yoshkeen` (id `111339999378567168`) — the **only** `/admin` user (`bot.is_owner`) |

The Hetzner box `deploy/hetzner-cloud-init.yaml` + `deploy/install.sh` document
the original provisioning. There is **no other server** (an old `h-claw-root`
box was deleted long ago).

---

## 3. The operational loop (deploy / test / debug)

**Deploy = push to `main`.** GitHub Actions (`.github/workflows/deploy.yml`)
runs `test` (pytest) then `deploy` (SSH via `appleboy/ssh-action` into Hetzner:
`git reset --hard origin/main` + `docker compose up -d --build`). Secrets:
`HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_KEY`.

```bash
# typical cycle
git add -A && git commit -m "..." && git push          # triggers CI
gh run list --limit 2                                   # watch Test + Deploy
ssh dnd-recap-bot 'docker logs --tail 20 dnd-recap-bot' # confirm healthy sync
```

A healthy boot logs: `Logged in as D&D Recap Bot#5177` → `Member of 2 guild(s)`
→ `Synced 3 global (DM-only) command(s); per-guild channel commands: ...`.

**Tests:** `pytest tests/ -q` (CI uses dummy `DISCORD_BOT_TOKEN`/`GEMINI_API_KEY`).
⚠️ **Local pytest on the original dev machine doesn't work** (`python3` there
lacks `pytest`/`google`/`yaml`). Rely on CI, or validate pure logic by inlining
it into a throwaway `python3` script (the pattern used all session). Always
`python3 -c "import ast; ast.parse(open('file.py').read())"` before pushing.

**Config changes on the server** (`.env`, `models.yaml`, `prices.yaml`) are
applied with `/admin restart` (or `docker compose up -d --force-recreate`).
A plain `docker compose restart` does **not** reload `.env`.

### ⚠️ Gotchas that have bitten us
- **`docker compose up -d --build` (recreate) WIPES `docker logs`.** Step-logs
  and prior-run history are lost on every deploy. The JSONL usage log
  (`data/usage_log.jsonl`) survives because it's on the bind mount — that's the
  durable audit trail, not docker logs.
- **3.7 GB RAM, no swap.** Be careful adding memory-heavy work (e.g. local
  Whisper) — it can OOM-kill the bot mid-recap.
- **Single job queue worker** — see §6. No parallel recaps.

---

## 4. Architecture & data flow

### The recap pipeline (`pipeline/orchestrator.py::run_job`)
`download VOD (yt-dlp) → extract mp3 (ffmpeg, mono 16k) → delete source →
split into 20 chunks → transcribe 20 chunks in parallel (Gemini) → summarize
into journal (Gemini + roster/scratchpad) → update roster + scratchpad
(parallel) → deliver (embed)`. Live progress is streamed to the requester's DM
via an edited status message; each step is tracked in `_step_ui`.

### Storage model — **keyed by Discord CATEGORY, not channel**
This is the single most important design fact. A Discord *category* (the
collapsible group of channels) = one campaign = one roster + scratchpad, shared
by all channels in it. Storage lives at
`data/categories/<category_id>/`:
- `roster.md`, `scratchpad.md` — category-wide, accumulated across all recaps
- `meta.yaml` — guild_id, style, journals_synced count
- `recaps/<NNNN>_<vodid>/` — per-recap folder: `audio.mp3`, `chunks/`,
  `transcript.txt`, `journal.md` (the journal is **kept on disk** as the
  durable source of truth)

`storage/files.py` owns all disk paths (`_category_root`, `make_or_reuse_recap_dir`,
roster/scratchpad/journal read/write, journal cache).

### Journals live in the Discord channel too
`storage/discord_journals.py` treats every message in a journal channel as a
journal entry. **Recaps are now posted as embeds with NO `.md` attachment**
(journal.md on disk is the source of truth). Re-ingestion (`list_for_channel` +
`fetch_content`, used by `/initialize` and the `/recap` sync-count check) reads
the journal back **out of the embed** (`_extract_embed_body` reconstructs
`# title\n\n{body}` from the content header + embed description, stripping a
` ```md ` fence if present). Old posts with `.md` attachments still re-ingest
via the attachment path.

### Models, profiles, cost
- `models.yaml` — per-action model assignment grouped into **profiles**:
  - `default` (cheaper): Flash everywhere, `gemini-2.5-flash-lite` for transcribe
  - `high` (best): `gemini-3.1-pro-preview` for summarize + full-history builds,
    `gemini-3.1-flash-lite` for transcribe
  - `/recap` + `/initialize` take an optional `profile:` arg (A/B testing);
    default is `default`. A profile only lists keys it overrides → falls back to
    `default` → `GEMINI_MODEL` env.
- `prices.yaml` — per-model USD/1M-token rates (Pro is **tiered** at >200k input
  tokens). `pipeline/cost.py` prices each call at its own model's rate.
  **Keep every model in `models.yaml` listed in `prices.yaml`** or cost
  tracking silently falls back to a default rate.
- `config.py::ModelConfig.get(action, profile)` resolves the model for a step.

### Cost tracking — the list rule
`transcribe_chunk` can fire **multiple API calls per chunk** across **different
models** (primary → retry-on-`high` → sub-chunk rescue). It returns a
**`list[UsageInfo]`** (one per call, each tagged with its model). `CostTracker.add`
accepts a list and prices each entry separately. **Never sum UsageInfos across
models** via `+` before billing — `UsageInfo.__add__` keeps only the first
model's tag and under-counts (this was a real bug; see `cost.py` docstring +
the regression test).

---

## 5. Key files (where to look)

| file | responsibility |
|---|---|
| `bot.py` | bot construction, on_ready sync, imports all command modules (registers them) |
| `commands/recap.py` | `/recap` — detect source, resolve category, perm preflight, claim, enqueue |
| `commands/initialize.py` | `/initialize` — rebuild roster/scratchpad from channel journal history |
| `commands/admin.py` | `/admin` group (DM-only, owner-only): `settings`, `restart`, `log` |
| `commands/roster.py`, `scratchpad.py` | show/edit/delete roster + scratchpad (still editable) |
| `commands/_helpers.py` | `resolve_category`, permission preflight (`bot_missing_channel_perms`) |
| `commands/_edit_button.py` | persistent "✏️ Edit" button (roster/scratchpad only now) |
| `pipeline/orchestrator.py` | `run_job` — the whole recap pipeline + DM status rendering |
| `pipeline/transcribe.py` | per-chunk transcription + **all recovery logic** (see §7) |
| `pipeline/summarize.py` | journal generation + **4000-char cap** (retry + trim) |
| `pipeline/chunk_audio.py` | ffmpeg split → `(paths, chunk_duration_sec)` |
| `pipeline/llm.py` | shared Gemini call w/ retry on transient 429/5xx |
| `pipeline/cost.py` | `UsageInfo`, `CostTracker`, `PriceTable` |
| `pipeline/state.py` | in-memory `ActiveJob` registry, keyed by category_id |
| `queue.py` | the single-worker FIFO job queue |
| `storage/discord_journals.py` | post/DM journals as embeds + re-ingestion |
| `storage/files.py` | all on-disk paths |
| `storage/usage.py` | append-only JSONL usage log (`data/usage_log.jsonl`) |
| `prompts/transcribe.py` | the (static) transcription prompt |
| `prompts/summarize.py` | journal prompt builder (incl. the length-cap instruction) |

---

## 6. Concurrency — IMPORTANT

**There is no parallel recap execution.** `queue.py` has a **single worker**
that pulls one `category_id` and `await run_job(...)` to completion before the
next. Two layers:
1. **Per-category claim** (`state.claim`) — refuses a 2nd recap for a category
   that already has one ("already an active recap job for this category").
2. **Single queue worker** — even different categories/servers run **back-to-back**,
   FIFO, not concurrently.

Within one recap the 20 chunks DO transcribe in parallel (`asyncio.gather` +
`Semaphore(20)`). Intentional: protects the 2-vCPU box + Gemini rate limits.
To allow N parallel recaps you'd spawn N workers in `JobQueue.start()` with a
concurrency cap — but mind RAM/CPU/rate limits first (probably N=2 max on this
box, or upgrade to a CX32).

---

## 7. Transcription robustness (the big saga — read before touching transcribe)

A specific Splitlanders VOD (`2783329200`, Twitch) exposed three failure modes.
The current `transcribe_chunk` handles all three and returns
`(transcript, list[UsageInfo], failure_reason, recovery_action)`:

1. **`MAX_TOKENS` runaway** — `gemini-2.5-flash-lite` sometimes loops and emits
   garbage to the output cap. We cap output at **12000 tokens** and run a
   sliding-window **repetition heuristic** (`_looks_repetitive`, strips
   timestamps first). If repetitive → real runaway. If not → it's just
   legit-long content; **keep the truncated text** (`recovery_action="truncated_kept"`).
2. **Repetitive/empty** → **retry that chunk on the `high` model**
   (`gemini-3.1-flash-lite`, empirically far more stable). `recovery_action="retry_high"`.
   Skipped if already on `high`. Safety failures are NOT retried (same gate).
3. **`PROHIBITED_CONTENT` (safety block)** — Gemini's **input-side** content gate
   (lives on `prompt_feedback.block_reason`, not `finish_reason`; shared across
   the whole flash-lite family, so a model swap does NOT help). The classifier
   scores the **whole ~9-min chunk**. Recovery: **split the blocked chunk into 8
   sub-chunks** and re-transcribe each — smaller windows score below threshold.
   Empirically **8/8 recovered** on the known-blocked chunk_005.
   `recovery_action="subchunk_rescue:N/M"`. Recursion guard
   (`_allow_safety_rescue=False`) prevents infinite splitting.

Failures + recoveries are surfaced in the DM finish status with **VOD
timestamp ranges** ("⚠️ Transcription gaps" / "✨ Recovered"). The orchestrator
computes timestamps from `chunk_duration_sec`.

**Candidate improvement discussed but not built:** route safety-blocked chunks
to a non-Gemini transcriber (Groq `whisper-large-v3-turbo` ~$0.04/hr, or
Deepgram which also gives diarization) — no content gate at all. See §9.

---

## 8. Notable behaviors / decisions

- **`/recap silent:true`** → journal DM'd to the requester as a ` ```md ` **code-block
  embed** (copy-paste preserves Markdown) instead of posting in the channel.
  Real "ephemeral" is impossible — the interaction token expires (~15 min) long
  before a 10–30 min recap finishes; hence DM.
- **4000-char journal cap** — `summarize.py` instructs <4000 chars, retries once
  if over, then trims at a `## ` section boundary. Keeps journals in one embed
  (Discord embed description limit 4096). This is a real content reduction for
  long sessions (journals used to run 3–5.4k chars). `transcript.txt` keeps the
  full transcript on disk.
- **`/admin log`** — rendered as an **embed** (4096 desc, masked `[title](url)`
  links render clickable). Each recap row: date · server · channel · **/cmd** ·
  **user name** · profile · cost, plus a `↳ 📺 [video name](link)` line.
  `source_url` + `vod_title` are logged for new recaps; old entries reconstruct
  the link from `vod_id` (numeric→Twitch, else→YouTube).
- **User name in the log** is captured at *claim* time
  (`ActiveJob.requested_by_name = interaction.user.display_name`) because the
  job logs from the orchestrator `finally` block where `bot.get_user()` usually
  misses the cache (that's why old rows show raw IDs).
- **`/recap_edit` was removed** — recaps are embed-only now (no attachment to
  swap), so journals aren't editable in place. Only roster + scratchpad are.
- **Empty channel is fine** — `/recap` works without `/initialize`; the LLM
  builds the first roster/scratchpad from that recap.
- **YouTube + Twitch** both supported (6h cap). `pipeline/download.py`.
- **Module-shadowing footgun (fixed, watch for it):** orchestrator imports the
  usage module as `usage_log` because a local `usage` UsageInfo var shadowed it
  and silently dropped recap log events.

---

## 9. Known limitations & candidate future work

- **No parallel recaps** (§6). Fine for occasional use; the 2nd requester just
  waits. Add N workers if contention appears.
- **Cheaper/more-robust transcription** (discussed, not built): cost is already
  trivial (~$0.10–0.12 transcribe / ~$0.20–0.30 full recap), so the win isn't
  money — it's **escaping Gemini's content blocks/runaways** and/or
  self-hosting. Local Whisper on the **current box is not recommended** (2 vCPU/
  3.7 GB/no-GPU → only `small`/`base`, slow, no diarization, OOM risk). Best
  options if revisited: **Groq whisper-large-v3-turbo** (cheap, fast, no gate,
  no diarization) or **Deepgram** (cheap-ish, built-in diarization). Highest-
  value low-risk move: wire one of these as the **safety-rescue fallback** so a
  PROHIBITED_CONTENT chunk goes to Whisper instead of (or in addition to)
  sub-chunking.
- **README is somewhat stale** — still says "Twitch only", "per-channel",
  ".md attachment". Reflects an earlier design. Update it if you touch user docs.
- **`/admin log` old entries** lack the real video *title* (only logged going
  forward) — they show the VOD id as link text.

---

## 10. How to talk to the user (yoshkeen)

- Prefers **free-form prose answers**, not `AskUserQuestion` popups (dismissed
  them repeatedly). Ask clarifying questions inline.
- Likes to **see things before they ship** — DM-ing live previews of Discord
  formatting (embeds, etc.) to yoshkeen before committing has worked well.
- Values **understanding the "why"**, not just patches — explain mechanisms.
- Wants **accurate cost accounting** and clear status/observability.

---

## 11. First commands to run when you take over

```bash
ssh dnd-recap-bot 'docker logs --tail 30 dnd-recap-bot'   # is it healthy?
ssh dnd-recap-bot 'tail -5 /opt/dnd-recap-bot/data/usage_log.jsonl'  # recent activity/costs
gh run list --limit 5                                      # recent CI
git log --oneline -15                                      # recent work
```

In Discord (as yoshkeen, in DMs with the bot): `/admin settings` (full config
dump), `/admin log` (usage), `/admin restart` (reload server-edited config).

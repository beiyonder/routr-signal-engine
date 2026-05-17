# signal-engine — Architecture

> The *how*. The *what* (rationale, anchor topics, voice rules) lives in
> private notes outside this repo.

## Goal

Every morning at **07:00 UTC** a GitHub Actions workflow:

1. Pulls fresh items from a handful of high-signal sources.
2. Sends candidate items to a small classifier LLM for relevance scoring + lead extraction.
3. Drafts a few post hooks (X thread, LinkedIn, Reddit, HN comment, Dev.to title).
4. Emits one digest to **Discord** primarily (Slack and email are optional).
5. Appends qualified leads to `data/leads/queue.jsonl` (gitignored; manual enrichment offline).

Everything runs on the GitHub Actions free tier (public repo = unlimited minutes).
LLM cost target: **<$10/month**.

## Source map

| Source           | Status | Endpoint                                                                 | Auth                  | Config                          |
|------------------|--------|--------------------------------------------------------------------------|-----------------------|---------------------------------|
| Hacker News      | live   | `hn.algolia.com/api/v1/search_by_date`                                   | none                  | `config/hn.yaml`                |
| Reddit           | live   | `old.reddit.com/r/<sub>/new.rss` + Android-OAuth fallback                | anon installed-client | `config/subreddits.yaml`        |
| GitHub issues    | live   | `api.github.com/repos/<owner>/<repo>/issues`                             | `GITHUB_TOKEN`        | `config/github_repos.yaml`      |
| X / Twitter      | live   | Playwright + imported cookies                                            | session cookie jar    | `config/twitter_watch.yaml`     |
| Discord (manual) | live   | parse `data/manual/discord-pastes/*.md`                                  | none                  | n/a (paste-in)                  |
| HF Papers        | live   | `huggingface.co/api/daily_papers`                                        | none                  | `config/hf_papers.yaml`         |
| Newsletters      | live   | RSS bundle: Latent Space, The Batch, Import AI, Simon Willison, ...      | none                  | `config/newsletters.yaml`       |
| Dev.to           | future | `dev.to/feed/tag/<tag>`                                                  | none                  |                                 |

Competitor repos initially monitored: `BerriAI/litellm`, `Portkey-AI/gateway`, `Helicone/helicone`, `maximhq/bifrost`. Edit `config/github_repos.yaml` to extend.

## Daily flow

```
07:00 UTC  daily-signals.yml
  │
  ├─ fetch (sequential, ~90s)
  │    sources/hn.py, reddit.py, github_issues.py, twitter.py,
  │    discord_paste.py, hf_papers.py, newsletters.py
  │    each upserts RawItems into signals table; dedupe by primary key
  │
  ├─ cosine prefilter (Gemini embeddings, ~5s)
  │    lib/cosine.py + lib/embeddings.py
  │    drops obviously off-topic items before paying classifier tokens
  │
  ├─ classify (Claude Haiku, chunks of 15, ~30s total)
  │    classify/pain_signal.py → topics, score, pain_summary, engagement_angle
  │
  ├─ draft (Gemini 3 Pro, ~10s)
  │    classify/post_drafter.py → 5 post hooks
  │    classify/voice_lint.py → soft warnings on em-dash/emoji/banned phrases
  │
  ├─ persist
  │    output/markdown_digest.py → data/digests/YYYY-MM-DD.md (gitignored)
  │    update `signals` rows: rank_in_run, action_label='queued'
  │    close `runs` row with status, counts, digest_md, hooks_json
  │
  └─ distribute
       output/discord.py → POST $DISCORD_WEBHOOK_URL?wait=true (native embeds)
       captures message IDs back; records on runs.discord_message_ids
       pre-creates one `posts` row per auto-dispatchable hook (status='pending')
       output/slack.py + output/email.py (optional, off by default in CI)
```

## Distribution flow (added 2026-05-17)

After the daily digest lands in Discord, two cron-driven workers handle
out-of-band publishing without any new infrastructure:

```
[user reacts ✅ on a digest message]

dispatch-approved.yml   (every 15 min, on :15/:30/:45)
  │
  ├─ poll Discord REST API for reactions on recent runs' message IDs
  │    lib/discord_inbox.py — bot token, no gateway connection
  │
  ├─ for each `pending` post matching an approved run + emoji:
  │    daily / ✅       → buffer_client.create_post(channel=BUFFER_X_CHANNEL_ID)
  │    synthesis / 📰   → beehiiv_client.create_draft_post(...)
  │
  └─ promote post: pending → posted (with buffer/beehiiv id + URL)
     bot reacts 🚀 on the message so the next poll skips it (or ❌ on failure)
```

```
weekly-synthesis.yml    (Sundays 14:00 UTC)
  │
  ├─ aggregate last 7 days of signals; pick top 10 by combined_score
  ├─ classify/synthesize.py → 400-500 word essay via flagship drafter model
  ├─ post to Discord as a "synthesis draft" message
  ├─ pre-create a `pending` Beehiiv post
  └─ user reacts 📰 to push the draft to Beehiiv (sends as draft;
     user reviews and clicks Send in Beehiiv UI when ready)
```

LinkedIn is **intentionally not auto-wired**: the user posts to LinkedIn
manually from the digest text. The Buffer channel set is X-only.

## Failure modes

| Failure                         | Behavior                                                                                |
|---------------------------------|-----------------------------------------------------------------------------------------|
| Source returns 0 items          | Source module logs warning, returns empty list. Digest builder continues.               |
| Source 403/429 (Reddit IP-ban)  | Module retries 3x with exponential backoff. Final failure → warning, empty payload.     |
| Claude API down                 | Items fall through with `[UNCLASSIFIED]` tag. Digest still ships with raw candidates.   |
| Slack/Discord/email webhook 5xx | Logged. Other outputs still attempt. Markdown digest in repo is always source of truth. |
| GitHub commit-back fails        | Workflow fails; next day's run picks up where we left off (dedupe handles overlap).     |

## Dedupe strategy

Dedupe lives in the SQLite `signals` table — the existence of a row IS the
dedupe state (primary key on `id`, a globally-unique string of the form
`<source>-<source_id>`).

Each source module:
1. Opens `SeenStore(<source>)`, which pre-loads the known IDs for that source.
2. For each fetched item, checks `seen.has(item.id)`; skips if known.
3. `seen.add_item(raw_item)` does `INSERT OR IGNORE` into `signals` and adds
   the id to the in-memory set.

The SQLite file at `data/intel.db` is gitignored. State travels between CI
runs via the Actions cache (10 GB, 7-day idle eviction). The `data/digests/`,
`data/raw/`, `data/leads/` outputs are uploaded as 90-day artifacts so old
runs are recoverable via `gh run download`.

## Local development

```pwsh
# Install with uv (recommended) or pip
uv sync

# Copy env template and fill in
cp .env.example .env

# Dry run (no Slack/Discord/email POSTs, no git commit)
$env:ROUTR_SIGNAL_COMMIT="0"; $env:ROUTR_SIGNAL_PUBLISH="0"; uv run routr-signal

# Run a single source for debugging
uv run python -m routr_signal.sources.hn

# Full run (writes digest, but skips publish)
$env:ROUTR_SIGNAL_PUBLISH="0"; uv run routr-signal
```

## Roadmap

| Phase   | Status   | What                                                                          |
|---------|----------|-------------------------------------------------------------------------------|
| 0       | done     | Scaffold, configs, workflow skeleton                                          |
| 1       | done     | HN + Reddit + GitHub sources, Claude classifier, post drafter, Discord output |
| 1b      | done     | SQLite persistence + Actions cache state + 90-day artifacts                   |
| 1c      | done     | Cosine prefilter (Gemini embeddings) between keyword and LLM                  |
| 3a      | done     | Source expansion (12 subreddits, 21 HN queries, X via Playwright cookies)     |
| 3b      | done     | HF Papers + Newsletters RSS sources                                           |
| D1      | done     | Distribution: dispatch-approved cron, Buffer X posting, Discord reaction poll |
| D2      | done     | Weekly synthesis cron (Sundays) + Beehiiv draft publish                       |
| D3      | done     | `posts` table for outgoing-post tracking + status lifecycle                   |
| 2       | future   | `people` + `signal_people` tables + weekly person snapshots                   |
| 4-7     | deferred | Cloudflare D1 + Pages dashboard + Access auth (see `40-distribution-stack`)   |
| 8       | future   | Engagement feedback loop (blocked by X API 402; revisit via Buffer analytics) |

## Auxiliary entry points

| Console script      | Trigger                            | What                                            |
|---------------------|------------------------------------|-------------------------------------------------|
| `routr-signal`      | `daily-signals.yml` (07:00 UTC)    | Daily fetch → classify → publish to Discord     |
| `routr-synthesize`  | `weekly-synthesis.yml` (Sun 14:00) | Aggregate week → draft essay → post to Discord  |
| `routr-dispatch`    | `dispatch-approved.yml` (every 15) | Poll reactions → post via Buffer / Beehiiv      |

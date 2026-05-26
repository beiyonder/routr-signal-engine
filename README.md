# signal-engine

A personal daily signal aggregator. Fetches discussions from Hacker News, Reddit, public GitHub issues, X/Twitter, and manually-pasted Discord threads. A small classifier LLM scores each item for relevance, a larger drafter LLM writes a few post hooks, and the digest lands in a Discord channel every morning at **07:00 UTC**.

Two-layer relevance: a deterministic cosine-similarity prefilter against curated topic anchors drops ~50% of items before any LLM call, then the classifier handles the rest. Items are attributed to authors and aggregated across sources over time.

Cost target: **<$10/month** (LLM API only; everything else is free tier).

## Quick start

```pwsh
# 1. Set up venv + deps
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m playwright install chromium  # for the X fallback

# 2. Configure
Copy-Item .env.example .env
# Fill in (at minimum):
#   ROUTR_SIGNAL_LLM_PROVIDER  (anthropic | gemini | openai)
#   the matching API key
#   GEMINI_API_KEY  (required for the cosine embedding layer)
#   DISCORD_WEBHOOK_URL  (native Discord webhook, no /slack suffix)

# 3. One-time X login (optional, only if twitter is in ROUTR_SIGNAL_SOURCES)
.\.venv\Scripts\python.exe tools/twitter_login.py
# Opens a real browser. Log in to your burner account. Cookies are written
# to data/cache/twitter-cookies.json (gitignored).

# 4. Dry run — fetches, classifies, writes markdown, DOES NOT publish or commit
$env:ROUTR_SIGNAL_COMMIT="0"; $env:ROUTR_SIGNAL_PUBLISH="0"
.\.venv\Scripts\python.exe -m routr_signal.main

# 5. Full run — writes everywhere
.\.venv\Scripts\python.exe -m routr_signal.main
```

```bash
# Linux / macOS / GitHub Actions
python3.12 -m venv .venv
./.venv/bin/python -m pip install -e .
cp .env.example .env
# edit .env, then:
./.venv/bin/python -m routr_signal.main
```

## Switching LLM providers

```pwsh
# Anthropic Claude Haiku 4.5 (default)
$env:ROUTR_SIGNAL_LLM_PROVIDER="anthropic"
$env:ROUTR_SIGNAL_LLM_MODEL="claude-haiku-4-5"

# Gemini 3 Flash Preview
$env:ROUTR_SIGNAL_LLM_PROVIDER="gemini"
$env:ROUTR_SIGNAL_LLM_MODEL="gemini-3-flash-preview"

# OpenAI (override model if your org doesn't have access to gpt-5-mini)
$env:ROUTR_SIGNAL_LLM_PROVIDER="openai"
$env:ROUTR_SIGNAL_LLM_MODEL="gpt-5-mini"
```

The system prompts emit strict JSON, so switching providers should not require prompt changes. Output quality on classification tasks is roughly Haiku 4.5 ≈ Gemini 3 Flash > GPT-5-mini in our tests; Gemini is fastest, Haiku has the best instruction-following on the negative class ("this is NOT a pain signal").

## What gets emitted

Each run produces:

- `data/intel.db` — SQLite with the canonical `signals`, `runs`, `posts` tables
  (gitignored locally; travels between CI runs via Actions cache).
- `data/digests/YYYY-MM-DD.md` — human-readable digest snapshot (gitignored;
  also archived via Actions artifacts, 90-day retention).
- Discord post to `$DISCORD_WEBHOOK_URL` (native embed format, NOT `/slack`).
  The webhook is hit with `?wait=true` so we get the message IDs back; those
  IDs live on the `runs.discord_message_ids` column so the dispatch worker
  can poll them for approval reactions.
- One `posts` row per auto-dispatchable hook (today: `x_thread`), `status='pending'`.
- Optional: Slack post to `$SLACK_WEBHOOK_URL` and email to `$EMAIL_TO` (off by
  default in CI).

## Daily digest structure

```
# Routr Daily Signal Digest — 2026-05-13
**Source counts:** `hn`: 19, `reddit`: 12, `github`: 4

## 🔴 Top pain signals
1. **[HN]** `cold_start` (score 0.84) by `@devname` — Ask HN: handling LLM provider failover
   - Pain: "LiteLLM cold-starts kill our P99 on Lambda — what's the playbook?"
   - Angle: post a concrete TS edge gateway sequence with timings; no product mention
   - Link: https://news.ycombinator.com/item?id=...
2. ...

## 📈 Active accounts (engage early)
- **github** `@username` — https://github.com/username
  - Pain: 500ms LiteLLM cold-start
  - Angle: lead with their stated symptom, no pitch

## ✍️ Pre-drafted post hooks
### X thread opener
> "We measured the actual hidden token markups across OpenRouter, Anthropic, and OpenAI ..."

### LinkedIn opener
> "Why your multi-LLM setup will break in production — and it's not the providers' fault."

### Reddit post title
> "Benchmarked TypeScript vs Python LLM-proxy overhead across 50k requests — here's the cold-start delta"

### HN comment seed
> "FWIW on cold-starts: we ran 50k requests through a TS/Hono gateway and the median was ..."

### Dev.to title
> "Stop paying the 5% LLM tax — a zero-markup, edge-deployed gateway in 200 lines of TypeScript"
```

## Secrets — GitHub repo → Settings → Secrets and variables → Actions

LLM provider/model are best stored as **repo Variables** (visible in run logs, easier to flip). API keys live as **Secrets**.

| Type    | Name                          | Required when              | Purpose                                                |
|---------|-------------------------------|----------------------------|--------------------------------------------------------|
| Variable| `ROUTR_SIGNAL_LLM_PROVIDER`   | always                     | `anthropic` (default), `gemini`, or `openai`           |
| Variable| `ROUTR_SIGNAL_LLM_MODEL`      | optional                   | Override default model for the provider                |
| Secret  | `ANTHROPIC_API_KEY`           | if provider = anthropic    | Claude Haiku 4.5                                       |
| Secret  | `GEMINI_API_KEY`              | if provider = gemini       | Gemini 3 Flash Preview                                 |
| Secret  | `OPENAI_API_KEY`              | if provider = openai       | OpenAI                                                 |
| Secret  | `DISCORD_WEBHOOK_URL`         | want Discord output (primary) | Native Discord webhook URL (no `/slack` suffix)     |
| Secret  | `SLACK_WEBHOOK_URL`           | want Slack output          | Daily digest push                                      |
| Secret  | `EMAIL_SMTP_HOST`             | want email output          | SMTP host                                              |
| Secret  | `EMAIL_SMTP_PORT`             | want email output          | `587` (STARTTLS) or `465` (SSL)                        |
| Secret  | `EMAIL_SMTP_USER`             | want email output          | SMTP login                                             |
| Secret  | `EMAIL_SMTP_PASS`             | want email output          | SMTP password / app-password                           |
| Secret  | `EMAIL_FROM`                  | want email output          | sender address                                         |
| Secret  | `EMAIL_TO`                    | want email output          | comma-separated recipient list                         |
| Secret  | `FIRECRAWL_API_KEY`           | later (weekly snapshots)   | TokScale + OpenRouter weekly diff                      |
| Secret  | `TWITTER_USERNAME`            | want X source              | Burner X account login                                 |
| Secret  | `TWITTER_EMAIL`               | want X source              | Burner X email (for verification codes)                |
| Secret  | `TWITTER_PASSWORD`            | want X source              | Burner X password                                      |
| Secret  | `TWITTER_TOTP_SECRET`         | optional                   | TOTP secret if 2FA is enabled on the burner            |
| Secret  | `TWITTER_COOKIES_B64`         | want X Playwright fallback | base64 of `data/cache/twitter-cookies.json`            |
| Secret  | `DISCORD_BOT_TOKEN`           | want dispatch worker       | Bot token for reading reactions via REST               |
| Secret  | `DISCORD_APP_ID`              | want dispatch worker       | Discord application id                                 |
| Secret  | `DISCORD_PUBLIC_KEY`          | future interactions URL    | Used for signature verification if we add buttons      |
| Secret  | `DISCORD_CHANNEL_ID`          | want dispatch worker       | Channel id where the digest is posted (REST polling)   |
| Secret  | `BUFFER_ACCESS_TOKEN`         | want X auto-posting        | Buffer GraphQL bearer                                  |
| Secret  | `BUFFER_ORG_ID`               | want X auto-posting        | Buffer organization id                                 |
| Secret  | `BUFFER_X_CHANNEL_ID`         | want X auto-posting        | Buffer channel id for the connected X profile          |
| Secret  | `BEEHIIV_API_KEY`             | want newsletter publish    | Beehiiv v2 API key                                     |
| Secret  | `BEEHIIV_PUBLICATION_ID`      | want newsletter publish    | `pub_<uuid>` for the target publication                |
| Secret  | `X_API_BEARER_TOKEN`          | optional / future          | Reserved for direct X read endpoints (currently 402)   |
| Secret  | `X_API_CONSUMER_KEY`          | optional / future          | Reserved for direct X user-context posting if needed   |
| Secret  | `X_API_CONSUMER_SECRET`       | optional / future          | Reserved for direct X user-context posting if needed   |

`GITHUB_TOKEN` is provided automatically by Actions — we use it to authenticate GitHub API
calls (1,000 req/hr/repo vs 60 unauthenticated).

## Triggering the workflows

Three workflows run on different schedules:

| Workflow                  | Schedule (UTC)                         | Console script      | What                                                   |
|---------------------------|----------------------------------------|---------------------|--------------------------------------------------------|
| `daily-signals.yml`       | 07:00 daily                            | `routr-signal`      | Fetch → classify → publish to Discord                  |
| `weekly-synthesis.yml`    | Sun 14:00                              | `routr-synthesize`  | 7-day aggregate → essay draft → post to Discord        |
| `dispatch-approved.yml`   | every 15 min on :15/:30/:45            | `routr-dispatch`    | Poll Discord reactions → post via Buffer / Beehiiv     |

Each accepts `workflow_dispatch` for manual runs. `daily-signals.yml` also
takes `dry_run` (skip publish) and `sources` (subset override) inputs.

## Distribution flow (cross-channel publishing)

After a daily digest lands in Discord, the dispatch worker (running every 15
min) watches for approval reactions on the digest's messages:

| Emoji | Triggers                                                                       |
|-------|--------------------------------------------------------------------------------|
| ✅    | Post the `x_thread` hook to X via Buffer.                                       |
| 📰    | Push the synthesis draft to Beehiiv as a newsletter draft (review + send in UI).|
| 🚀    | (Bot adds this after a successful dispatch — visible "done" marker.)            |
| ❌    | (Bot adds this on a failed dispatch — visible "needs attention" marker.)        |

LinkedIn is **intentionally not auto-wired**: copy the LinkedIn hook from the
digest and post it yourself.

## Tuning

Every source is configured in YAML under `config/`:

| File                         | Tweak when…                                                  |
|------------------------------|--------------------------------------------------------------|
| `config/keywords.yaml`       | adding/removing pain phrases the prefilter looks for         |
| `config/subreddits.yaml`     | adding/removing subreddits, changing throttle settings       |
| `config/github_repos.yaml`   | adding competitor repos to scan                              |
| `config/hn.yaml`             | tweaking HN tag queries and keyword queries                  |
| `config/prompts/*.md`        | changing Claude's tone, scoring rubric, or output schema     |

## Troubleshooting

### Reddit returns 403 / blocked

This is the most common failure. As of late 2025, Reddit aggressively rate-limits
unauthenticated scrapers and bans many cloud IP ranges, including some GitHub Actions
runners. The pipeline degrades gracefully — Reddit going dark won't kill the digest.

Symptoms:

- `reddit: all hosts failed for ... (last: 403 on reddit.com)`
- `data/raw/reddit/YYYY-MM-DD.json` is `[]` or missing

Mitigations, in order:

1. **Wait it out.** Reddit's blocks sometimes lift in 24-48h. Check whether the GitHub
   Actions run-history shows Reddit recovering on subsequent days.
2. **Edit your User-Agent.** In `config/subreddits.yaml`, change the `user_agent` to
   include a real Reddit username you control (e.g., `(by /u/your-handle)`).
3. **Use the Android OAuth client trick.** A community workaround is to hit
   `oauth.reddit.com` with the public Android OAuth client ID. See
   <https://github.com/redlib-org/redlib> for the recipe. Add as a Phase-5 enhancement
   if Reddit becomes a recurring blocker.
4. **Drop Reddit entirely.** Edit the workflow's `sources` input to exclude `reddit`.
   HN + GitHub issues alone produce a usable digest.

### Claude returns invalid JSON

We retry up to 3 times with a small backoff before falling back to the UNCLASSIFIED path.
If you see frequent JSON failures, edit `config/prompts/pain_signal_classifier.md` to
re-emphasize "Output strict JSON, no prose." Haiku 4.5 occasionally adds a preamble despite
the instruction.

### Slack returns `invalid_blocks`

Block Kit has a 3000-char limit per text block. We truncate to 2900 to leave a margin,
but very long pain summaries may still trip the limit. If you see this, edit
`src/routr_signal/output/slack.py` and lower `SLACK_TEXT_LIMIT`.

### Email fails with `authentication required`

For Gmail, generate an app-password at <https://myaccount.google.com/apppasswords>
(requires 2FA). Use port 587 with STARTTLS (default) or 465 with SSL.

### `ANTHROPIC_API_KEY is not set`

The pipeline still runs — it falls back to surfacing the keyword-filtered raw items in
the digest with a `[UNCLASSIFIED]` tag and a `notes` warning. No publish to Slack/email is
suppressed by this alone; you'll just get a less-curated digest.

### Workflow runs but no commit

If `data/` has no changes (i.e., dedupe says we've seen everything already), the
commit step skips with "No data/ changes to commit." Not a bug.

## What this is NOT

- Not a CRM. Lead data lands in `data/leads/queue.jsonl`. Enrich with Clay weekly,
  manually, when the queue has 20+ leads.
- Not an outbound sender. Drafts are advisory; you decide what to ship and where.
- Not a content scheduler. Use Typefully or Buffer for that — we only generate the angle.
- Not a tracker. We measure nothing about your engagement after the digest lands. That's
  on you to log.

## Sources

| Source           | Module                                    | Auth                                | Throughput        |
|------------------|-------------------------------------------|-------------------------------------|-------------------|
| Hacker News      | `sources/hn.py`                           | none (Algolia public API)           | ~20 items / run   |
| Reddit           | `sources/reddit.py`                       | anonymous Android OAuth fallback    | ~90 items / run   |
| GitHub issues    | `sources/github_issues.py`                | `GITHUB_TOKEN` (optional)           | ~15 items / run   |
| X / Twitter      | `sources/twitter.py`                      | Playwright + imported cookies       | ~80 items / run   |
| Discord paste-in | `sources/discord_paste.py`                | manual — drop `.md` files locally   | as you paste them |
| HF Papers        | `sources/hf_papers.py`                    | none (huggingface.co/api endpoint)  | ~30 items / run   |
| Newsletters      | `sources/newsletters.py`                  | none (RSS bundle)                   | ~50 items / run   |

### Discord paste-in

Discord aggressively bans accounts that automate against their API. Rather than
risk a burner-ban every few weeks, paste interesting Discord threads into
`data/manual/discord-pastes/<date>-<channel>.md` and the pipeline ingests them
on the next run alongside all other signals. See the in-file template comments
in `sources/discord_paste.py` for the expected markdown shape (sender on first
line, message body indented, blank line between messages).

## Architecture

See [ARCHITECTURE.md](./ARCHITECTURE.md) for source map, daily flow, dedupe
strategy, failure modes, and the roadmap.

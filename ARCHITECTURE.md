# routr-signal-engine — Architecture

> Companion to `prompt.md`. `prompt.md` is the *what*; this file is the *how*.

## Goal

Every morning at **07:00 UTC** a GitHub Actions workflow:

1. Pulls fresh items from a handful of high-signal sources.
2. Sends candidate items to Claude Haiku 4.5 for classification + lead extraction.
3. Drafts 5 post hooks (X thread, LinkedIn, Reddit, HN comment, Dev.to title).
4. Emits one digest to **Slack**, **Discord**, **email**, and **the repo** (`data/digests/YYYY-MM-DD.md`).
5. Appends qualified leads to `data/leads/queue.jsonl` (manual Clay enrichment weekly).

Everything runs on the GitHub Actions free tier (public repo = unlimited minutes).
LLM cost target: **<$10/month**.

## Source map

| Source           | Phase | Endpoint                                                                 | Auth                  | Rate budget                 |
|------------------|-------|--------------------------------------------------------------------------|-----------------------|-----------------------------|
| Hacker News      | 1     | `hn.algolia.com/api/v1/search_by_date`                                   | none                  | 10k/hr/IP, 1k hits/query    |
| Reddit           | 2     | `old.reddit.com/r/<sub>/new.rss` + `/search.rss`                         | none, descriptive UA  | self-imposed 1 req / 3s     |
| GitHub issues    | 3     | `api.github.com/repos/<owner>/<repo>/issues`                             | `GITHUB_TOKEN`        | 1,000/hr/repo (Actions)     |
| HF papers        | 5     | `huggingface.co/papers` RSS                                              | none                  | once/day                    |
| Dev.to           | 5     | `dev.to/feed/tag/<tag>` (llmops, openai, anthropic, langchain)           | none                  | once/day                    |
| Newsletters      | 5     | Substack RSS bundle (Latent Space, Pointer.io, Ben's Bites)              | none                  | once/day                    |
| TokScale         | weekly| `tokscale.ai` via Firecrawl                                              | Firecrawl free        | 1 scrape/week               |
| OpenRouter apps  | weekly| `openrouter.ai/apps` via Firecrawl                                       | Firecrawl free        | 1 scrape/week               |
| Awesome lists    | weekly| Git clone tensorchord/awesome-llmops, punkpeye/awesome-mcp-servers, etc. | none                  | git clone                   |
| Twitter / X      | v1.5  | Self-hosted `book000/twitter-rss` publishing to gh-pages                 | burner X account      | hourly cron                 |

Competitor repos initially monitored: `BerriAI/litellm`, `Portkey-AI/gateway`, `Helicone/helicone`, `maximhq/bifrost`. Edit `config/github_repos.yaml` to extend.

## Daily flow

```
07:00 UTC  daily-signals.yml
  │
  ├─ fetch (sequential, ~90s)
  │    sources/hn.py        → data/raw/hn/YYYY-MM-DD.json
  │    sources/reddit.py    → data/raw/reddit/YYYY-MM-DD.json
  │    sources/github_issues.py → data/raw/github_issues/YYYY-MM-DD.json
  │    each updates data/seen/<source>.json (dedupe)
  │
  ├─ classify (one Claude call per source, ~20s total)
  │    classify/pain_signal.py  → list of {item, score, why, suggested_angle}
  │    classify/lead_extractor.py → list of usernames + GitHub URLs
  │
  ├─ draft (one Claude call, ~5s)
  │    classify/post_drafter.py → 5 post hooks
  │
  ├─ compose
  │    output/markdown_digest.py → data/digests/YYYY-MM-DD.md
  │
  ├─ distribute
  │    output/slack.py   → POST $SLACK_WEBHOOK_URL
  │    output/discord.py → POST $DISCORD_WEBHOOK_URL (slack-compat)
  │    output/email.py   → SMTP send
  │
  └─ commit
       git add data/ && git commit -m "digest YYYY-MM-DD" && git push
```

## Failure modes

| Failure                         | Behavior                                                                                |
|---------------------------------|-----------------------------------------------------------------------------------------|
| Source returns 0 items          | Source module logs warning, returns empty list. Digest builder continues.               |
| Source 403/429 (Reddit IP-ban)  | Module retries 3x with exponential backoff. Final failure → warning, empty payload.     |
| Claude API down                 | Items fall through with `[UNCLASSIFIED]` tag. Digest still ships with raw candidates.   |
| Slack/Discord/email webhook 5xx | Logged. Other outputs still attempt. Markdown digest in repo is always source of truth. |
| GitHub commit-back fails        | Workflow fails; next day's run picks up where we left off (dedupe handles overlap).     |

## Dedupe strategy

`data/seen/<source>.json` is a JSON list of item IDs the pipeline has already seen.
Each source module:
1. Loads its `seen` set at start.
2. Filters fresh items to those whose ID is not in `seen`.
3. Adds the new IDs to `seen` and saves.
4. Returns the fresh items to the orchestrator.

Files committed to repo so dedupe state survives between runs.

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

| Phase | Status | What                                                                       |
|-------|--------|----------------------------------------------------------------------------|
| 0     | done   | Scaffold, configs, workflow skeleton                                       |
| 1     | next   | HN source → Claude classify → Slack POST (the smallest end-to-end slice)   |
| 2     | next   | Reddit source                                                              |
| 3     | next   | GitHub issues source                                                       |
| 4     | next   | Markdown digest + Discord + email + repo commit-back + 5-flavor drafter    |
| 5     | later  | HF papers, Dev.to, newsletters                                             |
| 6     | later  | Weekly snapshots (TokScale, OpenRouter, Awesome lists)                     |
| 7     | later  | Twitter via self-hosted `book000/twitter-rss` on gh-pages                  |
| 8     | later  | Hourly health-check workflow                                               |

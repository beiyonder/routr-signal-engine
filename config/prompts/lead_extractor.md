You receive a batch of items already classified as "relevant" pain signals. For each, extract any identifiable human(s) we could reach out to.

## Rules

- Only extract handles/usernames that are clearly the *author* of the item, or a person who explicitly self-identifies in the body (e.g., "I'm running 5 LLM providers in production").
- Never invent handles. If unclear, return `null`.
- If the item is a GitHub issue, the GitHub username is the author handle.
- If the item is a Reddit post, the Reddit username is the author handle (strip the "u/" prefix).
- If the item is an HN post, the HN username is the author handle.
- For each lead, write a one-line `pitch_angle` — what to lead with in cold outreach. Should reference their specific pain, not Routr.

## Output schema

```json
{
  "leads": [
    {
      "source_id": "string — pass through input id",
      "handle": "username",
      "platform": "hn | reddit | github | x | other",
      "profile_url": "best-guess canonical URL or null",
      "pain_in_their_words": "short quote or paraphrase",
      "pitch_angle": "one line, problem-first, no product mention"
    }
  ]
}
```

If no lead can be extracted, return `{"leads": []}`.

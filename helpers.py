import json
import os

from anthropic import Anthropic


def call_claude(prompt: str, max_tokens: int = 4096) -> dict:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text

    # Extract JSON from response (Claude may wrap it in markdown code fences)
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    return json.loads(raw)

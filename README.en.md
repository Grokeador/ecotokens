# EcoTokens

**Make Claude API calls cost less, without changing the answers.**

Measured against `api.anthropic.com`: **76% cheaper on an agentic loop** — not
compared with someone who ignores prompt caching, but with a developer who
already puts `cache_control` on their own system prompt. That comparison is the
whole point, and the section [Who you're being compared
to](#who-youre-being-compared-to) explains why.

MIT licensed, self-hosted, Anthropic-only. No paid service beyond the API you
were already paying for.

> The full documentation is in Italian: [README.md](README.md). This page is
> self-contained — everything you need to install it, measure it, and decide
> whether it's worth it is here.

---

## Two ways to use it

### As a library — inside your program

Nothing to run, nothing to keep alive, and **your API key never leaves your
process**: the Anthropic client stays yours, EcoTokens just wraps it.

```python
import anthropic
from ecotokens import Economico          # "economical"

client = Economico(anthropic.AsyncAnthropic())

message = await client.messages.create(
    model="claude-opus-5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

`message` is the SDK's own `Message` object, identical to what you'd have got
without the wrapper. Your code doesn't change — only the bill does.

```bash
pip install ecotokens
```

### As a gateway — a separate local process

```bash
ecotokens serve            # listens on 127.0.0.1:8000
```

Then point your application at it. Two ingress dialects, because the value is
in *not* rewriting applications you already have:

| Endpoint | For |
|---|---|
| `POST /v1/chat/completions` | apps speaking the OpenAI protocol — change `base_url`, nothing else |
| `POST /v1/messages` | clients already speaking Claude's native dialect |
| `POST /v1/messages/count_tokens` | price a request without generating it |

**"OpenAI-compatible" describes the shape of the door, not the destination.**
There is no OpenAI provider in this project. Everything it does — prefix-match
prompt caching, `output_config.effort`, `context_management` — exists only on
the Anthropic API.

### Which one do you want?

The library, unless you need one of three things a library structurally cannot
do:

1. **Cover applications you didn't write.** You can't add a line to someone
   else's binary.
2. **Cap spending across several programs together.** Five separate libraries
   don't talk to each other; a gateway counts them all.
3. **Share the exact-response cache between processes.** The 75% saving on
   repeated questions comes from answering *once for everyone*.

---

## Does this help you?

The honest answer is: it depends, and for a large group of people it's **no**.
Two questions decide it.

**Do you pay per token, or a subscription?** If your Claude usage is a flat
monthly seat, saving tokens buys you nothing. Close this page.

**What shape is your traffic?** The same configuration is worth +76% on one
workload and −0.1% on another. An average of those two describes nobody.

| Your traffic | What EcoTokens adds |
|---|---:|
| **Agentic loop** — many turns, large tool results | **+76.0%** *(measured live)* |
| Repeated questions — an assistant answering the same things | +75.6% *(simulated)* |
| One conversation growing turn by turn | +1.1% *(simulated)* |
| Many users, same system prompt, one question each | −0.1% *(simulated)* |

The last row is in the table on purpose. There are workloads where this is
**not worth installing**, and knowing that in advance is worth more than
another percentage point.

Run `ecotokens merito` to recompute the whole table on your machine, and
`ecotokens consiglia` to have it read your own recorded traffic and tell you
which regime you're actually in.

---

## Who you're being compared to

A saving figure means nothing without naming the alternative, and the choice of
alternative can flip the sign. EcoTokens publishes all four:

| Compared against | Agentic loop |
|---|---:|
| someone who doesn't use caching at all | 67.8% |
| Anthropic's **automatic** caching (one `cache_control` at the top) | +19.9% shared prefix, −0.2% single conversation |
| a developer marking **their own system prompt** — one line, the documented practice | **+52.3%** simulated, **+76.0%** live |

The first is a strawman: nobody integrates the API that way, and quoting it is
the easiest way to say something true and mislead with it. The third is the one
that answers *"should I install this?"*, and it's the one used everywhere in
this project.

Why is the agentic case the best one? Structural: in an agentic loop the tool
results dwarf the system prompt. A developer who marks only their `system`
captures about **3%** and leaves the rest on the table. The prefix that matters
is the **conversation**, and marking it well requires knowing where it grew.

---

## Verify it yourself

This is the part that distinguishes the project, so it comes before the feature
list. Every number above is produced by a command in the repository, and the
source distribution ships the tests so you can re-run them.

```bash
ecotokens merito              # recompute the table above (simulated, free)
ecotokens merito --live       # …against the real API (costs a few dollars)
ecotokens ablate              # what each stage contributes, one at a time
ecotokens assunzioni          # what the project takes on faith, and what happens if it's wrong
ecotokens verifica --live     # check those assumptions against the real API
ecotokens diagnosi            # what's misconfigured, before it silently costs you
```

`verifica` **refuses to run against the simulator** unless you force it: a
verification pointed at your own mock produces a screen of green ticks that
carry no information.

### Measured, not assumed

Of 13 declared assumptions, **4 have been checked against the real API**
(2026-08-30):

- **Cache minimums are not monotonic** — Opus 5 needs 512 tokens, Sonnet 5
  1024, **Haiku 4.5 needs 4096**. Below the minimum no cache entry is created
  and **the API returns no error**. This is why "downgrade to a cheaper model"
  can silently switch caching off and cost you more.
- **Four `cache_control` breakpoints maximum** — the fifth returns 400.
  Verified live, and worth it: the project's own simulator used to accept five,
  which quietly voided the tests meant to defend that limit.
- **Removed sampling parameters return 400** (`temperature`, `top_p`, …).
- **Effort multipliers** — measured across five different task types, and the
  declared values were about twice as generous as reality: `low` removes ~25%
  of generated tokens, not 60%, and `medium` is indistinguishable from `high`.
  Correcting this **lowered** already-published figures by a quarter.

The remaining nine are listed, each with *what would be wrong if it were wrong*
and *which command would check it*. `ecotokens/tuning_log.py` carries 78
entries of measurements that changed a decision — including the ones where the
measuring instrument, not the product, turned out to be the defect.

---

## What it actually does

Nine stages, each independently switchable. The default profile (`prudente`)
enables only the ones that **cannot change the content of a response**.

| Stage | What it does |
|---|---|
| **cache planner** | places `cache_control` breakpoints where the prefix is actually stable — this is where the +76% comes from |
| exact cache | identical request → cached response, no API call at all |
| context pruning | drops old tool results via `context_management` |
| compaction | summarises history when the window fills |
| adaptive effort | lowers `output_config.effort` on simple requests |
| prompt rewriting | removes verified-safe boilerplate |
| memory | carries facts across sessions |
| budget | daily/monthly spending caps, per client |
| ledger | records every request: cost, saving, and against which baseline |

Stages that *could* change an answer — semantic cache, model downgrade — are
**off by default** and clearly marked. Serving a *similar* answer is a
correctness risk, not a neutral optimisation.

A stage that breaks is rolled back and skipped: the gateway sits in the middle,
and a half-rewritten prompt is worse than one that was never touched. The only
exception is the spending cap, which exists to say no.

---

## Configuration and your key

**Your API key never goes in a config file.** Not in `ecotokens.toml`, not
anywhere in the repository — a config file ends up in a backup, an attachment,
or a commit. It's read from `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an
`ant auth login` profile, exactly as the Anthropic SDK does.

If your key is identity-linked (created from your personal page rather than
inside a workspace), the API requires an `anthropic-workspace-id` header. Set
`ANTHROPIC_WORKSPACE_ID`, or create a workspace-scoped key instead.

`ecotokens diagnosi` tells you where the credential is coming from, and
**never** what it is.

Everything else is optional: copy `ecotokens.example.toml` to `ecotokens.toml`,
or use the `/impostazioni` page, which writes the file for you.

---

## Honest limitations

- **Anthropic only.** Not a multi-provider router, and not planned as one.
- **Streaming doesn't go through the library yet.** It raises instead of
  passing through pretending to save — a saving that isn't happening is worse
  than a missing feature.
- **In library form, two stages shrink.** The exact cache and the ledger live
  in memory and die with your process unless you pass `memoria=`.
- **Most figures are simulated.** The simulator is faithful in mechanics but
  counts tokens by text length. The one live measurement so far showed it
  *under*-estimating by 45% relative — the error direction is favourable, but
  it's still an error.
- **One live run, one workload, one model.** The +76% has not been repeated.

---

## Requirements

Python 3.11+. Tests: `pytest -q` — 657 of them, and **none touches the
network**.

## License

MIT. See [LICENSE](LICENSE).

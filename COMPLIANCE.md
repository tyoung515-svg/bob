# Using your model subscriptions with BoB

BoB is a **consumer application**. Every backend it talks to is reached with
**your own credentials, on your own machine** — either a provider API key you hold,
or that provider's **own official CLI** logged in under **your own subscription**.
BoB does not bundle, proxy, resell, or multi-tenant anyone's access.

This document explains why the CLI-under-your-own-subscription pattern is the
release's headline and how to stay within each vendor's terms.

## The model, in one line

> Your credentials, the vendors' own official CLIs, on your own machine, within
> your own plan's limits — one operator per install.

That is the whole design. It is also what "sovereignty" means for BoB: you are not
renting access through a middleman, so there is no middleman to cut you off.

## The two red lines that actually apply

Across the major AI vendors, the programmatic-use rules that genuinely constrain a
tool like BoB reduce to two, and BoB satisfies **both by construction**:

1. **Don't share, resell, or multi-tenant a single login.** One BoB install is one
   operator using their own credentials. It is not a service that fans your login
   out to other people.
2. **Don't rotate multiple accounts to beat a plan's rate limits.** BoB runs within
   whatever limits your single plan grants. It does not pool or rotate accounts to
   circumvent caps.

Because a BoB install is *one person, own credentials, own machine, within their
own plan*, neither red line is crossed. That is the compliance story — and the
product story.

## What BoB does NOT claim

BoB is **not** "approved," "endorsed," or "certified" by any model provider. No such
blanket approval exists or is implied. **You are responsible for using each backend
within that provider's current Terms of Service and acceptable-use policy.** Terms
change; check them.

## Per-backend notes

For each backend you choose to enable, use **your own** API key or the vendor's
**official** CLI under **your own** login, and follow that vendor's published
guidance for programmatic / headless use. Consult each vendor's own documentation
for the current terms (BoB does not reproduce them here because they change):

| Backend | How BoB uses it | Where to look |
| --- | --- | --- |
| **Anthropic — `claude` CLI** (`claude_code`) | Runs `claude` headless (`claude -p`) under your Claude subscription. Anthropic ships `claude setup-token` specifically to seed subscription auth for scripts/CI. | Anthropic's Claude Code / CLI documentation |
| **Anthropic — API** (`claude_api`, `claude-pipeline`) | Direct Messages API with your `ANTHROPIC_API_KEY`. | platform.claude.com |
| **OpenAI — `codex` CLI** (`codex_code`) | Runs `codex exec` for non-interactive use, under your ChatGPT subscription or an API key (your choice). | OpenAI's Codex CLI documentation |
| **Google — `agy` (Antigravity) CLI** (`agy_code`) | Runs the Antigravity CLI headless under your Google login. A metered Gemini REST path (`gemini_pro`) with `GOOGLE_API_KEY` is a separate, independent backend. | Google's Antigravity / Gemini CLI documentation |
| **Z.AI — GLM coding plan** (`glm`) | OpenAI-compatible calls with your own key; coding-plan and pay-as-you-go endpoints are both supported. | Z.AI documentation |
| **Moonshot — Kimi** (`kimi_code`, `kimi_cli`, `kimi_platform`) | Your own key (membership or platform), or the Kimi CLI under your own login. | Moonshot / Kimi documentation |
| **DeepSeek, MiniMax** | OpenAI-compatible calls with your own key. | Each provider's documentation |
| **Local (Ollama, LM Studio, llama.cpp)** | Fully local; no third-party terms apply. | — |

Every subscription-CLI backend has an **API-key equivalent** (`claude_api`,
`gemini_pro`, etc.). If you would rather not use a subscription CLI for a given
backend, use the metered API path instead — it is always available.

## Why this matters

The value BoB adds is everything *past* aggregation — verification, orchestration,
and the fact that it runs on infrastructure you own with credentials you hold. The
single-operator / own-credentials model is what makes that sovereignty real, and it
is the same fact that keeps each backend within the vendor's intended use.

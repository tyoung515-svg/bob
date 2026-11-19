# Using your model subscriptions with BoB

BoB is a **consumer application**. Every backend it talks to is reached with
**your own credentials, on your own machine**: a provider API key you hold, or your
own subscription sign-in. BoB does not proxy, resell, share or multi-tenant anyone's
access.

This document says exactly how each route works, so that you can check it against each
vendor's terms yourself.

## The model, in one line

> Your credentials, on your own machine, within your own plan's limits — one operator
> per install.

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

## What BoB does NOT claim

BoB is **not** "approved," "endorsed," or "certified" by any model provider. No such
blanket approval exists or is implied. **You are responsible for using each backend
within that provider's current Terms of Service and acceptable-use policy.** Terms
change; check them. Every subscription route below has an API-key equivalent, and the
API-key route is always available.

## The Windows app: what is bundled, and how sign-in works

The Windows installer carries a runtime that includes two vendor programs, so that
sign-in works without a separate install:

| Program | Licence | Changed by BoB? |
| --- | --- | --- |
| OpenAI **Codex CLI** | Apache-2.0 | No |
| Anthropic **Claude Code** | Proprietary, Anthropic's terms | No. It is downloaded from Anthropic at build time, checked against a pinned hash, and shipped unmodified. |

### Claude subscription

1. In BoB you choose **Sign in with Claude (subscription)**. BoB starts Claude Code's own
   sign-in (`claude auth login`) inside your lane.
2. BoB shows the sign-in address that Claude Code produced. You open it in your browser
   and sign in **on Anthropic's own page**. BoB never sees your password.
3. Anthropic's page gives you a code. You paste it into BoB, which hands it to Claude
   Code. Claude Code stores the sign-in in your lane, on your machine.
4. Each turn runs the Claude Code program under that sign-in.

Anthropic publishes conditions for products that preinstall Claude Code and for signing
in to a Claude account from inside another application. Read them before you use this
route. If you would rather not, use an Anthropic API key.

### ChatGPT subscription

1. In BoB you choose **Sign in with OpenAI (ChatGPT)**. BoB starts the Codex CLI's own
   device sign-in (`codex login --device-auth`) inside your lane.
2. BoB shows the short code that Codex produced. You open **OpenAI's own page** in your
   browser and enter it. BoB never sees your password.
3. Codex stores the sign-in in your lane, on your machine.
4. **Turns do not run through the Codex CLI.** BoB reads the stored sign-in and sends
   your requests directly to OpenAI's Codex service (`chatgpt.com/backend-api/codex`),
   and renews the sign-in at `auth.openai.com` when it expires. BoB identifies itself
   as BoB in those requests.

This direct route is what lets you pick any model your plan offers per conversation and
lets the model use BoB's tools. It is not the route OpenAI documents for third-party
tools. Check your plan's terms before you use it. If you would rather not, use an
OpenAI API key, or on a from-source install use the `codex_code` backend, which runs
`codex exec`.

## Per-backend notes

For each backend you enable, use **your own** API key or **your own** sign-in, and
follow that vendor's published guidance for programmatic or headless use. Consult each
vendor's own documentation for the current terms (BoB does not reproduce them here
because they change):

| Backend | How BoB uses it | Where to look |
| --- | --- | --- |
| **Anthropic — Claude subscription** (Windows app) | Runs the bundled, unmodified Claude Code program under your sign-in, as described above. | Anthropic's Claude Code documentation and legal pages |
| **Anthropic — `claude` CLI** (`claude_code`, from source) | Runs `claude` headless (`claude -p`) under your Claude subscription, using the `claude` you installed yourself. | Anthropic's Claude Code / CLI documentation |
| **Anthropic — API** (`claude_api`, `claude-pipeline`) | Direct Messages API with your `ANTHROPIC_API_KEY`. | platform.claude.com |
| **OpenAI — ChatGPT subscription** (Windows app) | Signs in through the bundled Codex CLI, then calls OpenAI's Codex service directly with that sign-in, as described above. | OpenAI's terms and Codex documentation |
| **OpenAI — `codex` CLI** (`codex_code`, from source) | Runs `codex exec` for non-interactive use, under your ChatGPT subscription or an API key (your choice). | OpenAI's Codex CLI documentation |
| **OpenAI — API** | Direct calls with your own key. | platform.openai.com |
| **Google — `agy` (Antigravity) CLI** (`agy_code`, from source) | Runs the Antigravity CLI headless under your Google login. A metered Gemini REST path (`gemini_pro`) with `GOOGLE_API_KEY` is a separate, independent backend. | Google's Antigravity / Gemini CLI documentation |
| **Z.AI — GLM** (`glm`) | OpenAI-compatible calls with your own key; coding-plan and pay-as-you-go endpoints are both supported. | Z.AI documentation |
| **Moonshot — Kimi** (`kimi_code`, `kimi_cli`, `kimi_platform`) | Your own key (membership or platform), or the Kimi CLI under your own login. | Moonshot / Kimi documentation |
| **DeepSeek, Qwen (Alibaba Model Studio), MiniMax** | OpenAI-compatible calls with your own key. | Each provider's documentation |
| **Local (Ollama, LM Studio, llama.cpp)** | Fully local; no third-party terms apply. | — |

## Why this matters

The value BoB adds is everything *past* aggregation: verification, orchestration, and
the fact that it runs on infrastructure you own with credentials you hold. The
single-operator, own-credentials model is what makes that sovereignty real.

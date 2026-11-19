# Runtime third-party inventory

Source of facts: `Containerfile`, `cli-pins.json`, the two requirements files installed by the Containerfile, and package metadata read from an image built from this tree. This is an inventory of the build instructions, not a resolved image SBOM. Direct downloads, bundled components and direct install requirements each have a row. Resolved indirect dependencies are not enumerated here; an image built for 0.99.1 held about 100 Python distributions, 130 npm packages and 650 OS packages.

Where each licence cell comes from:

- **Python and npm packages:** what the installed package declares in its own metadata, read from a built image. Each package's licence file is installed with it inside the image.
- **uv, Qdrant, llama.cpp and Codex CLI**, which the build downloads as binaries: the licence file published with the pinned version. Copies are in `licenses/` beside this file and are installed with it under `/opt/bob/deploy/runtime/licenses/`.
- **Claude Code:** the statement in `cli-pins.json`.
- **Everything else** says **Not declared in build inputs** rather than infer a licence from a project name. Inside the image, each OS package has its own `/usr/share/doc/<package>/copyright`, and the managed Python runtime has its `LICENSE.txt`. The Chromium headless shell is downloaded by Playwright without a licence file; see the Chromium project for its terms.

This is not a complete licence inventory. That needs the resolved image, which differs from build to build because most versions are ranges.

**Claude Code is proprietary Anthropic software, not covered by this repository licence.** The pin states that product preinstallation is subject to Anthropic Commercial Terms and conditions in [Anthropic legal/compliance documentation](https://code.claude.com/docs/en/legal-and-compliance); individual subscription use also follows applicable Consumer Terms. This records the pin's statement and makes no legal assessment.

| Name | Version selected by build | Origin | Licence / terms | Verification specified by build |
| --- | --- | --- | --- | --- |
| Ubuntu base image | 24.04 (tag; no digest) | ubuntu:24.04 container registry image | Not declared in build inputs | Registry pull; no content digest pinned |
| uv | 0.13.6 | github.com/astral-sh/uv releases | MIT OR Apache-2.0 (`licenses/uv-LICENSE-MIT.txt`, `licenses/uv-LICENSE-APACHE.txt`) | fetch-checked SHA256 |
| Qdrant | 1.18.0 | github.com/qdrant/qdrant releases | Apache-2.0 (`licenses/qdrant-LICENSE.txt`) | fetch-checked SHA256 |
| llama.cpp server and lib*.so* | b11747 | github.com/ggml-org/llama.cpp releases | MIT (`licenses/llama.cpp-LICENSE.txt`) | Archive fetch-checked SHA256; server existence and backend check |
| Codex CLI | rust-v0.166.0 | github.com/openai/codex releases | Apache-2.0 (`licenses/codex-LICENSE.txt`, `licenses/codex-NOTICE.txt`) | fetch-checked SHA256; binary discovery |
| Claude Code CLI | 2.1.327 linux-x64 | downloads.claude.ai/claude-code-releases | Proprietary Anthropic Claude Code; Anthropic Commercial Terms for product preinstallation; applicable Consumer Terms for individual subscription use | fetch-checked SHA256 and exact 372904464-byte length; root:root, 0755 |
| Managed Python runtime | 3.13 (patch not pinned) | uv python install (download origin not specified) | Not declared in build inputs | uv installer; no explicit runtime digest in these inputs |
| Qwen3-Embedding-4B-Q4_K_M.gguf | 4B Q4_K_M (resolve/main; revision not pinned) | huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF | Not declared in build inputs | Install-time fetch-checked SHA256 from build-configured model catalog; not downloaded/bundled during image build |
| Qwen3-Embedding-0.6B-Q8_0.gguf | 0.6B Q8_0 (resolve/main; revision not pinned) | huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF | Not declared in build inputs | Install-time fetch-checked SHA256 from build-configured model catalog; not downloaded/bundled during image build |
| @modelcontextprotocol/server-filesystem | 2025.8.21 | npm (registry not specified) | MIT | Exact direct version; no committed lockfile or explicit digest in build |
| @playwright/mcp | 0.0.40 | npm (registry not specified) | Apache-2.0 | Exact direct version; no committed lockfile or explicit digest in build |
| playwright | 1.56.0-alpha-1758750661000 | npm (registry not specified) | Apache-2.0 | Exact direct version; no committed lockfile or explicit digest in build |
| playwright-core | 1.56.0-alpha-1758750661000 | npm (registry not specified) | Apache-2.0 | Exact direct version; no committed lockfile or explicit digest in build |
| Chromium headless shell | Playwright revision 1191 | playwright install --with-deps --only-shell chromium (download URL not specified) | Not declared in build inputs | Expected executable path test; no SHA256 pinned here |
| ca-certificates | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| curl | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| systemd | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| systemd-sysv | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| kmod | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| dbus | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| postgresql-16 | 16 series; package revision unpinned | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| redis-server | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| git | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| openssh-client | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| sudo | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| locales | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| libgomp1 | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| libstdc++6 | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| libcurl4t64 | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| nftables | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| iproute2 | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| acl | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| procps | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| nodejs | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| npm | Unpinned Ubuntu 24.04 package | apt repositories configured by Ubuntu base image | Not declared in build inputs | apt-get; no package version or content hash specified by build |
| aiohttp | >=3.9,<3.14 | uv pip install from core/gateway requirements (index not specified) | Apache-2.0 AND MIT | Resolver/install; no lockfile or --require-hashes in build |
| aiohttp-cors | >=0.7 | uv pip install from core/gateway requirements (index not specified) | Apache Software License | Resolver/install; no lockfile or --require-hashes in build |
| aiosqlite | >=0.19 | uv pip install from core/gateway requirements (index not specified) | MIT License | Resolver/install; no lockfile or --require-hashes in build |
| asyncpg | >=0.29 | uv pip install from core/gateway requirements (index not specified) | Apache-2.0 | Resolver/install; no lockfile or --require-hashes in build |
| blake3 | >=0.4 | uv pip install from core/gateway requirements (index not specified) | CC0-1.0 OR Apache-2.0 | Resolver/install; no lockfile or --require-hashes in build |
| python-dotenv | >=1.0 | uv pip install from core/gateway requirements (index not specified) | BSD-3-Clause | Resolver/install; no lockfile or --require-hashes in build |
| langgraph | >=0.2 | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| langchain-core | >=0.3 | uv pip install from core/gateway requirements (index not specified) | MIT License | Resolver/install; no lockfile or --require-hashes in build |
| langchain-openai | >=0.2 | uv pip install from core/gateway requirements (index not specified) | MIT License | Resolver/install; no lockfile or --require-hashes in build |
| httpx | >=0.28,<1 | uv pip install from core/gateway requirements (index not specified) | BSD License | Resolver/install; no lockfile or --require-hashes in build |
| langchain-mcp-adapters | >=0.3 | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| mcp | >=1.0 | uv pip install from core/gateway requirements (index not specified) | MIT License | Resolver/install; no lockfile or --require-hashes in build |
| PyJWT | >=2.0; >=2.8 | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| pydantic | >=2.0 | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| pyyaml | >=6.0 | uv pip install from core/gateway requirements (index not specified) | MIT License | Resolver/install; no lockfile or --require-hashes in build |
| croniter | >=2,<7 | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| anthropic | >=0.39 | uv pip install from core/gateway requirements (index not specified) | Apache Software License | Resolver/install; no lockfile or --require-hashes in build |
| redis | >=5.0 | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| python-frontmatter | >=1,<2 | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| markdown-it-py | >=3,<4 | uv pip install from core/gateway requirements (index not specified) | MIT License | Resolver/install; no lockfile or --require-hashes in build |
| tiktoken | >=0.5,<1 | uv pip install from core/gateway requirements (index not specified) | MIT License | Resolver/install; no lockfile or --require-hashes in build |
| qdrant-client | >=1.12 | uv pip install from core/gateway requirements (index not specified) | Apache Software License | Resolver/install; no lockfile or --require-hashes in build |
| pytest | >=7.4; >=7.0; also installed unversioned by Containerfile | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| pytest-asyncio | >=0.23 | uv pip install from core/gateway requirements (index not specified) | Apache-2.0 | Resolver/install; no lockfile or --require-hashes in build |
| pytest-socket | >=0.7 | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| aioresponses | >=0.7 | uv pip install from core/gateway requirements (index not specified) | MIT License | Resolver/install; no lockfile or --require-hashes in build |
| jinja2 | >=3.1 | uv pip install from core/gateway requirements (index not specified) | BSD License | Resolver/install; no lockfile or --require-hashes in build |
| watchdog | >=4.0 | uv pip install from core/gateway requirements (index not specified) | Apache-2.0 | Resolver/install; no lockfile or --require-hashes in build |
| bcrypt | >=4.0 | uv pip install from core/gateway requirements (index not specified) | Apache Software License | Resolver/install; no lockfile or --require-hashes in build |
| pyotp | >=2.9 | uv pip install from core/gateway requirements (index not specified) | MIT | Resolver/install; no lockfile or --require-hashes in build |
| Resolved OS, Python and npm transitive dependencies; Playwright --with-deps OS additions | Resolved at build time; not enumerated/pinned by inputs | apt / uv / npm / Playwright dependency resolvers | Not declared in build inputs | No complete resolved inventory or explicit dependency hashes in these build inputs |

`fetch-checked.sh` checks the configured SHA256 before installing an artifact. Model URLs and digests are build arguments persisted for an owner-selected download at setup time. None of the missing pins, lockfiles or licence metadata above is supplied by adding this inventory.

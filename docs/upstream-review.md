# Agent-Reach review

Reviewed the public `main` branch and MIT license on November 13, 2026. Universal Reach is a new implementation rather than a fork.

## Upstream design

Agent-Reach is a Python CLI organized around per-platform `Channel` classes. A channel declares actions and may list ordered backends; the dispatcher retries with the next backend for retryable failures. Channel definitions also describe prerequisites, login/session needs, and checks. Its runtime manages external tools, and its OpenCLI backend drives Chrome through a browser bridge and a Reach-owned browser profile. The repository also includes agent skill instructions and install/maintenance flows.

This design is useful where a platform needs several mature tools or a browser fallback. Its ordered backend model is a good operational pattern, and its per-platform isolation informed Universal Reach's adapter boundaries and capability reporting.

## Why this repository is separate

Universal Reach has a different contract: one local package exposes a compact shared service through both a CLI and a standard MCP server; anonymous public retrieval is the default; optional credentials are environment-only; and it does not import browser cookies, create an authenticated browser profile, or install a collection of third-party platform CLIs. The inspected Agent-Reach tree is centered on its own CLI, channel runtime, tool manager, browser/session support, and bundled skill instructions; it contains no MCP server module.

Forking it would couple Universal Reach to those installation and session-management paths. Reusing individual code paths would also require carrying the upstream MIT notices. No Agent-Reach source code is copied here. The implementation instead uses the same language and a small adapter boundary while sharing only the general ideas of per-platform modules, structured failures, and capability-aware diagnostics.

## Upstream references

- [Agent-Reach README](https://github.com/Panniantong/Agent-Reach/blob/main/README.md): user-facing platform list, installation, security, and usage model.
- [Channel base](https://github.com/Panniantong/Agent-Reach/blob/main/agent_reach/channels/base.py): action contracts, ordered backend fallback, prerequisites, and session metadata.
- [Tool manager](https://github.com/Panniantong/Agent-Reach/blob/main/agent_reach/runtime/tools.py): isolated external-tool installation and version policy.
- [OpenCLI backend](https://github.com/Panniantong/Agent-Reach/blob/main/agent_reach/backends/opencli.py): browser-bridge integration.
- [MIT license](https://github.com/Panniantong/Agent-Reach/blob/main/LICENSE): upstream licensing terms.

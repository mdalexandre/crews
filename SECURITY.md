# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub private vulnerability reporting: open the repository's Security tab and choose "Report a vulnerability". Do not open a public issue for a vulnerability.

Include the version, the steps to reproduce and the impact you expect. You can expect an acknowledgement within a few days. Fixes are released as a patch version and noted in the changelog.

## What the plugin does with your privileges

Crews is a Claude Code plugin. Like every plugin, it runs code with your user privileges, outside the Claude Code sandbox:

* four hooks (`hooks/hooks.json`): a seat guard, a check leak warning, a session start line and an inline budget counter. They read the hook payload from Claude Code and exit 0 to allow or 2 to block. Only the seat guard blocks by default (a generic or unplanned subagent dispatch), and only the inline budget, which is off by default, can block a Bash call. All of them fail open.
* a local MCP server (`server.py`), started with `uv`, offering five tools. It writes plan files, a plan index and run records under `CREWS_HOME` (the plugin data directory under the plugin), and nothing into the plugin install directory.

Review `hooks/hooks.json`, `.mcp.json` and `hooks/*.py` before enabling the plugin.

## Network

The plugin makes network calls only to TypeSafe (`https://api.typesafe.ai`), only when you configure a TypeSafe API key, and only after you approve each call with `crew approve`. A live call sends task text: the task, and for the role judge each role's name, kind, mission, write scope and professional frame; skill routing also sends candidate skill names and descriptions; the brief check sends the authored text of a check role. The key is sent only in the Authorization header and is never printed or stored by the plugin. With no key configured the plugin makes no network call.

`uv` itself downloads Python and the locked dependencies on first start.

## Scope

In scope: the hooks, the MCP server, the planner and the command line in this repository. Out of scope: vulnerabilities in Claude Code, uv, the TypeSafe service or third party dependencies (report those upstream), and the behavior of a model you dispatch.

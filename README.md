# Codex driven skills

Four skills in codex I have created for agent orchestration work. The orchestration is driven by creating and coordinating codex threads in the desktop app and allows for an easier managed view when working across multiple projects, tasks, or threads. Used mainly for taking a tracked repo from research through to a verified pr and merge:

- `$gepetto` — the orchestrator - delegates agents for research, implementation (`$pinocchio`), review, and completion
- `$pinocchio` — implementer - delivers one approved leaf as a verified pull request
- `$jiminy` — supervisor, and pr manager - monitors the work and validates exact-head merge gates
- `$checkpoint` — compaction - continues long-running work in a fresh Codex task. When a thread starts to exceed the context window, a checkpoint/handoff forms so work continues in a fresh thread, with the appropriate context needed for the next agent to kick off.

## Install

Run one of these commands:

```sh
npx @dylanmccavitt/skills@latest
```

```sh
bunx @dylanmccavitt/skills@latest
```

```sh
pnpm dlx @dylanmccavitt/skills@latest
```

```sh
yarn dlx @dylanmccavitt/skills@latest
```

The installer supports macOS and Linux and requires Node.js 18 or newer and
Python 3. It installs the suite under `${CODEX_HOME:-$HOME/.codex}`, links all
four skills into the Codex skills directory, and adds the orchestration hooks
without removing existing hook entries. If `hooks.json` already exists, the
installer saves a timestamped backup before updating it.

The installer refuses to replace unrelated skills, an unmanaged installation
directory, or a symlinked hook configuration. Resolve the reported conflict and
run the command again. Restart Codex or begin a new task after installation.

To update the suite, rerun the same command.

## Development

Run the complete test suite:

```sh
npm test
```

Inspect the files that will be published:

```sh
npm pack --dry-run
```

The machine-readable delivery graph at
`gepetto/references/workflow.json` records task flow, guards, invalidation
routes, and terminal states. Validate it directly with:

```sh
python3 hooks/orchestration_graph.py
```

## License

[MIT](LICENSE)

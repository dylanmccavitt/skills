# Codex driven skills

Four skills in codex I have created for agent orchestration work. The orchestration is driven by creating and coordinating codex threads in the desktop app and allows for an easier managed view when working across multiple projects, tasks, or threads. Used mainly for taking a tracked repo from research through to a verified pr and merge:

- `$gepetto` — the orchestrator - delegates agents for research, implementation (`$pinocchio`), review, and completion
- `$pinocchio` — implementer - delivers one approved leaf as a verified pull request
- `$jiminy` — supervisor, and pr manager - monitors the work and validates exact-head merge gates
- `$checkpoint` — compaction - continues long-running work in a fresh Codex task. When a thread starts to exceed the context window, a checkpoint/handoff forms so work continues in a fresh thread, with the appropriate context needed for the next agent to kick off.

<h2 align="center">Thread-driven agent graph</h2>

<p align="center">
  Each stage runs in a dedicated Codex thread with a focused responsibility.
</p>

<table align="center">
  <tr>
    <td align="center">
      <strong>Gepetto</strong><br>
      <sub>Coordinates delivery</sub>
    </td>
    <td align="center">→</td>
    <td align="center">
      <strong>Research</strong><br>
      <sub>Defines the work</sub>
    </td>
    <td align="center">→</td>
    <td align="center">
      <strong>Pinocchio</strong><br>
      <sub>Implements one leaf</sub>
    </td>
  </tr>
  <tr>
    <td></td>
    <td></td>
    <td colspan="3" align="center">↓</td>
  </tr>
  <tr>
    <td align="center">
      <strong>Complete</strong><br>
      <sub>PR merged</sub>
    </td>
    <td align="center">←</td>
    <td align="center">
      <strong>Jiminy</strong><br>
      <sub>Enforces merge gates</sub>
    </td>
    <td align="center">←</td>
    <td align="center">
      <strong>Review</strong><br>
      <sub>Validates the exact head</sub>
    </td>
  </tr>
</table>

<p align="center">
  <sub>
    Review findings return to the Pinocchio thread for repair, followed by a fresh review.
  </sub>
</p>

<table align="center">
  <tr>
    <td align="center">
      <strong>Checkpoint</strong><br>
      <sub>
        Continues any active role in a fresh Codex thread after compaction.
      </sub>
    </td>
  </tr>
</table>

<p>
  Gepetto launches the research, implementation, review, and supervision threads,
  supplies each thread with a focused contract, and routes structured results through
  the graph. The orchestration happens through active Codex thread creation—not a
  background automation or CI pipeline.
</p>

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

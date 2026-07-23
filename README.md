# Codex driven skills

Four Codex skills for agent orchestration. Coordinated Codex threads in the desktop app take a tracked repo from research through to a verified PR and merge:

- `$gepetto` — orchestrator - researches inline for single-leaf scope, delegates research, implementation (`$pinocchio`), and review threads otherwise
- `$pinocchio` — implementer - delivers one approved leaf as a verified pull request
- `$jiminy` — merge-time gatekeeper - created at JIMINY_READY, re-validates exact-head merge gates on live heads, merges in dependency order, verifies integration
- `$checkpoint` — compaction - continues long-running work in a fresh Codex thread with the context the successor needs
- Context refs — stable instructions and artifacts are exact-byte SHA-256 references, so unchanged content is not repeatedly loaded into task context
- State safety — process-locked CAS updates, crash-recoverable continuation journals, atomic graph/ledger transitions, and coordinator-bound Jiminy runners prevent competing authoritative state
- Supervision — mechanical liveness and pressure detection: hooks stamp heartbeats, measurable context/state pressure drives proactive checkpoints, and Gepetto owns every restart

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
      <sub>Merges at JIMINY_READY</sub>
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
    The reviewer collects all findings, runs one fixer pass, then re-reviews the changed delta.
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
  Gepetto researches single-leaf scope inline and launches dedicated research,
  implementation, and review threads otherwise, supplying each a focused contract
  and routing structured results through the graph. Jiminy is created only at
  JIMINY_READY to execute the merge set. The orchestration happens through active
  Codex thread creation—not a background automation or CI pipeline.
</p>

## Install

```sh
npx @dylanmccavitt/skills@latest
```

`bunx`, `pnpm dlx`, and `yarn dlx` work equally. Supports macOS and Linux;
requires Node.js 18+ and Python 3. Installs the suite under
`${CODEX_HOME:-$HOME/.codex}`, links all four skills into the Codex skills
directory, and adds the orchestration hooks without removing existing hook
entries. If `hooks.json` already exists, a timestamped backup is saved first.

The installer refuses to replace unrelated skills, an unmanaged installation
directory, or a symlinked hook configuration. Resolve the reported conflict and
rerun. Restart Codex or begin a new task after installation. To update, rerun
the same command.

The registry coordinates tasks under one user account; it is not a security
boundary against other processes running as that user.

### Uninstall

```sh
npx @dylanmccavitt/skills@latest uninstall
```

Removes managed symlinks, the managed install directory, and managed hook entries.

### Doctor

```sh
npx @dylanmccavitt/skills@latest doctor
```

Verifies the install; exits non-zero on problems.

## Development

Run the complete test suite:

```sh
npm test
```

Inspect the files that will be published:

```sh
npm pack --dry-run
```

Validate the repository-only evaluation fixtures and deterministic protocol
replay contracts:

```sh
python3 evaluation/validate.py
python3 -m unittest discover -s evaluation -p "test_*.py"
```

The versioned baseline corpus defines frozen inputs and held-out graders.
`evaluation/replay.py` replays a canonical trace against multiple exact local
workflow refs without checkout, network access, or historical-code execution,
emitting strict run-manifest, JSONL event, and result evidence. It does not run
agents, aggregate trials, or establish that one workflow version is better. See
[`evaluation/README.md`](evaluation/README.md) for canonical-byte rules,
supported workflow-v1 vocabulary, comparability keys, and the trust boundary.

### Releases

Releases are tag-driven. A `vX.Y.Z` tag must point to a commit on `main` and
must match the version in `package.json`. The release workflow tests and packs
that exact commit, publishes it to npm through npm Trusted Publishing, and then
creates the matching GitHub Release.

The first publication needs a one-time bootstrap because the npm package does
not exist yet:

```sh
npm login
npm publish --access public
npm trust github @dylanmccavitt/skills \
  --file release.yml \
  --repo dylanmccavitt/skills \
  --allow-publish
git tag v0.1.0
git push origin v0.1.0
```

Run those commands from an up-to-date, clean `main` branch after this workflow
has been merged. The initial `npm publish` creates the package; the trust command
authorizes only `.github/workflows/release.yml` to publish later versions. The
`v0.1.0` workflow run will recognize the existing npm version and create its
GitHub Release without publishing it twice.

For subsequent releases, update the package version in a pull request:

```sh
npm version patch --no-git-tag-version
```

After that pull request is merged, update `main`, tag the merge commit using the
same version, and push the tag:

```sh
git switch main
git pull --ff-only origin main
git tag vX.Y.Z
git push origin vX.Y.Z
```

Pushing the tag is the public release action. Do not reuse or move a published
release tag; publish a new package version instead.

The machine-readable delivery graph at
`gepetto/references/workflow.json` records task flow, guards, invalidation
routes, and terminal states. Validate it directly with:

```sh
python3 hooks/orchestration_graph.py
```

The watchdog classifies registered lanes against the graph's supervision
policies (healthy, stale, recycle, over-budget) and only reports; restarts stay
with the coordinator. Measured context/state pressure drives recycling, while
the event count remains a compatibility fallback when telemetry is absent.
Check it with:

```sh
python3 hooks/orchestration_watchdog.py check
```

## License

[MIT](LICENSE)

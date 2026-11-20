# Orchestrator TUI

`dirac orchestrate` runs the orchestration skill in a dedicated CLI process. Normal `dirac` and `dirac task` are unchanged. Use it for text-only delegation and instruction-revision pilots, not for agents that need to edit a workspace.

## Launch

With Node 22.13–24, dependencies installed, and the CLI built (`npm run cli:build` at the repo root):

```sh
node cli/dist/cli.mjs orchestrate --config "$HOME/.dirac-codex" \
  '^3 %6 !1 Ask one subagent to suggest a clearer title for this brief, then report the result.'
```

The default is **OpenAI Codex / GPT-6 Luna / low reasoning effort**. Omit `--config` if your default Dirac profile already has OpenAI Codex authentication. A terminal displays the limits and asks for approval before any model request. Type `y` and press Enter to open the normal Dirac TUI. For scripts, pass `--approve --json`. `--timeout <seconds>` can bound a headless trial.

Put controls at the beginning of the brief. `^N` is the total launch cap (1–100), `%P` is the full-run shortfall allowance in percent (1–10), and `!X` is the concurrency ceiling (1–10). All default to 1. Example: `^25 %6 !6` means 24–25 launches for a full run, at most six active, and no reason to launch filler. A launch consumed by a failure or cancellation still counts. A request rejected before launch does not. The session prints the ledger at exit. Quit and start a new process to approve a new budget.

## What is enforced

- The CLI parses and validates controls before approval. Only the leading control tokens count; occurrences in the task brief do not.
- The native runner checks remaining capacity before each launch and releases the active slot when it settles. A tool request larger than the available budget or concurrency is rejected before fan-out. Dirac's existing cancellation signal propagates to children.
- The parent can respond and request native subagents; the child is limited to response tools. The mode disables project hooks, checkpoints, automatic completion-verification subagents, named-agent overrides, utility-model overrides for child launches, and history forwarding to children. Normal Dirac still controls provider authentication and stores task transcripts.
- Model selection beyond the fixed invocation defaults, whether work is worth doing, held-out design, scoring, and stopping for lack of further improvement remain reasoning tasks. The ledger is a hard limit on **subagent launches**, not on provider requests, token spend or elapsed time. Independent final validation is an instruction, not a reserved ledger quota.

## Stock comparison

Run from the repo root after building:

```sh
DIRAC_STOCK_BIN=/path/to/installed/dirac node --test tests/e2e/cli/orchestration*.test.mjs
```

Without `DIRAC_STOCK_BIN`, the comparison uses the normal `task` command from the same build. The scripted provider sends both modes the same two-child batch under a one-launch brief. Installed Dirac 0.5.24 launched both children (four model requests). Orchestration mode rejected the batch and launched neither (two model requests). A two-launch budget allowed both; no approval produced zero requests. The PTY test exercises the interactive approval and completion path.

In one live Luna/low smoke comparison, both modes delegated once and returned `ALPHA` to the user. Dirac reported 5,244 input tokens and 112 output tokens across the parent and child in orchestration mode, versus 9,276 input and 99 output tokens in stock, with stock additionally reporting 4,480 cache-read tokens. The event spans were about 7.3 and 7.8 seconds. This is one run per mode, not evidence of a general speedup or billing savings.

import assert from "node:assert/strict"
import { mkdtemp, mkdir, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { test } from "node:test"
import { fileURLToPath } from "node:url"
import { startFixtureProvider } from "./fixture-provider.mjs"
import { TerminalSession } from "./terminal-session.mjs"

test("the Dirac TUI asks for approval before sending a model request", async () => {
	const directory = await mkdtemp(path.join(tmpdir(), "dirac-orchestrate-tui-"))
	const config = path.join(directory, "config")
	const workspace = path.join(directory, "workspace")
	await mkdir(workspace)
	const fixture = await startFixtureProvider(config, "orchestration-cap", workspace)
	let terminal
	try {
		terminal = new TerminalSession({
			command: process.execPath,
			args: [fileURLToPath(new URL("../../../cli/dist/cli.mjs", import.meta.url)), "orchestrate", "--config", config,
				"--provider", "openai", "--model", "tui-fixture", "^1 !1 Ask two subagents for short replies."],
			cwd: workspace,
			env: process.env,
			cols: 120,
			rows: 35,
		})
		await terminal.waitForText("Approve this delegation budget?")
		assert.equal(fixture.requests, 0)
		terminal.write("y\r")
		await terminal.waitForText("Orchestration fixture completed.", 20_000)
		assert.equal(fixture.requests, 2)
	} finally {
		await terminal?.stop()
		await fixture.stop()
		await rm(directory, { recursive: true, force: true })
	}
})

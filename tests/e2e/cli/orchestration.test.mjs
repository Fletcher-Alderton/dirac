import assert from "node:assert/strict"
import { spawn } from "node:child_process"
import { mkdtemp, mkdir, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { test } from "node:test"
import { fileURLToPath } from "node:url"
import { startFixtureProvider } from "./fixture-provider.mjs"

const bundle = fileURLToPath(new URL("../../../cli/dist/cli.mjs", import.meta.url))

async function run(mode, { controls = "^1 %1 !1", approved = true } = {}) {
	const directory = await mkdtemp(path.join(tmpdir(), "dirac-orchestrate-"))
	const config = path.join(directory, "config")
	const workspace = path.join(directory, "workspace")
	await mkdir(workspace)
	const fixture = await startFixtureProvider(config, "orchestration-cap", workspace)
	try {
		const common = ["--config", config, "--cwd", workspace, "--json", "--provider", "openai", "--model", "tui-fixture"]
		const brief = `${controls} Ask two subagents for short replies.`
		const specific = mode === "orchestrate" ? (approved ? ["--approve"] : []) : ["--act", "--subagents", "--no-index"]
		const stock = mode === "task" && process.env.DIRAC_STOCK_BIN
		const child = spawn(stock || process.execPath, [...(stock ? [] : [bundle]), mode, ...common, ...specific, brief], {
			cwd: workspace,
			stdio: ["ignore", "pipe", "pipe"],
			timeout: 30_000,
		})
		let output = ""
		child.stdout.on("data", (chunk) => { output += chunk })
		child.stderr.on("data", (chunk) => { output += chunk })
		const code = await new Promise((resolve, reject) => {
			child.on("error", reject)
			child.on("close", resolve)
		})
		return { code, output, requests: fixture.requests }
	} finally {
		await fixture.stop()
		await rm(directory, { recursive: true, force: true })
	}
}

test("stock Dirac launches the oversized batch; orchestration mode rejects it before fan-out", async () => {
	const [stock, limited] = await Promise.all([run("task"), run("orchestrate")])
	assert.equal(stock.code, 0, stock.output)
	assert.equal(limited.code, 0, limited.output)
	assert.equal(stock.requests, 4, stock.output) // parent, two children, parent
	assert.equal(limited.requests, 2, limited.output) // parent, rejected batch, parent
	assert.match(limited.output, /Orchestrator ledger: 0\/1 launched, 1 remaining/)
})

test("approved orchestration launches a batch that fits and counts both children", async () => {
	const runResult = await run("orchestrate", { controls: "^2 !2" })
	assert.equal(runResult.code, 0, runResult.output)
	assert.equal(runResult.requests, 4, runResult.output)
	assert.match(runResult.output, /Orchestrator ledger: 2\/2 launched, 0 remaining/)
})

test("no approval means no model calls", async () => {
	const runResult = await run("orchestrate", { approved: false })
	assert.equal(runResult.code, 1, runResult.output)
	assert.equal(runResult.requests, 0, runResult.output)
	assert.match(runResult.output, /Explicit approval is required/)
})

import assert from "node:assert/strict"
import { test } from "node:test"
import { OrchestratorSession, parseOrchestratorControls } from "../../../src/core/task/tools/subagent/OrchestratorSession.ts"

test("the leading controls are deterministic and never read from the brief", () => {
	const { limits, brief } = parseOrchestratorControls('^25 % 6 !6\nReview the example "^99 !9".')
	assert.deepEqual(limits, { cap: 25, shortfall: 6, concurrency: 6 })
	assert.equal(brief, 'Review the example "^99 !9".')
	assert.equal(new OrchestratorSession(limits).snapshot().minimum, 24)
	assert.throws(() => parseOrchestratorControls("^12 ^11 Review"), /Conflicting control/)
	assert.throws(() => parseOrchestratorControls("!11 Review"), /Out-of-range control/)
	assert.throws(() => parseOrchestratorControls("^1.5 Review"), /Invalid control/)
})

test("in-flight jobs hold capacity; failures and cancellations after launch still count", async () => {
	const session = new OrchestratorSession({ cap: 3, shortfall: 1, concurrency: 1 })
	const controller = new AbortController()
	const active = session.launch(controller.signal, () => new Promise((_resolve, reject) => {
		controller.signal.addEventListener("abort", () => reject(new Error("canceled")), { once: true })
	}))
	await assert.rejects(session.launch(undefined, async () => assert.fail("must not run")), /Concurrency limit/)
	controller.abort()
	await assert.rejects(active, /canceled/)
	await assert.rejects(session.launch(controller.signal, async () => assert.fail("must not run")), /aborted/i)
	await assert.rejects(session.launch(undefined, async () => { throw new Error("failed after launch") }), /failed after launch/)
	assert.deepEqual(session.snapshot(), { launched: 2, active: 0, remaining: 1, minimum: 3 })
	assert.throws(() => session.assertCapacity(2), /1 of 3 slots remain/)
	assert.equal(await session.launch(undefined, async () => "ok"), "ok")
	await assert.rejects(session.launch(undefined, async () => assert.fail("must not run")), /0 of 3 slots remain/)
})

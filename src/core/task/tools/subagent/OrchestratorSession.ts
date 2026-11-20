export interface OrchestratorLimits {
	cap: number
	shortfall: number
	concurrency: number
}

export function parseOrchestratorControls(input: string): { limits: OrchestratorLimits; brief: string } {
	const limits = { cap: 1, shortfall: 1, concurrency: 1 }
	const keys = { "^": "cap", "%": "shortfall", "!": "concurrency" } as const
	const seen = new Set<string>()
	// Controls belong on the first line, before the brief. Never scan the brief or quoted examples.
	const newline = input.indexOf("\n")
	const header = newline < 0 ? input : input.slice(0, newline)
	if (!/^[\s]*[\^%!]/.test(header)) return { limits, brief: input.trim() }
	const control = /\s*([\^%!])\s*(\d+)(?=\s|$)/y
	let offset = 0
	while (/^\s*[\^%!]/.test(header.slice(offset))) {
		control.lastIndex = offset
		const match = control.exec(header)
		if (!match) throw new Error("Invalid control; use ^N, %P, or !X followed by a space or end of line.")
		const marker = match[1] as keyof typeof keys
		const value = Number(match[2])
		if (value < 1 || value > (marker === "^" ? 100 : 10)) throw new Error(`Out-of-range control: ${match[0].trim()}`)
		if (seen.has(marker) && limits[keys[marker]] !== value) throw new Error(`Conflicting control: ${marker}`)
		seen.add(marker)
		limits[keys[marker]] = value
		offset = control.lastIndex
	}
	return { limits, brief: input.slice(offset).trim() }
}

export class OrchestratorSession {
	private launched = 0
	private active = 0

	readonly limits: Readonly<OrchestratorLimits>

	constructor(limits: OrchestratorLimits) {
		this.limits = Object.freeze({ ...limits })
	}

	snapshot() {
		return {
			launched: this.launched,
			active: this.active,
			remaining: this.limits.cap - this.launched,
			minimum: this.limits.cap - Math.floor((this.limits.cap * this.limits.shortfall) / 100),
		}
	}

	assertCapacity(count: number, signal?: AbortSignal) {
		signal?.throwIfAborted()
		const { remaining } = this.snapshot()
		if (count > remaining) throw new Error(`Launch cap: ${remaining} of ${this.limits.cap} slots remain; requested ${count}.`)
		const available = this.limits.concurrency - this.active
		if (count > available) throw new Error(`Concurrency limit: ${available} slots available; requested ${count}.`)
	}

	async launch<T>(signal: AbortSignal | undefined, run: () => Promise<T>): Promise<T> {
		this.assertCapacity(1, signal)
		this.launched++
		this.active++
		try {
			return await run()
		} finally {
			this.active--
		}
	}
}

// A dedicated CLI invocation owns one ledger, even if the TUI starts another task.
let session: OrchestratorSession | undefined

export function enableOrchestratorSession(limits: OrchestratorLimits): OrchestratorSession {
	if (session) throw new Error("An orchestration session is already active in this process.")
	session = new OrchestratorSession(limits)
	return session
}

export function getOrchestratorSession(): OrchestratorSession | undefined {
	return session
}

import { openAiCodexModels } from "@shared/api"
import { resolveReasoningEffortForModel } from "@shared/utils/reasoning-support"
import type { OpenaiReasoningEffort } from "@shared/storage/types"

export const ORCHESTRATOR_WORKER_MODELS = ["gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol"] as const

export function selectOrchestratorWorker(provider: string, model: unknown, effort: unknown): {
	workerModel?: string
	workerEffort?: OpenaiReasoningEffort
} {
	if (model == null && effort == null) return {}
	if (provider !== "openai-codex") throw new Error("Worker selection is available only with OpenAI Codex.")
	const selected = ORCHESTRATOR_WORKER_MODELS.find((id) => id === (model ?? "gpt-6-luna"))
	if (!selected) throw new Error("Worker model must be GPT-6 Luna, GPT-6.1 Terra, or GPT-6.2 Sol.")
	const requested = effort ?? "low"
	const info = openAiCodexModels[selected]
	if (typeof requested !== "string" || !info.reasoningEffortOptions.some((value) => value === requested) ||
		resolveReasoningEffortForModel(selected, info, requested) !== requested) {
		throw new Error(`Unsupported reasoning effort for ${selected}.`)
	}
	return { workerModel: selected, workerEffort: requested as OpenaiReasoningEffort }
}

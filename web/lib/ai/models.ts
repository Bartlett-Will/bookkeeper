// This build talks to exactly one model: a local Ollama instance (see
// lib/ai/providers.ts). There is no AI Gateway, no hosted-provider model
// catalog to fetch, and no per-model routing — PLAN.md §5.6 replaces all of
// that with a single local model chosen on tool-calling reliability grounds
// (§3.3, §5.3).

export const OLLAMA_MODEL_ID = process.env.OLLAMA_MODEL ?? "qwen3:8b";

export type ModelCapabilities = {
  tools: boolean;
  vision: boolean;
  reasoning: boolean;
};

export type ChatModel = {
  id: string;
  name: string;
  provider: string;
  description: string;
};

export const chatModels: ChatModel[] = [
  {
    description: `Local model served by Ollama at ${
      process.env.OLLAMA_BASE_URL ?? "http://localhost:11434"
    } (${OLLAMA_MODEL_ID}).`,
    id: "chat-model",
    name: `${OLLAMA_MODEL_ID} (local)`,
    provider: "ollama",
  },
];

export const DEFAULT_CHAT_MODEL = "chat-model";

export const titleModel = {
  description: "Same local model, used for chat-title generation",
  id: "title-model",
  name: chatModels[0].name,
  provider: "ollama",
};

export const allowedModelIds = new Set(chatModels.map((m) => m.id));

export const modelsByProvider = chatModels.reduce(
  (acc, model) => {
    if (!acc[model.provider]) {
      acc[model.provider] = [];
    }
    acc[model.provider].push(model);
    return acc;
  },
  {} as Record<string, ChatModel[]>
);

// qwen3:8b was chosen specifically for tool-calling reliability (PLAN.md
// §3.3, §5.3), so `tools: true` is a fact about the chosen model, not a
// guess. If OLLAMA_MODEL is overridden to something without tool support,
// update this.
const STATIC_CAPABILITIES: ModelCapabilities = {
  reasoning: false,
  tools: true,
  vision: false,
};

export function getCapabilities(): Record<string, ModelCapabilities> {
  return Object.fromEntries(
    chatModels.map((model) => [model.id, STATIC_CAPABILITIES])
  );
}

export function getActiveModels(): ChatModel[] {
  return chatModels;
}

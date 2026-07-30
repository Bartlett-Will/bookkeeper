import { customProvider } from "ai";
import { createOllama } from "ollama-ai-provider-v2";
import { isTestEnvironment } from "../constants";
import { OLLAMA_MODEL_ID } from "./models";

// Ollama's OpenAI-compatible endpoint lives at /v1, but this uses Ollama's
// native /api endpoint via a dedicated community provider instead — better
// streaming tool-call support and native option pass-through (num_ctx, seed,
// etc.) than the generic OpenAI-compatible shim. No API key: Ollama doesn't
// require one for local instances. See PLAN.md §5.3.
const OLLAMA_BASE_URL =
  process.env.OLLAMA_BASE_URL ?? "http://localhost:11434/api";

const ollama = createOllama({ baseURL: OLLAMA_BASE_URL });

function buildProvider() {
  if (isTestEnvironment) {
    // Dynamic require keeps the mock model (and its dependencies) out of
    // the production bundle.
    const { chatModel, titleModel: mockTitleModel } = require("./models.mock");
    return customProvider({
      languageModels: {
        "chat-model": chatModel,
        "title-model": mockTitleModel,
      },
    });
  }

  // Deliberately no fallback to a hosted provider here. If Ollama isn't
  // running or the model isn't pulled, calls should fail loudly rather than
  // silently routing to a paid API — this app is local-only by design.
  return customProvider({
    languageModels: {
      "chat-model": ollama(OLLAMA_MODEL_ID),
      "title-model": ollama(OLLAMA_MODEL_ID),
    },
  });
}

export const myProvider = buildProvider();

export function getLanguageModel(modelId: string) {
  return myProvider.languageModel(modelId);
}

export function getTitleModel() {
  return myProvider.languageModel("title-model");
}

// Qwen3 is a reasoning model: left at its default, it "thinks" before every
// reply, which measured ~16x slower on a simple tool-calling turn (32s vs
// 2s — see team notes). `think` is an Ollama-native /api/chat field, not
// something the OpenAI-compatible endpoint exposes, which is exactly why
// this app uses ollama-ai-provider-v2 instead of a generic baseURL swap
// (PLAN.md §5.3's "native option pass-through" requirement).
//
// This must be an explicit per-call choice, not a global default: chat
// wants it off for latency (see app/(chat)/api/chat/route.ts and
// app/(chat)/actions.ts), but Phase 3's batch categorization may want it on
// for novel merchant strings, where latency doesn't matter but reasoning
// might help accuracy.
export function withThinking(think: boolean) {
  return { ollama: { think } };
}

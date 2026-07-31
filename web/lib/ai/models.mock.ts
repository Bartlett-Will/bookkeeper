import type { LanguageModel } from "ai";

// The stand-in for Ollama under Playwright (see `providers.ts`, which swaps it
// in on `isTestEnvironment`). It exists so the e2e suite never needs a model
// loaded, and its job is to be deterministic, not clever.
//
// Every reply is one short sentence with no figures in it, which is the same
// contract `tools/bookkeeper/result.ts` holds the real model to: the numbers
// belong to the rendered card, not to the prose. A mock that recited a balance
// would let a UI regression pass e2e by making the assistant *look* like it had
// answered.

const mockResponses: Record<string, string> = {
  default: "This is a mock response for testing.",
  envelopes: "Here are your envelope balances.",
  greeting: "Hello! Ask me about your accounts, spending, or budget envelopes.",
  review: "Here is what is waiting for review.",
  spending: "Here is your spending for that period.",
  sync: "I have started a sync in the background.",
};

const mockUsage = {
  inputTokens: { cacheRead: 0, cacheWrite: 0, noCache: 10, total: 10 },
  outputTokens: { reasoning: 0, text: 20, total: 20 },
};

function getResponseForPrompt(prompt: unknown): string {
  const promptStr = JSON.stringify(prompt).toLowerCase();

  // Ordered most specific first. `sync` leads because "sync" appears in
  // messages that also mention transactions, and the more specific branch has
  // to win.
  if (promptStr.includes("sync") || promptStr.includes("refresh")) {
    return mockResponses.sync;
  }
  if (promptStr.includes("review") || promptStr.includes("uncategorized")) {
    return mockResponses.review;
  }
  if (promptStr.includes("envelope") || promptStr.includes("budget")) {
    return mockResponses.envelopes;
  }
  if (promptStr.includes("spend") || promptStr.includes("spent")) {
    return mockResponses.spending;
  }
  if (
    promptStr.includes("hello") ||
    promptStr.includes("hi") ||
    promptStr.includes("hey")
  ) {
    return mockResponses.greeting;
  }

  return mockResponses.default;
}

const createMockModel = (): LanguageModel =>
  ({
    defaultObjectGenerationMode: "tool",
    doGenerate: async ({ prompt }: { prompt: unknown }) => ({
      content: [{ text: getResponseForPrompt(prompt), type: "text" }],
      finishReason: "stop",
      usage: mockUsage,
      warnings: [],
    }),
    doStream: ({ prompt }: { prompt: unknown }) => {
      const response = getResponseForPrompt(prompt);
      const words = response.split(" ");

      return {
        stream: new ReadableStream({
          async start(controller) {
            controller.enqueue({ id: "t1", type: "text-start" });
            await words.reduce<Promise<void>>(async (previous, word) => {
              await previous;
              controller.enqueue({
                delta: `${word} `,
                id: "t1",
                type: "text-delta",
              });
              await new Promise((resolve) => {
                setTimeout(resolve, 10);
              });
            }, Promise.resolve());
            controller.enqueue({ id: "t1", type: "text-end" });
            controller.enqueue({
              finishReason: "stop",
              type: "finish",
              usage: mockUsage,
            });
            controller.close();
          },
        }),
      };
    },
    modelId: "mock-model",
    provider: "mock",
    specificationVersion: "v3",
    supportedUrls: {},
  }) as unknown as LanguageModel;

const createMockTitleModel = (): LanguageModel =>
  ({
    defaultObjectGenerationMode: "tool",
    doGenerate: async () => ({
      content: [{ text: "Test Conversation", type: "text" }],
      finishReason: "stop",
      usage: {
        inputTokens: { cacheRead: 0, cacheWrite: 0, noCache: 5, total: 5 },
        outputTokens: { reasoning: 0, text: 5, total: 5 },
      },
      warnings: [],
    }),
    doStream: () => ({
      stream: new ReadableStream({
        start(controller) {
          controller.enqueue({ id: "t1", type: "text-start" });
          controller.enqueue({
            delta: "Test Conversation",
            id: "t1",
            type: "text-delta",
          });
          controller.enqueue({ id: "t1", type: "text-end" });
          controller.enqueue({
            finishReason: "stop",
            type: "finish",
            usage: {
              inputTokens: {
                cacheRead: 0,
                cacheWrite: 0,
                noCache: 5,
                total: 5,
              },
              outputTokens: { reasoning: 0, text: 5, total: 5 },
            },
          });
          controller.close();
        },
      }),
    }),
    modelId: "mock-title-model",
    provider: "mock",
    specificationVersion: "v3",
    supportedUrls: {},
  }) as unknown as LanguageModel;

export const chatModel = createMockModel();
export const titleModel = createMockTitleModel();

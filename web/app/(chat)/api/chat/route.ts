import { ipAddress } from "@vercel/functions";
import {
  convertToModelMessages,
  createUIMessageStream,
  createUIMessageStreamResponse,
  generateId,
  isStepCount,
  streamText,
  toUIMessageStream,
} from "ai";
import { after } from "next/server";
import { createResumableStreamContext } from "resumable-stream";
import { auth, type UserType } from "@/app/(auth)/auth";
import { entitlementsByUserType } from "@/lib/ai/entitlements";
import {
  allowedModelIds,
  chatModels,
  DEFAULT_CHAT_MODEL,
  getCapabilities,
} from "@/lib/ai/models";
import { preRouteMessage } from "@/lib/ai/pre-route";
import { systemPrompt } from "@/lib/ai/prompts";
import { getLanguageModel, withThinking } from "@/lib/ai/providers";
import {
  BOOKKEEPER_TOOL_NAMES,
  bookkeeperTools,
} from "@/lib/ai/tools/bookkeeper";
import { createSidecarBookkeeperClient } from "@/lib/ai/tools/bookkeeper/sidecar-adapter";
import { createDocument } from "@/lib/ai/tools/create-document";
import { editDocument } from "@/lib/ai/tools/edit-document";
import { requestSuggestions } from "@/lib/ai/tools/request-suggestions";
import { updateDocument } from "@/lib/ai/tools/update-document";
import { isProductionEnvironment } from "@/lib/constants";
import {
  createStreamId,
  deleteChatById,
  getChatById,
  getMessageCountByUserId,
  getMessagesByChatId,
  saveChat,
  saveMessages,
  updateChatTitleById,
  updateMessage,
} from "@/lib/db/queries";
import type { DBMessage } from "@/lib/db/schema";
import { ChatbotError } from "@/lib/errors";
import { checkIpRateLimit } from "@/lib/ratelimit";
import type { ChatMessage, WaitingStatusData } from "@/lib/types";
import { convertToUIMessages, generateUUID } from "@/lib/utils";
import { generateTitleFromUserMessage } from "../../actions";
import { type PostRequestBody, postRequestBodySchema } from "./schema";

export const maxDuration = 60;

const HEALTH_CHECK_DELAY_MS = 9000;

/**
 * PLAN.md §5.6 keeps the artifacts feature but "initially unused". The four
 * artifact tools stay registered below so the feature and its UI remain wired
 * and upstream merges stay cheap, but they are not offered to the model.
 *
 * The reason is §3.3: small models are worst at deciding *which* tool to call,
 * and every inactive tool is one fewer wrong answer available. A bookkeeping
 * question that reaches `createDocument` produces a document instead of a
 * balance, which reads as a broken app rather than a wrong tool.
 *
 * Flip this to `true`, and add the names to `activeTools`, when artifacts get a
 * bookkeeping use — PLAN.md §5.6 names an editable monthly budget document.
 */
const ARTIFACT_TOOLS_ACTIVE = false;

/**
 * Every tool in this app is single-step by design (PLAN.md §5.3): each answers
 * one question in one sidecar call and returns data for React to render. So a
 * turn needs exactly two steps — the tool call, then the model's one-sentence
 * reply to its result — and `2` is the value that permits that and nothing
 * more.
 *
 * The template shipped `5`, which is actively harmful here rather than merely
 * generous. §3.3 measures ~90% per-step accuracy compounding to roughly a 40%
 * failure rate over five steps, and the failure it buys is specific: an 8B
 * model that has no further work to do will re-invoke the tool it just called
 * rather than stop, which §3.3 names as the invocation-loop failure mode. On
 * `allocate_to_envelope` — the one tool that writes — a loop is a duplicated
 * ledger entry. Capping at 2 makes that unreachable instead of unlikely.
 */
const MAX_STEPS_PER_TURN = 2;

const ARTIFACT_TOOL_NAMES = [
  "createDocument",
  "editDocument",
  "updateDocument",
  "requestSuggestions",
] as const;

const activeToolNames = ARTIFACT_TOOLS_ACTIVE
  ? [...BOOKKEEPER_TOOL_NAMES, ...ARTIFACT_TOOL_NAMES]
  : [...BOOKKEEPER_TOOL_NAMES];

function isModelStreamActivity(chunk: { type: string }) {
  return !["start", "start-step", "finish-step", "finish", "raw"].includes(
    chunk.type
  );
}

function getStreamContext() {
  try {
    return createResumableStreamContext({ waitUntil: after });
  } catch {
    return null;
  }
}

export { getStreamContext };

export async function POST(request: Request) {
  let requestBody: PostRequestBody;

  try {
    const json = await request.json();
    requestBody = postRequestBodySchema.parse(json);
  } catch {
    return new ChatbotError("bad_request:api").toResponse();
  }

  try {
    const { id, message, messages, selectedChatModel, selectedVisibilityType } =
      requestBody;

    const session = await auth();

    if (!session?.user) {
      return new ChatbotError("unauthorized:chat").toResponse();
    }

    const chatModel = allowedModelIds.has(selectedChatModel)
      ? selectedChatModel
      : DEFAULT_CHAT_MODEL;

    await checkIpRateLimit(ipAddress(request));

    const userType: UserType = session.user.type;

    const messageCount = await getMessageCountByUserId({
      differenceInHours: 1,
      id: session.user.id,
    });

    if (messageCount > entitlementsByUserType[userType].maxMessagesPerHour) {
      return new ChatbotError("rate_limit:chat").toResponse();
    }

    const isToolApprovalFlow = Boolean(messages);

    const chat = await getChatById({ id });
    let messagesFromDb: DBMessage[] = [];
    let titlePromise: Promise<string> | null = null;

    if (chat) {
      if (chat.userId !== session.user.id) {
        return new ChatbotError("forbidden:chat").toResponse();
      }
      messagesFromDb = await getMessagesByChatId({ id });
    } else if (message?.role === "user") {
      await saveChat({
        id,
        title: "New chat",
        userId: session.user.id,
        visibility: selectedVisibilityType,
      });
      titlePromise = generateTitleFromUserMessage({ message });
    }

    let uiMessages: ChatMessage[];

    if (isToolApprovalFlow && messages) {
      const dbMessages = convertToUIMessages(messagesFromDb);
      const approvalStates = new Map(
        messages.flatMap(
          (m) =>
            m.parts
              ?.filter(
                (p: Record<string, unknown>) =>
                  p.state === "approval-responded" ||
                  p.state === "output-denied"
              )
              .map((p: Record<string, unknown>) => [
                String(p.toolCallId ?? ""),
                p,
              ]) ?? []
        )
      );
      uiMessages = dbMessages.map((msg) => ({
        ...msg,
        parts: msg.parts.map((part) => {
          if (
            "toolCallId" in part &&
            approvalStates.has(String(part.toolCallId))
          ) {
            return { ...part, ...approvalStates.get(String(part.toolCallId)) };
          }
          return part;
        }),
      })) as ChatMessage[];
    } else {
      uiMessages = [
        ...convertToUIMessages(messagesFromDb),
        message as ChatMessage,
      ];
    }

    if (message?.role === "user") {
      await saveMessages({
        messages: [
          {
            attachments: [],
            chatId: id,
            createdAt: new Date(),
            id: message.id,
            parts: message.parts,
            role: "user",
          },
        ],
      });
    }

    const modelConfig = chatModels.find((m) => m.id === chatModel);
    const modelCapabilities = await getCapabilities();
    const capabilities = modelCapabilities[chatModel];
    const isReasoningModel = capabilities?.reasoning === true;
    const supportsTools = capabilities?.tools === true;

    const modelMessages = await convertToModelMessages(uiMessages);

    // Deterministic pre-routing (PLAN.md §5.3 rule 4). "sync my accounts" and
    // "show me the review queue" are commands, not questions, and we know
    // exactly what they mean — so the model is not asked to work it out.
    //
    // This forces the *choice* rather than skipping the call. Rule 4's stated
    // benefit is sidestepping "does this need a tool, and which?", which is the
    // §3.3 decision small models are worst at, and `toolChoice` removes that
    // decision completely: the tool runs, with no arguments to get wrong,
    // whatever the model would have picked. It does still cost one inference,
    // so it is not the "instant" half of rule 4 — bypassing `streamText`
    // outright would need the route to synthesise the tool call and its UI
    // parts by hand, which is a bigger change than this one is worth. Worth
    // revisiting if turn latency proves to be the thing users feel.
    const preRouted = isToolApprovalFlow ? null : preRouteMessage(message);

    const stream = createUIMessageStream({
      execute: async ({ writer: dataStream }) => {
        const modelName = modelConfig?.name ?? chatModel;
        let hasModelActivity = false;
        let healthCheckTimer: ReturnType<typeof setTimeout> | undefined;

        const clearHealthCheckTimer = () => {
          if (healthCheckTimer) {
            clearTimeout(healthCheckTimer);
          }
        };

        const writeWaitingStatus = (
          phase: WaitingStatusData["phase"],
          messageText: string
        ) => {
          if (hasModelActivity && phase !== "thinking") {
            return;
          }
          dataStream.write({
            data: {
              message: messageText,
              modelId: chatModel,
              modelName,
              phase,
            },
            transient: true,
            type: "data-waiting-status",
          });
        };

        writeWaitingStatus("waiting", "Waiting...");

        // There's no hosted gateway to ping for availability here — this is
        // a single local Ollama instance. A cold model load (mmap'ing
        // several GB of weights) can genuinely take a while, so just let
        // the user know we're still waiting rather than pretending to know
        // why.
        healthCheckTimer = setTimeout(() => {
          writeWaitingStatus(
            "still-waiting",
            `${modelName} is taking a while to respond — it may still be loading into memory...`
          );
        }, HEALTH_CHECK_DELAY_MS);

        const markModelActive = () => {
          if (hasModelActivity) {
            return;
          }
          hasModelActivity = true;
          clearHealthCheckTimer();
          writeWaitingStatus("thinking", "Thinking...");
        };

        const stopWaitingStatus = () => {
          hasModelActivity = true;
          clearHealthCheckTimer();
        };

        const result = streamText({
          activeTools: supportsTools ? activeToolNames : [],
          instructions: systemPrompt({
            includeArtifacts: supportsTools && ARTIFACT_TOOLS_ACTIVE,
          }),
          messages: modelMessages,
          model: getLanguageModel(chatModel),
          onAbort() {
            stopWaitingStatus();
          },
          onChunk({ chunk }) {
            if (isModelStreamActivity(chunk)) {
              markModelActive();
            }
          },
          onEnd() {
            stopWaitingStatus();
          },
          onError() {
            stopWaitingStatus();
          },
          // Interactive chat: thinking off. Qwen3 defaults to reasoning
          // before every reply, which measured ~16x slower on a simple
          // tool-calling turn — unusable for a chat turn. See
          // lib/ai/providers.ts#withThinking.
          providerOptions: withThinking(false),
          stopWhen: isStepCount(MAX_STEPS_PER_TURN),
          telemetry: {
            functionId: "stream-text",
            isEnabled: isProductionEnvironment,
          },
          toolChoice: preRouted
            ? { toolName: preRouted.toolName, type: "tool" }
            : "auto",
          tools: {
            // The six tools of PLAN.md §5.3 plus Phase 5's month-end report,
            // bound to the sidecar for this request. The set comes from
            // `bookkeeperTools`, so a tool is registered by being added there
            // and nothing here needs editing.
            //
            // Building them per-request rather than at module scope
            // is what lets the client carry `request.signal`: an abandoned
            // chat turn then cancels its in-flight sidecar call instead of
            // leaving the ledger service working on a result nobody will read.
            //
            // `POST /review/confirm` is deliberately absent — see
            // `CONFIRM_IS_NOT_A_TOOL` in tools/bookkeeper/index.ts. Approving
            // transactions is a button click straight to the API (§5.3 rule
            // 2), and routing it through the model here would undo the phase.
            ...bookkeeperTools(
              createSidecarBookkeeperClient({ signal: request.signal })
            ),
            createDocument: createDocument({
              dataStream,
              modelId: chatModel,
              session,
            }),
            editDocument: editDocument({ dataStream, session }),
            requestSuggestions: requestSuggestions({
              dataStream,
              modelId: chatModel,
              session,
            }),
            updateDocument: updateDocument({
              dataStream,
              modelId: chatModel,
              session,
            }),
          },
        });

        dataStream.merge(
          toUIMessageStream({
            sendReasoning: isReasoningModel,
            stream: result.stream,
          })
        );

        if (titlePromise) {
          try {
            const title = await titlePromise;
            dataStream.write({ data: title, type: "data-chat-title" });
            updateChatTitleById({ chatId: id, title });
          } catch {
            /* non-fatal */
          }
        }
      },
      generateId: generateUUID,
      onEnd: async ({ messages: finishedMessages }) => {
        if (isToolApprovalFlow) {
          await Promise.all(
            finishedMessages.map(async (finishedMsg) => {
              const existingMsg = uiMessages.find(
                (m) => m.id === finishedMsg.id
              );
              if (existingMsg) {
                await updateMessage({
                  id: finishedMsg.id,
                  parts: finishedMsg.parts,
                });
                return;
              }

              await saveMessages({
                messages: [
                  {
                    attachments: [],
                    chatId: id,
                    createdAt: new Date(),
                    id: finishedMsg.id,
                    parts: finishedMsg.parts,
                    role: finishedMsg.role,
                  },
                ],
              });
            })
          );
        } else if (finishedMessages.length > 0) {
          await saveMessages({
            messages: finishedMessages.map((currentMessage) => ({
              attachments: [],
              chatId: id,
              createdAt: new Date(),
              id: currentMessage.id,
              parts: currentMessage.parts,
              role: currentMessage.role,
            })),
          });
        }
      },
      onError: (error) => {
        if (
          error instanceof Error &&
          error.message?.includes(
            "AI Gateway requires a valid credit card on file to service requests"
          )
        ) {
          return "AI Gateway requires a valid credit card on file to service requests. Please visit https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3Dadd-credit-card to add a card and unlock your free credits.";
        }
        return "Oops, an error occurred!";
      },
      originalMessages: isToolApprovalFlow ? uiMessages : undefined,
    });

    return createUIMessageStreamResponse({
      async consumeSseStream({ stream: sseStream }) {
        if (!process.env.REDIS_URL) {
          return;
        }
        try {
          const streamContext = getStreamContext();
          if (streamContext) {
            const streamId = generateId();
            await createStreamId({ chatId: id, streamId });
            await streamContext.createNewResumableStream(
              streamId,
              () => sseStream
            );
          }
        } catch {
          /* non-critical */
        }
      },
      stream,
    });
  } catch (error) {
    const vercelId = request.headers.get("x-vercel-id");

    if (error instanceof ChatbotError) {
      return error.toResponse();
    }

    if (
      error instanceof Error &&
      error.message?.includes(
        "AI Gateway requires a valid credit card on file to service requests"
      )
    ) {
      return new ChatbotError("bad_request:activate_gateway").toResponse();
    }

    console.error("Unhandled error in chat API:", error, { vercelId });
    return new ChatbotError("offline:chat").toResponse();
  }
}

export async function DELETE(request: Request) {
  const { searchParams } = new URL(request.url);
  const id = searchParams.get("id");

  if (!id) {
    return new ChatbotError("bad_request:api").toResponse();
  }

  const session = await auth();

  if (!session?.user) {
    return new ChatbotError("unauthorized:chat").toResponse();
  }

  const chat = await getChatById({ id });

  if (chat?.userId !== session.user.id) {
    return new ChatbotError("forbidden:chat").toResponse();
  }

  const deletedChat = await deleteChatById({ id });

  return Response.json(deletedChat, { status: 200 });
}

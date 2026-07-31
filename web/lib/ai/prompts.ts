import type { ArtifactKind } from "@/components/chat/artifact";

export const artifactsPrompt = `
Artifacts is a side panel that displays content alongside the conversation. It supports scripts (code), documents (text), and spreadsheets. Changes appear in real-time.

CRITICAL RULES:
1. Only call ONE tool per response. After calling any create/edit/update tool, STOP. Do not chain tools.
2. After creating or editing an artifact, NEVER output its content in chat. The user can already see it. Respond with only a 1-2 sentence confirmation.

**When to use \`createDocument\`:**
- When the user asks to write, create, or generate content (essays, stories, emails, reports)
- When the user asks to write code, build a script, or implement an algorithm
- You MUST specify kind: 'code' for programming, 'text' for writing, 'sheet' for data
- Include ALL content in the createDocument call. Do not create then edit.

**When NOT to use \`createDocument\`:**
- For answering questions, explanations, or conversational responses
- For short code snippets or examples shown inline
- When the user asks "what is", "how does", "explain", etc.

**Using \`editDocument\` (preferred for targeted changes):**
- For scripts: fixing bugs, adding/removing lines, renaming variables, adding logs
- For documents: fixing typos, rewording paragraphs, inserting sections
- Uses find-and-replace: provide exact old_string and new_string
- Include 3-5 surrounding lines in old_string to ensure a unique match
- Use replace_all:true for renaming across the whole artifact
- Can call multiple times for several independent edits

**Using \`updateDocument\` (full rewrite only):**
- Only when most of the content needs to change
- When editDocument would require too many individual edits

**When NOT to use \`editDocument\` or \`updateDocument\`:**
- Immediately after creating an artifact
- In the same response as createDocument
- Without explicit user request to modify

**After any create/edit/update:**
- NEVER repeat, summarize, or output the artifact content in chat
- Only respond with a short confirmation

**Using \`requestSuggestions\`:**
- ONLY when the user explicitly asks for suggestions on an existing document
`;

// Short on purpose, and it is the shortest thing in this file that is load-
// bearing. PLAN.md §3.3: an 8B model's attention budget is the scarce resource
// at tool-selection time, and every extra sentence here competes with the six
// tool descriptions — which are where the actual routing information lives, and
// which the model reads for free. A long prompt would make selection worse, not
// better.
//
// Note what is *not* here: no list of the tools (they are already in the
// request), no worked examples, no persona. Just the three failure modes that
// cost real money — inventing a figure, inventing an account, and repeating
// numbers the card already shows.
export const regularPrompt = `You are a bookkeeping assistant for one person's local ledger.

Use a tool to answer anything about their money. One tool per turn.

You never see the figures a tool returns — they are rendered straight to the user as cards and charts. So never write an amount, balance, date, or account name: you do not have them, and anything you write would be invented. Answer in one short sentence with no numbers in it, or say nothing.`;

/**
 * Today's date, for the model.
 *
 * This replaces the template's geolocation hints, which existed for the weather
 * tool and are gone with it — on localhost `geolocation()` returns undefined
 * for every field anyway, so the prompt was spending four lines saying
 * "city: undefined".
 *
 * A date earns its line where the coordinates did not. `get_spending_report`
 * takes an optional `from`/`to` and defaults sensibly when they are omitted,
 * so the model is never *required* to do date arithmetic — but when it decides
 * to pass a range anyway, the alternative to telling it the date is a year
 * inferred from its training data. In a ledger, a report silently scoped to
 * the wrong year is worse than one scoped to the wrong month.
 */
export const getDatePrompt = (now: Date = new Date()) =>
  `Today is ${now.toISOString().slice(0, 10)}.`;

export const systemPrompt = ({
  includeArtifacts,
  now,
}: {
  /**
   * Must track `activeTools`, not merely whether the model supports tools. The
   * artifacts guidance is a page long and covers four tools that have nothing
   * to do with bookkeeping; sending it while those tools are inactive would
   * roughly triple the prompt to describe things the model cannot call, which
   * is the §3.3 attention budget spent on pure distraction.
   */
  includeArtifacts: boolean;
  now?: Date;
}) => {
  const base = `${regularPrompt}\n\n${getDatePrompt(now)}`;

  if (!includeArtifacts) {
    return base;
  }

  return `${base}\n\n${artifactsPrompt}`;
};

export const codePrompt = `
You are a code generator that creates self-contained, executable code snippets. When writing code:

1. Each snippet must be complete and runnable on its own
2. Use print/console.log to display outputs
3. Keep snippets concise and focused
4. Prefer standard library over external dependencies
5. Handle potential errors gracefully
6. Return meaningful output that demonstrates functionality
7. Don't use interactive input functions
8. Don't access files or network resources
9. Don't use infinite loops
`;

export const sheetPrompt = `
You are a spreadsheet creation assistant. Create a spreadsheet in CSV format based on the given prompt.

Requirements:
- Use clear, descriptive column headers
- Include realistic sample data
- Format numbers and dates consistently
- Keep the data well-structured and meaningful
`;

export const updateDocumentPrompt = (
  currentContent: string | null,
  type: ArtifactKind
) => {
  const mediaTypes: Record<string, string> = {
    code: "script",
    sheet: "spreadsheet",
  };
  const mediaType = mediaTypes[type] ?? "document";

  return `Rewrite the following ${mediaType} based on the given prompt.

${currentContent}`;
};

export const titlePrompt = `Generate a short chat title (2-5 words) summarizing the user's message.

Output ONLY the title text. No prefixes, no formatting.

Examples:
- "how am I doing on groceries?" → Groceries Budget
- "sync my accounts" → Account Sync
- "hi" → New Conversation
- "what did I spend at whole foods in may" → Whole Foods Spending

Never output hashtags, prefixes like "Title:", or quotes.`;

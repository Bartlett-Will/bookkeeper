import { randomUUID } from "node:crypto";
import type { InferSelectModel } from "drizzle-orm";
import {
  foreignKey,
  integer,
  primaryKey,
  sqliteTable,
  text,
} from "drizzle-orm/sqlite-core";

export const user = sqliteTable("User", {
  createdAt: integer("createdAt", { mode: "timestamp_ms" })
    .notNull()
    .$defaultFn(() => new Date()),
  email: text("email", { length: 64 }).notNull(),
  emailVerified: integer("emailVerified", { mode: "boolean" })
    .notNull()
    .default(false),
  id: text("id")
    .primaryKey()
    .notNull()
    .$defaultFn(() => randomUUID()),
  image: text("image"),
  isAnonymous: integer("isAnonymous", { mode: "boolean" })
    .notNull()
    .default(false),
  name: text("name"),
  password: text("password", { length: 64 }),
  updatedAt: integer("updatedAt", { mode: "timestamp_ms" })
    .notNull()
    .$defaultFn(() => new Date()),
});

export type User = InferSelectModel<typeof user>;

export const chat = sqliteTable("Chat", {
  createdAt: integer("createdAt", { mode: "timestamp_ms" }).notNull(),
  id: text("id")
    .primaryKey()
    .notNull()
    .$defaultFn(() => randomUUID()),
  title: text("title").notNull(),
  userId: text("userId")
    .notNull()
    .references(() => user.id),
  visibility: text("visibility", { enum: ["public", "private"] })
    .notNull()
    .default("private"),
});

export type Chat = InferSelectModel<typeof chat>;

export const message = sqliteTable("Message_v2", {
  attachments: text("attachments", { mode: "json" }).notNull(),
  chatId: text("chatId")
    .notNull()
    .references(() => chat.id),
  createdAt: integer("createdAt", { mode: "timestamp_ms" }).notNull(),
  id: text("id")
    .primaryKey()
    .notNull()
    .$defaultFn(() => randomUUID()),
  parts: text("parts", { mode: "json" }).notNull(),
  role: text("role").notNull(),
});

export type DBMessage = InferSelectModel<typeof message>;

export const vote = sqliteTable(
  "Vote_v2",
  {
    chatId: text("chatId")
      .notNull()
      .references(() => chat.id),
    isUpvoted: integer("isUpvoted", { mode: "boolean" }).notNull(),
    messageId: text("messageId")
      .notNull()
      .references(() => message.id),
  },
  (table) => ({
    pk: primaryKey({ columns: [table.chatId, table.messageId] }),
  })
);

export type Vote = InferSelectModel<typeof vote>;

export const document = sqliteTable(
  "Document",
  {
    content: text("content"),
    createdAt: integer("createdAt", { mode: "timestamp_ms" }).notNull(),
    id: text("id")
      .notNull()
      .$defaultFn(() => randomUUID()),
    kind: text("text", { enum: ["text", "code", "image", "sheet"] })
      .notNull()
      .default("text"),
    title: text("title").notNull(),
    userId: text("userId")
      .notNull()
      .references(() => user.id),
  },
  (table) => ({
    pk: primaryKey({ columns: [table.id, table.createdAt] }),
  })
);

export type Document = InferSelectModel<typeof document>;

export const suggestion = sqliteTable(
  "Suggestion",
  {
    createdAt: integer("createdAt", { mode: "timestamp_ms" }).notNull(),
    description: text("description"),
    documentCreatedAt: integer("documentCreatedAt", {
      mode: "timestamp_ms",
    }).notNull(),
    documentId: text("documentId").notNull(),
    id: text("id")
      .notNull()
      .$defaultFn(() => randomUUID()),
    isResolved: integer("isResolved", { mode: "boolean" })
      .notNull()
      .default(false),
    originalText: text("originalText").notNull(),
    suggestedText: text("suggestedText").notNull(),
    userId: text("userId")
      .notNull()
      .references(() => user.id),
  },
  (table) => ({
    documentRef: foreignKey({
      columns: [table.documentId, table.documentCreatedAt],
      foreignColumns: [document.id, document.createdAt],
    }),
    pk: primaryKey({ columns: [table.id] }),
  })
);

export type Suggestion = InferSelectModel<typeof suggestion>;

export const stream = sqliteTable(
  "Stream",
  {
    chatId: text("chatId").notNull(),
    createdAt: integer("createdAt", { mode: "timestamp_ms" }).notNull(),
    id: text("id")
      .notNull()
      .$defaultFn(() => randomUUID()),
  },
  (table) => ({
    chatRef: foreignKey({
      columns: [table.chatId],
      foreignColumns: [chat.id],
    }),
    pk: primaryKey({ columns: [table.id] }),
  })
);

export type Stream = InferSelectModel<typeof stream>;

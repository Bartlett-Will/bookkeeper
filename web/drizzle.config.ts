import { config } from "dotenv";
import { defineConfig } from "drizzle-kit";

config({
  path: ".env.local",
});

export default defineConfig({
  dbCredentials: {
    url: process.env.SQLITE_URL ?? "./data/local.db",
  },
  dialect: "sqlite",
  out: "./lib/db/migrations",
  schema: "./lib/db/schema.ts",
});

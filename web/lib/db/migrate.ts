import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import Database from "better-sqlite3";
import { config } from "dotenv";
import { drizzle } from "drizzle-orm/better-sqlite3";
import { migrate } from "drizzle-orm/better-sqlite3/migrator";

config({
  path: ".env.local",
});

const runMigrate = () => {
  const dbPath = process.env.SQLITE_URL ?? "./data/local.db";
  mkdirSync(dirname(dbPath), { recursive: true });

  const sqlite = new Database(dbPath);
  const db = drizzle(sqlite);

  console.log("Running migrations...");

  const start = Date.now();
  migrate(db, { migrationsFolder: "./lib/db/migrations" });
  const end = Date.now();

  console.log("Migrations completed in", end - start, "ms");
  sqlite.close();
  process.exit(0);
};

try {
  runMigrate();
} catch (err) {
  console.error("Migration failed");
  console.error(err);
  process.exit(1);
}

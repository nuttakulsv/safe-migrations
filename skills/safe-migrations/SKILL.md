---
name: safe-migrations
description: Use when writing, generating, reviewing, or running database schema migrations or data backfills (raw SQL, Prisma, Drizzle, Rails, Django, Alembic, Laravel, Knex, Ecto, Flyway, etc.), and before any command that resets, drops, truncates, or wipes a database. Rewrites operations that lock tables, rewrite tables, or break the running app version into zero-downtime, expand/contract steps for PostgreSQL and MySQL.
---

# Safe migrations

Write every migration as if it will run against production: a 50M-row table, live traffic, and the **previous app version still serving requests** while the deploy rolls out. Something that finishes instantly on a dev database can lock a production table for an hour.

## How a migration takes production down

1. **Lock queue.** DDL needs a lock. Even a lock that is held for a moment waits behind any long-running query, and every query that arrives afterwards waits behind the DDL. The site goes down even though the migration itself is "instant".
2. **Rewrite or full scan under lock.** The database rewrites or scans the whole table while it holds that lock.
3. **Schema/code mismatch.** Old code meets the new schema mid-deploy. Renames and drops break the version that is still running.
4. **Data loss.** Drops, type narrowing, ORM renames that are really drop + add, and resets.

## Workflow

1. **Find the engine and its version** in docker-compose, `schema.prisma` `provider`, `database.yml`, settings, or `SELECT version();`. The rules below depend on the version.
2. **Read the SQL that will actually run.** ORMs hide it. Generate it and read it (`prisma migrate dev --create-only`, `rails db:migrate` + `structure.sql`, `python manage.py sqlmigrate app 0042`, `alembic upgrade --sql`, `drizzle-kit generate`).
3. **Check every statement against the tables below** and rewrite the dangerous ones.
4. **One risky operation per migration.** In PostgreSQL, a transaction holds the strongest lock it has taken until it commits. In MySQL, DDL is not transactional, so a failure partway through leaves the schema half-applied.
5. **Set a lock timeout** at the top of each migration, so it fails fast instead of queueing the whole app behind it:
   - PostgreSQL: `SET lock_timeout = '5s';` (use `SET LOCAL` inside a transaction). The migration then fails cleanly and can be retried.
   - MySQL: `SET SESSION lock_wait_timeout = 5;`
6. **Split across deploys** when code and schema have to change in order (see expand/contract below). Say which deploy each step belongs to.
7. **Finish with the report** described at the end of this file.

## PostgreSQL: dangerous → safe

| Operation | Why it hurts | Do this instead |
|---|---|---|
| `CREATE INDEX` | Blocks all writes for the whole build | `CREATE INDEX CONCURRENTLY`. It cannot run inside a transaction. If it fails, it leaves an `INVALID` index behind: `DROP INDEX CONCURRENTLY` it and retry. |
| `DROP INDEX` | `ACCESS EXCLUSIVE` lock | `DROP INDEX CONCURRENTLY` |
| `REINDEX`, `VACUUM FULL`, `CLUSTER` | Rewrites the table under `ACCESS EXCLUSIVE` | `REINDEX ... CONCURRENTLY` (PG12+). Use `pg_repack` for bloat. |
| `ADD COLUMN ... DEFAULT <volatile>` (`gen_random_uuid()`, `random()`, `clock_timestamp()`), or any default on PG < 11 | Rewrites the whole table | Add the column nullable with no default, `SET DEFAULT` for new rows, then backfill in batches. Constant and `now()` defaults are metadata-only on PG11+ and safe. |
| `ALTER COLUMN ... SET NOT NULL` | Full scan under `ACCESS EXCLUSIVE` | PG12+: `ADD CONSTRAINT x CHECK (col IS NOT NULL) NOT VALID;` → next migration: `VALIDATE CONSTRAINT x;` → `ALTER COLUMN col SET NOT NULL;` (PG uses the validated check and skips the scan) → `DROP CONSTRAINT x;` |
| `ALTER COLUMN ... TYPE` (e.g. `int` → `bigint`) | Full rewrite under `ACCESS EXCLUSIVE`. Exceptions: `varchar(n)` → larger `n` or `text`. | Expand/contract: new column, dual-write, backfill, switch reads, drop the old one. |
| `ADD FOREIGN KEY` | Locks writes on **both** tables while it scans | `ADD CONSTRAINT fk ... FOREIGN KEY (...) REFERENCES ... NOT VALID;` → separate migration: `VALIDATE CONSTRAINT fk;` |
| `ADD CHECK` | Scans while holding the lock | Same as above: `NOT VALID`, then `VALIDATE CONSTRAINT` |
| `ADD UNIQUE`, `ADD PRIMARY KEY` | Builds the index under lock | `CREATE UNIQUE INDEX CONCURRENTLY idx ...;` → `ADD CONSTRAINT c UNIQUE USING INDEX idx;` (or `PRIMARY KEY USING INDEX idx`) |
| `RENAME COLUMN`, `RENAME TABLE` | Instant, but the code that is still running breaks | Expand/contract. For a table, rename it and `CREATE VIEW old_name AS SELECT * FROM new_name;` until the old code is gone. |
| `DROP COLUMN`, `DROP TABLE` | Instant, but old code still reads it, and the data is gone for good | Deploy 1: code stops reading and writing it (Rails `ignored_columns`, remove the field from the model). Deploy 2: drop it. |
| `ALTER TYPE ... ADD VALUE` (enum) | Before PG12 it cannot run in a transaction. A new value cannot be used in the same transaction that added it. | Put it in its own migration, before the code that uses it |
| `UPDATE`/`DELETE` across a big table inside the migration | One huge transaction: row locks, WAL spike, replica lag | A separate batched backfill (below), outside the schema migration |

Already safe, nothing to change: new tables, nullable columns without a default, constant defaults (PG11+), `SET DEFAULT` / `DROP DEFAULT`, `DROP NOT NULL`, growing a `varchar` or turning it into `text`, `COMMENT ON`.

## MySQL (InnoDB 8.0+)

- **Always state the algorithm:** `ALTER TABLE t ADD COLUMN c INT, ALGORITHM=INSTANT;` or `ALGORITHM=INPLACE, LOCK=NONE`. If MySQL can't do it that way, it **errors** instead of silently copying the table. That turns a hidden outage into a visible failure.
- Usually INSTANT: adding columns, setting or dropping defaults, adding ENUM values at the end. Version-dependent: drop and rename column.
- Copies the table and blocks writes: changing a column type, changing the primary key, converting the charset. Growing a `VARCHAR` is in-place only while it stays under the 256-byte length boundary, which is about 63 chars in `utf8mb4`. On big tables use `gh-ost` or `pt-online-schema-change`.
- Metadata locks queue the same way PostgreSQL locks do. One open transaction can stall the DDL and then everything behind it. Set `lock_wait_timeout`.
- Each DDL statement commits implicitly, so put one DDL statement in each migration.

## Expand / contract (renames, type changes, splitting columns)

1. **Expand:** add the new column or table, nullable. *(migration)*
2. **Dual-write:** the app writes both old and new. *(deploy)*
3. **Backfill** existing rows in batches. *(script or job)*
4. **Switch reads** to the new column. *(deploy)*
5. **Stop writing** the old one. *(deploy)*
6. **Contract:** drop the old column. *(migration, in a later release)*

Do the steps that belong in the current change. List the rest as **follow-ups the human must schedule**. Never collapse all six into one migration.

## Batched backfill

Keep it out of the schema migration. Walk the primary key in fixed ranges and commit each batch:

```sql
-- repeat with :start += 5000 until :start > max(id); sleep briefly between batches
UPDATE users SET email_lower = lower(email)
WHERE id >= :start AND id < :start + 5000 AND email_lower IS NULL;
```

Make it idempotent, so it can be killed and rerun safely. Watch replica lag on big tables.

## Running CONCURRENTLY outside a transaction, per tool

| Tool | How | Rename trap |
|---|---|---|
| Prisma | Put `CREATE INDEX CONCURRENTLY` **alone** in its own `migration.sql` (edit the file after `--create-only`) | A renamed field is generated as DROP + ADD, which **loses data**. Rewrite it as `RENAME COLUMN` or expand/contract. |
| Drizzle | Edit the generated SQL and keep the statement alone in its file | `drizzle-kit push` applies schema diffs directly. Never point it at prod. |
| Rails | `disable_ddl_transaction!` + `add_index ..., algorithm: :concurrently`; FKs: `add_foreign_key ..., validate: false` then `validate_foreign_key` | Use `ignored_columns` before dropping. The `strong_migrations` gem enforces most of this. |
| Django | `atomic = False` + `AddIndexConcurrently` (`django.contrib.postgres.operations`) | `RenameField` is a real rename, so old code breaks. Use `SeparateDatabaseAndState` for two-step drops. |
| Alembic | `with op.get_context().autocommit_block(): op.create_index(..., postgresql_concurrently=True)` | Autogenerate turns renames into drop + add. |
| Laravel | `public $withinTransaction = false;` + `DB::statement('CREATE INDEX CONCURRENTLY ...')` | |
| Knex | `exports.config = { transaction: false };` | |
| Ecto | `@disable_ddl_transaction true`, `@disable_migration_lock true`, `create index(..., concurrently: true)` | |

If [squawk](https://github.com/sbdchd/squawk) is installed, also run `squawk <file>.sql` on PostgreSQL migrations.

## Commands you never run on your own

`prisma migrate reset`, `prisma db push --force-reset|--accept-data-loss`, `drizzle-kit push --force`, `rails db:drop|reset|purge|schema:load`, `manage.py flush`, `manage.py migrate <app> zero`, `artisan migrate:fresh|reset|refresh|db:wipe`, `alembic downgrade base`, `flyway clean`, `supabase db reset`, `dropdb`, and raw `DROP` / `TRUNCATE` / `DELETE` without `WHERE` in a database client.

If one of these looks necessary, stop and tell the human exactly what it would destroy and which database it points at (check `DATABASE_URL` or the equivalent). Let them run it. Never run one against a host that isn't local.

## Report (end every migration task with this)

```
Migration safety
- <statement>: <risk> → <what you changed>          (one line per risky statement)
- Lock timeout: set / not applicable
- Follow-ups for later deploys: <expand/contract steps not done yet, or "none">
```

If a statement is dangerous and you could not make it safe, say so plainly at the top of the report.

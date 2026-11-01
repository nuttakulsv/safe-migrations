# safe-migrations

[ภาษาไทย](README.th.md)

Coding agents write migrations that run fine on localhost and then take production down. This repo is a skill that teaches them not to, plus a small guard for Claude Code that stops them from wiping your database.

## The problem

Ask an agent to "add an index on `orders.user_id` and make `users.email` required" and you'll usually get something like this:

```sql
CREATE INDEX orders_user_id_idx ON orders (user_id);
ALTER TABLE users ALTER COLUMN email SET NOT NULL;
```

Looks harmless, and on a dev database with a few hundred rows it is. On a real `orders` table, the first line blocks every insert and update until the index finishes building. The second scans all of `users` while holding a lock nothing else can get past. If a slow query happens to be running when the migration starts, everything piles up behind it and the app stops responding.

None of this shows up in tests. You find out when you deploy.

## What you get instead

Same request, with the skill installed:

```sql
-- 001 (on its own: CONCURRENTLY can't run inside a transaction)
SET lock_timeout = '5s';
CREATE INDEX CONCURRENTLY orders_user_id_idx ON orders (user_id);

-- 002
SET lock_timeout = '5s';
ALTER TABLE users ADD CONSTRAINT users_email_nn CHECK (email IS NOT NULL) NOT VALID;

-- 003: validate without blocking writes, then SET NOT NULL skips the scan
SET lock_timeout = '5s';
ALTER TABLE users VALIDATE CONSTRAINT users_email_nn;
ALTER TABLE users ALTER COLUMN email SET NOT NULL;
ALTER TABLE users DROP CONSTRAINT users_email_nn;
```

At the end the agent writes a short note on what it changed and why:

```
Migration safety
- CREATE INDEX on orders: blocks writes → CONCURRENTLY, own migration
- SET NOT NULL on users: full scan under lock → NOT VALID check + VALIDATE (PG12+)
- Lock timeout: set
- Follow-ups for later deploys: none
```

## What the skill covers

- Index builds that block writes, and doing them `CONCURRENTLY`. Also the catch: most ORMs wrap each migration in a transaction, and `CONCURRENTLY` won't run inside one.
- Adding foreign keys, check constraints, unique constraints and `NOT NULL` without holding a long lock.
- Type changes that rewrite the whole table, like `int` to `bigint`.
- `lock_timeout`, so a stuck migration gives up after a few seconds instead of freezing the site.
- Renames and drops that break the old version of the app while a deploy is rolling out. The skill splits these into expand/contract steps and tells you which ones belong to a later deploy.
- ORM "renames" that are really a drop plus an add. Prisma and Alembic both do this, and the data in that column is gone.
- Backfills that try to update millions of rows in a single transaction.
- MySQL: always writing `ALGORITHM=INSTANT` or `ALGORITHM=INPLACE, LOCK=NONE`, so MySQL throws an error instead of quietly copying the table. Also when it's time to use gh-ost.

It targets PostgreSQL 11+ and MySQL 8.0+, with specific notes for Prisma, Drizzle, Rails, Django, Alembic, Laravel, Knex and Ecto.

## The guard (Claude Code only)

The skill tells the agent what not to do. The guard actually stops it. It's a hook that looks at every shell command before it runs.

If a command can wipe data, Claude Code stops and asks you before running it. That covers `prisma migrate reset`, `rails db:drop`, `manage.py flush`, `artisan migrate:fresh`, `supabase db reset`, `dropdb`, a raw `DROP TABLE`, `TRUNCATE` or `DELETE` sent through psql, mysql or mongosh, and about 20 more.

If the command also looks like it points at production (`RAILS_ENV=production`, `supabase --linked`, an RDS, Supabase or Neon hostname), it doesn't run at all. The agent is told to hand it to you.

Everyday commands like `prisma migrate dev`, `rails db:migrate` or `grep "DROP TABLE"` run as usual.

## Install

Claude Code (skill and guard):

```
/plugin marketplace add nuttakulsv/safe-migrations
/plugin install safe-migrations@safe-migrations
```

Codex, Cursor, or anything else that reads SKILL.md (skill only):

```
npx skills add nuttakulsv/safe-migrations
```

Or copy `skills/safe-migrations/` into `~/.claude/skills/` (or your agent's skills folder) by hand.

The guard needs `python3` and nothing else.

## What it won't catch

The guard reads the command line, not the files a command runs. `psql -f cleanup.sql`, or a script the agent writes and then executes, will get past it. Think of it as a seatbelt, not a permission system. If an agent can reach your production database at all, give it read-only credentials.

There are no SQL Server, Oracle or SQLite-specific rules yet. PRs welcome.

## Tests

```
python3 hooks/guard.py --test
```

## License

MIT

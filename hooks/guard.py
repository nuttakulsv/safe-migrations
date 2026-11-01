#!/usr/bin/env python3
"""Claude Code PreToolUse hook: stop the agent from wiping a database on its own.

Destructive DB command -> ask the human first.
Destructive DB command aimed at something that looks like production -> deny.
Run `python3 guard.py --test` for the self-check.
"""
import json
import re
import sys

# Known gap: this only sees the command string, so `psql -f wipe.sql` or a script the agent wrote gets through.
DESTRUCTIVE = re.compile(r"""(?isx)
    prisma\s+migrate\s+reset
  | prisma\s+db\s+push\b.*--(force-reset|accept-data-loss)
  | drizzle-kit\s+push\b.*--force
  | \b(rails|rake)\s+db:(drop|reset|purge|schema:load|structure:load|truncate_all|migrate:reset|seed:replant)
  | \b(manage\.py|django-admin)\s+(flush|reset_db)
  | \bmanage\.py\s+migrate\s+\S+\s+zero\b
  | \bartisan\s+(migrate:(fresh|reset|refresh)|db:wipe)
  | \balembic\s+downgrade\s+base
  | \bflyway\b.*\bclean\b
  | \bliquibase\b.*\bdrop-?all\b
  | \bsupabase\s+db\s+reset
  | \bsequelize(-cli)?\s+db:(drop|migrate:undo:all)
  | \bknex\s+migrate:rollback\b.*--all
  | \btypeorm\b.*\bschema:drop
  | \bdropdb\b
  | \b(mysqladmin|mariadb-admin)\b.*\bdrop\b
  | \bpg_restore\b.*(--clean\b|(?-i:\s-c\b))
  | \bdropDatabase\s*\(
  | \bredis-cli\b.*\bflush(all|db)\b
""")

# Raw SQL only counts when a DB client is in the command (so `grep "DROP TABLE"` is fine).
DB_CLIENT = re.compile(
    r"(?i)\b(psql|pgcli|mysql|mariadb|mycli|sqlite3|sqlcmd|clickhouse-client|mongosh|mongo|dbconsole)\b"
    r"|prisma\s+db\s+execute|cockroach\s+sql|wrangler\s+d1\s+execute|turso\s+db\s+shell"
)
# Any DELETE through a client asks: judging whether a WHERE is "selective enough" is a losing game.
RAW_SQL = re.compile(r"""(?isx)
    \bdrop\s+(database|schema|table)\b
  | \btruncate\b
  | \balter\s+table\b.*\bdrop\s+column\b
  | \bdelete\s+from\b
  | \.drop\s*\(\s*\)
  | \.(deleteMany|remove)\s*\(
""")

# Letters-only boundaries so myapp_production, PROD_DB_URL and prod2 all count.
PROD = re.compile(
    r"(?i)(?<![a-z])prod(uction)?(?![a-z])|--linked\b"
    r"|\.rds\.amazonaws\.com|\.supabase\.co|\.neon\.tech|\.psdb\.cloud"
)


def check(cmd):
    cmd = cmd.replace("\\\n", " ")  # join shell line continuations
    if DESTRUCTIVE.search(cmd) or (DB_CLIENT.search(cmd) and RAW_SQL.search(cmd)):
        return "deny" if PROD.search(cmd) else "ask"
    return None


REASONS = {
    "ask": "safe-migrations: this command can wipe database data. Confirm it targets a "
           "disposable local database before allowing it.",
    "deny": "safe-migrations: blocked. This command can wipe data and looks like it targets a "
            "production or hosted database. Tell the human what it would destroy and let them "
            "run it themselves (e.g. `! <command>`).",
}


def main():
    try:
        cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "") or ""
    except (ValueError, AttributeError):
        return  # malformed input: no decision, normal permission flow applies
    decision = check(cmd)
    if decision:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": REASONS[decision],
        }}))


def test():
    cases = {
        "ask": [
            "npx prisma migrate reset --force",
            "pnpm prisma db push --accept-data-loss",
            "bin/rails db:drop",
            "bundle exec rake db:schema:load",
            "python manage.py flush --noinput",
            "python manage.py migrate billing zero",
            "php artisan migrate:fresh --seed",
            "alembic downgrade base",
            "supabase db reset",
            "dropdb myapp_dev",
            "redis-cli FLUSHALL",
            "psql $DATABASE_URL -c 'DROP TABLE users'",
            'psql -c "DELETE FROM users"',
            'mysql -e "TRUNCATE orders"',
            'psql -c "ALTER TABLE users DROP COLUMN email"',
            "mongosh --eval 'db.users.drop()'",
            "mongosh --eval 'db.users.deleteMany({})'",
            'psql -c "DELETE FROM users WHERE id = 1"',
            'psql -c "DELETE FROM users WHERE 1=1"',
            'psql -c "ALTER TABLE users\nDROP COLUMN email"',
            "npx prisma \\\n  migrate reset",
            "pnpm prisma db push \\\n  --accept-data-loss",
            "mysqladmin -u root drop app",
            "pg_restore --clean -d app dump.sql",
            "pg_restore -c -d app dump.sql",
            "bin/rails db:seed:replant",
            "dropdb reproduce_test",
        ],
        "deny": [
            "RAILS_ENV=production bin/rails db:drop",
            "supabase db reset --linked",
            "psql postgres://u@db.abc.rds.amazonaws.com/app -c 'TRUNCATE orders'",
            "psql postgres://localhost/myapp_production -c 'TRUNCATE orders'",
            "psql $PROD_DB_URL -c 'DROP TABLE users'",
            "dropdb app_prod2",
        ],
        None: [
            "npx prisma migrate dev",
            "bin/rails db:migrate RAILS_ENV=production",
            "git commit -m 'drop table support'",
            "grep -rn 'DROP TABLE' migrations/",
            'psql -c "SELECT * FROM products"',
            "pg_restore -Fc -d app dump.sql",
            "pg_restore -C -d app dump.sql",
            "python manage.py migrate",
            "drizzle-kit generate",
        ],
    }
    for want, cmds in cases.items():
        for cmd in cmds:
            got = check(cmd)
            assert got == want, f"{cmd!r}: expected {want}, got {got}"
    print(f"ok: {sum(map(len, cases.values()))} cases")


if __name__ == "__main__":
    test() if sys.argv[1:] == ["--test"] else main()

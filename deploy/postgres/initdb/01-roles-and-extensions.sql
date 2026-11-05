-- Runs once, when the Postgres data volume is first initialised.

-- Query statistics (top queries by total time, calls, rows, I/O):
--   SELECT query, calls, mean_exec_time, rows FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Guard rails at the role level. Settings here also apply to connections that
-- arrive through PgBouncer, which can't forward per-connection "options".
ALTER ROLE pricelens SET statement_timeout = '15s';
ALTER ROLE pricelens SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE pricelens SET lock_timeout = '5s';

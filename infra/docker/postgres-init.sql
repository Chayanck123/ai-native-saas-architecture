-- Database-per-service: every microservice owns its own database and schema,
-- so services can evolve (and scale) independently and never share tables.
--
-- The services connect as the unprivileged `app` role. That matters for tenant
-- isolation: PostgreSQL superusers bypass row-level security, `app` does not.

CREATE ROLE app LOGIN PASSWORD 'app' NOSUPERUSER NOBYPASSRLS;

CREATE DATABASE gateway OWNER app;
CREATE DATABASE knowledge OWNER app;
CREATE DATABASE agents OWNER app;
CREATE DATABASE billing OWNER app;
CREATE DATABASE growth OWNER app;

-- pgvector must be created by a superuser; the knowledge service then uses it.
\connect knowledge
CREATE EXTENSION IF NOT EXISTS vector;

-- Scratch database for `make test-integration` / `make smoke` (tests create and
-- drop their own tables here, away from the services' data).
CREATE DATABASE app_test OWNER app;
\connect app_test
CREATE EXTENSION IF NOT EXISTS vector;

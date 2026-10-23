-- Auto-executed by PostgreSQL on first container start.
-- Creates the test database alongside the main database.
-- The main database (staffplanner) is already created via POSTGRES_DB env var.

SELECT 'CREATE DATABASE staffplanner_test OWNER onboarding_user'
    WHERE NOT EXISTS (
        SELECT FROM pg_database WHERE datname = 'staffplanner_test'
    )\gexec


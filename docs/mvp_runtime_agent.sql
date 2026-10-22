-- REVIEW ONLY: executing this creates one application identity. Requires explicit
-- operator authorization. No runtime GRANT, credential or schema changes.
-- Execute as administrator with psql -X -v ON_ERROR_STOP=1 -d agentpal_dev.
BEGIN;
SET LOCAL ROLE agentpal_owner;
DO $$
BEGIN
  IF current_database() <> 'agentpal_dev' THEN
    RAISE EXCEPTION 'Wrong database; nothing changed';
  END IF;
  IF (SELECT array_agg(version_num::text) FROM public.alembic_version) IS DISTINCT FROM ARRAY['0006']::text[] THEN
    RAISE EXCEPTION 'Unexpected schema revision; nothing changed';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM agentpal.owners WHERE id=true) THEN
    RAISE EXCEPTION 'Owner bootstrap required; nothing changed';
  END IF;
  IF EXISTS (SELECT 1 FROM agentpal.agents WHERE owner_id=true AND name='Shopping agent' AND NOT enabled) THEN
    RAISE EXCEPTION 'Existing agent is disabled; inspect it instead of enabling implicitly';
  END IF;
  INSERT INTO agentpal.agents(name) VALUES ('Shopping agent') ON CONFLICT (owner_id,name) DO NOTHING;
END $$;
COMMIT;

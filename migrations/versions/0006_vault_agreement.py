"""Immutable provider agreement observations and explicit method replacement."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


# Preserve the 0004 guard on downgrade; committed migration files stay unchanged.
_OLD_GUARD = """CREATE OR REPLACE FUNCTION agentpal.guard_setup() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF OLD.status IN ('verified','canceled','failed')
    OR (OLD.flow_phase IS NULL AND NEW.flow_phase IS NOT NULL)
    OR (to_jsonb(NEW)-ARRAY['status','verified_at','setup_reference_ciphertext','encryption_key_id',
         'flow_phase','receipt_hash','returned_at','token_ciphertext','token_key_id']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['status','verified_at','setup_reference_ciphertext','encryption_key_id',
         'flow_phase','receipt_hash','returned_at','token_ciphertext','token_key_id'])
    OR (OLD.setup_reference_ciphertext IS NOT NULL AND
        (NEW.setup_reference_ciphertext IS DISTINCT FROM OLD.setup_reference_ciphertext OR
         NEW.encryption_key_id IS DISTINCT FROM OLD.encryption_key_id))
    OR (OLD.token_ciphertext IS NOT NULL AND
        (NEW.token_ciphertext IS DISTINCT FROM OLD.token_ciphertext OR
         NEW.token_key_id IS DISTINCT FROM OLD.token_key_id))
    OR (OLD.receipt_hash IS NOT NULL AND
        (NEW.receipt_hash IS DISTINCT FROM OLD.receipt_hash OR NEW.returned_at IS DISTINCT FROM OLD.returned_at))
  THEN RAISE EXCEPTION 'invalid Vault setup transition' USING ERRCODE='23514'; END IF;
  IF OLD.flow_phase IS NOT NULL THEN
    IF NOT ((OLD.flow_phase='setup_post' AND NEW.flow_phase IN ('awaiting_payer','review'))
      OR (OLD.flow_phase='awaiting_payer' AND NEW.flow_phase IN ('returned','canceled','expired'))
      OR (OLD.flow_phase='returned' AND NEW.flow_phase IN ('token_post','review','expired'))
      OR (OLD.flow_phase='token_post' AND NEW.flow_phase IN ('verifying_token','review'))
      OR (OLD.flow_phase='verifying_token' AND NEW.flow_phase IN ('complete','review')))
    THEN RAISE EXCEPTION 'invalid Vault flow transition' USING ERRCODE='23514'; END IF;
    IF NEW.flow_phase='complete' AND (NEW.token_ciphertext IS NULL OR NOT EXISTS
      (SELECT 1 FROM agentpal.owner_sessions WHERE id=NEW.owner_session_id
       AND revoked_at IS NULL AND expires_at>clock_timestamp()))
    THEN RAISE EXCEPTION 'Vault verification requires original active session' USING ERRCODE='23514'; END IF;
    INSERT INTO agentpal.audit_events(actor_kind,kind,details) VALUES
      ('system','vault_transition',jsonb_build_object('onboarding_id',NEW.id,
       'previous_state',OLD.flow_phase,'state',NEW.flow_phase));
  END IF;
  RETURN NEW;
END $$;"""


def upgrade() -> None:
    op.execute("""
    ALTER TABLE agentpal.vault_setups
      ADD COLUMN replaces_payment_method_id uuid REFERENCES agentpal.payment_methods(id),
      ADD COLUMN approved_reference_ciphertext bytea,
      ADD COLUMN approved_key_id text,
      ADD COLUMN agreement_evidence_ciphertext bytea,
      ADD COLUMN agreement_evidence_key_id text,
      ADD CONSTRAINT approved_material CHECK
        ((approved_reference_ciphertext IS NULL AND approved_key_id IS NULL) OR
         (approved_reference_ciphertext IS NOT NULL AND octet_length(approved_reference_ciphertext)>28
          AND approved_key_id IS NOT NULL AND length(approved_key_id)>0)),
      ADD CONSTRAINT agreement_material CHECK
        ((agreement_evidence_ciphertext IS NULL AND agreement_evidence_key_id IS NULL) OR
         (agreement_evidence_ciphertext IS NOT NULL AND octet_length(agreement_evidence_ciphertext)>28
          AND agreement_evidence_key_id IS NOT NULL AND length(agreement_evidence_key_id)>0)),
      ADD CONSTRAINT replacement_managed_flow CHECK (replaces_payment_method_id IS NULL OR flow_phase IS NOT NULL);
    """)
    op.execute(_OLD_GUARD.replace("'token_ciphertext','token_key_id']", "'token_ciphertext','token_key_id',"
        "'approved_reference_ciphertext','approved_key_id','agreement_evidence_ciphertext','agreement_evidence_key_id']"))
    op.execute("""
    CREATE FUNCTION agentpal.guard_agreement_material() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP='UPDATE' THEN
        IF (OLD.approved_reference_ciphertext IS NOT NULL AND
            (NEW.approved_reference_ciphertext IS DISTINCT FROM OLD.approved_reference_ciphertext
             OR NEW.approved_key_id IS DISTINCT FROM OLD.approved_key_id))
          OR (OLD.agreement_evidence_ciphertext IS NOT NULL AND
            (NEW.agreement_evidence_ciphertext IS DISTINCT FROM OLD.agreement_evidence_ciphertext
             OR NEW.agreement_evidence_key_id IS DISTINCT FROM OLD.agreement_evidence_key_id))
          OR (OLD.approved_reference_ciphertext IS NULL AND NEW.approved_reference_ciphertext IS NOT NULL
              AND (OLD.flow_phase='returned' AND NEW.flow_phase IN ('token_post','review')) IS NOT TRUE)
          OR (OLD.agreement_evidence_ciphertext IS NULL AND NEW.agreement_evidence_ciphertext IS NOT NULL
              AND (OLD.flow_phase='verifying_token' AND NEW.flow_phase IN ('complete','review')) IS NOT TRUE)
        THEN RAISE EXCEPTION 'agreement observations are immutable' USING ERRCODE='23514'; END IF;
      END IF;
      IF TG_OP='INSERT' AND NEW.replaces_payment_method_id IS NOT NULL
        AND NEW.flow_phase IS DISTINCT FROM 'setup_post'
      THEN RAISE EXCEPTION 'replacement must start with fresh consent' USING ERRCODE='23514'; END IF;
      IF NEW.replaces_payment_method_id IS NOT NULL AND (TG_OP='INSERT' OR NEW.flow_phase='complete')
        AND NOT EXISTS (SELECT 1 FROM agentpal.payment_methods WHERE id=NEW.replaces_payment_method_id
          AND owner_id=NEW.owner_id AND revoked_at IS NULL AND vault_setup_id<>NEW.id)
      THEN RAISE EXCEPTION 'replacement target is no longer active' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER guard_agreement_material BEFORE INSERT OR UPDATE ON agentpal.vault_setups
      FOR EACH ROW EXECUTE FUNCTION agentpal.guard_agreement_material();
    CREATE FUNCTION agentpal.lock_agreement_finances() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      PERFORM agentpal.lock_finances(); RETURN NULL;
    END $$;
    CREATE TRIGGER lock_agreement_finances BEFORE INSERT OR UPDATE ON agentpal.vault_setups
      FOR EACH STATEMENT EXECUTE FUNCTION agentpal.lock_agreement_finances();
    CREATE FUNCTION agentpal.replacement_commit_check() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF NEW.flow_phase='complete' AND NEW.replaces_payment_method_id IS NOT NULL AND
        (NOT EXISTS (SELECT 1 FROM agentpal.payment_methods WHERE id=NEW.replaces_payment_method_id AND revoked_at IS NOT NULL)
         OR NOT EXISTS (SELECT 1 FROM agentpal.payment_methods WHERE vault_setup_id=NEW.id
           AND verified_at=NEW.verified_at AND revoked_at IS NULL))
      THEN RAISE EXCEPTION 'replacement activation must be atomic' USING ERRCODE='23514'; END IF;
      RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER replacement_commit_check AFTER UPDATE ON agentpal.vault_setups
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION agentpal.replacement_commit_check();
    REVOKE ALL ON FUNCTION agentpal.guard_agreement_material(), agentpal.replacement_commit_check(),
      agentpal.lock_agreement_finances() FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM agentpal.vault_setups WHERE replaces_payment_method_id IS NOT NULL
        OR approved_reference_ciphertext IS NOT NULL OR agreement_evidence_ciphertext IS NOT NULL)
      THEN RAISE EXCEPTION 'Preserve agreement and replacement evidence; use forward migrations'; END IF;
    END $$;
    DROP TRIGGER replacement_commit_check ON agentpal.vault_setups;
    DROP FUNCTION agentpal.replacement_commit_check();
    DROP TRIGGER guard_agreement_material ON agentpal.vault_setups;
    DROP FUNCTION agentpal.guard_agreement_material();
    DROP TRIGGER lock_agreement_finances ON agentpal.vault_setups;
    DROP FUNCTION agentpal.lock_agreement_finances();
    ALTER TABLE agentpal.vault_setups DROP COLUMN replaces_payment_method_id,
      DROP COLUMN approved_reference_ciphertext, DROP COLUMN approved_key_id,
      DROP COLUMN agreement_evidence_ciphertext, DROP COLUMN agreement_evidence_key_id;
    """)
    op.execute(_OLD_GUARD)

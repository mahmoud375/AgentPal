"""Durable, single-owner Sandbox onboarding; preserve legacy Vault evidence."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE agentpal.vault_setups
      ADD COLUMN flow_phase text CHECK (flow_phase IN
        ('setup_post','awaiting_payer','returned','token_post','verifying_token','complete','review','canceled','expired')),
      ADD COLUMN flow_ciphertext bytea,
      ADD COLUMN flow_key_id text,
      ADD COLUMN setup_request_id uuid UNIQUE,
      ADD COLUMN token_request_id uuid UNIQUE,
      ADD COLUMN receipt_hash bytea CHECK (octet_length(receipt_hash)=32),
      ADD COLUMN returned_at timestamptz CHECK (isfinite(returned_at)),
      ADD COLUMN token_ciphertext bytea,
      ADD COLUMN token_key_id text,
      ADD CONSTRAINT vault_flow_material CHECK (flow_phase IS NULL OR
        (flow_ciphertext IS NOT NULL AND octet_length(flow_ciphertext)>28
         AND flow_key_id IS NOT NULL AND length(flow_key_id)>0
         AND setup_request_id IS NOT NULL AND token_request_id IS NOT NULL)),
      ADD CONSTRAINT vault_token_material CHECK
        ((token_ciphertext IS NULL AND token_key_id IS NULL) OR
         (token_ciphertext IS NOT NULL AND octet_length(token_ciphertext)>28
          AND token_key_id IS NOT NULL AND length(token_key_id)>0)),
      ADD CONSTRAINT vault_flow_status CHECK (flow_phase IS NULL OR
        ((flow_phase='complete')=(status='verified') AND
         (flow_phase='canceled')=(status='canceled') AND
         (flow_phase='expired')=(status='failed'))),
      ADD CONSTRAINT vault_return_material CHECK
        ((receipt_hash IS NULL)=(returned_at IS NULL));
    CREATE UNIQUE INDEX vault_one_open_flow ON agentpal.vault_setups(owner_id)
      WHERE flow_phase NOT IN ('complete','canceled','expired');
    CREATE OR REPLACE FUNCTION agentpal.guard_setup() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
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
    END $$;
    CREATE FUNCTION agentpal.vault_completion_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF NEW.flow_phase='complete' AND (NEW.expires_at<=clock_timestamp() OR NOT EXISTS
        (SELECT 1 FROM agentpal.owner_sessions WHERE id=NEW.owner_session_id
         AND revoked_at IS NULL AND expires_at>clock_timestamp()) OR NOT EXISTS
        (SELECT 1 FROM agentpal.payment_methods WHERE vault_setup_id=NEW.id AND verified_at=NEW.verified_at))
      THEN RAISE EXCEPTION 'Vault completion is no longer eligible' USING ERRCODE='23514'; END IF;
      RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER vault_completion_commit AFTER UPDATE ON agentpal.vault_setups
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION agentpal.vault_completion_commit();
    """)


def downgrade() -> None:
    # Older databases without managed flows can still exercise upgrade/downgrade
    # compatibility. Never remove committed onboarding/recovery evidence.
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM agentpal.vault_setups WHERE flow_phase IS NOT NULL)
      THEN RAISE EXCEPTION 'Preserve Vault evidence; use forward migrations'; END IF;
    END $$;
    DROP TRIGGER vault_completion_commit ON agentpal.vault_setups;
    DROP FUNCTION agentpal.vault_completion_commit();
    DROP INDEX agentpal.vault_one_open_flow;
    ALTER TABLE agentpal.vault_setups DROP COLUMN flow_phase, DROP COLUMN flow_ciphertext,
      DROP COLUMN flow_key_id, DROP COLUMN setup_request_id, DROP COLUMN token_request_id,
      DROP COLUMN receipt_hash, DROP COLUMN returned_at, DROP COLUMN token_ciphertext, DROP COLUMN token_key_id;
    CREATE OR REPLACE FUNCTION agentpal.guard_setup() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF OLD.status IN ('verified','canceled','failed')
        OR (to_jsonb(NEW)-ARRAY['status','verified_at','setup_reference_ciphertext','encryption_key_id']) IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['status','verified_at','setup_reference_ciphertext','encryption_key_id'])
        OR (OLD.setup_reference_ciphertext IS NOT NULL AND
            (NEW.setup_reference_ciphertext IS DISTINCT FROM OLD.setup_reference_ciphertext OR
             NEW.encryption_key_id IS DISTINCT FROM OLD.encryption_key_id))
      THEN RAISE EXCEPTION 'invalid Vault setup transition' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    """)

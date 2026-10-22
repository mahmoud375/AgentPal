"""Bind preparation evidence and close reservation/commit/release races."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


_AUTHORIZATION_SQL = """CREATE OR REPLACE FUNCTION agentpal.assert_authorized(purchase uuid) RETURNS void
LANGUAGE plpgsql AS $$ DECLARE p agentpal.purchases; pol agentpal.policies; active uuid; latest text; BEGIN
  SELECT active_policy_id INTO active FROM agentpal.owners WHERE id = true FOR UPDATE;
  SELECT * INTO STRICT p FROM agentpal.purchases WHERE id = purchase;
  SELECT * INTO STRICT pol FROM agentpal.policies WHERE id = p.policy_id;
  SELECT result INTO latest FROM agentpal.offer_revalidations WHERE purchase_id = purchase
    ORDER BY checked_at DESC, id DESC LIMIT 1;
  IF p.policy_id IS DISTINCT FROM active OR p.expires_at <= clock_timestamp()
     OR p.initial_decision = 'rejected' OR p.source_availability <> 'InStock'
     OR NOT p.merchant_domain = ANY(pol.merchant_allowlist)
     OR p.total_amount > pol.hard_purchase_limit OR latest IS DISTINCT FROM 'passed'
     OR EXISTS (SELECT 1 FROM agentpal.offer_revalidations WHERE purchase_id = purchase
                AND result NOT IN ('passed', 'source_error')) THEN
    RAISE EXCEPTION 'purchase not eligible for reservation/dispatch' USING ERRCODE = '23514';
  END IF;
  IF p.total_amount > pol.auto_approval_limit AND NOT EXISTS (
      SELECT 1 FROM agentpal.approvals WHERE purchase_id = purchase AND decision = 'approved') THEN
    RAISE EXCEPTION 'human approval required' USING ERRCODE = '23514';
  END IF;
  IF EXISTS (SELECT 1 FROM agentpal.approvals WHERE purchase_id = purchase AND decision = 'denied') THEN
    RAISE EXCEPTION 'human denied purchase' USING ERRCODE = '23514';
  END IF;
END $$;"""


def upgrade() -> None:
    # A historical awaiting label must not silently become automatic authority
    # just because its amount happens to be below the automatic threshold.
    op.execute(_AUTHORIZATION_SQL.replace(
        "IF p.total_amount > pol.auto_approval_limit AND NOT EXISTS",
        "IF (p.initial_decision='awaiting_approval' OR p.total_amount > pol.auto_approval_limit) AND NOT EXISTS"
    ).replace("AND decision = 'approved')", "AND decision = 'approved' AND owner_id=p.owner_id AND snapshot_hash=p.snapshot_hash)"))
    op.execute("""
    ALTER TABLE agentpal.payments
      ADD COLUMN preparation_revalidation_id uuid,
      ADD COLUMN preparation_check_valid_until timestamptz,
      ADD CONSTRAINT preparation_check_binding FOREIGN KEY (preparation_revalidation_id,purchase_id)
        REFERENCES agentpal.offer_revalidations(id,purchase_id),
      ADD CONSTRAINT preparation_check_deadline CHECK
        ((preparation_revalidation_id IS NULL)=(preparation_check_valid_until IS NULL)
         AND (preparation_check_valid_until IS NULL OR isfinite(preparation_check_valid_until)));

    CREATE FUNCTION agentpal.assert_definitely_unsent(payment uuid) RETURNS void LANGUAGE plpgsql AS $$
    DECLARE pay agentpal.payments; BEGIN
      PERFORM agentpal.lock_finances();
      SELECT * INTO STRICT pay FROM agentpal.payments WHERE id=payment FOR UPDATE;
      IF pay.status<>'prepared' OR pay.dispatch_started_at IS NOT NULL OR pay.provider_order_id IS NOT NULL
        OR pay.resolution_observation_id IS NOT NULL
        OR EXISTS (SELECT 1 FROM agentpal.payment_operations WHERE payment_id=pay.id AND
          (dispatch_count<>0 OR first_dispatch_at IS NOT NULL OR status<>'prepared' OR idempotency_valid_until IS NOT NULL))
        OR EXISTS (SELECT 1 FROM agentpal.provider_observations WHERE payment_id=pay.id)
        OR EXISTS (SELECT 1 FROM agentpal.payment_captures WHERE payment_id=pay.id)
      THEN RAISE EXCEPTION 'payment cannot be proven unsent' USING ERRCODE='23514'; END IF;
    END $$;

    CREATE FUNCTION agentpal.guard_unsent_release() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE pay agentpal.payments; BEGIN
      IF NEW.status='released' THEN
        PERFORM agentpal.lock_finances();
        SELECT * INTO pay FROM agentpal.payments WHERE purchase_id=NEW.purchase_id FOR UPDATE;
        IF pay.id IS NOT NULL AND pay.dispatch_started_at IS NULL THEN
          PERFORM agentpal.assert_definitely_unsent(pay.id);
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER guard_unsent_release BEFORE UPDATE ON agentpal.budget_reservations
      FOR EACH ROW EXECUTE FUNCTION agentpal.guard_unsent_release();

    CREATE FUNCTION agentpal.reservation_commit_check() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF EXISTS (SELECT 1 FROM agentpal.budget_reservations WHERE purchase_id=NEW.purchase_id AND status='held') THEN
        PERFORM agentpal.assert_authorized(NEW.purchase_id);
      END IF;
      RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER reservation_commit_check AFTER INSERT ON agentpal.budget_reservations
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION agentpal.reservation_commit_check();

    CREATE FUNCTION agentpal.preparation_commit_check() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE p agentpal.purchases; pol agentpal.policies; latest uuid; BEGIN
      IF NEW.preparation_revalidation_id IS NULL THEN RETURN NULL; END IF;
      PERFORM agentpal.assert_authorized(NEW.purchase_id);
      SELECT * INTO STRICT p FROM agentpal.purchases WHERE id=NEW.purchase_id;
      SELECT * INTO STRICT pol FROM agentpal.policies WHERE id=p.policy_id;
      SELECT id INTO latest FROM agentpal.offer_revalidations WHERE purchase_id=p.id ORDER BY checked_at DESC,id DESC LIMIT 1;
      IF NEW.preparation_check_valid_until<=clock_timestamp() OR latest<>NEW.preparation_revalidation_id
        OR NOT EXISTS (SELECT 1 FROM agentpal.offer_revalidations v WHERE v.id=NEW.preparation_revalidation_id
          AND v.result='passed' AND v.checked_at<=NEW.created_at AND v.checked_at<NEW.preparation_check_valid_until)
        OR NOT EXISTS (SELECT 1 FROM agentpal.budget_reservations r JOIN agentpal.budget_balances b ON b.id=r.period_id
          WHERE r.purchase_id=p.id AND r.status='held' AND r.amount=p.total_amount
          AND b.spent_amount+b.reserved_amount<=b.limit_amount)
        OR NOT EXISTS (SELECT 1 FROM agentpal.payment_methods m JOIN agentpal.vault_setups s ON s.id=m.vault_setup_id
          WHERE m.id=NEW.payment_method_id AND m.revoked_at IS NULL AND m.verified_at<=clock_timestamp()
            AND s.status='verified' AND s.verified_at=m.verified_at)
        OR (SELECT count(*) FROM agentpal.payment_methods WHERE owner_id=p.owner_id AND revoked_at IS NULL)<>1
        OR NEW.sandbox_payee !~ '^[2-9A-HJ-NP-Z]{13}$'
        OR NEW.sandbox_payee IS DISTINCT FROM (SELECT sandbox_business_account_id FROM agentpal.owners WHERE id=p.owner_id)
        OR (p.initial_decision='auto_authorized' AND p.total_amount>pol.auto_approval_limit)
        OR (p.initial_decision='awaiting_approval' AND NOT EXISTS
          (SELECT 1 FROM agentpal.approvals a WHERE a.purchase_id=p.id AND a.owner_id=p.owner_id
            AND a.decision='approved' AND a.snapshot_hash=p.snapshot_hash))
      THEN RAISE EXCEPTION 'financial preparation no longer eligible' USING ERRCODE='23514'; END IF;
      RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER preparation_commit_check AFTER INSERT ON agentpal.payments
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION agentpal.preparation_commit_check();

    -- Serialize method insertion/revocation with financial writers, including
    -- phantom insertions that a SHARE lock on one selected method cannot cover.
    CREATE FUNCTION agentpal.lock_method_finances() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      PERFORM agentpal.lock_finances(); RETURN NULL;
    END $$;
    CREATE TRIGGER lock_method_finances BEFORE INSERT OR UPDATE ON agentpal.payment_methods
      FOR EACH STATEMENT EXECUTE FUNCTION agentpal.lock_method_finances();
    REVOKE ALL ON FUNCTION agentpal.assert_definitely_unsent(uuid), agentpal.guard_unsent_release(),
      agentpal.reservation_commit_check(), agentpal.preparation_commit_check(), agentpal.lock_method_finances()
      FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM agentpal.payments WHERE preparation_revalidation_id IS NOT NULL)
      THEN RAISE EXCEPTION 'Preserve financial preparation evidence; use forward migrations'; END IF;
    END $$;
    DROP TRIGGER lock_method_finances ON agentpal.payment_methods;
    DROP FUNCTION agentpal.lock_method_finances();
    DROP TRIGGER preparation_commit_check ON agentpal.payments;
    DROP FUNCTION agentpal.preparation_commit_check();
    DROP TRIGGER reservation_commit_check ON agentpal.budget_reservations;
    DROP FUNCTION agentpal.reservation_commit_check();
    DROP TRIGGER guard_unsent_release ON agentpal.budget_reservations;
    DROP FUNCTION agentpal.guard_unsent_release();
    DROP FUNCTION agentpal.assert_definitely_unsent(uuid);
    ALTER TABLE agentpal.payments DROP COLUMN preparation_revalidation_id, DROP COLUMN preparation_check_valid_until;
    """)
    op.execute(_AUTHORIZATION_SQL)

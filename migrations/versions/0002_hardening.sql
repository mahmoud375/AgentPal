-- Targeted lifecycle, capture-accounting and limit-adjustment guards.

-- A single exact, linked full USD capture replaces the held amount immediately,
-- even if status/settlement commits later. Unexpected outcomes retain the full
-- held protection in addition to recorded USD spend until evidence-based closure.
CREATE OR REPLACE VIEW agentpal.budget_balances AS
SELECT b.*,
  coalesce((SELECT sum(r.amount) FROM agentpal.budget_reservations r
    WHERE r.period_id = b.id AND r.status = 'held' AND NOT EXISTS (
      SELECT 1 FROM agentpal.payments pay JOIN agentpal.purchases p ON p.id = pay.purchase_id
      WHERE pay.purchase_id = r.purchase_id AND pay.status IN ('dispatching','uncertain','completed','reconciled')
        AND (SELECT count(*) = 1 AND bool_and(c.currency = 'USD' AND c.amount = p.total_amount
              AND c.provider_order_id = pay.provider_order_id)
             FROM agentpal.payment_captures c WHERE c.payment_id = pay.id) IS TRUE)), 0) AS reserved_amount,
  coalesce((SELECT sum(c.amount) FROM agentpal.payment_captures c
    JOIN agentpal.payments p ON p.id = c.payment_id
    JOIN agentpal.budget_reservations r ON r.purchase_id = p.purchase_id
    WHERE r.period_id = b.id AND c.currency = 'USD'), 0) AS spent_amount
FROM agentpal.budget_periods b;

CREATE OR REPLACE FUNCTION agentpal.guard_payment() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE configured_payee text; expected numeric; BEGIN
  PERFORM agentpal.lock_finances();
  SELECT sandbox_business_account_id INTO configured_payee FROM agentpal.owners WHERE id = NEW.owner_id FOR UPDATE;
  IF TG_OP = 'INSERT' THEN
    IF configured_payee IS NULL OR NEW.sandbox_payee <> configured_payee
       OR NEW.status <> 'prepared' OR NEW.dispatch_started_at IS NOT NULL
       OR NOT EXISTS (SELECT 1 FROM agentpal.budget_reservations WHERE purchase_id = NEW.purchase_id AND status = 'held')
       OR NOT EXISTS (SELECT 1 FROM agentpal.payment_methods WHERE id = NEW.payment_method_id AND revoked_at IS NULL) THEN
      RAISE EXCEPTION 'payment preparation is invalid' USING ERRCODE = '23514';
    END IF;
  ELSE
    IF (to_jsonb(NEW) - ARRAY['status','provider_order_id','dispatch_started_at','resolution_observation_id']) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','provider_order_id','dispatch_started_at','resolution_observation_id'])
       OR (OLD.provider_order_id IS NOT NULL AND NEW.provider_order_id IS DISTINCT FROM OLD.provider_order_id)
       OR (OLD.dispatch_started_at IS NOT NULL AND NEW.dispatch_started_at IS DISTINCT FROM OLD.dispatch_started_at)
       OR (NEW.dispatch_started_at IS NOT NULL AND OLD.dispatch_started_at IS NULL AND NOT EXISTS
           (SELECT 1 FROM agentpal.payment_operations WHERE payment_id = NEW.id AND dispatch_count > 0)) THEN
      RAISE EXCEPTION 'payment identity cannot change' USING ERRCODE = '23514';
    END IF;
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.status IS DISTINCT FROM OLD.status AND NOT (
      (OLD.status = 'prepared' AND NEW.status IN ('dispatching','confirmed_failed','needs_review'))
      OR (OLD.status = 'dispatching' AND NEW.status IN ('uncertain','payer_action_required','completed','confirmed_failed','needs_review'))
      OR (OLD.status = 'uncertain' AND NEW.status IN ('dispatching','payer_action_required','completed','confirmed_failed','needs_review'))
      OR (OLD.status IN ('payer_action_required','needs_review') AND NEW.status IN ('completed','confirmed_failed','reconciled','needs_review'))
      OR (OLD.status IN ('completed','confirmed_failed','reconciled') AND NEW.status = 'needs_review')) THEN
    RAISE EXCEPTION 'invalid payment status transition' USING ERRCODE = '23514';
  END IF;
  IF NEW.status = 'dispatching' AND
      (TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status
       OR NEW.dispatch_started_at IS DISTINCT FROM OLD.dispatch_started_at)
      AND (NEW.dispatch_started_at IS NULL OR NOT EXISTS (
      SELECT 1 FROM agentpal.payment_operations WHERE payment_id = NEW.id AND status = 'dispatched'
        AND dispatch_count > 0)) THEN
    RAISE EXCEPTION 'dispatching requires a claimed operation' USING ERRCODE = '23514';
  END IF;
  IF NEW.status = 'uncertain' AND NEW.dispatch_started_at IS NULL THEN
    RAISE EXCEPTION 'uncertain payment must have entered dispatch' USING ERRCODE = '23514';
  END IF;
  -- Review/stopped states cannot be cleared using an old response or decision.
  IF TG_OP = 'UPDATE' AND (
      (OLD.status IN ('needs_review','payer_action_required') AND NEW.status IN ('completed','confirmed_failed'))
      OR (NEW.status = 'reconciled' AND NEW.status IS DISTINCT FROM OLD.status)) AND (
      NEW.resolution_observation_id IS NOT DISTINCT FROM OLD.resolution_observation_id
      OR NOT EXISTS (SELECT 1 FROM agentpal.provider_observations o
        WHERE o.id = NEW.resolution_observation_id AND o.payment_id = NEW.id
          AND ((o.source = 'orders_get' AND NEW.status = 'completed' AND o.outcome = 'capture')
            OR (NEW.status = 'confirmed_failed' AND o.source IN ('orders_get','manual_review') AND o.outcome = 'no_charge')
            OR (o.source = 'manual_review' AND o.outcome = 'resolved'))
          AND o.observed_at >= coalesce(NEW.dispatch_started_at, NEW.created_at)
          AND NOT EXISTS (SELECT 1 FROM agentpal.payment_captures c
            JOIN agentpal.provider_observations evidence ON evidence.id = c.observation_id
            WHERE c.payment_id = NEW.id AND evidence.observed_at > o.observed_at))) THEN
    RAISE EXCEPTION 'fresh reconciliation evidence required to close review' USING ERRCODE = '23514';
  END IF;
  IF NEW.status = 'confirmed_failed' AND (
      NOT EXISTS (SELECT 1 FROM agentpal.provider_observations
        WHERE id = NEW.resolution_observation_id AND payment_id = NEW.id AND outcome = 'no_charge'
          AND observed_at >= coalesce(NEW.dispatch_started_at, NEW.created_at))
      OR EXISTS (SELECT 1 FROM agentpal.payment_captures WHERE payment_id = NEW.id)) THEN
    RAISE EXCEPTION 'confirmed failure requires no-charge evidence' USING ERRCODE = '23514';
  END IF;
  IF NEW.status = 'completed' THEN
    SELECT total_amount INTO STRICT expected FROM agentpal.purchases WHERE id = NEW.purchase_id;
    IF (SELECT count(*) = 1 AND bool_and(currency = 'USD' AND amount = expected AND provider_order_id = NEW.provider_order_id)
            FROM agentpal.payment_captures WHERE payment_id = NEW.id) IS NOT TRUE THEN
      RAISE EXCEPTION 'completion requires exactly one full USD capture' USING ERRCODE = '23514';
    END IF;
  END IF;
  IF NEW.status = 'reconciled' AND (
      NOT EXISTS (SELECT 1 FROM agentpal.provider_observations WHERE id = NEW.resolution_observation_id
        AND payment_id = NEW.id AND source = 'manual_review' AND outcome = 'resolved')
      OR NOT EXISTS (SELECT 1 FROM agentpal.payment_captures WHERE payment_id = NEW.id)
      OR EXISTS (SELECT 1 FROM agentpal.payment_captures WHERE payment_id = NEW.id AND currency <> 'USD')) THEN
    RAISE EXCEPTION 'reconciliation requires owner resolution and known USD capture facts' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION agentpal.guard_operation() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE pay agentpal.payments; latest_check uuid; BEGIN
  PERFORM agentpal.lock_finances();
  SELECT * INTO STRICT pay FROM agentpal.payments WHERE id = NEW.payment_id FOR UPDATE;
  IF (NEW.kind = 'create_order' AND NEW.endpoint <> '/v2/checkout/orders')
     OR (NEW.kind = 'capture_order' AND
         (pay.provider_order_id IS NULL OR NEW.endpoint <> '/v2/checkout/orders/' || pay.provider_order_id || '/capture')) THEN
    RAISE EXCEPTION 'endpoint does not identify this operation' USING ERRCODE = '23514';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.dispatch_count <> 0 OR NEW.status <> 'prepared' OR NEW.first_dispatch_at IS NOT NULL
       OR NEW.idempotency_valid_until IS NOT NULL THEN
      RAISE EXCEPTION 'new operation must be prepared and unsent' USING ERRCODE = '23514';
    END IF;
  ELSE
    IF (OLD.status IN ('completed','confirmed_failed','payer_action_required') AND NEW.status <> OLD.status)
       OR (OLD.dispatch_count > 0 AND NEW.status = 'prepared')
       OR (NEW.status = 'dispatched' AND OLD.status <> 'dispatched' AND NEW.dispatch_count = OLD.dispatch_count) THEN
      RAISE EXCEPTION 'invalid operation status transition' USING ERRCODE = '23514';
    END IF;
    IF NEW.dispatch_count = OLD.dispatch_count AND NEW.status IS DISTINCT FROM OLD.status THEN
      IF OLD.status NOT IN ('dispatched','uncertain')
         OR NEW.status NOT IN ('uncertain','completed','confirmed_failed','payer_action_required') THEN
        RAISE EXCEPTION 'invalid operation status transition' USING ERRCODE = '23514';
      END IF;
      IF NEW.status <> 'uncertain' AND NOT EXISTS (
          SELECT 1 FROM agentpal.provider_observations o WHERE o.payment_id = NEW.payment_id
            AND o.operation_id = NEW.id AND o.source IN ('paypal_response','orders_get')
            AND o.observed_at >= OLD.first_dispatch_at
            AND ((NEW.status = 'completed' AND o.outcome IN ('capture','other'))
              OR (NEW.status = 'confirmed_failed' AND o.outcome = 'no_charge')
              OR (NEW.status = 'payer_action_required' AND o.outcome = 'payer_action_required'))) THEN
        RAISE EXCEPTION 'operation outcome requires bound provider evidence' USING ERRCODE = '23514';
      END IF;
    END IF;
    IF (to_jsonb(NEW) - ARRAY['dispatch_count','status','first_dispatch_at','idempotency_valid_until','dispatch_revalidation_id']) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['dispatch_count','status','first_dispatch_at','idempotency_valid_until','dispatch_revalidation_id'])
       OR (OLD.dispatch_count > 0 AND NEW.dispatch_revalidation_id IS DISTINCT FROM OLD.dispatch_revalidation_id) THEN
      RAISE EXCEPTION 'operation identity and payload are immutable' USING ERRCODE = '23514';
    END IF;
    IF NEW.dispatch_count <> OLD.dispatch_count THEN
      IF NEW.dispatch_count <> OLD.dispatch_count + 1
         OR OLD.status IN ('completed', 'confirmed_failed', 'payer_action_required')
         OR pay.status IN ('completed', 'confirmed_failed', 'payer_action_required', 'needs_review', 'reconciled') THEN
        RAISE EXCEPTION 'invalid dispatch claim' USING ERRCODE = '23514';
      END IF;
      IF EXISTS (SELECT 1 FROM agentpal.payment_captures WHERE payment_id = NEW.payment_id)
         OR EXISTS (SELECT 1 FROM agentpal.payment_operations WHERE payment_id = NEW.payment_id
                    AND id <> NEW.id AND status IN ('dispatched','uncertain','payer_action_required'))
         OR (OLD.dispatch_count = 0 AND NEW.kind = 'capture_order' AND NOT EXISTS (
             SELECT 1 FROM agentpal.payment_operations WHERE payment_id = NEW.payment_id
               AND kind = 'create_order' AND status = 'completed')) THEN
        RAISE EXCEPTION 'capture evidence or unresolved operation blocks further execution' USING ERRCODE = '23514';
      END IF;
      IF OLD.dispatch_count = 0 THEN
        PERFORM agentpal.assert_authorized(pay.purchase_id);
        IF NOT EXISTS (SELECT 1 FROM agentpal.budget_reservations r
            JOIN agentpal.budget_balances b ON b.id=r.period_id
            WHERE r.purchase_id=pay.purchase_id AND r.status='held'
              AND b.reserved_amount+b.spent_amount <= b.limit_amount) THEN
          RAISE EXCEPTION 'first dispatch requires a held, within-limit reservation' USING ERRCODE = '23514';
        END IF;
        SELECT id INTO latest_check FROM agentpal.offer_revalidations WHERE purchase_id = pay.purchase_id
          ORDER BY checked_at DESC, id DESC LIMIT 1;
        IF NEW.dispatch_revalidation_id IS DISTINCT FROM latest_check OR NOT EXISTS (
            SELECT 1 FROM agentpal.offer_revalidations WHERE id = NEW.dispatch_revalidation_id
              AND purchase_id = pay.purchase_id AND result = 'passed' AND checked_at >= OLD.created_at)
           OR NOT EXISTS (SELECT 1 FROM agentpal.payment_methods WHERE id = pay.payment_method_id AND revoked_at IS NULL) THEN
          RAISE EXCEPTION 'fresh operation-bound listing check and active method required' USING ERRCODE = '23514';
        END IF;
        NEW.first_dispatch_at := clock_timestamp();
        NEW.idempotency_valid_until := NEW.first_dispatch_at + NEW.replay_window_seconds * interval '1 second';
      ELSE
        IF OLD.status <> 'uncertain' OR pay.status <> 'uncertain' THEN
          RAISE EXCEPTION 'an in-flight operation cannot be claimed concurrently' USING ERRCODE = '23514';
        END IF;
        IF OLD.idempotency_valid_until IS NULL OR clock_timestamp() >= OLD.idempotency_valid_until
           OR NEW.first_dispatch_at IS DISTINCT FROM OLD.first_dispatch_at
           OR NEW.idempotency_valid_until IS DISTINCT FROM OLD.idempotency_valid_until THEN
          RAISE EXCEPTION 'safe replay window unavailable' USING ERRCODE = '23514';
        END IF;
      END IF;
      NEW.status := 'dispatched';
    ELSIF NEW.first_dispatch_at IS DISTINCT FROM OLD.first_dispatch_at
       OR NEW.idempotency_valid_until IS DISTINCT FROM OLD.idempotency_valid_until THEN
      RAISE EXCEPTION 'dispatch timing is immutable' USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION agentpal.record_capture() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE pay agentpal.payments; p agentpal.purchases; BEGIN
  PERFORM agentpal.lock_finances();
  SELECT * INTO STRICT pay FROM agentpal.payments WHERE id = NEW.payment_id FOR UPDATE;
  PERFORM 1 FROM agentpal.budget_periods b JOIN agentpal.budget_reservations r ON r.period_id = b.id
    WHERE r.purchase_id = pay.purchase_id FOR UPDATE OF b;
  SELECT * INTO STRICT p FROM agentpal.purchases WHERE id = pay.purchase_id;
  IF pay.dispatch_started_at IS NULL OR NEW.currency <> 'USD' OR NEW.amount <> p.total_amount
     OR NEW.provider_order_id IS DISTINCT FROM pay.provider_order_id
     OR pay.status IN ('confirmed_failed', 'reconciled')
     OR EXISTS (SELECT 1 FROM agentpal.budget_reservations WHERE purchase_id = pay.purchase_id AND status = 'released')
     OR (SELECT count(*) FROM agentpal.payment_captures WHERE payment_id = NEW.payment_id) > 1 THEN
    UPDATE agentpal.payments SET status = 'needs_review' WHERE id = NEW.payment_id;
  END IF;
  INSERT INTO agentpal.audit_events(purchase_id, payment_id, actor_kind, kind, details)
    VALUES (pay.purchase_id, pay.id, 'system', 'capture_recorded',
      jsonb_build_object('capture_id', NEW.provider_capture_id, 'amount', NEW.amount, 'currency', NEW.currency));
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION agentpal.guard_period() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'period history cannot be deleted' USING ERRCODE = '23514'; END IF;
  PERFORM agentpal.lock_finances();
  IF (to_jsonb(NEW) - ARRAY['limit_amount','starts_at','ends_at']) IS DISTINCT FROM
     (to_jsonb(OLD) - ARRAY['limit_amount','starts_at','ends_at']) THEN
    RAISE EXCEPTION 'period identity and initial limit are immutable' USING ERRCODE = '23514';
  END IF;
  IF NEW.limit_amount IS DISTINCT FROM OLD.limit_amount THEN
    IF NEW.limit_amount < OLD.limit_amount AND NEW.limit_amount < (
        SELECT reserved_amount + spent_amount FROM agentpal.budget_balances WHERE id = OLD.id) THEN
      RAISE EXCEPTION 'budget limit cannot fall below spending and outstanding protection' USING ERRCODE = '23514';
    END IF;
    INSERT INTO agentpal.audit_events(actor_kind, kind, details) VALUES ('system', 'period_limit_changed',
      jsonb_build_object('period_id', OLD.id, 'old_limit', OLD.limit_amount, 'new_limit', NEW.limit_amount));
  END IF;
  RETURN NEW;
END $$;

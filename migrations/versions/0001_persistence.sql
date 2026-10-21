CREATE SCHEMA agentpal;

-- No typmod: NUMERIC(p, 2) rounds before a CHECK can reject fractional cents.
CREATE DOMAIN agentpal.usd_amount AS numeric
  CHECK (VALUE >= 0 AND VALUE < 'Infinity'::numeric
         AND VALUE <> 'NaN'::numeric AND scale(VALUE) <= 2);

CREATE FUNCTION agentpal.valid_domain(value text) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT length(value) <= 253 AND value = lower(value)
    AND value ~ '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'
$$;
CREATE FUNCTION agentpal.valid_allowlist(domains text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT array_position(domains, NULL) IS NULL
    AND NOT EXISTS (SELECT 1 FROM unnest(domains) AS d WHERE NOT agentpal.valid_domain(d))
    AND cardinality(domains) = (SELECT count(DISTINCT d) FROM unnest(domains) AS d)
$$;

CREATE TABLE agentpal.owners (
  id boolean PRIMARY KEY DEFAULT true CHECK (id),
  login_name text NOT NULL UNIQUE CHECK (length(btrim(login_name)) > 0),
  password_hash text NOT NULL CHECK (password_hash LIKE '$argon2id$%'),
  active_policy_id uuid,
  sandbox_business_account_id text CHECK (length(btrim(sandbox_business_account_id)) > 0),
  finance_revision bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE agentpal.owner_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id boolean NOT NULL DEFAULT true REFERENCES agentpal.owners(id),
  token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
  csrf_token_hash bytea NOT NULL CHECK (octet_length(csrf_token_hash) = 32),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  UNIQUE (id, owner_id),
  CHECK (expires_at > created_at),
  CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);
CREATE INDEX owner_sessions_expiry_idx ON agentpal.owner_sessions(expires_at)
  WHERE revoked_at IS NULL;
CREATE TABLE agentpal.agents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id boolean NOT NULL DEFAULT true REFERENCES agentpal.owners(id),
  name text NOT NULL CHECK (length(btrim(name)) > 0),
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (id, owner_id), UNIQUE (owner_id, name)
);
CREATE TABLE agentpal.policies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id boolean NOT NULL DEFAULT true REFERENCES agentpal.owners(id),
  monthly_limit agentpal.usd_amount NOT NULL,
  auto_approval_limit agentpal.usd_amount NOT NULL,
  hard_purchase_limit agentpal.usd_amount NOT NULL,
  purchase_ttl_seconds bigint NOT NULL CHECK (purchase_ttl_seconds > 0),
  merchant_allowlist text[] NOT NULL CHECK (agentpal.valid_allowlist(merchant_allowlist)),
  created_by_session_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (id, owner_id),
  FOREIGN KEY (created_by_session_id, owner_id) REFERENCES agentpal.owner_sessions(id, owner_id),
  CHECK (auto_approval_limit <= hard_purchase_limit)
);
ALTER TABLE agentpal.owners ADD CONSTRAINT owner_active_policy_fk
  FOREIGN KEY (active_policy_id, id) REFERENCES agentpal.policies(id, owner_id);

CREATE TABLE agentpal.budget_periods (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id boolean NOT NULL DEFAULT true REFERENCES agentpal.owners(id),
  month_start date NOT NULL CHECK (isfinite(month_start) AND extract(day FROM month_start) = 1),
  starts_at timestamptz GENERATED ALWAYS AS
    (month_start::timestamp AT TIME ZONE 'UTC') STORED,
  ends_at timestamptz GENERATED ALWAYS AS
    ((month_start + interval '1 month') AT TIME ZONE 'UTC') STORED,
  initial_policy_id uuid NOT NULL,
  initial_limit agentpal.usd_amount NOT NULL,
  limit_amount agentpal.usd_amount NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (owner_id, month_start), UNIQUE (id, owner_id),
  FOREIGN KEY (initial_policy_id, owner_id) REFERENCES agentpal.policies(id, owner_id)
);

CREATE TABLE agentpal.purchases (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id boolean NOT NULL DEFAULT true REFERENCES agentpal.owners(id),
  agent_id uuid NOT NULL,
  origin_ref_hash bytea NOT NULL UNIQUE CHECK (octet_length(origin_ref_hash) = 32),
  policy_id uuid NOT NULL,
  provider text NOT NULL DEFAULT 'channel3' CHECK (provider = 'channel3'),
  product_id text NOT NULL CHECK (length(btrim(product_id)) > 0),
  merchant_domain text NOT NULL CHECK (agentpal.valid_domain(merchant_domain)),
  title text NOT NULL,
  url_fingerprint bytea NOT NULL CHECK (octet_length(url_fingerprint) = 32),
  source_condition text,
  matching_version text NOT NULL CHECK (length(btrim(matching_version)) > 0),
  matching_offer_count integer NOT NULL CHECK (matching_offer_count = 1),
  unit_price agentpal.usd_amount NOT NULL,
  quantity bigint NOT NULL CHECK (quantity > 0),
  total_amount agentpal.usd_amount GENERATED ALWAYS AS (unit_price::numeric * quantity) STORED,
  currency text NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
  source_availability text NOT NULL,
  source_retrieved_at timestamptz NOT NULL,
  initial_decision text NOT NULL CHECK
    (initial_decision IN ('rejected', 'awaiting_approval', 'auto_authorized')),
  reason_codes text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  snapshot_hash_version smallint NOT NULL DEFAULT 1 CHECK (snapshot_hash_version = 1),
  snapshot_hash bytea NOT NULL CHECK (octet_length(snapshot_hash) = 32),
  UNIQUE (id, owner_id),
  FOREIGN KEY (agent_id, owner_id) REFERENCES agentpal.agents(id, owner_id),
  FOREIGN KEY (policy_id, owner_id) REFERENCES agentpal.policies(id, owner_id),
  CHECK (source_retrieved_at <= created_at), CHECK (expires_at > created_at)
);
CREATE INDEX purchases_agent_idx ON agentpal.purchases(agent_id);
CREATE INDEX purchases_policy_idx ON agentpal.purchases(policy_id);
CREATE INDEX purchases_created_idx ON agentpal.purchases(created_at DESC);
CREATE INDEX purchases_expiry_idx ON agentpal.purchases(expires_at);

CREATE TABLE agentpal.approvals (
  purchase_id uuid PRIMARY KEY,
  owner_id boolean NOT NULL DEFAULT true,
  owner_session_id uuid NOT NULL,
  snapshot_hash bytea NOT NULL CHECK (octet_length(snapshot_hash) = 32),
  decision text NOT NULL CHECK (decision IN ('approved', 'denied')),
  decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (purchase_id, owner_id) REFERENCES agentpal.purchases(id, owner_id),
  FOREIGN KEY (owner_session_id, owner_id) REFERENCES agentpal.owner_sessions(id, owner_id)
);
CREATE INDEX approvals_session_idx ON agentpal.approvals(owner_session_id);

CREATE TABLE agentpal.vault_setups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id boolean NOT NULL DEFAULT true REFERENCES agentpal.owners(id),
  owner_session_id uuid NOT NULL,
  state_hash bytea NOT NULL UNIQUE CHECK (octet_length(state_hash) = 32),
  setup_reference_ciphertext bytea,
  encryption_key_id text CHECK (length(btrim(encryption_key_id)) > 0),
  status text NOT NULL DEFAULT 'created' CHECK
    (status IN ('created', 'pending', 'verified', 'canceled', 'failed')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  verified_at timestamptz,
  UNIQUE (id, owner_id),
  FOREIGN KEY (owner_session_id, owner_id) REFERENCES agentpal.owner_sessions(id, owner_id),
  CHECK (expires_at > created_at),
  CHECK ((setup_reference_ciphertext IS NULL) = (encryption_key_id IS NULL)),
  CHECK (setup_reference_ciphertext IS NULL OR octet_length(setup_reference_ciphertext) > 0),
  CHECK ((status = 'verified') = (verified_at IS NOT NULL))
);
CREATE TABLE agentpal.payment_methods (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id boolean NOT NULL DEFAULT true REFERENCES agentpal.owners(id),
  vault_setup_id uuid NOT NULL UNIQUE,
  reference_ciphertext bytea NOT NULL CHECK (octet_length(reference_ciphertext) > 0),
  encryption_key_id text NOT NULL CHECK (length(btrim(encryption_key_id)) > 0),
  verified_at timestamptz NOT NULL,
  revoked_at timestamptz,
  UNIQUE (id, owner_id),
  FOREIGN KEY (vault_setup_id, owner_id) REFERENCES agentpal.vault_setups(id, owner_id),
  CHECK (revoked_at IS NULL OR revoked_at >= verified_at)
);

CREATE TABLE agentpal.offer_revalidations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  purchase_id uuid NOT NULL REFERENCES agentpal.purchases(id),
  checked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  result text NOT NULL CHECK (result IN
    ('passed', 'identity_changed', 'price_changed', 'unavailable', 'ambiguous', 'unsupported', 'missing', 'source_error')),
  product_id text,
  merchant_domain text,
  url_fingerprint bytea CHECK (octet_length(url_fingerprint) = 32),
  source_condition text,
  matching_offer_count integer CHECK (matching_offer_count >= 0),
  observed_price numeric,
  currency text,
  availability text,
  reason_code text,
  UNIQUE (id, purchase_id)
);
CREATE INDEX revalidations_purchase_time_idx ON agentpal.offer_revalidations(purchase_id, checked_at DESC);

CREATE TABLE agentpal.budget_reservations (
  purchase_id uuid PRIMARY KEY REFERENCES agentpal.purchases(id),
  period_id uuid NOT NULL REFERENCES agentpal.budget_periods(id),
  amount agentpal.usd_amount NOT NULL,
  status text NOT NULL DEFAULT 'held' CHECK (status IN ('held', 'released', 'settled')),
  reserved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  resolved_at timestamptz,
  CHECK ((status = 'held') = (resolved_at IS NULL))
);
CREATE INDEX reservations_period_idx ON agentpal.budget_reservations(period_id);

CREATE TABLE agentpal.payments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  purchase_id uuid NOT NULL UNIQUE REFERENCES agentpal.budget_reservations(purchase_id),
  owner_id boolean NOT NULL DEFAULT true,
  payment_method_id uuid NOT NULL,
  sandbox_payee text NOT NULL CHECK (length(btrim(sandbox_payee)) > 0),
  provider_order_id text UNIQUE,
  status text NOT NULL DEFAULT 'prepared' CHECK (status IN
    ('prepared', 'dispatching', 'uncertain', 'payer_action_required', 'completed', 'confirmed_failed', 'needs_review', 'reconciled')),
  dispatch_started_at timestamptz,
  resolution_observation_id uuid,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (id, purchase_id),
  FOREIGN KEY (purchase_id, owner_id) REFERENCES agentpal.purchases(id, owner_id),
  FOREIGN KEY (payment_method_id, owner_id) REFERENCES agentpal.payment_methods(id, owner_id)
);
CREATE INDEX payments_method_idx ON agentpal.payments(payment_method_id);
CREATE INDEX payments_unresolved_idx ON agentpal.payments(status)
  WHERE status IN ('dispatching', 'uncertain', 'payer_action_required', 'needs_review');

CREATE TABLE agentpal.payment_operations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  payment_id uuid NOT NULL REFERENCES agentpal.payments(id),
  kind text NOT NULL CHECK (kind IN ('create_order', 'capture_order')),
  endpoint text NOT NULL CHECK (endpoint LIKE '/v2/checkout/orders%'),
  request_id text NOT NULL CHECK (length(btrim(request_id)) > 0),
  request_payload_ciphertext bytea NOT NULL CHECK (octet_length(request_payload_ciphertext) > 0),
  encryption_key_id text NOT NULL CHECK (length(btrim(encryption_key_id)) > 0),
  request_body_sha256 bytea NOT NULL CHECK (octet_length(request_body_sha256) = 32),
  dispatch_revalidation_id uuid REFERENCES agentpal.offer_revalidations(id),
  replay_window_seconds bigint CHECK (replay_window_seconds > 0),
  retention_documentation text CHECK (length(btrim(retention_documentation)) > 0),
  first_dispatch_at timestamptz,
  idempotency_valid_until timestamptz,
  dispatch_count integer NOT NULL DEFAULT 0 CHECK (dispatch_count >= 0),
  status text NOT NULL DEFAULT 'prepared' CHECK (status IN
    ('prepared', 'dispatched', 'uncertain', 'completed', 'confirmed_failed', 'payer_action_required')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (payment_id, kind), UNIQUE (endpoint, request_id), UNIQUE (id, payment_id),
  CHECK ((replay_window_seconds IS NULL) = (retention_documentation IS NULL)),
  CHECK ((dispatch_count = 0) = (first_dispatch_at IS NULL)),
  CHECK (idempotency_valid_until IS NULL OR idempotency_valid_until > first_dispatch_at)
);
CREATE TABLE agentpal.provider_observations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  payment_id uuid NOT NULL REFERENCES agentpal.payments(id),
  operation_id uuid,
  source text NOT NULL CHECK (source IN ('paypal_response', 'orders_get', 'manual_review')),
  outcome text NOT NULL CHECK (outcome IN ('unknown', 'no_charge', 'capture', 'payer_action_required', 'other', 'resolved')),
  reviewed_by_session_id uuid REFERENCES agentpal.owner_sessions(id),
  provider_status text,
  facts jsonb NOT NULL CHECK (jsonb_typeof(facts) = 'object'),
  observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((source = 'manual_review') = (reviewed_by_session_id IS NOT NULL)),
  CHECK (outcome <> 'resolved' OR (source = 'manual_review'
    AND (facts -> 'no_further_charge') IS NOT DISTINCT FROM to_jsonb(true))),
  UNIQUE (id, payment_id),
  FOREIGN KEY (operation_id, payment_id) REFERENCES agentpal.payment_operations(id, payment_id)
);
CREATE INDEX observations_payment_idx ON agentpal.provider_observations(payment_id, observed_at);
ALTER TABLE agentpal.payments ADD CONSTRAINT payment_resolution_observation_fk
  FOREIGN KEY (resolution_observation_id, id) REFERENCES agentpal.provider_observations(id, payment_id);

-- Canonical confirmed captures; non-final/unknown facts belong in observations.
-- Actual provider evidence deliberately permits unexpected currencies/precision.
CREATE TABLE agentpal.payment_captures (
  provider_capture_id text PRIMARY KEY CHECK (length(btrim(provider_capture_id)) > 0),
  payment_id uuid NOT NULL REFERENCES agentpal.payments(id),
  observation_id uuid NOT NULL,
  provider_order_id text NOT NULL,
  amount numeric NOT NULL CHECK (amount >= 0 AND amount < 'Infinity'::numeric AND amount <> 'NaN'::numeric),
  currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  captured_at timestamptz NOT NULL,
  FOREIGN KEY (observation_id, payment_id) REFERENCES agentpal.provider_observations(id, payment_id)
);
CREATE INDEX captures_payment_idx ON agentpal.payment_captures(payment_id);

CREATE TABLE agentpal.audit_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purchase_id uuid REFERENCES agentpal.purchases(id),
  payment_id uuid REFERENCES agentpal.payments(id),
  actor_kind text NOT NULL CHECK (actor_kind IN ('system', 'owner', 'agent')),
  owner_session_id uuid REFERENCES agentpal.owner_sessions(id),
  agent_id uuid REFERENCES agentpal.agents(id),
  kind text NOT NULL CHECK (length(btrim(kind)) > 0),
  details jsonb NOT NULL CHECK (jsonb_typeof(details) = 'object'),
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (payment_id, purchase_id) REFERENCES agentpal.payments(id, purchase_id),
  CHECK ((actor_kind = 'owner' AND owner_session_id IS NOT NULL AND agent_id IS NULL)
      OR (actor_kind = 'agent' AND agent_id IS NOT NULL AND owner_session_id IS NULL)
      OR (actor_kind = 'system' AND owner_session_id IS NULL AND agent_id IS NULL))
);
CREATE INDEX audit_purchase_idx ON agentpal.audit_events(purchase_id, occurred_at);
CREATE INDEX audit_payment_idx ON agentpal.audit_events(payment_id, occurred_at);

CREATE VIEW agentpal.budget_balances AS
SELECT b.*,
  coalesce((SELECT sum(r.amount) FROM agentpal.budget_reservations r
            WHERE r.period_id = b.id AND r.status = 'held'), 0) AS reserved_amount,
  coalesce((SELECT sum(c.amount) FROM agentpal.payment_captures c
            JOIN agentpal.payments p ON p.id = c.payment_id
            JOIN agentpal.budget_reservations r ON r.purchase_id = p.purchase_id
            WHERE r.period_id = b.id AND c.currency = 'USD'), 0) AS spent_amount
FROM agentpal.budget_periods b;

-- A real row UPDATE also causes serialization failure under repeatable-read
-- rather than allowing an aggregate check against an obsolete MVCC snapshot.
CREATE FUNCTION agentpal.lock_finances() RETURNS void LANGUAGE sql AS $$
  UPDATE agentpal.owners SET finance_revision = finance_revision + 1 WHERE id = true
$$;

CREATE FUNCTION agentpal.immutable_record() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  RAISE EXCEPTION '% records are immutable', TG_TABLE_NAME USING ERRCODE = '23514';
END $$;
DO $$ DECLARE table_name text; BEGIN
  FOREACH table_name IN ARRAY ARRAY['policies', 'purchases', 'approvals', 'offer_revalidations',
      'provider_observations', 'payment_captures', 'audit_events'] LOOP
    EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON agentpal.%I
                    FOR EACH ROW EXECUTE FUNCTION agentpal.immutable_record()', table_name);
  END LOOP;
END $$;

CREATE FUNCTION agentpal.prepare_purchase() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE ttl bigint; active uuid; BEGIN
  SELECT active_policy_id INTO active FROM agentpal.owners WHERE id = NEW.owner_id FOR UPDATE;
  IF NEW.policy_id IS DISTINCT FROM active THEN
    RAISE EXCEPTION 'proposal requires current policy' USING ERRCODE = '23514';
  END IF;
  SELECT purchase_ttl_seconds INTO STRICT ttl FROM agentpal.policies WHERE id = NEW.policy_id;
  NEW.expires_at := NEW.created_at + ttl * interval '1 second';
  NEW.snapshot_hash := sha256(convert_to(((to_jsonb(NEW) - ARRAY[
      'snapshot_hash','total_amount','created_at','expires_at','source_retrieved_at']) ||
      jsonb_build_object('created_epoch',extract(epoch FROM NEW.created_at),
                        'expires_epoch',extract(epoch FROM NEW.expires_at),
                        'source_epoch',extract(epoch FROM NEW.source_retrieved_at)))::text, 'UTF8'));
  RETURN NEW;
END $$;
CREATE TRIGGER prepare_purchase BEFORE INSERT ON agentpal.purchases
  FOR EACH ROW EXECUTE FUNCTION agentpal.prepare_purchase();

CREATE FUNCTION agentpal.validate_approval() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE p agentpal.purchases; s agentpal.owner_sessions; active uuid; BEGIN
  SELECT active_policy_id INTO active FROM agentpal.owners WHERE id = NEW.owner_id FOR UPDATE;
  SELECT * INTO STRICT p FROM agentpal.purchases WHERE id = NEW.purchase_id;
  SELECT * INTO STRICT s FROM agentpal.owner_sessions WHERE id = NEW.owner_session_id FOR SHARE;
  IF p.initial_decision <> 'awaiting_approval' OR p.policy_id IS DISTINCT FROM active
     OR p.expires_at <= clock_timestamp() OR NEW.snapshot_hash <> p.snapshot_hash
     OR s.owner_id <> NEW.owner_id OR s.revoked_at IS NOT NULL
     OR s.expires_at <= clock_timestamp() OR NEW.decided_at < s.created_at
     OR EXISTS (SELECT 1 FROM agentpal.payments WHERE purchase_id = p.id AND dispatch_started_at IS NOT NULL) THEN
    RAISE EXCEPTION 'invalid human approval binding or session' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER validate_approval BEFORE INSERT ON agentpal.approvals
  FOR EACH ROW EXECUTE FUNCTION agentpal.validate_approval();

CREATE FUNCTION agentpal.validate_revalidation() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE p agentpal.purchases; BEGIN
  SELECT * INTO STRICT p FROM agentpal.purchases WHERE id = NEW.purchase_id;
  IF NEW.checked_at < p.created_at THEN RAISE EXCEPTION 'check predates proposal' USING ERRCODE = '23514'; END IF;
  IF NEW.result = 'passed' AND (
      NEW.product_id IS DISTINCT FROM p.product_id
      OR NEW.merchant_domain IS DISTINCT FROM p.merchant_domain
      OR NEW.url_fingerprint IS DISTINCT FROM p.url_fingerprint
      OR NEW.source_condition IS DISTINCT FROM p.source_condition
      OR NEW.matching_offer_count IS DISTINCT FROM 1
      OR NEW.observed_price IS DISTINCT FROM p.unit_price::numeric
      OR NEW.currency IS DISTINCT FROM 'USD' OR NEW.availability IS DISTINCT FROM 'InStock') THEN
    RAISE EXCEPTION 'passed revalidation differs from snapshot' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER validate_revalidation BEFORE INSERT ON agentpal.offer_revalidations
  FOR EACH ROW EXECUTE FUNCTION agentpal.validate_revalidation();

-- This checks stored evidence, never contacts a provider or authenticates HTTP.
CREATE FUNCTION agentpal.assert_authorized(purchase uuid) RETURNS void
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
END $$;

CREATE FUNCTION agentpal.ensure_current_budget_period() RETURNS uuid
LANGUAGE plpgsql AS $$ DECLARE pol agentpal.policies; result uuid; period_month date; BEGIN
  PERFORM agentpal.lock_finances();
  SELECT p.* INTO STRICT pol FROM agentpal.owners o JOIN agentpal.policies p ON p.id = o.active_policy_id
    WHERE o.id = true FOR UPDATE OF o;
  period_month := date_trunc('month', clock_timestamp() AT TIME ZONE 'UTC')::date;
  INSERT INTO agentpal.budget_periods(owner_id, month_start, initial_policy_id, initial_limit, limit_amount)
    VALUES (true, period_month,
            pol.id, pol.monthly_limit, pol.monthly_limit)
    ON CONFLICT (owner_id, month_start) DO NOTHING;
  SELECT id INTO STRICT result FROM agentpal.budget_periods
    WHERE owner_id = true AND month_start = period_month;
  RETURN result;
END $$;

CREATE FUNCTION agentpal.guard_reservation() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE p agentpal.purchases; b agentpal.budget_balances; pay agentpal.payments; BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'reservation history cannot be deleted' USING ERRCODE = '23514'; END IF;
  IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - ARRAY['status','resolved_at']) IS DISTINCT FROM
      (to_jsonb(OLD) - ARRAY['status','resolved_at']) THEN
    RAISE EXCEPTION 'reservation identity is immutable' USING ERRCODE = '23514';
  END IF;
  -- One owner lock serializes financial writers, including unexpected captures.
  PERFORM agentpal.lock_finances();
  SELECT * INTO STRICT p FROM agentpal.purchases WHERE id = NEW.purchase_id;
  PERFORM 1 FROM agentpal.budget_periods WHERE id = NEW.period_id FOR UPDATE;
  SELECT * INTO b FROM agentpal.budget_balances WHERE id = NEW.period_id;
  IF b.id IS NULL THEN RAISE EXCEPTION 'unknown budget period' USING ERRCODE = '23503'; END IF;
  IF TG_OP = 'INSERT' THEN
    PERFORM agentpal.assert_authorized(NEW.purchase_id);
    NEW.reserved_at := clock_timestamp();
    IF NEW.status <> 'held' OR NEW.amount <> p.total_amount OR b.owner_id <> p.owner_id
       OR NEW.reserved_at < b.starts_at OR NEW.reserved_at >= b.ends_at
       OR b.reserved_amount + b.spent_amount + NEW.amount > b.limit_amount THEN
      RAISE EXCEPTION 'invalid or insufficient budget reservation' USING ERRCODE = '23514';
    END IF;
  ELSE
    IF (to_jsonb(NEW) - 'status' - 'resolved_at') IS DISTINCT FROM
       (to_jsonb(OLD) - 'status' - 'resolved_at') OR OLD.status <> 'held'
       OR NEW.status NOT IN ('released', 'settled') THEN
      RAISE EXCEPTION 'invalid reservation transition' USING ERRCODE = '23514';
    END IF;
    SELECT * INTO pay FROM agentpal.payments WHERE purchase_id = NEW.purchase_id FOR UPDATE;
    IF NEW.status = 'released' AND pay.dispatch_started_at IS NOT NULL AND pay.status <> 'confirmed_failed' THEN
      RAISE EXCEPTION 'possibly dispatched reservation must remain held' USING ERRCODE = '23514';
    END IF;
    IF NEW.status = 'settled' AND (pay.id IS NULL OR pay.status NOT IN ('completed', 'reconciled')) THEN
      RAISE EXCEPTION 'settlement requires confirmed full capture' USING ERRCODE = '23514';
    END IF;
    NEW.resolved_at := clock_timestamp();
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_reservation BEFORE INSERT OR UPDATE OR DELETE ON agentpal.budget_reservations
  FOR EACH ROW EXECUTE FUNCTION agentpal.guard_reservation();

CREATE FUNCTION agentpal.guard_payment() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE configured_payee text; expected numeric; BEGIN
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
  IF NEW.status = 'confirmed_failed' AND (
      NOT EXISTS (SELECT 1 FROM agentpal.provider_observations
        WHERE id = NEW.resolution_observation_id AND payment_id = NEW.id AND outcome = 'no_charge')
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
CREATE TRIGGER guard_payment BEFORE INSERT OR UPDATE ON agentpal.payments
  FOR EACH ROW EXECUTE FUNCTION agentpal.guard_payment();
CREATE TRIGGER immutable_payment_delete BEFORE DELETE ON agentpal.payments
  FOR EACH ROW EXECUTE FUNCTION agentpal.immutable_record();

CREATE FUNCTION agentpal.guard_operation() RETURNS trigger
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
CREATE TRIGGER guard_operation BEFORE INSERT OR UPDATE ON agentpal.payment_operations
  FOR EACH ROW EXECUTE FUNCTION agentpal.guard_operation();
CREATE TRIGGER immutable_operation_delete BEFORE DELETE ON agentpal.payment_operations
  FOR EACH ROW EXECUTE FUNCTION agentpal.immutable_record();
CREATE FUNCTION agentpal.record_dispatch() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  IF NEW.dispatch_count > OLD.dispatch_count THEN
    UPDATE agentpal.payments SET dispatch_started_at = coalesce(dispatch_started_at, NEW.first_dispatch_at),
      status = 'dispatching' WHERE id = NEW.payment_id;
    INSERT INTO agentpal.audit_events(payment_id, actor_kind, kind, details)
      VALUES (NEW.payment_id, 'system', 'payment_dispatch_claimed', jsonb_build_object('operation_id', NEW.id, 'dispatch_count', NEW.dispatch_count));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER record_dispatch AFTER UPDATE ON agentpal.payment_operations
  FOR EACH ROW EXECUTE FUNCTION agentpal.record_dispatch();

CREATE FUNCTION agentpal.record_capture() RETURNS trigger
LANGUAGE plpgsql AS $$ DECLARE pay agentpal.payments; p agentpal.purchases; BEGIN
  PERFORM agentpal.lock_finances();
  SELECT * INTO STRICT pay FROM agentpal.payments WHERE id = NEW.payment_id FOR UPDATE;
  PERFORM 1 FROM agentpal.budget_periods b JOIN agentpal.budget_reservations r ON r.period_id = b.id
    WHERE r.purchase_id = pay.purchase_id FOR UPDATE OF b;
  SELECT * INTO STRICT p FROM agentpal.purchases WHERE id = pay.purchase_id;
  IF NEW.currency <> 'USD' OR NEW.amount <> p.total_amount
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
CREATE TRIGGER record_capture AFTER INSERT ON agentpal.payment_captures
  FOR EACH ROW EXECUTE FUNCTION agentpal.record_capture();

CREATE FUNCTION agentpal.audit_reservation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  INSERT INTO agentpal.audit_events(purchase_id, actor_kind, kind, details)
    VALUES (NEW.purchase_id, 'system', 'budget_' || NEW.status,
      jsonb_build_object('period_id', NEW.period_id, 'amount', NEW.amount));
  RETURN NEW;
END $$;
CREATE TRIGGER audit_reservation AFTER INSERT OR UPDATE ON agentpal.budget_reservations
  FOR EACH ROW EXECUTE FUNCTION agentpal.audit_reservation();

CREATE FUNCTION agentpal.guard_period() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'period history cannot be deleted' USING ERRCODE = '23514'; END IF;
  PERFORM 1 FROM agentpal.owners WHERE id = true FOR UPDATE;
  IF (to_jsonb(NEW) - ARRAY['limit_amount','starts_at','ends_at']) IS DISTINCT FROM
     (to_jsonb(OLD) - ARRAY['limit_amount','starts_at','ends_at']) THEN
    RAISE EXCEPTION 'period identity and initial limit are immutable' USING ERRCODE = '23514';
  END IF;
  IF NEW.limit_amount IS DISTINCT FROM OLD.limit_amount THEN
    INSERT INTO agentpal.audit_events(actor_kind, kind, details) VALUES ('system', 'period_limit_changed',
      jsonb_build_object('period_id', OLD.id, 'old_limit', OLD.limit_amount, 'new_limit', NEW.limit_amount));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_period BEFORE UPDATE OR DELETE ON agentpal.budget_periods
  FOR EACH ROW EXECUTE FUNCTION agentpal.guard_period();

CREATE FUNCTION agentpal.guard_payment_method() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
  IF TG_OP = 'UPDATE' AND (OLD.revoked_at IS NOT NULL OR
      (to_jsonb(NEW) - 'revoked_at') IS DISTINCT FROM (to_jsonb(OLD) - 'revoked_at')) THEN
    RAISE EXCEPTION 'vaulted method is immutable except revocation' USING ERRCODE = '23514';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM agentpal.vault_setups WHERE id = NEW.vault_setup_id
                 AND status = 'verified' AND verified_at = NEW.verified_at) THEN
    RAISE EXCEPTION 'verified Vault completion required' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_payment_method BEFORE INSERT OR UPDATE ON agentpal.payment_methods
  FOR EACH ROW EXECUTE FUNCTION agentpal.guard_payment_method();
CREATE TRIGGER immutable_method_delete BEFORE DELETE ON agentpal.payment_methods
  FOR EACH ROW EXECUTE FUNCTION agentpal.immutable_record();

CREATE FUNCTION agentpal.guard_owner() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF NEW.sandbox_business_account_id IS DISTINCT FROM OLD.sandbox_business_account_id
      AND EXISTS (SELECT 1 FROM agentpal.payments) THEN
    RAISE EXCEPTION 'MVP Sandbox recipient cannot change after payment preparation' USING ERRCODE = '23514';
  END IF;
  IF NEW.active_policy_id IS DISTINCT FROM OLD.active_policy_id THEN
    INSERT INTO agentpal.audit_events(actor_kind,kind,details) VALUES ('system','active_policy_changed',
      jsonb_build_object('previous_policy_id',OLD.active_policy_id,'policy_id',NEW.active_policy_id));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_owner BEFORE UPDATE ON agentpal.owners
  FOR EACH ROW EXECUTE FUNCTION agentpal.guard_owner();

CREATE FUNCTION agentpal.guard_session() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF OLD.revoked_at IS NOT NULL OR (to_jsonb(NEW) - 'revoked_at') IS DISTINCT FROM (to_jsonb(OLD) - 'revoked_at') THEN
    RAISE EXCEPTION 'session identity is immutable; create a new session' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_session BEFORE UPDATE ON agentpal.owner_sessions
  FOR EACH ROW EXECUTE FUNCTION agentpal.guard_session();

CREATE FUNCTION agentpal.guard_setup() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF OLD.status IN ('verified','canceled','failed')
     OR (to_jsonb(NEW) - ARRAY['status','verified_at','setup_reference_ciphertext','encryption_key_id']) IS DISTINCT FROM
        (to_jsonb(OLD) - ARRAY['status','verified_at','setup_reference_ciphertext','encryption_key_id'])
     OR (OLD.setup_reference_ciphertext IS NOT NULL AND
         (NEW.setup_reference_ciphertext IS DISTINCT FROM OLD.setup_reference_ciphertext
          OR NEW.encryption_key_id IS DISTINCT FROM OLD.encryption_key_id)) THEN
    RAISE EXCEPTION 'invalid Vault setup transition' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_setup BEFORE UPDATE ON agentpal.vault_setups
  FOR EACH ROW EXECUTE FUNCTION agentpal.guard_setup();
CREATE TRIGGER immutable_setup_delete BEFORE DELETE ON agentpal.vault_setups
  FOR EACH ROW EXECUTE FUNCTION agentpal.immutable_record();

CREATE FUNCTION agentpal.audit_purchase() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  INSERT INTO agentpal.audit_events(purchase_id,actor_kind,agent_id,kind,details)
    VALUES (NEW.id,'agent',NEW.agent_id,'proposal_recorded',jsonb_build_object(
      'decision',NEW.initial_decision,'reason_codes',NEW.reason_codes,
      'policy_id',NEW.policy_id,'snapshot_hash',encode(NEW.snapshot_hash,'hex')));
  RETURN NEW;
END $$;
CREATE TRIGGER audit_purchase AFTER INSERT ON agentpal.purchases
  FOR EACH ROW EXECUTE FUNCTION agentpal.audit_purchase();

CREATE FUNCTION agentpal.audit_approval() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  INSERT INTO agentpal.audit_events(purchase_id,actor_kind,owner_session_id,kind,details)
    VALUES (NEW.purchase_id,'owner',NEW.owner_session_id,'human_decision_recorded',jsonb_build_object('decision',NEW.decision));
  RETURN NEW;
END $$;
CREATE TRIGGER audit_approval AFTER INSERT ON agentpal.approvals
  FOR EACH ROW EXECUTE FUNCTION agentpal.audit_approval();

CREATE FUNCTION agentpal.guard_manual_observation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF NEW.source = 'manual_review' AND NOT EXISTS (SELECT 1 FROM agentpal.owner_sessions
      WHERE id = NEW.reviewed_by_session_id AND revoked_at IS NULL AND expires_at > clock_timestamp()) THEN
    RAISE EXCEPTION 'manual review requires an active owner session' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guard_manual_observation BEFORE INSERT ON agentpal.provider_observations
  FOR EACH ROW EXECUTE FUNCTION agentpal.guard_manual_observation();
CREATE FUNCTION agentpal.audit_manual_observation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF NEW.source = 'manual_review' THEN
    INSERT INTO agentpal.audit_events(payment_id,actor_kind,owner_session_id,kind,details)
      VALUES (NEW.payment_id,'owner',NEW.reviewed_by_session_id,'provider_manual_review',
              jsonb_build_object('observation_id',NEW.id,'outcome',NEW.outcome));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER audit_manual_observation AFTER INSERT ON agentpal.provider_observations
  FOR EACH ROW EXECUTE FUNCTION agentpal.audit_manual_observation();

-- Restrict accidental PUBLIC access. Use a separate non-owner application role;
-- the migration owner necessarily remains able to change its own schema.
DO $$ DECLARE col record; BEGIN
  FOR col IN SELECT c.table_name,c.column_name FROM information_schema.columns c
      JOIN information_schema.tables t USING (table_schema,table_name)
      WHERE c.table_schema='agentpal' AND t.table_type='BASE TABLE'
        AND c.data_type='timestamp with time zone' LOOP
    EXECUTE format('ALTER TABLE agentpal.%I ADD CONSTRAINT %I CHECK (%I IS NULL OR isfinite(%I))',
      col.table_name,'finite_' || col.column_name,col.column_name,col.column_name);
  END LOOP;
END $$;
REVOKE ALL ON SCHEMA agentpal FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA agentpal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA agentpal FROM PUBLIC;

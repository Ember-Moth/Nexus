CREATE FUNCTION billing.v1_reserve_item(rid uuid, org uuid, project uuid, service uuid, kid uuid,
 model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb,
 at_time timestamptz, node uuid, cluster uuid, rev bigint, batch boolean)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE wallet billing.accounts; prior billing.request_runs; sale billing.price_versions;
 discount billing.discount_versions; over_budget boolean;
 r numeric; matched uuid[];
BEGIN
 IF inputs IS NULL OR outputs IS NULL OR inputs NOT BETWEEN 0 AND 1000000000
 OR outputs NOT BETWEEN 0 AND 1000000000 OR ttl NOT BETWEEN 1 AND (CASE WHEN batch THEN 90000 ELSE 86400 END) THEN
  RAISE EXCEPTION 'invalid_bounds'; END IF;
 -- Serialize gateway admissions for this wallet before contending on its tuple.
 -- Other writers still coordinate on the authoritative account row.
 IF node IS NOT NULL THEN
  PERFORM pg_advisory_xact_lock(hashtextextended(org::text,398221));
 END IF;
 SELECT * INTO wallet FROM billing.accounts WHERE organization_id=org FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'account_not_ready'; END IF;
 IF NOT EXISTS(SELECT FROM billing.settings WHERE id=1 AND currency=wallet.currency) THEN
  RAISE EXCEPTION 'currency_config_mismatch'; END IF;
 SELECT * INTO prior FROM billing.request_runs WHERE id=rid;
 IF FOUND THEN
  IF prior.request_digest<>fingerprint OR prior.account_id<>wallet.id OR prior.node_id IS DISTINCT FROM node THEN
   RAISE EXCEPTION 'request_conflict'; END IF;
  RETURN rid;
 END IF;
 IF wallet.status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 PERFORM revision FROM billing.source_revision WHERE id=1 FOR SHARE;
 IF node IS NOT NULL THEN at_time=clock_timestamp(); END IF;
 SELECT * INTO sale FROM billing.price_versions WHERE kind='sale' AND scope_id=model
  AND state='published' AND effective_at<=at_time ORDER BY effective_at DESC LIMIT 1;
 IF NOT FOUND OR NOT sale.enabled OR sale.model_id<>model OR sale.currency<>wallet.currency
 OR NOT (sale.rates ? 'input_uncached_tokens' AND sale.rates ? 'output_tokens')
 OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(sale.rates) k)
 IS DISTINCT FROM (SELECT array_agg(k ORDER BY k) FROM jsonb_array_elements_text(dimensions) k)
 THEN RAISE EXCEPTION 'price_unavailable'; END IF;
 SELECT * INTO STRICT discount FROM billing.discount_versions
 WHERE (organization_id=org OR organization_id IS NULL) AND state='published' AND effective_at<=at_time
 ORDER BY organization_id NULLS LAST,effective_at DESC LIMIT 1;
 SELECT ceil((inputs*max(rate) FILTER(WHERE dim<>'output_tokens')+
               outputs*max(rate) FILTER(WHERE dim='output_tokens'))*discount.factor*(CASE WHEN batch THEN 0.5 ELSE 1 END)*1000000000000)
               *0.000000000001 INTO r FROM
 (SELECT key dim,(value->>'unit_price')::numeric *
  CASE (value->>'unit_quantity')::integer WHEN 1 THEN 1 WHEN 1000 THEN 0.001
  WHEN 1000000 THEN 0.000001 END rate FROM jsonb_each(sale.rates)) rates;
 IF r IS NULL OR r>=10000000000000000 THEN RAISE EXCEPTION 'invalid_reserve_amount'; END IF;
 IF wallet.balance-wallet.held_amount<r THEN RAISE EXCEPTION 'insufficient_funds'; END IF;
 -- The wallet lock serializes all changes to this organization's budget policies,
 -- periods and holds. Reserve every matching period in one write and reject the
 -- whole transaction (including newly created periods) if any limit is exceeded.
 WITH policies AS (
   SELECT id,version,limit_amount,period,date_trunc(period,at_time) AS start_at
   FROM billing.budget_policies
   WHERE organization_id=org AND status='active' AND
     (scope_type='organization' OR (scope_type='project' AND project_id=project)
      OR (scope_type='key' AND api_key_id=kid))
 ), reserved_periods AS (
   INSERT INTO billing.budget_periods AS bp
     (id,organization_id,policy_id,policy_version,period_start,period_end,limit_amount,reserved)
   SELECT gen_random_uuid(),org,id,version,start_at,
     start_at+CASE period WHEN 'day' THEN interval '1 day' ELSE interval '1 month' END,
     limit_amount,r FROM policies ORDER BY id
   ON CONFLICT(policy_id,period_start) DO UPDATE SET
     policy_version=excluded.policy_version,limit_amount=excluded.limit_amount,
     reserved=bp.reserved+excluded.reserved
   RETURNING bp.id,bp.spent,bp.reserved,bp.limit_amount
 )
 SELECT array_agg(id),bool_or(spent+reserved>limit_amount)
 INTO matched,over_budget FROM reserved_periods;
 IF over_budget THEN RAISE EXCEPTION 'budget_exceeded'; END IF;
 INSERT INTO billing.request_runs(id,organization_id,account_id,project_id,service_account_id,api_key_id,
 model_id,request_digest,sale_price_version_id,discount_version_id,billing_policy_version,input_limit,
 output_limit,reserved_amount,expires_at,created_at,node_id,cluster_id,config_revision)
 VALUES(rid,org,wallet.id,project,service,kid,model,fingerprint,sale.id,discount.id,CASE WHEN batch THEN 'anthropic_message_batch_v1' ELSE 'customer_usage_v1' END,
 inputs,outputs,r,at_time+make_interval(secs=>ttl),at_time,node,cluster,rev);
 IF matched IS NOT NULL THEN
   INSERT INTO billing.budget_holds(request_id,period_id,organization_id,reserved,spent)
   SELECT rid,period_id,org,r,0 FROM unnest(matched) AS period_id;
 END IF;
 UPDATE billing.accounts SET held_amount=held_amount+r,version=version+1 WHERE id=wallet.id;
 INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),
 org,'request.reserved',rid,jsonb_build_object('amount',r::text,'sale_price_version_id',sale.id,'discount_version_id',discount.id));
 RETURN rid;
END $$;

CREATE OR REPLACE FUNCTION billing.v1_reserve(rid uuid, org uuid, project uuid, service uuid, kid uuid,
 model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb,
 at_time timestamptz, node uuid, cluster uuid, rev bigint) RETURNS uuid LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$
 SELECT billing.v1_reserve_item(rid,org,project,service,kid,model,inputs,outputs,ttl,fingerprint,dimensions,at_time,node,cluster,rev,false)
$$;

CREATE FUNCTION billing.v1_batch_pricing(rid uuid,node uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT billing_policy_version='anthropic_message_batch_v1' FROM billing.request_runs WHERE id=rid AND node_id=node
$$;

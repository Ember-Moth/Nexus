CREATE FUNCTION billing.v1_reserve(rid uuid, org uuid, project uuid, service uuid, kid uuid,
 model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb,
 at_time timestamptz, node uuid, cluster uuid, rev bigint)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE wallet billing.accounts; prior billing.request_runs; sale billing.price_versions;
 discount billing.discount_versions; policy billing.budget_policies; period billing.budget_periods;
 r numeric; start_at timestamptz; end_at timestamptz; matched uuid[] := '{}'; pid uuid;
BEGIN
 IF inputs IS NULL OR outputs IS NULL OR inputs NOT BETWEEN 0 AND 1000000000
 OR outputs NOT BETWEEN 0 AND 1000000000 OR ttl NOT BETWEEN 1 AND 86400 THEN
  RAISE EXCEPTION 'invalid_bounds'; END IF;
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
               outputs*max(rate) FILTER(WHERE dim='output_tokens'))*discount.factor*1000000000000)
               *0.000000000001 INTO r FROM
 (SELECT key dim,(value->>'unit_price')::numeric *
  CASE (value->>'unit_quantity')::integer WHEN 1 THEN 1 WHEN 1000 THEN 0.001
  WHEN 1000000 THEN 0.000001 END rate FROM jsonb_each(sale.rates)) rates;
 IF r IS NULL OR r>=10000000000000000 THEN RAISE EXCEPTION 'invalid_reserve_amount'; END IF;
 IF wallet.balance-wallet.held_amount<r THEN RAISE EXCEPTION 'insufficient_funds'; END IF;
 FOR policy IN SELECT * FROM billing.budget_policies WHERE organization_id=org AND status='active'
   AND ((scope_type='organization') OR (scope_type='project' AND project_id=project)
   OR (scope_type='key' AND api_key_id=kid)) ORDER BY id LOOP
  start_at=date_trunc(policy.period,at_time);
  end_at=start_at+CASE policy.period WHEN 'day' THEN interval '1 day' ELSE interval '1 month' END;
  INSERT INTO billing.budget_periods(id,organization_id,policy_id,policy_version,period_start,period_end,limit_amount)
   VALUES(gen_random_uuid(),org,policy.id,policy.version,start_at,end_at,policy.limit_amount)
   ON CONFLICT(policy_id,period_start) DO UPDATE SET policy_version=excluded.policy_version,
   limit_amount=excluded.limit_amount RETURNING * INTO period;
  IF period.spent+period.reserved+r>period.limit_amount THEN RAISE EXCEPTION 'budget_exceeded'; END IF;
  matched=array_append(matched,period.id);
 END LOOP;
 INSERT INTO billing.request_runs(id,organization_id,account_id,project_id,service_account_id,api_key_id,
 model_id,request_digest,sale_price_version_id,discount_version_id,billing_policy_version,input_limit,
 output_limit,reserved_amount,expires_at,created_at,node_id,cluster_id,config_revision)
 VALUES(rid,org,wallet.id,project,service,kid,model,fingerprint,sale.id,discount.id,'customer_usage_v1',
 inputs,outputs,r,at_time+make_interval(secs=>ttl),at_time,node,cluster,rev);
 FOREACH pid IN ARRAY matched LOOP
  UPDATE billing.budget_periods SET reserved=reserved+r WHERE id=pid;
  INSERT INTO billing.budget_holds(request_id,period_id,organization_id,reserved,spent) VALUES(rid,pid,org,r,0);
 END LOOP;
 UPDATE billing.accounts SET held_amount=held_amount+r,version=version+1 WHERE id=wallet.id;
 INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),
 org,'request.reserved',rid,jsonb_build_object('amount',r::text,'sale_price_version_id',sale.id,'discount_version_id',discount.id));
 RETURN rid;
END $$;

CREATE FUNCTION billing.v1_attempt(org uuid,rid uuid,aid uuid,deployment uuid, target_model uuid,
 seq integer,at_time timestamptz,node uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE wallet billing.accounts; req billing.request_runs; prior billing.request_attempts;
 cost billing.price_versions; sale billing.price_versions;
BEGIN
 SELECT * INTO wallet FROM billing.accounts WHERE organization_id=org FOR UPDATE;
 SELECT * INTO req FROM billing.request_runs WHERE id=rid AND organization_id=org FOR UPDATE;
 IF NOT FOUND OR req.node_id IS DISTINCT FROM node THEN RAISE EXCEPTION 'not_found'; END IF;
 SELECT * INTO prior FROM billing.request_attempts WHERE id=aid;
 IF FOUND THEN
  IF prior.request_id<>rid OR prior.deployment_id<>deployment OR prior.sequence IS DISTINCT FROM seq THEN
   RAISE EXCEPTION 'attempt_conflict'; END IF;
  RETURN jsonb_build_object('attempt_id',aid,'cost_price_version_id',prior.cost_price_version_id,'state',prior.state,'replayed',true);
 END IF;
 IF node IS NOT NULL THEN at_time=clock_timestamp(); END IF;
 IF wallet.status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 IF req.reservation_state<>'held' OR req.state='review' OR req.expires_at<=at_time OR
 EXISTS(SELECT FROM billing.usage_events WHERE request_id=rid AND kind='request.closed')
 THEN RAISE EXCEPTION 'request_not_executable'; END IF;
 IF req.model_id IS DISTINCT FROM target_model THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 IF node IS NOT NULL AND (seq IS NULL OR seq>1024 OR seq<>(SELECT count(*)+1 FROM billing.request_attempts WHERE request_id=rid))
 THEN RAISE EXCEPTION 'invalid_attempt_sequence'; END IF;
 SELECT * INTO cost FROM billing.price_versions WHERE kind='cost' AND scope_id=deployment
 AND state='published' AND effective_at<=at_time ORDER BY effective_at DESC LIMIT 1;
 SELECT * INTO STRICT sale FROM billing.price_versions WHERE id=req.sale_price_version_id;
 IF cost.id IS NULL OR NOT cost.enabled OR cost.model_id<>target_model OR cost.currency<>sale.currency
 OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(cost.rates) k)
 IS DISTINCT FROM (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(sale.rates) k)
 THEN RAISE EXCEPTION 'cost_unavailable'; END IF;
 INSERT INTO billing.request_attempts(id,request_id,deployment_id,cost_price_version_id,sequence)
 VALUES(aid,rid,deployment,cost.id,seq);
 UPDATE billing.request_runs SET state='running' WHERE id=rid;
 INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details)
 VALUES(gen_random_uuid(),org,'attempt.started',aid,jsonb_build_object('request_id',rid,'cost_price_version_id',cost.id));
 RETURN jsonb_build_object('attempt_id',aid,'cost_price_version_id',cost.id,'state','started','replayed',false);
END $$;

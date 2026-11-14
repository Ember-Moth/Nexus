CREATE FUNCTION billing.v1_attempt(org uuid,rid uuid,aid uuid,deployment uuid, target_model uuid,
 seq integer,at_time timestamptz,node uuid,expected_format text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE wallet billing.accounts; req billing.request_runs; prior billing.request_attempts;
 cost billing.price_versions; sale billing.price_versions;
BEGIN
 -- Attempts only read wallet status. The shared row lock prevents a concurrent freeze.
 -- Serialize gateway admissions for this wallet before contending on its tuple.
 -- Other writers still coordinate on the authoritative account row.
 IF node IS NOT NULL THEN
  PERFORM pg_advisory_xact_lock(hashtextextended(org::text,398221));
 END IF;
 SELECT * INTO wallet FROM billing.accounts WHERE organization_id=org FOR SHARE;
 SELECT * INTO req FROM billing.request_runs WHERE id=rid AND organization_id=org FOR UPDATE;
 IF NOT FOUND OR req.node_id IS DISTINCT FROM node THEN RAISE EXCEPTION 'not_found'; END IF;
 SELECT * INTO prior FROM billing.request_attempts WHERE id=aid;
 IF FOUND THEN
  IF prior.request_id<>rid OR prior.deployment_id<>deployment OR prior.sequence IS DISTINCT FROM seq
   OR prior.expected_usage_format IS DISTINCT FROM expected_format THEN
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
 INSERT INTO billing.request_attempts(id,request_id,deployment_id,cost_price_version_id,sequence,expected_usage_format)
 VALUES(aid,rid,deployment,cost.id,seq,expected_format);
 UPDATE billing.request_runs SET state='running' WHERE id=rid;
 INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details)
 VALUES(gen_random_uuid(),org,'attempt.started',aid,jsonb_build_object('request_id',rid,'cost_price_version_id',cost.id));
 RETURN jsonb_build_object('attempt_id',aid,'cost_price_version_id',cost.id,'state','started','replayed',false);
END $$;

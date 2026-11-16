-- billing functions and trigger functions.
-- Created by their owning role, so their privileges are exactly the grants in 17_privileges.sql.

-- Owned by nexus_billing.
SET LOCAL ROLE nexus_billing;

CREATE FUNCTION billing.v1_append(message text, node uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $_$
DECLARE e jsonb; body jsonb; raw jsonb; rid uuid; aid uuid; eid uuid; kind text;
 req billing.request_runs; old billing.usage_events; ids uuid[]; started uuid[]; delivered uuid;
BEGIN
 IF message IS NULL OR octet_length(message)>65536 THEN RAISE EXCEPTION 'invalid_event_size'; END IF;
 IF NOT (message IS JSON OBJECT WITH UNIQUE KEYS) THEN RAISE EXCEPTION 'invalid_usage_envelope'; END IF;
 e=message::jsonb;
 IF jsonb_typeof(e)<>'object' OR e-'schema_version'-'event_id'-'event_type'-'request_id'-'attempt_id'
 -'occurred_at'-'config_revision'-'payload'<>'{}' OR e->'schema_version' IS DISTINCT FROM '1'::jsonb
 OR NOT (e ?& ARRAY['event_id','event_type','request_id','occurred_at','config_revision','payload'])
 OR jsonb_typeof(e->'config_revision') IS DISTINCT FROM 'string'
 OR jsonb_typeof(e->'occurred_at') IS DISTINCT FROM 'string' OR
 (e->>'occurred_at') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T.{8,24}(Z|[+-][0-9]{2}:[0-9]{2})$'
 THEN RAISE EXCEPTION 'invalid_usage_envelope'; END IF;
 PERFORM (e->>'occurred_at')::timestamptz;
 rid=(e->>'request_id')::uuid; eid=(e->>'event_id')::uuid; aid=(e->>'attempt_id')::uuid;
 kind=e->>'event_type'; body=e->'payload';
 IF eid IS NULL OR kind IS NULL OR kind NOT IN ('attempt.finished','request.closed') OR jsonb_typeof(body)<>'object'
 THEN RAISE EXCEPTION 'invalid_usage_event'; END IF;
 SELECT * INTO req FROM billing.request_runs WHERE id=rid AND node_id=node FOR UPDATE;
 IF NOT FOUND OR req.config_revision::text IS DISTINCT FROM e->>'config_revision' THEN RAISE EXCEPTION 'not_found'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(eid::text,87411));
 SELECT * INTO old FROM billing.usage_events WHERE id=eid;
 IF FOUND AND old.envelope=e THEN
      INSERT INTO billing.ingest_counters(name,total) VALUES('usage',1)
      ON CONFLICT(name) DO UPDATE SET total=billing.ingest_counters.total+1;
      RETURN jsonb_build_object('status','accepted','event_id',eid); END IF;
 IF old.id IS NOT NULL OR EXISTS(SELECT FROM billing.usage_events u WHERE u.request_id=rid AND u.kind=e->>'event_type'
 AND (u.attempt_id IS NOT DISTINCT FROM aid)) THEN
  INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),
  req.organization_id,'usage.conflict',rid,jsonb_build_object('event_id',eid,'node_id',node));
  RETURN jsonb_build_object('status','conflict','event_id',eid);
 END IF;
 IF req.state='canceled' THEN RAISE EXCEPTION 'request_canceled'; END IF;
 IF kind='attempt.finished' THEN
  IF aid IS NULL OR NOT EXISTS(SELECT FROM billing.request_attempts WHERE id=aid AND request_id=rid)
  OR body-'outcome'-'upstream_request_id'-'usage_format'-'raw_usage'-'elapsed_ms'<>'{}'
  OR NOT(body ?& ARRAY['outcome','upstream_request_id','usage_format','raw_usage','elapsed_ms'])
  OR body->>'outcome' IS NULL OR body->>'outcome' NOT IN ('succeeded','failed','cancelled','unknown','declined')
  OR jsonb_typeof(body->'upstream_request_id') NOT IN ('null','string')
  OR coalesce(body->>'upstream_request_id','') !~ '^[[:alnum:]_.:/-]{0,200}$'
  OR jsonb_typeof(body->'usage_format')<>'string' OR body->>'usage_format' !~ '^[a-zA-Z0-9_.-]{1,80}$'
  OR jsonb_typeof(body->'elapsed_ms')<>'number' OR body->>'elapsed_ms' !~ '^[0-9]{1,10}$'
  THEN RAISE EXCEPTION 'invalid_attempt_fact'; END IF;
  raw=body->'raw_usage';
  IF jsonb_typeof(raw) NOT IN ('object','null') THEN RAISE EXCEPTION 'invalid_usage_shape'; END IF;
  -- A declined attempt was not executed: it reports no usage or only zeros.
  IF body->>'outcome'='declined' AND jsonb_typeof(raw)='object' AND EXISTS(SELECT FROM jsonb_each(raw) WHERE value IS DISTINCT FROM '0'::jsonb) THEN RAISE EXCEPTION 'invalid_attempt_fact'; END IF;
  IF jsonb_typeof(raw)='object' THEN
   IF (SELECT count(*) FROM jsonb_each(raw))>(CASE WHEN body->>'usage_format'='aws.bedrock.converse.v1' THEN 24 ELSE 16 END) OR EXISTS(SELECT FROM jsonb_each(raw)
   WHERE (key NOT IN ('web_search_requests','file_search_calls','vector_storage_byte_seconds','code_execution_requests','container_1g_minutes','container_4g_minutes','container_16g_minutes','container_64g_minutes','guardrail_content_units','guardrail_image_units','guardrail_topic_units','guardrail_sensitive_units','guardrail_grounding_units','guardrail_reasoning_units','guardrail_word_units','guardrail_sensitive_free_units','guardrail_reasoning_text_units','guardrail_reasoning_policies','guardrail_usage_unverified') AND key !~ '^[a-z][a-z0-9_]{0,48}_tokens$') OR jsonb_typeof(value) NOT IN ('number','null') OR length(value::text)>24)
   THEN RAISE EXCEPTION 'invalid_usage_shape'; END IF;
  END IF;
 ELSE
  IF aid IS NOT NULL OR body-'delivery_outcome'-'attempt_ids'-'delivered_attempt_id'<>'{}'
  OR NOT(body ?& ARRAY['delivery_outcome','attempt_ids','delivered_attempt_id'])
  OR body->>'delivery_outcome' IS NULL OR body->>'delivery_outcome' NOT IN ('succeeded','client_cancelled','upstream_failed','unknown')
  OR jsonb_typeof(body->'attempt_ids')<>'array' THEN RAISE EXCEPTION 'invalid_close_fact'; END IF;
  SELECT coalesce(array_agg(v::uuid ORDER BY v::uuid),'{}') INTO ids FROM jsonb_array_elements_text(body->'attempt_ids') v;
  SELECT coalesce(array_agg(id ORDER BY id),'{}') INTO started FROM billing.request_attempts WHERE request_id=rid;
  delivered=(body->>'delivered_attempt_id')::uuid;
  IF ids IS DISTINCT FROM started OR (delivered IS NOT NULL AND NOT delivered=ANY(started))
  OR (body->>'delivery_outcome'='succeeded' AND delivered IS NULL)

  THEN RAISE EXCEPTION 'invalid_attempt_set'; END IF;
 END IF;
 INSERT INTO billing.usage_events(id,request_id,attempt_id,kind,envelope) VALUES(eid,rid,aid,kind,e);
 INSERT INTO billing.usage_jobs(request_id,state) VALUES(rid,'ready') ON CONFLICT(request_id)
 DO UPDATE SET state='ready',next_attempt_at=clock_timestamp(),failures=0,generation=billing.usage_jobs.generation+1,reason=NULL;
 RETURN jsonb_build_object('status','accepted','event_id',eid);
END $_$;

CREATE FUNCTION billing.v1_attempt(org uuid, rid uuid, aid uuid, deployment uuid, target_model uuid, seq integer, at_time timestamp with time zone, node uuid, expected_format text DEFAULT NULL::text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
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

CREATE FUNCTION billing.v1_batch_pricing(rid uuid, node uuid) RETURNS boolean
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT billing_policy_version='anthropic_message_batch_v1' FROM billing.request_runs WHERE id=rid AND node_id=node
$$;

CREATE FUNCTION billing.v1_cancel(rid uuid, node uuid, reason text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $_$
DECLARE req billing.request_runs;
BEGIN
 IF reason IS NULL OR reason !~ '^[a-z0-9_]{1,64}$' THEN RAISE EXCEPTION 'invalid_reason'; END IF;
 SELECT * INTO req FROM billing.request_runs WHERE id=rid AND node_id=node;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 PERFORM id FROM billing.accounts WHERE id=req.account_id FOR UPDATE;
 SELECT * INTO req FROM billing.request_runs WHERE id=rid FOR UPDATE;
 IF req.state='canceled' THEN RETURN billing.v1_status(rid,node); END IF;
 IF req.reservation_state<>'held' OR EXISTS(SELECT FROM billing.request_attempts WHERE request_id=rid)
 OR EXISTS(SELECT FROM billing.usage_events WHERE request_id=rid AND kind='request.closed')
 THEN RAISE EXCEPTION 'cannot_cancel'; END IF;
 UPDATE billing.budget_periods p SET reserved=p.reserved-h.reserved FROM billing.budget_holds h
 WHERE h.request_id=rid AND h.period_id=p.id;
 UPDATE billing.budget_holds SET reserved=0 WHERE request_id=rid;
 UPDATE billing.accounts SET held_amount=held_amount-req.reserved_amount,version=version+1 WHERE id=req.account_id;
 UPDATE billing.request_runs SET state='canceled',reservation_state='released',charged_amount=0,charge_details='[]',
 settled_at=clock_timestamp(),settlement_digest=encode(sha256(convert_to(reason,'UTF8')),'hex'),review_reason=NULL WHERE id=rid;
 INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),
 req.organization_id,'request.canceled',rid,jsonb_build_object('reason',reason,'node_id',node));
 RETURN billing.v1_status(rid,node);
END $_$;

CREATE FUNCTION billing.v1_container_cover(meter uuid, origin_id uuid, reach timestamp with time zone, dims jsonb, step integer) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE m billing.container_sessions; base billing.request_runs; needed integer; added integer; ext uuid;
BEGIN
 SELECT * INTO STRICT m FROM billing.container_sessions WHERE id=meter FOR UPDATE;
 IF reach>m.potential_end THEN
  UPDATE billing.container_sessions SET potential_end=reach WHERE id=meter RETURNING * INTO m;
 END IF;
 needed=billing.v1_container_minutes(m.tariff,m.potential_end-m.created_at);
 IF needed<=m.covered_minutes THEN RETURN 0; END IF;
 SELECT * INTO STRICT base FROM billing.request_runs WHERE id=m.id;
 added=greatest(needed-m.covered_minutes,coalesce(step,0));
 IF m.tariff='session_20m' THEN added=20*ceil(added/20.0)::integer; END IF;
 IF dims IS NULL THEN
  SELECT jsonb_agg(k) INTO dims FROM (SELECT p.rates FROM billing.price_versions p WHERE p.kind='sale' AND p.scope_id=m.model_id
   AND p.state='published' AND p.effective_at<=clock_timestamp() ORDER BY p.effective_at DESC LIMIT 1) s,jsonb_object_keys(s.rates) k;
 END IF;
 ext=gen_random_uuid();
 PERFORM billing.v1_reserve_capacity(ext,m.organization_id,base.project_id,base.service_account_id,m.api_key_id,m.model_id,
   0,0,86400,encode(sha256(convert_to(ext::text||':'||base.container_unit,'UTF8')),'hex'),dims,clock_timestamp(),
   m.node_id,m.cluster_id,m.revision,false,0,0,0,NULL,'none',0,base.container_unit,added,m.tariff);
 UPDATE billing.request_runs SET container_origin=origin_id,container_handle=base.container_handle WHERE id=ext;
 INSERT INTO billing.container_extensions(request_id,meter,origin,sequence,start_minute,minutes)
 VALUES(ext,m.id,origin_id,(SELECT count(*)+1 FROM billing.container_extensions e WHERE e.meter=m.id),m.covered_minutes,added);
 UPDATE billing.container_sessions SET covered_minutes=covered_minutes+added WHERE id=m.id;
 RETURN added;
END $$;

CREATE FUNCTION billing.v1_container_due(handle_id uuid, kid uuid, confirmed boolean) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 UPDATE billing.container_sessions SET review_after=clock_timestamp(),closed_at=CASE WHEN confirmed THEN clock_timestamp() ELSE NULL END WHERE handle=handle_id AND api_key_id=kid AND state IN ('active','provisional')
$$;

CREATE FUNCTION billing.v1_container_hold(origin uuid, existing uuid, principal jsonb, kid uuid, model uuid, dep uuid, dims jsonb, tier text, tariff text, minutes integer, node uuid, cluster uuid, revision bigint, fresh boolean, supplier uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $_$
DECLARE org uuid:=(principal->>'organization_id')::uuid; m billing.container_sessions; op billing.container_operations;
 cost billing.price_versions; r billing.request_runs; at_time timestamptz:=clock_timestamp();
 wallet_status text; unit_name text:='container_'||tier||'_minutes'; cap_id uuid:=coalesce(existing,origin); new_meter boolean:=false;
 reach timestamptz; first integer;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(org::text,398221));
 SELECT a.status INTO STRICT wallet_status FROM billing.accounts a WHERE a.organization_id=org FOR UPDATE;
 IF wallet_status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 SELECT * INTO op FROM billing.container_operations WHERE container_operations.origin=$1;
 IF FOUND THEN
  IF op.api_key_id<>kid OR op.deployment_id<>dep OR op.requested_handle IS DISTINCT FROM existing THEN RAISE EXCEPTION 'request_conflict'; END IF;
  RETURN;
 END IF;
 reach=billing.v1_container_reach(origin);
 SELECT * INTO m FROM billing.container_sessions WHERE handle=cap_id AND state IN ('provisional','active','review') FOR UPDATE;
 IF FOUND THEN
  IF m.api_key_id<>kid OR m.deployment_id<>dep OR m.memory_tier<>tier THEN RAISE EXCEPTION 'resource_conflict'; END IF;
  -- Reconciliation has closed this meter's events, and the supplier has idled the container out.
  IF m.state='review' THEN RAISE EXCEPTION 'resource_not_found'; END IF;
  -- Reserve before the operation can extend the supplier's billing; a failure refuses the reuse.
  PERFORM billing.v1_container_cover(m.id,origin,reach+make_interval(mins=>m.idle_minutes),dims,minutes);
 ELSE
  m.id=gen_random_uuid();new_meter=true;
  first=greatest(minutes,billing.v1_container_minutes(tariff,reach+interval '20 minutes'-at_time));
  PERFORM billing.v1_reserve_capacity(m.id,org,(principal->>'project_id')::uuid,(principal->>'service_account_id')::uuid,kid,model,
    0,0,86400,encode(sha256(convert_to(origin::text||':'||unit_name,'UTF8')),'hex'),dims,at_time,node,cluster,revision,false,0,0,0,NULL,'none',0,unit_name,first,tariff);
  UPDATE billing.request_runs SET container_origin=origin,container_handle=CASE WHEN existing IS NOT NULL THEN 'cntr_nx_'||existing::text END WHERE id=m.id;
  SELECT * INTO STRICT r FROM billing.request_runs WHERE id=m.id;
  SELECT * INTO cost FROM billing.price_versions p WHERE p.kind='cost' AND p.scope_id=dep AND p.state='published' AND p.effective_at<=at_time ORDER BY p.effective_at DESC LIMIT 1;
  IF cost.id IS NULL OR NOT cost.enabled OR cost.model_id<>model OR cost.currency IS DISTINCT FROM (SELECT p.currency FROM billing.price_versions p WHERE p.id=r.sale_price_version_id)
    OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(cost.rates) k) IS DISTINCT FROM (SELECT array_agg(k ORDER BY k) FROM jsonb_array_elements_text(dims) k)
    OR cost.rates->unit_name->>'unit_quantity' NOT IN ('1','20') THEN RAISE EXCEPTION 'cost_unavailable'; END IF;
  -- A new container idles out after twenty minutes unless its receipt says otherwise.
  INSERT INTO billing.container_sessions(id,handle,organization_id,api_key_id,model_id,deployment_id,supplier_id,node_id,cluster_id,revision,attempt_id,cost_price_id,
     memory_tier,tariff,state,explicit_creation,review_after,created_at,covered_minutes,potential_end,idle_minutes)
  VALUES(m.id,cap_id,org,kid,model,dep,supplier,node,cluster,revision,gen_random_uuid(),cost.id,
     tier,tariff,'provisional',fresh,clock_timestamp()+interval '3 minutes',at_time,
     CASE WHEN tariff='session_20m' THEN first/20*20 ELSE first END,reach+interval '20 minutes',20);
 END IF;
 INSERT INTO billing.container_operations(origin,meter,api_key_id,requested_handle,deployment_id,provisional)
 VALUES(origin,m.id,kid,existing,dep,new_meter);
END $_$;

CREATE FUNCTION billing.v1_container_link(origin_id uuid, canonical uuid, kid uuid, dep uuid, pid text, minutes integer, executed boolean, actual_tier text, verified_tier boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE op billing.container_operations; m billing.container_sessions; prior billing.container_sessions;
 idle integer; reach timestamptz; detached boolean:=false;
BEGIN
 SELECT * INTO STRICT op FROM billing.container_operations WHERE origin=origin_id AND api_key_id=kid AND deployment_id=dep;
 SELECT * INTO STRICT m FROM billing.container_sessions WHERE id=op.meter;
 PERFORM pg_advisory_xact_lock(hashtextextended(m.organization_id::text,398221));
 PERFORM id FROM billing.accounts WHERE organization_id=m.organization_id FOR UPDATE;
 SELECT * INTO STRICT op FROM billing.container_operations WHERE origin=origin_id FOR UPDATE;
 SELECT * INTO STRICT m FROM billing.container_sessions WHERE id=op.meter FOR UPDATE;
 IF op.requested_handle IS NOT NULL AND op.requested_handle<>canonical OR m.provider_id IS NOT NULL AND m.provider_id<>pid THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 IF op.finished AND m.handle=canonical AND m.provider_id=pid THEN
  IF (executed AND NOT m.completed_call OR verified_tier AND NOT m.tier_verified) AND m.state IN ('active','review') THEN
   UPDATE billing.container_sessions SET completed_call=completed_call OR executed,tier_verified=tier_verified OR verified_tier WHERE id=m.id; END IF;
  RETURN;
 END IF;
 idle=least(1440,greatest(CASE WHEN m.tariff='minute_min_5' THEN 5 ELSE 20 END,coalesce(minutes,20)));
 -- The operation has run: the supplier may bill until its request deadline plus the idle expiry.
 reach=greatest(clock_timestamp(),coalesce((SELECT r.expires_at FROM billing.request_runs r WHERE r.id=origin_id AND r.reservation_state='held'),clock_timestamp()))
   +make_interval(mins=>idle);
 IF m.handle<>canonical THEN
  SELECT * INTO prior FROM billing.container_sessions WHERE handle=canonical AND state IN ('active','review','provisional') FOR UPDATE;
  IF FOUND THEN
   IF prior.api_key_id<>kid OR prior.deployment_id<>dep OR prior.memory_tier<>actual_tier OR m.state<>'provisional'
   THEN RAISE EXCEPTION 'resource_conflict'; END IF;
   IF prior.state='review' THEN
    -- The prior meter's events are closed. Keep this operation's own hold and meter.
    detached=true;
   ELSE
    UPDATE billing.container_sessions SET idle_minutes=greatest(idle_minutes,idle),potential_end=greatest(potential_end,reach)
    WHERE id=prior.id RETURNING * INTO prior;
    IF billing.v1_container_minutes(prior.tariff,prior.potential_end-prior.created_at)<=prior.covered_minutes THEN
     PERFORM billing.v1_cancel(m.id,m.node_id,'container_reused');
    ELSE
     -- The supplier reused a container beyond its holds: the duplicate hold now extends it.
     INSERT INTO billing.container_extensions(request_id,meter,origin,sequence,start_minute,minutes)
     VALUES(m.id,prior.id,origin_id,(SELECT count(*)+1 FROM billing.container_extensions e WHERE e.meter=prior.id),
       prior.covered_minutes,m.covered_minutes);
     UPDATE billing.container_sessions SET covered_minutes=covered_minutes+m.covered_minutes WHERE id=prior.id;
     UPDATE billing.request_runs SET container_handle='cntr_nx_'||canonical::text WHERE id=m.id;
    END IF;
    UPDATE billing.container_sessions SET state='released' WHERE id=m.id;
    UPDATE billing.container_operations SET meter=prior.id,provisional=false WHERE origin=origin_id;
    m=prior;
   END IF;
  END IF;
 END IF;
 IF m.memory_tier<>actual_tier THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 IF m.state NOT IN ('provisional','active','review') THEN
  IF op.finished AND m.handle=canonical AND m.provider_id=pid THEN RETURN; END IF;
  RAISE EXCEPTION 'resource_conflict';
 END IF;
 UPDATE billing.request_runs SET container_handle='cntr_nx_'||canonical::text WHERE id=m.id AND container_handle IS NULL;
 UPDATE billing.request_runs r SET container_handle='cntr_nx_'||canonical::text FROM billing.container_extensions e
 WHERE e.meter=m.id AND r.id=e.request_id AND r.container_handle IS NULL;
 UPDATE billing.container_sessions SET handle=CASE WHEN detached THEN handle ELSE canonical END,provider_id=pid,
 tier_verified=tier_verified OR verified_tier,completed_call=completed_call OR executed,state=CASE WHEN state='review' THEN state ELSE 'active' END,
 last_seen=clock_timestamp(),review_after=clock_timestamp()+make_interval(mins=>idle),idle_minutes=idle
 WHERE id=m.id;
 IF m.state<>'review' THEN
  PERFORM billing.v1_container_settle_cover(m.id,origin_id,reach,(SELECT r.container_minutes_limit FROM billing.request_runs r WHERE r.id=m.id));
 END IF;
 UPDATE billing.container_operations SET finished=true WHERE origin=origin_id;
END $$;

CREATE FUNCTION billing.v1_container_minutes(tariff text, span interval) RETURNS integer
    LANGUAGE sql IMMUTABLE
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT CASE WHEN tariff='session_20m' THEN 20*greatest(1,ceil(extract(epoch FROM span)/1200))::integer
  ELSE greatest(5,ceil(extract(epoch FROM span)/60))::integer END
$$;

CREATE FUNCTION billing.v1_container_reach(origin_id uuid) RETURNS timestamp with time zone
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT greatest(clock_timestamp(),coalesce((SELECT r.expires_at FROM billing.request_runs r
   WHERE r.id=origin_id AND r.reservation_state='held'),clock_timestamp()+interval '15 minutes'))
$$;

CREATE FUNCTION billing.v1_container_release(origin_id uuid, kid uuid, dep uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE op billing.container_operations; m billing.container_sessions;
BEGIN
 SELECT * INTO op FROM billing.container_operations WHERE origin=origin_id AND api_key_id=kid AND deployment_id=dep;
 IF NOT FOUND THEN RETURN; END IF;
 SELECT * INTO STRICT m FROM billing.container_sessions WHERE id=op.meter;
 PERFORM pg_advisory_xact_lock(hashtextextended(m.organization_id::text,398221));
 PERFORM id FROM billing.accounts WHERE organization_id=m.organization_id FOR UPDATE;
 SELECT * INTO STRICT op FROM billing.container_operations WHERE origin=origin_id FOR UPDATE;
 SELECT * INTO STRICT m FROM billing.container_sessions WHERE id=op.meter FOR UPDATE;
 IF op.finished THEN RETURN; END IF;
 UPDATE billing.container_operations SET finished=true WHERE origin=origin_id;
 IF m.state='provisional' AND NOT EXISTS(
    SELECT FROM billing.container_operations WHERE meter=m.id AND origin<>origin_id AND NOT finished) THEN
  PERFORM billing.v1_cancel(m.id,m.node_id,'container_not_used');
  UPDATE billing.container_sessions SET state='released' WHERE id=m.id;
 END IF;
END $$;

CREATE FUNCTION billing.v1_container_settle_cover(meter uuid, origin_id uuid, reach timestamp with time zone, step integer) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE m billing.container_sessions;
BEGIN
 BEGIN
  PERFORM billing.v1_container_cover(meter,origin_id,reach,NULL,step);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT IN ('account_frozen','insufficient_funds','budget_exceeded','price_unavailable','invalid_reserve_amount','invalid_bounds','settlement_backlog')
  THEN RAISE; END IF;
  SELECT * INTO STRICT m FROM billing.container_sessions WHERE id=meter;
  UPDATE billing.container_sessions SET potential_end=greatest(potential_end,reach) WHERE id=meter;
  -- Settlement backpressure is transient: the next operation on this session reserves the
  -- recorded reach before it may run, so the receipt is kept without freezing the wallet.
  IF SQLERRM='settlement_backlog' THEN
   INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),m.organization_id,
    'container.coverage_deferred',m.id,jsonb_build_object('origin',origin_id,'reach',reach,'covered_minutes',m.covered_minutes));
   RETURN;
  END IF;
  INSERT INTO billing.account_restrictions(account_id,source_type,source_id)
  SELECT r.account_id,'request',r.id FROM billing.request_runs r WHERE r.id=m.id
  ON CONFLICT(account_id,source_type,source_id) DO UPDATE SET released_at=NULL;
  UPDATE billing.accounts SET status='frozen',version=version+1 WHERE organization_id=m.organization_id AND status<>'frozen';
  INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),m.organization_id,
   'container.coverage_unreserved',m.id,jsonb_build_object('reason',SQLERRM,'origin',origin_id,'reach',reach,'covered_minutes',m.covered_minutes));
 END;
END $$;

CREATE FUNCTION billing.v1_container_settled() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 UPDATE billing.container_sessions SET state='settled' WHERE id=NEW.id AND state IN ('review','active','provisional');
 RETURN NEW;
END $$;

CREATE FUNCTION billing.v1_execution_attempt(rid uuid, kid uuid, dep uuid, cluster uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT jsonb_build_object('revision',r.config_revision,'model',r.model_id,'deployment',a.deployment_id,'mode',r.execution_mode,'searches',r.web_search_limit)
 FROM billing.request_runs r JOIN billing.request_attempts a ON a.request_id=r.id
 WHERE r.id=rid AND r.cluster_id=cluster AND r.api_key_id=kid AND r.execution_mode<>'none'
 AND a.deployment_id=dep AND a.expected_usage_format='anthropic.messages.v1' LIMIT 1
$$;

CREATE FUNCTION billing.v1_execution_supplier(rid uuid, aid uuid, node uuid, supplier uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 IF supplier IS NULL OR NOT EXISTS(SELECT FROM billing.request_runs WHERE id=rid AND node_id=node) THEN RAISE EXCEPTION 'not_found'; END IF;
 UPDATE billing.request_attempts SET meter_supplier_id=supplier WHERE id=aid AND request_id=rid AND meter_supplier_id IS NULL;
 IF NOT FOUND AND NOT EXISTS(SELECT FROM billing.request_attempts WHERE id=aid AND request_id=rid AND meter_supplier_id=supplier)
 THEN RAISE EXCEPTION 'attempt_conflict'; END IF;
END $$;

CREATE FUNCTION billing.v1_renew_storage(days integer, policy text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    SET "TimeZone" TO 'UTC'
    AS $$
DECLARE c billing.vector_storage_contracts; failure text; horizon interval; now_at timestamptz:=clock_timestamp();
BEGIN
 IF days IS NULL OR days NOT BETWEEN 1 AND 31 OR policy IS NULL OR policy NOT IN ('delete','suspend') THEN
  RAISE EXCEPTION 'invalid_storage_policy'; END IF;
 horizon=make_interval(days=>days);
 SELECT s.* INTO c FROM billing.vector_storage_contracts s WHERE NOT s.closed AND s.renew_after<=clock_timestamp()
  AND s.lapse_action IS DISTINCT FROM 'delete' AND (s.prepaid_until IS NULL OR s.renewal_failed_at IS NOT NULL
   OR s.usage_bytes>s.prepaid_bytes OR s.prepaid_until<clock_timestamp()+horizon*0.75
   OR EXISTS(SELECT FROM billing.request_runs r WHERE r.id=s.hold_id AND r.expires_at<clock_timestamp()+interval '6 hours'))
 ORDER BY s.renew_after,s.id LIMIT 1 FOR UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN false; END IF;
 BEGIN
  -- Growth headroom keeps a store that is still indexing from replacing its hold every minute.
  PERFORM billing.v1_storage_hold(c.id,clock_timestamp()+horizon,c.usage_bytes+c.usage_bytes/4);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT IN ('account_frozen','insufficient_funds','budget_exceeded','invalid_reserve_amount','settlement_backlog') THEN RAISE; END IF;
  failure=SQLERRM;
 END;
 IF failure IS NULL THEN
  UPDATE billing.vector_storage_contracts SET renewal_failed_at=NULL,renew_after=clock_timestamp()+interval '1 minute',
   lapsed_at=NULL,lapse_action=NULL WHERE id=c.id;
  IF c.renewal_failed_at IS NOT NULL THEN
   INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),c.organization_id,
    'vector_storage.renewed',c.id,jsonb_build_object('prepaid_days',days));
  END IF;
  RETURN true;
 END IF;
 IF failure='settlement_backlog' THEN
  UPDATE billing.vector_storage_contracts SET renew_after=clock_timestamp()+interval '1 minute' WHERE id=c.id;
  RETURN true;
 END IF;
 -- The hold funds prepaid_bytes until prepaid_until; at the observed size it lasts proportionally less.
 UPDATE billing.vector_storage_contracts SET renewal_failed_at=coalesce(renewal_failed_at,now_at),
  renew_after=now_at+interval '5 minutes',
  prepaid_until=CASE WHEN prepaid_until IS NULL OR hold_id IS NULL AND usage_bytes>0 THEN now_at
   WHEN usage_bytes>prepaid_bytes THEN least(prepaid_until,now_at+greatest(prepaid_until-now_at,interval '0')
     *(prepaid_bytes::float8/usage_bytes))
   ELSE prepaid_until END WHERE id=c.id RETURNING * INTO c;
 IF c.lapsed_at IS NULL AND c.prepaid_until<=clock_timestamp() THEN
  UPDATE billing.vector_storage_contracts SET lapsed_at=clock_timestamp(),lapse_action=policy WHERE id=c.id;
  INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),c.organization_id,
   'vector_storage.lapsed',c.id,jsonb_build_object('reason',failure,'policy',policy,'usage_bytes',c.usage_bytes));
 ELSIF c.renewal_failed_at=now_at THEN
  INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),c.organization_id,
   'vector_storage.renewal_failed',c.id,jsonb_build_object('reason',failure,'prepaid_until',c.prepaid_until));
 END IF;
 RETURN true;
END $$;

CREATE FUNCTION billing.v1_reserve(rid uuid, org uuid, project uuid, service uuid, kid uuid, model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb, at_time timestamp with time zone, node uuid, cluster uuid, rev bigint) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT billing.v1_reserve_item(rid,org,project,service,kid,model,inputs,outputs,ttl,fingerprint,dimensions,at_time,node,cluster,rev,false)
$$;

CREATE FUNCTION billing.v1_reserve_capacity(rid uuid, org uuid, project uuid, service uuid, kid uuid, model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb, at_time timestamp with time zone, node uuid, cluster uuid, rev bigint, batch boolean, searches integer, files integer, storage numeric, store_id uuid, exec_mode text, exec_bound integer, cu text, minutes integer, tariff text) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT billing.v1_reserve_guarded_capacity(rid,org,project,service,kid,model,inputs,outputs,ttl,fingerprint,dimensions,
     at_time,node,cluster,rev,batch,searches,files,storage,store_id,exec_mode,exec_bound,cu,minutes,tariff,0)
$$;

CREATE FUNCTION billing.v1_reserve_execution(rid uuid, org uuid, project uuid, service uuid, kid uuid, model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb, at_time timestamp with time zone, node uuid, cluster uuid, rev bigint, batch boolean, searches integer, files integer, storage numeric, store_id uuid, exec_mode text, exec_bound integer) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT billing.v1_reserve_capacity(rid,org,project,service,kid,model,inputs,outputs,ttl,fingerprint,dimensions,
     at_time,node,cluster,rev,batch,searches,files,storage,store_id,exec_mode,exec_bound,NULL,0,NULL)
$$;

CREATE FUNCTION billing.v1_reserve_guarded_capacity(rid uuid, org uuid, project uuid, service uuid, kid uuid, model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb, at_time timestamp with time zone, node uuid, cluster uuid, rev bigint, batch boolean, searches integer, files integer, storage numeric, store_id uuid, exec_mode text, exec_bound integer, cu text, minutes integer, tariff text, guard_units integer) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    SET "TimeZone" TO 'UTC'
    AS $$
DECLARE wallet billing.accounts; prior billing.request_runs; sale billing.price_versions;
 contract billing.vector_storage_contracts; discount billing.discount_versions; over_budget boolean;
 numerator numeric; r numeric; matched uuid[];
BEGIN
 IF guard_units IS NULL OR guard_units NOT BETWEEN 0 AND 1000000000 OR
 (guard_units>0 AND (cu IS NOT NULL OR store_id IS NOT NULL OR exec_mode<>'none' OR batch OR searches>0 OR files>0 OR
 NOT EXISTS(SELECT FROM jsonb_array_elements_text(dimensions) dim WHERE starts_with(dim,'guardrail_'))))
 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF minutes IS NULL OR minutes<0 OR minutes>1000000000 OR (cu IS NULL)<>(minutes=0) OR (tariff IS NULL)<>(cu IS NULL)
 OR (cu IS NOT NULL AND (cu NOT IN ('container_1g_minutes','container_4g_minutes','container_16g_minutes','container_64g_minutes')
    OR NOT dimensions ? cu OR tariff NOT IN ('minute_min_5','session_20m') OR minutes<(CASE WHEN tariff='minute_min_5' THEN 5 ELSE 20 END)
    OR inputs<>0 OR outputs<>0 OR files<>0 OR searches<>0 OR store_id IS NOT NULL OR exec_mode<>'none' OR batch))
 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF exec_mode IS NULL OR exec_mode NOT IN ('none','bundled','metered','preloaded') OR exec_bound IS NULL
 OR (exec_mode IN ('metered','preloaded') AND (exec_bound NOT BETWEEN 300 AND 1000000000 OR NOT dimensions ? 'code_execution_seconds'))
 OR (exec_mode IN ('none','bundled') AND exec_bound<>0) OR (store_id IS NOT NULL AND exec_mode<>'none')
 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF files IS NULL OR files NOT BETWEEN 0 AND 1000000000 OR (files>0 AND NOT dimensions ? 'file_search_calls') OR
 storage IS NULL OR storage<0 OR trunc(storage)<>storage OR storage>999999999999999999999999 OR (storage>0 AND store_id IS NULL)
 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF store_id IS NOT NULL THEN
  SELECT * INTO STRICT contract FROM billing.vector_storage_contracts WHERE id=store_id AND api_key_id=kid AND organization_id=org AND model_id=model AND node_id=node;
 END IF;
 IF searches IS NULL OR searches NOT BETWEEN 0 AND 1000000000 OR (searches>0 AND NOT dimensions ? 'web_search_requests') THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
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
  IF prior.guardrail_unit_limit<>guard_units OR prior.container_unit IS DISTINCT FROM cu OR prior.container_minutes_limit<>minutes OR prior.container_tariff IS DISTINCT FROM tariff OR prior.execution_mode<>exec_mode OR prior.execution_seconds_limit<>exec_bound OR prior.file_search_limit<>files OR prior.storage_byte_seconds<>storage OR prior.web_search_limit<>searches OR prior.request_digest<>fingerprint OR prior.account_id<>wallet.id OR prior.node_id IS DISTINCT FROM node THEN
   RAISE EXCEPTION 'request_conflict'; END IF;
  RETURN rid;
 END IF;
 IF wallet.status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 PERFORM revision FROM billing.source_revision WHERE id=1 FOR SHARE;
 IF node IS NOT NULL AND store_id IS NULL THEN at_time=clock_timestamp(); END IF;
 IF store_id IS NOT NULL THEN SELECT * INTO sale FROM billing.price_versions WHERE id=contract.sale_id; ELSE
 SELECT * INTO sale FROM billing.price_versions WHERE kind='sale' AND scope_id=model
  AND state='published' AND effective_at<=at_time ORDER BY effective_at DESC LIMIT 1; END IF;
 IF sale.id IS NULL OR NOT sale.enabled OR sale.model_id<>model OR sale.currency<>wallet.currency
 OR NOT (sale.rates ? 'input_uncached_tokens' AND sale.rates ? 'output_tokens')
 OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(sale.rates) k)
 IS DISTINCT FROM (SELECT array_agg(k ORDER BY k) FROM jsonb_array_elements_text(dimensions) k)
 THEN RAISE EXCEPTION 'price_unavailable'; END IF;
 IF store_id IS NOT NULL THEN SELECT * INTO STRICT discount FROM billing.discount_versions WHERE id=contract.discount_id; ELSE
 SELECT * INTO STRICT discount FROM billing.discount_versions
 WHERE (organization_id=org OR organization_id IS NULL) AND state='published' AND effective_at<=at_time
 ORDER BY organization_id NULLS LAST,effective_at DESC LIMIT 1; END IF;
 IF cu IS NOT NULL THEN
  IF (sale.rates->cu->>'unit_quantity') NOT IN ('1','20') THEN RAISE EXCEPTION 'price_unavailable'; END IF;
  numerator=minutes*(sale.rates->cu->>'unit_price')::numeric*discount.factor*1000000000000;
  r=(div(numerator,(sale.rates->cu->>'unit_quantity')::numeric)+sign(mod(numerator,(sale.rates->cu->>'unit_quantity')::numeric)))*0.000000000001;
 ELSIF store_id IS NOT NULL THEN
  numerator=storage*(sale.rates#>>'{vector_storage_byte_seconds,unit_price}')::numeric*discount.factor*1000000000000;
  r=(div(numerator,92771293593600)+sign(mod(numerator,92771293593600)))*0.000000000001;
 ELSE
 SELECT (
    (inputs*max(rate) FILTER(WHERE dim LIKE 'input_%') + outputs*max(rate) FILTER(WHERE dim='output_tokens'))
    * (CASE WHEN batch THEN 0.5 ELSE 1 END)
    + guard_units*coalesce(sum(rate) FILTER(WHERE starts_with(dim,'guardrail_')),0)
    + greatest(searches*coalesce(max(rate) FILTER(WHERE dim='web_search_requests'),0),
        files*coalesce(max(rate) FILTER(WHERE dim='file_search_calls'),0))
 )*3600 + exec_bound*coalesce(max(hour_price) FILTER(WHERE dim='code_execution_seconds'),0) INTO numerator
 FROM (SELECT key dim,(value->>'unit_price')::numeric *
   CASE (value->>'unit_quantity')::bigint WHEN 1 THEN 1 WHEN 1000 THEN 0.001 WHEN 1000000 THEN 0.000001 END rate,
   (value->>'unit_price')::numeric * CASE (value->>'unit_quantity')::bigint WHEN 3600 THEN 1 WHEN 1 THEN 3600 END hour_price
 FROM jsonb_each(sale.rates)) rates;
 numerator=numerator*discount.factor*1000000000000;
 r=(div(numerator,3600)+sign(mod(numerator,3600)))*0.000000000001;
 END IF;
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
 output_limit,guardrail_unit_limit,container_unit,container_minutes_limit,container_tariff,execution_mode,execution_seconds_limit,web_search_limit,file_search_limit,storage_byte_seconds,reserved_amount,expires_at,created_at,node_id,cluster_id,config_revision)
 VALUES(rid,org,wallet.id,project,service,kid,model,fingerprint,sale.id,discount.id,CASE WHEN cu IS NOT NULL THEN 'openai_container_v1' WHEN store_id IS NOT NULL THEN 'openai_vector_storage_v1' WHEN batch THEN 'anthropic_message_batch_v1' ELSE 'customer_usage_v1' END,
 inputs,outputs,guard_units,cu,minutes,tariff,exec_mode,exec_bound,searches,files,storage,r,(CASE WHEN store_id IS NULL THEN at_time ELSE clock_timestamp() END)+make_interval(secs=>ttl),at_time,node,cluster,rev);
 IF matched IS NOT NULL THEN
   INSERT INTO billing.budget_holds(request_id,period_id,organization_id,reserved,spent)
   SELECT rid,period_id,org,r,0 FROM unnest(matched) AS period_id;
 END IF;
 UPDATE billing.accounts SET held_amount=held_amount+r,version=version+1 WHERE id=wallet.id;
 INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),
 org,'request.reserved',rid,jsonb_build_object('amount',r::text,'sale_price_version_id',sale.id,'discount_version_id',discount.id));
 RETURN rid;
END $$;

CREATE FUNCTION billing.v1_reserve_item(rid uuid, org uuid, project uuid, service uuid, kid uuid, model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb, at_time timestamp with time zone, node uuid, cluster uuid, rev bigint, batch boolean) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT billing.v1_reserve_metered(rid,org,project,service,kid,model,inputs,outputs,ttl,fingerprint,dimensions,
    at_time,node,cluster,rev,batch,0)
$$;

CREATE FUNCTION billing.v1_reserve_metered(rid uuid, org uuid, project uuid, service uuid, kid uuid, model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb, at_time timestamp with time zone, node uuid, cluster uuid, rev bigint, batch boolean, searches integer) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT billing.v1_reserve_tools(rid,org,project,service,kid,model,inputs,outputs,ttl,fingerprint,dimensions,
     at_time,node,cluster,rev,batch,searches,0,0,NULL)
$$;

CREATE FUNCTION billing.v1_reserve_tools(rid uuid, org uuid, project uuid, service uuid, kid uuid, model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb, at_time timestamp with time zone, node uuid, cluster uuid, rev bigint, batch boolean, searches integer, files integer, storage numeric, store_id uuid) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT billing.v1_reserve_execution(rid,org,project,service,kid,model,inputs,outputs,ttl,fingerprint,dimensions,
     at_time,node,cluster,rev,batch,searches,files,storage,store_id,'none',0)
$$;

CREATE FUNCTION billing.v1_response_attempt(rid uuid, aid uuid, node uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT jsonb_build_object('key',r.api_key_id,'model',r.model_id,'revision',r.config_revision,
 'deployment',a.deployment_id)
 FROM billing.request_runs r JOIN billing.request_attempts a ON a.request_id=r.id
 WHERE r.id=rid AND a.id=aid AND r.node_id=node AND a.expected_usage_format='openai.responses.v1'
$$;

CREATE FUNCTION billing.v1_review_container() RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE m billing.container_sessions; base jsonb; answer jsonb; minimum integer; measured boolean; usage jsonb;
 ext billing.container_extensions; ended timestamptz;
BEGIN
 SELECT s.* INTO m FROM billing.container_sessions s JOIN billing.accounts a ON a.organization_id=s.organization_id
 WHERE s.state IN ('provisional','active') AND s.review_after<=clock_timestamp()
 AND NOT EXISTS(SELECT FROM billing.container_operations o JOIN billing.request_runs r ON r.id=o.origin
    WHERE o.meter=s.id AND NOT o.finished AND r.reservation_state='held' AND r.expires_at>clock_timestamp()
    AND NOT EXISTS(SELECT FROM billing.usage_events e WHERE e.request_id=r.id AND e.kind='request.closed')) ORDER BY review_after LIMIT 1 FOR UPDATE OF a SKIP LOCKED;
 IF NOT FOUND THEN RETURN false; END IF;
 SELECT * INTO STRICT m FROM billing.container_sessions WHERE id=m.id FOR UPDATE;
 IF m.state NOT IN ('provisional','active') OR m.review_after>clock_timestamp() THEN RETURN false; END IF;
 INSERT INTO billing.request_attempts(id,request_id,deployment_id,cost_price_version_id,meter_supplier_id,sequence,expected_usage_format)
 VALUES(m.attempt_id,m.id,m.deployment_id,m.cost_price_id,m.supplier_id,1,'openai.container.v1');
 minimum=CASE WHEN m.tariff='session_20m' THEN 20 ELSE 5 END;
 -- A confirmed new container, a completed invocation, and confirmed deletion
 -- inside its first minimum window bound the fee to exactly one minimum.
 measured=m.explicit_creation AND m.tier_verified AND m.completed_call AND m.closed_at IS NOT NULL
  AND m.closed_at>=m.created_at AND m.closed_at-m.created_at<=make_interval(mins=>minimum);
 usage=CASE WHEN measured THEN jsonb_build_object('container_'||m.memory_tier||'_minutes',minimum) END;
 base=jsonb_build_object('request_id',m.id,'schema_version',1,'occurred_at',to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
     'config_revision',m.revision::text);
 answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','attempt.finished','attempt_id',m.attempt_id,
 'payload',jsonb_build_object('outcome',CASE WHEN measured THEN 'succeeded' ELSE 'unknown' END,'upstream_request_id',coalesce(m.provider_id,m.handle::text),'elapsed_ms',0,'usage_format','openai.container.v1','raw_usage',usage)))::text,m.node_id);
 IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','request.closed','attempt_id',NULL,
 'payload',jsonb_build_object('delivery_outcome',CASE WHEN measured THEN 'succeeded' ELSE 'unknown' END,'attempt_ids',jsonb_build_array(m.attempt_id),'delivered_attempt_id',CASE WHEN measured THEN m.attempt_id END)))::text,m.node_id);
 IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 -- The supplier cannot bill past a confirmed deletion or the latest admitted reach.
 ended=least(m.potential_end,m.closed_at);
 FOR ext IN SELECT x.* FROM billing.container_extensions x JOIN billing.request_runs r ON r.id=x.request_id
   WHERE x.meter=m.id AND r.reservation_state='held' ORDER BY x.sequence LOOP
  IF measured OR m.created_at+make_interval(mins=>ext.start_minute)>=ended THEN
   PERFORM billing.v1_cancel(ext.request_id,m.node_id,'container_not_used');
   CONTINUE;
  END IF;
  INSERT INTO billing.request_attempts(id,request_id,deployment_id,cost_price_version_id,meter_supplier_id,sequence,expected_usage_format)
  VALUES(ext.attempt_id,ext.request_id,m.deployment_id,m.cost_price_id,m.supplier_id,1,'openai.container.v1');
  base=jsonb_build_object('request_id',ext.request_id,'schema_version',1,'occurred_at',to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
      'config_revision',m.revision::text);
  answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','attempt.finished','attempt_id',ext.attempt_id,
  'payload',jsonb_build_object('outcome','unknown','upstream_request_id',coalesce(m.provider_id,m.handle::text),'elapsed_ms',0,'usage_format','openai.container.v1','raw_usage',NULL)))::text,m.node_id);
  IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
  answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','request.closed','attempt_id',NULL,
  'payload',jsonb_build_object('delivery_outcome','unknown','attempt_ids',jsonb_build_array(ext.attempt_id),'delivered_attempt_id',NULL)))::text,m.node_id);
  IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 END LOOP;
 UPDATE billing.container_sessions SET state='review' WHERE id=m.id;
 RETURN true;
END $$;

CREATE FUNCTION billing.v1_stage_storage() RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    SET "TimeZone" TO 'UTC'
    AS $$
DECLARE period billing.vector_storage_days; c billing.vector_storage_contracts; dims jsonb; stamp text; base jsonb; answer jsonb; gap boolean;
 released boolean:=false;
BEGIN
 SELECT * INTO period FROM billing.vector_storage_days WHERE (state='ready' OR (state='review' AND reason IN ('account_frozen','insufficient_funds','budget_exceeded')))
 AND next_attempt<=clock_timestamp() ORDER BY next_attempt,id LIMIT 1 FOR UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN false; END IF;
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=period.store_id;
 SELECT jsonb_agg(k) INTO dims FROM billing.price_versions p CROSS JOIN LATERAL jsonb_object_keys(p.rates) k WHERE p.id=c.sale_id;
 gap=period.uncertain;
 BEGIN
  -- The prepaid hold already secures this day's storage: release it so the day's own
  -- reservation does not need the same funds twice. A failure below restores it.
  released=billing.v1_storage_release(c.id,'storage_hold_applied');
  PERFORM billing.v1_reserve_tools(period.id,c.organization_id,c.project_id,c.service_account_id,c.api_key_id,c.model_id,
    0,0,86400,encode(sha256(convert_to(period.id::text,'UTF8')),'hex'),dims,period.day::timestamptz,c.node_id,c.cluster_id,
    c.revision,false,0,0,CASE WHEN gap THEN 0 ELSE period.byte_seconds END,c.id);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT IN ('account_frozen','insufficient_funds','budget_exceeded','price_unavailable','invalid_reserve_amount','currency_config_mismatch','invalid_bounds') THEN RAISE; END IF;
  UPDATE billing.vector_storage_days SET state='review',next_attempt=clock_timestamp()+interval '1 minute',
     reason=SQLERRM WHERE id=period.id;
  RETURN true;
 END;
 INSERT INTO billing.request_attempts(id,request_id,deployment_id,cost_price_version_id,sequence,expected_usage_format)
 VALUES(period.attempt_id,period.id,c.deployment_id,c.cost_id,1,'openai.vector_storage.v1');
 stamp=to_char(period.covered_until AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"');
 base=jsonb_build_object('schema_version',1,'request_id',period.id,'occurred_at',stamp,'config_revision',c.revision::text);
 answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','attempt.finished','attempt_id',period.attempt_id,
 'payload',jsonb_build_object('outcome',CASE WHEN gap THEN 'unknown' ELSE 'succeeded' END,'elapsed_ms',0,'upstream_request_id',c.id::text,
    'usage_format','openai.vector_storage.v1','raw_usage',CASE WHEN gap THEN NULL ELSE jsonb_build_object('vector_storage_byte_seconds',period.byte_seconds) END)))::text,c.node_id);
 IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','request.closed','attempt_id',NULL,
 'payload',jsonb_build_object('delivery_outcome',CASE WHEN gap THEN 'unknown' ELSE 'succeeded' END,'attempt_ids',jsonb_build_array(period.attempt_id),
    'delivered_attempt_id',CASE WHEN gap THEN NULL ELSE period.attempt_id END)))::text,c.node_id);
 IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 UPDATE billing.vector_storage_days SET state='staged' WHERE id=period.id;
 IF released THEN PERFORM billing.v1_storage_restore(c.id); END IF;
 RETURN true;
END $$;

CREATE FUNCTION billing.v1_status(rid uuid, node uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT jsonb_build_object('request_id',id,'node_id',node_id,'organization_id',organization_id,
 'api_key_id',api_key_id,'model_id',model_id,'config_revision',config_revision::text,'input_limit',input_limit,
 'output_limit',output_limit,'execution_mode',execution_mode,'execution_seconds_limit',execution_seconds_limit,'guardrail_unit_limit',guardrail_unit_limit,'web_search_limit',web_search_limit,'file_search_limit',file_search_limit,'storage_byte_seconds',storage_byte_seconds,'billing_policy_version',billing_policy_version,'state',state,'reservation_state',reservation_state,'reserved_amount',reserved_amount::text,
 'sale_price_version_id',sale_price_version_id,'discount_version_id',discount_version_id,'charged_amount',charged_amount::text,
 'review_reason',review_reason,'processing_state',(SELECT j.state FROM billing.usage_jobs j WHERE j.request_id=rid),
 'closed',EXISTS(SELECT FROM billing.usage_events e WHERE e.request_id=rid AND e.kind='request.closed'),
 'attempts',coalesce((SELECT jsonb_agg(jsonb_build_object('id',a.id,'sequence',a.sequence,'state',a.state)
 ORDER BY a.sequence) FROM billing.request_attempts a WHERE a.request_id=rid),'[]'::jsonb))
 FROM billing.request_runs WHERE id=rid AND node_id=node
$$;

CREATE FUNCTION billing.v1_storage_hold(handle uuid, until timestamp with time zone, bytes bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    SET "TimeZone" TO 'UTC'
    AS $$
DECLARE c billing.vector_storage_contracts; hold uuid; dims jsonb; span numeric;
BEGIN
 PERFORM billing.v1_storage_release(handle,'storage_hold_replaced');
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 span=greatest(0,ceil(extract(epoch FROM until-clock_timestamp())));
 IF span>0 AND bytes>0 THEN
  hold=gen_random_uuid();
  SELECT jsonb_agg(k) INTO dims FROM billing.price_versions p CROSS JOIN LATERAL jsonb_object_keys(p.rates) k WHERE p.id=c.sale_id;
  PERFORM billing.v1_reserve_tools(hold,c.organization_id,c.project_id,c.service_account_id,c.api_key_id,c.model_id,
    0,0,86400,encode(sha256(convert_to(hold::text,'UTF8')),'hex'),dims,clock_timestamp(),c.node_id,c.cluster_id,
    c.revision,false,0,0,bytes::numeric*span,c.id);
 END IF;
 UPDATE billing.vector_storage_contracts SET hold_id=hold,prepaid_until=until,prepaid_bytes=greatest(bytes,0) WHERE id=handle;
END $$;

CREATE FUNCTION billing.v1_storage_lapsed(handle uuid) RETURNS text
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT lapse_action FROM billing.vector_storage_contracts WHERE id=handle AND lapsed_at IS NOT NULL AND NOT closed
$$;

CREATE FUNCTION billing.v1_storage_missed(handle uuid, at_time timestamp with time zone) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE c billing.vector_storage_contracts;
BEGIN
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 PERFORM billing.v1_storage_sample(handle,at_time,c.usage_bytes,c.expires_at,false,true);
END $$;

CREATE FUNCTION billing.v1_storage_open(handle uuid, principal jsonb, kid uuid, model uuid, dep uuid, node uuid, cluster uuid, rev bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE org uuid := (principal->>'organization_id')::uuid; wallet billing.accounts; sale billing.price_versions;
 cost billing.price_versions; discount billing.discount_versions; now_at timestamptz:=clock_timestamp();
BEGIN
 SELECT * INTO STRICT wallet FROM billing.accounts WHERE organization_id=org FOR SHARE;
 IF wallet.status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 PERFORM revision FROM billing.source_revision WHERE id=1 FOR SHARE;
 SELECT * INTO sale FROM billing.price_versions WHERE kind='sale' AND scope_id=model AND state='published' AND effective_at<=now_at
 ORDER BY effective_at DESC LIMIT 1;
 SELECT * INTO cost FROM billing.price_versions WHERE kind='cost' AND scope_id=dep AND state='published' AND effective_at<=now_at
 ORDER BY effective_at DESC LIMIT 1;
 IF sale.id IS NULL OR NOT sale.enabled OR cost.id IS NULL OR NOT cost.enabled OR sale.currency<>wallet.currency
 OR cost.currency<>wallet.currency OR sale.model_id<>model OR cost.model_id<>model OR NOT sale.rates ? 'vector_storage_byte_seconds'
 OR sale.rates#>>'{vector_storage_byte_seconds,unit_quantity}' IS DISTINCT FROM '92771293593600'
 OR cost.rates#>>'{vector_storage_byte_seconds,unit_quantity}' IS DISTINCT FROM '92771293593600' THEN RAISE EXCEPTION 'price_unavailable'; END IF;
 PERFORM billing.v1_storage_writable(kid,org,(principal->>'project_id')::uuid,(sale.rates#>>'{vector_storage_byte_seconds,unit_price}')::numeric>0);
 SELECT * INTO STRICT discount FROM billing.discount_versions WHERE (organization_id=org OR organization_id IS NULL)
 AND state='published' AND effective_at<=now_at ORDER BY organization_id NULLS LAST,effective_at DESC LIMIT 1;
 INSERT INTO billing.vector_storage_contracts(id,node_id,cluster_id,revision,organization_id,project_id,service_account_id,api_key_id,model_id,deployment_id,sale_id,cost_id,discount_id)
 VALUES(handle,node,cluster,rev,org,(principal->>'project_id')::uuid,(principal->>'service_account_id')::uuid,kid,model,dep,sale.id,cost.id,discount.id);
END $$;

CREATE FUNCTION billing.v1_storage_release(handle uuid, reason text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE c billing.vector_storage_contracts;
BEGIN
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 IF c.hold_id IS NULL OR NOT EXISTS(SELECT FROM billing.request_runs WHERE id=c.hold_id AND reservation_state='held') THEN RETURN false; END IF;
 PERFORM billing.v1_cancel(c.hold_id,c.node_id,reason);
 RETURN true;
END $$;

CREATE FUNCTION billing.v1_storage_restore(handle uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE c billing.vector_storage_contracts;
BEGIN
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 BEGIN
  PERFORM billing.v1_storage_hold(handle,greatest(c.prepaid_until,clock_timestamp()),c.prepaid_bytes);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT IN ('account_frozen','insufficient_funds','budget_exceeded','invalid_reserve_amount','settlement_backlog') THEN RAISE; END IF;
  UPDATE billing.vector_storage_contracts SET hold_id=NULL,prepaid_until=least(prepaid_until,clock_timestamp()),
   renewal_failed_at=coalesce(renewal_failed_at,clock_timestamp()),renew_after=clock_timestamp() WHERE id=handle;
  INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),c.organization_id,
   'vector_storage.renewal_failed',handle,jsonb_build_object('reason',SQLERRM,'prepaid_until',clock_timestamp()));
 END;
END $$;

CREATE FUNCTION billing.v1_storage_sample(handle uuid, at_time timestamp with time zone, bytes bigint, expiry timestamp with time zone, closing boolean, uncertain boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    SET "TimeZone" TO 'UTC'
    AS $$
DECLARE c billing.vector_storage_contracts; start_at timestamptz; end_at timestamptz; until_at timestamptz; quantity numeric; gap boolean; day_id_in uuid;
BEGIN
 IF bytes<0 OR at_time IS NULL OR at_time>clock_timestamp()+interval '5 seconds' THEN RAISE EXCEPTION 'invalid_storage_sample'; END IF;
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 IF c.closed THEN
  IF NOT closing AND NOT uncertain AND at_time>=c.observed_at THEN
   UPDATE billing.vector_storage_contracts SET closed=false,segment=segment+1,observed_at=date_trunc('second',at_time),
      usage_bytes=bytes,expires_at=expiry,observation_unknown=false WHERE id=handle;
  END IF;
  RETURN;
 END IF;
 IF c.observed_at>at_time AND NOT closing THEN RETURN; END IF;
 -- Recording/splitting at whole seconds avoids double rounding at adjacent boundaries.
 at_time=date_trunc('second',greatest(c.observed_at,at_time));
 start_at=c.observed_at; end_at=least(at_time,coalesce(c.expires_at,at_time));
 gap=uncertain OR c.observation_unknown OR at_time-start_at>interval '5 minutes';
 IF closing AND expiry IS NOT NULL THEN end_at=least(end_at,expiry); END IF;
 IF NOT closing AND c.expires_at<at_time AND (expiry IS NULL OR expiry>c.expires_at) THEN
  -- A renewed store bridged a previously observed expiry without a supporting sample.
  gap=true; end_at=at_time;
 END IF;
 IF c.observed_at IS NULL AND uncertain THEN
  INSERT INTO billing.vector_storage_days(store_id,day,segment,byte_seconds,covered_until,state,uncertain,reason)
  VALUES(handle,at_time::date,c.segment,0,at_time,CASE WHEN closing THEN 'ready' ELSE 'open' END,true,
      CASE WHEN closing THEN 'submission_unknown' ELSE 'observation_gap' END) ON CONFLICT(store_id,day,segment) DO NOTHING;
 END IF;
 WHILE start_at<end_at AND (c.usage_bytes>0 OR gap) LOOP
  until_at=least(end_at,date_trunc('day',start_at)+interval '1 day');
  quantity=c.usage_bytes::numeric*extract(epoch FROM until_at-start_at);
  INSERT INTO billing.vector_storage_days(store_id,day,segment,byte_seconds,covered_until,state,uncertain,reason)
  VALUES(handle,start_at::date,c.segment,quantity,until_at,CASE WHEN closing OR until_at::date>start_at::date THEN 'ready' ELSE 'open' END,gap,
     CASE WHEN gap THEN 'observation_gap' END)
  ON CONFLICT(store_id,day,segment) DO UPDATE SET byte_seconds=billing.vector_storage_days.byte_seconds+excluded.byte_seconds,
      covered_until=excluded.covered_until,state=excluded.state,uncertain=billing.vector_storage_days.uncertain OR excluded.uncertain,
      reason=coalesce(billing.vector_storage_days.reason,excluded.reason) WHERE billing.vector_storage_days.state='open'
  RETURNING id INTO day_id_in;
  IF day_id_in IS NULL THEN RAISE EXCEPTION 'storage_period_conflict'; END IF;
  UPDATE billing.vector_storage_segments AS seg SET ended_at=until_at WHERE seg.day_id=day_id_in AND seg.ended_at=start_at
      AND seg.usage_bytes=c.usage_bytes AND seg.uncertain=gap;
  IF NOT FOUND THEN INSERT INTO billing.vector_storage_segments(day_id,started_at,ended_at,usage_bytes,uncertain)
    VALUES(day_id_in,start_at,until_at,c.usage_bytes,gap); END IF;
  start_at=until_at;
 END LOOP;
 UPDATE billing.vector_storage_days SET state='ready' WHERE store_id=handle AND state='open' AND (day<at_time::date OR closing);
 UPDATE billing.vector_storage_contracts SET observation_unknown=uncertain,observed_at=at_time,usage_bytes=bytes,expires_at=expiry,closed=closing WHERE id=handle;
 -- A closed store accrues nothing further; its remaining days settle from the released funds.
 IF closing THEN PERFORM billing.v1_storage_release(handle,'storage_closed'); END IF;
END $$;

CREATE FUNCTION billing.v1_storage_writable(kid uuid, org uuid, project uuid, paid boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    SET "TimeZone" TO 'UTC'
    AS $$
DECLARE wallet billing.accounts;
BEGIN
 SELECT * INTO STRICT wallet FROM billing.accounts WHERE organization_id=org FOR SHARE;
 IF wallet.status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 IF NOT paid THEN RETURN; END IF;
 IF wallet.balance<=wallet.held_amount OR EXISTS(SELECT FROM billing.vector_storage_days u JOIN billing.vector_storage_contracts c
   ON c.id=u.store_id WHERE c.organization_id=org AND u.state='review' AND u.reason='insufficient_funds') THEN RAISE EXCEPTION 'insufficient_funds'; END IF;
 IF EXISTS(SELECT FROM billing.vector_storage_days u JOIN billing.vector_storage_contracts c ON c.id=u.store_id
   WHERE c.api_key_id=kid AND u.state='review' AND u.reason='budget_exceeded') OR EXISTS(
    SELECT FROM billing.budget_policies p LEFT JOIN billing.budget_periods b ON b.policy_id=p.id AND b.period_start=date_trunc(p.period,clock_timestamp())
    WHERE p.organization_id=org AND p.status='active' AND (p.scope_type='organization' OR p.scope_type='project' AND p.scope_id=project
     OR p.scope_type='key' AND p.scope_id=kid) AND p.limit_amount<=coalesce(b.spent+b.reserved,0))
 THEN RAISE EXCEPTION 'budget_exceeded'; END IF;
END $$;

CREATE FUNCTION billing.v1_storage_write(handle uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 PERFORM billing.v1_storage_writable(c.api_key_id,c.organization_id,c.project_id,(p.rates#>>'{vector_storage_byte_seconds,unit_price}')::numeric>0)
 FROM billing.vector_storage_contracts c JOIN billing.price_versions p ON p.id=c.sale_id WHERE c.id=handle;
 -- New bytes would outgrow storage the wallet could not prepay.
 IF EXISTS(SELECT FROM billing.vector_storage_contracts WHERE id=handle AND (renewal_failed_at IS NOT NULL OR lapsed_at IS NOT NULL))
 THEN RAISE EXCEPTION 'insufficient_funds'; END IF;
END $$;

RESET ROLE;

-- Owned by nexus_migrator.
CREATE FUNCTION billing.advance_revision() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$ BEGIN
            UPDATE billing.source_revision SET revision=revision+1 WHERE id=1;
            RETURN NULL; END $$;

CREATE FUNCTION billing.announce_revision() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$ BEGIN
            PERFORM pg_notify('nexus_source_revision','billing'); RETURN NULL; END $$;

CREATE FUNCTION billing.bump_review_evidence() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$ BEGIN
 UPDATE billing.review_cases SET version=version+1 WHERE request_id=NEW.request_id AND state='open';
 RETURN NEW;
END $$;

CREATE FUNCTION billing.check_settlement_backlog() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE threshold integer; backlog integer; counted timestamptz;
BEGIN
    SELECT settlement_backlog_limit INTO STRICT threshold FROM billing.settings WHERE id=1;
    SELECT g.ready, g.observed_at INTO STRICT backlog, counted
    FROM billing.settlement_backlog g WHERE g.id=1;
    IF counted < clock_timestamp() - interval '100 milliseconds' THEN
        -- One reservation recounts; the others keep the last count instead of waiting.
        PERFORM FROM billing.settlement_backlog g WHERE g.id=1 FOR UPDATE SKIP LOCKED;
        IF FOUND THEN
            SELECT count(*) INTO backlog FROM (
                SELECT FROM billing.usage_jobs j WHERE j.state='ready' LIMIT threshold) due;
            UPDATE billing.settlement_backlog SET ready=backlog, observed_at=clock_timestamp()
            WHERE id=1;
        END IF;
    END IF;
    IF backlog >= threshold THEN
        RAISE EXCEPTION 'settlement_backlog';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION billing.close_cost_review() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 UPDATE billing.review_cases rc SET state='resolved',version=version+1,resolved_at=clock_timestamp()
 WHERE source_type='attempt' AND source_id=NEW.id AND state='open' AND
 (reason<>'statement_difference' OR NEW.cost_amount=(SELECT amount FROM billing.upstream_statements
 WHERE id=(rc.evidence->>'statement_id')::uuid));
 RETURN NEW;
END $$;

CREATE FUNCTION billing.default_payment_quote() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
          IF NEW.collection_amount IS NULL AND NEW.collection_currency IS NULL THEN
            NEW.collection_amount=NEW.amount;
            NEW.collection_currency=NEW.currency;
          END IF;
          RETURN NEW;
        END $$;

CREATE FUNCTION billing.dirty_corrected_cost() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 UPDATE billing.report_changes SET generation=generation+1,changed_at=clock_timestamp()
 WHERE request_id=(SELECT request_id FROM billing.request_attempts WHERE id=NEW.attempt_id);
 RETURN NEW;
END $$;

CREATE FUNCTION billing.mark_report_change() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE rid uuid;
BEGIN
 IF TG_TABLE_NAME='request_runs' THEN rid=NEW.id; ELSE rid=NEW.request_id; END IF;
 INSERT INTO billing.report_changes(request_id) VALUES(rid)
 ON CONFLICT(request_id) DO UPDATE SET generation=billing.report_changes.generation+1,
 changed_at=clock_timestamp();
 RETURN NEW;
END $$;

CREATE FUNCTION billing.protect_account_identity() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF ROW(OLD.id, OLD.organization_id, OLD.currency)
                IS DISTINCT FROM ROW(NEW.id, NEW.organization_id, NEW.currency)
            THEN RAISE EXCEPTION 'account identity is immutable' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_admission() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF ROW(OLD.organization_id, OLD.account_id, OLD.project_id,
                OLD.service_account_id, OLD.api_key_id, OLD.model_id, OLD.request_digest,
                OLD.sale_price_version_id, OLD.discount_version_id, OLD.billing_policy_version,
                OLD.input_limit, OLD.output_limit, OLD.reserved_amount,
                OLD.created_at, OLD.expires_at)
            IS DISTINCT FROM ROW(NEW.organization_id, NEW.account_id, NEW.project_id,
                NEW.service_account_id, NEW.api_key_id, NEW.model_id, NEW.request_digest,
                NEW.sale_price_version_id, NEW.discount_version_id, NEW.billing_policy_version,
                NEW.input_limit, NEW.output_limit, NEW.reserved_amount,
                NEW.created_at, NEW.expires_at) THEN
                RAISE EXCEPTION 'request admission is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.reservation_state <> 'held' AND
                ROW(OLD.state, OLD.reservation_state, OLD.charged_amount,
                    OLD.settlement_digest, OLD.charge_details, OLD.settled_at)
                IS DISTINCT FROM
                ROW(NEW.state, NEW.reservation_state, NEW.charged_amount,
                    NEW.settlement_digest, NEW.charge_details, NEW.settled_at) THEN
                RAISE EXCEPTION 'settlement is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_attempt() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF OLD.state='confirmed' OR
                ROW(OLD.request_id, OLD.deployment_id, OLD.cost_price_version_id)
                IS DISTINCT FROM
                ROW(NEW.request_id, NEW.deployment_id, NEW.cost_price_version_id) THEN
                RAISE EXCEPTION 'attempt evidence is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_batch_attempt() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$ BEGIN
        IF EXISTS(SELECT FROM billing.request_runs WHERE id=NEW.request_id
                  AND billing_policy_version='anthropic_message_batch_v1') THEN
            RAISE EXCEPTION 'invalid_attempt_sequence';
        END IF;
        RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_gateway_binding() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 IF ROW(OLD.cluster_id,OLD.node_id,OLD.config_revision) IS DISTINCT FROM ROW(NEW.cluster_id,NEW.node_id,NEW.config_revision)
 THEN RAISE EXCEPTION 'immutable gateway binding'; END IF; RETURN NEW;
END $$;

CREATE FUNCTION billing.protect_payment_channel() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF ROW(OLD.id,OLD.adapter_type,OLD.stable_merchant_id,OLD.environment,
                   OLD.receipt_namespace,OLD.currency)
                IS DISTINCT FROM ROW(NEW.id,NEW.adapter_type,NEW.stable_merchant_id,NEW.environment,
                   NEW.receipt_namespace,NEW.currency) THEN
                RAISE EXCEPTION 'payment channel binding is immutable' USING ERRCODE='23514';
            END IF; RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_payment_config() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF ROW(OLD.id,OLD.channel_id,OLD.version,OLD.key_version,OLD.ciphertext,
                   OLD.public_configuration,OLD.capabilities,OLD.created_at)
                IS DISTINCT FROM ROW(NEW.id,NEW.channel_id,NEW.version,NEW.key_version,
                   NEW.ciphertext,
                   NEW.public_configuration,NEW.capabilities,NEW.created_at) THEN
                RAISE EXCEPTION 'payment config is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
                RAISE EXCEPTION 'revoked payment credentials cannot be restored'
                    USING ERRCODE='23514';
            END IF; RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_payment_event() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF ROW(OLD.id,OLD.channel_id,OLD.verification_version_id,OLD.receipt_namespace,
                OLD.order_id,OLD.operation_id,OLD.wake_version,OLD.event_key,OLD.event_fingerprint,
                OLD.body_digest,OLD.verified_fields,OLD.created_at)
            IS DISTINCT FROM ROW(NEW.id,NEW.channel_id,NEW.verification_version_id,
                NEW.receipt_namespace,NEW.order_id,NEW.operation_id,NEW.wake_version,
                NEW.event_key,NEW.event_fingerprint,NEW.body_digest,NEW.verified_fields,NEW.created_at)
            THEN RAISE EXCEPTION 'payment notice is immutable' USING ERRCODE='23514'; END IF;
            IF OLD.processing_state IN ('processed','ignored')
                AND NEW.processing_state <> OLD.processing_state THEN
                RAISE EXCEPTION 'completed notice cannot regress' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_payment_operation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF ROW(OLD.id,OLD.order_id,OLD.kind,OLD.operation_key,OLD.idempotency_key,
                OLD.target_provider_ref) IS DISTINCT FROM ROW(NEW.id,NEW.order_id,NEW.kind,
                NEW.operation_key,NEW.idempotency_key,NEW.target_provider_ref)
                OR NEW.fencing_token < OLD.fencing_token THEN
                RAISE EXCEPTION 'payment operation binding is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.remote_started_at IS NOT NULL AND ROW(OLD.remote_started_at,
                OLD.idempotency_mode,OLD.idempotency_expires_at) IS DISTINCT FROM
                ROW(NEW.remote_started_at,NEW.idempotency_mode,NEW.idempotency_expires_at) THEN
                RAISE EXCEPTION 'original creation intent is immutable' USING ERRCODE='23514';
            END IF; RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_payment_order() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF ROW(OLD.id,OLD.organization_id,OLD.account_id,OLD.channel_id,OLD.config_version_id,
                   OLD.merchant_order_ref,OLD.stable_merchant_id,OLD.environment,
                   OLD.receipt_namespace,
                   OLD.amount,OLD.credit_amount,OLD.currency,OLD.created_by,OLD.callback_url,
                   OLD.return_url,OLD.expires_at,OLD.created_at)
                IS DISTINCT FROM ROW(NEW.id,NEW.organization_id,NEW.account_id,NEW.channel_id,
                   NEW.config_version_id,NEW.merchant_order_ref,NEW.stable_merchant_id,NEW.environment,
                   NEW.receipt_namespace,NEW.amount,NEW.credit_amount,NEW.currency,NEW.created_by,
                   NEW.callback_url,NEW.return_url,NEW.expires_at,NEW.created_at) THEN
                RAISE EXCEPTION 'payment expectation is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.credit_state='credited' AND ROW(OLD.credit_state,OLD.funding_record_id,
                OLD.ledger_entry_id,OLD.credited_at) IS DISTINCT FROM ROW(NEW.credit_state,
                NEW.funding_record_id,NEW.ledger_entry_id,NEW.credited_at) THEN
                RAISE EXCEPTION 'original payment credit is immutable' USING ERRCODE='23514';
            END IF; RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_posted_record() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF OLD.status = 'posted' THEN
                RAISE EXCEPTION 'posted record is immutable' USING ERRCODE='23514';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_published_version() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF OLD.state = 'published' THEN
                RAISE EXCEPTION 'published version is immutable' USING ERRCODE='23514';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_receiving_identity() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            IF ROW(OLD.id, OLD.reference, OLD.receipt_namespace, OLD.currency)
                IS DISTINCT FROM ROW(NEW.id, NEW.reference, NEW.receipt_namespace, NEW.currency)
            THEN RAISE EXCEPTION 'receipt namespace is immutable' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$;

CREATE FUNCTION billing.protect_search_limit() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN RAISE EXCEPTION 'request admission is immutable' USING ERRCODE='23514'; END $$;

CREATE FUNCTION billing.reject_immutable_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN
            RAISE EXCEPTION 'immutable billing fact' USING ERRCODE='23514';
        END $$;

CREATE FUNCTION billing.track_account_check() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 INSERT INTO billing.account_checks(organization_id) VALUES(NEW.organization_id)
 ON CONFLICT(organization_id) DO NOTHING;
 RETURN NEW;
END $$;

CREATE FUNCTION billing.track_request_review() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 IF NEW.state='review' THEN
  INSERT INTO billing.review_cases(organization_id,source_type,source_id,request_id,reason)
  VALUES(NEW.organization_id,'request',NEW.id,NEW.id,coalesce(NEW.review_reason,'unknown'))
  ON CONFLICT(source_type,source_id) DO UPDATE SET reason=excluded.reason,state='open',
  resolved_at=NULL,resolution_id=NULL,version=billing.review_cases.version+1
  WHERE billing.review_cases.state<>'open' OR billing.review_cases.reason<>excluded.reason;
 ELSIF NEW.reservation_state<>'held' THEN
  UPDATE billing.review_cases SET state='resolved',resolved_at=clock_timestamp(),version=version+1
  WHERE source_type='request' AND source_id=NEW.id AND state='open';
 END IF;
 IF NEW.state='review' OR NEW.reservation_state<>'held' THEN
  INSERT INTO billing.review_cases(organization_id,source_type,source_id,request_id,reason)
  SELECT NEW.organization_id,'attempt',a.id,NEW.id,'missing_attempt_cost'
  FROM billing.request_attempts a WHERE a.request_id=NEW.id AND a.state<>'confirmed'
  AND NOT EXISTS(SELECT FROM billing.cost_corrections c WHERE c.attempt_id=a.id)
  ON CONFLICT(source_type,source_id) DO NOTHING;
 END IF;
 RETURN NEW;
END $$;

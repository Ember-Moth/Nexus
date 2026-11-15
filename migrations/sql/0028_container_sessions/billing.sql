CREATE TABLE billing.container_sessions(
 id uuid PRIMARY KEY,handle uuid NOT NULL,organization_id uuid NOT NULL,api_key_id uuid NOT NULL,model_id uuid NOT NULL,deployment_id uuid NOT NULL,
 supplier_id uuid NOT NULL,node_id uuid NOT NULL,cluster_id uuid NOT NULL,revision bigint NOT NULL,attempt_id uuid NOT NULL UNIQUE,cost_price_id uuid NOT NULL REFERENCES billing.price_versions,
 memory_tier text NOT NULL,tariff text NOT NULL,state text NOT NULL CHECK(state IN ('provisional','active','review','released','settled')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),last_seen timestamptz NOT NULL DEFAULT clock_timestamp(),review_after timestamptz NOT NULL,
 explicit_creation boolean NOT NULL,tier_verified boolean NOT NULL DEFAULT false,completed_call boolean NOT NULL DEFAULT false,closed_at timestamptz,provider_id text
);
CREATE UNIQUE INDEX container_open_meter ON billing.container_sessions(handle) WHERE state IN ('provisional','active','review');
CREATE INDEX container_idle_meter ON billing.container_sessions(review_after) WHERE state IN ('provisional','active');
CREATE TABLE billing.container_operations(
 origin uuid PRIMARY KEY,meter uuid NOT NULL REFERENCES billing.container_sessions,api_key_id uuid NOT NULL,requested_handle uuid,
 deployment_id uuid NOT NULL, provisional boolean NOT NULL,finished boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION billing.v1_container_hold(origin uuid,existing uuid,principal jsonb,kid uuid,model uuid,dep uuid,dims jsonb,
 tier text,tariff text,minutes integer,node uuid,cluster uuid,revision bigint,fresh boolean,supplier uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE org uuid:=(principal->>'organization_id')::uuid; m billing.container_sessions; op billing.container_operations;
 cost billing.price_versions; r billing.request_runs; at_time timestamptz:=clock_timestamp();
 wallet_status text; unit_name text:='container_'||tier||'_minutes'; cap_id uuid:=coalesce(existing,origin); new_meter boolean:=false;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(org::text,398221));
 SELECT a.status INTO STRICT wallet_status FROM billing.accounts a WHERE a.organization_id=org FOR UPDATE;
 IF wallet_status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 SELECT * INTO op FROM billing.container_operations WHERE container_operations.origin=$1;
 IF FOUND THEN
  IF op.api_key_id<>kid OR op.deployment_id<>dep OR op.requested_handle IS DISTINCT FROM existing THEN RAISE EXCEPTION 'request_conflict'; END IF;
  RETURN;
 END IF;
 SELECT * INTO m FROM billing.container_sessions WHERE handle=cap_id AND state IN ('provisional','active','review') FOR UPDATE;
 IF FOUND THEN
  IF m.api_key_id<>kid OR m.deployment_id<>dep OR m.memory_tier<>tier THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 ELSE
  m.id=gen_random_uuid();new_meter=true;
  PERFORM billing.v1_reserve_capacity(m.id,org,(principal->>'project_id')::uuid,(principal->>'service_account_id')::uuid,kid,model,
    0,0,86400,encode(sha256(convert_to(origin::text||':'||unit_name,'UTF8')),'hex'),dims,at_time,node,cluster,revision,false,0,0,0,NULL,'none',0,unit_name,minutes,tariff);
  UPDATE billing.request_runs SET container_origin=origin,container_handle=CASE WHEN existing IS NOT NULL THEN 'cntr_nx_'||existing::text END WHERE id=m.id;
  SELECT * INTO STRICT r FROM billing.request_runs WHERE id=m.id;
  SELECT * INTO cost FROM billing.price_versions p WHERE p.kind='cost' AND p.scope_id=dep AND p.state='published' AND p.effective_at<=at_time ORDER BY p.effective_at DESC LIMIT 1;
  IF cost.id IS NULL OR NOT cost.enabled OR cost.model_id<>model OR cost.currency IS DISTINCT FROM (SELECT p.currency FROM billing.price_versions p WHERE p.id=r.sale_price_version_id)
    OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(cost.rates) k) IS DISTINCT FROM (SELECT array_agg(k ORDER BY k) FROM jsonb_array_elements_text(dims) k)
    OR cost.rates->unit_name->>'unit_quantity' NOT IN ('1','20') THEN RAISE EXCEPTION 'cost_unavailable'; END IF;
  INSERT INTO billing.container_sessions(id,handle,organization_id,api_key_id,model_id,deployment_id,supplier_id,node_id,cluster_id,revision,attempt_id,cost_price_id,
     memory_tier,tariff,state,explicit_creation,review_after) VALUES(m.id,cap_id,org,kid,model,dep,supplier,node,cluster,revision,gen_random_uuid(),cost.id,
     tier,tariff,'provisional',fresh,clock_timestamp()+interval '3 minutes');
 END IF;
 INSERT INTO billing.container_operations(origin,meter,api_key_id,requested_handle,deployment_id,provisional)
 VALUES(origin,m.id,kid,existing,dep,new_meter);
END $$;
CREATE FUNCTION billing.v1_container_link(origin_id uuid,canonical uuid,kid uuid,dep uuid,pid text,minutes integer,executed boolean,actual_tier text,verified_tier boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE op billing.container_operations; m billing.container_sessions; prior billing.container_sessions;
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
 IF m.handle<>canonical THEN
  SELECT * INTO prior FROM billing.container_sessions WHERE handle=canonical AND state IN ('active','review','provisional') FOR UPDATE;
  IF FOUND THEN
   IF prior.api_key_id<>kid OR prior.deployment_id<>dep OR prior.memory_tier<>actual_tier OR m.state<>'provisional'
   THEN RAISE EXCEPTION 'resource_conflict'; END IF;
   PERFORM billing.v1_cancel(m.id,m.node_id,'container_reused');
   UPDATE billing.container_sessions SET state='released' WHERE id=m.id;
   UPDATE billing.container_operations SET meter=prior.id,provisional=false WHERE origin=origin_id;
   m=prior;
  END IF;
 END IF;
 IF m.memory_tier<>actual_tier THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 IF m.state NOT IN ('provisional','active','review') THEN
  IF op.finished AND m.handle=canonical AND m.provider_id=pid THEN RETURN; END IF;
  RAISE EXCEPTION 'resource_conflict';
 END IF;
 UPDATE billing.request_runs SET container_handle='cntr_nx_'||canonical::text WHERE id=m.id AND container_handle IS NULL;
 UPDATE billing.container_sessions SET handle=canonical,provider_id=pid,tier_verified=tier_verified OR verified_tier,completed_call=completed_call OR executed,state=CASE WHEN state='review' THEN state ELSE 'active' END,
 last_seen=clock_timestamp(),review_after=clock_timestamp()+make_interval(mins=>least(1440,greatest(CASE WHEN m.tariff='minute_min_5' THEN 5 ELSE 20 END,coalesce(minutes,20))))
 WHERE id=m.id;
 UPDATE billing.container_operations SET finished=true WHERE origin=origin_id;
END $$;
CREATE FUNCTION billing.v1_container_release(origin_id uuid,kid uuid,dep uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION billing.v1_container_due(handle_id uuid,kid uuid,confirmed boolean) RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 UPDATE billing.container_sessions SET review_after=clock_timestamp(),closed_at=CASE WHEN confirmed THEN clock_timestamp() ELSE NULL END WHERE handle=handle_id AND api_key_id=kid AND state IN ('active','provisional')
$$;
-- Expiry and deletion schedule evidence review. Neither last_active_at nor the idle
-- timeout is a supplier duration receipt. Preserve the hold until reconciliation.
CREATE FUNCTION billing.v1_review_container() RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE m billing.container_sessions; base jsonb; answer jsonb; minimum integer; measured boolean; usage jsonb;
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
 UPDATE billing.container_sessions SET state='review' WHERE id=m.id;
 RETURN true;
END $$;
CREATE FUNCTION billing.v1_container_settled() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 UPDATE billing.container_sessions SET state='settled' WHERE id=NEW.id AND state IN ('review','active','provisional');
 RETURN NEW;
END $$;

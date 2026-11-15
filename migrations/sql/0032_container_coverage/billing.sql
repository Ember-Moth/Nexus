-- Supplier minutes billable for a container alive for `span` under the frozen tariff.
CREATE FUNCTION billing.v1_container_minutes(tariff text,span interval) RETURNS integer
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT CASE WHEN tariff='session_20m' THEN 20*greatest(1,ceil(extract(epoch FROM span)/1200))::integer
  ELSE greatest(5,ceil(extract(epoch FROM span)/60))::integer END
$$;
-- The last moment an admitted operation can still touch the container: its request's
-- reservation deadline, or fifteen minutes for a direct container call (the interactive TTL).
CREATE FUNCTION billing.v1_container_reach(origin_id uuid) RETURNS timestamptz
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT greatest(clock_timestamp(),coalesce((SELECT r.expires_at FROM billing.request_runs r
   WHERE r.id=origin_id AND r.reservation_state='held'),clock_timestamp()+interval '15 minutes'))
$$;
-- Hold enough funds for every minute the supplier may bill until `reach`. Callers hold the
-- wallet lock. The meter's own reservation and each extension are separate immutable holds;
-- a failed reservation raises its admission error and leaves the coverage unchanged.
CREATE FUNCTION billing.v1_container_cover(meter uuid,origin_id uuid,reach timestamptz,dims jsonb,step integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
-- After the supplier executed an operation, coverage can no longer be refused. If it cannot be
-- reserved, freeze the wallet with the meter as its source so no further exposure is admitted.
CREATE FUNCTION billing.v1_container_settle_cover(meter uuid,origin_id uuid,reach timestamptz,step integer) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE OR REPLACE FUNCTION billing.v1_container_hold(origin uuid,existing uuid,principal jsonb,kid uuid,model uuid,dep uuid,dims jsonb,
 tier text,tariff text,minutes integer,node uuid,cluster uuid,revision bigint,fresh boolean,supplier uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
END $$;
CREATE OR REPLACE FUNCTION billing.v1_container_link(origin_id uuid,canonical uuid,kid uuid,dep uuid,pid text,minutes integer,executed boolean,actual_tier text,verified_tier boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
-- Expiry and deletion schedule evidence review. Neither last_active_at nor the idle
-- timeout is a supplier duration receipt. Preserve the hold until reconciliation.
-- Extension holds whose window starts after the container provably ended are released.
CREATE OR REPLACE FUNCTION billing.v1_review_container() RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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

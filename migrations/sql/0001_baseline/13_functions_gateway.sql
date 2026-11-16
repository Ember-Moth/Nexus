-- gateway_v1: the SQL contract of Relay nodes (docs/plane-protocol.md).
-- Created by their owning role, so their privileges are exactly the grants in 17_privileges.sql.

-- Owned by nexus_bridge.
SET LOCAL ROLE nexus_bridge;

CREATE FUNCTION gateway_v1.ack_config(revision bigint, hash text, applied boolean, reason text DEFAULT ''::text) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$SELECT publication.v1_ack(revision,hash,applied,reason)$$;

CREATE FUNCTION gateway_v1.admit(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, batch boolean, searches integer, files integer, exec_mode text, exec_bound integer, guard_dep uuid, guard_units integer, aid uuid, dep uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals; adm jsonb; snap jsonb; k jsonb; principal jsonb; dims jsonb;
 previous jsonb; org uuid; attempt jsonb; target record;
BEGIN
 IF (aid IS NULL)<>(dep IS NULL) THEN RAISE EXCEPTION 'invalid_attempt'; END IF;
 p=publication.v1_principal();
 PERFORM pg_advisory_xact_lock(hashtextextended(rid::text,87412));
 previous=billing.v1_status(rid,p.node_id);
 IF previous IS NOT NULL THEN
  IF (previous->>'guardrail_unit_limit')::int IS DISTINCT FROM guard_units OR previous->>'execution_mode' IS DISTINCT FROM exec_mode OR (previous->>'execution_seconds_limit')::int IS DISTINCT FROM exec_bound OR (previous->>'billing_policy_version')='openai_vector_storage_v1' OR (previous->>'file_search_limit')::int IS DISTINCT FROM files OR (previous->>'web_search_limit')::int IS DISTINCT FROM searches OR billing.v1_batch_pricing(rid,p.node_id) IS DISTINCT FROM batch OR (previous->>'api_key_id')::uuid IS DISTINCT FROM kid OR (previous->>'model_id')::uuid IS DISTINCT FROM model
   OR (previous->>'config_revision')::bigint IS DISTINCT FROM rev OR (previous->>'input_limit')::bigint IS DISTINCT FROM inputs
   OR (previous->>'output_limit')::bigint IS DISTINCT FROM outputs THEN RAISE EXCEPTION 'request_conflict'; END IF;
  IF aid IS NULL THEN RETURN previous || '{"replayed":true}'; END IF;
  attempt=gateway_v1.open_attempt(p,previous,aid,1,dep,NULL);
  RETURN billing.v1_status(rid,p.node_id) || jsonb_build_object('replayed',true,'attempt',attempt);
 END IF;
 adm=publication.v1_admission(p,rev,kid,NULL,model,true);
 k=adm->'key';
 IF jsonb_typeof(k) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'key_outside_cluster'; END IF;
 snap=adm->'manifest';
 principal=identity.v1_authorize(kid,model); org=(principal->>'organization_id')::uuid;
 IF k->>'organization_id' IS DISTINCT FROM org::text OR k->>'project_id' IS DISTINCT FROM principal->>'project_id'
   OR k->>'service_account_id' IS DISTINCT FROM principal->>'service_account_id'
 OR NOT EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'access_policies') a
   WHERE a->>'organization_id'=org::text AND a->>'scope_type'='organization' AND a->>'layer'='platform')
 OR EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'access_policies') a
   WHERE a->>'organization_id'=org::text AND (a->>'scope_type'='organization' OR
    (a->>'scope_type'='project' AND a->>'scope_id'=principal->>'project_id') OR
    (a->>'scope_type'='key' AND a->>'scope_id'=kid::text))
   AND NOT ((a->'allowed_models') ? model::text)) THEN RAISE EXCEPTION 'snapshot_forbidden'; END IF;
 IF guard_units>0 THEN
  dims=catalog.v1_eligible(model,guard_dep,snap,org,inputs,outputs);
  IF guard_dep IS NULL OR NOT EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'deployments') d
    WHERE d->>'id'=guard_dep::text AND d#>>'{relay_profile,endpoint}'='bedrock_converse'
     AND coalesce((d#>>'{relay_profile,bedrock_guardrail_unit_ceiling}')::integer,0)=guard_units
     AND (jsonb_typeof(d#>'{relay_profile,bedrock_guardrail}')='object' OR EXISTS (
        SELECT FROM jsonb_array_elements(d#>'{relay_profile,bedrock_request_options}') opt
        WHERE jsonb_typeof(opt->'guardrailConfig')='object')))
  THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 ELSIF exec_mode<>'none' THEN
  dims=catalog.v1_execution_eligible(model,snap,org,inputs,outputs,searches,exec_mode,exec_bound);
 ELSIF files>0 THEN
  dims=catalog.v1_file_search_eligible(model,snap,org,inputs,outputs,files,searches);
 ELSIF searches>0 THEN
  dims=catalog.v1_search_eligible(model,snap,org,inputs,outputs,searches);
 ELSE
  -- With a first attempt this also checks its deployment.
  dims=catalog.v1_eligible(model,dep,snap,org,inputs,outputs);
 END IF;
 IF aid IS NOT NULL THEN
  -- The tool variants chose their dimensions from any deployment; check the attempt's own.
  IF guard_units=0 AND (exec_mode<>'none' OR files>0 OR searches>0) THEN
   PERFORM catalog.v1_eligible(model,dep,snap,org,inputs,outputs);
  END IF;
  SELECT * INTO target FROM gateway_v1.attempt_target(snap,dep,guard_units,exec_mode,exec_bound,files,searches);
  IF NOT FOUND OR (guard_units>0 AND dep IS DISTINCT FROM guard_dep) THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
  IF p.applied_revision<rev OR EXISTS(SELECT FROM jsonb_array_elements_text(adm->'required_capabilities') cap
     WHERE NOT cap=ANY(p.capabilities)) THEN RAISE EXCEPTION 'configuration_not_applied'; END IF;
 END IF;
 PERFORM billing.v1_reserve_guarded_capacity(rid,org,(principal->>'project_id')::uuid,(principal->>'service_account_id')::uuid,kid,
 model,inputs,outputs,CASE WHEN batch THEN 90000 ELSE 900 END,encode(sha256(convert_to(jsonb_build_array(kid,model,rev,inputs,outputs,p.node_id)::text,'UTF8')),'hex'),
 dims,clock_timestamp(),p.node_id,p.cluster_id,rev,batch,searches,files,0,NULL,exec_mode,exec_bound,NULL,0,NULL,guard_units);
 IF aid IS NOT NULL THEN
  attempt=billing.v1_attempt(org,rid,aid,dep,model,1,clock_timestamp(),p.node_id,target.expected_format);
  IF exec_mode IN ('metered','preloaded') OR target.expected_format='aws.bedrock.converse.v1' THEN
   PERFORM billing.v1_execution_supplier(rid,aid,p.node_id,target.supplier);
  END IF;
 END IF;
 RETURN billing.v1_status(rid,p.node_id) || jsonb_build_object('replayed',false,'attempt',attempt);
END $$;

CREATE FUNCTION gateway_v1.append_usage_event(message text) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$SELECT billing.v1_append(message,(publication.v1_principal()).node_id)$$;

CREATE FUNCTION gateway_v1.attempt_target(snap jsonb, dep uuid, guard_units integer, exec_mode text, exec_bound integer, files integer, searches integer) RETURNS TABLE(expected_format text, supplier uuid)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT d->>'usage_format',(d->>'upstream_id')::uuid
 FROM jsonb_array_elements(snap->'resources'->'deployments') d WHERE d->>'id'=dep::text
 AND (CASE WHEN guard_units>0
     THEN d#>>'{relay_profile,endpoint}'='bedrock_converse' AND
       coalesce((d#>>'{relay_profile,bedrock_guardrail_unit_ceiling}')::integer,0) BETWEEN 1 AND guard_units
     ELSE NOT(coalesce(jsonb_typeof(d#>'{relay_profile,bedrock_guardrail}')='object',false)
          AND coalesce((d#>>'{relay_profile,bedrock_guardrail_unit_ceiling}')::integer,0)>0) END)
 AND (exec_mode='none' OR (d#>>'{relay_profile,endpoint}'='anthropic_messages' AND (d#>>'{relay_profile,code_execution}'='true' OR (exec_mode='bundled' AND searches>0))
       AND coalesce((d#>>'{relay_profile,execution_reserve_seconds}')::int,0)>=exec_bound))
 AND coalesce((d#>>'{relay_profile,file_search_ceiling}')::int,0)>=files
 AND coalesce((d#>>'{relay_profile,web_search_ceiling}')::int,0)>=searches
$$;

CREATE FUNCTION gateway_v1.background_claim() RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$SELECT publication.v1_background_claim()$$;

CREATE FUNCTION gateway_v1.background_finish(handle_id uuid, outcome text, delivery text, usage jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE j jsonb; base jsonb; result jsonb; node uuid;
BEGIN
 j=publication.v1_job(handle_id); IF j IS NULL THEN RAISE EXCEPTION 'not_found'; END IF;
 IF (j->>'done')::boolean THEN RETURN; END IF;
 node=(publication.v1_principal()).node_id;
 base=jsonb_build_object('request_id',j->'rid','schema_version',1,'config_revision',j->>'revision',
   'occurred_at',to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'));
 result=billing.v1_append((base||jsonb_build_object('event_id',j->'attempt_event','event_type','attempt.finished','attempt_id',j->'aid',
 'payload',jsonb_build_object('outcome',outcome,'upstream_request_id',j->>'provider','usage_format','openai.responses.v1',
 'elapsed_ms',least(4294967295,greatest(0,floor(extract(epoch FROM clock_timestamp()-(j->>'created_at')::timestamptz)*1000)))::bigint,'raw_usage',usage)))::text,node);
 IF result->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 result=billing.v1_append((base||jsonb_build_object('event_id',j->'close_event','event_type','request.closed','attempt_id',NULL,
 'payload',jsonb_build_object('delivery_outcome',delivery,'attempt_ids',jsonb_build_array(j->'aid'),
 'delivered_attempt_id',CASE WHEN delivery IN ('succeeded','client_cancelled') THEN j->'aid' ELSE NULL END)))::text,node);
 IF result->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 PERFORM publication.v1_job_done(handle_id);
END $$;

CREATE FUNCTION gateway_v1.background_register(handle uuid, rid uuid, aid uuid, pid text, e1 uuid, e2 uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE f jsonb;
BEGIN
 f=billing.v1_response_attempt(rid,aid,(publication.v1_principal()).node_id);
 IF f IS NULL OR jsonb_array_length(billing.v1_status(rid,(publication.v1_principal()).node_id)->'attempts')<>1
 THEN RAISE EXCEPTION 'invalid_attempt_set'; END IF;
 PERFORM gateway_v1.save_response(handle,rid,aid,pid);
 PERFORM publication.v1_background_register(handle,rid,aid,(f->>'revision')::bigint,e1,e2);
END $$;

CREATE FUNCTION gateway_v1.batch_claim() RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$SELECT publication.v1_batch_claim()$$;

CREATE FUNCTION gateway_v1.batch_execution(batch uuid, custom text, kid uuid, rev bigint) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE item jsonb; req jsonb;
BEGIN
 PERFORM gateway_v1.batch_resolve(batch,kid,rev);
 item=publication.v1_batch_execution(batch,custom,kid);
 IF item IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 req=billing.v1_status((item->>'rid')::uuid,(item->>'node')::uuid);
 RETURN item||jsonb_build_object('execution',req->>'execution_mode'<>'none');
END $$;

CREATE FUNCTION gateway_v1.batch_finish(handle uuid, pid text, meta jsonb, results jsonb, fallback text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE job jsonb; i record; item jsonb; base jsonb; event jsonb; outcome text; delivery text; n integer := 0;
    node uuid := (publication.v1_principal()).node_id;
BEGIN
 job=publication.v1_batch_lock(handle);
 IF job IS NULL OR job->>'provider_id' IS DISTINCT FROM pid THEN RAISE EXCEPTION 'not_found'; END IF;
 IF (job->>'done')::boolean THEN RETURN; END IF;
 IF fallback IS DISTINCT FROM 'unknown' AND fallback IS DISTINCT FROM 'failed' THEN RAISE EXCEPTION 'invalid_outcome'; END IF;
 IF meta IS NOT NULL AND pid IS NOT NULL THEN PERFORM publication.v1_batch_update(handle,pid,meta,false); END IF;
 FOR i IN SELECT * FROM publication.v1_batch_items(handle) ORDER BY rid LOOP
  item=results->i.custom_id; outcome=coalesce(item->>'outcome',fallback);
  delivery=CASE outcome WHEN 'succeeded' THEN 'succeeded' WHEN 'failed' THEN 'upstream_failed'
     WHEN 'cancelled' THEN 'client_cancelled' ELSE 'unknown' END;
  base=jsonb_build_object('request_id',i.rid,'schema_version',1,'config_revision',job->>'revision',
      'occurred_at',to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'));
  event=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','attempt.finished','attempt_id',i.aid,
    'payload',jsonb_build_object('outcome',outcome,'upstream_request_id',pid,'usage_format','anthropic.messages.v1',
      'elapsed_ms',least(4294967295,greatest(0,floor(extract(epoch FROM clock_timestamp()-(job->>'created_at')::timestamptz)*1000)))::bigint,
      'raw_usage',item->'usage')))::text,node);
  IF event->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
  event=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','request.closed','attempt_id',NULL,
    'payload',jsonb_build_object('delivery_outcome',delivery,'attempt_ids',jsonb_build_array(i.aid),
      'delivered_attempt_id',CASE WHEN delivery IN ('succeeded','client_cancelled') THEN i.aid ELSE NULL END)))::text,node);
  IF event->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
  IF item IS NOT NULL THEN n=n+1; END IF;
 END LOOP;
 IF n<>(SELECT count(*) FROM jsonb_object_keys(results)) THEN RAISE EXCEPTION 'unexpected_batch_item'; END IF;
 PERFORM publication.v1_batch_done(handle);
END $$;

CREATE FUNCTION gateway_v1.batch_page(kid uuid, rev bigint, stamp uuid, newer boolean, n integer) RETURNS SETOF jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,NULL,NULL);
 RETURN QUERY SELECT * FROM publication.v1_batch_page(kid,stamp,newer,n);
END $$;

CREATE FUNCTION gateway_v1.batch_prepare(handle uuid, kid uuid, rev bigint, dep uuid, items jsonb, h jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE manifest jsonb; d jsonb; i record; reserved jsonb;
BEGIN
 manifest=publication.v1_snapshot(rev,true)->'manifest';
 SELECT value INTO STRICT d FROM jsonb_array_elements(manifest->'resources'->'deployments') WHERE value->>'id'=dep::text
 AND value#>>'{relay_profile,endpoint}'='anthropic_messages';
 IF jsonb_array_length(items) NOT BETWEEN 1 AND 100000 OR jsonb_typeof(h)<>'object'
 OR h-'anthropic-beta'-'anthropic-version'<>'{}' OR octet_length(h::text)>8192 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 PERFORM publication.v1_batch_prepare(handle,kid,rev,d,items,h);
 FOR i IN SELECT * FROM jsonb_to_recordset(items) AS t(rid uuid,aid uuid,model uuid,deployment uuid,inputs bigint,outputs bigint,searches integer,execution_mode text,execution_seconds integer) LOOP
  IF NOT EXISTS(SELECT FROM jsonb_array_elements(manifest->'resources'->'deployments') target WHERE
    target->>'id'=i.deployment::text AND target->>'model_id'=i.model::text
    AND target#>>'{relay_profile,endpoint}'='anthropic_messages' AND target->>'upstream_id'=d->>'upstream_id'
    AND target->>'credential_id'=d->>'credential_id' AND target->>'credential_version'=d->>'credential_version')
    THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
  reserved=gateway_v1.reserve_with_execution(i.rid,kid,i.model,rev,i.inputs,i.outputs,true,coalesce(i.searches,0),0,coalesce(i.execution_mode,'none'),coalesce(i.execution_seconds,0));
  IF (reserved->>'replayed')::boolean THEN RAISE EXCEPTION 'request_conflict'; END IF;
  PERFORM gateway_v1.start_attempt(i.rid,i.aid,1,i.deployment);
 END LOOP;
END $$;

CREATE FUNCTION gateway_v1.batch_record(handle uuid, kid uuid, rev bigint, pid text, meta jsonb, removing boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE b jsonb;
BEGIN
 b=publication.v1_batch_lock(handle);
 IF b IS NULL OR (b->>'api_key_id')::uuid<>kid OR b->>'provider_id' IS NOT NULL THEN
  PERFORM gateway_v1.batch_resolve(handle,kid,rev);
 END IF;
 PERFORM publication.v1_batch_update(handle,pid,meta,removing);
END $$;

CREATE FUNCTION gateway_v1.batch_resolve(handle uuid, kid uuid, rev bigint) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE b jsonb; manifest jsonb; target jsonb;
BEGIN
 b=publication.v1_batch_lookup(handle,kid);
 IF b IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 FOR target IN SELECT value FROM jsonb_array_elements(b->'targets') LOOP
  PERFORM gateway_v1.preflight(kid,rev,(target->>'model')::uuid,(target->>'deployment')::uuid);
 END LOOP;
 manifest=publication.v1_snapshot(rev,true)->'manifest';
 IF NOT EXISTS(SELECT FROM jsonb_array_elements(manifest->'resources'->'deployments') d
  WHERE d->>'id'=b->>'deployment_id' AND d->>'credential_id'=b->>'credential_id'
  AND d->>'credential_version'=b->>'credential_version' AND d->>'upstream_version'=b->>'upstream_version'
  AND d#>>'{relay_profile,endpoint}'='anthropic_messages') THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN b;
END $$;

CREATE FUNCTION gateway_v1.cancel_unstarted_request(rid uuid, reason text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals:=publication.v1_principal(); status jsonb;
BEGIN
 status=billing.v1_cancel(rid,p.node_id,reason);
 PERFORM gateway_v1.container_no_use(rid,(status->>'api_key_id')::uuid,true);
 RETURN status;
END $$;

CREATE FUNCTION gateway_v1.claim_purges(grace_seconds integer, n integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals := publication.v1_principal(); owners uuid[];
BEGIN
 IF grace_seconds IS NULL OR grace_seconds<0 OR n IS NULL OR n NOT BETWEEN 1 AND 100 THEN
  RAISE EXCEPTION 'invalid_purge_claim';
 END IF;
 owners=publication.v1_purge_owners(p.cluster_id);
 PERFORM publication.v1_purge_schedule(p.cluster_id,owners,identity.v1_key_activity(owners),
  make_interval(secs=>grace_seconds));
 RETURN publication.v1_purge_claim(p.cluster_id,p.node_id,n);
END $$;

CREATE FUNCTION gateway_v1.config_head() RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$SELECT publication.v1_config_head()$$;

CREATE FUNCTION gateway_v1.config_keys(revision bigint, since bigint, after_sequence bigint, after_key uuid, after_present boolean, page integer) RETURNS TABLE(change_sequence bigint, change_key uuid, present boolean, digest text, organization_id uuid, project_id uuid, service_account_id uuid, expires_at timestamp with time zone)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT * FROM publication.v1_config_keys(revision,since,after_sequence,after_key,after_present,page)
$$;

CREATE FUNCTION gateway_v1.container_admit(origin uuid, kid uuid, rev bigint, model uuid, dep uuid, requested uuid, tier text, fresh boolean, files_present boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE d jsonb:=gateway_v1.execution_deployment(kid,rev,model,dep,'openai_responses'); c jsonb; principal jsonb; dims jsonb;
 p publication.gateway_principals:=publication.v1_principal(); cap integer:=coalesce((d#>>'{relay_profile,container_reserve_minutes}')::int,0);
 tariff text:=coalesce(d#>>'{relay_profile,container_tariff}','session_20m');
BEGIN
 IF tier NOT IN ('1g','4g','16g','64g') OR cap<(CASE WHEN tariff='minute_min_5' THEN 5 ELSE 20 END) THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 IF requested IS NOT NULL THEN
  c=gateway_v1.container_resolve(requested,kid,rev,'openai_responses');
  IF c->>'deployment'<>dep::text OR c#>>'{metadata,memory_limit}' IS DISTINCT FROM tier THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 END IF;
 principal=identity.v1_authorize(kid,model);
 dims=catalog.v1_eligible(model,dep,publication.v1_snapshot(rev,true)->'manifest',(principal->>'organization_id')::uuid,
    (d#>>'{relay_profile,input_ceiling}')::bigint,(d#>>'{relay_profile,output_ceiling}')::bigint);
 IF NOT dims ? ('container_'||tier||'_minutes') THEN RAISE EXCEPTION 'price_unavailable'; END IF;
 PERFORM publication.v1_container_operation(origin,kid,rev,d,requested,tier,files_present);
 PERFORM billing.v1_container_hold(origin,requested,principal,kid,model,dep,dims,tier,tariff,cap,p.node_id,p.cluster_id,rev,fresh,(d->>'upstream_id')::uuid);
END $$;

CREATE FUNCTION gateway_v1.container_bind(kid uuid, rev bigint, model uuid, dep uuid, ep text, pid text, meta jsonb, creating boolean) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE d jsonb:=gateway_v1.execution_deployment(kid,rev,model,dep,ep);
BEGIN
 IF creating THEN RAISE EXCEPTION 'missing_execution_origin'; END IF;
 RETURN publication.v1_container_bind(kid,d,pid,ep,meta,creating);
END $$;

CREATE FUNCTION gateway_v1.container_cleanup(origin_id uuid, handle_id uuid, kid uuid, rev bigint) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE c jsonb; d jsonb;
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,NULL,NULL);
 c=publication.v1_container_cleanup(origin_id,handle_id,kid,rev);
 SELECT value INTO d FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=c->>'deployment_id' AND value->>'credential_id'=c->>'credential_id' AND
 value->>'credential_version'=c->>'credential_version' AND value->>'upstream_version'=c->>'upstream_version'
 AND value#>>'{relay_profile,endpoint}'='openai_responses';
 IF d IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN jsonb_build_object('model',c->'model_id','deployment',c->'deployment_id','provider_id',c->'provider_id','metadata',c->'metadata');
END $$;

CREATE FUNCTION gateway_v1.container_file(handle uuid, kid uuid, rev bigint, fid uuid, removing boolean) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN PERFORM gateway_v1.container_resolve(handle,kid,rev,'openai_responses'); RETURN publication.v1_container_file(handle,kid,fid,removing); END $$;

CREATE FUNCTION gateway_v1.container_file_bind(handle uuid, kid uuid, rev bigint, pid text, creating boolean) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN PERFORM gateway_v1.container_resolve(handle,kid,rev,'openai_responses'); RETURN publication.v1_container_file_bind(handle,kid,pid,creating); END $$;

CREATE FUNCTION gateway_v1.container_no_use(origin_id uuid, kid uuid, definite boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE o jsonb:=publication.v1_container_origin(origin_id);
BEGIN
 IF o IS NOT NULL AND o->>'kid'=kid::text AND (definite OR NOT (o->>'preloaded')::boolean) THEN PERFORM billing.v1_container_release(origin_id,kid,(o->>'deployment_id')::uuid); END IF;
END $$;

CREATE FUNCTION gateway_v1.container_receipt(origin_id uuid, kid uuid, pid text, meta jsonb, executed boolean) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE o jsonb:=publication.v1_container_origin(origin_id); d jsonb; result uuid; info jsonb;
BEGIN
 IF o IS NULL OR o->>'kid'<>kid::text OR (o->>'cleanup')::boolean THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 -- The supplier accepted an already admitted operation. Persist its result on the frozen binding,
 -- including after key revocation; future customer access checks the live key and deployment.
 IF meta ? 'memory_limit' AND meta->>'memory_limit' IS DISTINCT FROM o->>'memory_tier' THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 IF o->>'requested_handle' IS NOT NULL AND publication.v1_container((o->>'requested_handle')::uuid,kid,'openai_responses')->>'provider_id' IS DISTINCT FROM pid
 THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 d=jsonb_build_object('id',o->'deployment_id','model_id',o->'model_id','credential_id',o->'credential_id',
       'credential_version',o->'credential_version','upstream_version',o->'upstream_version');
 info=jsonb_strip_nulls(jsonb_build_object('object','container','name',meta->'name','created_at',meta->'created_at',
   'last_active_at',meta->'last_active_at','expires_after',meta->'expires_after','status',meta->'status',
   'memory_limit',o->'memory_tier','network_policy',meta->'network_policy'));
 result=publication.v1_container_bind(kid,d,pid,'openai_responses',info,true);
 PERFORM publication.v1_container_attest(origin_id,result);
 PERFORM billing.v1_container_link(origin_id,result,kid,(o->>'deployment_id')::uuid,pid,(publication.v1_container(result,kid,'openai_responses')#>>'{metadata,expires_after,minutes}')::int,executed,
     publication.v1_container(result,kid,'openai_responses')#>>'{metadata,memory_limit}',coalesce(meta ? 'memory_limit',false));
 RETURN result;
END $$;

CREATE FUNCTION gateway_v1.container_resolve(handle uuid, kid uuid, rev bigint, ep text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE c jsonb:=publication.v1_container(handle,kid,ep); d jsonb;
BEGIN
 IF c IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 d=gateway_v1.execution_deployment(kid,rev,(c->>'model_id')::uuid,(c->>'deployment_id')::uuid,ep);
 IF d->>'credential_id'<>c->>'credential_id' OR d->>'credential_version'<>c->>'credential_version'
    OR d->>'upstream_version'<>c->>'upstream_version' THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN jsonb_build_object('model',c->'model_id','deployment',c->'deployment_id','provider_id',c->'provider_id','metadata',c->'metadata');
END $$;

CREATE FUNCTION gateway_v1.container_retired(origin_id uuid, kid uuid, pid text, confirmed boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE o jsonb:=publication.v1_container_origin(origin_id); c jsonb; h uuid:=(o->>'handle')::uuid;
BEGIN
 IF o IS NULL OR o->>'kid'<>kid::text OR o->>'cleanup'<>'true' THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 c=publication.v1_container(h,kid,'openai_responses');
 IF c IS NULL THEN RETURN; END IF;
 IF c->>'provider_id'<>pid THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 PERFORM publication.v1_container_remove(h,kid); PERFORM billing.v1_container_due(h,kid,confirmed);
END $$;

CREATE FUNCTION gateway_v1.containers(kid uuid, rev bigint, cursor_id uuid, asc_order boolean, n integer) RETURNS SETOF jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE visible jsonb:=gateway_v1.preflight(kid,rev,NULL,NULL); allowed uuid[];
BEGIN
 SELECT array_agg((m->>'id')::uuid) INTO allowed FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,models}') m
 WHERE EXISTS(SELECT FROM jsonb_array_elements(visible->'data') v WHERE v->>'id'=m->>'public_name');
 RETURN QUERY SELECT * FROM publication.v1_containers(kid,cursor_id,asc_order,n,allowed);
END $$;

CREATE FUNCTION gateway_v1.conversation_id(kid uuid, rev bigint, model uuid, dep uuid, pid text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);
 RETURN publication.v1_conversation_id(kid,dep,pid);
END $$;

CREATE FUNCTION gateway_v1.conversation_remove(handle uuid, kid uuid, rev bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN PERFORM gateway_v1.conversation_resolve(handle,kid,rev); PERFORM publication.v1_conversation_remove(handle,kid); END $$;

CREATE FUNCTION gateway_v1.conversation_resolve(handle uuid, kid uuid, rev bigint) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE c jsonb;
BEGIN
 c=publication.v1_conversation(handle,kid);
 IF c IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 PERFORM gateway_v1.preflight(kid,rev,(c->>'model_id')::uuid,(c->>'deployment_id')::uuid);
 IF NOT EXISTS(SELECT FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}') d
 WHERE d->>'id'=c->>'deployment_id' AND d->>'credential_id'=c->>'credential_id' AND d->>'credential_version'=c->>'credential_version'
 AND d->>'upstream_version'=c->>'upstream_version' AND d#>>'{relay_profile,endpoint}'='openai_responses') THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN jsonb_build_object('model',c->'model_id','deployment',c->'deployment_id','provider_id',c->'provider_id');
END $$;

CREATE FUNCTION gateway_v1.conversation_save(handle uuid, kid uuid, rev bigint, model uuid, dep uuid, pid text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE d jsonb;
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);
 SELECT value INTO STRICT d FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=dep::text AND value->>'model_id'=model::text AND value#>>'{relay_profile,endpoint}'='openai_responses';
 PERFORM publication.v1_conversation_save(handle,kid,d,pid);
END $$;

CREATE FUNCTION gateway_v1.execution_deployment(kid uuid, rev bigint, model uuid, dep uuid, ep text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE d jsonb;
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);
 SELECT value INTO d FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=dep::text AND value->>'model_id'=model::text AND value#>>'{relay_profile,endpoint}'=ep;
 IF d IS NULL THEN RAISE EXCEPTION 'deployment_unavailable'; END IF; RETURN d;
END $$;

CREATE FUNCTION gateway_v1.execution_file(kid uuid, rev bigint, model uuid, dep uuid, pid text, creating boolean) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE d jsonb:=gateway_v1.execution_deployment(kid,rev,model,dep,'anthropic_messages');
BEGIN
 IF creating THEN RAISE EXCEPTION 'missing_execution_origin'; END IF;
 RETURN publication.v1_execution_file(kid,d,pid,creating);
END $$;

CREATE FUNCTION gateway_v1.execution_file_refresh(handle uuid, kid uuid, rev bigint, meta jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 PERFORM gateway_v1.native_file_resolve(handle,kid,rev,'anthropic_messages');
 PERFORM publication.v1_execution_file_refresh(handle,kid,meta);
END $$;

CREATE FUNCTION gateway_v1.execution_output(rid uuid, kid uuid, dep uuid, pid text, is_file boolean) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE f jsonb:=billing.v1_execution_attempt(rid,kid,dep,(publication.v1_principal()).cluster_id); d jsonb;
BEGIN
 IF f IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 SELECT value INTO STRICT d FROM jsonb_array_elements(publication.v1_snapshot((f->>'revision')::bigint,false)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=dep::text AND value->>'model_id'=f->>'model' AND value#>>'{relay_profile,endpoint}'='anthropic_messages'
 AND (value#>>'{relay_profile,code_execution}'='true' OR (f->>'mode'='bundled' AND (f->>'searches')::int>0));
 IF is_file THEN RETURN publication.v1_execution_file(kid,d,pid,true); END IF;
 RETURN publication.v1_container_bind(kid,d,pid,'anthropic_messages',NULL,true);
END $$;

CREATE FUNCTION gateway_v1.finish_purge(purge uuid, outcome text, error text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals := publication.v1_principal();
BEGIN
 PERFORM publication.v1_purge_finish(purge,p.cluster_id,p.node_id,outcome,error);
END $$;

CREATE FUNCTION gateway_v1.forget_response(handle uuid, kid uuid, rev bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 PERFORM gateway_v1.resolve_response(handle,kid,rev);
 PERFORM publication.v1_forget_response(handle,kid);
END $$;

CREATE FUNCTION gateway_v1.get_config_snapshot(revision bigint) RETURNS bytea
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$SELECT publication.v1_config_payload(revision)$$;

CREATE FUNCTION gateway_v1.get_credential_envelope(revision bigint, credential_id uuid, credential_version integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 DECLARE envelope jsonb;
 BEGIN
  envelope=publication.v1_envelope(revision,credential_id,credential_version);
  IF NOT catalog.v1_credential_current(credential_id,credential_version) THEN RAISE EXCEPTION 'credential_unavailable'; END IF;
  RETURN envelope;
 END $$;

CREATE FUNCTION gateway_v1.get_request_status(request_id uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$SELECT billing.v1_status(request_id,(publication.v1_principal()).node_id)$$;

CREATE FUNCTION gateway_v1.native_file_remove(handle uuid, kid uuid, rev bigint, ep text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN PERFORM gateway_v1.native_file_resolve(handle,kid,rev,ep); PERFORM publication.v1_file_remove(handle,kid); END $$;

CREATE FUNCTION gateway_v1.native_file_resolve(handle uuid, kid uuid, rev bigint, ep text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE f jsonb; snap jsonb;
BEGIN
 f=publication.v1_native_file(handle,kid,ep);
 IF f IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 PERFORM gateway_v1.preflight(kid,rev,(f->>'model')::uuid,(f->>'deployment')::uuid);
 snap=publication.v1_snapshot(rev,true)->'manifest';
 IF NOT EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'deployments') d WHERE d->>'id'=f->>'deployment'
 AND d->>'credential_id'=f->>'credential' AND d->>'credential_version'=f->>'credential_version'
 AND d->>'upstream_version'=f->>'upstream_version' AND d#>>'{relay_profile,endpoint}'=ep)
 THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN f;
END $$;

CREATE FUNCTION gateway_v1.native_file_save(handle uuid, kid uuid, rev bigint, model uuid, dep uuid, meta jsonb, ep text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE snap jsonb; d jsonb;
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);snap=publication.v1_snapshot(rev,true)->'manifest';
 SELECT v INTO d FROM jsonb_array_elements(snap->'resources'->'deployments') v WHERE v->>'id'=dep::text AND
 v->>'model_id'=model::text AND v#>>'{relay_profile,endpoint}'=ep AND ep IN ('anthropic_messages','openai_responses');
 IF d IS NULL THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 PERFORM publication.v1_native_file_save(handle,kid,model,d,meta,ep);
END $$;

CREATE FUNCTION gateway_v1.native_files(kid uuid, rev bigint, stamp uuid, newer boolean, n integer, ep text, ascending boolean, purpose text) RETURNS SETOF jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,NULL,NULL);
 RETURN QUERY SELECT * FROM publication.v1_native_files(kid,stamp,newer,n,ep,ascending,purpose);
END $$;

CREATE FUNCTION gateway_v1.open_attempt(p publication.gateway_principals, req jsonb, aid uuid, seq integer, dep uuid, adm jsonb) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE snap jsonb; model uuid:=(req->>'model_id')::uuid; org uuid:=(req->>'organization_id')::uuid;
 rev bigint:=(req->>'config_revision')::bigint; rid uuid:=(req->>'request_id')::uuid;
 target record; answer jsonb;
BEGIN
 IF adm IS NULL THEN adm=publication.v1_admission(p,rev,NULL,org,model,false); END IF;
 snap=adm->'manifest';
 IF p.applied_revision<rev OR EXISTS(
  SELECT FROM jsonb_array_elements_text(adm->'required_capabilities') cap WHERE NOT cap=ANY(p.capabilities))
 THEN RAISE EXCEPTION 'configuration_not_applied'; END IF;
 PERFORM identity.v1_authorize((req->>'api_key_id')::uuid,model);
 PERFORM catalog.v1_eligible(model,dep,snap,org,(req->>'input_limit')::bigint,(req->>'output_limit')::bigint);
 SELECT * INTO target FROM gateway_v1.attempt_target(snap,dep,(req->>'guardrail_unit_limit')::integer,
  req->>'execution_mode',(req->>'execution_seconds_limit')::integer,(req->>'file_search_limit')::integer,
  (req->>'web_search_limit')::integer);
 IF NOT FOUND THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 answer=billing.v1_attempt(org,rid,aid,dep,model,seq,clock_timestamp(),p.node_id,target.expected_format);
 IF req->>'execution_mode' IN ('metered','preloaded') OR target.expected_format='aws.bedrock.converse.v1' THEN PERFORM billing.v1_execution_supplier(rid,aid,p.node_id,target.supplier); END IF;
 RETURN answer;
END $$;

CREATE FUNCTION gateway_v1.openai_file_id(kid uuid, rev bigint, model uuid, dep uuid, pid text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);
 RETURN publication.v1_openai_file_id(kid,dep,pid);
END $$;

CREATE FUNCTION gateway_v1.preflight(kid uuid, rev bigint, requested_model uuid, dep uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE adm jsonb; snap jsonb; k jsonb; org uuid; m jsonb; d jsonb; authorized jsonb;
        visible jsonb := '[]'; limits jsonb;
BEGIN
 adm=publication.v1_admission(publication.v1_principal(),rev,kid,NULL,requested_model,true);
 k=adm->'key';
 IF jsonb_typeof(k) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'key_inactive'; END IF;
 snap=adm->'manifest';
 org=(k->>'organization_id')::uuid;
 -- A null model checks key liveness even for a key with zero allowed models.
 BEGIN
  PERFORM identity.v1_authorize(kid,NULL);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM <> 'model_forbidden' THEN RAISE; END IF;
 END;
 FOR m IN SELECT v FROM jsonb_array_elements(snap->'resources'->'models') v
   WHERE requested_model IS NULL OR v->>'id'=requested_model::text ORDER BY v->>'public_name' LOOP
  BEGIN
   authorized=identity.v1_authorize(kid,(m->>'id')::uuid);
   IF authorized->>'organization_id' IS DISTINCT FROM k->>'organization_id'
      OR authorized->>'project_id' IS DISTINCT FROM k->>'project_id'
      OR authorized->>'service_account_id' IS DISTINCT FROM k->>'service_account_id'
      OR NOT EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'access_policies') p
         WHERE p->>'organization_id'=org::text AND p->>'scope_type'='organization' AND p->>'layer'='platform')
      OR EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'access_policies') p
         WHERE p->>'organization_id'=org::text AND (p->>'scope_type'='organization'
          OR p->>'scope_type'='project' AND p->>'scope_id'=k->>'project_id'
          OR p->>'scope_type'='key' AND p->>'scope_id'=kid::text)
         AND NOT ((p->'allowed_models') ? (m->>'id'))) THEN RAISE EXCEPTION 'model_forbidden'; END IF;
   FOR d IN SELECT v FROM jsonb_array_elements(snap->'resources'->'deployments') v
     WHERE v->>'model_id'=m->>'id' AND v ? 'relay_profile' AND (dep IS NULL OR v->>'id'=dep::text) LOOP
    limits=d->'relay_profile';
    BEGIN
     PERFORM catalog.v1_eligible((m->>'id')::uuid,(d->>'id')::uuid,snap,org,
             (limits->>'input_ceiling')::bigint,(limits->>'output_ceiling')::bigint);
     visible=visible || jsonb_build_array(jsonb_build_object('id',m->>'public_name',
        'object','model','created',catalog.v1_model_created((m->>'id')::uuid),'owned_by','nexus'));
     EXIT;
    EXCEPTION WHEN raise_exception THEN
     IF SQLERRM NOT IN ('deployment_unavailable','model_unavailable') THEN RAISE; END IF;
    END;
   END LOOP;
  EXCEPTION WHEN raise_exception THEN
   IF SQLERRM <> 'model_forbidden' OR requested_model IS NOT NULL THEN RAISE; END IF;
  END;
 END LOOP;
 IF requested_model IS NOT NULL AND visible='[]'::jsonb THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 RETURN jsonb_build_object('object','list','data',visible);
END $$;

CREATE FUNCTION gateway_v1.record_orphan(kid uuid, rev bigint, kind text, dep uuid, ep text, pid text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals := publication.v1_principal(); d jsonb;
BEGIN
 SELECT value INTO d FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=dep::text;
 IF d IS NULL THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 PERFORM publication.v1_purge_orphan(p.cluster_id,kid,kind,d,ep,pid);
END $$;

CREATE FUNCTION gateway_v1.reserve_execution_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, searches integer, exec_mode text, exec_bound integer, aid uuid DEFAULT NULL::uuid, dep uuid DEFAULT NULL::uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,searches,0,exec_mode,exec_bound,NULL,0,aid,dep)
$$;

CREATE FUNCTION gateway_v1.reserve_file_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, searches integer, files integer, aid uuid DEFAULT NULL::uuid, dep uuid DEFAULT NULL::uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,searches,files,'none',0,NULL,0,aid,dep)
$$;

CREATE FUNCTION gateway_v1.reserve_guardrail_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, dep uuid, units integer, aid uuid DEFAULT NULL::uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 IF units IS NULL OR units NOT BETWEEN 1 AND 1000000000 OR dep IS NULL THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 RETURN gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,0,0,'none',0,dep,units,aid,CASE WHEN aid IS NULL THEN NULL ELSE dep END);
END $$;

CREATE FUNCTION gateway_v1.reserve_interpreter(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, searches integer, files integer, dep uuid, requested uuid, tier text, files_present boolean, aid uuid DEFAULT NULL::uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE reply jsonb; p publication.gateway_principals;
BEGIN
 reply=gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,searches,files,'none',0,NULL,0,NULL,NULL);
 PERFORM gateway_v1.container_admit(rid,kid,rev,model,dep,requested,tier,false,files_present);
 IF aid IS NULL THEN RETURN reply; END IF;
 p=publication.v1_principal();
 RETURN billing.v1_status(rid,p.node_id) || jsonb_build_object('replayed',reply->'replayed',
  'attempt',gateway_v1.open_attempt(p,billing.v1_status(rid,p.node_id),aid,1,dep,NULL));
END $$;

CREATE FUNCTION gateway_v1.reserve_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, aid uuid DEFAULT NULL::uuid, dep uuid DEFAULT NULL::uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,0,0,'none',0,NULL,0,aid,dep)
$$;

CREATE FUNCTION gateway_v1.reserve_search_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, searches integer, aid uuid DEFAULT NULL::uuid, dep uuid DEFAULT NULL::uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,searches,0,'none',0,NULL,0,aid,dep)
$$;

CREATE FUNCTION gateway_v1.reserve_with_execution(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, batch boolean, searches integer, files integer, exec_mode text, exec_bound integer) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,batch,searches,files,exec_mode,exec_bound,NULL,0,NULL,NULL)
$$;

CREATE FUNCTION gateway_v1.resolve_response(handle uuid, kid uuid, rev bigint) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE f jsonb; snap jsonb;
BEGIN
 f=publication.v1_stored_response(handle,kid);
 IF f IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 PERFORM gateway_v1.preflight(kid,rev,(f->>'model')::uuid,(f->>'deployment')::uuid);
 snap=publication.v1_snapshot(rev,true)->'manifest';
 IF NOT EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'deployments') d
   WHERE d->>'id'=f->>'deployment' AND d->>'credential_id'=f->>'credential'
   AND d->>'credential_version'=f->>'credential_version' AND d->>'upstream_version'=f->>'upstream_version'
   AND d->>'upstream_model'=f->>'upstream_model' AND d#>>'{relay_profile,endpoint}'='openai_responses')
 THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN f;
END $$;

CREATE FUNCTION gateway_v1.resource_usage(kind text, kid uuid, rev bigint, keys uuid[], key_cap bigint, org_cap bigint) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,NULL,NULL);
 IF cardinality(keys)>100000 THEN RAISE EXCEPTION 'invalid_resource_kind'; END IF;
 RETURN publication.v1_resource_usage(kind,kid,keys,key_cap,org_cap);
END $$;

CREATE FUNCTION gateway_v1.response_can_delete(handle_id uuid, kid uuid, rev bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$BEGIN
 PERFORM gateway_v1.resolve_response(handle_id,kid,rev);
 IF publication.v1_job_pending(handle_id) THEN RAISE EXCEPTION 'response_pending'; END IF;
END $$;

CREATE FUNCTION gateway_v1.save_response(handle uuid, rid uuid, aid uuid, pid text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE f jsonb; snap jsonb; d jsonb;
BEGIN
 f=billing.v1_response_attempt(rid,aid,(publication.v1_principal()).node_id);
 IF f IS NULL THEN RAISE EXCEPTION 'not_found'; END IF;
 snap=publication.v1_snapshot((f->>'revision')::bigint,false)->'manifest';
 SELECT v INTO d FROM jsonb_array_elements(snap->'resources'->'deployments') v
 WHERE v->>'id'=f->>'deployment' AND v#>>'{relay_profile,endpoint}'='openai_responses';
 IF d IS NULL THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 PERFORM publication.v1_save_response(handle,rid,(f->>'key')::uuid,(f->>'model')::uuid,d,pid);
END $$;

CREATE FUNCTION gateway_v1.start_attempt(rid uuid, aid uuid, seq integer, dep uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals; req jsonb;
BEGIN
 p=publication.v1_principal(); req=billing.v1_status(rid,p.node_id);
 IF req IS NULL THEN RAISE EXCEPTION 'not_found'; END IF;
 RETURN gateway_v1.open_attempt(p,req,aid,seq,dep,NULL);
END $$;

CREATE FUNCTION gateway_v1.stored_interpreter(handle_id uuid, kid uuid, rev bigint) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN PERFORM gateway_v1.resolve_response(handle_id,kid,rev); RETURN publication.v1_stored_interpreter(handle_id,kid); END $$;

CREATE FUNCTION gateway_v1.vector_batch(store uuid, kid uuid, rev bigint, id_in uuid, pid text, create_in boolean) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN PERFORM gateway_v1.vector_resolve(store,kid,rev); RETURN publication.v1_vector_batch(store,id_in,pid,create_in); END $$;

CREATE FUNCTION gateway_v1.vector_binding(handle uuid, kid uuid, rev bigint, cleanup boolean) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE v jsonb;
BEGIN
 v=publication.v1_vector(handle,kid);
 IF v IS NULL OR v->>'state' NOT IN ('active','expired') THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF cleanup THEN PERFORM gateway_v1.preflight(kid,rev,NULL,NULL); ELSE
  PERFORM gateway_v1.preflight(kid,rev,(v->>'model_id')::uuid,(v->>'deployment_id')::uuid); END IF;
 IF NOT EXISTS(SELECT FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}') d
   WHERE d->>'id'=v->>'deployment_id' AND d->>'credential_id'=v->>'credential_id' AND d->>'credential_version'=v->>'credential_version'
   AND d->>'upstream_version'=v->>'upstream_version' AND d#>>'{relay_profile,endpoint}'='openai_responses')
 THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 -- A store whose prepaid storage lapsed only accepts deletion until the wallet is funded.
 IF NOT cleanup AND billing.v1_storage_lapsed(handle) IS NOT NULL THEN RAISE EXCEPTION 'insufficient_funds'; END IF;
 RETURN v||jsonb_build_object('stamp',clock_timestamp());
END $$;

CREATE FUNCTION gateway_v1.vector_claim() RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT j||jsonb_build_object('stamp',clock_timestamp(),
   'retire',coalesce(billing.v1_storage_lapsed((j->>'id')::uuid)='delete',false)) FROM publication.v1_vector_claim() j
$$;

CREATE FUNCTION gateway_v1.vector_cleanup(handle uuid, kid uuid, rev bigint) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$SELECT gateway_v1.vector_binding(handle,kid,rev,true)$$;

CREATE FUNCTION gateway_v1.vector_due(handle uuid, kid uuid, rev bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN PERFORM gateway_v1.vector_resolve(handle,kid,rev); PERFORM publication.v1_vector_due(handle,kid); END $$;

CREATE FUNCTION gateway_v1.vector_id(kid uuid, rev bigint, model uuid, dep uuid, pid text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN PERFORM gateway_v1.preflight(kid,rev,model,dep); RETURN publication.v1_vector_id(kid,dep,pid); END $$;

CREATE FUNCTION gateway_v1.vector_locator(handle uuid, kid uuid, pid text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE prior jsonb:=publication.v1_vector(handle,kid);
BEGIN
 PERFORM publication.v1_vector_locator(handle,kid,pid);
 IF prior->>'state'='submitting' AND prior->>'provider_id' IS NULL THEN
  PERFORM billing.v1_storage_missed(handle,clock_timestamp());
 END IF;
END $$;

CREATE FUNCTION gateway_v1.vector_missed(handle uuid, stamp timestamp with time zone) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 IF stamp IS NULL OR stamp>clock_timestamp() OR NOT publication.v1_vector_mark_missed(handle,stamp) THEN RETURN; END IF;
 PERFORM billing.v1_storage_missed(handle,clock_timestamp());
END $$;

CREATE FUNCTION gateway_v1.vector_observe(handle uuid, kid uuid, rev bigint, pid text, observed timestamp with time zone, meta jsonb, new_state text, uncertain boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE v jsonb; bytes bigint; expiry timestamptz; closing boolean := new_state IN ('deleted','expired','unknown','rejected');
BEGIN
 v=publication.v1_vector(handle,kid);
 IF v IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 -- Finishing an already authorized create/delete must survive a concurrent key revocation.
 IF kid IS NOT NULL AND v->>'api_key_id' IS DISTINCT FROM kid::text THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF kid IS NOT NULL AND v->>'provider_id' IS NULL AND v->>'owner_node'<>(publication.v1_principal()).node_id::text THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF observed IS NULL OR observed>clock_timestamp() OR observed<(v->>'created_at')::timestamptz THEN RAISE EXCEPTION 'invalid_storage_sample'; END IF;
 IF NOT publication.v1_vector_update(handle,kid,pid,observed,meta,new_state) THEN RETURN; END IF;
 IF new_state IN ('active','expired') THEN
   bytes=(meta->>'usage_bytes')::bigint;
   IF bytes IS NULL OR bytes<0 THEN RAISE EXCEPTION 'invalid_storage_sample'; END IF;
   expiry=to_timestamp((meta->>'expires_at')::bigint);
 ELSE bytes=0; END IF;
 PERFORM billing.v1_storage_sample(handle,clock_timestamp(),bytes,expiry,closing,uncertain);
END $$;

CREATE FUNCTION gateway_v1.vector_page(kid uuid, rev bigint, stamp uuid, asc_order boolean, n integer) RETURNS SETOF jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE visible jsonb; allowed uuid[];
BEGIN
 visible=gateway_v1.preflight(kid,rev,NULL,NULL);
 SELECT array_agg((model->>'id')::uuid) INTO allowed FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,models}') model
 WHERE EXISTS(SELECT FROM jsonb_array_elements(visible->'data') v WHERE v->>'id'=model->>'public_name');
 RETURN QUERY SELECT * FROM publication.v1_vectors(kid,stamp,asc_order,n,allowed);
END $$;

CREATE FUNCTION gateway_v1.vector_prepare(handle uuid, kid uuid, rev bigint, model uuid, dep uuid) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE existing jsonb; d jsonb; p publication.gateway_principals := publication.v1_principal();
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);
 SELECT value INTO d FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=dep::text AND value->>'model_id'=model::text AND value#>>'{relay_profile,endpoint}'='openai_responses'
 AND coalesce((value#>>'{relay_profile,file_search_ceiling}')::integer,0)>0;
 IF d IS NULL THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(handle::text,91503));
 existing=publication.v1_vector(handle,kid);
 IF existing IS NOT NULL THEN
  IF existing->>'state'<>'submitting' OR existing->>'owner_node'<>p.node_id::text OR existing->>'model_id'<>model::text
     OR existing->>'deployment_id'<>dep::text OR existing->>'credential_version'<>d->>'credential_version'
     OR existing->>'credential_id'<>d->>'credential_id' OR existing->>'upstream_version'<>d->>'upstream_version'
    THEN RAISE EXCEPTION 'resource_conflict'; END IF;
  RETURN to_jsonb(clock_timestamp());
 END IF;
 PERFORM billing.v1_storage_open(handle,identity.v1_authorize(kid,model),kid,model,dep,p.node_id,p.cluster_id,rev);
 PERFORM publication.v1_vector_prepare(handle,kid,d);
 RETURN to_jsonb(clock_timestamp());
END $$;

CREATE FUNCTION gateway_v1.vector_resolve(handle uuid, kid uuid, rev bigint) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$SELECT gateway_v1.vector_binding(handle,kid,rev,false)$$;

CREATE FUNCTION gateway_v1.vector_writable(handle uuid, kid uuid, rev bigint) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN PERFORM gateway_v1.vector_resolve(handle,kid,rev); PERFORM billing.v1_storage_write(handle); END $$;

RESET ROLE;

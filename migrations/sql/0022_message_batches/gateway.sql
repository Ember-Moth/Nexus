CREATE FUNCTION gateway_v1.reserve_item(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,batch boolean)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; snap jsonb; principal jsonb; dims jsonb; previous jsonb; org uuid;
BEGIN
 p=publication.v1_principal();
 PERFORM pg_advisory_xact_lock(hashtextextended(rid::text,87412));
 previous=billing.v1_status(rid,p.node_id);
 IF previous IS NOT NULL THEN
  IF billing.v1_batch_pricing(rid,p.node_id) IS DISTINCT FROM batch OR (previous->>'api_key_id')::uuid IS DISTINCT FROM kid OR (previous->>'model_id')::uuid IS DISTINCT FROM model
   OR (previous->>'config_revision')::bigint IS DISTINCT FROM rev OR (previous->>'input_limit')::bigint IS DISTINCT FROM inputs
   OR (previous->>'output_limit')::bigint IS DISTINCT FROM outputs THEN RAISE EXCEPTION 'request_conflict'; END IF;
  RETURN previous || '{"replayed":true}';
 END IF;
 snap=publication.v1_snapshot(rev,true)->'manifest';
 IF NOT EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'key_verifiers') k WHERE k->>'id'=kid::text)
 THEN RAISE EXCEPTION 'key_outside_cluster'; END IF;
 principal=identity.v1_authorize(kid,model); org=(principal->>'organization_id')::uuid;
 IF NOT EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'key_verifiers') k
   WHERE k->>'id'=kid::text AND k->>'organization_id'=org::text
   AND k->>'project_id'=principal->>'project_id' AND k->>'service_account_id'=principal->>'service_account_id')
 OR NOT EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'access_policies') a
   WHERE a->>'organization_id'=org::text AND a->>'scope_type'='organization' AND a->>'layer'='platform')
 OR EXISTS(SELECT FROM jsonb_array_elements(snap->'resources'->'access_policies') a
   WHERE a->>'organization_id'=org::text AND (a->>'scope_type'='organization' OR
    (a->>'scope_type'='project' AND a->>'scope_id'=principal->>'project_id') OR
    (a->>'scope_type'='key' AND a->>'scope_id'=kid::text))
   AND NOT ((a->'allowed_models') ? model::text)) THEN RAISE EXCEPTION 'snapshot_forbidden'; END IF;
 dims=catalog.v1_eligible(model,NULL,snap,org,inputs,outputs);
 PERFORM billing.v1_reserve_item(rid,org,(principal->>'project_id')::uuid,(principal->>'service_account_id')::uuid,kid,
 model,inputs,outputs,CASE WHEN batch THEN 90000 ELSE 900 END,encode(sha256(convert_to(jsonb_build_array(kid,model,rev,inputs,outputs,p.node_id)::text,'UTF8')),'hex'),
 dims,clock_timestamp(),p.node_id,p.cluster_id,rev,batch);
 RETURN billing.v1_status(rid,p.node_id) || '{"replayed":false}';
END $$;

REVOKE ALL ON FUNCTION gateway_v1.reserve_item(uuid,uuid,uuid,bigint,bigint,bigint,boolean) FROM PUBLIC;
CREATE OR REPLACE FUNCTION gateway_v1.reserve_request(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT gateway_v1.reserve_item(rid,kid,model,rev,inputs,outputs,false)
$$;
CREATE FUNCTION gateway_v1.batch_prepare(handle uuid,kid uuid,rev bigint,dep uuid,items jsonb,h jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE manifest jsonb; d jsonb; i record; reserved jsonb;
BEGIN
 manifest=publication.v1_snapshot(rev,true)->'manifest';
 SELECT value INTO STRICT d FROM jsonb_array_elements(manifest->'resources'->'deployments') WHERE value->>'id'=dep::text
 AND value#>>'{relay_profile,endpoint}'='anthropic_messages';
 IF jsonb_array_length(items) NOT BETWEEN 1 AND 100000 OR jsonb_typeof(h)<>'object'
 OR h-'anthropic-beta'-'anthropic-version'<>'{}' OR octet_length(h::text)>8192 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 PERFORM publication.v1_batch_prepare(handle,kid,rev,d,items,h);
 FOR i IN SELECT * FROM jsonb_to_recordset(items) AS t(rid uuid,aid uuid,model uuid,deployment uuid,inputs bigint,outputs bigint) LOOP
  IF NOT EXISTS(SELECT FROM jsonb_array_elements(manifest->'resources'->'deployments') target WHERE
    target->>'id'=i.deployment::text AND target->>'model_id'=i.model::text
    AND target#>>'{relay_profile,endpoint}'='anthropic_messages' AND target->>'upstream_id'=d->>'upstream_id'
    AND target->>'credential_id'=d->>'credential_id' AND target->>'credential_version'=d->>'credential_version')
    THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
  reserved=gateway_v1.reserve_item(i.rid,kid,i.model,rev,i.inputs,i.outputs,true);
  IF (reserved->>'replayed')::boolean THEN RAISE EXCEPTION 'request_conflict'; END IF;
  PERFORM gateway_v1.start_attempt(i.rid,i.aid,1,i.deployment);
 END LOOP;
END $$;
CREATE FUNCTION gateway_v1.batch_resolve(handle uuid,kid uuid,rev bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION gateway_v1.batch_page(kid uuid,rev bigint,stamp uuid,newer boolean,n integer) RETURNS SETOF jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,NULL,NULL);
 RETURN QUERY SELECT * FROM publication.v1_batch_page(kid,stamp,newer,n);
END $$;
CREATE FUNCTION gateway_v1.batch_record(handle uuid,kid uuid,rev bigint,pid text,meta jsonb,removing boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE b jsonb;
BEGIN
 b=publication.v1_batch_lock(handle);
 IF b IS NULL OR (b->>'api_key_id')::uuid<>kid OR b->>'provider_id' IS NOT NULL THEN
  PERFORM gateway_v1.batch_resolve(handle,kid,rev);
 END IF;
 PERFORM publication.v1_batch_update(handle,pid,meta,removing);
END $$;
CREATE FUNCTION gateway_v1.batch_claim() RETURNS jsonb LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$SELECT publication.v1_batch_claim()$$;
CREATE FUNCTION gateway_v1.batch_finish(handle uuid,pid text,meta jsonb,results jsonb,fallback text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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

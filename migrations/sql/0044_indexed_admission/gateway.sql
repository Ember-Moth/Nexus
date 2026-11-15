DROP FUNCTION gateway_v1.reserve_request(uuid,uuid,uuid,bigint,bigint,bigint);
DROP FUNCTION gateway_v1.reserve_item(uuid,uuid,uuid,bigint,bigint,bigint,boolean);
DROP FUNCTION gateway_v1.reserve_search_request(uuid,uuid,uuid,bigint,bigint,bigint,integer);
DROP FUNCTION gateway_v1.reserve_metered(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer);
DROP FUNCTION gateway_v1.reserve_file_request(uuid,uuid,uuid,bigint,bigint,bigint,integer,integer);
DROP FUNCTION gateway_v1.reserve_interpreter(uuid,uuid,uuid,bigint,bigint,bigint,integer,integer,uuid,uuid,text,boolean);
DROP FUNCTION gateway_v1.reserve_tools(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer);
DROP FUNCTION gateway_v1.reserve_execution_request(uuid,uuid,uuid,bigint,bigint,bigint,integer,text,integer);
DROP FUNCTION gateway_v1.reserve_guardrail_request(uuid,uuid,uuid,bigint,bigint,bigint,uuid,integer);
DROP FUNCTION gateway_v1.reserve_with_execution(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer,text,integer);
DROP FUNCTION gateway_v1.reserve_with_guardrail(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer,text,integer,uuid,integer);
DROP FUNCTION gateway_v1.get_config_snapshot(bigint);

-- Register attempt `seq` of a reserved request on deployment `dep`. `adm` is the admission
-- view of the request's revision when the caller already read it in this transaction.
CREATE FUNCTION gateway_v1.open_attempt(p publication.gateway_principals, req jsonb, aid uuid,
 seq integer, dep uuid, adm jsonb)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE snap jsonb; model uuid:=(req->>'model_id')::uuid; org uuid:=(req->>'organization_id')::uuid;
 rev bigint:=(req->>'config_revision')::bigint; rid uuid:=(req->>'request_id')::uuid;
 expected_format text; supplier uuid; answer jsonb;
BEGIN
 IF adm IS NULL THEN adm=publication.v1_admission(p,rev,NULL,org,model,false); END IF;
 snap=adm->'manifest';
 IF p.applied_revision<rev OR EXISTS(
  SELECT FROM jsonb_array_elements_text(adm->'required_capabilities') cap WHERE NOT cap=ANY(p.capabilities))
 THEN RAISE EXCEPTION 'configuration_not_applied'; END IF;
 PERFORM identity.v1_authorize((req->>'api_key_id')::uuid,model);
 PERFORM catalog.v1_eligible(model,dep,snap,org,(req->>'input_limit')::bigint,(req->>'output_limit')::bigint);
 SELECT d->>'usage_format',(d->>'upstream_id')::uuid INTO expected_format,supplier
 FROM jsonb_array_elements(snap->'resources'->'deployments') d WHERE d->>'id'=dep::text
 AND (CASE WHEN (req->>'guardrail_unit_limit')::integer>0
     THEN d#>>'{relay_profile,endpoint}'='bedrock_converse' AND
       coalesce((d#>>'{relay_profile,bedrock_guardrail_unit_ceiling}')::integer,0) BETWEEN 1 AND (req->>'guardrail_unit_limit')::integer
     ELSE NOT(coalesce(jsonb_typeof(d#>'{relay_profile,bedrock_guardrail}')='object',false)
          AND coalesce((d#>>'{relay_profile,bedrock_guardrail_unit_ceiling}')::integer,0)>0) END)
 AND (req->>'execution_mode'='none'  OR (d#>>'{relay_profile,endpoint}'='anthropic_messages' AND (d#>>'{relay_profile,code_execution}'='true' OR (req->>'execution_mode'='bundled' AND (req->>'web_search_limit')::int>0))
       AND coalesce((d#>>'{relay_profile,execution_reserve_seconds}')::int,0)>=(req->>'execution_seconds_limit')::int))
 AND coalesce((d#>>'{relay_profile,file_search_ceiling}')::int,0)>=(req->>'file_search_limit')::int
 AND coalesce((d#>>'{relay_profile,web_search_ceiling}')::int,0)>=(req->>'web_search_limit')::int;
 IF NOT FOUND THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 answer=billing.v1_attempt(org,rid,aid,dep,model,seq,clock_timestamp(),p.node_id,expected_format);
 IF req->>'execution_mode' IN ('metered','preloaded') OR expected_format='aws.bedrock.converse.v1' THEN PERFORM billing.v1_execution_supplier(rid,aid,p.node_id,supplier); END IF;
 RETURN answer;
END $$;

-- Reserve a request and, when `aid` and `dep` are given, register its first attempt in the same
-- transaction: the organization's serial section is entered once and one commit covers both.
-- Any failure, including of the attempt, rolls back the reservation with it. A replay with the
-- same arguments returns the committed request and replays its first attempt.
CREATE FUNCTION gateway_v1.admit(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,
 batch boolean,searches integer,files integer,exec_mode text,exec_bound integer,guard_dep uuid,
 guard_units integer,aid uuid,dep uuid)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; adm jsonb; snap jsonb; k jsonb; principal jsonb; dims jsonb;
 previous jsonb; org uuid; attempt jsonb;
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
  dims=catalog.v1_eligible(model,NULL,snap,org,inputs,outputs);
 END IF;
 PERFORM billing.v1_reserve_guarded_capacity(rid,org,(principal->>'project_id')::uuid,(principal->>'service_account_id')::uuid,kid,
 model,inputs,outputs,CASE WHEN batch THEN 90000 ELSE 900 END,encode(sha256(convert_to(jsonb_build_array(kid,model,rev,inputs,outputs,p.node_id)::text,'UTF8')),'hex'),
 dims,clock_timestamp(),p.node_id,p.cluster_id,rev,batch,searches,files,0,NULL,exec_mode,exec_bound,NULL,0,NULL,guard_units);
 IF aid IS NOT NULL THEN
  attempt=gateway_v1.open_attempt(p,billing.v1_status(rid,p.node_id),aid,1,dep,adm);
 END IF;
 RETURN billing.v1_status(rid,p.node_id) || jsonb_build_object('replayed',false,'attempt',attempt);
END $$;

CREATE OR REPLACE FUNCTION gateway_v1.start_attempt(rid uuid,aid uuid,seq integer,dep uuid)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; req jsonb;
BEGIN
 p=publication.v1_principal(); req=billing.v1_status(rid,p.node_id);
 IF req IS NULL THEN RAISE EXCEPTION 'not_found'; END IF;
 RETURN gateway_v1.open_attempt(p,req,aid,seq,dep,NULL);
END $$;

-- Message batches reserve every item and start its attempt inside batch_prepare.
CREATE FUNCTION gateway_v1.reserve_with_execution(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,batch boolean,searches integer,files integer,exec_mode text,exec_bound integer)
 RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,batch,searches,files,exec_mode,exec_bound,NULL,0,NULL,NULL)
$$;

-- The Relay's reservations. With `aid` and `dep` the first attempt is registered as well.
CREATE FUNCTION gateway_v1.reserve_request(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,
 aid uuid DEFAULT NULL,dep uuid DEFAULT NULL)
 RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,0,0,'none',0,NULL,0,aid,dep)
$$;
CREATE FUNCTION gateway_v1.reserve_search_request(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,
 searches integer,aid uuid DEFAULT NULL,dep uuid DEFAULT NULL)
 RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,searches,0,'none',0,NULL,0,aid,dep)
$$;
CREATE FUNCTION gateway_v1.reserve_file_request(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,
 searches integer,files integer,aid uuid DEFAULT NULL,dep uuid DEFAULT NULL)
 RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,searches,files,'none',0,NULL,0,aid,dep)
$$;
CREATE FUNCTION gateway_v1.reserve_execution_request(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,
 searches integer,exec_mode text,exec_bound integer,aid uuid DEFAULT NULL,dep uuid DEFAULT NULL)
 RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,searches,0,exec_mode,exec_bound,NULL,0,aid,dep)
$$;
CREATE FUNCTION gateway_v1.reserve_guardrail_request(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,
 dep uuid,units integer,aid uuid DEFAULT NULL)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF units IS NULL OR units NOT BETWEEN 1 AND 1000000000 OR dep IS NULL THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 RETURN gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,0,0,'none',0,dep,units,aid,CASE WHEN aid IS NULL THEN NULL ELSE dep END);
END $$;
CREATE FUNCTION gateway_v1.reserve_interpreter(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,
 searches integer,files integer,dep uuid,requested uuid,tier text,files_present boolean,aid uuid DEFAULT NULL)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE reply jsonb; p publication.gateway_principals;
BEGIN
 reply=gateway_v1.admit(rid,kid,model,rev,inputs,outputs,false,searches,files,'none',0,NULL,0,NULL,NULL);
 PERFORM gateway_v1.container_admit(rid,kid,rev,model,dep,requested,tier,false,files_present);
 IF aid IS NULL THEN RETURN reply; END IF;
 p=publication.v1_principal();
 RETURN billing.v1_status(rid,p.node_id) || jsonb_build_object('replayed',reply->'replayed',
  'attempt',gateway_v1.open_attempt(p,billing.v1_status(rid,p.node_id),aid,1,dep,NULL));
END $$;

CREATE OR REPLACE FUNCTION gateway_v1.preflight(kid uuid, rev bigint, requested_model uuid, dep uuid)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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

-- Node configuration polling. config_head is what every poll reads; the payload and the key
-- changes are only read when the revision or the node's key sequence moved.
CREATE FUNCTION gateway_v1.config_head() RETURNS jsonb
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$SELECT publication.v1_config_head()$$;
CREATE FUNCTION gateway_v1.get_config_snapshot(revision bigint) RETURNS bytea
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$SELECT publication.v1_config_payload(revision)$$;
CREATE FUNCTION gateway_v1.config_keys(revision bigint, since bigint, after_sequence bigint,
 after_key uuid, after_present boolean, page integer)
 RETURNS TABLE(change_sequence bigint, change_key uuid, present boolean, digest text,
  organization_id uuid, project_id uuid, service_account_id uuid, expires_at timestamptz)
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT * FROM publication.v1_config_keys(revision,since,after_sequence,after_key,after_present,page)
$$;

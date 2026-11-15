CREATE FUNCTION gateway_v1.reserve_with_guardrail(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,batch boolean,searches integer,files integer,exec_mode text,exec_bound integer,guard_dep uuid,guard_units integer)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; snap jsonb; principal jsonb; dims jsonb; previous jsonb; org uuid;
BEGIN
 p=publication.v1_principal();
 PERFORM pg_advisory_xact_lock(hashtextextended(rid::text,87412));
 previous=billing.v1_status(rid,p.node_id);
 IF previous IS NOT NULL THEN
  IF (previous->>'guardrail_unit_limit')::int IS DISTINCT FROM guard_units OR previous->>'execution_mode' IS DISTINCT FROM exec_mode OR (previous->>'execution_seconds_limit')::int IS DISTINCT FROM exec_bound OR (previous->>'billing_policy_version')='openai_vector_storage_v1' OR (previous->>'file_search_limit')::int IS DISTINCT FROM files OR (previous->>'web_search_limit')::int IS DISTINCT FROM searches OR billing.v1_batch_pricing(rid,p.node_id) IS DISTINCT FROM batch OR (previous->>'api_key_id')::uuid IS DISTINCT FROM kid OR (previous->>'model_id')::uuid IS DISTINCT FROM model
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
 RETURN billing.v1_status(rid,p.node_id) || '{"replayed":false}';
END $$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_with_execution(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,batch boolean,searches integer,files integer,exec_mode text,exec_bound integer)
 RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT gateway_v1.reserve_with_guardrail(rid,kid,model,rev,inputs,outputs,batch,searches,files,exec_mode,exec_bound,NULL,0)
$$;

CREATE FUNCTION gateway_v1.reserve_guardrail_request(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,dep uuid,units integer)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF units IS NULL OR units NOT BETWEEN 1 AND 1000000000 OR dep IS NULL THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 RETURN gateway_v1.reserve_with_guardrail(rid,kid,model,rev,inputs,outputs,false,0,0,'none',0,dep,units);
END $$;

CREATE OR REPLACE FUNCTION gateway_v1.start_attempt(rid uuid,aid uuid,seq integer,dep uuid)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; req jsonb; snap jsonb; model uuid; org uuid; expected_format text; supplier uuid; answer jsonb;
BEGIN
 p=publication.v1_principal(); req=billing.v1_status(rid,p.node_id);
 IF req IS NULL THEN RAISE EXCEPTION 'not_found'; END IF;
 model=(req->>'model_id')::uuid;org=(req->>'organization_id')::uuid;
 snap=publication.v1_snapshot((req->>'config_revision')::bigint,false)->'manifest';
 IF p.applied_revision<(req->>'config_revision')::bigint OR EXISTS(
  SELECT FROM jsonb_array_elements_text(snap->'required_capabilities') cap WHERE NOT cap=ANY(p.capabilities))
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

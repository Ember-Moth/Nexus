-- Definitions as of 0043, restored by the 0044 downgrade (pg_get_functiondef output).
CREATE OR REPLACE FUNCTION gateway_v1.reserve_with_guardrail(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, batch boolean, searches integer, files integer, exec_mode text, exec_bound integer, guard_dep uuid, guard_units integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
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
END $function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_with_execution(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, batch boolean, searches integer, files integer, exec_mode text, exec_bound integer)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT gateway_v1.reserve_with_guardrail(rid,kid,model,rev,inputs,outputs,batch,searches,files,exec_mode,exec_bound,NULL,0)
$function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_tools(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, batch boolean, searches integer, files integer)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT gateway_v1.reserve_with_execution(rid,kid,model,rev,inputs,outputs,batch,searches,files,'none',0)
$function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_metered(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, batch boolean, searches integer)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT gateway_v1.reserve_tools(rid,kid,model,rev,inputs,outputs,batch,searches,0)
$function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_item(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, batch boolean)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT gateway_v1.reserve_metered(rid,kid,model,rev,inputs,outputs,batch,0)
$function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT gateway_v1.reserve_item(rid,kid,model,rev,inputs,outputs,false)
$function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_search_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, searches integer)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT gateway_v1.reserve_metered(rid,kid,model,rev,inputs,outputs,false,searches)
$function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_file_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, searches integer, files integer)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT gateway_v1.reserve_tools(rid,kid,model,rev,inputs,outputs,false,searches,files)
$function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_execution_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, searches integer, exec_mode text, exec_bound integer)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT gateway_v1.reserve_with_execution(rid,kid,model,rev,inputs,outputs,false,searches,0,exec_mode,exec_bound)
$function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_guardrail_request(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, dep uuid, units integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
BEGIN
 IF units IS NULL OR units NOT BETWEEN 1 AND 1000000000 OR dep IS NULL THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 RETURN gateway_v1.reserve_with_guardrail(rid,kid,model,rev,inputs,outputs,false,0,0,'none',0,dep,units);
END $function$;

CREATE OR REPLACE FUNCTION gateway_v1.reserve_interpreter(rid uuid, kid uuid, model uuid, rev bigint, inputs bigint, outputs bigint, searches integer, files integer, dep uuid, requested uuid, tier text, files_present boolean)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE reply jsonb;
BEGIN
 reply=gateway_v1.reserve_tools(rid,kid,model,rev,inputs,outputs,false,searches,files);
 PERFORM gateway_v1.container_admit(rid,kid,rev,model,dep,requested,tier,false,files_present);
 RETURN reply;
END $function$;

CREATE OR REPLACE FUNCTION gateway_v1.get_config_snapshot(revision bigint DEFAULT NULL::bigint)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$SELECT publication.v1_snapshot(revision,false)$function$;

CREATE OR REPLACE FUNCTION gateway_v1.start_attempt(rid uuid, aid uuid, seq integer, dep uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
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
END $function$;

CREATE OR REPLACE FUNCTION gateway_v1.preflight(kid uuid, rev bigint, requested_model uuid, dep uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE snap jsonb; k jsonb; org uuid; m jsonb; d jsonb; authorized jsonb;
        visible jsonb := '[]'; limits jsonb;
BEGIN
 snap=publication.v1_snapshot(rev,true)->'manifest';
 SELECT v INTO k FROM jsonb_array_elements(snap->'resources'->'key_verifiers') v
  WHERE v->>'id'=kid::text;
 IF k IS NULL THEN RAISE EXCEPTION 'key_inactive'; END IF;
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
END $function$;


CREATE FUNCTION gateway_v1.get_config_snapshot(revision bigint DEFAULT NULL) RETURNS jsonb
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$SELECT publication.v1_snapshot(revision,false)$$;
CREATE FUNCTION gateway_v1.get_credential_envelope(revision bigint,credential_id uuid,credential_version integer) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 DECLARE envelope jsonb;
 BEGIN
  envelope=publication.v1_envelope(revision,credential_id,credential_version);
  IF NOT catalog.v1_credential_current(credential_id,credential_version) THEN RAISE EXCEPTION 'credential_unavailable'; END IF;
  RETURN envelope;
 END $$;
CREATE FUNCTION gateway_v1.ack_config(revision bigint,hash text,applied boolean,reason text DEFAULT '') RETURNS jsonb
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$SELECT publication.v1_ack(revision,hash,applied,reason)$$;
CREATE FUNCTION gateway_v1.get_request_status(request_id uuid) RETURNS jsonb
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$SELECT billing.v1_status(request_id,(publication.v1_principal()).node_id)$$;
CREATE FUNCTION gateway_v1.reserve_request(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; snap jsonb; principal jsonb; dims jsonb; previous jsonb; org uuid;
BEGIN
 p=publication.v1_principal();
 PERFORM pg_advisory_xact_lock(hashtextextended(rid::text,87412));
 previous=billing.v1_status(rid,p.node_id);
 IF previous IS NOT NULL THEN
  IF (previous->>'api_key_id')::uuid IS DISTINCT FROM kid OR (previous->>'model_id')::uuid IS DISTINCT FROM model
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
 PERFORM billing.v1_reserve(rid,org,(principal->>'project_id')::uuid,(principal->>'service_account_id')::uuid,kid,
 model,inputs,outputs,900,encode(sha256(convert_to(jsonb_build_array(kid,model,rev,inputs,outputs,p.node_id)::text,'UTF8')),'hex'),
 dims,clock_timestamp(),p.node_id,p.cluster_id,rev);
 RETURN billing.v1_status(rid,p.node_id) || '{"replayed":false}';
END $$;
CREATE FUNCTION gateway_v1.start_attempt(rid uuid,aid uuid,seq integer,dep uuid)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; req jsonb; snap jsonb; model uuid; org uuid;
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
 RETURN billing.v1_attempt(org,rid,aid,dep,model,seq,clock_timestamp(),p.node_id);
END $$;
CREATE FUNCTION gateway_v1.append_usage_event(message text) RETURNS jsonb
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$SELECT billing.v1_append(message,(publication.v1_principal()).node_id)$$;
CREATE FUNCTION gateway_v1.cancel_unstarted_request(rid uuid,reason text) RETURNS jsonb
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$SELECT billing.v1_cancel(rid,(publication.v1_principal()).node_id,reason)$$;

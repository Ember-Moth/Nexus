CREATE FUNCTION gateway_v1.conversation_save(handle uuid,kid uuid,rev bigint,model uuid,dep uuid,pid text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE d jsonb;
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);
 SELECT value INTO STRICT d FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=dep::text AND value->>'model_id'=model::text AND value#>>'{relay_profile,endpoint}'='openai_responses';
 PERFORM publication.v1_conversation_save(handle,kid,d,pid);
END $$;
CREATE FUNCTION gateway_v1.conversation_resolve(handle uuid,kid uuid,rev bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION gateway_v1.conversation_id(kid uuid,rev bigint,model uuid,dep uuid,pid text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);
 RETURN publication.v1_conversation_id(kid,dep,pid);
END $$;
CREATE FUNCTION gateway_v1.conversation_remove(handle uuid,kid uuid,rev bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.conversation_resolve(handle,kid,rev); PERFORM publication.v1_conversation_remove(handle,kid); END $$;

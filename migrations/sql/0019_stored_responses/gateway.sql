CREATE FUNCTION gateway_v1.save_response(handle uuid,rid uuid,aid uuid,pid text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION gateway_v1.resolve_response(handle uuid,kid uuid,rev bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION gateway_v1.forget_response(handle uuid,kid uuid,rev bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM gateway_v1.resolve_response(handle,kid,rev);
 PERFORM publication.v1_forget_response(handle,kid);
END $$;

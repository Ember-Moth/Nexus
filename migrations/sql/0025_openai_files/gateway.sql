CREATE FUNCTION gateway_v1.native_files(kid uuid,rev bigint,stamp uuid,newer boolean,n integer,ep text,ascending boolean,purpose text) RETURNS SETOF jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,NULL,NULL);
 RETURN QUERY SELECT * FROM publication.v1_native_files(kid,stamp,newer,n,ep,ascending,purpose);
END $$;
CREATE FUNCTION gateway_v1.native_file_save(handle uuid,kid uuid,rev bigint,model uuid,dep uuid,meta jsonb,ep text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE snap jsonb; d jsonb;
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);snap=publication.v1_snapshot(rev,true)->'manifest';
 SELECT v INTO d FROM jsonb_array_elements(snap->'resources'->'deployments') v WHERE v->>'id'=dep::text AND
 v->>'model_id'=model::text AND v#>>'{relay_profile,endpoint}'=ep AND ep IN ('anthropic_messages','openai_responses');
 IF d IS NULL THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 PERFORM publication.v1_native_file_save(handle,kid,model,d,meta,ep);
END $$;
CREATE FUNCTION gateway_v1.native_file_resolve(handle uuid,kid uuid,rev bigint,ep text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION gateway_v1.native_file_remove(handle uuid,kid uuid,rev bigint,ep text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.native_file_resolve(handle,kid,rev,ep); PERFORM publication.v1_file_remove(handle,kid); END $$;
CREATE FUNCTION gateway_v1.openai_file_id(kid uuid,rev bigint,model uuid,dep uuid,pid text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);
 RETURN publication.v1_openai_file_id(kid,dep,pid);
END $$;

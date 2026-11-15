CREATE FUNCTION gateway_v1.execution_deployment(kid uuid,rev bigint,model uuid,dep uuid,ep text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE d jsonb;
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,model,dep);
 SELECT value INTO d FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=dep::text AND value->>'model_id'=model::text AND value#>>'{relay_profile,endpoint}'=ep;
 IF d IS NULL THEN RAISE EXCEPTION 'deployment_unavailable'; END IF; RETURN d;
END $$;
REVOKE ALL ON FUNCTION gateway_v1.execution_deployment(uuid,bigint,uuid,uuid,text) FROM PUBLIC;
CREATE FUNCTION gateway_v1.container_bind(kid uuid,rev bigint,model uuid,dep uuid,ep text,pid text,meta jsonb,creating boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE d jsonb:=gateway_v1.execution_deployment(kid,rev,model,dep,ep);
BEGIN
 IF creating THEN RAISE EXCEPTION 'missing_execution_origin'; END IF;
 RETURN publication.v1_container_bind(kid,d,pid,ep,meta,creating);
END $$;
CREATE FUNCTION gateway_v1.container_resolve(handle uuid,kid uuid,rev bigint,ep text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c jsonb:=publication.v1_container(handle,kid,ep); d jsonb;
BEGIN
 IF c IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 d=gateway_v1.execution_deployment(kid,rev,(c->>'model_id')::uuid,(c->>'deployment_id')::uuid,ep);
 IF d->>'credential_id'<>c->>'credential_id' OR d->>'credential_version'<>c->>'credential_version'
    OR d->>'upstream_version'<>c->>'upstream_version' THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN jsonb_build_object('model',c->'model_id','deployment',c->'deployment_id','provider_id',c->'provider_id','metadata',c->'metadata');
END $$;
CREATE FUNCTION gateway_v1.container_remove(handle uuid,kid uuid,rev bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.container_resolve(handle,kid,rev,'openai_responses'); PERFORM publication.v1_container_remove(handle,kid); END $$;
CREATE FUNCTION gateway_v1.container_file_bind(handle uuid,kid uuid,rev bigint,pid text,creating boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.container_resolve(handle,kid,rev,'openai_responses'); RETURN publication.v1_container_file_bind(handle,kid,pid,creating); END $$;
CREATE FUNCTION gateway_v1.container_file(handle uuid,kid uuid,rev bigint,fid uuid,removing boolean) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.container_resolve(handle,kid,rev,'openai_responses'); RETURN publication.v1_container_file(handle,kid,fid,removing); END $$;
CREATE FUNCTION gateway_v1.execution_file(kid uuid,rev bigint,model uuid,dep uuid,pid text,creating boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE d jsonb:=gateway_v1.execution_deployment(kid,rev,model,dep,'anthropic_messages');
BEGIN
 IF creating THEN RAISE EXCEPTION 'missing_execution_origin'; END IF;
 RETURN publication.v1_execution_file(kid,d,pid,creating);
END $$;
CREATE FUNCTION gateway_v1.batch_execution(batch uuid,custom text,kid uuid,rev bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE item jsonb; req jsonb;
BEGIN
 PERFORM gateway_v1.batch_resolve(batch,kid,rev);
 item=publication.v1_batch_execution(batch,custom,kid);
 IF item IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 req=billing.v1_status((item->>'rid')::uuid,(item->>'node')::uuid);
 RETURN item||jsonb_build_object('execution',req->>'execution_mode'<>'none');
END $$;
CREATE FUNCTION gateway_v1.execution_file_refresh(handle uuid,kid uuid,rev bigint,meta jsonb) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM gateway_v1.native_file_resolve(handle,kid,rev,'anthropic_messages');
 PERFORM publication.v1_execution_file_refresh(handle,kid,meta);
END $$;
CREATE FUNCTION gateway_v1.execution_output(rid uuid,kid uuid,dep uuid,pid text,is_file boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE f jsonb:=billing.v1_execution_attempt(rid,kid,dep,(publication.v1_principal()).cluster_id); d jsonb;
BEGIN
 IF f IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 SELECT value INTO STRICT d FROM jsonb_array_elements(publication.v1_snapshot((f->>'revision')::bigint,false)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=dep::text AND value->>'model_id'=f->>'model' AND value#>>'{relay_profile,endpoint}'='anthropic_messages'
 AND (value#>>'{relay_profile,code_execution}'='true' OR (f->>'mode'='bundled' AND (f->>'searches')::int>0));
 IF is_file THEN RETURN publication.v1_execution_file(kid,d,pid,true); END IF;
 RETURN publication.v1_container_bind(kid,d,pid,'anthropic_messages',NULL,true);
END $$;

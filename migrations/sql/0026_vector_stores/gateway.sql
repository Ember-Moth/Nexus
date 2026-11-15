CREATE FUNCTION gateway_v1.vector_prepare(handle uuid,kid uuid,rev bigint,model uuid,dep uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION gateway_v1.vector_binding(handle uuid,kid uuid,rev bigint,cleanup boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
 RETURN v||jsonb_build_object('stamp',clock_timestamp());
END $$;
REVOKE ALL ON FUNCTION gateway_v1.vector_binding(uuid,uuid,bigint,boolean) FROM PUBLIC;
CREATE FUNCTION gateway_v1.vector_resolve(handle uuid,kid uuid,rev bigint) RETURNS jsonb LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$SELECT gateway_v1.vector_binding(handle,kid,rev,false)$$;
CREATE FUNCTION gateway_v1.vector_cleanup(handle uuid,kid uuid,rev bigint) RETURNS jsonb LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$SELECT gateway_v1.vector_binding(handle,kid,rev,true)$$;
CREATE FUNCTION gateway_v1.vector_page(kid uuid,rev bigint,stamp uuid,asc_order boolean,n integer) RETURNS SETOF jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE visible jsonb; allowed uuid[];
BEGIN
 visible=gateway_v1.preflight(kid,rev,NULL,NULL);
 SELECT array_agg((model->>'id')::uuid) INTO allowed FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,models}') model
 WHERE EXISTS(SELECT FROM jsonb_array_elements(visible->'data') v WHERE v->>'id'=model->>'public_name');
 RETURN QUERY SELECT * FROM publication.v1_vectors(kid,stamp,asc_order,n,allowed);
END $$;
CREATE FUNCTION gateway_v1.vector_claim() RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT j||jsonb_build_object('stamp',clock_timestamp()) FROM publication.v1_vector_claim() j
$$;
CREATE FUNCTION gateway_v1.vector_observe(handle uuid,kid uuid,rev bigint,pid text,observed timestamptz,meta jsonb,new_state text,uncertain boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION gateway_v1.vector_batch(store uuid,kid uuid,rev bigint,id_in uuid,pid text,create_in boolean) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.vector_resolve(store,kid,rev); RETURN publication.v1_vector_batch(store,id_in,pid,create_in); END $$;
CREATE FUNCTION gateway_v1.vector_id(kid uuid,rev bigint,model uuid,dep uuid,pid text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.preflight(kid,rev,model,dep); RETURN publication.v1_vector_id(kid,dep,pid); END $$;

CREATE FUNCTION gateway_v1.vector_writable(handle uuid,kid uuid,rev bigint) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.vector_resolve(handle,kid,rev); PERFORM billing.v1_storage_write(handle); END $$;
CREATE FUNCTION gateway_v1.vector_missed(handle uuid,stamp timestamptz) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF stamp IS NULL OR stamp>clock_timestamp() OR NOT publication.v1_vector_mark_missed(handle,stamp) THEN RETURN; END IF;
 PERFORM billing.v1_storage_missed(handle,clock_timestamp());
END $$;
CREATE FUNCTION gateway_v1.vector_due(handle uuid,kid uuid,rev bigint) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.vector_resolve(handle,kid,rev); PERFORM publication.v1_vector_due(handle,kid); END $$;
CREATE FUNCTION gateway_v1.vector_locator(handle uuid,kid uuid,pid text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE prior jsonb:=publication.v1_vector(handle,kid);
BEGIN
 PERFORM publication.v1_vector_locator(handle,kid,pid);
 IF prior->>'state'='submitting' AND prior->>'provider_id' IS NULL THEN
  PERFORM billing.v1_storage_missed(handle,clock_timestamp());
 END IF;
END $$;

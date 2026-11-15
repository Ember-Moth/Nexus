CREATE FUNCTION gateway_v1.container_admit(origin uuid,kid uuid,rev bigint,model uuid,dep uuid,requested uuid,tier text,fresh boolean,files_present boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE d jsonb:=gateway_v1.execution_deployment(kid,rev,model,dep,'openai_responses'); c jsonb; principal jsonb; dims jsonb;
 p publication.gateway_principals:=publication.v1_principal(); cap integer:=coalesce((d#>>'{relay_profile,container_reserve_minutes}')::int,0);
 tariff text:=coalesce(d#>>'{relay_profile,container_tariff}','session_20m');
BEGIN
 IF tier NOT IN ('1g','4g','16g','64g') OR cap<(CASE WHEN tariff='minute_min_5' THEN 5 ELSE 20 END) THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 IF requested IS NOT NULL THEN
  c=gateway_v1.container_resolve(requested,kid,rev,'openai_responses');
  IF c->>'deployment'<>dep::text OR c#>>'{metadata,memory_limit}' IS DISTINCT FROM tier THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 END IF;
 principal=identity.v1_authorize(kid,model);
 dims=catalog.v1_eligible(model,dep,publication.v1_snapshot(rev,true)->'manifest',(principal->>'organization_id')::uuid,
    (d#>>'{relay_profile,input_ceiling}')::bigint,(d#>>'{relay_profile,output_ceiling}')::bigint);
 IF NOT dims ? ('container_'||tier||'_minutes') THEN RAISE EXCEPTION 'price_unavailable'; END IF;
 PERFORM publication.v1_container_operation(origin,kid,rev,d,requested,tier,files_present);
 PERFORM billing.v1_container_hold(origin,requested,principal,kid,model,dep,dims,tier,tariff,cap,p.node_id,p.cluster_id,rev,fresh,(d->>'upstream_id')::uuid);
END $$;
CREATE FUNCTION gateway_v1.reserve_interpreter(rid uuid,kid uuid,model uuid,rev bigint,inputs bigint,outputs bigint,searches integer,files integer,dep uuid,requested uuid,tier text,files_present boolean)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE reply jsonb;
BEGIN
 reply=gateway_v1.reserve_tools(rid,kid,model,rev,inputs,outputs,false,searches,files);
 PERFORM gateway_v1.container_admit(rid,kid,rev,model,dep,requested,tier,false,files_present);
 RETURN reply;
END $$;
CREATE FUNCTION gateway_v1.container_receipt(origin_id uuid,kid uuid,pid text,meta jsonb,executed boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE o jsonb:=publication.v1_container_origin(origin_id); d jsonb; result uuid; info jsonb;
BEGIN
 IF o IS NULL OR o->>'kid'<>kid::text OR (o->>'cleanup')::boolean THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 -- The supplier accepted an already admitted operation. Persist its result on the frozen binding,
 -- including after key revocation; future customer access checks the live key and deployment.
 IF meta ? 'memory_limit' AND meta->>'memory_limit' IS DISTINCT FROM o->>'memory_tier' THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 IF o->>'requested_handle' IS NOT NULL AND publication.v1_container((o->>'requested_handle')::uuid,kid,'openai_responses')->>'provider_id' IS DISTINCT FROM pid
 THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 d=jsonb_build_object('id',o->'deployment_id','model_id',o->'model_id','credential_id',o->'credential_id',
       'credential_version',o->'credential_version','upstream_version',o->'upstream_version');
 info=jsonb_strip_nulls(jsonb_build_object('object','container','name',meta->'name','created_at',meta->'created_at',
   'last_active_at',meta->'last_active_at','expires_after',meta->'expires_after','status',meta->'status',
   'memory_limit',o->'memory_tier','network_policy',meta->'network_policy'));
 result=publication.v1_container_bind(kid,d,pid,'openai_responses',info,true);
 PERFORM publication.v1_container_attest(origin_id,result);
 PERFORM billing.v1_container_link(origin_id,result,kid,(o->>'deployment_id')::uuid,pid,(publication.v1_container(result,kid,'openai_responses')#>>'{metadata,expires_after,minutes}')::int,executed,
     publication.v1_container(result,kid,'openai_responses')#>>'{metadata,memory_limit}',coalesce(meta ? 'memory_limit',false));
 RETURN result;
END $$;
CREATE FUNCTION gateway_v1.container_no_use(origin_id uuid,kid uuid,definite boolean) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE o jsonb:=publication.v1_container_origin(origin_id);
BEGIN
 IF o IS NOT NULL AND o->>'kid'=kid::text AND (definite OR NOT (o->>'preloaded')::boolean) THEN PERFORM billing.v1_container_release(origin_id,kid,(o->>'deployment_id')::uuid); END IF;
END $$;
CREATE FUNCTION gateway_v1.containers(kid uuid,rev bigint,cursor_id uuid,asc_order boolean,n integer) RETURNS SETOF jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE visible jsonb:=gateway_v1.preflight(kid,rev,NULL,NULL); allowed uuid[];
BEGIN
 SELECT array_agg((m->>'id')::uuid) INTO allowed FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,models}') m
 WHERE EXISTS(SELECT FROM jsonb_array_elements(visible->'data') v WHERE v->>'id'=m->>'public_name');
 RETURN QUERY SELECT * FROM publication.v1_containers(kid,cursor_id,asc_order,n,allowed);
END $$;
CREATE OR REPLACE FUNCTION gateway_v1.container_remove(handle uuid,kid uuid,rev bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM gateway_v1.container_resolve(handle,kid,rev,'openai_responses');
 PERFORM publication.v1_container_remove(handle,kid);
 PERFORM billing.v1_container_due(handle,kid,true);
END $$;
CREATE OR REPLACE FUNCTION gateway_v1.cancel_unstarted_request(rid uuid,reason text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals:=publication.v1_principal(); status jsonb;
BEGIN
 status=billing.v1_cancel(rid,p.node_id,reason);
 PERFORM gateway_v1.container_no_use(rid,(status->>'api_key_id')::uuid,true);
 RETURN status;
END $$;
CREATE FUNCTION gateway_v1.container_cleanup(origin_id uuid,handle_id uuid,kid uuid,rev bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c jsonb; d jsonb;
BEGIN
 PERFORM gateway_v1.preflight(kid,rev,NULL,NULL);
 c=publication.v1_container_cleanup(origin_id,handle_id,kid,rev);
 SELECT value INTO d FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=c->>'deployment_id' AND value->>'credential_id'=c->>'credential_id' AND
 value->>'credential_version'=c->>'credential_version' AND value->>'upstream_version'=c->>'upstream_version'
 AND value#>>'{relay_profile,endpoint}'='openai_responses';
 IF d IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN jsonb_build_object('model',c->'model_id','deployment',c->'deployment_id','provider_id',c->'provider_id','metadata',c->'metadata');
END $$;
CREATE FUNCTION gateway_v1.container_retired(origin_id uuid,kid uuid,pid text,confirmed boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE o jsonb:=publication.v1_container_origin(origin_id); c jsonb; h uuid:=(o->>'handle')::uuid;
BEGIN
 IF o IS NULL OR o->>'kid'<>kid::text OR o->>'cleanup'<>'true' THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 c=publication.v1_container(h,kid,'openai_responses');
 IF c IS NULL THEN RETURN; END IF;
 IF c->>'provider_id'<>pid THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 PERFORM publication.v1_container_remove(h,kid); PERFORM billing.v1_container_due(h,kid,confirmed);
END $$;
CREATE FUNCTION gateway_v1.stored_interpreter(handle_id uuid,kid uuid,rev bigint) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN PERFORM gateway_v1.resolve_response(handle_id,kid,rev); RETURN publication.v1_stored_interpreter(handle_id,kid); END $$;

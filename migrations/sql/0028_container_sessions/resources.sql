CREATE TABLE publication.container_operations(
 origin uuid PRIMARY KEY,kid uuid NOT NULL,cluster_id uuid NOT NULL,node_id uuid NOT NULL,revision bigint NOT NULL,
 model_id uuid NOT NULL,deployment_id uuid NOT NULL,credential_id uuid NOT NULL,credential_version integer NOT NULL,upstream_version integer NOT NULL,
 requested_handle uuid,handle uuid,preloaded boolean NOT NULL DEFAULT false,memory_tier text NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION publication.v1_container_operation(origin_id uuid,kid uuid,rev bigint,d jsonb,requested uuid,tier text,files_present boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals:=publication.v1_principal();
BEGIN
 INSERT INTO publication.container_operations(origin,kid,cluster_id,node_id,revision,model_id,deployment_id,credential_id,credential_version,upstream_version,requested_handle,memory_tier,preloaded)
 VALUES(origin_id,kid,p.cluster_id,p.node_id,rev,(d->>'model_id')::uuid,(d->>'id')::uuid,(d->>'credential_id')::uuid,
   (d->>'credential_version')::int,(d->>'upstream_version')::int,requested,tier,files_present) ON CONFLICT(origin) DO NOTHING;
 IF NOT EXISTS(SELECT FROM publication.container_operations o WHERE o.origin=origin_id AND o.kid=$2 AND o.cluster_id=p.cluster_id AND o.node_id=p.node_id
   AND o.revision=rev AND o.deployment_id=(d->>'id')::uuid AND o.requested_handle IS NOT DISTINCT FROM requested AND o.memory_tier=tier AND o.preloaded=files_present)
 THEN RAISE EXCEPTION 'request_conflict'; END IF;
END $$;
CREATE FUNCTION publication.v1_container_origin(origin_id uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT to_jsonb(o) FROM publication.container_operations o WHERE origin=origin_id AND cluster_id=(publication.v1_principal()).cluster_id
$$;
CREATE FUNCTION publication.v1_container_attest(origin_id uuid,id_in uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 UPDATE publication.container_operations SET handle=id_in WHERE origin=origin_id AND cluster_id=(publication.v1_principal()).cluster_id AND
   (requested_handle IS NULL OR requested_handle=id_in) AND (handle IS NULL OR handle=id_in);
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;
CREATE FUNCTION publication.v1_containers(kid uuid,cursor_id uuid,ascending boolean,n integer,models uuid[]) RETURNS SETOF jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE cluster uuid:=(publication.v1_principal()).cluster_id; after_at bigint;
BEGIN
 IF n NOT BETWEEN 1 AND 101 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF cursor_id IS NOT NULL THEN
  SELECT coalesce((metadata->>'created_at')::bigint,floor(extract(epoch FROM created_at))::bigint) INTO after_at
   FROM publication.execution_containers WHERE id=cursor_id AND cluster_id=cluster AND api_key_id=kid AND endpoint='openai_responses';
  IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 END IF;
 RETURN QUERY SELECT to_jsonb(c) FROM publication.execution_containers c WHERE cluster_id=cluster AND api_key_id=kid AND endpoint='openai_responses'
 AND deleted_at IS NULL AND model_id=ANY(models) AND (cursor_id IS NULL OR
   CASE WHEN ascending THEN (coalesce((metadata->>'created_at')::bigint,floor(extract(epoch FROM created_at))::bigint),id)>(after_at,cursor_id)
   ELSE (coalesce((metadata->>'created_at')::bigint,floor(extract(epoch FROM created_at))::bigint),id)<(after_at,cursor_id) END)
 ORDER BY CASE WHEN ascending THEN coalesce((metadata->>'created_at')::bigint,floor(extract(epoch FROM created_at))::bigint) END ASC,
   CASE WHEN ascending THEN id END ASC,coalesce((metadata->>'created_at')::bigint,floor(extract(epoch FROM created_at))::bigint) DESC,id DESC LIMIT n;
END $$;
ALTER TABLE publication.container_operations ADD COLUMN cleanup boolean NOT NULL DEFAULT false;
CREATE FUNCTION publication.v1_container_cleanup(origin_id uuid,handle_id uuid,kid_in uuid,rev bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c jsonb:=publication.v1_container(handle_id,kid_in,'openai_responses'); p publication.gateway_principals:=publication.v1_principal();
BEGIN
 IF c IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 INSERT INTO publication.container_operations(origin,kid,cluster_id,node_id,revision,model_id,deployment_id,credential_id,credential_version,
    upstream_version,requested_handle,handle,memory_tier,cleanup)
 VALUES(origin_id,kid_in,p.cluster_id,p.node_id,rev,(c->>'model_id')::uuid,(c->>'deployment_id')::uuid,(c->>'credential_id')::uuid,
    (c->>'credential_version')::int,(c->>'upstream_version')::int,handle_id,handle_id,c#>>'{metadata,memory_limit}',true);
 RETURN c;
END $$;
CREATE FUNCTION publication.v1_stored_interpreter(handle_id uuid,kid_in uuid) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT o.origin FROM publication.stored_responses s JOIN publication.container_operations o ON o.origin=s.request_id AND o.kid=s.api_key_id
 WHERE s.id=handle_id AND s.api_key_id=kid_in AND s.cluster_id=(publication.v1_principal()).cluster_id AND s.deleted_at IS NULL AND NOT o.cleanup
$$;

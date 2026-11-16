-- publication functions and trigger functions.
-- Created by their owning role, so their privileges are exactly the grants in 17_privileges.sql.

-- Owned by nexus_publication.
SET LOCAL ROLE nexus_publication;

CREATE FUNCTION publication.index_snapshot(cid uuid, rev bigint) RETURNS void
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE nil constant uuid:='00000000-0000-0000-0000-000000000000'; m jsonb; stale bigint;
BEGIN
 SELECT s.manifest->'resources' INTO STRICT m FROM publication.config_snapshots s
 WHERE s.cluster_id=cid AND s.revision=rev;
 INSERT INTO publication.snapshot_index(cluster_id,revision,kind,primary_id,secondary_id,body)
 SELECT cid,rev,'route',(r->>'organization_id')::uuid,(r->>'model_id')::uuid,r
  FROM jsonb_array_elements(m->'routes') r
 UNION ALL SELECT cid,rev,'deployment',(d->>'id')::uuid,nil,d FROM jsonb_array_elements(m->'deployments') d
 UNION ALL SELECT cid,rev,'model',(x->>'id')::uuid,nil,x FROM jsonb_array_elements(m->'models') x
 UNION ALL SELECT cid,rev,'access',(a->>'organization_id')::uuid,nil,jsonb_agg(a)
  FROM jsonb_array_elements(m->'access_policies') a GROUP BY (a->>'organization_id')::uuid;
 SELECT max(s.revision) INTO stale FROM publication.config_snapshots s
 WHERE s.cluster_id=cid AND s.revision<rev AND s.created_at<clock_timestamp()-interval '1 day';
 IF stale IS NOT NULL THEN
  DELETE FROM publication.snapshot_index WHERE cluster_id=cid AND revision<stale;
 END IF;
END $$;

CREATE FUNCTION publication.stage_keys(cid uuid, keys jsonb) RETURNS bigint
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE cur_seq bigint; next_seq bigint; changed bigint:=0; n bigint; horizon bigint;
BEGIN
 SELECT key_sequence INTO STRICT cur_seq FROM publication.gateway_clusters WHERE id=cid FOR NO KEY UPDATE;
 next_seq=cur_seq+1;
 WITH incoming AS MATERIALIZED (SELECT * FROM jsonb_to_recordset(keys) AS x(id uuid, digest text,
  organization_id uuid, project_id uuid, service_account_id uuid, expires_at timestamptz))
 UPDATE publication.cluster_keys k SET removed_sequence=next_seq
 WHERE k.cluster_id=cid AND k.removed_sequence IS NULL AND NOT EXISTS(SELECT FROM incoming i
  WHERE i.id=k.key_id AND i.digest=k.digest AND i.organization_id=k.organization_id
  AND i.project_id=k.project_id AND i.service_account_id=k.service_account_id
  AND i.expires_at IS NOT DISTINCT FROM k.expires_at);
 GET DIAGNOSTICS n=ROW_COUNT; changed=changed+n;
 INSERT INTO publication.cluster_keys(cluster_id,key_id,added_sequence,digest,organization_id,
  project_id,service_account_id,expires_at)
 SELECT cid,i.id,next_seq,i.digest,i.organization_id,i.project_id,i.service_account_id,i.expires_at
 FROM jsonb_to_recordset(keys) AS i(id uuid, digest text, organization_id uuid, project_id uuid,
  service_account_id uuid, expires_at timestamptz)
 WHERE NOT EXISTS(SELECT FROM publication.cluster_keys k
  WHERE k.cluster_id=cid AND k.key_id=i.id AND k.removed_sequence IS NULL);
 GET DIAGNOSTICS n=ROW_COUNT; changed=changed+n;
 SELECT min(s.key_sequence) INTO horizon FROM publication.gateway_principals g
 JOIN publication.config_snapshots s ON s.cluster_id=g.cluster_id AND s.revision=g.applied_revision
 WHERE g.cluster_id=cid AND g.active;
 IF horizon IS NOT NULL THEN
  DELETE FROM publication.cluster_keys WHERE cluster_id=cid AND removed_sequence<=horizon;
 END IF;
 IF changed=0 THEN RETURN cur_seq; END IF;
 UPDATE publication.gateway_clusters SET key_sequence=next_seq WHERE id=cid;
 RETURN next_seq;
END $$;

CREATE FUNCTION publication.v1_ack(rev bigint, hash text, applied boolean, reason text) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $_$
DECLARE p publication.gateway_principals; r jsonb;
BEGIN
 -- Serialize acknowledgements on this node without upgrading a shared row lock.
 p=publication.v1_principal(true);
 r=publication.v1_revision(p,rev,false);
 IF hash IS DISTINCT FROM r->>'sha256' OR applied IS NULL OR reason IS NULL OR reason !~ '^[a-z0-9_]{0,64}$'
 THEN RAISE EXCEPTION 'invalid_ack'; END IF;
 INSERT INTO publication.node_config_acks(node_id,cluster_id,revision,digest,applied,error_code)
 VALUES(p.node_id,p.cluster_id,rev,hash,applied,reason);
 IF applied THEN
  IF EXISTS(SELECT FROM jsonb_array_elements_text(r->'required_capabilities') c
            WHERE NOT (c=ANY(p.capabilities))) THEN RAISE EXCEPTION 'unsupported_capability'; END IF;
  UPDATE publication.gateway_principals SET applied_revision=rev,applied_at=clock_timestamp()
   WHERE node_id=p.node_id AND applied_revision<rev;
 END IF;
 RETURN jsonb_build_object('revision',rev::text,'applied',applied);
END $_$;

CREATE FUNCTION publication.v1_admission(p publication.gateway_principals, rev bigint, kid uuid, org uuid, model uuid, for_admission boolean) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE r jsonb; k jsonb; seq bigint; routes jsonb; deps uuid[]; models uuid[];
 nil constant uuid:='00000000-0000-0000-0000-000000000000';
BEGIN
 r=publication.v1_revision(p,rev,for_admission);
 seq=(r->>'key_sequence')::bigint; rev=(r->>'revision')::bigint;
 IF kid IS NOT NULL THEN
  SELECT jsonb_build_object('id',c.key_id,'organization_id',c.organization_id,'project_id',c.project_id,
   'service_account_id',c.service_account_id,'expires_at',c.expires_at) INTO k
  FROM publication.cluster_keys c WHERE c.cluster_id=p.cluster_id AND c.key_id=kid
   AND c.added_sequence<=seq AND (c.removed_sequence IS NULL OR c.removed_sequence>seq);
  org=coalesce(org,(k->>'organization_id')::uuid);
 END IF;
 SELECT coalesce(jsonb_agg(i.body ORDER BY i.secondary_id),'[]'::jsonb),
  array_agg(DISTINCT i.secondary_id) INTO routes,models
 FROM publication.snapshot_index i WHERE i.cluster_id=p.cluster_id AND i.revision=rev
  AND i.kind='route' AND i.primary_id=org AND (model IS NULL OR i.secondary_id=model);
 SELECT array_agg(DISTINCT (c->>'deployment_id')::uuid) INTO deps
 FROM jsonb_array_elements(routes) x, jsonb_array_elements(x->'candidates') c;
 RETURN r || jsonb_build_object('key',k,'manifest',jsonb_build_object(
  'required_capabilities',r->'required_capabilities','resources',jsonb_build_object(
   'routes',routes,
   'deployments',(SELECT coalesce(jsonb_agg(i.body ORDER BY i.primary_id),'[]'::jsonb)
     FROM publication.snapshot_index i WHERE i.cluster_id=p.cluster_id AND i.revision=rev
     AND i.kind='deployment' AND i.primary_id=ANY(deps) AND i.secondary_id=nil),
   'models',(SELECT coalesce(jsonb_agg(i.body ORDER BY i.primary_id),'[]'::jsonb)
     FROM publication.snapshot_index i WHERE i.cluster_id=p.cluster_id AND i.revision=rev
     AND i.kind='model' AND (i.primary_id=ANY(models) OR i.primary_id=model) AND i.secondary_id=nil),
   'access_policies',coalesce((SELECT i.body FROM publication.snapshot_index i
     WHERE i.cluster_id=p.cluster_id AND i.revision=rev AND i.kind='access'
     AND i.primary_id=org AND i.secondary_id=nil),'[]'::jsonb))));
END $$;

CREATE FUNCTION publication.v1_background_claim() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE job publication.response_jobs; stored publication.stored_responses;
BEGIN
 WITH candidate AS (SELECT j.handle FROM publication.response_jobs j WHERE owner_node=(publication.v1_principal()).node_id
 AND NOT done AND next_poll<=clock_timestamp() ORDER BY next_poll LIMIT 1 FOR UPDATE SKIP LOCKED)
 UPDATE publication.response_jobs j SET next_poll=clock_timestamp()+interval '4 seconds'
 FROM candidate c WHERE j.handle=c.handle RETURNING j.* INTO job;
 IF job.handle IS NULL THEN RETURN NULL; END IF;
 SELECT * INTO STRICT stored FROM publication.stored_responses WHERE id=job.handle;
 RETURN to_jsonb(job)||jsonb_build_object('deployment',stored.deployment_id,'credential',stored.credential_id,
 'credential_version',stored.credential_version,'upstream_version',stored.upstream_version,'provider',stored.provider_id);
END $$;

CREATE FUNCTION publication.v1_background_register(handle uuid, rid uuid, aid uuid, rev bigint, attempt_event uuid, close_event uuid) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 INSERT INTO publication.response_jobs(handle,rid,aid,owner_node,revision,attempt_event,close_event)
 VALUES(handle,rid,aid,(publication.v1_principal()).node_id,rev,attempt_event,close_event) ON CONFLICT(handle) DO NOTHING
$$;

CREATE FUNCTION publication.v1_batch_claim() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE job publication.message_batches;
BEGIN
 WITH candidate AS (SELECT id FROM publication.message_batches WHERE owner_node=(publication.v1_principal()).node_id
    AND NOT done AND next_poll<=clock_timestamp() ORDER BY next_poll LIMIT 1 FOR UPDATE SKIP LOCKED)
 UPDATE publication.message_batches b SET next_poll=clock_timestamp()+interval '15 seconds'
 FROM candidate c WHERE b.id=c.id RETURNING b.* INTO job;
 IF job.id IS NULL THEN RETURN NULL; END IF;
 RETURN to_jsonb(job);
END $$;

CREATE FUNCTION publication.v1_batch_done(handle uuid) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 UPDATE publication.message_batches SET done=true WHERE id=handle AND owner_node=(publication.v1_principal()).node_id
$$;

CREATE FUNCTION publication.v1_batch_execution(batch uuid, custom text, kid uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT jsonb_build_object('rid',i.rid,'model',i.model_id,'deployment',i.deployment_id,'node',b.owner_node)
 FROM publication.batch_items i JOIN publication.message_batches b ON b.id=i.batch_id
 WHERE b.id=batch AND i.custom_id=custom AND b.api_key_id=kid AND b.cluster_id=(publication.v1_principal()).cluster_id AND NOT b.deleted
$$;

CREATE FUNCTION publication.v1_batch_items(handle uuid) RETURNS TABLE(custom_id text, rid uuid, aid uuid)
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT i.custom_id::text,i.rid,i.aid FROM publication.batch_items i JOIN publication.message_batches b ON b.id=i.batch_id
 WHERE b.id=handle AND b.owner_node=(publication.v1_principal()).node_id
$$;

CREATE FUNCTION publication.v1_batch_lock(handle uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT to_jsonb(b) FROM publication.message_batches b WHERE id=handle AND owner_node=(publication.v1_principal()).node_id FOR UPDATE
$$;

CREATE FUNCTION publication.v1_batch_lookup(handle uuid, kid uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT to_jsonb(b)||jsonb_build_object('targets',(SELECT jsonb_agg(DISTINCT jsonb_build_object('model', model_id,'deployment',deployment_id))
 FROM publication.batch_items WHERE batch_id=b.id)) FROM publication.message_batches b WHERE id=handle AND api_key_id=kid
 AND cluster_id=(publication.v1_principal()).cluster_id AND provider_id IS NOT NULL AND NOT deleted
$$;

CREATE FUNCTION publication.v1_batch_page(kid uuid, stamp uuid, newer boolean, n integer) RETURNS SETOF jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE cluster uuid := (publication.v1_principal()).cluster_id; cursor_at timestamptz;
BEGIN
 IF stamp IS NOT NULL THEN
  SELECT created_at INTO cursor_at FROM publication.message_batches WHERE id=stamp AND api_key_id=kid AND cluster_id=cluster;
  IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 END IF;
 IF n NOT BETWEEN 1 AND 1001 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 RETURN QUERY SELECT metadata FROM publication.message_batches WHERE api_key_id=kid AND cluster_id=cluster AND NOT deleted
 AND provider_id IS NOT NULL AND
 (stamp IS NULL OR CASE WHEN newer THEN (created_at,id)>(cursor_at,stamp) ELSE (created_at,id)<(cursor_at,stamp) END)
 ORDER BY created_at DESC,id DESC LIMIT n;
END $$;

CREATE FUNCTION publication.v1_batch_prepare(handle uuid, kid uuid, rev bigint, d jsonb, items jsonb, h jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals := publication.v1_principal();
BEGIN
 INSERT INTO publication.message_batches(id,api_key_id,cluster_id,owner_node,revision,model_id,deployment_id,
    credential_id,credential_version,upstream_version,headers,next_poll)
 VALUES(handle,kid,p.cluster_id,p.node_id,rev,(d->>'model_id')::uuid,(d->>'id')::uuid,
    (d->>'credential_id')::uuid,(d->>'credential_version')::integer,(d->>'upstream_version')::integer,
    h,clock_timestamp()+interval '3 minutes');
 INSERT INTO publication.batch_items(batch_id,custom_id,rid,aid,model_id,deployment_id)
 SELECT handle,t.custom_id,t.rid,t.aid,t.model,t.deployment FROM jsonb_to_recordset(items) AS t(custom_id text,rid uuid,aid uuid,model uuid,deployment uuid);
END $$;

CREATE FUNCTION publication.v1_batch_update(handle uuid, pid text, meta jsonb, removing boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 UPDATE publication.message_batches SET provider_id=pid, metadata=CASE WHEN removing THEN metadata
    WHEN metadata->>'processing_status'='ended' THEN metadata ELSE meta END,
    deleted=deleted OR removing, next_poll=CASE WHEN provider_id IS NULL THEN clock_timestamp() ELSE next_poll END
 WHERE id=handle AND cluster_id=(publication.v1_principal()).cluster_id AND
   (provider_id=pid OR (provider_id IS NULL AND owner_node=(publication.v1_principal()).node_id AND NOT done))
   AND (NOT removing OR done);
 IF NOT FOUND THEN RAISE EXCEPTION 'batch_unavailable'; END IF;
END $$;

CREATE FUNCTION publication.v1_config_head() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 RETURN publication.v1_revision(publication.v1_principal(),NULL,false);
END $$;

CREATE FUNCTION publication.v1_config_keys(rev bigint, since bigint, after_sequence bigint, after_key uuid, after_present boolean, page integer) RETURNS TABLE(change_sequence bigint, change_key uuid, present boolean, digest text, organization_id uuid, project_id uuid, service_account_id uuid, expires_at timestamp with time zone)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals; r jsonb; target bigint;
 cs bigint:=coalesce(after_sequence,0); ck uuid:=coalesce(after_key,'00000000-0000-0000-0000-000000000000');
 cp boolean:=coalesce(after_present,false);
BEGIN
 p=publication.v1_principal(); r=publication.v1_revision(p,rev,false); target=(r->>'key_sequence')::bigint;
 IF since IS NULL OR since<0 OR since>target OR page IS NULL OR page NOT BETWEEN 1 AND 10000
 THEN RAISE EXCEPTION 'invalid_cursor'; END IF;
 RETURN QUERY SELECT x.s,x.k,x.present,x.digest,x.organization_id,x.project_id,x.service_account_id,x.expires_at FROM (
  SELECT c.added_sequence s,c.key_id k,true present,c.digest,c.organization_id,c.project_id,
   c.service_account_id,c.expires_at
  FROM publication.cluster_keys c WHERE c.cluster_id=p.cluster_id
   AND c.added_sequence>greatest(since,cs-1) AND c.added_sequence<=target
   AND (c.removed_sequence IS NULL OR c.removed_sequence>target)
   AND (c.added_sequence,c.key_id,true)>(cs,ck,cp)
  UNION ALL
  SELECT c.removed_sequence,c.key_id,false,NULL,NULL,NULL,NULL,NULL
  FROM publication.cluster_keys c WHERE c.cluster_id=p.cluster_id
   AND c.removed_sequence>greatest(since,cs-1) AND c.removed_sequence<=target AND c.added_sequence<=since
   AND (c.removed_sequence,c.key_id,false)>(cs,ck,cp)
 ) x ORDER BY x.s,x.k,x.present LIMIT page;
END $$;

CREATE FUNCTION publication.v1_config_payload(rev bigint) RETURNS bytea
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals; r jsonb;
BEGIN
 p=publication.v1_principal(); r=publication.v1_revision(p,rev,false);
 RETURN (SELECT s.payload_bytes FROM publication.config_snapshots s
  WHERE s.cluster_id=p.cluster_id AND s.revision=(r->>'revision')::bigint);
END $$;

CREATE FUNCTION publication.v1_container(handle uuid, kid uuid, ep text) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT to_jsonb(c) FROM publication.execution_containers c WHERE id=handle AND api_key_id=kid AND endpoint=ep AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;

CREATE FUNCTION publication.v1_container_attest(origin_id uuid, id_in uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 UPDATE publication.container_operations SET handle=id_in WHERE origin=origin_id AND cluster_id=(publication.v1_principal()).cluster_id AND
   (requested_handle IS NULL OR requested_handle=id_in) AND (handle IS NULL OR handle=id_in);
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;

CREATE FUNCTION publication.v1_container_bind(kid uuid, d jsonb, pid text, ep text, meta jsonb, creating boolean) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $_$
DECLARE c publication.execution_containers; cluster uuid:=(publication.v1_principal()).cluster_id;
BEGIN
 IF pid IS NULL OR pid !~ '^[A-Za-z0-9_-]{1,200}$' OR ep NOT IN ('openai_responses','anthropic_messages') THEN RAISE EXCEPTION 'invalid_container'; END IF;
 IF creating THEN
  INSERT INTO publication.execution_containers(id,cluster_id,api_key_id,model_id,deployment_id,endpoint,credential_id,credential_version,
      upstream_version,provider_id,metadata) VALUES(gen_random_uuid(),cluster,kid,(d->>'model_id')::uuid,(d->>'id')::uuid,ep,
     (d->>'credential_id')::uuid,(d->>'credential_version')::integer,(d->>'upstream_version')::integer,pid,coalesce(meta,'{}'))
  ON CONFLICT(cluster_id,credential_id,credential_version,provider_id) DO NOTHING;
 END IF;
 SELECT * INTO c FROM publication.execution_containers WHERE cluster_id=cluster AND credential_id=(d->>'credential_id')::uuid
 AND credential_version=(d->>'credential_version')::integer AND provider_id=pid AND endpoint=ep AND api_key_id=kid
 AND deployment_id=(d->>'id')::uuid AND model_id=(d->>'model_id')::uuid AND upstream_version=(d->>'upstream_version')::integer AND deleted_at IS NULL;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF meta IS NOT NULL THEN UPDATE publication.execution_containers SET metadata=metadata||meta||
  CASE WHEN c.metadata ? 'memory_limit' THEN jsonb_build_object('memory_limit',c.metadata->'memory_limit') ELSE '{}'::jsonb END WHERE id=c.id; END IF;
 RETURN c.id;
END $_$;

CREATE FUNCTION publication.v1_container_cleanup(origin_id uuid, handle_id uuid, kid_in uuid, rev bigint) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE c jsonb:=publication.v1_container(handle_id,kid_in,'openai_responses'); p publication.gateway_principals:=publication.v1_principal();
BEGIN
 IF c IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 INSERT INTO publication.container_operations(origin,kid,cluster_id,node_id,revision,model_id,deployment_id,credential_id,credential_version,
    upstream_version,requested_handle,handle,memory_tier,cleanup)
 VALUES(origin_id,kid_in,p.cluster_id,p.node_id,rev,(c->>'model_id')::uuid,(c->>'deployment_id')::uuid,(c->>'credential_id')::uuid,
    (c->>'credential_version')::int,(c->>'upstream_version')::int,handle_id,handle_id,c#>>'{metadata,memory_limit}',true);
 RETURN c;
END $$;

CREATE FUNCTION publication.v1_container_file(handle uuid, kid uuid, fid uuid, removing boolean) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE pid text;
BEGIN
 IF publication.v1_container(handle,kid,'openai_responses') IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 SELECT provider_id INTO pid FROM publication.container_files WHERE id=fid AND container_id=handle AND deleted_at IS NULL;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF removing THEN UPDATE publication.container_files SET deleted_at=clock_timestamp() WHERE id=fid; END IF;
 RETURN pid;
END $$;

CREATE FUNCTION publication.v1_container_file_bind(handle uuid, kid uuid, pid text, creating boolean) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE fid uuid;
BEGIN
 IF publication.v1_container(handle,kid,'openai_responses') IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF creating THEN
  INSERT INTO publication.container_files(id,container_id,provider_id) VALUES(gen_random_uuid(),handle,pid)
  ON CONFLICT(container_id,provider_id) DO NOTHING;
 END IF;
 SELECT id INTO fid FROM publication.container_files WHERE container_id=handle AND provider_id=pid AND deleted_at IS NULL;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN fid;
END $$;

CREATE FUNCTION publication.v1_container_operation(origin_id uuid, kid uuid, rev bigint, d jsonb, requested uuid, tier text, files_present boolean) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $_$
DECLARE p publication.gateway_principals:=publication.v1_principal();
BEGIN
 INSERT INTO publication.container_operations(origin,kid,cluster_id,node_id,revision,model_id,deployment_id,credential_id,credential_version,upstream_version,requested_handle,memory_tier,preloaded)
 VALUES(origin_id,kid,p.cluster_id,p.node_id,rev,(d->>'model_id')::uuid,(d->>'id')::uuid,(d->>'credential_id')::uuid,
   (d->>'credential_version')::int,(d->>'upstream_version')::int,requested,tier,files_present) ON CONFLICT(origin) DO NOTHING;
 IF NOT EXISTS(SELECT FROM publication.container_operations o WHERE o.origin=origin_id AND o.kid=$2 AND o.cluster_id=p.cluster_id AND o.node_id=p.node_id
   AND o.revision=rev AND o.deployment_id=(d->>'id')::uuid AND o.requested_handle IS NOT DISTINCT FROM requested AND o.memory_tier=tier AND o.preloaded=files_present)
 THEN RAISE EXCEPTION 'request_conflict'; END IF;
END $_$;

CREATE FUNCTION publication.v1_container_origin(origin_id uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT to_jsonb(o) FROM publication.container_operations o WHERE origin=origin_id AND cluster_id=(publication.v1_principal()).cluster_id
$$;

CREATE FUNCTION publication.v1_container_remove(handle uuid, kid uuid) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 UPDATE publication.execution_containers SET deleted_at=clock_timestamp() WHERE id=handle AND api_key_id=kid
 AND cluster_id=(publication.v1_principal()).cluster_id AND deleted_at IS NULL
$$;

CREATE FUNCTION publication.v1_containers(kid uuid, cursor_id uuid, ascending boolean, n integer, models uuid[]) RETURNS SETOF jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
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

CREATE FUNCTION publication.v1_conversation(handle uuid, kid uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT to_jsonb(c) FROM publication.conversations c WHERE id=handle AND api_key_id=kid AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;

CREATE FUNCTION publication.v1_conversation_id(kid uuid, dep uuid, pid text) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT id FROM publication.conversations WHERE api_key_id=kid AND deployment_id=dep AND provider_id=pid
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;

CREATE FUNCTION publication.v1_conversation_remove(handle uuid, kid uuid) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 UPDATE publication.conversations SET deleted_at=clock_timestamp() WHERE id=handle AND api_key_id=kid AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;

CREATE FUNCTION publication.v1_conversation_save(handle uuid, kid uuid, d jsonb, pid text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 INSERT INTO publication.conversations(id,api_key_id,cluster_id,model_id,deployment_id,credential_id,credential_version,
   upstream_version,provider_id) VALUES(handle,kid,(publication.v1_principal()).cluster_id,(d->>'model_id')::uuid,
   (d->>'id')::uuid,(d->>'credential_id')::uuid,(d->>'credential_version')::int,(d->>'upstream_version')::int,pid)
 ON CONFLICT(id) DO NOTHING;
 IF publication.v1_conversation_id(kid,(d->>'id')::uuid,pid) IS DISTINCT FROM handle THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;

CREATE FUNCTION publication.v1_envelope(rev bigint, cid uuid, ver integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals; e publication.credential_envelopes;
BEGIN
 p=publication.v1_principal();
 PERFORM publication.v1_revision(p,rev,false);
 SELECT * INTO e FROM publication.credential_envelopes WHERE cluster_id=p.cluster_id AND revision=rev
 AND credential_id=cid AND credential_version=ver;
 IF NOT FOUND THEN RAISE EXCEPTION 'credential_unavailable'; END IF;
 RETURN jsonb_build_object('algorithm','libsodium-sealed-box','key_version',e.key_version,'ciphertext',encode(e.ciphertext,'base64'));
END $$;

CREATE FUNCTION publication.v1_execution_file(kid uuid, d jsonb, pid text, creating boolean) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $_$
DECLARE f publication.tenant_files; cluster uuid:=(publication.v1_principal()).cluster_id; handle uuid;
BEGIN
 IF pid IS NULL OR pid !~ '^[A-Za-z0-9_-]{1,200}$' THEN RAISE EXCEPTION 'invalid_file_reference'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended((d->>'credential_id')||':'||pid,93817));
 IF EXISTS(SELECT FROM publication.tenant_files WHERE cluster_id=cluster AND credential_id=(d->>'credential_id')::uuid
 AND credential_version=(d->>'credential_version')::int AND provider_id=pid AND api_key_id<>kid AND endpoint='anthropic_messages')
 THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 SELECT * INTO f FROM publication.tenant_files WHERE cluster_id=cluster AND deployment_id=(d->>'id')::uuid AND api_key_id=kid
 AND credential_id=(d->>'credential_id')::uuid AND credential_version=(d->>'credential_version')::int
 AND upstream_version=(d->>'upstream_version')::int AND provider_id=pid AND endpoint='anthropic_messages'
 ORDER BY created_at DESC,id DESC LIMIT 1;
 IF FOUND THEN
  IF f.deleted_at IS NOT NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
  RETURN f.id;
 END IF;
 IF NOT creating THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 handle=gen_random_uuid();
 PERFORM publication.v1_native_file_save(handle,kid,(d->>'model_id')::uuid,d,jsonb_build_object('type','file','id',pid),'anthropic_messages');
 RETURN handle;
END $_$;

CREATE FUNCTION publication.v1_execution_file_refresh(handle uuid, kid uuid, meta jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 IF meta->>'type' IS DISTINCT FROM 'file' OR jsonb_typeof(meta->'filename') IS DISTINCT FROM 'string'
 OR jsonb_typeof(meta->'size_bytes') IS DISTINCT FROM 'number' OR jsonb_typeof(meta->'created_at') IS DISTINCT FROM 'string'
 OR jsonb_typeof(meta->'mime_type') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'invalid_file_metadata'; END IF;
 UPDATE publication.tenant_files SET metadata=jsonb_strip_nulls(jsonb_build_object('id','file_nx_'||handle::text,'type','file',
   'filename',meta->'filename','mime_type',meta->'mime_type','size_bytes',meta->'size_bytes','created_at',meta->'created_at',
   'downloadable',meta->'downloadable','expires_at',meta->'expires_at')),expires_at=(meta->>'expires_at')::timestamptz
 WHERE id=handle AND api_key_id=kid AND provider_id=meta->>'id' AND endpoint='anthropic_messages' AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
END $$;

CREATE FUNCTION publication.v1_file_remove(handle uuid, kid uuid) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 UPDATE publication.tenant_files SET deleted_at=clock_timestamp() WHERE id=handle AND api_key_id=kid AND cluster_id=(publication.v1_principal()).cluster_id AND deleted_at IS NULL
$$;

CREATE FUNCTION publication.v1_forget_response(handle uuid, kid uuid) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 UPDATE publication.stored_responses SET deleted_at=clock_timestamp() WHERE id=handle AND api_key_id=kid
 AND cluster_id=(publication.v1_principal()).cluster_id AND deleted_at IS NULL
$$;

CREATE FUNCTION publication.v1_job(handle_id uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT to_jsonb(j)||jsonb_build_object('provider',s.provider_id) FROM publication.response_jobs j
 JOIN publication.stored_responses s ON s.id=j.handle WHERE handle=handle_id AND owner_node=(publication.v1_principal()).node_id
 FOR UPDATE OF j
$$;

CREATE FUNCTION publication.v1_job_done(handle_id uuid) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 UPDATE publication.response_jobs SET done=true WHERE handle=handle_id AND owner_node=(publication.v1_principal()).node_id
$$;

CREATE FUNCTION publication.v1_job_pending(handle_id uuid) RETURNS boolean
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT EXISTS(SELECT FROM publication.response_jobs WHERE handle=handle_id AND NOT done)
$$;

CREATE FUNCTION publication.v1_native_file(handle uuid, kid uuid, ep text) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT jsonb_build_object('model',model_id,'deployment',deployment_id,'credential',credential_id,
    'credential_version',credential_version,'upstream_version',upstream_version,'provider_id',provider_id)
 FROM publication.tenant_files WHERE id=handle AND api_key_id=kid AND endpoint=ep AND deleted_at IS NULL
 AND (expires_at IS NULL OR expires_at>clock_timestamp()) AND cluster_id=(publication.v1_principal()).cluster_id
$$;

CREATE FUNCTION publication.v1_native_file_save(handle uuid, kid uuid, model uuid, d jsonb, meta jsonb, ep text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE view jsonb; expiry timestamptz; created timestamptz := clock_timestamp();
BEGIN
 IF ep='anthropic_messages' AND meta->>'type'='file' THEN
  view=jsonb_strip_nulls(jsonb_build_object('type','file','filename',meta->'filename','mime_type',meta->'mime_type',
     'created_at',meta->'created_at','size_bytes',meta->'size_bytes','downloadable',meta->'downloadable','expires_at',meta->'expires_at'));
  expiry=(meta->>'expires_at')::timestamptz;
 ELSIF ep='openai_responses' AND meta->>'object'='file' AND jsonb_typeof(meta->'created_at')='number'
 AND jsonb_typeof(meta->'purpose')='string' THEN
  SELECT jsonb_object_agg(key,value) INTO view FROM jsonb_each(meta)
   WHERE key IN ('object','bytes','created_at','expires_at','filename','purpose','status','status_details');
  expiry=to_timestamp((meta->>'expires_at')::bigint);
  created=to_timestamp((meta->>'created_at')::bigint);
 ELSE RAISE EXCEPTION 'invalid_file_metadata'; END IF;
 INSERT INTO publication.tenant_files(id,api_key_id,cluster_id,model_id,deployment_id,credential_id,credential_version,
    upstream_version,provider_id,metadata,endpoint,expires_at,created_at)
 VALUES(handle,kid,(publication.v1_principal()).cluster_id,model,(d->>'id')::uuid,(d->>'credential_id')::uuid,
    (d->>'credential_version')::int,(d->>'upstream_version')::int,meta->>'id',view||jsonb_build_object('id','file_nx_'||handle::text),ep,expiry,created)
 ON CONFLICT(id) DO NOTHING;
 IF NOT EXISTS(SELECT FROM publication.tenant_files WHERE id=handle AND api_key_id=kid AND model_id=model AND endpoint=ep
  AND cluster_id=(publication.v1_principal()).cluster_id AND deployment_id=(d->>'id')::uuid AND provider_id=meta->>'id'
  AND deleted_at IS NULL) THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;

CREATE FUNCTION publication.v1_native_files(kid uuid, stamp uuid, newer boolean, n integer, ep text, ascending boolean, purpose text) RETURNS SETOF jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE cluster uuid := (publication.v1_principal()).cluster_id; at_cursor timestamptz;
BEGIN
 IF ep NOT IN ('anthropic_messages','openai_responses') OR n NOT BETWEEN 1 AND
  (CASE ep WHEN 'openai_responses' THEN 10001 ELSE 1001 END) THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF stamp IS NOT NULL THEN
  SELECT created_at INTO at_cursor FROM publication.tenant_files WHERE id=stamp AND api_key_id=kid AND endpoint=ep AND cluster_id=cluster;
  IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 END IF;
 RETURN QUERY SELECT metadata FROM publication.tenant_files WHERE api_key_id=kid AND cluster_id=cluster AND endpoint=ep
 AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at>clock_timestamp()) AND (purpose IS NULL OR metadata->>'purpose'=purpose)
 AND (stamp IS NULL OR CASE WHEN newer THEN (created_at,id)>(at_cursor,stamp) ELSE (created_at,id)<(at_cursor,stamp) END)
 ORDER BY CASE WHEN ascending THEN created_at END ASC,CASE WHEN ascending THEN id END ASC,
   CASE WHEN NOT ascending THEN created_at END DESC,CASE WHEN NOT ascending THEN id END DESC LIMIT n;
END $$;

CREATE FUNCTION publication.v1_openai_file_id(kid uuid, dep uuid, pid text) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT id FROM publication.tenant_files WHERE api_key_id=kid AND cluster_id=(publication.v1_principal()).cluster_id
 AND deployment_id=dep AND provider_id=pid AND endpoint='openai_responses'
 ORDER BY (deleted_at IS NULL) DESC,created_at DESC,id DESC LIMIT 1
$$;

CREATE FUNCTION publication.v1_principal(for_ack boolean DEFAULT false) RETURNS publication.gateway_principals
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals; cid uuid;
BEGIN
 SELECT cluster_id INTO cid FROM publication.gateway_principals WHERE database_role=session_user;
 PERFORM id FROM publication.gateway_clusters WHERE id=cid AND active FOR KEY SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'gateway_unauthorized'; END IF;
 IF for_ack THEN
  SELECT * INTO p FROM publication.gateway_principals WHERE database_role=session_user AND cluster_id=cid AND active FOR NO KEY UPDATE;
 ELSE
  SELECT * INTO p FROM publication.gateway_principals WHERE database_role=session_user AND cluster_id=cid AND active FOR KEY SHARE;
 END IF;
 IF NOT FOUND THEN RAISE EXCEPTION 'gateway_unauthorized'; END IF;
 RETURN p;
END $$;

CREATE FUNCTION publication.v1_purge_claim(cluster uuid, node uuid, n integer) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE result jsonb;
BEGIN
 WITH picked AS (SELECT id FROM publication.upstream_purges WHERE cluster_id=cluster AND state='pending'
  AND next_attempt<=clock_timestamp() ORDER BY next_attempt LIMIT n FOR UPDATE SKIP LOCKED),
 claimed AS (UPDATE publication.upstream_purges p SET claimed_by=node,attempts=p.attempts+1,
  next_attempt=clock_timestamp()+interval '10 minutes' FROM picked WHERE p.id=picked.id RETURNING p.*)
 SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'kind',kind,'resource_id',resource_id,
  'api_key_id',api_key_id,'deployment_id',deployment_id,'credential_id',credential_id,
  'endpoint',endpoint,'provider_id',provider_id)),'[]'::jsonb) INTO result FROM claimed;
 RETURN result;
END $$;

CREATE FUNCTION publication.v1_purge_finish(purge uuid, cluster uuid, node uuid, outcome text, error text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $_$
DECLARE p publication.upstream_purges; action text;
BEGIN
 SELECT * INTO p FROM publication.upstream_purges WHERE id=purge AND cluster_id=cluster
 AND claimed_by=node AND state='pending' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF outcome='done' THEN
  CASE p.kind
  WHEN 'response' THEN UPDATE publication.stored_responses SET deleted_at=coalesce(deleted_at,clock_timestamp()) WHERE id=p.resource_id;
  WHEN 'file' THEN UPDATE publication.tenant_files SET deleted_at=coalesce(deleted_at,clock_timestamp()) WHERE id=p.resource_id;
  WHEN 'conversation' THEN UPDATE publication.conversations SET deleted_at=coalesce(deleted_at,clock_timestamp()) WHERE id=p.resource_id;
  WHEN 'container' THEN UPDATE publication.execution_containers SET deleted_at=coalesce(deleted_at,clock_timestamp()) WHERE id=p.resource_id;
  WHEN 'batch' THEN UPDATE publication.message_batches SET deleted=true WHERE id=p.resource_id;
  -- The node records a deleted vector store through its storage observation, which closes metering.
  ELSE NULL;
  END CASE;
  UPDATE publication.upstream_purges SET state='done',finished_at=clock_timestamp(),last_error=NULL WHERE id=purge;
  action='upstream_resource.purged';
 ELSIF outcome='failed' AND error ~ '^[a-z0-9_]{1,64}$' THEN
  IF p.attempts>=30 THEN
   UPDATE publication.upstream_purges SET state='abandoned',finished_at=clock_timestamp(),last_error=error WHERE id=purge;
   action='upstream_resource.purge_abandoned';
  ELSE
   UPDATE publication.upstream_purges SET last_error=error,
    next_attempt=clock_timestamp()+least(interval '1 minute'*power(2,least(p.attempts,11)),interval '1 day')
   WHERE id=purge;
   action='upstream_resource.purge_failed';
  END IF;
 ELSE RAISE EXCEPTION 'invalid_purge_outcome';
 END IF;
 INSERT INTO publication.audit_events(organization_id,action,resource_id,details)
 VALUES(p.organization_id,action,coalesce(p.resource_id,p.id),jsonb_build_object('purge_id',p.id,'kind',p.kind,
  'reason',p.reason,'cluster_id',cluster,'node_id',node,'deployment_id',p.deployment_id,
  'attempts',p.attempts,'error',error));
END $_$;

CREATE FUNCTION publication.v1_purge_orphan(cluster uuid, kid uuid, kind text, d jsonb, ep text, pid text) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 INSERT INTO publication.upstream_purges(cluster_id,kind,api_key_id,deployment_id,credential_id,
  credential_version,endpoint,provider_id,reason)
 VALUES(cluster,kind,kid,(d->>'id')::uuid,(d->>'credential_id')::uuid,(d->>'credential_version')::int,ep,pid,'orphan')
 ON CONFLICT(cluster_id,kind,deployment_id,provider_id) DO NOTHING
$$;

CREATE FUNCTION publication.v1_purge_owners(cluster uuid) RETURNS uuid[]
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT coalesce(array_agg(DISTINCT k),'{}') FROM (
  SELECT api_key_id k FROM publication.stored_responses WHERE cluster_id=cluster AND deleted_at IS NULL
  UNION ALL SELECT api_key_id FROM publication.tenant_files WHERE cluster_id=cluster AND deleted_at IS NULL
  UNION ALL SELECT api_key_id FROM publication.conversations WHERE cluster_id=cluster AND deleted_at IS NULL
  UNION ALL SELECT api_key_id FROM publication.vector_stores WHERE cluster_id=cluster AND state IN ('active','expired')
  UNION ALL SELECT api_key_id FROM publication.execution_containers WHERE cluster_id=cluster AND deleted_at IS NULL
  UNION ALL SELECT api_key_id FROM publication.message_batches WHERE cluster_id=cluster AND NOT deleted
 ) owners
$$;

CREATE FUNCTION publication.v1_purge_schedule(cluster uuid, owners uuid[], activity jsonb, grace interval) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 DELETE FROM publication.inactive_keys i USING jsonb_array_elements(activity) a
 WHERE i.api_key_id=(a->>'api_key_id')::uuid AND a->>'reason' IS NULL;
 INSERT INTO publication.inactive_keys(api_key_id,organization_id,reason,inactive_since)
 SELECT (a->>'api_key_id')::uuid,(a->>'organization_id')::uuid,a->>'reason',
  least(coalesce((a->>'since')::timestamptz,clock_timestamp()),clock_timestamp())
 FROM jsonb_array_elements(activity) a WHERE a->>'reason' IS NOT NULL
 ON CONFLICT(api_key_id) DO UPDATE SET reason=excluded.reason,observed_at=clock_timestamp();
 INSERT INTO publication.upstream_purges(cluster_id,kind,resource_id,api_key_id,organization_id,
  deployment_id,credential_id,credential_version,endpoint,provider_id,reason)
 SELECT cluster,r.kind,r.id,r.api_key_id,i.organization_id,r.deployment_id,r.credential_id,
  r.credential_version,r.endpoint,r.provider_id,'key_inactive'
 FROM publication.upstream_resources r JOIN publication.inactive_keys i USING(api_key_id)
 WHERE r.cluster_id=cluster AND r.retained AND r.api_key_id=ANY(owners)
  AND i.inactive_since<=clock_timestamp()-grace
 ON CONFLICT(cluster_id,kind,deployment_id,provider_id) DO NOTHING;
END $$;

CREATE FUNCTION publication.v1_resource_usage(kind text, kid uuid, keys uuid[], key_cap bigint, org_cap bigint) RETURNS jsonb
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE c uuid := (publication.v1_principal()).cluster_id; mine bigint; org bigint; owners uuid[] := array_append(keys,kid);
BEGIN
 IF key_cap IS NULL OR org_cap IS NULL OR key_cap<1 OR org_cap<1 THEN RAISE EXCEPTION 'invalid_resource_limit'; END IF;
 CASE kind
 WHEN 'file' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.tenant_files
   WHERE cluster_id=c AND api_key_id=kid AND deleted_at IS NULL LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.tenant_files
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND deleted_at IS NULL LIMIT org_cap) o;
 WHEN 'conversation' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.conversations
   WHERE cluster_id=c AND api_key_id=kid AND deleted_at IS NULL LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.conversations
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND deleted_at IS NULL LIMIT org_cap) o;
 WHEN 'vector_store' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.vector_stores
   WHERE cluster_id=c AND api_key_id=kid AND state IN ('submitting','active','unknown') LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.vector_stores
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND state IN ('submitting','active','unknown') LIMIT org_cap) o;
 WHEN 'container' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.execution_containers
   WHERE cluster_id=c AND api_key_id=kid AND deleted_at IS NULL
   AND metadata->>'status' IS DISTINCT FROM 'expired' LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.execution_containers
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND deleted_at IS NULL
   AND metadata->>'status' IS DISTINCT FROM 'expired' LIMIT org_cap) o;
 WHEN 'batch' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.message_batches
   WHERE cluster_id=c AND api_key_id=kid AND NOT deleted LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.message_batches
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND NOT deleted LIMIT org_cap) o;
 ELSE RAISE EXCEPTION 'invalid_resource_kind';
 END CASE;
 RETURN jsonb_build_object('key',mine,'organization',org);
END $$;

CREATE FUNCTION publication.v1_revision(p publication.gateway_principals, rev bigint, for_admission boolean) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE c publication.gateway_clusters; s record;
BEGIN
 SELECT * INTO c FROM publication.gateway_clusters WHERE id=p.cluster_id;
 SELECT revision,sha256,source_revision,key_sequence,required_capabilities INTO s
 FROM publication.config_snapshots WHERE cluster_id=p.cluster_id AND revision=coalesce(rev,c.desired_revision);
 IF NOT FOUND THEN RAISE EXCEPTION 'snapshot_unavailable'; END IF;
 IF c.scope_generation IS DISTINCT FROM (s.source_revision->>'scope_generation')::integer
  OR (c.key_version IS DISTINCT FROM (s.source_revision->>'key_version')::integer AND NOT EXISTS(
   SELECT FROM publication.previous_keys k WHERE k.cluster_id=c.id
   AND k.key_version=(s.source_revision->>'key_version')::integer AND k.valid_until>clock_timestamp()))
 THEN RAISE EXCEPTION 'snapshot_scope_expired'; END IF;
 IF for_admission AND (p.applied_revision IS DISTINCT FROM rev OR (rev<>c.desired_revision AND NOT EXISTS(
   SELECT FROM publication.config_snapshots newest WHERE newest.cluster_id=p.cluster_id
   AND newest.revision=c.desired_revision AND p.applied_at<=newest.created_at
   AND clock_timestamp()<newest.created_at+interval '5 seconds')))
 THEN RAISE EXCEPTION 'revision_not_current'; END IF;
 RETURN jsonb_build_object('node_id',p.node_id,'cluster_id',p.cluster_id,'revision',s.revision::text,
  'applied_revision',p.applied_revision::text,'sha256',s.sha256,'key_sequence',s.key_sequence::text,
  'required_capabilities',to_jsonb(s.required_capabilities));
END $$;

CREATE FUNCTION publication.v1_save_response(handle uuid, rid uuid, kid uuid, model uuid, d jsonb, pid text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 INSERT INTO publication.stored_responses(id,request_id,api_key_id,cluster_id,model_id,deployment_id,
 credential_id,credential_version,upstream_version,upstream_model,provider_id)
 VALUES(handle,rid,kid,(publication.v1_principal()).cluster_id,model,(d->>'id')::uuid,
 (d->>'credential_id')::uuid,(d->>'credential_version')::int,(d->>'upstream_version')::int,d->>'upstream_model',pid)
 ON CONFLICT(id) DO NOTHING;
 IF NOT EXISTS(SELECT FROM publication.stored_responses WHERE id=handle AND request_id=rid AND
 api_key_id=kid AND provider_id=pid AND deployment_id=(d->>'id')::uuid AND deleted_at IS NULL)
 THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;

CREATE FUNCTION publication.v1_snapshot(rev bigint, for_admission boolean) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals; r jsonb;
BEGIN
 p=publication.v1_principal(); r=publication.v1_revision(p,rev,for_admission);
 RETURN r || jsonb_build_object('manifest',(SELECT s.manifest FROM publication.config_snapshots s
  WHERE s.cluster_id=p.cluster_id AND s.revision=(r->>'revision')::bigint));
END $$;

CREATE FUNCTION publication.v1_stored_interpreter(handle_id uuid, kid_in uuid) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT o.origin FROM publication.stored_responses s JOIN publication.container_operations o ON o.origin=s.request_id AND o.kid=s.api_key_id
 WHERE s.id=handle_id AND s.api_key_id=kid_in AND s.cluster_id=(publication.v1_principal()).cluster_id AND s.deleted_at IS NULL AND NOT o.cleanup
$$;

CREATE FUNCTION publication.v1_stored_response(handle uuid, kid uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT jsonb_build_object('model',model_id,'deployment',deployment_id,'credential',credential_id,
  'credential_version',credential_version,'upstream_version',upstream_version,
  'upstream_model',upstream_model,'provider_id',provider_id)
 FROM publication.stored_responses WHERE id=handle AND api_key_id=kid AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;

CREATE FUNCTION publication.v1_vector(handle uuid, kid uuid) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT to_jsonb(v) FROM publication.vector_stores v WHERE id=handle AND cluster_id=(publication.v1_principal()).cluster_id
 AND (kid IS NULL AND (publication.v1_principal()).node_id IN (owner_node,poll_node) OR api_key_id=kid)
$$;

CREATE FUNCTION publication.v1_vector_batch(store uuid, id_in uuid, pid text, create_in boolean) RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE result text;
BEGIN
 IF create_in THEN INSERT INTO publication.vector_file_batches(id,store_id,provider_id)
   VALUES(id_in,store,pid) ON CONFLICT(id) DO NOTHING; END IF;
 SELECT provider_id INTO result FROM publication.vector_file_batches WHERE id=id_in AND store_id=store;
 IF result IS NULL OR (create_in AND result IS DISTINCT FROM pid) THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN result;
END $$;

CREATE FUNCTION publication.v1_vector_claim() RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE job publication.vector_stores;
BEGIN
 WITH candidate AS (SELECT id FROM publication.vector_stores WHERE cluster_id=(publication.v1_principal()).cluster_id
  AND state IN ('submitting','active') AND next_poll<=clock_timestamp() ORDER BY next_poll LIMIT 1 FOR UPDATE SKIP LOCKED)
 UPDATE publication.vector_stores v SET poll_node=(publication.v1_principal()).node_id,next_poll=clock_timestamp()+interval '1 minute' FROM candidate c WHERE v.id=c.id RETURNING v.* INTO job;
 IF job.id IS NULL THEN RETURN NULL; END IF; RETURN to_jsonb(job);
END $$;

CREATE FUNCTION publication.v1_vector_due(handle uuid, kid uuid) RETURNS void
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 UPDATE publication.vector_stores SET next_poll=least(next_poll,clock_timestamp()) WHERE id=handle AND api_key_id=kid
 AND cluster_id=(publication.v1_principal()).cluster_id AND state='active'
$$;

CREATE FUNCTION publication.v1_vector_id(kid uuid, dep uuid, pid text) RETURNS uuid
    LANGUAGE sql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
 SELECT id FROM publication.vector_stores WHERE api_key_id=kid AND deployment_id=dep AND provider_id=pid
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;

CREATE FUNCTION publication.v1_vector_locator(handle uuid, kid uuid, pid text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 UPDATE publication.vector_stores SET provider_id=pid,next_poll=clock_timestamp()
 WHERE id=handle AND api_key_id=kid AND owner_node=(publication.v1_principal()).node_id
 AND cluster_id=(publication.v1_principal()).cluster_id AND state='submitting'
 AND (provider_id IS NULL OR provider_id=pid);
 IF NOT FOUND AND NOT EXISTS(SELECT FROM publication.vector_stores WHERE id=handle AND api_key_id=kid
  AND owner_node=(publication.v1_principal()).node_id AND provider_id=pid) THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;

CREATE FUNCTION publication.v1_vector_mark_missed(handle uuid, stamp timestamp with time zone) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 UPDATE publication.vector_stores SET observed_at=stamp WHERE id=handle AND state IN ('submitting','active') AND provider_id IS NOT NULL
 AND cluster_id=(publication.v1_principal()).cluster_id AND poll_node=(publication.v1_principal()).node_id
 AND stamp>=created_at AND (observed_at IS NULL OR observed_at<stamp);
 RETURN FOUND;
END $$;

CREATE FUNCTION publication.v1_vector_prepare(handle uuid, kid uuid, d jsonb) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE p publication.gateway_principals := publication.v1_principal();
BEGIN
 INSERT INTO publication.vector_stores(id,cluster_id,api_key_id,owner_node,model_id,deployment_id,credential_id,credential_version,upstream_version)
 VALUES(handle,p.cluster_id,kid,p.node_id,(d->>'model_id')::uuid,(d->>'id')::uuid,(d->>'credential_id')::uuid,
   (d->>'credential_version')::integer,(d->>'upstream_version')::integer);
END $$;

CREATE FUNCTION publication.v1_vector_update(handle uuid, kid uuid, pid text, stamp timestamp with time zone, metadata_in jsonb, new_state text) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE v publication.vector_stores; recovering boolean;
BEGIN
 SELECT * INTO v FROM publication.vector_stores WHERE id=handle AND cluster_id=(publication.v1_principal()).cluster_id
 AND (kid IS NULL AND (publication.v1_principal()).node_id IN (owner_node,poll_node) OR api_key_id=kid) FOR UPDATE;
 IF NOT FOUND OR v.provider_id IS NOT NULL AND v.provider_id IS DISTINCT FROM pid THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 recovering=v.state='unknown' AND v.provider_id IS NULL AND pid IS NOT NULL AND kid=v.api_key_id
  AND v.owner_node=(publication.v1_principal()).node_id AND new_state IN ('active','expired');
 IF NOT coalesce(recovering,false) AND (v.state IN ('deleted','rejected','unknown') OR (v.observed_at>=stamp AND new_state<>'deleted')) THEN RETURN false; END IF;
 IF new_state IN ('active','expired') AND (pid IS NULL OR metadata_in->>'id' IS DISTINCT FROM 'vs_nx_'||handle::text
    OR metadata_in->>'object' IS DISTINCT FROM 'vector_store' OR jsonb_typeof(metadata_in->'created_at') IS DISTINCT FROM 'number'
    OR (v.provider_created_at IS NOT NULL AND v.provider_created_at IS DISTINCT FROM to_timestamp((metadata_in->>'created_at')::bigint))) THEN RAISE EXCEPTION 'invalid_vector_metadata'; END IF;
 IF new_state IN ('rejected','unknown') AND v.state<>'submitting' THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 UPDATE publication.vector_stores SET provider_id=pid,state=new_state,observed_at=greatest(v.observed_at,stamp),
    metadata=coalesce(metadata_in,v.metadata),provider_created_at=coalesce(v.provider_created_at,to_timestamp((metadata_in->>'created_at')::bigint)),next_poll=clock_timestamp()+interval '1 minute' WHERE id=handle;
 RETURN true;
END $$;

CREATE FUNCTION publication.v1_vectors(kid uuid, stamp uuid, asc_order boolean, n integer, allowed uuid[]) RETURNS SETOF jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE cluster uuid := (publication.v1_principal()).cluster_id; cursor_at timestamptz;
BEGIN
 IF n NOT BETWEEN 1 AND 101 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF stamp IS NOT NULL THEN
  SELECT provider_created_at INTO cursor_at FROM publication.vector_stores WHERE id=stamp AND api_key_id=kid AND cluster_id=cluster;
  IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 END IF;
 RETURN QUERY SELECT metadata FROM publication.vector_stores WHERE api_key_id=kid AND cluster_id=cluster AND state IN ('active','expired') AND model_id=ANY(allowed)
 AND (stamp IS NULL OR CASE WHEN asc_order THEN (provider_created_at,id)>(cursor_at,stamp) ELSE (provider_created_at,id)<(cursor_at,stamp) END)
 ORDER BY CASE WHEN asc_order THEN provider_created_at END ASC, CASE WHEN asc_order THEN id END ASC,provider_created_at DESC,id DESC LIMIT n;
END $$;

RESET ROLE;

-- Owned by nexus_migrator.
CREATE FUNCTION publication.announce_cluster() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog'
    AS $$ BEGIN
        PERFORM pg_notify('nexus_source_revision','publication'); RETURN NULL; END $$;

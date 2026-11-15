CREATE TABLE publication.message_batches(
    id uuid PRIMARY KEY, api_key_id uuid NOT NULL, cluster_id uuid NOT NULL, owner_node uuid NOT NULL,
    revision bigint NOT NULL, model_id uuid NOT NULL, deployment_id uuid NOT NULL,
    credential_id uuid NOT NULL, credential_version integer NOT NULL, upstream_version integer NOT NULL,
    provider_id text CHECK(provider_id ~ '^[A-Za-z0-9_-]{1,200}$'), headers jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), next_poll timestamptz NOT NULL,
    done boolean NOT NULL DEFAULT false, deleted boolean NOT NULL DEFAULT false, metadata jsonb
);
CREATE INDEX batch_poll ON publication.message_batches(owner_node,next_poll) WHERE NOT done;
CREATE INDEX batch_page ON publication.message_batches(cluster_id,api_key_id,created_at,id)
    WHERE NOT deleted AND provider_id IS NOT NULL;
CREATE TABLE publication.batch_items(
    batch_id uuid REFERENCES publication.message_batches NOT NULL, custom_id varchar(64) NOT NULL
        CHECK(custom_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    rid uuid UNIQUE NOT NULL, aid uuid UNIQUE NOT NULL, model_id uuid NOT NULL, deployment_id uuid NOT NULL,
    PRIMARY KEY(batch_id,custom_id)
);
CREATE FUNCTION publication.v1_batch_prepare(handle uuid,kid uuid,rev bigint,d jsonb,items jsonb,h jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION publication.v1_batch_update(handle uuid,pid text,meta jsonb,removing boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 UPDATE publication.message_batches SET provider_id=pid, metadata=CASE WHEN removing THEN metadata
    WHEN metadata->>'processing_status'='ended' THEN metadata ELSE meta END,
    deleted=deleted OR removing, next_poll=CASE WHEN provider_id IS NULL THEN clock_timestamp() ELSE next_poll END
 WHERE id=handle AND cluster_id=(publication.v1_principal()).cluster_id AND
   (provider_id=pid OR (provider_id IS NULL AND owner_node=(publication.v1_principal()).node_id AND NOT done))
   AND (NOT removing OR done);
 IF NOT FOUND THEN RAISE EXCEPTION 'batch_unavailable'; END IF;
END $$;
CREATE FUNCTION publication.v1_batch_lookup(handle uuid,kid uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT to_jsonb(b)||jsonb_build_object('targets',(SELECT jsonb_agg(DISTINCT jsonb_build_object('model', model_id,'deployment',deployment_id))
 FROM publication.batch_items WHERE batch_id=b.id)) FROM publication.message_batches b WHERE id=handle AND api_key_id=kid
 AND cluster_id=(publication.v1_principal()).cluster_id AND provider_id IS NOT NULL AND NOT deleted
$$;
CREATE FUNCTION publication.v1_batch_page(kid uuid,stamp uuid,newer boolean,n integer) RETURNS SETOF jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION publication.v1_batch_claim() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE job publication.message_batches;
BEGIN
 WITH candidate AS (SELECT id FROM publication.message_batches WHERE owner_node=(publication.v1_principal()).node_id
    AND NOT done AND next_poll<=clock_timestamp() ORDER BY next_poll LIMIT 1 FOR UPDATE SKIP LOCKED)
 UPDATE publication.message_batches b SET next_poll=clock_timestamp()+interval '15 seconds'
 FROM candidate c WHERE b.id=c.id RETURNING b.* INTO job;
 IF job.id IS NULL THEN RETURN NULL; END IF;
 RETURN to_jsonb(job);
END $$;
CREATE FUNCTION publication.v1_batch_lock(handle uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$
 SELECT to_jsonb(b) FROM publication.message_batches b WHERE id=handle AND owner_node=(publication.v1_principal()).node_id FOR UPDATE
$$;
CREATE FUNCTION publication.v1_batch_items(handle uuid) RETURNS TABLE(custom_id text,rid uuid,aid uuid)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT i.custom_id::text,i.rid,i.aid FROM publication.batch_items i JOIN publication.message_batches b ON b.id=i.batch_id
 WHERE b.id=handle AND b.owner_node=(publication.v1_principal()).node_id
$$;
CREATE FUNCTION publication.v1_batch_done(handle uuid) RETURNS void LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$
 UPDATE publication.message_batches SET done=true WHERE id=handle AND owner_node=(publication.v1_principal()).node_id
$$;

-- Schedule the purge of upstream copies owned by keys unusable for longer than the grace
-- period, then lease due purges to this node.
CREATE FUNCTION gateway_v1.claim_purges(grace_seconds integer,n integer) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals := publication.v1_principal(); owners uuid[];
BEGIN
 IF grace_seconds IS NULL OR grace_seconds<0 OR n IS NULL OR n NOT BETWEEN 1 AND 100 THEN
  RAISE EXCEPTION 'invalid_purge_claim';
 END IF;
 owners=publication.v1_purge_owners(p.cluster_id);
 PERFORM publication.v1_purge_schedule(p.cluster_id,owners,identity.v1_key_activity(owners),
  make_interval(secs=>grace_seconds));
 RETURN publication.v1_purge_claim(p.cluster_id,p.node_id,n);
END $$;
CREATE FUNCTION gateway_v1.finish_purge(purge uuid,outcome text,error text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals := publication.v1_principal();
BEGIN
 PERFORM publication.v1_purge_finish(purge,p.cluster_id,p.node_id,outcome,error);
END $$;
-- An upstream resource created for this key whose local binding could not be saved.
CREATE FUNCTION gateway_v1.record_orphan(kid uuid,rev bigint,kind text,dep uuid,ep text,pid text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals := publication.v1_principal(); d jsonb;
BEGIN
 SELECT value INTO d FROM jsonb_array_elements(publication.v1_snapshot(rev,true)#>'{manifest,resources,deployments}')
 WHERE value->>'id'=dep::text;
 IF d IS NULL THEN RAISE EXCEPTION 'deployment_unavailable'; END IF;
 PERFORM publication.v1_purge_orphan(p.cluster_id,kid,kind,d,ep,pid);
END $$;

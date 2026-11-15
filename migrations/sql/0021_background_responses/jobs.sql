CREATE TABLE publication.response_jobs(
 handle uuid PRIMARY KEY, rid uuid UNIQUE NOT NULL, aid uuid NOT NULL, owner_node uuid NOT NULL, revision bigint NOT NULL,
 attempt_event uuid NOT NULL, close_event uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 next_poll timestamptz NOT NULL DEFAULT clock_timestamp(),done boolean NOT NULL DEFAULT false
);
CREATE INDEX response_jobs_pending ON publication.response_jobs(owner_node,next_poll) WHERE NOT done;
CREATE FUNCTION publication.v1_background_register(handle uuid,rid uuid,aid uuid,rev bigint,attempt_event uuid,close_event uuid)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 INSERT INTO publication.response_jobs(handle,rid,aid,owner_node,revision,attempt_event,close_event)
 VALUES(handle,rid,aid,(publication.v1_principal()).node_id,rev,attempt_event,close_event) ON CONFLICT(handle) DO NOTHING
$$;
CREATE FUNCTION publication.v1_background_claim() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
CREATE FUNCTION publication.v1_job(handle_id uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT to_jsonb(j)||jsonb_build_object('provider',s.provider_id) FROM publication.response_jobs j
 JOIN publication.stored_responses s ON s.id=j.handle WHERE handle=handle_id AND owner_node=(publication.v1_principal()).node_id
 FOR UPDATE OF j
$$;
CREATE FUNCTION publication.v1_job_done(handle_id uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 UPDATE publication.response_jobs SET done=true WHERE handle=handle_id AND owner_node=(publication.v1_principal()).node_id
$$;
CREATE FUNCTION publication.v1_job_pending(handle_id uuid) RETURNS boolean
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT EXISTS(SELECT FROM publication.response_jobs WHERE handle=handle_id AND NOT done)
$$;

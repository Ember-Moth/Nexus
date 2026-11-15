CREATE FUNCTION gateway_v1.background_register(handle uuid,rid uuid,aid uuid,pid text,e1 uuid,e2 uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE f jsonb;
BEGIN
 f=billing.v1_response_attempt(rid,aid,(publication.v1_principal()).node_id);
 IF f IS NULL OR jsonb_array_length(billing.v1_status(rid,(publication.v1_principal()).node_id)->'attempts')<>1
 THEN RAISE EXCEPTION 'invalid_attempt_set'; END IF;
 PERFORM gateway_v1.save_response(handle,rid,aid,pid);
 PERFORM publication.v1_background_register(handle,rid,aid,(f->>'revision')::bigint,e1,e2);
END $$;
CREATE FUNCTION gateway_v1.background_claim() RETURNS jsonb LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$SELECT publication.v1_background_claim()$$;
CREATE FUNCTION gateway_v1.background_finish(handle_id uuid,outcome text,delivery text,usage jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE j jsonb; base jsonb; result jsonb; node uuid;
BEGIN
 j=publication.v1_job(handle_id); IF j IS NULL THEN RAISE EXCEPTION 'not_found'; END IF;
 IF (j->>'done')::boolean THEN RETURN; END IF;
 node=(publication.v1_principal()).node_id;
 base=jsonb_build_object('request_id',j->'rid','schema_version',1,'config_revision',j->>'revision',
   'occurred_at',to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'));
 result=billing.v1_append((base||jsonb_build_object('event_id',j->'attempt_event','event_type','attempt.finished','attempt_id',j->'aid',
 'payload',jsonb_build_object('outcome',outcome,'upstream_request_id',j->>'provider','usage_format','openai.responses.v1',
 'elapsed_ms',least(4294967295,greatest(0,floor(extract(epoch FROM clock_timestamp()-(j->>'created_at')::timestamptz)*1000)))::bigint,'raw_usage',usage)))::text,node);
 IF result->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 result=billing.v1_append((base||jsonb_build_object('event_id',j->'close_event','event_type','request.closed','attempt_id',NULL,
 'payload',jsonb_build_object('delivery_outcome',delivery,'attempt_ids',jsonb_build_array(j->'aid'),
 'delivered_attempt_id',CASE WHEN delivery IN ('succeeded','client_cancelled') THEN j->'aid' ELSE NULL END)))::text,node);
 IF result->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 PERFORM publication.v1_job_done(handle_id);
END $$;
CREATE FUNCTION gateway_v1.response_can_delete(handle_id uuid,kid uuid,rev bigint) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$BEGIN
 PERFORM gateway_v1.resolve_response(handle_id,kid,rev);
 IF publication.v1_job_pending(handle_id) THEN RAISE EXCEPTION 'response_pending'; END IF;
END $$;

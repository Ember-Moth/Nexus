CREATE FUNCTION billing.v1_append(message text,node uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE e jsonb; body jsonb; raw jsonb; rid uuid; aid uuid; eid uuid; kind text;
 req billing.request_runs; old billing.usage_events; ids uuid[]; started uuid[]; delivered uuid;
BEGIN
 IF message IS NULL OR octet_length(message)>65536 THEN RAISE EXCEPTION 'invalid_event_size'; END IF;
 IF NOT (message IS JSON OBJECT WITH UNIQUE KEYS) THEN RAISE EXCEPTION 'invalid_usage_envelope'; END IF;
 e=message::jsonb;
 IF jsonb_typeof(e)<>'object' OR e-'schema_version'-'event_id'-'event_type'-'request_id'-'attempt_id'
 -'occurred_at'-'config_revision'-'payload'<>'{}' OR e->'schema_version' IS DISTINCT FROM '1'::jsonb
 OR NOT (e ?& ARRAY['event_id','event_type','request_id','occurred_at','config_revision','payload'])
 OR jsonb_typeof(e->'config_revision') IS DISTINCT FROM 'string'
 OR jsonb_typeof(e->'occurred_at') IS DISTINCT FROM 'string' OR
 (e->>'occurred_at') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T.{8,24}(Z|[+-][0-9]{2}:[0-9]{2})$'
 THEN RAISE EXCEPTION 'invalid_usage_envelope'; END IF;
 PERFORM (e->>'occurred_at')::timestamptz;
 rid=(e->>'request_id')::uuid; eid=(e->>'event_id')::uuid; aid=(e->>'attempt_id')::uuid;
 kind=e->>'event_type'; body=e->'payload';
 IF eid IS NULL OR kind IS NULL OR kind NOT IN ('attempt.finished','request.closed') OR jsonb_typeof(body)<>'object'
 THEN RAISE EXCEPTION 'invalid_usage_event'; END IF;
 SELECT * INTO req FROM billing.request_runs WHERE id=rid AND node_id=node FOR UPDATE;
 IF NOT FOUND OR req.config_revision::text IS DISTINCT FROM e->>'config_revision' THEN RAISE EXCEPTION 'not_found'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(eid::text,87411));
 SELECT * INTO old FROM billing.usage_events WHERE id=eid;
 IF FOUND AND old.envelope=e THEN RETURN jsonb_build_object('status','accepted','event_id',eid); END IF;
 IF old.id IS NOT NULL OR EXISTS(SELECT FROM billing.usage_events u WHERE u.request_id=rid AND u.kind=e->>'event_type'
 AND (u.attempt_id IS NOT DISTINCT FROM aid)) THEN
  INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),
  req.organization_id,'usage.conflict',rid,jsonb_build_object('event_id',eid,'node_id',node));
  RETURN jsonb_build_object('status','conflict','event_id',eid);
 END IF;
 IF req.state='canceled' THEN RAISE EXCEPTION 'request_canceled'; END IF;
 IF kind='attempt.finished' THEN
  IF aid IS NULL OR NOT EXISTS(SELECT FROM billing.request_attempts WHERE id=aid AND request_id=rid)
  OR body-'outcome'-'upstream_request_id'-'usage_format'-'raw_usage'-'elapsed_ms'<>'{}'
  OR NOT(body ?& ARRAY['outcome','upstream_request_id','usage_format','raw_usage','elapsed_ms'])
  OR body->>'outcome' IS NULL OR body->>'outcome' NOT IN ('succeeded','failed','cancelled','unknown')
  OR jsonb_typeof(body->'upstream_request_id') NOT IN ('null','string')
  OR coalesce(body->>'upstream_request_id','') !~ '^[[:alnum:]_.:/-]{0,200}$'
  OR jsonb_typeof(body->'usage_format')<>'string' OR body->>'usage_format' !~ '^[a-zA-Z0-9_.-]{1,80}$'
  OR jsonb_typeof(body->'elapsed_ms')<>'number' OR body->>'elapsed_ms' !~ '^[0-9]{1,10}$'
  THEN RAISE EXCEPTION 'invalid_attempt_fact'; END IF;
  raw=body->'raw_usage';
  IF jsonb_typeof(raw) NOT IN ('object','null') THEN RAISE EXCEPTION 'invalid_usage_shape'; END IF;
  IF jsonb_typeof(raw)='object' THEN
   IF (SELECT count(*) FROM jsonb_each(raw))>16 OR EXISTS(SELECT FROM jsonb_each(raw)
   WHERE key !~ '^[a-z][a-z0-9_]{0,48}_tokens$' OR jsonb_typeof(value) NOT IN ('number','null') OR length(value::text)>24)
   THEN RAISE EXCEPTION 'invalid_usage_shape'; END IF;
  END IF;
 ELSE
  IF aid IS NOT NULL OR body-'delivery_outcome'-'attempt_ids'-'delivered_attempt_id'<>'{}'
  OR NOT(body ?& ARRAY['delivery_outcome','attempt_ids','delivered_attempt_id'])
  OR body->>'delivery_outcome' IS NULL OR body->>'delivery_outcome' NOT IN ('succeeded','client_cancelled','upstream_failed','unknown')
  OR jsonb_typeof(body->'attempt_ids')<>'array' THEN RAISE EXCEPTION 'invalid_close_fact'; END IF;
  SELECT coalesce(array_agg(v::uuid ORDER BY v::uuid),'{}') INTO ids FROM jsonb_array_elements_text(body->'attempt_ids') v;
  SELECT coalesce(array_agg(id ORDER BY id),'{}') INTO started FROM billing.request_attempts WHERE request_id=rid;
  delivered=(body->>'delivered_attempt_id')::uuid;
  IF ids IS DISTINCT FROM started OR (delivered IS NOT NULL AND NOT delivered=ANY(started))
  OR (body->>'delivery_outcome'='succeeded' AND delivered IS NULL)

  THEN RAISE EXCEPTION 'invalid_attempt_set'; END IF;
 END IF;
 INSERT INTO billing.usage_events(id,request_id,attempt_id,kind,envelope) VALUES(eid,rid,aid,kind,e);
 INSERT INTO billing.usage_jobs(request_id,state) VALUES(rid,'ready') ON CONFLICT(request_id)
 DO UPDATE SET state='ready',next_attempt_at=clock_timestamp(),failures=0,generation=billing.usage_jobs.generation+1,reason=NULL;
 RETURN jsonb_build_object('status','accepted','event_id',eid);
END $$;

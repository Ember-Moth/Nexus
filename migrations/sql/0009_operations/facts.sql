CREATE VIEW billing.report_source AS
 SELECT r.id,r.organization_id,r.project_id,r.api_key_id,r.model_id,
 coalesce((closed.envelope->>'occurred_at')::timestamptz,r.created_at) occurred_at,r.settled_at posted_at,
 c.generation,c.changed_at source_at,
 (to_jsonb(r)-'request_digest'-'settlement_digest'-'node_id'-'cluster_id'-'config_revision') ||
 jsonb_build_object('currency',w.currency,'reserved_amount',r.reserved_amount::text,
 'charged_amount',r.charged_amount::text,'refunded_amount',r.refunded_amount::text,
 'waived_amount',coalesce(dec.result->>'waived_amount','0.000000000000'),
 'gross_amount',coalesce(dec.result->>'gross_amount',r.charged_amount::text),
 'occurred_at',coalesce((closed.envelope->>'occurred_at')::timestamptz,r.created_at),
 'posted_at',r.settled_at,'costs',coalesce(costs.items,'[]'::jsonb),
 'refunds',coalesce(refunds.items,'[]'::jsonb)) payload
 FROM billing.request_runs r JOIN billing.accounts w ON w.id=r.account_id
 JOIN billing.report_changes c ON c.request_id=r.id
 LEFT JOIN billing.case_decisions dec ON dec.id=r.review_decision_id
 LEFT JOIN billing.usage_events closed ON closed.request_id=r.id AND closed.kind='request.closed'
 LEFT JOIN LATERAL (
  SELECT jsonb_agg(jsonb_build_object('attempt_id',a.id,'deployment_id',a.deployment_id,
   'cost_price_version_id',a.cost_price_version_id,'cost_amount',coalesce(fix.amount,a.cost_amount)::text,
   'original_cost',a.cost_amount::text,'cost_lines',a.cost_details,'correction_id',fix.id,
   'corrected_quantities',fix.quantities,'bookings',coalesce(bookings.items,'[]'::jsonb),'occurred_at',coalesce((e.envelope->>'occurred_at')::timestamptz,a.created_at),
   'posted_at',coalesce(fix.created_at,au.created_at)) ORDER BY a.created_at,a.id) items
  FROM billing.request_attempts a
  LEFT JOIN billing.usage_events e ON e.attempt_id=a.id AND e.kind='attempt.finished'
  LEFT JOIN LATERAL (SELECT id,amount,quantities,created_at FROM billing.cost_corrections
    WHERE attempt_id=a.id ORDER BY created_at DESC,id DESC LIMIT 1) fix ON true
  LEFT JOIN LATERAL (SELECT created_at FROM billing.audit_events WHERE resource_id=a.id
    AND action='attempt.cost_confirmed' ORDER BY created_at,id LIMIT 1) au ON true
  LEFT JOIN LATERAL (SELECT jsonb_agg(jsonb_build_object('id',b.id,'kind',b.kind,'amount',b.amount::text,
    'occurred_at',b.occurred_at,'posted_at',b.posted_at) ORDER BY b.posted_at,b.id) items FROM (
      SELECT a.id,'cost' kind,a.cost_amount amount,coalesce((e.envelope->>'occurred_at')::timestamptz,a.created_at) occurred_at,
       au.created_at posted_at WHERE a.cost_amount IS NOT NULL
      UNION ALL SELECT x.id,'cost_correction',x.amount-coalesce(lag(x.amount) OVER(ORDER BY x.created_at,x.id),a.cost_amount,0),
       x.created_at,x.created_at FROM billing.cost_corrections x WHERE x.attempt_id=a.id
    ) b) bookings ON true
  WHERE a.request_id=r.id
 ) costs ON true
 LEFT JOIN LATERAL (
  SELECT jsonb_agg(jsonb_build_object('id',a.id,'ledger_entry_id',l.id,'amount',a.amount::text,
   'occurred_at',a.created_at,'posted_at',a.posted_at) ORDER BY a.posted_at,a.id) items
  FROM billing.ledger_entries debit JOIN billing.adjustments a ON a.source_entry_id=debit.id
  JOIN billing.ledger_entries l ON l.source_type='adjustment' AND l.source_id=a.id
  WHERE debit.source_type='request' AND debit.source_id=r.id AND a.status='posted'
 ) refunds ON true;
GRANT SELECT ON billing.report_source TO nexus_billing;
CREATE INDEX attempts_cost_audit ON billing.audit_events(resource_id,created_at,id)
 WHERE action='attempt.cost_confirmed';
CREATE INDEX adjustments_source_posted ON billing.adjustments(source_entry_id) WHERE status='posted';

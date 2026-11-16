-- Views: the reporting source of billing (nexus_migrator's) and the upstream resources of
-- publication, which 16_ownership.sql hands to nexus_publication.

CREATE VIEW billing.report_source AS
 SELECT r.id,
    r.organization_id,
    r.project_id,
    r.api_key_id,
    r.model_id,
    COALESCE(((closed.envelope ->> 'occurred_at'::text))::timestamp with time zone, r.created_at) AS occurred_at,
    r.settled_at AS posted_at,
    c.generation,
    c.changed_at AS source_at,
    ((((((to_jsonb(r.*) - 'request_digest'::text) - 'settlement_digest'::text) - 'node_id'::text) - 'cluster_id'::text) - 'config_revision'::text) || jsonb_build_object('currency', w.currency, 'reserved_amount', (r.reserved_amount)::text, 'charged_amount', (r.charged_amount)::text, 'refunded_amount', (r.refunded_amount)::text, 'waived_amount', COALESCE(("dec".result ->> 'waived_amount'::text), '0.000000000000'::text), 'gross_amount', COALESCE(("dec".result ->> 'gross_amount'::text), (r.charged_amount)::text), 'occurred_at', COALESCE(((closed.envelope ->> 'occurred_at'::text))::timestamp with time zone, r.created_at), 'posted_at', r.settled_at, 'costs', COALESCE(costs.items, '[]'::jsonb), 'refunds', COALESCE(refunds.items, '[]'::jsonb))) AS payload
   FROM ((((((billing.request_runs r
     JOIN billing.accounts w ON ((w.id = r.account_id)))
     JOIN billing.report_changes c ON ((c.request_id = r.id)))
     LEFT JOIN billing.case_decisions "dec" ON (("dec".id = r.review_decision_id)))
     LEFT JOIN billing.usage_events closed ON (((closed.request_id = r.id) AND (closed.kind = 'request.closed'::text))))
     LEFT JOIN LATERAL ( SELECT jsonb_agg(jsonb_build_object('attempt_id', a.id, 'deployment_id', a.deployment_id, 'cost_price_version_id', a.cost_price_version_id, 'cost_amount', (COALESCE(fix.amount, a.cost_amount))::text, 'original_cost', (a.cost_amount)::text, 'cost_lines', a.cost_details, 'correction_id', fix.id, 'corrected_quantities', fix.quantities, 'bookings', COALESCE(bookings.items, '[]'::jsonb), 'occurred_at', COALESCE(((e.envelope ->> 'occurred_at'::text))::timestamp with time zone, a.created_at), 'posted_at', COALESCE(fix.created_at, au.created_at)) ORDER BY a.created_at, a.id) AS items
           FROM ((((billing.request_attempts a
             LEFT JOIN billing.usage_events e ON (((e.attempt_id = a.id) AND (e.kind = 'attempt.finished'::text))))
             LEFT JOIN LATERAL ( SELECT cost_corrections.id,
                    cost_corrections.amount,
                    cost_corrections.quantities,
                    cost_corrections.created_at
                   FROM billing.cost_corrections
                  WHERE (cost_corrections.attempt_id = a.id)
                  ORDER BY cost_corrections.created_at DESC, cost_corrections.id DESC
                 LIMIT 1) fix ON (true))
             LEFT JOIN LATERAL ( SELECT audit_events.created_at
                   FROM billing.audit_events
                  WHERE ((audit_events.resource_id = a.id) AND ((audit_events.action)::text = 'attempt.cost_confirmed'::text))
                  ORDER BY audit_events.created_at, audit_events.id
                 LIMIT 1) au ON (true))
             LEFT JOIN LATERAL ( SELECT jsonb_agg(jsonb_build_object('id', b.id, 'kind', b.kind, 'amount', (b.amount)::text, 'occurred_at', b.occurred_at, 'posted_at', b.posted_at) ORDER BY b.posted_at, b.id) AS items
                   FROM ( SELECT a.id,
                            'cost'::text AS kind,
                            a.cost_amount AS amount,
                            COALESCE(((e.envelope ->> 'occurred_at'::text))::timestamp with time zone, a.created_at) AS occurred_at,
                            au.created_at AS posted_at
                          WHERE (a.cost_amount IS NOT NULL)
                        UNION ALL
                         SELECT x.id,
                            'cost_correction',
                            (x.amount - COALESCE(lag(x.amount) OVER (ORDER BY x.created_at, x.id), a.cost_amount, (0)::numeric)),
                            x.created_at,
                            x.created_at
                           FROM billing.cost_corrections x
                          WHERE (x.attempt_id = a.id)) b) bookings ON (true))
          WHERE (a.request_id = r.id)) costs ON (true))
     LEFT JOIN LATERAL ( SELECT jsonb_agg(jsonb_build_object('id', a.id, 'ledger_entry_id', l.id, 'amount', (a.amount)::text, 'occurred_at', a.created_at, 'posted_at', a.posted_at) ORDER BY a.posted_at, a.id) AS items
           FROM ((billing.ledger_entries debit
             JOIN billing.adjustments a ON ((a.source_entry_id = debit.id)))
             JOIN billing.ledger_entries l ON ((((l.source_type)::text = 'adjustment'::text) AND (l.source_id = a.id))))
          WHERE (((debit.source_type)::text = 'request'::text) AND (debit.source_id = r.id) AND ((a.status)::text = 'posted'::text))) refunds ON (true));

CREATE VIEW publication.upstream_resources AS
 SELECT 'response'::text AS kind,
    r.id,
    r.cluster_id,
    r.api_key_id,
    r.deployment_id,
    r.credential_id,
    r.credential_version,
    'openai_responses'::text AS endpoint,
    r.provider_id,
    r.created_at,
    (r.deleted_at IS NULL) AS retained
   FROM publication.stored_responses r
UNION ALL
 SELECT 'file'::text AS kind,
    f.id,
    f.cluster_id,
    f.api_key_id,
    f.deployment_id,
    f.credential_id,
    f.credential_version,
    f.endpoint,
    f.provider_id,
    f.created_at,
    (f.deleted_at IS NULL) AS retained
   FROM publication.tenant_files f
UNION ALL
 SELECT 'conversation'::text AS kind,
    c.id,
    c.cluster_id,
    c.api_key_id,
    c.deployment_id,
    c.credential_id,
    c.credential_version,
    'openai_responses'::text AS endpoint,
    c.provider_id,
    c.created_at,
    (c.deleted_at IS NULL) AS retained
   FROM publication.conversations c
UNION ALL
 SELECT 'vector_store'::text AS kind,
    v.id,
    v.cluster_id,
    v.api_key_id,
    v.deployment_id,
    v.credential_id,
    v.credential_version,
    'openai_responses'::text AS endpoint,
    v.provider_id,
    v.created_at,
    (v.state = ANY (ARRAY['active'::text, 'expired'::text])) AS retained
   FROM publication.vector_stores v
  WHERE (v.provider_id IS NOT NULL)
UNION ALL
 SELECT 'container'::text AS kind,
    x.id,
    x.cluster_id,
    x.api_key_id,
    x.deployment_id,
    x.credential_id,
    x.credential_version,
    x.endpoint,
    x.provider_id,
    x.created_at,
    (x.deleted_at IS NULL) AS retained
   FROM publication.execution_containers x
UNION ALL
 SELECT 'batch'::text AS kind,
    b.id,
    b.cluster_id,
    b.api_key_id,
    b.deployment_id,
    b.credential_id,
    b.credential_version,
    'anthropic_messages'::text AS endpoint,
    b.provider_id,
    b.created_at,
    (NOT b.deleted) AS retained
   FROM publication.message_batches b
  WHERE (b.provider_id IS NOT NULL);

-- The rows every installation starts with: the source revision counters, the settlement backlog
-- gauge, the neutral global discount and each typed policy at its defaults. They are inserted
-- before the triggers exist, so none of them counts as a source change.

-- Identity reaches revision 3 with the built-in administrator (18_builtin_admin.sql).
INSERT INTO identity.source_revision (id, revision) VALUES (1, 2);
INSERT INTO catalog.source_revision (id, revision) VALUES (1, 1);
INSERT INTO billing.source_revision (id, revision) VALUES (1, 1);

INSERT INTO identity.auth_policy DEFAULT VALUES;
INSERT INTO identity.site_policy DEFAULT VALUES;
INSERT INTO catalog.resource_quota_defaults DEFAULT VALUES;
INSERT INTO billing.settings (id) VALUES (1);
INSERT INTO publication.cluster_policy_defaults DEFAULT VALUES;
INSERT INTO reporting.export_policy DEFAULT VALUES;

INSERT INTO billing.settlement_backlog DEFAULT VALUES;
-- Every request without an organization discount prices at factor 1.
INSERT INTO billing.discount_versions (id, factor, effective_at, published_at, state)
VALUES ('00000000-0000-0000-0000-000000000001', 1, 'epoch', now(), 'published');

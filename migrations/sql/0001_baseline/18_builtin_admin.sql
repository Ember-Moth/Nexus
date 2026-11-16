-- The built-in administrator (docs/identity-api.md#初始化向导): login admin, the unproven address
-- admin@example.com and the password "password", stored as its Argon2id (m=65536, t=3, p=4) hash.
-- The management API stays locked until it completes the setup wizard; `nexus setup reset`
-- restores this state. Inserting the user also creates its personal organization
-- (identity.on_user_created).

INSERT INTO identity.users (id, login, password_hash, status, unverified_email)
VALUES (
    '00000000-0000-4000-8000-000000000001',
    'admin',
    '$argon2id$v=19$m=65536,t=3,p=4$yzLccdji9Va1kKs1NYFVnQ$Azl8a7QXycjkcYPyGwz9gIVuLqFtwLHlTObS0HVofSA',
    'active',
    'admin@example.com'
);
INSERT INTO identity.platform_role_bindings (user_id, role)
VALUES ('00000000-0000-4000-8000-000000000001', 'platform_admin');

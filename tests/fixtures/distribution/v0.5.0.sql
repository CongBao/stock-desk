-- Credential-free synthetic data-directory marker for installer upgrade tests.
-- The v0.5.0 application schema is deliberately created by the bundled Alembic
-- chain so this fixture remains forward-migratable as new revisions are added.
PRAGMA user_version = 500;

CREATE TABLE distribution_fixture (
    release_version TEXT PRIMARY KEY NOT NULL,
    created_for TEXT NOT NULL
);

INSERT INTO distribution_fixture (release_version, created_for)
VALUES ('0.5.0', 'source-free-installer-upgrade-test');

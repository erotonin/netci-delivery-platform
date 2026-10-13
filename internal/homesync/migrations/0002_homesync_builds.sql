-- ADR-078: one owner per job, and every build number reserved before a build takes it.
CREATE TABLE home_job_owners (
    job        text        PRIMARY KEY,
    owner      text        NOT NULL,   -- the replica, from its credential
    epoch      bigint      NOT NULL,   -- grows on every change of hands: the fencing token
    expires_at timestamptz NOT NULL
);
CREATE SEQUENCE home_owner_epochs;

-- A number is in this table before any replica's build of the job has it; a reservation is made
-- only under the job's live lease, so a replica that lost the job cannot take a number.
CREATE TABLE home_build_numbers (
    job         text        NOT NULL,
    number      integer     NOT NULL,
    owner       text        NOT NULL,
    epoch       bigint      NOT NULL,
    reserved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job, number)
);

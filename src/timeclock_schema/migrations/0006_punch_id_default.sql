-- punches.id defaults to a random UUID, for punches the owner adds by hand (owner, 2026-11-04). A clock still sends the id
-- it made when the punch was recorded, so a retried or LAN-shared copy is the same row and ON CONFLICT (id) skips it.
-- Applied by hand to both databases first; setting it again changes nothing.
ALTER TABLE punches ALTER COLUMN id SET DEFAULT gen_random_uuid();

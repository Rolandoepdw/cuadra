-- =========================================================
-- 007: messaging zones as a catalog (mirrors categories)
--
-- `broadcast_groups.zone` (free text) becomes the `zones` catalog with a
-- many-to-many link (`group_zones`), exactly like `group_categories`.
-- Existing strings are migrated, then the column is dropped.
-- =========================================================

PRAGMA foreign_keys = OFF;
BEGIN;

CREATE TABLE zones (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL UNIQUE,
    active INTEGER DEFAULT 1
);

CREATE TABLE group_zones (
    group_id INTEGER NOT NULL REFERENCES broadcast_groups(id) ON DELETE CASCADE,
    zone_id  INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, zone_id)
);

INSERT OR IGNORE INTO zones (name, active)
SELECT DISTINCT trim(zone), 1
FROM broadcast_groups
WHERE zone IS NOT NULL AND trim(zone) <> '';

INSERT INTO group_zones (group_id, zone_id)
SELECT g.id, z.id
FROM broadcast_groups g
JOIN zones z ON z.name = trim(g.zone)
WHERE g.zone IS NOT NULL AND trim(g.zone) <> '';

ALTER TABLE broadcast_groups DROP COLUMN zone;

COMMIT;
PRAGMA foreign_keys = ON;

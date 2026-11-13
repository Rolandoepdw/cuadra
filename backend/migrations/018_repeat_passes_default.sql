-- Lower the seeded repeat_passes from 2 to 1 so a campaign is published at most
-- once per target group inside the cycle window. Existing databases keep any
-- value the operator set by hand: only the old seeded default is rewritten.
-- The application default lives in cuadra/domain/settings.py.

UPDATE settings
SET value = '1', updated_at = datetime('now')
WHERE key = 'broadcast.repeat_passes' AND value = '2';

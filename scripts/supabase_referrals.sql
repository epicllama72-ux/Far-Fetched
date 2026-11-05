-- Referrals: a friend who joins from your challenge link carries your short referral code on their scores,
-- so the game can count real friends who joined (and unlock the Carrier pigeon). Run once in the Supabase SQL editor.
alter table scores add column if not exists ref text;
alter table scores drop constraint if exists scores_ref_check;
alter table scores add constraint scores_ref_check check (ref is null or (char_length(ref) between 1 and 12 and ref ~ '^[a-z0-9]+$'));
create index if not exists scores_ref_idx on scores (ref) where ref is not null;
notify pgrst, 'reload schema';

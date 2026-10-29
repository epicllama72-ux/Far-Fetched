-- Lets a player's name change reach the scores they have already posted.
-- Run once in Supabase: SQL editor > New query > paste > Run.
-- Only the "name" column can be changed by the public (anon) key.
revoke update on public.scores from anon;
grant update (name) on public.scores to anon;
create policy "anyone can rename scores" on public.scores
  for update to anon using (true) with check (true);

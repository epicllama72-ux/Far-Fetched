-- How many players a fake fooled: two numbers per request (about 20 bytes), counted on the server from the saved answers.
-- answers[p_i] is [callOk, ...]; for a fake, callOk 0 means the player called it real (fooled). Read-only; safe to run again.
create or replace function story_fooled(p_day text, p_kind text, p_i integer, p_player text) returns jsonb
language sql stable security definer set search_path = public as $$
  with a as (
    select s.answers::jsonb -> p_i -> 0 as ok
      from scores s
     where s.day::text = p_day and s.kind::text = p_kind and s.answers is not null
       and jsonb_typeof(s.answers::jsonb) = 'array'
       and (p_player is null or s.player::text <> p_player)
  )
  select jsonb_build_object('n', count(*), 'f', count(*) filter (where ok::text = '0'))
    from a where jsonb_typeof(ok) = 'number';
$$;
grant execute on function story_fooled(text,text,integer,text) to anon, authenticated;

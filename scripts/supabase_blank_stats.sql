-- Fill the Blank: how many players found each headline's word. One request returns all five headlines (about 100 bytes), counted on the server
-- from the saved answers. For a blank score, answers[i] is [callOk, null, exact, seconds]; exact = 1 means the player found the hidden word
-- (or a word that means the same). Scores saved before this existed have no third value and are left out. Read-only; safe to run again.
create or replace function blank_stats(p_day text, p_player text) returns jsonb
language sql stable security definer set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object('n', n, 'e', e) order by i), '[]'::jsonb)
    from (
      select g.i,
             count(*) filter (where jsonb_typeof(s.answers::jsonb -> g.i -> 2) = 'number') as n,
             count(*) filter (where (s.answers::jsonb -> g.i ->> 2) = '1') as e
        from generate_series(0, 9) as g(i)
        left join scores s
          on s.day::text = p_day and s.kind::text = 'blank' and s.answers is not null
         and jsonb_typeof(s.answers::jsonb) = 'array'
         and (p_player is null or s.player::text <> p_player)
       group by g.i
    ) t;
$$;
grant execute on function blank_stats(text, text) to anon, authenticated;

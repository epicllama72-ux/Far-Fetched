-- Scores sent by an out-of-date copy of the game (a home-screen app that never reloaded) still use the
-- old scale: depth m of 0-20 and a perfect day of 4,780 points (5,000 for Fill the Blank).
-- This trigger converts any such row to the current scale as it arrives (1,000-point day, m = score x 10 metres),
-- and the update below fixes the rows already stored. Safe to run more than once.

create or replace function scores_fix_old_scale() returns trigger
language plpgsql as $$
begin
  -- a current client always sends m close to score x 10, so a tiny m with a large score can only be the old scale
  if new.m is not null and new.score is not null and new.m <= 20 and new.score > greatest(new.m * 20, 50) then
    new.score := round(new.score::numeric * case when new.kind::text = 'blank' then 0.2 else 1000.0/4780 end);
    new.m     := new.score * 10;
    if new.kind::text <> 'blank' then
      new.zone := case when new.m < 1250 then 'THE BASEMENT' when new.m < 3000 then 'THE DIRT'
                       when new.m < 5000 then 'BEDROCK' when new.m < 7000 then 'MAGMA'
                       when new.m < 9000 then 'THE CORE' else 'AUSTRALIA' end;
    end if;
  end if;
  return new;
end $$;

drop trigger if exists scores_fix_old_scale on scores;
create trigger scores_fix_old_scale before insert or update on scores
  for each row execute function scores_fix_old_scale();

-- the rows already stored: touching them runs the trigger
update scores set score = score where m <= 20 and score > greatest(m * 20, 50);

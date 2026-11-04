-- A new employee's names are capitalized as proper nouns when typed all lowercase, all uppercase, or with a lowercase first
-- letter (owner, 2026-11-04): "don" and "SMITH" become "Don" and "Smith", "o'brien" "O'Brien". Any other mixed case was
-- typed on purpose ("DeNiro", "McBee") and is kept. Inserts only: the owner's own edits correct names the rule gets wrong.
-- Applied by hand to both databases first, hence OR REPLACE; the existing names were fixed by hand then, not here, since
-- the backend reruns this file on those databases and would undo an edit made since.
CREATE OR REPLACE FUNCTION proper_name(name text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN name = lower(name) OR name = upper(name) OR left(name, 1) <> upper(left(name, 1)) THEN initcap(name) ELSE name END
$$;

CREATE OR REPLACE FUNCTION employees_proper_names() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.first_name := proper_name(NEW.first_name);
  NEW.middle_name := proper_name(NEW.middle_name);
  NEW.last_name := proper_name(NEW.last_name);
  RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER employees_proper_names BEFORE INSERT ON employees FOR EACH ROW EXECUTE FUNCTION employees_proper_names();

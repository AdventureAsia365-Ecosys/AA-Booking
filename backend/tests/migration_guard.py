"""Static guard for ``migrations/*.sql`` (root ADR 0004 decision 3, "migration guard").

Every Booking migration must:

- be named ``NNN_snake_name.sql`` (the DB Migrate input pattern);
- be wrapped in ``BEGIN; ... COMMIT;`` and register its own ``bk-NNN`` in
  ``shared.schema_versions`` with ``ON CONFLICT (version) DO NOTHING``;
- create, alter, drop, grant on and write to nothing outside schema ``booking`` — the only
  exceptions are the ``shared.schema_versions`` insert and reads of that one table;
- never create or alter a login, set a password, create an extension or a SECURITY DEFINER
  function, or grant to PUBLIC;
- state default privileges ``FOR ROLE aa_cis_admin IN SCHEMA booking`` (DB Migrate runs as the RDS
  master user, ADR 0004 correction d);
- revoke INSERT/UPDATE/DELETE from role ``booking`` on every view it creates.

The checks are regexes over the SQL with comments removed and string literals blanked, so they
are deliberately strict: loosen a rule here, in review, when a migration genuinely needs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

FILENAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
OWNER_SCHEMA = "booking"
MIGRATION_ROLE = "aa_cis_admin"
APP_ROLE = "booking"

_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QUALIFIED = rf"{_IDENT}(?:\s*\.\s*{_IDENT})?"


@dataclass(frozen=True)
class Violation:
    file: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.file}: [{self.rule}] {self.detail}"


def _strip(sql: str) -> tuple[str, str]:
    """Return (code_with_strings, code_with_blank_strings); comments removed in both.

    Handles ``-- line`` and ``/* block */`` comments, ``'...'`` literals (with ``''`` escapes) and
    ``$tag$ ... $tag$`` bodies (kept as code, since DO blocks and functions hold real DDL).
    """
    keep: list[str] = []
    blank: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            keep.append(" ")
            blank.append(" ")
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                j += 1
            literal = sql[i : j + 1]
            keep.append(literal)
            blank.append("''")
            i = j + 1
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                end = n if j == -1 else j + len(tag)
                body = sql[i + len(tag) : (n if j == -1 else j)]
                inner_keep, inner_blank = _strip(body)
                keep.append(f" {inner_keep} ")
                blank.append(f" {inner_blank} ")
                i = end
                continue
        keep.append(ch)
        blank.append(ch)
        i += 1
    return "".join(keep), "".join(blank)


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", name).replace('"', "").lower()


def _in_owner_schema(name: str) -> bool:
    return _norm(name).startswith(f"{OWNER_SCHEMA}.")


def _grant_objects_in_owner_schema(objects: str) -> bool:
    """The ON clause of a GRANT/REVOKE names only schema booking or objects inside it."""
    if re.fullmatch(rf"SCHEMA\s+{OWNER_SCHEMA}", objects, flags=re.IGNORECASE):
        return True
    if re.fullmatch(
        rf"ALL\s+(TABLES|SEQUENCES|FUNCTIONS|ROUTINES)\s+IN\s+SCHEMA\s+{OWNER_SCHEMA}", objects, flags=re.IGNORECASE
    ):
        return True
    # Bare object kinds only occur inside ALTER DEFAULT PRIVILEGES, which is checked for IN SCHEMA booking.
    if re.fullmatch(r"(TABLES|SEQUENCES|FUNCTIONS|ROUTINES|TYPES)", objects, flags=re.IGNORECASE):
        return True
    names = re.sub(r"^(TABLE|SEQUENCE|FUNCTION|ROUTINE|PROCEDURE|TYPE)\s+", "", objects, flags=re.IGNORECASE)
    names = re.sub(r"\([^)]*\)", "", names)  # drop function argument lists, which may contain commas
    return all(_in_owner_schema(name.strip()) for name in names.split(","))


def check_sql(name: str, sql: str) -> list[Violation]:
    out: list[Violation] = []

    def bad(rule: str, detail: str) -> None:
        out.append(Violation(name, rule, detail))

    m = FILENAME_RE.match(name)
    if not m:
        bad("filename", "must look like NNN_snake_name.sql")
        return out
    version = f"bk-{m.group(1)}"

    code, blank = _strip(sql)
    flat = re.sub(r"\s+", " ", blank).strip()
    flat_u = flat.upper()

    # Transaction wrapper.
    if not re.match(r"^BEGIN\s*;", flat_u):
        bad("transaction", "first statement must be BEGIN;")
    if not re.search(r"\bCOMMIT\s*;\s*$", flat_u):
        bad("transaction", "last statement must be COMMIT;")
    if re.search(r"\bROLLBACK\b", flat_u):
        bad("transaction", "ROLLBACK is not allowed")

    # Self-registration (needs the string literals).
    code_flat = re.sub(r"\s+", " ", code)
    reg = re.findall(
        r"INSERT INTO shared\.schema_versions\b[^;]*?'(bk-\d{3})'[^;]*?ON CONFLICT \(version\) DO NOTHING",
        code_flat,
        flags=re.IGNORECASE,
    )
    if reg != [version]:
        bad("register", f"must insert exactly '{version}' into shared.schema_versions ON CONFLICT (version) DO NOTHING")

    # shared.* — only schema_versions.
    for ref in re.findall(rf"\bshared\s*\.\s*({_IDENT})", flat, flags=re.IGNORECASE):
        if _norm(ref) != "schema_versions":
            bad("foreign-schema", f"shared.{ref} is outside schema booking")

    # Forbidden capabilities.
    for word, rule in (
        (r"\bPASSWORD\b", "no-password"),
        (r"\bLOGIN\b", "no-login"),
        (r"\b(?:SUPERUSER|CREATEROLE|CREATEDB|BYPASSRLS|REPLICATION)\b", "no-role-attributes"),
        (r"\bSECURITY\s+DEFINER\b", "no-security-definer"),
        (r"\bCREATE\s+EXTENSION\b", "no-extension"),
        (r"\bALTER\s+ROLE\b", "no-alter-role"),
        (r"\bTO\s+PUBLIC\b", "no-public-grant"),
        (r"\bSET\s+ROLE\b", "no-set-role"),
    ):
        if re.search(word, flat_u):
            bad(rule, f"matched {word}")

    # CREATE targets.
    create_re = re.compile(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+)?(?:TEMP(?:ORARY)?\s+)?(?:MATERIALIZED\s+)?"
        r"(TABLE|VIEW|FUNCTION|PROCEDURE|TYPE|DOMAIN|SEQUENCE|SCHEMA|ROLE|INDEX|TRIGGER|POLICY|RULE)\b"
        r"(?:\s+CONCURRENTLY)?(?:\s+IF\s+NOT\s+EXISTS)?\s+([^;]*)",
        re.IGNORECASE,
    )
    for cm in create_re.finditer(flat):
        kind, rest = cm.group(1).upper(), cm.group(2)
        target = re.match(_QUALIFIED, rest)
        target_s = target.group(0) if target else rest[:40]
        if kind == "SCHEMA":
            if _norm(target_s) != OWNER_SCHEMA:
                bad("create-schema", f"only schema {OWNER_SCHEMA} may be created, got {target_s}")
        elif kind == "ROLE":
            if not re.match(rf"^{APP_ROLE}\s+NOLOGIN\s*;?", rest, flags=re.IGNORECASE):
                bad("create-role", f"only 'CREATE ROLE {APP_ROLE} NOLOGIN' is allowed, got {rest[:40]!r}")
        elif kind in {"INDEX", "TRIGGER", "POLICY", "RULE"}:
            on = re.search(rf"\bON\s+(?:ONLY\s+)?({_QUALIFIED})", rest, flags=re.IGNORECASE)
            if not on or not _in_owner_schema(on.group(1)):
                bad("create-target", f"CREATE {kind} must be ON {OWNER_SCHEMA}.<table>: {rest[:60]!r}")
        elif not _in_owner_schema(target_s):
            bad("create-target", f"CREATE {kind} {target_s} is outside schema {OWNER_SCHEMA}")

    # ALTER targets.
    alter_re = re.compile(
        r"\bALTER\s+(DEFAULT\s+PRIVILEGES|TABLE|VIEW|MATERIALIZED\s+VIEW|FUNCTION|PROCEDURE|TYPE|DOMAIN|SEQUENCE|"
        r"SCHEMA|INDEX|TRIGGER|POLICY)\b(?:\s+IF\s+EXISTS)?(?:\s+ONLY)?\s+([^;]*)",
        re.IGNORECASE,
    )
    for am in alter_re.finditer(flat):
        kind, rest = re.sub(r"\s+", " ", am.group(1).upper()), am.group(2)
        if kind == "DEFAULT PRIVILEGES":
            if not re.match(
                rf"^FOR\s+ROLE\s+{MIGRATION_ROLE}\s+IN\s+SCHEMA\s+{OWNER_SCHEMA}\s+(GRANT|REVOKE)\b",
                rest,
                flags=re.IGNORECASE,
            ):
                bad(
                    "default-privileges",
                    f"must be 'ALTER DEFAULT PRIVILEGES FOR ROLE {MIGRATION_ROLE} IN SCHEMA {OWNER_SCHEMA} ...'",
                )
            continue
        target = re.match(_QUALIFIED, rest)
        target_s = target.group(0) if target else rest[:40]
        if kind == "SCHEMA":
            if _norm(target_s) != OWNER_SCHEMA:
                bad("alter-target", f"ALTER SCHEMA {target_s}")
        elif kind in {"TRIGGER", "POLICY"}:
            on = re.search(rf"\bON\s+({_QUALIFIED})", rest, flags=re.IGNORECASE)
            if not on or not _in_owner_schema(on.group(1)):
                bad("alter-target", f"ALTER {kind} outside schema {OWNER_SCHEMA}")
        elif not _in_owner_schema(target_s):
            bad("alter-target", f"ALTER {kind} {target_s} is outside schema {OWNER_SCHEMA}")

    # DROP targets.
    for dm in re.finditer(r"\bDROP\s+(\w+(?:\s+VIEW)?)\b(?:\s+IF\s+EXISTS)?\s+([^;]*)", flat, flags=re.IGNORECASE):
        kind, rest = dm.group(1).upper(), dm.group(2)
        if kind in {"DEFAULT", "NOT", "CONSTRAINT", "COLUMN"}:  # ALTER ... DROP DEFAULT / NOT NULL / COLUMN
            continue
        if kind in {"TRIGGER", "POLICY", "RULE"}:
            on = re.search(rf"\bON\s+({_QUALIFIED})", rest, flags=re.IGNORECASE)
            ok = bool(on) and _in_owner_schema(on.group(1))
        elif kind == "SCHEMA":
            ok = False
        else:
            target = re.match(_QUALIFIED, rest)
            ok = bool(target) and _in_owner_schema(target.group(0))
        if not ok:
            bad("drop-target", f"DROP {kind} {rest[:60]!r} is outside schema {OWNER_SCHEMA}")

    # DML targets.
    for dml in re.finditer(
        rf"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+(?:ONLY\s+)?({_QUALIFIED})",
        flat,
        flags=re.IGNORECASE,
    ):
        verb, target = re.sub(r"\s+", " ", dml.group(1).upper()), _norm(dml.group(2))
        if target in {"of", "on"}:
            continue  # privilege lists ("REVOKE UPDATE, TRUNCATE ON"), triggers ("BEFORE UPDATE ON", "UPDATE OF col")
        if verb == "INSERT INTO" and target == "shared.schema_versions":
            continue
        if not target.startswith(f"{OWNER_SCHEMA}."):
            bad("dml-target", f"{verb} {target} is outside schema {OWNER_SCHEMA}")

    # Views are read-only for the app. Default privileges grant INSERT/UPDATE/DELETE on every new
    # relation, and a simple view is auto-updatable (it would write through with its owner's rights).
    for vm in re.finditer(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+({_QUALIFIED})", flat, flags=re.IGNORECASE):
        view = re.escape(_norm(vm.group(1)))
        revoke = re.search(
            rf"\bREVOKE\s+INSERT\s*,\s*UPDATE\s*,\s*DELETE\b[^;]*\bON\s+(?:TABLE\s+)?{view}\s+FROM\s+{APP_ROLE}\b",
            flat,
            flags=re.IGNORECASE,
        )
        if not revoke:
            bad("view-read-only", f"REVOKE INSERT, UPDATE, DELETE ON {vm.group(1)} FROM {APP_ROLE} is missing")

    # GRANT / REVOKE: objects in booking only, to/from role booking (REVOKE may also name PUBLIC).
    for gm in re.finditer(r"\b(GRANT|REVOKE)\b([^;]*)", flat, flags=re.IGNORECASE):
        verb, rest = gm.group(1).upper(), gm.group(2)
        on = re.search(r"\bON\s+(.*?)\s+(?:TO|FROM)\s+(.*)$", rest, flags=re.IGNORECASE)
        if not on:
            bad("grant", f"{verb} without ON ... TO/FROM: {rest[:60]!r}")
            continue
        objects, grantees = on.group(1).strip(), on.group(2).strip()
        if not _grant_objects_in_owner_schema(objects):
            bad("grant", f"{verb} ON {objects} is outside schema {OWNER_SCHEMA}")
        allowed = {APP_ROLE} | ({"public"} if verb == "REVOKE" else set())
        grantees = re.sub(r"\s+(CASCADE|RESTRICT|WITH\s+GRANT\s+OPTION)\s*$", "", grantees, flags=re.IGNORECASE)
        for g in re.split(r"\s*,\s*", grantees):
            if _norm(g) not in allowed:
                bad("grant", f"{verb} ... {g} — only role {APP_ROLE} may be a grantee")

    return out


def check_file(path: Path) -> list[Violation]:
    return check_sql(path.name, path.read_text(encoding="utf-8"))

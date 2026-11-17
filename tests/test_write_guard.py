"""The write guard: what may reach the Gmail API, and from where.

Replaces `test_gmail_client_contains_no_write_call`, which scanned only
`app/gmail_client.py` for a denylist of write verbs. This covers a strict
superset: every `.py` file under `app/` and `scripts/`, and an ALLOWLIST of
API calls instead of a denylist of verbs.

Why an allowlist. Every Gmail API call goes through `svc.users()`, so that is
the shape the guard keys on. A denylist of verbs ("update", "create", ...)
cannot run repo-wide, because plain Python uses the same names -
`app/evalset.py` calls `.update()` on a set - and the AST carries no types to
tell them apart. An allowlist also fails closed: a write nobody thought to
list (`batchModify`, `import_`, `settings().filters().create`) is caught
because it is not on the list, and a new read shows up in a diff.

The three rules:

1. Only `app/gmail_client.py` imports `googleapiclient`.
2. Only `app/gmail_client.py` calls `.users()`.
3. The `.users()`-rooted calls in `app/gmail_client.py` are exactly
   `ALLOWED_API_CALLS`.
4. Only the files in `MODIFY_LABELS_CALLERS` refer to `modify_labels`, the
   one function that changes a message. Undo came first (A1); `app/agent.py`
   joined at step 7, after the undo rehearsal passed, and is the only `app/`
   module on the list.
5. Only the files in `CLASSIFY_AND_LABEL_CALLERS` refer to
   `classify_and_label`, the agent's entry point. Rule 4 stops at
   `agent.py`, so without this a second script that calls the agent with
   `dry_run=False` would write without appearing in any list (step 8 Q6).

Limit: this catches mistakes, not deliberate evasion - `getattr(svc, "users")`
gets past it. The granted OAuth scope is the backstop for that.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = ("app", "scripts")
GMAIL_CLIENT = "app/gmail_client.py"

# Exempt by exact path, not by a `spike_*.py` pattern, so a new spike script
# is caught rather than waved through. spike_gmail.py is Phase 0 throwaway
# code that builds its own client; it is kept as the record of how the Phase 0
# numbers were measured, and its header says it must stay read-only. Agreed at
# Phase 3 plan review, 2026-11-02.
EXEMPT = frozenset({"scripts/spike_gmail.py"})

# Every Gmail API call gmail_client may make, as the names after `.users()`.
ALLOWED_API_CALLS = frozenset({
    ("messages", "list"),     # search_refs
    ("messages", "get"),      # fetch_raw
    ("messages", "modify"),   # modify_labels (Phase 3 step 5)
    ("labels", "list"),       # all_labels
    ("labels", "create"),     # create_label
})

# The files allowed to refer to gmail_client.modify_labels (A1). Each addition
# is its own deliberate diff:
# - scripts/undo_run.py (step 6): undo is a write too, and A1 makes it the
#   first writer, so the first live writes can be reversed.
# - scripts/rehearse_undo.py (step 6, Part 3): writes the rehearsal's
#   fixture. Acts only on subjects tagged `[personai rehearsal] M<n>`, and
#   is kept after the pass so the rehearsal stays repeatable (decision Q9).
# - app/agent.py (step 7): the writer itself. Joined only after the undo
#   rehearsal passed on 2026-11-03, so the first real writes could already be
#   reversed (A1).
MODIFY_LABELS_CALLERS: frozenset[str] = frozenset(
    {"scripts/undo_run.py", "scripts/rehearse_undo.py", "app/agent.py"}
)

# The files allowed to refer to agent.classify_and_label, beside agent.py
# itself (rule 5). Each addition is its own deliberate diff:
# - scripts/run_pass.py (step 8): the Phase 3 CLI, dry run unless --live.
CLASSIFY_AND_LABEL_CALLERS: frozenset[str] = frozenset({"scripts/run_pass.py"})
AGENT = "app/agent.py"


# --- the analysis ---------------------------------------------------------


def imports_googleapiclient(tree: ast.AST) -> bool:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    return any(module.split(".")[0] == "googleapiclient" for module in modules)


def _users_calls(tree: ast.AST) -> list[ast.Call]:
    """Every `<anything>.users()` call."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "users"
    ]


def api_calls(tree: ast.AST) -> set[tuple[str, ...]]:
    """The names called after each `.users()`, e.g. ("messages", "get").

    Walks outward from each `.users()` call through the chain of
    `.name(...)` calls built on it. Stops at the first thing that is not
    another call in the chain - `.execute()` excepted, which runs a request
    rather than naming one. A chain that does not end in a method, such as
    binding `svc.users().messages()` to a variable, records a short tuple
    like ("messages",), which is not on the allowlist and so fails.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    found: set[tuple[str, ...]] = set()
    for users_call in _users_calls(tree):
        names: list[str] = []
        current: ast.AST = users_call
        while True:
            attribute = parents.get(current)
            call = parents.get(attribute) if attribute is not None else None
            if not (
                isinstance(attribute, ast.Attribute)
                and isinstance(call, ast.Call)
                and call.func is attribute
                and attribute.attr != "execute"
            ):
                break
            names.append(attribute.attr)
            current = call
        found.add(tuple(names))
    return found


def refers_to(tree: ast.AST, name: str) -> bool:
    """Any call, import or mention of `name`: as a bare name, an attribute,
    or an imported alias."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
        if isinstance(node, ast.alias) and node.name == name:
            return True
    return False


def refers_to_modify_labels(tree: ast.AST) -> bool:
    return refers_to(tree, "modify_labels")


def _scanned_files() -> list[tuple[str, ast.AST]]:
    files = []
    for directory in SCANNED_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative in EXEMPT:
                continue
            files.append((relative, ast.parse(path.read_text(), filename=relative)))
    return files


# --- the three rules, against the real tree --------------------------------


def test_only_gmail_client_imports_googleapiclient():
    offenders = [
        name for name, tree in _scanned_files()
        if name != GMAIL_CLIENT and imports_googleapiclient(tree)
    ]
    assert not offenders, f"googleapiclient imported outside gmail_client: {offenders}"


def test_no_gmail_api_call_outside_gmail_client():
    offenders = [
        name for name, tree in _scanned_files()
        if name != GMAIL_CLIENT and _users_calls(tree)
    ]
    assert not offenders, (
        f".users() called outside gmail_client: {offenders}. "
        "Add a function to app/gmail_client.py instead."
    )


def test_gmail_client_api_calls_are_exactly_the_allowlist():
    """Equality, not subset: a call removed from gmail_client should also
    leave the allowlist, so the list never claims more than the code does."""
    tree = ast.parse((ROOT / GMAIL_CLIENT).read_text())
    assert api_calls(tree) == ALLOWED_API_CALLS


def test_only_allowed_callers_refer_to_modify_labels():
    """A1: every writer is listed. Undo was first; the agent joined after
    the undo rehearsal passed."""
    offenders = [
        name for name, tree in _scanned_files()
        if name != GMAIL_CLIENT
        and name not in MODIFY_LABELS_CALLERS
        and refers_to_modify_labels(tree)
    ]
    assert not offenders, (
        f"modify_labels referenced outside MODIFY_LABELS_CALLERS: {offenders}"
    )


def test_every_exempt_path_still_exists():
    """A stale exemption would silently cover whatever is created next at
    that path."""
    missing = [path for path in EXEMPT if not (ROOT / path).exists()]
    assert not missing, f"remove these from EXEMPT: {missing}"


def test_the_real_tree_is_actually_scanned():
    """Guards the guard: a wrong ROOT would scan nothing and pass."""
    names = {name for name, _ in _scanned_files()}
    assert GMAIL_CLIENT in names
    assert "scripts/measure_bodies.py" in names
    assert not names & EXEMPT


# --- proof the rules can fail ------------------------------------------------


def calls_in(source: str) -> set[tuple[str, ...]]:
    return api_calls(ast.parse(source))


def test_a_trash_chain_is_seen():
    assert calls_in(
        "svc.users().messages().trash(userId='me', id=i).execute()"
    ) == {("messages", "trash")}


def test_a_nested_resource_chain_is_seen_in_full():
    assert calls_in(
        "svc.users().settings().filters().create(userId='me', body={})"
    ) == {("settings", "filters", "create")}


def test_a_chain_split_across_lines_is_seen():
    source = (
        "r = (\n"
        "    svc.users()\n"
        "    .labels()\n"
        "    .create(userId='me', body={'name': n})\n"
        ")\n"
    )
    assert calls_in(source) == {("labels", "create")}


def test_binding_a_resource_to_a_variable_is_not_on_the_allowlist():
    """The method call on the variable is invisible, so the partial chain is
    what gets recorded - and it can never match an allowlisted pair."""
    found = calls_in("m = svc.users().messages()\nm.trash(id=i)")
    assert found == {("messages",)}
    assert not found <= ALLOWED_API_CALLS


def test_a_resourceless_call_is_recorded():
    assert calls_in("svc.users().getProfile(userId='me')") == {("getProfile",)}


@pytest.mark.parametrize(
    "source",
    [
        "gmail_client.modify_labels(svc, i, add_ids=[], remove_ids=['INBOX'])",
        "from app.gmail_client import modify_labels",
        "write = modify_labels",
    ],
)
def test_every_form_of_reference_to_modify_labels_is_seen(source):
    assert refers_to_modify_labels(ast.parse(source))


def test_a_different_name_is_not_a_reference_to_modify_labels():
    assert not refers_to_modify_labels(ast.parse("gmail_client.label_ids(svc, n)"))


def test_a_googleapiclient_import_is_seen_in_both_forms():
    assert imports_googleapiclient(ast.parse("import googleapiclient.discovery"))
    assert imports_googleapiclient(ast.parse("from googleapiclient.errors import HttpError"))
    assert not imports_googleapiclient(ast.parse("from google.oauth2 import credentials"))


@pytest.mark.parametrize(
    "source",
    [
        "taken.update(picks)",
        "parts.insert(0, x)",
        "session.send(request)",
        "d.create()",
    ],
)
def test_ordinary_python_with_write_named_methods_is_not_an_api_call(source):
    """The reason this is not a denylist of verbs."""
    tree = ast.parse(source)
    assert not _users_calls(tree)
    assert api_calls(tree) == set()


def test_every_listed_caller_exists_and_calls_modify_labels():
    """A stale entry would silently license whatever is created next at that
    path - the same reason EXEMPT paths must exist."""
    for path in MODIFY_LABELS_CALLERS:
        source = ROOT / path
        assert source.exists(), f"remove {path} from MODIFY_LABELS_CALLERS"
        assert refers_to_modify_labels(ast.parse(source.read_text())), path


def test_agent_is_the_only_app_module_that_writes():
    """Replaces `test_no_app_module_writes_until_the_rehearsal_passes`,
    changed deliberately in step 7's diff once the rehearsal had passed
    (2026-11-03). What it protected still holds: a second `app/` module that
    starts writing fails here, and has to be added on purpose."""
    assert {path for path in MODIFY_LABELS_CALLERS if path.startswith("app/")} == {
        "app/agent.py"
    }


def test_only_allowed_callers_refer_to_classify_and_label():
    """Rule 5: every entry point that can make the agent write is listed."""
    offenders = [
        name for name, tree in _scanned_files()
        if name != AGENT
        and name not in CLASSIFY_AND_LABEL_CALLERS
        and refers_to(tree, "classify_and_label")
    ]
    assert not offenders, (
        f"classify_and_label referenced outside CLASSIFY_AND_LABEL_CALLERS: {offenders}"
    )


def test_every_listed_agent_caller_exists_and_calls_it():
    for path in CLASSIFY_AND_LABEL_CALLERS:
        source = ROOT / path
        assert source.exists(), f"remove {path} from CLASSIFY_AND_LABEL_CALLERS"
        assert refers_to(ast.parse(source.read_text()), "classify_and_label"), path


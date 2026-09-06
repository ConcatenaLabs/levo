"""The API document names every route the router has, and no other.

doc/api.md is written by hand, and the router's table is what answers. The two
drift apart one endpoint at a time and nobody notices, because a reader of the
document does not have the table open and a reader of the table does not have
the document. This walks both. It reads the document the way it is written --
an entry is **`METHOD /path`**, and a long one wraps after the method -- so it
does not have to be reformatted to be checked.
"""

import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server as SV  # noqa: E402

DOC = HERE.parent.parent / "doc" / "api.md"


def routes_in_the_router():
    out = set()
    for shape, methods in SV.API_METHODS:
        path = "/api/" + "/".join("<slug>" if s == "*" else s for s in shape)
        for m in methods:
            out.add((m, path))
    return out


def routes_in_the_doc():
    text = DOC.read_text(encoding="utf-8")
    # **`GET /api/x`** possibly wrapped as **`POST\n/api/x`**
    found = set()
    for m in re.finditer(r"\*\*`(GET|POST|PATCH|DELETE)\s+(/api/[^`\s]+)`\*\*", text):
        found.add((m.group(1), m.group(2)))
    return found


def test_every_route_is_documented(t):
    router, doc = routes_in_the_router(), routes_in_the_doc()
    t.ok(len(router) >= 25, "the router has its routes", len(router))
    missing = sorted(router - doc)
    t.eq(missing, [], "every route the router answers is in doc/api.md")


def test_the_doc_names_no_route_the_router_lacks(t):
    router, doc = routes_in_the_router(), routes_in_the_doc()
    extra = sorted(doc - router)
    t.eq(extra, [], "doc/api.md names no route the router does not have")


# --- settings: what the code reads, the example file, and the README --------

ROOT = HERE.parent.parent


def _settings_read():
    code = "".join(f.read_text(encoding="utf-8") for f in (ROOT / "levod").glob("*.py"))
    cli = (ROOT / "bin" / "levo").read_text(encoding="utf-8")
    levod = set(re.findall(r"[\"']((?:LEVOD)_[A-Z_]+)[\"']", code + cli))
    cli_vars = set(re.findall(r"[\"']((?:LEVO|SEQUENTIA)_[A-Z_]+)[\"']", cli))
    return levod, cli_vars


def test_every_setting_levod_reads_is_in_the_example_file_and_nowhere_else(t):
    """contrib/levod.env.example claims to list every setting. A setting the
    code reads and the file lacks is one an operator cannot know to set; a
    line in the file nothing reads is one they set for nothing."""
    levod, _ = _settings_read()
    example = set(re.findall(r"^#?(LEVOD?_[A-Z_]+)=", (ROOT / "contrib" / "levod.env.example").read_text(encoding="utf-8"), re.M))
    backup_only = {"LEVO_BACKUP_DIR", "LEVO_BACKUP_KEEP"}     # read by levo-backup.sh
    t.eq(sorted(levod - example), [], "every setting levod reads is in the example file")
    t.eq(sorted(example - levod - backup_only), [], "and the file names nothing that is not read")


def test_every_setting_is_in_the_readme(t):
    """The README's two tables -- levod's settings and the command line's
    environment -- are what a reader arrives at; the code is what runs."""
    levod, cli_vars = _settings_read()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    named = set(re.findall(r"`((?:LEVOD|LEVO|SEQUENTIA)_[A-Z_]+)`", readme))
    t.eq(sorted(levod - named), [], "every levod setting is in the README")
    t.eq(sorted(cli_vars - named), [], "every variable the command line reads is in the README")



# --- error codes: what the document lists is what the server emits ---------

def test_the_documented_error_codes_are_the_ones_the_server_emits(t):
    """doc/api.md lists the codes a client may branch on. Each has to be one
    the server actually sends -- the two refusals written by hand before a
    request is parsed once carried none -- and the server must send none
    the document does not name."""
    doc = DOC.read_text(encoding="utf-8")
    m = re.search(r"The codes are (.*?)\. New codes may appear", doc, re.S)
    t.ok(m, "the document lists its codes")
    listed = set(re.findall(r"`([a-z_]+)`", m.group(1))) - {"allowance_atoms"}   # a field named beside a code
    src = (ROOT / "levod" / "server.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'"code":\s*"([a-z_]+)"', src))
    for a, b in re.findall(r'"([a-z_]+)" if [^\n]*? else "([a-z_]+)"', src):
        emitted |= {a, b}
    t.eq(sorted(listed - emitted), [], "every documented code is one the server emits")
    t.eq(sorted(emitted - listed), [], "and the server emits no code the document does not name")


# --- the command line: what the README lists is what levo has ---------------

def test_the_readme_lists_every_levo_command(t):
    """The README says `levo --help` lists every command and names them. The
    parser is what answers; the sentence has to match it both ways."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"`levo --help` lists every command: (.*?)\.", readme, re.S)
    t.ok(m, "the README names the commands")
    listed = set(re.findall(r"`([a-z]+)`", m.group(1)))
    cli = (ROOT / "bin" / "levo").read_text(encoding="utf-8")
    have = set(re.findall(r'sub\.add_parser\("([a-z]+)"', cli))
    t.eq(sorted(listed - have), [], "the README lists no command levo lacks")
    t.eq(sorted(have - listed), [], "and every command levo has is listed")
    # The usage block at the top of the script is what `levo` with no
    # arguments and `levo --help` open with, and it is typed by hand.
    usage = set(re.findall(r"^    levo ([a-z]+)", cli.split('"""')[1], re.M))
    t.eq(sorted(usage - have), [], "the usage block names no command levo lacks")
    t.eq(sorted(have - usage), [], "and every command levo has is in the usage block")


# --- limits: the numbers the document states are the constants ---------------

def test_the_documented_limits_are_the_constants(t):
    """A cap a client can hit is stated in doc/api.md, and the number there
    has to be the one the code enforces."""
    import market as M
    doc = DOC.read_text(encoding="utf-8")
    m = re.search(r"\*\*Limits a client can meet\.\*\*(.*?)\n\n", doc, re.S)
    t.ok(m, "the document states its limits")
    text = " ".join(m.group(1).split())          # the paragraph wraps; the numbers do not
    t.ok("at most %d of the buyer's inputs" % M.MAX_INPUTS in text, "inputs per purchase")
    t.ok("at most %d purchases per account per sale" % M.MAX_PURCHASES_PER_ACCOUNT in text,
         "ledger entries per account per sale")
    t.ok("at most %d links" % M.MAX_LINKS in text, "links per listing")
    t.ok("at most %d bytes" % SV.MAX_BODY in text, "the request body")


def test_deploy_asks_where_levod_listens_by_default(t):
    """deploy.sh's fallback health URL is levod's own default host and port.

    The script reads the unit's environment file first; this pins the
    fallback for a box where the unit is not installed yet, so a typo in the
    port cannot make every deployment end with "did not answer".
    """
    script = (ROOT / "contrib" / "deploy.sh").read_text()
    server = (ROOT / "levod" / "server.py").read_text()
    port = re.search(r'_setting\("LEVOD_PORT", (\d+)', server).group(1)
    host = re.search(r'"LEVOD_HOST", "([^"]+)"', server).group(1)
    t.eq(re.search(r"^  port=(\d+)$", script, re.M).group(1), port, "fallback port")
    t.eq(re.search(r"^  host=(\S+)$", script, re.M).group(1), host, "fallback host")
    t.ok("EnvironmentFiles" in script, "reads the unit's environment file")


def test_the_documented_defaults_are_the_code(t):
    """The README's Default column is typed by hand; the number that runs is
    the one in the code. Every numeric default levod reads has to appear in
    the row that names its setting."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    rows = {}
    for m in re.finditer(r"^\| (.*?) \| (.*?) \| .*\|$", readme, re.M):
        for name in re.findall(r"`(LEVOD_[A-Z_]+)`", m.group(1)):
            rows[name] = m.group(2)
    defaults = {}
    for f in sorted((ROOT / "levod").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for name, num in re.findall(r'_setting\("(LEVOD_[A-Z_]+)",\s*([0-9.]+)', src):
            defaults[name] = num
        for name, num in re.findall(r'environ\.get\("(LEVOD_[A-Z_]+)",\s*"?([0-9.]+)"?\)', src):
            defaults.setdefault(name, num)
    t.ok(len(defaults) >= 8, "the code declares numeric defaults", sorted(defaults))
    for name, num in sorted(defaults.items()):
        shown = num[:-2] if num.endswith(".0") else num
        t.ok(name in rows, "the README has a row for %s" % name)
        t.ok(name in rows and re.search(r"(?<![0-9.])%s(?![0-9.])" % re.escape(shown), rows[name]),
             "and its default column says %s" % shown, rows.get(name, "")[:80])


def test_every_python_file_parses_under_the_3_8_grammar(t):
    """The README says levod runs on Python 3.8 or later and that every file
    parses under the 3.8 grammar. A newer construct slipping in -- a match
    statement, a parenthesised with -- would fail on the older interpreter
    the README invites, at import time, with a syntax error."""
    files = sorted(list((ROOT / "levod").glob("*.py")) + list((ROOT / "levod" / "tests").glob("*.py"))
                   + list((ROOT / "tools").glob("*.py")) + [ROOT / "bin" / "levo"])
    t.ok(len(files) >= 30, "the walk found the sources", len(files))
    bad = []
    for f in files:
        try:
            ast.parse(f.read_text(encoding="utf-8"), filename=str(f), feature_version=(3, 8))
        except SyntaxError as e:
            bad.append("%s:%s %s" % (f.relative_to(ROOT), e.lineno, e.msg))
    t.eq(bad, [], "every Python file parses under the 3.8 grammar")


def test_levod_imports_the_standard_library_and_its_own_modules_only(t):
    """The README says levod needs nothing outside the standard library. A
    dependency creeping in would turn "python3 levod/server.py" into an
    install step nobody was told about."""
    own = {f.stem for f in (ROOT / "levod").glob("*.py")}
    # The demo's stub node signs, which is what a wallet does and what the
    # backend must never do, so the signer lives with the tests and the demo
    # alone may reach for it. Every other module under levod/ verifies only.
    test_only = {f.stem for f in (ROOT / "levod" / "tests").glob("*.py")}
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:
        t.ok(True, "this interpreter cannot name the standard library; skipped")
        return
    foreign, signing = [], []
    for f in sorted((ROOT / "levod").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in test_only and f.name != "demo.py":
                    signing.append("%s imports %s" % (f.name, name))
                elif top not in stdlib and top not in own and top not in test_only:
                    foreign.append("%s imports %s" % (f.name, name))
    t.eq(sorted(set(foreign)), [], "levod imports only the standard library and itself")
    t.eq(sorted(set(signing)), [], "and nothing but the demo reaches the test-only signer")


def test_the_entry_points_answer_help_and_refuse_arguments(t):
    """python3 levod/server.py --help used to START the server, and so did
    the demo: the one question a newcomer asks first got a listening socket
    for an answer. Both say what they are and where the settings live, and
    refuse an argument rather than ignore it."""
    import subprocess
    for script, needle in (("levod/server.py", "Configuration table"), ("levod/demo.py", "stub node")):
        r = subprocess.run([sys.executable, str(ROOT / script), "--help"],
                           capture_output=True, text=True, timeout=30)
        t.eq(r.returncode, 0, "%s --help exits 0" % script)
        t.ok("takes no arguments" in r.stdout and needle in r.stdout,
             "and says what it is and where its settings are", r.stdout[:200])
        r = subprocess.run([sys.executable, str(ROOT / script), "--port=1"],
                           capture_output=True, text=True, timeout=30)
        t.eq(r.returncode, 2, "%s refuses an argument" % script)
        t.ok("no arguments" in r.stderr, "with a sentence", r.stderr[:200])
    # The vectors generator is the one whose accidental run is a migration:
    # --help and a stray argument must leave levod/vectors.json untouched.
    vectors = ROOT / "levod" / "vectors.json"
    before = vectors.read_bytes()
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_vectors.py"), "--help"],
                       capture_output=True, text=True, timeout=30)
    t.eq(r.returncode, 0, "gen_vectors --help exits 0")
    t.ok("migration" in r.stdout, "and says that running it is a migration", r.stdout[:200])
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_vectors.py"), "--force"],
                       capture_output=True, text=True, timeout=30)
    t.eq(r.returncode, 2, "and it refuses an argument")
    t.eq(vectors.read_bytes(), before, "with the frozen vectors untouched either way")
    # The unit runner itself: --help used to run the whole suite.
    r = subprocess.run([sys.executable, str(ROOT / "levod" / "tests" / "run.py"), "--help"],
                       capture_output=True, text=True, timeout=30)
    t.eq(r.returncode, 0, "run.py --help exits 0")
    t.ok("unit test" in r.stdout and "STANDALONE" in r.stdout, "and says what it runs", r.stdout[:120])
    # The smoke test used to take --help for the address of a Levo and dial it.
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "smoke.py"), "--help"],
                       capture_output=True, text=True, timeout=30)
    t.eq(r.returncode, 0, "smoke.py --help exits 0")
    t.ok("Smoke-test a running Levo" in r.stdout, "and says what it is", r.stdout[:120])
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "smoke.py"), "not-an-address"],
                       capture_output=True, text=True, timeout=30)
    t.eq(r.returncode, 2, "and it refuses a word that is not an address")
    t.ok("one argument" in r.stderr, "with a sentence", r.stderr[:200])
    # The two shell scripts in contrib answer too, and run nothing for it.
    for script in ("contrib/deploy.sh", "contrib/levo-backup.sh", "contrib/levo-alert.sh", "contrib/levo-check.sh"):
        r = subprocess.run(["bash", str(ROOT / script), "--help"], capture_output=True, text=True, timeout=30)
        t.eq(r.returncode, 0, "%s --help exits 0" % script)
        t.ok(r.stdout.startswith("Usage:"), "and prints its usage", r.stdout[:80])


def test_the_units_pass_systemd_verify(t):
    """OnFailure= sat under [Service] once, where systemd does not know it: the
    unit loaded, the journal said "Unknown key name 'OnFailure' ... ignoring",
    and nothing paged anyone. systemd's own verifier says so before a deploy
    does; where it is not installed the check says it skipped."""
    import shutil
    import subprocess
    analyze = shutil.which("systemd-analyze")
    if not analyze:
        t.ok(True, "no systemd-analyze here; the unit check did not run")
        return
    units = sorted(str(p) for p in (ROOT / "contrib").glob("*.service")) + \
            sorted(str(p) for p in (ROOT / "contrib").glob("*.timer"))
    t.ok(len(units) >= 6, "the units are where the deploy script looks", units)
    r = subprocess.run([analyze, "verify", "--man=no"] + units, capture_output=True, text=True, timeout=60)
    noise = [l for l in (r.stdout + r.stderr).splitlines()
             if l.strip() and "Unknown key" in l or "ignoring" in l.lower() or "Failed to" in l]
    t.eq(noise, [], "systemd-analyze verify has nothing to say about the units")
    for p in ("contrib/levod.service", "contrib/levo-backup.service"):
        text = (ROOT / p).read_text()
        unit_section = text.split("[Service]", 1)[0]
        t.ok("OnFailure=levo-alert@%n.service" in unit_section, "%s names the alert under [Unit]" % p)


def test_the_cli_and_the_board_say_a_sale_state_in_the_same_words(t):
    """The API says `partial`; the board said "open" and the CLI said
    "partial", so the same sale read differently in the two places a person
    looks. Both tables live in source; this keeps them one table."""
    import ast
    web = (ROOT / "web" / "src" / "pages" / "Projects.jsx").read_text()
    m = re.search(r"export const STATUS_LABEL = \{(.*?)\}", web, re.S)
    t.ok(m, "the board's STATUS_LABEL is where it was")
    board = dict(re.findall(r"(\w+):\s*'([^']*)'", m.group(1)))
    cli = (ROOT / "bin" / "levo").read_text()
    m2 = re.search(r"STATUS_WORDS = (\{.*?\})\n", cli, re.S)
    t.ok(m2, "the CLI's STATUS_WORDS is where it was")
    words = ast.literal_eval(m2.group(1))
    t.eq(words, board, "the CLI and the board map every sale status to the same word")
    t.ok("partial" in words and "ghost" in words and "closed" in words, "and every status the API can say is in it")

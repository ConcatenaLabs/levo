"""The API document names every route the router has, and no other.

doc/api.md is written by hand, and the router's table is what answers. The two
drift apart one endpoint at a time and nobody notices, because a reader of the
document does not have the table open and a reader of the table does not have
the document. This walks both. It reads the document the way it is written --
an entry is **`METHOD /path`**, and a long one wraps after the method -- so it
does not have to be reformatted to be checked.
"""

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

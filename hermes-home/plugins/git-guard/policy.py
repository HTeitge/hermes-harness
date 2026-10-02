"""git-guard policy engine (pure Python, no Hermes imports; unit-testable on its own).

Purpose: a tool-layer guard for Hermes Agent that makes "never push to / mutate the
git origin" a hard deny, and closes the shell-escape routes around it. It is layer
L1 of three (L2 = read-only system gitconfig + pre-push hook, L3 = egress proxy
with no origin credentials). It is deliberately conservative: anything it cannot
parse or resolve is BLOCKED, never allowed.

Entry points:
    check_tool_call(tool_name, args) -> str | None   # reason to block, or None
    analyze_command(command, workdir=None) -> str | None

The engine never raises out of check_tool_call(); any internal error becomes a
block reason (fail closed).
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

__all__ = ["check_tool_call", "analyze_command", "GuardError", "PROTECTED_TOOLS"]

MAX_COMMAND_CHARS = 64 * 1024
MAX_FILE_SCAN_BYTES = 256 * 1024
MAX_FILES_PER_COMMAND = 5
MAX_RECURSION = 12

PROTECTED_TOOLS = {"terminal", "execute_code", "write_file", "patch", "skill_manage", "process_manage"}


class GuardError(Exception):
    """Raised internally for anything unparseable; always converted to a block."""


# --------------------------------------------------------------------------------------
# Configuration (environment)
# --------------------------------------------------------------------------------------

def _hermes_home() -> str:
    return os.path.realpath(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes"))


def _guard_root() -> str:
    return os.environ.get("GIT_GUARD_ROOT") or "/opt/guard"


def _lifecycle_allowed() -> bool:
    return os.environ.get("GIT_GUARD_ALLOW_CONTAINER_LIFECYCLE", "").lower() in {"1", "true", "yes"}


# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------

SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "fish", "ash", "csh", "tcsh"}
SHELL_STDIN_FLAGS = {"-s", "--stdin"}
SHELL_VALUE_OPTS = {"-o", "+o", "--rcfile", "--init-file"}

INLINE_INTERPRETERS = {
    # name -> options that carry inline code
    "python": {"-c"}, "python3": {"-c"}, "python2": {"-c"}, "pypy": {"-c"}, "pypy3": {"-c"},
    "perl": {"-e", "-E"}, "ruby": {"-e"}, "node": {"-e", "--eval", "-p", "--print"},
    "nodejs": {"-e", "--eval"}, "deno": {"eval"}, "bun": {"-e", "--eval"}, "php": {"-r"},
    "lua": {"-e"}, "luajit": {"-e"}, "Rscript": {"-e"}, "pwsh": {"-c", "-Command", "-command"},
    "powershell": {"-c", "-Command", "-command"}, "awk": set(), "gawk": set(), "mawk": set(), "nawk": set(),
    "sed": set(), "osascript": {"-e"}, "uv": set(), "uvx": set(),
}
PY_LIKE = {"python", "python3", "python2", "pypy", "pypy3"}
INTERP_FILE_RUNNERS = PY_LIKE | {"perl", "ruby", "node", "nodejs", "deno", "bun", "php", "lua", "luajit", "Rscript", "pwsh", "powershell"}

TRANSPARENT_WRAPPERS = {
    # wrapper -> (options that take a value, number of leading positional operands to skip)
    "sudo": ({"-u", "-g", "-p", "-C", "-D", "-h", "-r", "-t", "-T", "-U", "--user", "--group", "--prompt",
              "--chdir", "--host", "--role", "--type", "--other-user"}, 0),
    "doas": ({"-u", "-C"}, 0),
    "env": ({"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-a", "--argv0"}, 0),
    "command": (set(), 0),
    "builtin": (set(), 0),
    "exec": ({"-a"}, 0),
    "nohup": (set(), 0),
    "setsid": (set(), 0),
    "time": ({"-f", "--format", "-o", "--output"}, 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "ionice": ({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"}, 0),
    "chrt": ({"-p", "--pid"}, 1),
    "taskset": ({"-p", "-c", "--pid", "--cpu-list"}, 1),
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, 0),
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, 1),
    "chroot": ({"--userspec", "--groups"}, 1),
    "unshare": ({"-r", "--map-user", "--map-group", "--setgroups", "--wd", "--kill-child"}, 0),
    "flock": ({"-w", "--timeout", "-E", "--conflict-exit-code"}, 1),
    "script": ({"-c", "--command"}, 0),  # handled specially below (-c payload)
    "watch": ({"-n", "--interval", "-d", "--differences"}, 0),
    "strace": ({"-o", "-e", "-p", "-s", "-u", "-E"}, 0),
    "ltrace": ({"-o", "-e", "-p", "-s", "-u"}, 0),
    "xargs": ({"-I", "-i", "-n", "-P", "-d", "-a", "-s", "-L", "-E", "--max-args", "--max-procs", "--delimiter",
               "--arg-file", "--max-chars", "--max-lines", "--eof", "--replace", "--process-slot-var"}, 0),
    "busybox": (set(), 0),
    "su": ({"-c", "--command", "-s", "--shell", "-g", "-G"}, 0),  # -c handled specially
}

ALWAYS_BLOCKED = {
    "eval": "eval executes a constructed string",
    "source": "sourcing a file executes it opaquely",
    ".": "sourcing a file executes it opaquely",
    "trap": "trap defers execution past the guard",
    "alias": "alias can shadow git",
    "crontab": "crontab defers execution past the guard",
    "at": "at defers execution past the guard",
    "batch": "batch defers execution past the guard",
    "systemd-run": "systemd-run defers execution past the guard",
    "tmux": "tmux can send keystrokes to a hidden shell",
    "screen": "screen can send keystrokes to a hidden shell",
    "expect": "expect drives interactive programs opaquely",
    "ssh": "ssh is a git transport (and no credentials exist here)",
    "scp": "scp is an ssh transport",
    "sftp": "sftp is an ssh transport",
    "ssh-add": "ssh-agent manipulation",
    "ssh-keygen": "ssh key manipulation",
    "ssh-agent": "ssh-agent manipulation",
    "gh": "gh can mutate the hosted repository",
    "glab": "glab can mutate the hosted repository",
    "hub": "hub can mutate the hosted repository",
    "tea": "tea can mutate the hosted repository",
    "jj": "jj can push to the git remote",
    "hg": "hg can push to a remote",
    "svn": "svn can commit to a remote",
    "git-receive-pack": "server-side push plumbing",
    "git-send-pack": "client-side push plumbing",
    "git-http-push": "push plumbing",
    "git-push": "git push",
    "git-send-email": "git send-email",
    "git-filter-branch": "runs arbitrary shell per commit",
    "git-filter-repo": "history rewrite tool",
    "git-credential": "credential helper",
    "git-credential-store": "credential helper",
    "git-credential-cache": "credential helper",
    "git-remote-http": "git transport helper",
    "git-remote-https": "git transport helper",
    "git-remote-ssh": "git transport helper",
    "git-remote-ftp": "git transport helper",
    "git-remote-ftps": "git transport helper",
    "git-remote-ext": "git transport helper",
    "git-remote-fd": "git transport helper",
}

MUTATORS = {
    "sed", "tee", "cp", "mv", "rm", "ln", "install", "truncate", "chmod", "chown", "chgrp", "chattr", "dd",
    "rsync", "unzip", "tar", "patch", "shred", "rmdir", "mkdir", "touch", "vim", "vi", "nvim", "nano", "ed",
    "ex", "perl", "python", "python3", "ruby", "node", "sort", "git-config", "setfacl", "mkfifo", "mknod", "gzip",
    "gunzip", "xz", "unxz", "bzip2", "bunzip2", "zip", "7z", "7za", "cat",  # cat only matters via redirects, harmless here
}

ENV_PREFIX_BLOCK = ("GIT_", "DOCKER_", "SSH_", "HERMES_", "COPILOT_", "GITHUB_", "GH_", "LD_PRELOAD", "LD_LIBRARY_PATH")
ENV_NAME_BLOCK = {
    "PATH", "HOME", "XDG_CONFIG_HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "PYTHONPATH", "PYTHONSTARTUP", "PERL5LIB", "NODE_OPTIONS", "BASH_ENV", "ENV",
    "PROMPT_COMMAND", "SHELL", "IFS", "GIT_CONFIG_PARAMETERS",
}

GIT_GLOBAL_FLAGS_OK = {
    "--version", "--help", "-h", "-p", "--paginate", "-P", "--no-pager", "--no-replace-objects", "--bare",
    "--no-optional-locks", "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
    "--no-lazy-fetch", "--no-advice", "--html-path", "--man-path", "--info-path",
}
GIT_GLOBAL_FLAGS_WITH_VALUE_OK = {"-C"}
GIT_GLOBAL_FLAGS_BLOCK = {"-c", "--exec-path", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                          "--config-env", "--attr-source"}

GIT_SUB_ALLOW = {
    "add", "am", "annotate", "apply", "archive", "bisect", "blame", "branch", "bundle", "cat-file", "check-attr",
    "check-ignore", "check-mailmap", "check-ref-format", "checkout", "checkout-index", "cherry", "cherry-pick",
    "citool", "clean", "clone", "column", "commit", "commit-graph", "commit-tree", "config", "count-objects",
    "describe", "diff", "diff-files", "diff-index", "diff-tree", "difftool", "fast-export", "fast-import", "fetch",
    "fetch-pack", "fmt-merge-msg", "for-each-ref", "for-each-repo", "format-patch", "fsck", "gc", "get-tar-commit-id",
    "grep", "gui", "hash-object", "help", "hook", "index-pack", "init", "interpret-trailers", "log", "ls-files",
    "ls-remote", "ls-tree", "mailinfo", "mailsplit", "maintenance", "merge", "merge-base", "merge-file",
    "merge-index", "merge-one-file", "merge-tree", "mergetool", "mktag", "mktree", "multi-pack-index", "mv",
    "name-rev", "notes", "pack-objects", "pack-redundant", "pack-refs", "patch-id", "prune", "prune-packed", "pull",
    "range-diff", "read-tree", "rebase", "reflog", "remote", "repack", "replace", "request-pull", "rerere", "reset",
    "restore", "rev-list", "rev-parse", "revert", "rm", "shortlog", "show", "show-branch", "show-index", "show-ref",
    "sparse-checkout", "stage", "stash", "status", "stripspace", "submodule", "switch", "symbolic-ref", "tag",
    "unpack-file", "unpack-objects", "update-index", "update-ref", "update-server-info", "var", "verify-commit",
    "verify-pack", "verify-tag", "version", "whatchanged", "worktree", "write-tree", "lfs", "subtree", "flow",
    "scalar", "diagnose", "credential-fill",  # harmless read of credential config (none exists)
}
GIT_SUB_BLOCK = {
    "push", "send-email", "send-pack", "receive-pack", "http-push", "svn", "p4", "daemon", "instaweb", "cvsexportcommit",
    "cvsserver", "cvsimport", "imap-send", "upload-pack", "upload-archive", "filter-branch", "filter-repo", "credential",
    "credential-store", "credential-cache", "credential-cache--daemon", "http-fetch", "http-backend", "remote-http",
    "remote-https", "remote-ssh", "remote-ftp", "remote-ftps", "remote-ext", "remote-fd", "shell", "archimport",
    "quiltimport", "fast-import--remote", "web--browse", "sh-i18n--envsubst",
}
GIT_REMOTE_READ = {"-v", "--verbose", "show", "get-url", "-h", "--help", "update", "prune"}
GIT_REMOTE_WRITE = {"add", "rename", "remove", "rm", "set-head", "set-branches", "set-url"}
GIT_CONFIG_READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color", "--get-colorbool",
                         "--list", "-l", "--show-origin", "--show-scope", "--null", "-z", "--name-only", "--includes",
                         "--no-includes", "--bool", "--int", "--bool-or-int", "--path", "--expiry-date", "--type",
                         "--default", "--fixed-value", "--global", "--local", "--system", "--worktree", "--blob", "-f",
                         "--file", "-h", "--help"}
GIT_CONFIG_WRITE_FLAGS = {"--add", "--unset", "--unset-all", "--replace-all", "--edit", "-e", "--rename-section",
                          "--remove-section"}
GIT_CONFIG_SUBCMDS_READ = {"list", "get"}
GIT_CONFIG_SUBCMDS_WRITE = {"set", "unset", "edit", "rename-section", "remove-section"}
GIT_EXEC_OPTS = {"-x", "--exec", "--extcmd", "--tool-cmd"}  # rebase -x, difftool -x/--extcmd
GIT_SYSTEM_PATHS = {"/usr/bin/git", "/bin/git", "/usr/local/bin/git"}

DOCKER_READ_SUBS = {"ps", "logs", "inspect", "top", "stats", "port", "diff", "images", "version", "info", "events",
                    "wait", "export"}
DOCKER_LIFECYCLE_SUBS = {"start", "stop", "restart", "kill", "pause", "unpause"}
DOCKER_EXEC_SUBS = {"exec", "attach"}
DOCKER_GROUP_READ = {("image", "ls"), ("image", "list"), ("image", "inspect"), ("image", "history"),
                     ("container", "ls"), ("container", "list"), ("container", "inspect"), ("container", "logs"),
                     ("container", "top"), ("container", "stats"), ("container", "port"), ("container", "diff"),
                     ("container", "exec"), ("network", "ls"), ("network", "inspect"), ("volume", "ls"),
                     ("volume", "inspect"), ("system", "df"), ("system", "info"), ("system", "events"),
                     ("compose", "ps"), ("compose", "logs"), ("compose", "top"), ("compose", "config"),
                     ("compose", "images"), ("compose", "ls"), ("compose", "events")}
DOCKER_GROUP_LIFECYCLE = {("container", "start"), ("container", "stop"), ("container", "restart"),
                         ("container", "kill"), ("compose", "up"), ("compose", "down"), ("compose", "restart"),
                         ("compose", "start"), ("compose", "stop"), ("compose", "kill")}

KEYWORDS_STRIP_LEADING = {"{", "}", "!", "if", "while", "until", "then", "do", "else", "elif", "time", "coproc"}
KEYWORDS_SEPARATOR = {"then", "do", "else", "elif", "fi", "done", "esac"}
KEYWORDS_NOEXEC_SEGMENT = {"for", "case", "select", "function", "in"}

SEPARATOR_TOKENS = {";", ";;", ";&", ";;&", "&&", "||", "|", "|&", "&", "(", ")"}
REDIRECT_OUT_PREFIXES = (">", "&>", ">>", ">|", ">&")
REDIRECT_TOKENS = {">", ">>", "<", "<<<", "&>", "&>>", ">&", "<&", ">|", "<>"}

SUBST_PLACEHOLDER = "__GG_SUBST__"
HEREDOC_PLACEHOLDER = "__GG_HEREDOC__"

# Strict text heuristics for opaque code we cannot parse (inline interpreter code, scripts).
_STRICT_PATTERNS = [
    (re.compile(r"\bgit\b[^\n;|&]{0,200}?\bpush\b"), "git push"),
    (re.compile(r"\bgit\b[^\n;|&]{0,200}?\bsend-email\b"), "git send-email"),
    (re.compile(r"\b(receive-pack|send-pack|git-http-push)\b"), "push plumbing"),
    (re.compile(r"\bgit\b[^\n;|&]{0,200}?\bremote\s+(set-url|add|rm|remove|rename|set-head|set-branches)\b"),
     "git remote mutation"),
    (re.compile(r"\bgit\b[^\n;|&]{0,200}?\bconfig\b[^\n;|&]{0,200}?(--add|--unset|--replace-all|--edit|\s-e\b|\bset\b|"
                r"\bunset\b|\bedit\b|core\.hooksPath|hooksPath|url\.|remote\.|alias\.|credential|pushurl|insteadOf)"),
     "git config mutation"),
    (re.compile(r"\b(pushurl|pushInsteadOf|hooksPath|GIT_CONFIG\w*|GIT_SSH\w*|GIT_ASKPASS|GIT_EXEC_PATH|GIT_PROXY_COMMAND|"
                r"core\.sshCommand|credential\.helper)\b"), "git config/env override"),
    (re.compile(r"""['"]git['"][^\n]{0,160}?['"](push|send-email)['"]"""), "argv-style git push"),
    (re.compile(r"\bgh\s+(pr\s+(create|merge|close|edit|ready|comment|review)|release|repo\s+(create|delete|fork|rename|"
                r"edit|sync)|api\b[^\n]{0,80}(-X|--method)\s*(POST|PUT|PATCH|DELETE))"), "gh mutation"),
    (re.compile(r"\b(glab|hub)\s+(mr|pr|push|repo|release)\b"), "hosted-repo CLI mutation"),
    (re.compile(r"\bssh\b[^\n]{0,120}\bgit@"), "ssh to a git host"),
]
_STRICT_INLINE_EXTRA = [
    (re.compile(r"\b(b64decode|base64\s+(-d|--decode|-D)\b|fromCharCode|codecs\.decode\([^\n]{0,80}rot)"),
     "decode-then-execute obfuscation"),
    (re.compile(r"(\\x[0-9a-fA-F]{2}){3,}"), "hex-escaped string in inline code"),
    (re.compile(r"\b(subprocess|os\.system|os\.popen|os\.exec\w*|Popen|child_process|execSync|spawnSync|"
                r"shell_exec|passthru|proc_open|system\s*\()[^\n]{0,200}\b(git|gh|ssh)\b"), "shell-out to git/ssh"),
]


# --------------------------------------------------------------------------------------
# Pre-scanner: quote-aware pass that lifts substitutions and heredocs out of the text
# --------------------------------------------------------------------------------------

@dataclass
class _Scan:
    text: str
    substitutions: List[str] = field(default_factory=list)
    heredocs: List[str] = field(default_factory=list)


def _find_matching(s: str, i: int, open_ch: str, close_ch: str) -> int:
    """Index of the close char matching s[i]==open_ch, respecting quotes. Raises GuardError."""
    depth = 0
    in_s = in_d = False
    j = i
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\" and not in_s and j + 1 < n:
            j += 2
            continue
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return j
        j += 1
    raise GuardError("unbalanced command substitution")


def _prescan(s: str) -> _Scan:
    out: List[str] = []
    subs: List[str] = []
    heredocs: List[str] = []
    pending_heredocs: List[Tuple[str, bool]] = []  # (delimiter, strip_tabs)
    in_s = in_d = False
    i = 0
    n = len(s)

    def at_word_start() -> bool:
        return not out or out[-1][-1:] in {"", " ", "\t", ";", "(", "|", "&", "\n"}

    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        if c == "\\" and not in_s:
            if nxt == "\n":  # line continuation
                i += 2
                continue
            out.append(s[i:i + 2])
            i += 2
            continue
        if c == "'" and not in_d:
            in_s = not in_s
            out.append(c)
            i += 1
            continue
        if c == '"' and not in_s:
            in_d = not in_d
            out.append(c)
            i += 1
            continue
        if in_s:
            out.append(c)
            i += 1
            continue
        # --- outside single quotes from here (double quotes still expand) ---
        if c == "$" and nxt == "(":
            if s[i + 2:i + 3] == "(":  # arithmetic $(( ))
                j = _find_matching(s, i + 1, "(", ")")
                inner = s[i + 3:j - 1] if s[j - 1:j] == ")" else s[i + 3:j]
                subs.append(inner)
                out.append(SUBST_PLACEHOLDER)
                i = j + 1
                continue
            j = _find_matching(s, i + 1, "(", ")")
            subs.append(s[i + 2:j])
            out.append(SUBST_PLACEHOLDER)
            i = j + 1
            continue
        if c == "`":
            j = i + 1
            while j < n and not (s[j] == "`" and s[j - 1] != "\\"):
                j += 1
            if j >= n:
                raise GuardError("unbalanced backtick")
            subs.append(s[i + 1:j])
            out.append(SUBST_PLACEHOLDER)
            i = j + 1
            continue
        if in_d:
            out.append(c)
            i += 1
            continue
        # --- fully unquoted from here ---
        if c in "<>" and nxt == "(":  # process substitution
            j = _find_matching(s, i + 1, "(", ")")
            subs.append(s[i + 2:j])
            out.append(" " + SUBST_PLACEHOLDER + " ")
            i = j + 1
            continue
        if c == "<" and nxt == "<" and s[i + 2:i + 3] != "<":  # heredoc
            k = i + 2
            strip_tabs = False
            if s[k:k + 1] == "-":
                strip_tabs = True
                k += 1
            while k < n and s[k] in " \t":
                k += 1
            # delimiter word (may be quoted)
            if s[k:k + 1] in {"'", '"'}:
                q = s[k]
                e = s.find(q, k + 1)
                if e < 0:
                    raise GuardError("unterminated heredoc delimiter")
                delim = s[k + 1:e]
                k = e + 1
            else:
                e = k
                while e < n and not s[e].isspace() and s[e] not in ";|&<>()":
                    if s[e] == "\\":
                        e += 1
                    e += 1
                delim = s[k:e].replace("\\", "")
                k = e
            if not delim:
                raise GuardError("empty heredoc delimiter")
            pending_heredocs.append((delim, strip_tabs))
            out.append(" " + HEREDOC_PLACEHOLDER + " ")
            i = k
            continue
        if c == "#" and at_word_start():
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c == "\n":
            out.append(" ; ")
            i += 1
            if pending_heredocs:
                for delim, strip_tabs in pending_heredocs:
                    body_lines: List[str] = []
                    while True:
                        if i >= n:
                            raise GuardError("unterminated heredoc body")
                        e = s.find("\n", i)
                        line = s[i:] if e < 0 else s[i:e]
                        i = n if e < 0 else e + 1
                        cmp_line = line.lstrip("\t") if strip_tabs else line
                        if cmp_line == delim:
                            break
                        body_lines.append(line)
                    heredocs.append("\n".join(body_lines))
                pending_heredocs.clear()
            continue
        out.append(c)
        i += 1
    if in_s or in_d:
        raise GuardError("unbalanced quotes")
    if pending_heredocs:
        # heredoc operator with no body (command ended): treat the rest as empty bodies
        for _ in pending_heredocs:
            heredocs.append("")
    return _Scan("".join(out), subs, heredocs)


# --------------------------------------------------------------------------------------
# Tokenizer + segmenter
# --------------------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""  # comments were removed by the prescan; '#' inside words is data
    try:
        return list(lex)
    except ValueError as exc:
        raise GuardError(f"tokenizer: {exc}") from None


def _is_punct(tok: str) -> bool:
    return bool(tok) and all(ch in "();<>|&" for ch in tok)


@dataclass
class _Segment:
    argv: List[str]
    out_targets: List[str]
    has_heredoc: bool
    stdin_from_pipe: bool


def _segments(tokens: Sequence[str]) -> List[_Segment]:
    segs: List[_Segment] = []
    cur: List[str] = []
    outs: List[str] = []
    has_heredoc = False
    prev_sep: Optional[str] = None
    i = 0
    n = len(tokens)

    def flush(sep: Optional[str]) -> None:
        nonlocal cur, outs, has_heredoc, prev_sep
        if cur or outs or has_heredoc:
            segs.append(_Segment(cur, outs, has_heredoc, prev_sep in {"|", "|&"}))
        cur, outs, has_heredoc = [], [], False
        prev_sep = sep

    while i < n:
        tok = tokens[i]
        if _is_punct(tok):
            if tok in SEPARATOR_TOKENS:
                flush(tok)
                i += 1
                continue
            if "<" in tok or ">" in tok:
                if tok not in REDIRECT_TOKENS:
                    raise GuardError(f"unrecognised redirection {tok!r}")
                # redirect: drop a preceding single-digit fd token, consume target
                if cur and re.fullmatch(r"[0-9]", cur[-1]):
                    cur.pop()
                target = tokens[i + 1] if i + 1 < n else ""
                if _is_punct(target):
                    target = ""
                    i += 1
                else:
                    i += 2
                if tok.startswith(REDIRECT_OUT_PREFIXES) and target and not re.fullmatch(r"-|[0-9]+", target):
                    outs.append(target)
                continue
            raise GuardError(f"unrecognised shell operator {tok!r}")
        if tok == HEREDOC_PLACEHOLDER:
            has_heredoc = True
            i += 1
            continue
        if tok in KEYWORDS_SEPARATOR:
            flush(";")
            i += 1
            continue
        cur.append(tok)
        i += 1
    flush(None)
    return segs


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def _home() -> str:
    return os.path.expanduser("~")


def _norm_path_token(tok: str) -> str:
    t = tok
    if t.startswith("~"):
        t = _home() + t[1:] if t == "~" or t.startswith("~/") else t
    if t.startswith("$HOME/") or t == "$HOME":
        t = _home() + t[5:]
    if t.startswith("${HOME}/") or t == "${HOME}":
        t = _home() + t[7:]
    return t


_PROTECTED_RES = [
    re.compile(r"(^|/)\.git(/|$)"),
    re.compile(r"(^|/)\.gitconfig$"),
    re.compile(r"(^|/)\.git-credentials$"),
    re.compile(r"(^|/)\.ssh(/|$)"),
    re.compile(r"^/etc/(gitconfig|ssh)(/|$)"),
    re.compile(r"(^|/)\.hermes/(plugins|hooks)(/|$)"),
    re.compile(r"(^|/)\.config/git(/|$)"),
    re.compile(r"(^|/)\.docker/config\.json$"),
]


def _is_protected_path(tok: str) -> bool:
    t = _norm_path_token(tok)
    if not t or t.startswith("-"):
        return False
    for rx in _PROTECTED_RES:
        if rx.search(t):
            return True
    hh = _hermes_home()
    try:
        rp = os.path.realpath(t) if t.startswith("/") else t
    except Exception:
        rp = t
    for base in (t, rp):
        if base == hh:
            return True
        if base.startswith(hh + "/"):
            rest = base[len(hh) + 1:].split("/", 1)[0]
            if rest in {"config.yaml", ".env", "plugins", "hooks", "auth.json", "git-guard", "vault", "profiles"}:
                return True
        if base.startswith(_guard_root() + "/") or base == _guard_root():
            return True
    return False


def _strict_scan(text: str, inline: bool) -> Optional[str]:
    if not text:
        return None
    for rx, why in _STRICT_PATTERNS:
        if rx.search(text):
            return why
    if inline:
        for rx, why in _STRICT_INLINE_EXTRA:
            if rx.search(text):
                return why
    return None


def _read_small_file(path: str, workdir: Optional[str]) -> Optional[str]:
    cands = [path]
    if not os.path.isabs(path):
        if workdir:
            cands.insert(0, os.path.join(workdir, path))
        cands.append(os.path.join(os.getcwd(), path))
    for p in cands:
        try:
            if os.path.isfile(p) and os.path.getsize(p) <= MAX_FILE_SCAN_BYTES:
                with open(p, "rb") as fh:
                    data = fh.read(MAX_FILE_SCAN_BYTES)
                if b"\x00" in data[:4096]:
                    return None  # binary
                return data.decode("utf-8", errors="replace")
        except OSError:
            continue
    return None


def _env_name_blocked(name: str) -> bool:
    return name in ENV_NAME_BLOCK or name.startswith(ENV_PREFIX_BLOCK)


# --------------------------------------------------------------------------------------
# Command analysis
# --------------------------------------------------------------------------------------

class _Ctx:
    def __init__(self, workdir: Optional[str]):
        self.workdir = workdir
        self.files_scanned = 0
        self.depth = 0


def _git_rules(argv: List[str]) -> Optional[str]:
    """argv[0] is git. Return block reason or None."""
    i = 1
    n = len(argv)
    while i < n:
        a = argv[i]
        if not a.startswith("-"):
            break
        if a in GIT_GLOBAL_FLAGS_OK or a.startswith("--list-cmds"):
            i += 1
            continue
        if a in GIT_GLOBAL_FLAGS_WITH_VALUE_OK:
            i += 2
            continue
        base = a.split("=", 1)[0]
        if base in GIT_GLOBAL_FLAGS_BLOCK or a.startswith("-c"):
            return f"git global option {base!r} can redirect configuration or binaries"
        return f"unknown git global option {a!r}"
    if i >= n:
        return None  # bare `git` prints usage
    sub = argv[i]
    rest = argv[i + 1:]
    if any(ch in sub for ch in "$*?[{") or sub == SUBST_PLACEHOLDER:
        return "git subcommand is not a literal"
    if sub in GIT_SUB_BLOCK:
        return f"git {sub} is denied (origin mutation / transport / history rewrite)"
    if sub not in GIT_SUB_ALLOW:
        return f"git subcommand {sub!r} is not allowlisted (could be an alias)"
    # subcommand-specific checks
    if sub == "remote":
        positional = [a for a in rest if not a.startswith("-")]
        verb = positional[0] if positional else None
        if verb in GIT_REMOTE_WRITE:
            return f"git remote {verb} mutates remote configuration"
        if verb is not None and verb not in GIT_REMOTE_READ:
            return f"git remote {verb!r} is not a read-only form"
        return None
    if sub == "config":
        positional = [a for a in rest if not a.startswith("-")]
        flags = [a.split("=", 1)[0] for a in rest if a.startswith("-")]
        for f in flags:
            if f in GIT_CONFIG_WRITE_FLAGS:
                return f"git config {f} writes configuration"
            if f not in GIT_CONFIG_READ_FLAGS:
                return f"git config option {f!r} not recognised as read-only"
        if positional and positional[0] in GIT_CONFIG_SUBCMDS_WRITE:
            return f"git config {positional[0]} writes configuration"
        if positional and positional[0] in GIT_CONFIG_SUBCMDS_READ:
            positional = positional[1:]
        # value-taking read flags consume one positional each
        consuming = sum(1 for a in rest if a in {"--type", "--default", "-f", "--file", "--blob", "--get-urlmatch",
                                                   "--get-color", "--get-colorbool"} )
        if len(positional) - consuming > 1 and not any(f in {"--get", "--get-all", "--get-regexp", "--get-urlmatch",
                                                            "--get-color", "--get-colorbool"} for f in flags):
            return "git config <name> <value> writes configuration"
        return None
    if sub == "submodule":
        if "foreach" in rest:
            return "git submodule foreach runs arbitrary commands"
        return None
    if sub == "archive" and any(a.startswith("--remote") for a in rest):
        return "git archive --remote contacts a remote"
    if sub in {"rebase", "difftool", "mergetool", "bisect"}:
        for a in rest:
            if a.split("=", 1)[0] in GIT_EXEC_OPTS:
                return f"git {sub} {a} runs arbitrary commands"
        if sub == "bisect" and rest[:1] == ["run"]:
            return "git bisect run executes a command per step (not inspectable here)"
    if sub == "lfs" and rest[:1] and rest[0] in {"push", "migrate", "env", "install"}:
        return f"git lfs {rest[0]} is denied"
    if sub == "subtree" and rest[:1] == ["push"]:
        return "git subtree push is denied"
    if sub == "flow" and any(a in {"publish", "finish"} for a in rest):
        return "git flow publish/finish pushes"
    if sub == "hook" and rest[:1] == ["run"]:
        return None
    for a in rest:
        if a.startswith("--output=") or a == "--output":
            tgt = a.split("=", 1)[1] if "=" in a else ""
            if tgt and _is_protected_path(tgt):
                return "git --output targets a protected path"
    return None


def _docker_rules(argv: List[str]) -> Optional[str]:
    rest = argv[1:]
    i = 0
    # global flags
    while i < len(rest) and rest[i].startswith("-"):
        f = rest[i].split("=", 1)[0]
        if f in {"-H", "--host", "--context", "-c", "--config", "--tlscacert", "--tlscert", "--tlskey"}:
            return f"docker {f} can retarget the daemon"
        if f in {"-l", "--log-level"}:
            i += 2
            continue
        i += 1
    if i >= len(rest):
        return None
    sub = rest[i]
    nxt = rest[i + 1] if i + 1 < len(rest) else ""
    if sub in DOCKER_READ_SUBS or sub in DOCKER_EXEC_SUBS:
        return None
    if sub in DOCKER_LIFECYCLE_SUBS:
        return None if _lifecycle_allowed() else f"docker {sub} is container lifecycle (set GIT_GUARD_ALLOW_CONTAINER_LIFECYCLE=1 to permit)"
    if (sub, nxt) in DOCKER_GROUP_READ:
        return None
    if (sub, nxt) in DOCKER_GROUP_LIFECYCLE:
        return None if _lifecycle_allowed() else f"docker {sub} {nxt} is container lifecycle (set GIT_GUARD_ALLOW_CONTAINER_LIFECYCLE=1 to permit)"
    return f"docker {sub} {nxt}".strip() + " is not an allowlisted read/exec/lifecycle operation (create/run/build/cp/push/login are denied)"


def _analyze_segment(seg: _Segment, scan: _Scan, ctx: _Ctx) -> Optional[str]:
    argv = list(seg.argv)
    # redirect targets
    for t in seg.out_targets:
        if _is_protected_path(t):
            return f"redirect writes to protected path {t!r}"
        if t == SUBST_PLACEHOLDER or "$" in t:
            return "redirect target is not a literal"
    # leading keywords
    while argv and argv[0] in KEYWORDS_STRIP_LEADING:
        argv.pop(0)
    if not argv:
        return None
    if argv[0] in KEYWORDS_NOEXEC_SEGMENT:
        return None
    # leading env assignments
    while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0], re.S):
        name = argv[0].split("=", 1)[0]
        if _env_name_blocked(name):
            return f"environment override of {name} is denied"
        argv.pop(0)
    if not argv:
        return None
    return _dispatch(argv, seg, scan, ctx)


def _dispatch(argv: List[str], seg: _Segment, scan: _Scan, ctx: _Ctx) -> Optional[str]:
    ctx.depth += 1
    try:
        if ctx.depth > MAX_RECURSION:
            return "command nesting too deep"
        argv0 = argv[0]
        if argv0 in {"[", "[[", "test", ":", "true", "false"}:
            return None  # test expressions / no-ops do not execute anything
        if argv0 == SUBST_PLACEHOLDER or any(ch in argv0 for ch in "$*?[{"):
            return f"executable {argv0!r} is not a literal"
        name = os.path.basename(argv0)
        is_path = "/" in argv0

        if name in ALWAYS_BLOCKED:
            return f"{name}: {ALWAYS_BLOCKED[name]}"

        if name in {"export", "declare", "typeset", "readonly", "unset", "local"}:
            for a in argv[1:]:
                if a.startswith("-"):
                    continue
                var = a.split("=", 1)[0]
                if _env_name_blocked(var):
                    return f"{name} of {var} is denied"
            return None

        if name == "git":
            if is_path and argv0 not in GIT_SYSTEM_PATHS:
                r = _scan_file_operand(argv0, ctx, inline=False)
                if r:
                    return r
            return _git_rules(["git"] + argv[1:])

        if name == "docker" or name == "podman" or name == "nerdctl":
            return _docker_rules(argv)

        if name in SHELLS:
            return _shell_rules(argv, seg, scan, ctx)

        if name in TRANSPARENT_WRAPPERS:
            return _wrapper_rules(name, argv, seg, scan, ctx)

        if name == "find":
            return _find_rules(argv, seg, scan, ctx)

        if name in INLINE_INTERPRETERS:
            r = _interpreter_rules(name, argv, seg, scan, ctx)
            if r:
                return r

        if name in MUTATORS:
            for a in argv[1:]:
                if _is_protected_path(a):
                    return f"{name} touches protected path {a!r}"

        # a script/binary invoked by path: scan it if readable
        if is_path and name not in INLINE_INTERPRETERS:
            r = _scan_file_operand(argv0, ctx, inline=False)
            if r:
                return r

        if seg.has_heredoc and name in (SHELLS | INTERP_FILE_RUNNERS):
            for body in scan.heredocs:
                r = _strict_scan(body, inline=True)
                if r:
                    return f"heredoc fed to {name} contains {r}"
        return None
    finally:
        ctx.depth -= 1


def _scan_file_operand(path: str, ctx: _Ctx, inline: bool) -> Optional[str]:
    if ctx.files_scanned >= MAX_FILES_PER_COMMAND:
        return None
    ctx.files_scanned += 1
    content = _read_small_file(_norm_path_token(path), ctx.workdir)
    if content is None:
        return None
    r = _strict_scan(content, inline=inline)
    if r:
        return f"script {path!r} contains {r}"
    return None


def _shell_rules(argv: List[str], seg: _Segment, scan: _Scan, ctx: _Ctx) -> Optional[str]:
    name = os.path.basename(argv[0])
    i = 1
    payload: Optional[str] = None
    file_operand: Optional[str] = None
    while i < len(argv):
        a = argv[i]
        if a == "-c" or (a.startswith("-") and not a.startswith("--") and "c" in a[1:] and a[1:].isalpha()):
            if a == "-c" or a.endswith("c"):
                payload = argv[i + 1] if i + 1 < len(argv) else ""
                break
            return f"{name} {a}: cannot determine -c payload position"
        if a in SHELL_STDIN_FLAGS:
            return f"{name} -s reads a script from stdin"
        if a in SHELL_VALUE_OPTS:
            i += 2
            continue
        if a == "--":
            i += 1
            if i < len(argv):
                file_operand = argv[i]
            break
        if a.startswith("-") or a.startswith("+"):
            i += 1
            continue
        file_operand = a
        break
    if payload is not None:
        if payload == SUBST_PLACEHOLDER or (not payload and seg.stdin_from_pipe):
            return f"{name} -c payload is not a literal"
        return _analyze_text(payload, ctx)
    if file_operand is not None:
        if any(ch in file_operand for ch in "$*?[{") or file_operand == SUBST_PLACEHOLDER:
            return f"{name}: script path is not a literal"
        return _scan_file_operand(file_operand, ctx, inline=False) or (
            None if _read_small_file(_norm_path_token(file_operand), ctx.workdir) is not None
            else f"{name}: script {file_operand!r} is not readable for inspection")
    if seg.stdin_from_pipe or seg.has_heredoc:
        if seg.has_heredoc:
            for body in scan.heredocs:
                r = _strict_scan(body, inline=True)
                if r:
                    return f"heredoc script contains {r}"
            return f"{name} reading a script from a heredoc is denied"
        return f"{name} reading a script from a pipe is denied"
    return f"interactive {name} with no command is denied"


def _wrapper_rules(name: str, argv: List[str], seg: _Segment, scan: _Scan, ctx: _Ctx) -> Optional[str]:
    value_opts, skip_positional = TRANSPARENT_WRAPPERS[name]
    i = 1
    n = len(argv)
    while i < n:
        a = argv[i]
        if name in {"script", "su"} and a in {"-c", "--command"}:
            payload = argv[i + 1] if i + 1 < n else ""
            return _analyze_text(payload, ctx) if payload != SUBST_PLACEHOLDER else f"{name} -c payload not literal"
        if name == "env" and (a in {"-S", "--split-string"} or a.startswith("--split-string=") or a.startswith("-S")):
            payload = a.split("=", 1)[1] if a.startswith("--split-string=") else (a[2:] if a.startswith("-S") and len(a) > 2 else (argv[i + 1] if i + 1 < n else ""))
            tail = argv[i + 2:] if payload == (argv[i + 1] if i + 1 < n else None) else argv[i + 1:]
            try:
                inner = _tokenize(payload) + tail
            except GuardError as exc:
                return f"env -S: {exc}"
            if not inner:
                return None
            return _dispatch(inner, seg, scan, ctx)
        if name == "env" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", a, re.S):
            if _env_name_blocked(a.split("=", 1)[0]):
                return f"env override of {a.split('=', 1)[0]} is denied"
            i += 1
            continue
        if name == "command" and a in {"-v", "-V"}:
            return None  # lookup, not execution
        if name == "xargs" and a in {"-I", "-i"} or (name == "xargs" and a.startswith("-I")):
            i += 2 if a in {"-I", "-i"} else 1
            continue
        if a == "--":
            i += 1
            break
        if a.startswith("-"):
            base = a.split("=", 1)[0]
            if base in value_opts and "=" not in a:
                i += 2
            else:
                i += 1
            continue
        break
    i += skip_positional
    inner = argv[i:]
    if not inner:
        return None if name != "xargs" else None  # xargs with no command = echo
    if name == "sudo" and inner[0] in {"-i", "-s"}:
        return "sudo shell is denied"
    return _dispatch(inner, seg, scan, ctx)


def _find_rules(argv: List[str], seg: _Segment, scan: _Scan, ctx: _Ctx) -> Optional[str]:
    i = 1
    n = len(argv)
    while i < n:
        a = argv[i]
        if a in {"-exec", "-execdir", "-ok", "-okdir"}:
            j = i + 1
            inner: List[str] = []
            while j < n and argv[j] not in {";", "+"}:
                inner.append(argv[j])
                j += 1
            inner = [t for t in inner if t != "{}"]
            if inner:
                r = _dispatch(inner, seg, scan, ctx)
                if r:
                    return f"find {a}: {r}"
            i = j + 1
            continue
        if a == "-delete":
            pass
        if a in {"-fprint", "-fprint0", "-fprintf", "-fls"} and i + 1 < n and _is_protected_path(argv[i + 1]):
            return f"find {a} writes a protected path"
        i += 1
    return None


def _interpreter_rules(name: str, argv: List[str], seg: _Segment, scan: _Scan, ctx: _Ctx) -> Optional[str]:
    inline_opts = INLINE_INTERPRETERS[name]
    args = argv[1:]
    if name in {"pwsh", "powershell"} and any(a.lower() in {"-encodedcommand", "-enc", "-e", "-ec"} for a in args):
        return f"{name} -EncodedCommand is denied"
    if name in {"awk", "gawk", "mawk", "nawk", "sed"}:
        # program text = first non-option arg (or -e/-f values)
        prog_parts: List[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in {"-e", "--expression", "-f", "--file"} and i + 1 < len(args):
                if a in {"-f", "--file"}:
                    content = _read_small_file(_norm_path_token(args[i + 1]), ctx.workdir)
                    if content is None:
                        return f"{name} -f program {args[i + 1]!r} is not readable for inspection"
                    prog_parts.append(content)
                else:
                    prog_parts.append(args[i + 1])
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            if not prog_parts:
                prog_parts.append(a)
            i += 1
        prog = "\n".join(prog_parts)
        if name.endswith("awk") and re.search(r"\bsystem\s*\(|\|\s*\"?(sh|bash)\b|getline\s*<", prog):
            return f"{name} program shells out"
        if name == "sed" and re.search(r"(^|;|\n)\s*e\b|\bw\s*/", prog):
            return "sed program executes or writes"
        return _strict_scan(prog, inline=True)
    if name in {"uv", "uvx"}:
        if args[:1] == ["run"] and len(args) > 1:
            return _dispatch(args[1:], seg, scan, ctx) if args[1] not in {"--", "-"} else None
        return None
    if name == "deno":
        if args[:1] == ["eval"] and len(args) > 1:
            return _strict_scan(args[1], inline=True)
        return None
    i = 0
    while i < len(args):
        a = args[i]
        if a in inline_opts:
            code = args[i + 1] if i + 1 < len(args) else ""
            r = _strict_scan(code, inline=True)
            return f"{name} inline code contains {r}" if r else None
        if a == "-" or a == "/dev/stdin":
            if seg.stdin_from_pipe:
                return f"{name} reading a script from a pipe is denied"
            if seg.has_heredoc:
                for body in scan.heredocs:
                    r = _strict_scan(body, inline=True)
                    if r:
                        return f"heredoc fed to {name} contains {r}"
                return None
            return None
        if name in PY_LIKE and a == "-m":
            return None  # module run; project code, not inspectable here
        if a.startswith("-"):
            i += 1
            continue
        # first positional = script file
        if name in INTERP_FILE_RUNNERS:
            return _scan_file_operand(a, ctx, inline=False)
        return None
    if not args and (seg.stdin_from_pipe or seg.has_heredoc):
        if seg.has_heredoc:
            for body in scan.heredocs:
                r = _strict_scan(body, inline=True)
                if r:
                    return f"heredoc fed to {name} contains {r}"
            return None
        return f"{name} reading a script from a pipe is denied"
    return None


def _analyze_text(text: str, ctx: _Ctx) -> Optional[str]:
    if len(text) > MAX_COMMAND_CHARS:
        return "command too long to inspect"
    scan = _prescan(text)
    for sub in scan.substitutions:
        r = _analyze_text(sub, ctx)
        if r:
            return f"in command substitution: {r}"
    tokens = _tokenize(scan.text)
    for seg in _segments(tokens):
        r = _analyze_segment(seg, scan, ctx)
        if r:
            return r
    return None


def analyze_command(command: str, workdir: Optional[str] = None) -> Optional[str]:
    """Return a block reason for a shell command, or None if it passes."""
    if not isinstance(command, str):
        return "command is not a string"
    if not command.strip():
        return None
    ctx = _Ctx(workdir if isinstance(workdir, str) and workdir else None)
    try:
        return _analyze_text(command, ctx)
    except GuardError as exc:
        return f"unparseable command ({exc})"
    except RecursionError:
        return "command nesting too deep"


# --------------------------------------------------------------------------------------
# Tool-call entry point
# --------------------------------------------------------------------------------------

def check_tool_call(tool_name: str, args) -> Optional[str]:
    """Return a block reason or None. Never raises."""
    try:
        if not isinstance(args, dict):
            args = {}
        if tool_name == "terminal":
            return analyze_command(args.get("command"), args.get("workdir"))
        if tool_name == "execute_code":
            return ("execute_code is disabled by git-guard: sandboxed Python can spawn processes that bypass the "
                    "terminal guard. Use the terminal tool.")
        if tool_name in {"write_file", "patch"}:
            p = args.get("path")
            if not isinstance(p, str):
                return "path is missing"
            if _is_protected_path(p):
                return f"{tool_name} to protected path {p!r} is denied"
            return None
        if tool_name == "skill_manage":
            p = args.get("file_path")
            if isinstance(p, str) and _is_protected_path(p):
                return f"skill_manage file_path {p!r} is protected"
            for key in ("content", "file_content", "new_string"):
                v = args.get(key)
                if isinstance(v, str):
                    r = _strict_scan(v, inline=False)
                    if r:
                        return f"skill content contains {r}"
            return None
        if tool_name == "process_manage":
            for key, v in args.items():
                if isinstance(v, str) and key not in {"session_id", "action"}:
                    r = analyze_command(v)
                    if r and r.startswith("unparseable"):
                        continue
                    if r:
                        return f"process_manage {key}: {r}"
            return None
        return None
    except Exception as exc:  # noqa: BLE001 - fail closed on anything
        return f"git-guard internal error ({type(exc).__name__}: {exc}); failing closed"

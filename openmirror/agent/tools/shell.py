"""Running commands on the machine.

The risk classifier below is the part that matters. It exists so that the
approval prompt means something: if every command needs a human, the human
stops reading, and the one command that deletes their home directory gets the
same reflexive yes as the forty `git status` calls before it. So a read is a
read, and `rm -rf` is never anything but destructive.

It is a heuristic and it is deliberately biased. Anything it cannot parse
confidently is escalated, never waved through — a command it does not
understand is exactly the sort it should be asking about. It is not a
sandbox, and it is not a substitute for one: it decides what to *ask*, and
containment is the container's job.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import sys
from typing import Any

from openmirror.agent.tools.base import Assessment, Output, Tool, ToolContext, ToolError, truncate
from openmirror.protocol.agent import Risk

WINDOWS = sys.platform == 'win32'


def _spawn_kwargs() -> dict[str, Any]:
    """Start the command in its own group, whatever the platform calls that.

    The point is the same on both: a timeout or an interrupt has to reach the
    command's children, not just the shell that launched them. Killing only
    the shell leaves an orphaned dev server holding its port, which is the
    failure people actually hit.
    """
    if WINDOWS:
        # CREATE_NEW_PROCESS_GROUP is the closest Windows analogue, and the
        # flag `taskkill /T` needs to walk the tree later.
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    # POSIX: setsid, so the whole group can be signalled by negative pid.
    return {'start_new_session': True}


async def _kill_tree(proc: Any, hard: bool = True) -> None:
    """Kill a process and everything it started.

    Windows has no process groups in the POSIX sense and no SIGTERM, so the
    only reliable way to get the children is to shell out to taskkill. It is
    ugly and it is what works.
    """
    if proc.returncode is not None:
        return
    if WINDOWS:
        try:
            killer = await asyncio.create_subprocess_exec(
                'taskkill', '/F', '/T', '/PID', str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=10)
        except (TimeoutError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return

    try:
        os.killpg(os.getpgid(proc.pid), 9 if hard else 15)
    except (ProcessLookupError, PermissionError):
        pass

# Commands that only look. Anything not on this list is at least `execute`.
READ_ONLY = {
    'ls', 'cat', 'head', 'tail', 'wc', 'file', 'stat', 'pwd', 'whoami', 'date',
    'echo', 'printf', 'which', 'type', 'env', 'printenv', 'uname', 'hostname',
    'df', 'du', 'free', 'ps', 'top', 'uptime', 'id', 'groups', 'tree',
    'grep', 'egrep', 'fgrep', 'rg', 'ag', 'find', 'fd', 'locate',
    'diff', 'cmp', 'md5sum', 'sha256sum', 'basename', 'dirname', 'realpath',
    'sort', 'uniq', 'cut', 'awk', 'sed', 'jq', 'column', 'nl', 'tr',
    # Windows and PowerShell equivalents. Without these every `dir` on Windows
    # asks for approval, and a prompt that fires constantly is a prompt nobody
    # reads — which is the whole failure this classifier exists to avoid.
    'dir', 'where', 'findstr', 'more', 'tasklist', 'systeminfo', 'ver',
    'get-content', 'get-childitem', 'get-location', 'get-process', 'get-item',
    'select-string', 'measure-object', 'select-object', 'sort-object',
    'format-list', 'format-table', 'out-string', 'write-output', 'write-host',
    'test-path', 'get-date', 'get-help', 'gci', 'gc',
}

# Read-only subcommands of tools that are otherwise not read-only.
READ_ONLY_SUB = {
    'git': {'status', 'log', 'diff', 'show', 'branch', 'remote', 'blame', 'describe', 'ls-files', 'rev-parse'},
    'docker': {'ps', 'images', 'logs', 'inspect', 'version'},
    'podman': {'ps', 'images', 'logs', 'inspect', 'version'},
    'npm': {'ls', 'list', 'view', 'outdated'},
    'kubectl': {'get', 'describe', 'logs', 'version'},
    'systemctl': {'status', 'show', 'list-units', 'is-active', 'is-enabled'},
}

# Matched against the whole command line, after the token scan. These are the
# ones that are worth being blunt about.
#
# Both shells are here regardless of platform, and deliberately. `bash` on
# Windows is a real thing (Git Bash, WSL) and PowerShell runs on Linux and
# macOS; matching only the host's native shell would leave a hole exactly
# where somebody is being clever.
DESTRUCTIVE_PATTERNS = [
    (re.compile(r'\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+'), 'recursive or forced delete'),
    (re.compile(r'\bdd\s+.*\bof=/dev/'), 'writing directly to a device'),
    (re.compile(r'\bmkfs(\.\w+)?\b'), 'formatting a filesystem'),
    (re.compile(r'>\s*/dev/(sd|nvme|hd|vd)'), 'writing to a block device'),
    (re.compile(r'\bgit\s+push\b.*(--force\b|--force-with-lease\b|\s-f\b)'), 'force push'),
    (re.compile(r'\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f)'), 'discarding uncommitted work'),
    (re.compile(r'\b(shutdown|reboot|halt|poweroff)\b'), 'powering the machine down'),
    (re.compile(r'\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b', re.I), 'dropping a database object'),
    (re.compile(r'\bchmod\s+(-R\s+)?777\b'), 'making files world-writable'),
    (re.compile(r'\b(kill|pkill|killall)\s+(-9|-KILL)\b'), 'force-killing processes'),
    (re.compile(r':\(\)\s*\{.*\|.*&.*\}\s*;?\s*:'), 'fork bomb'),
    (re.compile(r'\bcrontab\s+-r\b'), 'deleting the crontab'),

    # --- Windows: cmd.exe ---
    (re.compile(r'\bdel\s+(?:/[a-z]\s+)*/[sq]\b', re.I), 'recursive delete'),
    (re.compile(r'\b(?:rd|rmdir)\s+(?:/[a-z]\s+)*/s\b', re.I), 'recursive directory delete'),
    (re.compile(r'\bformat\s+[a-z]:', re.I), 'formatting a drive'),
    (re.compile(r'\bdiskpart\b', re.I), 'partitioning a disk'),
    (re.compile(r'\bvssadmin\s+delete\s+shadows', re.I), 'deleting shadow copies'),
    (re.compile(r'\bcipher\s+/w', re.I), 'wiping free space'),
    (re.compile(r'\breg\s+delete\b', re.I), 'deleting a registry key'),
    (re.compile(r'\bbcdedit\b', re.I), 'changing the boot configuration'),
    (re.compile(r'\bshutdown\s+/[rs]\b', re.I), 'shutting the machine down'),
    (re.compile(r'\btaskkill\s+.*\/f\b', re.I), 'force-killing processes'),

    # --- Windows: PowerShell ---
    (re.compile(r'\bRemove-Item\b.*-(?:Recurse|Force)\b', re.I), 'recursive or forced delete'),
    (re.compile(r'\bRemove-Item\b.*\*', re.I), 'wildcard delete'),
    (re.compile(r'\b(?:Format-Volume|Clear-Disk|Initialize-Disk)\b', re.I), 'formatting a disk'),
    (re.compile(r'\bStop-Computer\b|\bRestart-Computer\b', re.I), 'powering the machine down'),
    (re.compile(r'\bRemove-ItemProperty\b|\bRemove-LocalUser\b', re.I), 'removing a registry value or user'),
    (re.compile(r'\bSet-ExecutionPolicy\s+(?:Unrestricted|Bypass)\b', re.I), 'disabling script signing checks'),
    (re.compile(r'\bInvoke-Expression\b|\biex\b', re.I), 'executing a constructed string'),
]

NETWORK_COMMANDS = {
    'curl', 'wget', 'ssh', 'scp', 'rsync', 'sftp', 'ftp', 'nc', 'ncat', 'telnet',
    'pip', 'pip3', 'npm', 'npx', 'yarn', 'pnpm', 'apt', 'apt-get', 'dnf', 'yum',
    'brew', 'cargo', 'go', 'gem', 'composer', 'uv', 'poetry',
    'winget', 'choco', 'scoop', 'nuget',
    'invoke-webrequest', 'invoke-restmethod', 'iwr', 'curl.exe', 'start-bitstransfer',
}

# Spending money, seen from a command line.
#
# The browser tool grades "Book this room — pay now" as a purchase, and
# `ApprovalPolicy` never lets a purchase through without asking. The shell was
# the way round that: `curl -X POST https://api.stripe.com/v1/charges -d
# amount=5000` graded as plain network traffic, and `trusted` mode allows
# network traffic without asking. Same money, same consequence, no prompt —
# reached by the one tool that can do anything the others cannot.
#
# **This is string matching, and that is the honest limit of it.** A payment
# script can be named anything and compute the URL at runtime, and nothing here
# can follow it. What this buys is that the *obvious* routes stop being free:
# the agent has to work at being subtle to spend without asking, rather than
# reaching for the tool it was told to prefer. `OPAQUE` already grades a
# runtime-built command as EXECUTE for the same reason.
#
# Hostnames rather than keywords wherever possible — `api.stripe.com` is a
# payment API, `stripe` alone is a library name and `npm install stripe` is
# not a purchase.
PAYMENT_HOSTS = (
    'stripe.com',
    'api.stripe.com',
    'paypal.com',
    'api.paypal.com',
    'checkout.stripe.com',
    'squareup.com',
    'api.squareup.com',
    'checkout.squareup.com',
    'braintreegateway.com',
    'adyen.com',
    'checkout.adyen.com',
    'klarna.com',
    'checkout.klarna.com',
    'razorpay.com',
    'api.razorpay.com',
    'chargebee.com',
    'api.chargebee.com',
    'paddle.com',
    'api.paddle.com',
    'lemonsqueezy.com',
    'gocardless.com',
    # Booking, travel and ticketing: these sell, which is the same act.
    'stripe.cn',
    'weixin.qq.com',
    'alipay.com',
    'openapi.alipay.com',
    'smartling.com',
    'sabre.com',
    'amadeus.com',
    'booking.com',
    'hotels.com',
    'expedia.com',
    'eventbrite.com',
    'ticketmaster.com',
    # Cloud spend. A `gcloud`/`aws`/`az` command that creates a billable
    # resource is a purchase, and it is a purchase nobody thinks of as one.
    'compute.googleapis.com',
    'billing.googleapis.com',
    'ec2.amazonaws.com',
    'sts.amazonaws.com',
    'management.azure.com',
)

# HTTP methods that mean "this changes something". Not money on their own —
# they are what turns a payment URL from a documentation lookup into a write.
#
# The separator is explicit because curl accepts both `-XPOST` and `-X POST`,
# and the flag may carry a quoted value. `\s*` alone would also match the `-X`
# out of `-XPOST` and then read `POST` as a separate token, which happens to
# work for one spelling and silently misses the other.
PAYMENT_METHODS = re.compile(
    r'(?:-X|--request)\s*[\'\"]?(POST|PUT|PATCH|DELETE)\b', re.I
)

# Providers whose *own CLIs* bill. `gh` is not here — it is authenticated
# already and its actions are not purchases.
PAYMENT_CLIS = {
    'stripe',
    'gcloud',
    'gsutil',
    'bq',
    'az',
    'aws',
    'wrangler',
    'flyctl',
    'fly',
    'doctl',
    'heroku',
    'vercel',
    'netlify',
    'railway',
    'terraform',
    'pulumi',
}

# Flag names that mean a charge, on a CLI we do not otherwise recognise.
PAYMENT_FLAGS = re.compile(
    r'--(?:premium|upgrade|billing|plan|subscription|pro)\b'
    r'|\binstances\s+create\b'
    r'|\bserverless\s+deploy\b',
    re.I,
)

# Subcommands of a billing CLI that change nothing, and so cost nothing.
#
# Matched against *any* word of the subcommand rather than a single resolved
# "verb", because the position of the action is not fixed: `gcloud compute
# instances list` ends in the verb, `aws ec2 describe-instances` is one
# hyphenated token, and `gcloud compute instances create box` ends in a
# resource name. Scanning for a known action is the only reading that holds
# for all three.
_CLI_READ_ONLY = re.compile(
    r'^(?:list|ls|describe|get|show|status|version|config|help|info|'
    r'validate|plan|init|whoami|account|session|logs|history|diff|listen)'
    r'(?:-|$)',
    re.I,
)

# And the ones that do bill. Checked first, and separately, so that a command
# mixing both — `gcloud compute instances delete` has `instances` and `list`
# nowhere but `terraform plan apply` is a real plan *and* a real apply — is
# graded as spending.
_CLI_SPENDS = re.compile(
    r'^(?:create|run|start|launch|delete|update|set|put|post|patch|apply|'
    r'deploy|provision|attach|detach|import|restore|rollback|copy|move|'
    r'scale|resize|terminate|stop|add|remove|make|build|publish|release|'
    r'subscribe|upgrade|enable|install|register|purchase|order|buy|'
    r'checkout|pay|activate)(?:-|$)',
    re.I,
)

# Reading a provider's documentation is not paying them. `-I`/`--head` and the
# silent flags say so outright.
#
# Anchored on whitespace rather than `\b`: in `curl -d x`, the `-` and the
# space either side are both non-word characters, so a word boundary is never
# there to match and `\b-d\b` silently never fires. The trailing lookahead
# stops `-d` matching inside `--delete` or `-directory`.
_HTTP_READ_ONLY = re.compile(
    r'(?:^|\s)(?:-I|--head|-O|-J|-s|-S|--silent)(?=\s|$|=)', re.I
)

# Flags that make a `curl` write. Without one of these it is a GET, whatever
# the URL looks like.
_HTTP_WRITE_FLAG = re.compile(
    r'(?:^|\s)(?:-X|--request|-d|--data|--data-raw|--data-binary|-F|--form'
    r'|-T|--upload-file|--json)(?=\s|$|=)',
    re.I,
)

# A URL that *is* the purchase: a hosted checkout page, a payment link. Fetching
# one of these is the closest thing on the wire to tapping "Pay", and it is
# also the easiest purchase to run by accident — `curl <link>` in a log, a
# paste in chat, a link preview that fetches on its own.
_PAYMENT_PAGE = re.compile(
    r'(?:^|[./])(?:checkout|payment|pay|invoice|order|billing)(?:[./?#]|$)',
    re.I,
)

# Where the payment APIs keep their documentation, which is the one place
# nobody is spending money by reading it. Narrow on purpose: `docs.` and a
# `/docs` path are documentation markers, whereas a bare `api` substring is
# not, because `/api/checkout` is where the real endpoints live.
_DOCS_HINT = re.compile(
    r'(?:^|[./])(?:docs|developers?|reference|guides)(?:[./?#-]|$)'
    r'|/(?:docs|developers?)/',
    re.I,
)


def _spends_money(command: str) -> str | None:
    """Why this command line looks like it spends money, or None.

    A `why` and not a bool, because the approval prompt shows it and "this
    spends money" with no more detail is a prompt nobody can act on — the
    person being asked has no way to tell which of three charges it is.
    """
    lowered = command.lower()
    is_fetch = bool(re.search(r'\b(?:curl|wget|invoke-webrequest|iwr|invoke-restmethod)\b', command, re.I))

    # A fetch that is plainly a read is documentation. The hostnames below are
    # public, and somebody reading Stripe's docs to work out how to integrate
    # it should not be asked to approve a purchase every time they do.
    # `-d` is a write (curl infers POST from it), so it is not a read.
    #
    # A checkout link is the exception even when it is a bare GET: reading a
    # docs page and opening someone's payment link differ only in the path, and
    # the path is the part that spends. A `/docs` path is exempt even there,
    # because Stripe's own checkout reference lives under that URL too.
    if is_fetch and not _HTTP_WRITE_FLAG.search(command):
        if _PAYMENT_PAGE.search(command) and not _DOCS_HINT.search(command):
            return 'a payment link'
        if _HTTP_READ_ONLY.search(command) or not PAYMENT_METHODS.search(command):
            return None

    for host in PAYMENT_HOSTS:
        if host in lowered:
            return f'names {host}, a payment or booking API'

    # A POST to anything is worth a look, but only alongside something that
    # makes it a charge: an agent POSTing to its own API should not be stopped
    # every time.
    for pattern, why in (
        (re.compile(r'\b(?:checkout|pay|charge|billing|invoice|subscription)\b[^\s]*=?\d', re.I),
         'a payment parameter'),
        (re.compile(r'\b(?:amount|price|total|charge_amt|invoice_total)\s*[=:]\s*[\d.]+', re.I),
         'an explicit amount'),
        (re.compile(r'\bstripe\b[^\n]*(?:token|charge|intent|price_)\b', re.I),
         'a Stripe object'),
    ):
        if pattern.search(command):
            return why

    if PAYMENT_METHODS.search(command) and re.search(r'(?:amount|price|total|charge|invoice)\s*[=:]', command, re.I):
        return 'a write to a payment API'

    for seg in _segments_or_none(command):
        name = _command_name(seg[0]) if seg else ''
        if name in PAYMENT_CLIS:
            if _cli_is_read_only(seg):
                continue
            return f'`{name}` bills for what it creates'
        if len(seg) > 1 and PAYMENT_FLAGS.search(' '.join(seg[1:])):
            return f'`{name}` with a paid option'

    return None


def _segments_or_none(command: str) -> list[list[str]]:
    try:
        return _segments(command)
    except ToolError:
        return []


def _cli_is_read_only(seg: list[str]) -> bool:
    """Whether this billing-CLI invocation is one that cannot cost anything.

    Takes the raw segment rather than the name- and flag-stripped view,
    because `_command_name` is deliberately for finding the *program*: it
    returns the last token before a `--` terminator, which is `POST` in
    `curl -X POST ...` and the wrong word entirely for deciding whether a
    cloud CLI is being asked to create something.
    """
    # Long flags carry the verb as often as the subcommand does: `s3 ls
    # --delete` reads like a listing and destroys the bucket, and the flag is
    # the only place the deletion is named. So flags are kept for the spend
    # test and dropped for the read test, rather than dropped for both.
    flags = [w for w in seg[1:] if w.startswith('-')]
    words = [w for w in seg[1:] if not w.startswith('-')]
    # A long flag is `--delete`; the verb inside it is `delete`.
    verbs_in_flags = [f.lstrip('-') for f in flags]
    if not words and not any(_CLI_SPENDS.match(v) for v in verbs_in_flags):
        # No subcommand at all — `stripe --version`, `az account` with nothing
        # after it. Flags on their own cannot create anything billable.
        return True
    # A known action beats everything else. An unrecognised one falls through
    # to "spends", which is the safe direction to be wrong in: the cost of
    # asking about a read is one prompt, and the cost of not asking about a
    # write is somebody's bill.
    if any(_CLI_SPENDS.match(w) for w in words + verbs_in_flags):
        return False
    if any(_CLI_READ_ONLY.match(w) for w in words):
        return True
    return False


# Shell metacharacters that make a token scan unreliable, because what runs is
# decided at runtime rather than visible in the text.
OPAQUE = re.compile(r'\$\(|`|\beval\b|\bexec\b|\|\s*(sh|bash|zsh)\b')


def _command_name(token: str) -> str:
    """The comparable name of a command, across shells.

    Two normalisations, both narrow on purpose:

    `.exe`, `.cmd` and `.bat` are stripped, so `curl.exe` is `curl`.

    A hyphenated token is lower-cased, because that is the shape of a
    PowerShell cmdlet and PowerShell is case-insensitive — `Get-ChildItem` and
    `get-childitem` are one command. POSIX command names are *not*
    case-insensitive, so everything else keeps its case: lower-casing `LS`
    into `ls` would grade an unknown binary as a known-safe read, which is the
    one direction this must never fail in.
    """
    name = os.path.basename(token)
    for suffix in ('.exe', '.cmd', '.bat', '.ps1'):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.lower() if '-' in name else name


def _segments(command: str) -> list[list[str]]:
    """Split a command line into the individual commands it will run.

    Best-effort by construction: `shlex` understands quoting but not shell
    grammar, so a line it cannot lex is reported as unparseable and the caller
    escalates rather than guessing.
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError as exc:
        raise ToolError(f'unbalanced quoting: {exc}') from exc

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in ('&&', '||', ';', '|', '&'):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


# Paths that are worth escalating for on sight, because a command touching
# them is leaving the working directory whatever else it is doing.
_PATH_LIKE = re.compile(r'(?:^|[\s=\'"(])((?:~|/|\.\./)[^\s\'"();|&]*)')


def paths_outside(command: str, ctx: object) -> list[str]:
    """Absolute or parent-relative paths in a command that leave the root.

    The shell cannot be confined — what a command touches is decided at
    runtime, and no amount of string inspection changes that. What it *can*
    do is stop grading `cat /etc/shadow` as a plain read, so that under a
    policy that auto-runs reads it still stops and asks.

    Real containment is the OS's job: a container, a namespace, seccomp.
    This is honesty about the boundary, not enforcement of it.
    """
    from openmirror.agent.tools.base import escapes_root

    found = []
    for match in _PATH_LIKE.finditer(command):
        candidate = match.group(1)
        if escapes_root(candidate, ctx):
            found.append(candidate)
    return found


def classify(command: str) -> tuple[Risk, str]:
    """The risk of this specific command line, and why."""
    for pattern, why in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return Risk.DESTRUCTIVE, why

    if OPAQUE.search(command):
        # Command substitution, eval, or a pipe into a shell: what actually
        # runs is not in the text, so no token scan can be trusted.
        return Risk.EXECUTE, 'builds the command at runtime'

    # Before the network check, deliberately. A Stripe charge *is* a network
    # call, and grading it as one put it in the set `trusted` runs without
    # asking — so the one tool that can do anything could spend money silently
    # while every other route to spending money asked first.
    paid = _spends_money(command)
    if paid:
        return Risk.PURCHASE, paid

    try:
        segments = _segments(command)
    except ToolError:
        return Risk.EXECUTE, 'could not be parsed'

    if not segments:
        return Risk.READ, 'empty'

    risk = Risk.READ
    why = 'reads only'

    for seg in segments:
        # Skip a leading VAR=value assignment so `FOO=1 ls` still reads as ls.
        idx = 0
        while idx < len(seg) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', seg[idx]):
            idx += 1
        if idx >= len(seg):
            continue

        argv = seg[idx:]
        name = _command_name(argv[0])
        if name in ('sudo', 'doas', 'env', 'nice', 'nohup', 'time', 'xargs') and len(argv) > 1:
            # sudo raises the floor: whatever follows now runs as root.
            escalated = name in ('sudo', 'doas')
            argv = argv[1:]
            name = _command_name(argv[0])
            if escalated and risk is Risk.READ:
                risk, why = Risk.EXECUTE, 'runs as root'

        if name in NETWORK_COMMANDS:
            if risk in (Risk.READ, Risk.EXECUTE):
                risk, why = Risk.NETWORK, f'{name} reaches the network'
            continue

        sub = READ_ONLY_SUB.get(name)
        if sub is not None:
            args = [a for a in argv[1:] if not a.startswith('-')]
            if args and args[0] in sub:
                continue
            if risk is Risk.READ:
                risk, why = Risk.EXECUTE, f'{name} {args[0] if args else ""}'.strip()
            continue

        if name in READ_ONLY:
            # A read-only command with its output redirected is a write.
            continue

        if risk is Risk.READ:
            risk, why = Risk.EXECUTE, f'runs {name}'

    # Redirection writes files whatever the command was.
    if re.search(r'(?<![0-9<>])>{1,2}(?!&)', command) and risk is Risk.READ:
        risk, why = Risk.WRITE, 'redirects output to a file'

    return risk, why


class ShellTool(Tool):
    name = 'shell'
    description = (
        'Run a shell command on the machine and return its output. '
        'Use this for builds, tests, git, package managers and anything else with a CLI. '
        'Prefer the dedicated file tools for reading and editing files: they are cheaper and '
        'their results are easier to act on.'
    )
    input_schema = {
        'type': 'object',
        'properties': {
            'command': {'type': 'string', 'description': 'The command line to run.'},
            'cwd': {'type': 'string', 'description': 'Working directory, relative to the session root.'},
            'timeout': {
                'type': 'integer',
                'description': 'Seconds before the command is killed. Default 120, maximum 1800.',
            },
            'background': {
                'type': 'boolean',
                'description': (
                    'Start it and return at once, for something that keeps running — a dev server, '
                    'a watcher, a long build. Read what it prints with the tasks tool; you are told '
                    'when it finishes.'
                ),
            },
        },
        'required': ['command'],
    }

    def __init__(self, default_timeout: int = 120, max_output: int = 30_000) -> None:
        self.default_timeout = default_timeout
        self.max_output = max_output

    def assess(self, args: dict[str, Any], ctx: ToolContext) -> Assessment:
        command = (args.get('command') or '').strip()
        if not command:
            return Assessment(risk=Risk.READ, summary='', invalid='command is required')

        risk, why = classify(command)

        # A command naming a path outside the root is never a plain read,
        # however innocent the verb. Without this, `cat /etc/shadow` grades as
        # `read` and runs silently under the default policy — the file tools
        # would have refused the same path outright.
        if risk in (Risk.READ, Risk.WRITE):
            outside = paths_outside(command, ctx)
            if outside:
                risk = Risk.EXECUTE
                why = f'reaches outside the working root ({outside[0]})'

        # One line, the command itself first, because that is what a person
        # reads. The reason follows for the cases where it is not obvious.
        #
        # Backgrounding changes nothing about the grade: a command is as
        # dangerous left running as waited for, and more so unwatched.
        shown = command if len(command) <= 120 else command[:117] + '...'
        if args.get('background'):
            why = f'{why}; left running in the background' if risk is not Risk.READ else 'left running in the background'
            return Assessment(risk=risk, summary=f'{shown}   ({why})')
        return Assessment(risk=risk, summary=shown if risk is Risk.READ else f'{shown}   ({why})')

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> Output:
        from openmirror.agent.tools.base import resolve_in_root

        command = args['command']
        cwd = resolve_in_root(args['cwd'], ctx) if args.get('cwd') else ctx.cwd
        if not cwd.is_dir():
            raise ToolError(f'{cwd}: not a directory')

        if args.get('background'):
            if ctx.tasks is None:
                raise ToolError(
                    'nothing can be left running from here — run it in the foreground, with a timeout'
                )
            task = await ctx.tasks.start_shell(command, cwd, {**os.environ, **ctx.env})
            return Output(
                content=(
                    f'Started in the background as {task.id}, and still running. Read what it prints '
                    f'with tasks (action "output", id "{task.id}"); you will be told when it finishes.'
                ),
                display={'command': command, 'cwd': str(cwd), 'background': True, 'task': task.id},
            )

        timeout = min(int(args.get('timeout') or self.default_timeout), 1800)

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env={**os.environ, **ctx.env},
            **_spawn_kwargs(),
        )

        chunks: list[str] = []
        size = 0


        async def pump(stream: asyncio.StreamReader, which: str) -> None:
            nonlocal size
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode('utf-8', 'replace')
                # Streamed to the watcher in full; kept for the model only up
                # to the cap, so a runaway loop cannot exhaust the context.
                await ctx.emit(text, which)
                if size < self.max_output * 2:
                    chunks.append(text)
                    size += len(text)

        pumps = asyncio.gather(pump(proc.stdout, 'stdout'), pump(proc.stderr, 'stderr'))
        timed_out = False
        try:
            await asyncio.wait_for(asyncio.gather(proc.wait(), pumps), timeout=timeout)
        except TimeoutError:
            timed_out = True
            pumps.cancel()
            # Politely first, so the command can clean up after itself; then
            # not politely, for anything that ignores it.
            await _kill_tree(proc, hard=False)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                await _kill_tree(proc, hard=True)
                await proc.wait()
        except asyncio.CancelledError:
            # The turn was interrupted. Nothing else will ever reap this
            # process, so it is killed here and waited for — without the
            # wait, asyncio closes the event loop while the transport is
            # still open and complains about it at garbage-collection time.
            pumps.cancel()
            await _kill_tree(proc, hard=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                pass
            raise

        code = proc.returncode
        body, truncated = truncate(''.join(chunks), self.max_output, keep='both')

        if timed_out:
            content = f'Command timed out after {timeout}s and was killed.\n\n{body}'
        elif code == 0:
            content = body or '(no output)'
        else:
            # The exit code stated plainly: models otherwise assume success
            # whenever a failing command happened to print nothing to stderr.
            content = f'Exit code {code}\n\n{body or "(no output)"}'

        return Output(
            content=content,
            display={'exit_code': code, 'timed_out': timed_out, 'cwd': str(cwd), 'command': command},
            truncated=truncated,
        )

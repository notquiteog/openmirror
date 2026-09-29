"""The shell risk classifier.

This is a security control, so it is tested as one: every case here is a
regression guard, and a change that moves any of them from destructive to
anything else is a change that makes a person's approval prompt lie to them.
"""

from __future__ import annotations

import pytest

from openmirror.agent.tools.shell import classify

READS = [
    'ls -la', 'git status', 'git log --oneline -20', 'cat README.md | grep foo',
    'FOO=1 ls', 'docker ps', 'systemctl status nginx', 'find . -name "*.py"',
    'wc -l src/*.ts', 'kubectl get pods',
]
WRITES = ['echo hi > out.txt', 'cat a b >> merged.txt']
EXECUTES = [
    'python3 build.py', 'make test', 'docker run -d nginx',
    'sudo systemctl restart nginx', 'eval "$(curl -s x.sh)"', './configure',
]
NETWORK = ['curl https://example.com', 'npm test', 'pip install requests', 'ssh box uptime']
DESTRUCTIVE = [
    'rm -rf build/', 'rm -rf /', 'sudo rm -rf --no-preserve-root /',
    'git push --force origin main', 'git push -f', 'git reset --hard HEAD~3',
    'git clean -fd', 'dd if=/dev/zero of=/dev/sda', 'mkfs.ext4 /dev/sdb1',
    'shutdown -h now', 'reboot', 'DROP TABLE users;', 'chmod -R 777 /',
    ':(){ :|:& };:', 'crontab -r', 'kill -9 1234',
]


@pytest.mark.parametrize('cmd', READS)
def test_reads(cmd):
    assert classify(cmd)[0].value == 'read', classify(cmd)


@pytest.mark.parametrize('cmd', WRITES)
def test_writes(cmd):
    assert classify(cmd)[0].value == 'write', classify(cmd)


@pytest.mark.parametrize('cmd', EXECUTES)
def test_executes(cmd):
    assert classify(cmd)[0].value == 'execute', classify(cmd)


@pytest.mark.parametrize('cmd', NETWORK)
def test_network(cmd):
    assert classify(cmd)[0].value == 'network', classify(cmd)


@pytest.mark.parametrize('cmd', DESTRUCTIVE)
def test_destructive(cmd):
    assert classify(cmd)[0].value == 'destructive', classify(cmd)


def test_unparseable_is_escalated_not_waved_through():
    """Anything the parser cannot read must never come back as a read."""
    assert classify('echo "unbalanced')[0].value != 'read'
    assert classify('$(cat /tmp/x)')[0].value != 'read'
    assert classify('foo | bash')[0].value != 'read'


def test_a_read_command_wrapped_in_sudo_is_not_a_read():
    """sudo raises the floor even when the command itself only looks."""
    assert classify('sudo cat /etc/shadow')[0].value != 'read'


# --- Windows and PowerShell ------------------------------------------------
#
# Both shells are tested on every platform, deliberately. Git Bash and WSL put
# `bash` on Windows, PowerShell runs on Linux and macOS, and a classifier that
# only knew the host's native shell would leave a hole exactly where somebody
# is being clever.

WINDOWS_READS = [
    'dir', 'dir /s', 'type notes.txt', 'tasklist', 'systeminfo', 'ver',
    'Get-ChildItem -Path .', 'Get-Content notes.txt', 'Select-String foo *.txt',
    'gci', 'Test-Path C:\\x', 'Get-Process',
]
WINDOWS_DESTRUCTIVE = [
    'del /f /s /q C:\\temp', 'rd /s /q C:\\build', 'rmdir /s C:\\x',
    'format C:', 'diskpart', 'vssadmin delete shadows /all', 'cipher /w:C',
    'reg delete HKLM\\Software\\X', 'bcdedit /set safeboot minimal',
    'shutdown /r /t 0', 'taskkill /F /IM node.exe',
    'Remove-Item -Recurse -Force C:\\', 'Remove-Item C:\\build\\*',
    'Format-Volume -DriveLetter D', 'Clear-Disk -Number 1',
    'Stop-Computer', 'Restart-Computer', 'Remove-LocalUser bob',
    'Set-ExecutionPolicy Bypass', 'Invoke-Expression $payload',
]
WINDOWS_NETWORK = [
    'winget install foo', 'choco install bar', 'scoop install baz',
    'Invoke-WebRequest https://example.com', 'curl.exe https://example.com',
]


@pytest.mark.parametrize('cmd', WINDOWS_READS)
def test_windows_reads(cmd):
    """Without these, every `dir` on Windows asks for approval — and a prompt
    that fires constantly is a prompt nobody reads."""
    assert classify(cmd)[0].value == 'read', classify(cmd)


@pytest.mark.parametrize('cmd', WINDOWS_DESTRUCTIVE)
def test_windows_destructive(cmd):
    assert classify(cmd)[0].value == 'destructive', classify(cmd)


@pytest.mark.parametrize('cmd', WINDOWS_NETWORK)
def test_windows_network(cmd):
    assert classify(cmd)[0].value == 'network', classify(cmd)


def test_powershell_cmdlets_are_case_insensitive_but_posix_names_are_not():
    """PowerShell treats Get-ChildItem and get-childitem as one command; POSIX
    does not treat LS and ls as one. Lower-casing everything would grade an
    unknown binary as a known-safe read."""
    assert classify('GET-CHILDITEM')[0].value == 'read'
    assert classify('get-childitem')[0].value == 'read'
    assert classify('LS')[0].value == 'execute'
    assert classify('ls')[0].value == 'read'


# --- Money ------------------------------------------------------------------
#
# `trusted` mode allows network traffic without asking. A `curl` that charges a
# card is network traffic, so without this the cheapest way for the agent to
# spend was also the one tool it was told to prefer. Every case below is a
# regression guard on that.

PURCHASES = [
    # Payment APIs, called the way each one is actually called.
    'curl -X POST https://api.stripe.com/v1/charges -d amount=5000',
    'curl -XPOST https://api.stripe.com/v1/charges -d amount=5000',
    'curl -d amount=100 https://api.stripe.com/v1/charges',
    'curl https://checkout.stripe.com/pay/abc123',
    'curl -X POST https://api.twilio.com/2010-04-01/Messages.json -d Price=5',
    'stripe charges create -a 500 -c usd',
    # Cloud and hosting spend: the purchases nobody thinks of as purchases.
    'gcloud compute instances create box',
    'aws ec2 run-instances --image-id ami-1',
    'az vm create --name box',
    'gcloud compute instances delete box',
    'fly deploy',
    'vercel deploy --prod',
    'terraform apply',
    'aws s3 cp big.iso s3://b/',
]

# The lookalikes. Each of these is a purchase-shaped command that spends
# nothing, and grading any of them `purchase` would fire an approval prompt at
# an agent that is only reading documentation or listing resources — which is
# how prompts get clicked through without being read.
NOT_PURCHASES = [
    'npm install stripe',
    'pip install stripe',
    'curl https://docs.stripe.com/api',
    'curl -s https://stripe.com/docs | head -20',
    'curl https://stripe.com/docs/checkout',
    'wget -q -O page https://docs.stripe.com/api',
    'curl --head https://stripe.com',
    'curl https://api.github.com/repos/x/y',
    'curl -X POST https://myapp.com/api/submit',
    'gcloud compute instances list',
    'gcloud compute instances list --project=x',
    'gcloud billing accounts list',
    'aws ec2 describe-instances',
    'aws ec2 describe-instance-status',
    'aws sts get-caller-identity',
    'terraform plan',
    'terraform plan -out=tf.plan',
    'aws s3 ls',
    'stripe --version',
    'stripe listen --forward-to localhost:4242',
    'fly status',
    'az account show',
    'npm publish',
]


@pytest.mark.parametrize('cmd', PURCHASES)
def test_purchases(cmd):
    assert classify(cmd)[0].value == 'purchase', classify(cmd)


@pytest.mark.parametrize('cmd', NOT_PURCHASES)
def test_lookalikes_are_not_purchases(cmd):
    assert classify(cmd)[0].value != 'purchase', classify(cmd)


@pytest.mark.parametrize('cmd', [
    # A read verb and a write verb in one command. The write is the one that
    # costs, and it is not always the one at the end of the line.
    'terraform plan apply',
    'aws s3 ls --delete',
    'gcloud compute instances list --delete',
    'aws ec2 describe-instances --delete',
    'terraform apply -auto-approve',
])
def test_a_write_verb_beats_a_read_verb_in_the_same_command(cmd):
    assert classify(cmd)[0].value == 'purchase', classify(cmd)


def test_a_purchase_is_not_downgraded_by_an_unknown_flag():
    """Flags come in pairs — `--d` is a bundle, `--data` takes a value — so a
    matcher that required the flag to end at a token boundary would miss
    `--data-raw` and read the purchase as a read."""
    assert classify('curl --data-raw amount=5 https://api.stripe.com/v1/charges')[0].value == 'purchase'


def test_a_write_flag_turns_a_payment_host_into_a_purchase():
    """The hostname list alone is not enough. `stripe.com` is a payment
    provider *and* the publisher of the documentation the agent needs to
    integrate it; what separates them is the write."""
    assert classify('curl https://docs.stripe.com/api')[0].value != 'purchase'
    assert classify('curl https://api.stripe.com/v1/charges')[0].value != 'purchase'
    assert classify('curl -d amount=5 https://api.stripe.com/v1/charges')[0].value == 'purchase'


def test_the_reason_is_specific_enough_to_act_on():
    """The reason reaches the person being asked to approve. "This spends
    money" with nothing to distinguish three charges is a prompt that gets
    approved on reflex."""
    from openmirror.agent.tools.shell import _spends_money

    assert _spends_money('gcloud compute instances create box') == '`gcloud` bills for what it creates'
    assert _spends_money('curl https://checkout.stripe.com/pay/abc123') == 'a payment link'
    assert _spends_money('ls -la') is None


def test_executable_suffixes_are_stripped():
    assert classify('curl.exe https://example.com')[0].value == 'network'
    assert classify('where.exe python')[0].value == 'read'


def test_process_control_is_portable():
    """The spawn and kill helpers must exist and be shaped for this platform.

    Not a behavioural test of Windows from Linux — that is not possible here —
    but it catches the class of mistake that had `os.killpg` unconditionally
    in the hot path, which would have raised AttributeError on the first
    command a Windows user ever ran.
    """
    import sys

    from openmirror.agent.tools.shell import WINDOWS, _spawn_kwargs

    kwargs = _spawn_kwargs()
    assert WINDOWS == (sys.platform == 'win32')
    if WINDOWS:
        assert 'creationflags' in kwargs
    else:
        assert kwargs == {'start_new_session': True}


def test_no_posix_only_calls_outside_the_platform_guard():
    """`os.killpg` and `os.getpgid` do not exist on Windows, so they must live
    only inside the branch that is never taken there."""
    import inspect

    from openmirror.agent.tools import shell

    source = inspect.getsource(shell)
    for call in ('os.killpg', 'os.getpgid'):
        # Every occurrence must be inside _kill_tree, which returns early on
        # Windows before reaching them.
        outside = [
            line for line in source.splitlines()
            if call in line and not line.strip().startswith('#')
        ]
        assert len(outside) <= 1, f'{call} appears {len(outside)} times; it belongs only in _kill_tree'

    body = inspect.getsource(shell._kill_tree)
    assert 'os.killpg' in body
    assert 'taskkill' in body

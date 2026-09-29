"""Putting a session together.

One function, because assembling an agent is where the mistakes happen: a
session built with the wrong root, or with tools its policy will refuse
everything from, fails in ways that look like the model being stupid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openmirror.agent import prompt as prompt_mod
from openmirror.agent.approval import ApprovalPolicy, Mode
from openmirror.agent.session import AgentSession
from openmirror.agent.tools.ask import AskUserTool
from openmirror.agent.tools.base import FILE_WRITERS, Tool
from openmirror.agent.tools.code import ApplyPatchTool, MultiEditTool, OutlineTool, ReadFilesTool
from openmirror.agent.tools.files import EditTool, ListDirTool, ReadTool, WriteTool
from openmirror.agent.tools.git import GitTool
from openmirror.agent.tools.notebook import NotebookEditTool
from openmirror.agent.tools.planning import ProposePlanTool
from openmirror.agent.tools.search import GlobTool, GrepTool
from openmirror.agent.tools.shell import ShellTool
from openmirror.agent.tools.tasks import TasksTool
from openmirror.agent.tools.todo import TodoTool

# What each named toolset contains. Groups rather than individual names,
# because the choice a person actually makes is "this session is for browsing"
# rather than a list of eleven function names.
#
# This exists because of a measured failure, not a preference. gemma4:12b —
# a model this project explicitly targets — was given the full set of thirty
# and could not complete a five-step browser task: it opened the page, then
# lost the thread and reported that it had no way to browse. The same model,
# the same task, the same prompt, with only the browser tools: done in
# twenty-six seconds, correctly, first try. The tools were not the problem.
# The *list* was.
TOOLSETS: dict[str, tuple[str, ...]] = {
    'files': ('read_file', 'read_files', 'write_file', 'edit_file', 'multi_edit',
              'apply_patch', 'notebook_edit', 'outline', 'list_dir', 'glob', 'grep', 'lsp'),
    'shell': ('shell', 'tasks'),
    # Version control, and not inside `shell`: the grades differ, and a
    # project session is the one place where every turn starts by asking what
    # has changed.
    'git': ('git',),
    # Mail. Offered only when an account is configured, because a `mail` tool
    # on a machine with no mailbox is a tool that can only fail.
    'mail': ('mail',),
    'calendar': ('calendar',),
    'todo': ('todo',),
    'agents': ('agent',),
    'skills': ('skill',),
    'ask': ('ask_user',),
    'web': ('web_search', 'web_fetch', 'research'),
    'browser': ('browser_navigate', 'browser_read', 'browser_click', 'browser_type',
                'browser_screenshot', 'browser_hand_over'),
    'desktop': ('desktop_screenshot', 'desktop_click', 'desktop_type', 'desktop_key',
                'desktop_scroll'),
    'system': ('system_info', 'package_search', 'package_install', 'package_remove',
               'display_info', 'display_hdr'),
    'media': ('media_params', 'generate_image', 'generate_video', 'media_job', 'import_workflow'),
    'memory': ('remember', 'recall'),
}


def resolve_toolset(names: list[str]) -> set[str] | None:
    """Turn what a caller asked for into a set of tool names, or None for all.

    Unknown entries are kept as literal tool names rather than rejected: a
    caller who wants exactly `shell` and `read_file` should be able to say so
    without a group existing for it. `ask_user` is always in, whatever was
    asked for — a session that cannot ask a question is a session that guesses.
    """
    if not names:
        return None
    allowed: set[str] = set(TOOLSETS['ask'])
    for name in names:
        allowed.update(TOOLSETS.get(name, (name,)))
    return allowed


def default_tools() -> list[Tool]:
    return [
        ReadTool(),
        ReadFilesTool(),
        WriteTool(),
        EditTool(),
        MultiEditTool(),
        ApplyPatchTool(),
        NotebookEditTool(),
        OutlineTool(),
        ListDirTool(),
        GlobTool(),
        GrepTool(),
        ShellTool(),
        TasksTool(),
        GitTool(),
        TodoTool(),
        AskUserTool(),
    ]


def calendar_tools(cfg: Any) -> list[Tool]:
    """The `calendar` tool, when this install has somewhere to read one from.

    Offered whenever calendar support is on, rather than only when the folder
    has files in it: the folder is usually empty on a fresh install and gets
    its first calendar within a day, and a tool that appears the moment a file
    lands is a tool the model has never seen in a schema it was told about.
    """
    if not getattr(cfg, 'calendar_enabled', False):
        return []
    from openmirror.agent.tools.calendar import CalendarTool
    from openmirror.calendar.store import CalendarStore

    return [CalendarTool(CalendarStore(Path(getattr(cfg, 'calendar_dir', ''))))]


def mail_tools(cfg: Any) -> list[Tool]:
    """The `mail` tool, when this install has a mailbox to use it on.

    Gated on there actually being an account, which is the same rule the
    media and desktop tools follow: a tool that is offered and can only fail
    is worse than no tool, because the model will spend a turn discovering
    that and then tell the person it cannot read their mail.
    """
    if not getattr(cfg, 'mail_enabled', False):
        return []
    from openmirror.agent.tools.mail import MailTool
    from openmirror.mail.accounts import AccountStore, from_env

    path = Path(getattr(cfg, 'mail_accounts', ''))
    if not str(path):
        return []
    store = AccountStore(path)
    return [MailTool(store)] if from_env(store) else []


def web_tools(cfg: Any) -> list[Tool]:
    from openmirror.agent.tools.research import ResearchTool
    from openmirror.agent.tools.web import WebFetchTool, WebSearchTool

    fetch = WebFetchTool(allow_private=cfg.web_allow_private)
    search = WebSearchTool(
        backend=cfg.search_backend,
        api_key=cfg.search_key,
        base_url=cfg.search_url,
        engine=cfg.search_engine,
        headless=cfg.browser_headless,
    )
    # `research` is given the other two rather than its own: one set of keys,
    # one private-address guard, and no way for the composite to reach
    # somewhere the individual tools would refuse.
    return [fetch, search, ResearchTool(search, fetch)]


def mcp_tools(manager: Any) -> list[Tool]:
    from openmirror.agent.tools.mcp import mcp_tools as build

    return build(manager)


def system_tools() -> list[Tool]:
    from openmirror.agent.tools.system import system_tools as build

    return build()


def media_tools(service: Any) -> list[Tool]:
    from openmirror.agent.tools.media import media_tools as build

    return build(service)


def desktop_tools(stage: Any) -> list[Tool]:
    from openmirror.agent.tools.desktop import desktop_tools as build

    return build(stage)


def browser_tools(browser: Any) -> list[Tool]:
    from openmirror.agent.tools.browser import (
        BrowserAskHumanTool,
        BrowserClickTool,
        BrowserNavigateTool,
        BrowserReadTool,
        BrowserScreenshotTool,
        BrowserTypeTool,
    )

    return [
        BrowserNavigateTool(browser),
        BrowserReadTool(browser),
        BrowserClickTool(browser),
        BrowserTypeTool(browser),
        BrowserScreenshotTool(browser),
        BrowserAskHumanTool(browser),
    ]


def build_session(
    *,
    root: str | Path,
    provider: Any,
    model: str,
    mode: Mode | str = Mode.ASK,
    # How hard the model thinks: off … max, or None for its own default.
    effort: str | None = None,
    tools: list[Tool] | None = None,
    session_id: str | None = None,
    env: dict[str, str] | None = None,
    extra_prompt: str = '',
    title: str = '',
    memory: Any = None,
    user_id: str = '',
    confined: bool = True,
    # None means "whatever ApprovalPolicy says", which is the only place the
    # default should live. Repeating it here is how these two ended up
    # meaning `False` after the policy changed — and under the new policy
    # `False` is a refusal rather than a prompt, so every session built
    # without an explicit config silently lost the ability to buy anything.
    allow_purchases: bool | None = None,
    allow_credentials: bool | None = None,
    allow_messages: bool | None = None,
    web: Any = None,
    browser: Any = None,
    stage: Any = None,
    media: Any = None,
    system: bool = False,
    # The config, for the tools whose availability depends on what this
    # install has configured rather than on a flag the caller set. `None`
    # means no mail or calendar tool at all.
    mail: Any = None,
    calendar: Any = None,
    toolset: list[str] | None = None,
    mcp: Any = None,
    checkpoints: Any = None,
    # Subagents and skills, on unless turned off. `lsp` is the language
    # servers this machine has (openmirror.agent.lsp.find_servers), or None.
    agents: bool = True,
    skills: bool = True,
    lsp: list[Any] | None = None,
    compact_at: int = 0,
    # Where a person's own skills and agent definitions are looked for. None
    # looks only in the project and in what ships with openmirror — which is
    # what a test wants, since a test that passed because of whatever happens
    # to be in someone's home directory has tested their home directory.
    home: Path | None = None,
) -> AgentSession:
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ValueError(f'{root_path}: working root is not a directory')

    permissions = {
        name: value
        for name, value in (
            ('allow_purchases', allow_purchases),
            ('allow_credentials', allow_credentials),
            ('allow_messages', allow_messages),
        )
        if value is not None
    }
    policy = ApprovalPolicy(mode=Mode(mode), **permissions)
    chosen = tools if tools is not None else default_tools()

    # In read-only mode the tools that can change things are left out entirely
    # rather than registered and refused. A model that can see `write_file`
    # will keep proposing it and spend the turn being told no.
    if policy.mode is Mode.READ_ONLY:
        chosen = [t for t in chosen if t.name not in FILE_WRITERS]

    # Memory is added as tools and as a per-turn hook, and only when this
    # person has switched it on — so a model in a session without memory is
    # never shown a `remember` tool it will be refused for using.
    context_provider = None
    on_user_text = None
    if memory is not None and user_id and memory.settings(user_id).enabled:
        from openmirror.agent.tools.memory import RecallTool, RememberTool

        chosen = [*chosen, RememberTool(memory, user_id), RecallTool(memory, user_id)]

        async def context_provider(text: str) -> str:  # noqa: F811
            return await memory.context_for(user_id, text)

        async def on_user_text(text: str) -> None:  # noqa: F811
            await memory.capture(user_id, text, source='chat')

    if web is not None:
        chosen = [*chosen, *web_tools(web)]
    if mail is not None:
        chosen = [*chosen, *mail_tools(mail)]
    if calendar is not None:
        chosen = [*chosen, *calendar_tools(calendar)]
    if browser is not None:
        chosen = [*chosen, *browser_tools(browser)]
    if stage is not None:
        chosen = [*chosen, *desktop_tools(stage)]
    if media is not None:
        chosen = [*chosen, *media_tools(media)]
    if system:
        chosen = [*chosen, *system_tools()]
    if mcp is not None:
        chosen = [*chosen, *mcp_tools(mcp)]

    found_skills: dict[str, Any] = {}
    if skills:
        from openmirror.agent import skills as skills_mod
        from openmirror.agent.tools.skill import SkillTool

        found_skills = skills_mod.discover(root_path, home=home)
        # The tool only when there is something for the model to load. A
        # person-only skill still runs from `/name` without it.
        if any(s.model_invocable for s in found_skills.values()):
            chosen = [*chosen, SkillTool(found_skills)]

    after_write: list[Any] = []
    if lsp:
        from openmirror.agent.lsp import LspPool
        from openmirror.agent.tools.lsp import LspTool

        pool = LspPool(root_path, list(lsp))
        chosen = [*chosen, LspTool(pool)]
        after_write.append(pool.after_write)

    # Narrowed after everything has been added, so a group name means the
    # same thing whichever capabilities happen to be attached.
    allowed = resolve_toolset(toolset or [])
    if allowed is not None:
        chosen = [t for t in chosen if t.name in allowed]

    # Agents last, because which kinds are worth offering depends on the tools
    # they would be given — and those are the ones that survived narrowing.
    kinds: dict[str, Any] = {}
    if agents and (allowed is None or 'agent' in allowed):
        from openmirror.agent import agents as agents_mod
        from openmirror.agent.tools.agent import AgentTool

        kinds = agents_mod.offered(agents_mod.load(root_path, home=home), {t.name: t for t in chosen})
        if kinds:
            chosen = [*chosen, AgentTool(kinds)]

    # The way out of plan mode belongs to the mode, not to a toolset: a
    # session narrowed to its files can still be switched to planning, and a
    # plan nobody can approve is a session stuck reading for ever. It is only
    # shown to the model while the session is planning.
    if not any(t.name == 'propose_plan' for t in chosen):
        chosen = [*chosen, ProposePlanTool()]

    # What this session actually has, for the prompt. Derived from the tools
    # that ended up on it rather than from the arguments, so a capability that
    # was asked for and could not be built — or was narrowed away — is not
    # claimed.
    names = {t.name for t in chosen}
    capabilities = [
        name for name, marker in (
            ('browser', 'browser_navigate'),
            ('desktop', 'desktop_screenshot'),
            ('media', 'generate_image'),
            ('system', 'system_info'),
            ('memory', 'recall'),
            ('todo', 'todo'),
            ('agents', 'agent'),
            ('skills', 'skill'),
            ('lsp', 'lsp'),
            ('git', 'git'),
            ('mail', 'mail'),
            ('calendar', 'calendar'),
        )
        if marker in names
    ]

    project = prompt_mod.project_context(root_path)
    context = f'{project}\n\n{extra_prompt}' if project and extra_prompt else (project or extra_prompt)

    return AgentSession(
        session_id=session_id,
        root=root_path,
        provider=provider,
        model=model,
        effort=effort,
        tools=chosen,
        policy=policy,
        system_prompt=prompt_mod.build(
            root_path,
            policy=policy.describe(),
            extra=context,
            confined=confined,
            capabilities=capabilities,
        ),
        env=env,
        title=title,
        context_provider=context_provider,
        on_user_text=on_user_text,
        confined=confined,
        browser=browser,
        checkpoints=checkpoints,
        stage=stage,
        agent_kinds=kinds,
        skills=found_skills,
        compact_at=compact_at,
        after_write=after_write,
        project_context=project,
    )

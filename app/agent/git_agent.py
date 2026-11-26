"""The git agent: runs ONE turn of the chat for ONE client session, on any worker.

Stateless per job:
  1. fresh copy of the client's repo in its own folder  (work_root/<client_id>/<session_id>/repo)
  2. Claude conversation resumed from PostgreSQL         (PostgresSessionStore)
  3. Claude reads/edits ONLY that copy; OUR tools check, build, commit and push
  4. the folder is deleted when the job ends
"""
import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from app.agent.git_ops import Git, GitError, rmtree, run, safe_env
from app.agent import copied_menu
from app.agent.prompt import BUILD_FAILED_MESSAGE, KICKOFF, NOTHING_ADDED_MESSAGE, STEP_LABELS, SYSTEM_PROMPT, find_existing, review_change, tidy_choice
from app.agent.session_store import PostgresSessionStore
from app.config import Settings
from app.platforms import Provider

_BLOCKED_DIRS = {".git", "node_modules", ".next", "dist", "build", ".vercel", ".netlify"}
_READ_TOOLS = {"Read", "Glob", "Grep"}
_EDIT_TOOLS = {"Edit", "MultiEdit"}


@dataclass
class TurnContext:
    """Everything one turn needs. The callbacks write to the DB (chat lines, pushed commits)."""
    settings: Settings
    client_id: str
    session_id: str
    agent_session_id: str
    provider: Provider
    token: str
    repo: str
    branch: str
    store: PostgresSessionStore
    say: Callable[[str, str], Awaitable[None]]                    # (who, text)
    record_change: Callable[[str, str, list[str]], Awaitable[None]]  # (sha, summary, files)
    before_push: Callable[[], Awaitable[None]] | None = None        # e.g. look at the live site first
    edited: set[str] = field(default_factory=set)


def session_dir(settings: Settings, client_id: str, session_id: str) -> Path:
    """Same path for the same session on every worker (the SDK keys its conversation by folder)."""
    return settings.work_root / _safe(client_id) / session_id


def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s)[:128]


def _inside(root: Path, p: Path) -> bool:
    return p == root or root in p.parents


class GitAgentTurn:
    def __init__(self, ctx: TurnContext):
        self.ctx = ctx
        base = session_dir(ctx.settings, ctx.client_id, ctx.session_id)
        self.base = base
        self.repo_dir = base / "repo"
        self.pushed = False          # after a push the system speaks to the client, not the agent
        self.build_failed = False    # a failed build check ends the turn: nothing is published
        self.gave_up = False         # nothing can be added: the system tells the client and closes the session
        self.built: set[Path] = set()   # app folders whose build passed since the last edit
        self.home = base / "home"
        self.git = Git(ctx.provider, ctx.token, ctx.repo, ctx.branch, self.repo_dir, self.home)

    # ---------- tools Claude can call (our code, not Claude, does git and builds) ----------
    def _tools(self):
        ctx, git, turn = self.ctx, self.git, self

        async def _diff() -> str:
            return await git("diff", "--", *sorted(ctx.edited)) if ctx.edited else ""

        @tool("show_my_changes", "Show the git diff of the files you edited (what will be published).", {})
        async def show_my_changes(_args):
            return {"content": [{"type": "text", "text": (await _diff())[:8000] or "No changes."}]}

        @tool("check_build", "Install dependencies and run the site's build in the given app folder "
                             "(relative to the repo root, e.g. '.' or 'client-site'). Returns success or the error.",
              {"app_dir": str})
        async def check_build(args):
            root = turn.repo_dir.resolve()
            app = (root / str(args.get("app_dir") or ".")).resolve()
            if not _inside(root, app):
                return _tool_error("app_dir must be inside the repo.")
            if not (app / "package.json").exists():
                return _tool_text("No package.json there: static site, no build needed.")
            ok, out = await turn._build(app)
            if ok:
                turn.built.add(app)
                return _tool_text(out)
            turn.build_failed = True
            return _tool_error("Build check failed. STOP now: do not edit or publish anything and write nothing "
                               "more - the system tells the client.\n" + out[-1500:])

        @tool("commit_and_push", "Commit your change and push it to the website's branch (the hosting platform "
                                 "then deploys it). Only call after show_my_changes and check_build.",
              {"message": str})
        async def commit_and_push(args):
            if not ctx.edited:
                return _tool_text("Nothing to commit: you haven't edited any file.")
            if turn.build_failed:
                return _tool_text("Not published: the build check failed. Stop and write nothing more.")
            unbuilt = [p for p in turn._apps_to_build() if p not in turn.built]
            if unbuilt:     # never publish a site whose build was not checked after the last edit
                rel = unbuilt[0].relative_to(turn.repo_dir.resolve()).as_posix() or "."
                return _tool_text(f"Not published: call check_build with app_dir '{rel}' first.")
            try:
                # code-level guard: ONLY the Team Portal link, ONLY in the nav / footer / side nav
                wide = await git("diff", "-U15", "--", *sorted(ctx.edited))
                problem = review_change(wide, turn._read_new)
                if problem:
                    return _tool_text(f"Not published: {problem} Fix it and try again.")
                files = sorted(ctx.edited)
                message = (str(args.get("message") or "Add Team Portal link")).strip()[:200]
                sha = await git.commit(message, files)
                if ctx.before_push:
                    await ctx.before_push()
                await ctx.say("step", "Deploying your website…")
                await git.push()
                await ctx.record_change(sha, message, files)
                turn.pushed = True
                ctx.edited.clear()
                # drop anything the install/build changed, so the copy matches what was pushed
                await git("checkout", "--", ".", check=False)
                await git("clean", "-fd", check=False)
                return _tool_text(f"Pushed {sha[:7]} ({', '.join(files)}). The hosting platform is deploying it "
                                  "now; the system checks the live site and tells the client when it is live.")
            except GitError as e:
                return _tool_error(f"Push failed: {e}")

        @tool("copy_to_all_pages", "COPIED menu only (plain HTML pages that each have their own copy of the menu): "
                                   "after you edited ONE page, this makes the same change in every other page.",
              {"page": str})
        async def copy_to_all_pages(args):
            page = str(args.get("page") or "").strip().lstrip("./")
            if page not in ctx.edited or Path(page).suffix.lower() not in copied_menu.PAGE_EXT:
                return _tool_error("First add the item to one .html page with Edit, then pass that page.")
            try:
                old = await git("show", f"HEAD:{page}")
                updated, skipped = await asyncio.to_thread(copied_menu.spread, turn.repo_dir, page, old)
            except (GitError, ValueError) as e:
                return _tool_error(str(e))
            ctx.edited.update(updated)
            turn.built.clear()
            msg = f"Same change made in {len(updated)} other page(s)."
            if skipped:
                msg += (" Could not place it in: " + ", ".join(skipped[:20]) +
                        ". Add the same item there yourself with Edit.")
            return _tool_text(msg)

        @tool("nothing_can_be_added", "Call when the Team Portal link can't be added anywhere (every place failed, the "
                                      "client said no, or the site has no top menu and no footer). The system tells "
                                      "the client and closes the session. Write nothing more after it.", {})
        async def nothing_can_be_added(_args):
            if turn.pushed:
                return _tool_error("A change was already published in this reply: do not call this.")
            turn.gave_up = True
            return _tool_text("OK. Write nothing more.")

        return [show_my_changes, check_build, commit_and_push, copy_to_all_pages, nothing_can_be_added]

    def _apps_to_build(self) -> set[Path]:
        """The app folder (nearest package.json above it) of every edited file. Plain HTML: none."""
        root = self.repo_dir.resolve()
        apps = set()
        for rel in self.ctx.edited:
            d = (root / rel).resolve().parent
            while _inside(root, d):
                if (d / "package.json").exists():
                    apps.add(d)
                    break
                if d == root:
                    break
                d = d.parent
        return apps

    def _read_new(self, rel_path: str) -> str | None:
        """The edited file's current content (only inside this client's repo copy)."""
        root = self.repo_dir.resolve()
        p = (root / rel_path).resolve()
        if not _inside(root, p) or not p.is_file():
            return None
        return p.read_text(encoding="utf-8", errors="replace")

    async def _build(self, app: Path) -> tuple[bool, str]:
        s = self.ctx.settings
        npm = "npm.cmd" if os.name == "nt" else "npm"
        install = [npm, "ci"] if (app / "package-lock.json").exists() else [npm, "install"]
        env = safe_env(self.home)
        for cmd in (install, [npm, "run", "build", "--if-present"]):
            code, out = await run(cmd, app, env, s.build_timeout_seconds)
            if code != 0:
                return False, out[-3000:]
        return True, "Build succeeded."

    # ---------- what Claude may touch: ONLY this client's repo copy ----------
    async def _can_use_tool(self, tool_name: str, tool_input: dict, _context):
        if tool_name.startswith("mcp__axolo__"):
            return PermissionResultAllow()
        root = self.repo_dir.resolve()
        if tool_name in _EDIT_TOOLS or tool_name in _READ_TOOLS:
            raw = tool_input.get("file_path") or tool_input.get("path") or str(root)
            pattern = tool_input.get("pattern") if tool_name == "Glob" else None
            if pattern and (pattern.startswith(("/", "\\", "~")) or Path(pattern).is_absolute()
                            or ".." in Path(pattern).parts):
                return PermissionResultDeny(message="You may only use the website's own files.")
            p = Path(raw)
            p = (p if p.is_absolute() else root / p).resolve()
            if not _inside(root, p):
                return PermissionResultDeny(message="You may only use the website's own files.")
            rel_parts = set(p.relative_to(root).parts)
            if tool_name in _EDIT_TOOLS:
                if _BLOCKED_DIRS & rel_parts:
                    return PermissionResultDeny(message="You may only edit the website's own source files.")
                if self.build_failed:
                    return PermissionResultDeny(message="The build check failed: stop, edit nothing more.")
                self.ctx.edited.add(p.relative_to(root).as_posix())
                self.built.clear()
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"{tool_name} is not available to you.")

    # ---------- one turn ----------
    async def run(self, message: str | None, note: str | None = None) -> None:
        ctx, s = self.ctx, self.ctx.settings
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            await self.git.clone()

            resume = await ctx.store.exists(ctx.agent_session_id)
            options = ClaudeAgentOptions(
                model=s.claude_model,
                system_prompt=SYSTEM_PROMPT,
                cwd=str(self.repo_dir),
                tools=["Read", "Glob", "Grep", "Edit"],
                mcp_servers={"axolo": create_sdk_mcp_server("axolo", tools=self._tools())},
                allowed_tools=["mcp__axolo__show_my_changes", "mcp__axolo__check_build",
                               "mcp__axolo__commit_and_push", "mcp__axolo__copy_to_all_pages",
                               "mcp__axolo__nothing_can_be_added"],
                disallowed_tools=["Bash", "Write", "WebFetch", "WebSearch", "Task", "NotebookEdit"],
                can_use_tool=self._can_use_tool,
                setting_sources=[],              # ignore any Claude settings on the worker
                max_turns=s.agent_max_turns,
                env={"ANTHROPIC_API_KEY": s.anthropic_api_key.get_secret_value(),
                     "CLAUDE_CONFIG_DIR": str(self.base / "claude")},
                session_store=ctx.store,
                **({"resume": ctx.agent_session_id} if resume else {"session_id": ctx.agent_session_id}),
            )
            prompt = message if message is not None else KICKOFF
            existing = await asyncio.to_thread(find_existing, self.repo_dir)
            if existing:   # checked by our code, not left to the model
                prompt = (f"[System check: the site ALREADY has a Team Portal link in: {', '.join(existing)}. "
                          "Do not add a second one. Only move it if the client asks.]\n\n" + prompt)
            if note:
                prompt = f"[{note}]\n\n{prompt}"

            # The agent's words are held until the turn ends: if it pushed, gave up or the build failed, the
            # system's own message is shown instead (no "No menu exists." / "deploying now" lines before it).
            said: list[str] = []
            errored = False
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for b in msg.content:
                            if isinstance(b, TextBlock) and b.text.strip():
                                said.append(tidy_choice(b.text))
                            elif isinstance(b, ToolUseBlock):
                                await ctx.say("step", STEP_LABELS.get(b.name, "Working…"))
                    elif isinstance(msg, ResultMessage) and msg.is_error:
                        errored = True
            if self.pushed:
                pass                                    # the system says "deploying" after the push
            elif self.build_failed:
                await ctx.say("agent", BUILD_FAILED_MESSAGE)
            elif self.gave_up:
                await ctx.say("agent", NOTHING_ADDED_MESSAGE)
            else:
                if said:        # only the final message (the question or answer), no "I'm adding…" chatter
                    await ctx.say("agent", said[-1])
                if errored:
                    await ctx.say("system", "The agent stopped with an error. Please try again.")
        finally:
            await asyncio.to_thread(rmtree, self.base)


def _tool_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def new_agent_session_id() -> str:
    return str(uuid.uuid4())

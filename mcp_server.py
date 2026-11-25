"""
mcp_server.py - LAYER 4: Aria's tools served over MCP (Model Context Protocol).

The voice agent calls the tools in-process (fastest). This file serves the SAME registry
(tools.py) over MCP, so any MCP client can use them with the same validation and permissions:
  - Claude Desktop / Claude Code: "who are my hot leads?", "what did Ravi ask last call?"
  - the Fortivex CRM or another internal agent

RBAC: the client gets the role in MCP_ROLE (.env, default sales_rep). Only the tools that role
may use are even listed, and every call is checked again and written to the audit log.

Run it (stdio transport, what Claude Desktop uses):
    .venv\\Scripts\\python mcp_server.py

Claude Desktop config (claude_desktop_config.json):
    {"mcpServers": {"fortivex-aria": {
        "command": "C:\\\\path\\\\to\\\\Fortivex Ai\\\\.venv\\\\Scripts\\\\python.exe",
        "args": ["C:\\\\path\\\\to\\\\Fortivex Ai\\\\mcp_server.py"],
        "env": {"MCP_ROLE": "sales_rep"}}}}
"""
import inspect
import json
import sys
from types import SimpleNamespace

import config
from crm import get_crm
from layers.observability import audit
from layers.rbac import ROLES
from tools import REGISTRY, run_tool, tools_for

# Tools that act on ONE customer need to know which lead: MCP clients pass the phone number.
NEEDS_LEAD = {"save_lead_info", "check_availability", "book_appointment", "handoff_to_human"}


def _context(phone: str | None):
    """A minimal stand-in for a call Session, so the same tool functions work over MCP."""
    lead_id = None
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        crm = get_crm()
        lead = crm.find_lead_by_phone(digits) or crm.create_lead(digits)
        lead_id = lead["id"]
    return SimpleNamespace(lead_id=lead_id, sources=[], events=[], handoff=False)


def call(name: str, args: dict, role: str) -> dict:
    """What every MCP tool does: audit -> build context -> run_tool (validation + RBAC inside)."""
    phone = args.pop("phone", None) if name in NEEDS_LEAD else None
    if name in NEEDS_LEAD and not phone:
        return {"error": "phone is required for this tool"}
    audit(f"mcp:{role}", role, "mcp_tool", f"{name} {json.dumps(args, ensure_ascii=False)[:300]}")
    session = _context(phone)
    return run_tool(session, name, args, role=role)


def _wrapper(tool, role: str):
    """Build a function whose signature = the tool's input model, so MCP shows a real JSON schema."""
    params = []
    if tool.name in NEEDS_LEAD:
        params.append(inspect.Parameter("phone", inspect.Parameter.KEYWORD_ONLY, annotation=str))
    for fname, field in tool.args.model_fields.items():
        default = inspect.Parameter.empty if field.is_required() else field.default
        params.append(inspect.Parameter(fname, inspect.Parameter.KEYWORD_ONLY, default=default,
                                        annotation=field.annotation))

    def fn(**kwargs) -> dict:
        return call(tool.name, kwargs, role)

    fn.__name__ = tool.name
    fn.__doc__ = tool.help + (" phone: the customer's phone number." if tool.name in NEEDS_LEAD else "")
    fn.__signature__ = inspect.Signature(params, return_annotation=dict)
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": dict}
    return fn


def build_server(role: str):
    from mcp.server.mcpserver import MCPServer

    if role not in ROLES:
        raise SystemExit(f"MCP_ROLE must be one of {sorted(ROLES)}")
    server = MCPServer(
        name="fortivex-aria",
        instructions=("Tools of Aria, Fortivex AI's sales voice agent: company knowledge, CRM leads, "
                      f"demo slots and bookings. Your role is '{role}'."),
    )
    for tool in tools_for(role):
        server.add_tool(_wrapper(tool, role), name=tool.name, description=tool.help)
    return server


if __name__ == "__main__":
    import builtins

    # stdout is the MCP channel in stdio mode. The app's modules log with print(), so send
    # every print() without an explicit file to stderr instead of corrupting the protocol.
    _print = builtins.print
    builtins.print = lambda *a, **k: _print(*a, **{"file": sys.stderr, **k})
    role = config.MCP_ROLE
    names = [t.name for t in tools_for(role)]
    print(f"[mcp] fortivex-aria role={role} tools={names}", file=sys.stderr)
    build_server(role).run("stdio")

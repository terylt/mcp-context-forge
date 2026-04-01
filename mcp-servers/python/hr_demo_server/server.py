# -*- coding: utf-8 -*-
"""Location: ./mcp-servers/python/hr_demo_server/server.py
Copyright 2026
SPDX-License-Identifier: Apache-2.0
Authors: Teryl Taylor

HR Demo MCP Server.

A simple MCP server with HR tools for demonstrating APL policy
enforcement through the ContextForge gateway.

Tools:
  - get_compensation: Returns employee compensation data (salary, SSN, etc.)
  - send_email: Sends an email (simulated — represents a forwarding action)
  - display_compensation: Returns compensation for display (view-only action)
  - get_directory: Returns employee directory info (non-sensitive)

Run with ContextForge translate (stdio → HTTP):
    python -m mcpgateway.translate --stdio "python mcp-servers/python/hr_demo_server/server.py" --port 9100

Or register directly as a gateway:
    POST /gateways
    { "name": "HR Demo", "url": "stdio://python mcp-servers/python/hr_demo_server/server.py" }
"""

import json
import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("hr_demo_server")

# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

EMPLOYEES = {
    "EMP-001234": {
        "employee_id": "EMP-001234",
        "name": "Jane Smith",
        "salary": 125000,
        "bonus": 15000,
        "ssn": "123-45-6789",
        "department": "Engineering",
        "internal_notes": "Performance review pending, do not disclose",
        "email": "jane.smith@corp.com",
        "title": "Senior Software Engineer",
    },
    "EMP-005678": {
        "employee_id": "EMP-005678",
        "name": "Bob Johnson",
        "salary": 95000,
        "bonus": 8000,
        "ssn": "987-65-4321",
        "department": "Marketing",
        "internal_notes": "Promotion candidate Q2",
        "email": "bob.johnson@corp.com",
        "title": "Marketing Manager",
    },
    "EMP-009012": {
        "employee_id": "EMP-009012",
        "name": "Alice Chen",
        "salary": 145000,
        "bonus": 20000,
        "ssn": "456-78-9012",
        "department": "Engineering",
        "internal_notes": "Team lead, retention risk",
        "email": "alice.chen@corp.com",
        "title": "Principal Engineer",
    },
}

SENT_EMAILS: list[dict] = []

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("hr-demo-server")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_compensation",
            description="Get compensation data for an employee. Returns salary, bonus, department, and optionally SSN. This tool returns sensitive PII data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee identifier (e.g., EMP-001234)"},
                    "include_ssn": {"type": "boolean", "description": "Whether to include SSN", "default": False},
                },
                "required": ["employee_id"],
            },
        ),
        Tool(
            name="send_email",
            description="Send an email (simulated). Represents a forwarding action.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body"},
                },
                "required": ["to", "subject", "body"],
            },
        ),
        Tool(
            name="display_compensation",
            description="Display compensation summary for an employee. View-only action.",
            inputSchema={
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string", "description": "Employee identifier"},
                },
                "required": ["employee_id"],
            },
        ),
        Tool(
            name="get_directory",
            description="Get employee directory listing. Non-sensitive data only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "department": {"type": "string", "description": "Optional department filter", "default": ""},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
    arguments = arguments or {}

    if name == "get_compensation":
        employee_id = arguments.get("employee_id", "")
        include_ssn = arguments.get("include_ssn", False)
        logger.info("get_compensation: employee_id=%s, include_ssn=%s", employee_id, include_ssn)

        employee = EMPLOYEES.get(employee_id)
        if not employee:
            return [TextContent(type="text", text=json.dumps({"error": f"Employee {employee_id} not found"}))]

        result = {
            "employee_id": employee["employee_id"],
            "name": employee["name"],
            "salary": employee["salary"],
            "bonus": employee["bonus"],
            "department": employee["department"],
            "title": employee["title"],
            "internal_notes": employee["internal_notes"],
        }
        if include_ssn:
            result["ssn"] = employee["ssn"]

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "send_email":
        to = arguments.get("to", "")
        subject = arguments.get("subject", "")
        body = arguments.get("body", "")
        logger.info("send_email: to=%s, subject=%s", to, subject)

        email = {"to": to, "subject": subject, "body": body, "status": "sent"}
        SENT_EMAILS.append(email)

        result = {"status": "sent", "message_id": f"msg-{len(SENT_EMAILS):04d}", "to": to, "subject": subject}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "display_compensation":
        employee_id = arguments.get("employee_id", "")
        logger.info("display_compensation: employee_id=%s", employee_id)

        employee = EMPLOYEES.get(employee_id)
        if not employee:
            return [TextContent(type="text", text=json.dumps({"error": f"Employee {employee_id} not found"}))]

        result = {
            "employee_id": employee["employee_id"],
            "name": employee["name"],
            "department": employee["department"],
            "title": employee["title"],
            "salary_band": "senior" if employee["salary"] >= 120000 else "mid" if employee["salary"] >= 80000 else "junior",
            "has_bonus": employee["bonus"] > 0,
        }
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_directory":
        department = arguments.get("department", "")
        logger.info("get_directory: department=%s", department)

        entries = []
        for emp in EMPLOYEES.values():
            if department and emp["department"].lower() != department.lower():
                continue
            entries.append({
                "name": emp["name"],
                "department": emp["department"],
                "title": emp["title"],
                "email": emp["email"],
            })
        return [TextContent(type="text", text=json.dumps(entries, indent=2))]

    else:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        logger.info("HR Demo MCP Server starting (stdio)")
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

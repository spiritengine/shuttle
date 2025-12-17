#!/usr/bin/env python3
"""
shuttle export - Export CC sessions to markdown

This is a standalone prototype. To integrate with shuttle, the export_session()
function can be called from bash via Python, or this logic can be ported to
pure bash/jq for consistency with the rest of shuttle.

Usage:
    python3 export_prototype.py <session-file> [--last N] [--json] [--with-tools]
    python3 export_prototype.py --help

Examples:
    # Export a session file to markdown
    python3 export_prototype.py ~/.claude/projects/-home-patrick-projects-shuttle/abc123.jsonl

    # Only last 20 messages
    python3 export_prototype.py session.jsonl --last 20

    # Include tool usage summaries
    python3 export_prototype.py session.jsonl --with-tools
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def export_session(
    session_file: Path,
    last_n: Optional[int] = None,
    with_tools: bool = False,
    as_json: bool = False
) -> str:
    """Convert a CC session JSONL file to markdown.

    Args:
        session_file: Path to the .jsonl session file
        last_n: Only process last N lines (messages + internal)
        with_tools: Include summaries of tool usage
        as_json: Return cleaned JSON instead of markdown

    Returns:
        Formatted markdown string (or JSON if as_json=True)
    """

    lines = session_file.read_text().strip().split('\n')

    if last_n:
        lines = lines[-last_n:]

    # Find first user message for metadata
    session_id = "unknown"
    cwd = "unknown"
    date = "unknown"
    git_branch = ""

    for line in lines:
        try:
            msg = json.loads(line)
            if msg.get('type') == 'user' and msg.get('message', {}).get('role') == 'user':
                session_id = msg.get('sessionId', 'unknown')
                cwd = msg.get('cwd', 'unknown')
                git_branch = msg.get('gitBranch', '')
                timestamp = msg.get('timestamp', '')
                date = timestamp.split('T')[0] if timestamp else 'unknown'
                break
        except json.JSONDecodeError:
            continue

    project = Path(cwd).name if cwd != 'unknown' else 'unknown'

    # If JSON format requested, build structured data
    if as_json:
        return export_as_json(lines, session_id, cwd, project, date, git_branch)

    # Build markdown output
    output = []
    output.append(f"# Session: {session_id}")
    output.append(f"**Project:** {project}")
    output.append(f"**Directory:** {cwd}")
    if git_branch:
        output.append(f"**Branch:** {git_branch}")
    output.append(f"**Date:** {date}")
    output.append("")
    output.append("---")
    output.append("")

    pending_tools = []  # Collect tool uses for --with-tools

    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get('type', '')
        role = msg.get('message', {}).get('role', '')

        # Skip non-message entries
        if msg_type not in ('user', 'assistant'):
            continue

        content = msg.get('message', {}).get('content', '')

        if msg_type == 'user' and role == 'user':
            # Extract user text content
            text = extract_user_text(content)

            # Skip empty messages (pure tool_result)
            if text.strip():
                output.append("## 👤 User")
                output.append("")
                output.append(text)
                output.append("")

        elif msg_type == 'assistant' and role == 'assistant':
            # Extract assistant text content and optionally tool usage
            text, tools = extract_assistant_content(content, with_tools)

            # Skip empty messages (pure tool_use)
            if text.strip() or (with_tools and tools):
                output.append("## 🤖 Claude")
                output.append("")
                if text.strip():
                    output.append(text)
                    output.append("")
                if with_tools and tools:
                    for tool in tools:
                        output.append(f"> *{tool}*")
                    output.append("")

    return '\n'.join(output)


def extract_user_text(content) -> str:
    """Extract text content from user message (string or array format)."""
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and block.get('type') == 'text':
                texts.append(block.get('text', ''))
        return '\n'.join(texts)
    return ''


def extract_assistant_content(content, with_tools: bool = False) -> tuple[str, list[str]]:
    """Extract text and optionally tool summaries from assistant message.

    Returns:
        Tuple of (text_content, list_of_tool_descriptions)
    """
    texts = []
    tools = []

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                block_type = block.get('type', '')
                if block_type == 'text':
                    texts.append(block.get('text', ''))
                elif block_type == 'tool_use' and with_tools:
                    tool_name = block.get('name', 'unknown')
                    tool_input = block.get('input', {})
                    # Create a readable summary
                    if tool_name == 'Bash':
                        cmd = tool_input.get('description') or tool_input.get('command', '')[:50]
                        tools.append(f"Used Bash: {cmd}")
                    elif tool_name == 'Read':
                        path = tool_input.get('file_path', '')
                        tools.append(f"Read: {path}")
                    elif tool_name in ('Edit', 'Write'):
                        path = tool_input.get('file_path', '')
                        tools.append(f"{tool_name}: {path}")
                    elif tool_name == 'Glob':
                        pattern = tool_input.get('pattern', '')
                        tools.append(f"Glob: {pattern}")
                    elif tool_name == 'Grep':
                        pattern = tool_input.get('pattern', '')
                        tools.append(f"Grep: {pattern}")
                    else:
                        tools.append(f"Used {tool_name}")

    return '\n'.join(texts), tools


def export_as_json(lines: list, session_id: str, cwd: str, project: str,
                   date: str, git_branch: str) -> str:
    """Export session as cleaned JSON structure."""
    messages = []

    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get('type', '')
        role = msg.get('message', {}).get('role', '')

        if msg_type not in ('user', 'assistant'):
            continue

        content = msg.get('message', {}).get('content', '')

        if msg_type == 'user' and role == 'user':
            text = extract_user_text(content)
            if text.strip():
                messages.append({
                    'role': 'user',
                    'content': text,
                    'timestamp': msg.get('timestamp', '')
                })

        elif msg_type == 'assistant' and role == 'assistant':
            text, _ = extract_assistant_content(content, with_tools=False)
            if text.strip():
                messages.append({
                    'role': 'assistant',
                    'content': text,
                    'timestamp': msg.get('timestamp', '')
                })

    result = {
        'session_id': session_id,
        'project': project,
        'directory': cwd,
        'branch': git_branch,
        'date': date,
        'messages': messages
    }

    return json.dumps(result, indent=2)


def print_help():
    print(__doc__)


if __name__ == '__main__':
    if len(sys.argv) < 2 or '--help' in sys.argv or '-h' in sys.argv:
        print_help()
        sys.exit(0 if '--help' in sys.argv or '-h' in sys.argv else 1)

    session_file = Path(sys.argv[1])
    last_n = None
    with_tools = '--with-tools' in sys.argv
    as_json = '--json' in sys.argv

    if '--last' in sys.argv:
        idx = sys.argv.index('--last')
        if idx + 1 < len(sys.argv):
            last_n = int(sys.argv[idx + 1])

    if not session_file.exists():
        print(f"Error: File not found: {session_file}", file=sys.stderr)
        sys.exit(1)

    print(export_session(session_file, last_n, with_tools, as_json))
